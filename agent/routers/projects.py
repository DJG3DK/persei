"""Projects: onboarding, creation, removal, archives and deploy keys.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-27. The
routes are unchanged -- tests/test_route_inventory.py and
tests/test_repo_scope.py pin every path, method, guard and repo check. All
of them are admin-only (`auth.require_admin` inside the handler, after
`require_full_auth`): a project entry names host paths and credential files.

agent/provisioning.py, agent/project_removal.py and agent/deploy_keys.py are
imported, not moved: docs/todo.md says provisioning.py and history_index.py
never travel with a seam, and the reason is in provisioning.py's docstring.

The three helpers the routes share take the app explicitly --
`_running_repos(app)`, `_provision_from_report(app, ...)`,
`_create_project(app, ...)` -- because importing `app` back from server.py,
which includes this router, would be a cycle. Through it they reach the
store, the auth pool, the config and `app.state.find_planning_meta`, the
planning lookup that stays in server.py with the turn machinery. server.py's
`_create_project_from_fields` binds `_create_project` to the app for the
planning router's new-project decision. The one route left in server.py that
reads a checkout's remote, POST /api/github/repos, reaches
`_project_remote_slugs` here for the same reason. PROJECTS is read off
`agent.config` at call time, so a test that swaps it is seen.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import subprocess
from types import SimpleNamespace
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from agent import audit, auth, cartographer, github_settings, history_index, live_state, paths
from agent import config as agent_config
from agent.auth import User, require_full_auth
from agent.backends import backend_for_dsn
from agent.routers import audit_store
from agent.routers import tasks as tasks_routes

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["projects"])


class RemoveProjectRequest(BaseModel):
    # What to do with everything the agent LEARNED about this project.
    memory: Literal["archive", "delete"] = "archive"
    # And what to do with the checkout. `keep` is the default and the rule the
    # removal module is built around; `delete` is for a repository Persei
    # cloned by itself and the operator never wanted, and is refused unless
    # the server can show that nothing would be lost by it.
    files: Literal["keep", "delete"] = "keep"


# ---------------------------------------------------------------------------
# Project onboarding wizard (agent/provisioning.py)
#
# Admin-only, and deliberately three separate calls -- detect, then confirm,
# then provision. The middle step is not ceremony: detection can propose a
# check command that would run against a live production service, and the
# only reliable filter for that is a human who knows the system. See
# agent/provisioning.py's docstring.
# ---------------------------------------------------------------------------


class DetectProjectRequest(BaseModel):
    path: str


class ProvisionProjectRequest(BaseModel):
    # Only the path and the operator's answers cross the wire. `live` and
    # `sandbox` are deliberately NOT accepted: they were a filesystem write
    # primitive supplied by the client. The server re-runs detection and
    # derives both, then confirms the answers are a subset of what it just
    # proposed (agent/provisioning.validate_choices).
    path: str
    choices: dict
    grant_access: bool = True
    # The filename of an archive to restore into the new project, from the
    # `archives` list the detect step returned. A name, never a path -- see
    # project_removal.read_archive for the containment that enforces.
    restore_archive: str | None = None


async def _running_repos(app) -> set[str]:
    """Which projects have work in flight right now.

    Removing a project underneath a running task would pull its worktree out
    from under the agent mid-edit and leave a half-finished branch nobody
    owns, so removal refuses instead. Resolving each running task's repo from
    its checkpoint is a handful of reads; there are never many.
    """
    repos: set[str] = set()
    for task_id in list(live_state.running_tasks):
        repo = await tasks_routes._resolve_task_repo(app, task_id)
        if repo:
            repos.add(repo)
    for session_id in list(live_state.running_planning_turns):
        # find_planning_meta returns (repo, meta), the shape every planning
        # route destructures. Reading it as a dict here raised AttributeError
        # and turned every removal and move into a 500 while any planning
        # turn was open (2026-09-29).
        repo, _meta = await app.state.find_planning_meta(session_id)
        if repo:
            repos.add(repo)
    return repos


@router.get("/api/projects/archives")
async def list_project_archives(user: User = Depends(require_full_auth)):
    """Archived memory from removed projects, newest first."""
    auth.require_admin(user)
    from agent import project_removal  # noqa: PLC0415
    return {"archives": project_removal.list_archives()}


@router.delete("/api/projects/archives/{filename}")
async def delete_project_archive(filename: str, user: User = Depends(require_full_auth)):
    """Throw away one archive. Separate from removing a project so that
    forgetting a project and forgetting what it knew are two decisions."""
    auth.require_admin(user)
    from agent import project_removal  # noqa: PLC0415
    try:
        project_removal.delete_archive(filename)
    except project_removal.RemovalError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _checkout_verdict(name: str) -> dict:
    """Whether `name`'s checkout could be deleted along with the project.

    Split out so the answer the operator is shown and the answer the deletion
    acts on come from one place; the deletion asks again at the moment it
    would delete, because a working tree can change between the two.
    """
    from agent import project_removal  # noqa: PLC0415

    entry = agent_config.PROJECTS.get(name) or {}
    live = entry.get("live", "")
    secrets = (entry.get("review") or {}).get("secretFiles") or []
    runs = project_removal.runs_on_this_box(entry)
    removable, reason = project_removal.checkout_disposable(live, secrets, runs)
    return {"live": live, "removable": removable, "reason": reason}


@router.get("/api/projects/{name}/checkout")
async def project_checkout_endpoint(name: str, user: User = Depends(require_full_auth)):
    """What deleting this project's checkout would cost, before anyone picks.

    Asked for when the removal panel opens rather than with the project list:
    it is several git commands per project, and nobody needs the answer until
    they are standing in front of the choice.
    """
    auth.require_admin(user)
    if name not in agent_config.PROJECTS:
        raise HTTPException(404, f"no project named {name!r}")
    return await asyncio.to_thread(_checkout_verdict, name)


@router.delete("/api/projects/{name}")
async def remove_project_endpoint(request: Request, name: str, req: RemoveProjectRequest,
                                  user: User = Depends(require_full_auth)):
    """Take a project off the agent.

    What this does NOT do is the important half: the live repository is left
    exactly as it is -- every file, every branch the agent ever pushed to it,
    and its remote. Removing a project means Persei forgets it, not that
    anybody's code goes away. The only thing touched inside the live repo is
    `core.sshCommand`, which is unset because the agent set it when it minted
    the deploy key, and leaving it would point the operator's own git at a key
    file that no longer exists.

    Each step reports independently, for the same reason provisioning does: a
    failure after the worktree is gone must not read as "nothing happened".
    """
    auth.require_admin(user)
    from agent import deploy_keys, project_removal  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH, reload_projects  # noqa: PLC0415

    entry = agent_config.PROJECTS.get(name)
    if entry is None:
        raise HTTPException(404, f"no project named {name!r}")

    busy = await _running_repos(request.app)
    if name in busy:
        raise HTTPException(409, (
            f"{name} has work in flight -- stop the running task or planning turn first, "
            "or removing it would pull the workspace out from under the agent mid-edit"))

    live, sandbox = entry.get("live", ""), entry.get("sandbox", "")
    secret_files = (entry.get("review") or {}).get("secretFiles") or []
    runs_here = project_removal.runs_on_this_box(entry)

    # Checked here, before a single destructive step: a refusal after the
    # memory is archived and the worktree is gone is a half-removed project
    # and an operator with no idea which half.
    if req.files == "delete":
        ok, reason = await asyncio.to_thread(
            project_removal.checkout_disposable, live, secret_files, runs_here)
        if not ok:
            raise HTTPException(409, f"{live} cannot be deleted: {reason}")

    steps: list[dict] = []
    archived: str | None = None

    # Knowledge first: while the project is still configured, so a failure
    # here leaves it whole rather than half-removed and unreachable.
    store = getattr(request.app.state, "store", None)
    if store is not None:
        # Before anything is archived or purged. On Postgres the index is
        # where every already-pruned episode lives, so with no index object
        # in this process the archive would quietly omit those rows AND the
        # purge would leave every one of the project's rows behind in
        # agent_history_fts -- the project's own goal text surviving its
        # removal, which is exactly the leftover the removal tests exist to
        # catch. Refusing costs a restart; continuing costs both halves.
        if (backend_for_dsn(request.app.state.config.dsn) == "postgres"
                and history_index.default_index() is None):
            raise HTTPException(503, (
                f"the history index is not open in this process, so {name}'s searchable "
                "history could be neither archived nor removed -- nothing was removed; "
                "restart the server and try again"))
        if req.memory == "archive":
            try:
                doc = await project_removal.collect(store, name, history_index.default_index())
                path = await asyncio.to_thread(project_removal.write_archive, doc)
                archived = path.name
                steps.append({"step": "archive", "ok": True,
                              "detail": f"{doc['item_count']} item(s) saved to {path.name}"})
            except Exception:  # noqa: BLE001
                # Refuse rather than continue: the operator asked to keep this,
                # and deleting it anyway is the one mistake with no undo.
                logger.exception("remove: archiving %s failed", name)
                # The reason is in the log, not the response: an arbitrary
                # exception's text carries paths and internals that a caller
                # has no business seeing (CodeQL py/stack-trace-exposure).
                raise HTTPException(500, (
                    f"could not archive {name}'s memory, so nothing was removed "
                    "-- see the server log"))
        removed = await project_removal.purge(store, name, history_index.default_index())
        steps.append({"step": "memory", "ok": True,
                      "detail": f"{removed} item(s) {'archived and removed' if archived else 'deleted'}"})

    ok, detail = await asyncio.to_thread(project_removal.remove_worktree, live, sandbox)
    steps.append({"step": "workspace", "ok": ok, "detail": detail})

    try:
        deploy_keys.remove_key(name, live)
        steps.append({"step": "deploy-key", "ok": True,
                      "detail": "key deleted and the repo's core.sshCommand unset"})
    except Exception as e:  # noqa: BLE001 -- never fatal; the key is ours, not theirs
        steps.append({"step": "deploy-key", "ok": False,
                      "detail": _provisioning_public_error(e)})

    reviewer_state = paths.REPO_ROOT / "services" / "commit-reviewer" / "state.json"
    if project_removal.clear_reviewer_state(reviewer_state, name):
        steps.append({"step": "review-state", "ok": True, "detail": "last verdict cleared"})

    try:
        project_removal.remove_project_entry(_PROJECTS_CONFIG_PATH, name)
    except project_removal.RemovalError as e:
        raise HTTPException(500, str(e))
    reload_projects()
    steps.append({"step": "config", "ok": True,
                  "detail": f"{name} removed; the review services drop it on their next poll"})

    # Last, because everything above is recoverable and this is not.
    deleted = False
    if req.files == "delete":
        deleted, detail = await asyncio.to_thread(
            project_removal.delete_checkout, live, secret_files, runs_here)
        steps.append({"step": "checkout", "ok": deleted, "detail": detail})

    await audit.record(audit_store(request), actor=user.email, action="project.remove",
                       target=name, detail=f"memory {req.memory}, files {req.files}",
                       extra={"archive": archived, "checkout_deleted": deleted})

    return {"ok": True, "name": name, "steps": steps, "archive": archived,
            "live_untouched": None if deleted else live,
            "live_removed": live if deleted else None}


@router.get("/api/projects")
async def list_projects_config(user: User = Depends(require_full_auth)):
    """Every configured project with its full entry -- the wizard's landing
    view. Admin-only because the entries name host paths and credential
    filenames."""
    auth.require_admin(user)
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    return {
        "projects": agent_config.PROJECTS,
        "config_path": str(_PROJECTS_CONFIG_PATH),
        "restart_required_hint": (
            "A project added from the dashboard is live immediately, and the review "
            "and deploy services re-read projects.json on their next poll. A project "
            "written to this file by hand or by scripts/add_project.py needs "
            "`pm2 restart tektonix` before the agent process sees it."
        ),
    }


def _project_remote_slugs() -> dict[str, str]:
    """`owner/repo` (lowercased) -> project name, for every configured project
    whose checkout has a GitHub origin.

    Read from the checkouts rather than from projects.json, because how a
    project was added says nothing about where it lives now: one the operator
    typed a path for is just as likely to be on GitHub as one the agent
    cloned. Blocking calls -- run it in a thread.
    """
    import subprocess  # noqa: PLC0415

    from agent.tools import github_tools  # noqa: PLC0415

    known: dict[str, str] = {}
    for name, cfg in agent_config.PROJECTS.items():
        live = (cfg or {}).get("live")
        if not live:
            continue
        try:
            out = subprocess.run(
                ["git", "config", "--local", "--get", "remote.origin.url"],
                capture_output=True, text=True, cwd=live, timeout=10, check=False,
            )
        except Exception:  # noqa: BLE001 -- a project whose checkout is gone is not this route's problem
            continue
        slug = github_tools.repo_slug_from_remote((out.stdout or "").strip())
        if slug:
            known[slug.lower()] = name
    return known


async def _onboarded_as(slug: str, token: str | None) -> str | None:
    """The project a repository is ALREADY onboarded as, or None.

    Tries the slug as given, then asks GitHub what each configured project's
    remote is called now. A repository transferred to an organisation leaves
    every checkout made before the move pointing at the old path, which GitHub
    still serves by redirect -- so the remote works, the slug no longer
    matches, and without this the same repository can be onboarded twice, one
    copy live and one a fresh clone beside it.

    The resolving loop is bounded by the number of configured projects whose
    remote did not match outright, and only runs on the manual add path.
    """
    from agent import github_repos as gh_repos  # noqa: PLC0415

    known = await asyncio.to_thread(_project_remote_slugs)
    hit = known.get(slug.lower())
    if hit or not token:
        return hit
    for other, name in known.items():
        if other == slug.lower():
            continue
        current = await gh_repos.resolve_slug(token, other)
        if current and current.lower() == slug.lower():
            return name
    return None


class OnboardFromGitHubRequest(BaseModel):
    slug: str                    # owner/repo, from the repo list
    token_name: str | None = None
    ship: str | None = None      # push | pr; the default is asked for, not inferred


@router.post("/api/projects/onboard-github")
async def onboard_github_endpoint(request: Request, req: OnboardFromGitHubRequest,
                                  user: User = Depends(require_full_auth)):
    """Clone a repository the token can reach, and provision it in one step.

    The long way round -- clone, read the report, tick the boxes -- still
    exists and is what somebody wants for a project with unusual checks. This
    is for the common case: a repository the operator can see in the list,
    onboarded with exactly the answers the wizard would have pre-ticked.
    """
    auth.require_admin(user)
    from agent import github_settings, provisioning  # noqa: PLC0415

    slug = (req.slug or "").strip()
    if not provisioning.parse_github_source(slug):
        raise HTTPException(400, f"{slug!r} is not a GitHub repository")

    token = getattr(request.app.state.config, "github_token", None)
    if req.token_name:
        settings = await github_settings.load(request.app.state.store)
        entry = settings["tokens"].get(req.token_name)
        if not entry:
            raise HTTPException(404, f"no token named {req.token_name!r}")
        token = github_settings.decrypt_token(request.app.state.config, entry["enc"])

    # A repository that is already a project must not become a second one.
    # The list this slug was picked from is built when somebody opens it, and
    # a transfer in GitHub after that moves a project's remote out from under
    # it -- so the refusal belongs here, where the clone is, and not only in
    # the list. Without it, adding a repository already onboarded under its
    # old path clones it again next to the live checkout, and two projects
    # then point at one repository.
    duplicate = await _onboarded_as(slug, token)
    if duplicate:
        raise HTTPException(
            409,
            f"{slug} is already onboarded as {duplicate!r}"
            + ("" if duplicate.lower() == slug.split("/")[-1].lower()
               else " (its checkout still has the path this repository had before it moved)"),
        )

    try:
        path = await asyncio.to_thread(
            provisioning.clone_repository, slug, existing_names=list(agent_config.PROJECTS), token=token,
        )
        report = await asyncio.to_thread(
            provisioning.detect_project, path, existing_names=list(agent_config.PROJECTS)
        )
        choices = provisioning.validate_choices(report, provisioning.recommended_choices(report))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    # Asked for, not inferred. A repository the agent cloned defaults to
    # opening pull requests, but the operator chose that in the list.
    choices["ship"] = req.ship if req.ship in ("push", "pr") else "pr"
    if report.blockers:
        raise HTTPException(400, "; ".join(report.blockers))

    ok, steps = await _provision_from_report(request.app, report, choices, user, True)
    return {"ok": ok, "name": report.name, "path": path, "steps": steps,
            "ship": choices["ship"], "slug": slug}


@router.post("/api/projects/clone")
async def clone_project_endpoint(request: Request, req: DetectProjectRequest, user: User = Depends(require_full_auth)):
    """Clone a GitHub repository into an allowed root, then detect it.

    Separate from /detect on purpose: that one promises to create nothing, and
    an operator who pastes a URL expecting a look is entitled to that promise.
    This one says in its name that it writes.

    Everything after the clone is the ordinary onboarding path, against the
    path the clone produced -- the wizard, the worktree and projects.json do
    not know the directory arrived over the network.
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    source = req.path.strip()
    if not provisioning.parse_github_source(source):
        raise HTTPException(400, f"{source!r} is not a GitHub URL or owner/repo")
    try:
        path = await asyncio.to_thread(
            provisioning.clone_repository, source,
            # The project does not exist yet, so there is no per-project token
            # to prefer -- the environment fallback is the only one there is.
            existing_names=list(agent_config.PROJECTS), token=getattr(request.app.state.config, "github_token", None),
        )
        report = await asyncio.to_thread(
            provisioning.detect_project, path, existing_names=list(agent_config.PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    out = report.to_dict()
    out["cloned_to"] = path
    # Carried into provisioning so the entry's `ship` default reflects how the
    # project arrived. A repository the agent cloned is not one it was asked
    # to own, so it opens pull requests unless the operator says otherwise.
    out["cloned_from_github"] = True
    out["recommended_ship"] = "pr"
    from agent import project_removal  # noqa: PLC0415
    out["archives"] = project_removal.list_archives(report.name) if report.name else []
    return out


@router.post("/api/projects/detect")
async def detect_project_endpoint(req: DetectProjectRequest, user: User = Depends(require_full_auth)):
    """Read-only inspection of a candidate directory. Creates nothing.

    A GitHub URL is reported as such rather than treated as a path, so the
    wizard can offer to clone instead of failing with "no such directory".
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    if provisioning.parse_github_source(req.path.strip(), allow_slug=False):
        raise HTTPException(
            400,
            "that is a GitHub repository, not a path on this machine. "
            "Use Clone to bring it down first.",
        )
    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(agent_config.PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    from agent import project_removal  # noqa: PLC0415
    # A project removed earlier leaves its memory behind on purpose. Surfacing
    # it HERE is what closes the loop: the operator sees "there is archived
    # memory for a project called this" at the moment they are deciding to add
    # it, rather than discovering the file months later with no idea what it is.
    out = report.to_dict()
    out["archives"] = project_removal.list_archives(report.name) if report.name else []
    return out


@router.post("/api/projects/provision")
async def provision_project_endpoint(request: Request, req: ProvisionProjectRequest, user: User = Depends(require_full_auth)):
    """Create the worktree, write the config entry, and seed the agent's
    knowledge for this project. Each step reports independently: a failure
    after the worktree exists must not read as "nothing happened".
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    # Re-detect rather than trust the client's copy of the report: between the
    # wizard's two calls the directory may have changed, and a hand-made
    # request could otherwise assert facts (paths, commands) the server never
    # established.
    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(agent_config.PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    if report.blockers:
        raise HTTPException(400, "; ".join(report.blockers))

    name = report.name
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "invalid project name")
    if name in agent_config.PROJECTS:
        raise HTTPException(400, f"{name!r} is already configured")

    try:
        choices = provisioning.validate_choices(report, req.choices)
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))

    ok, steps = await _provision_from_report(request.app, report, choices, user, req.grant_access)
    if not ok:
        return {"ok": False, "steps": steps}

    # Onboarding hands an agent bash and write access to a directory, which
    # makes "who added this project, and when" a question worth being able to
    # answer later.
    if req.restore_archive:
        # After provisioning, never before: restoring memory into a project
        # whose worktree or config then failed to land would leave rows for a
        # project that does not exist.
        from agent import project_removal  # noqa: PLC0415
        try:
            doc = project_removal.read_archive(req.restore_archive)
            written = await project_removal.restore(request.app.state.store, name, doc,
                                                    history_index.default_index())
            steps.append({"step": "restore", "ok": True,
                          "detail": f"{written} item(s) restored from {req.restore_archive}"})
        except Exception as e:  # noqa: BLE001 -- the project is already live; this is additive
            logger.exception("provision: restoring %s failed", req.restore_archive)
            steps.append({"step": "restore", "ok": False,
                          "detail": _provisioning_public_error(e)})

    await audit.record(audit_store(request), actor=user.email, action="project.onboard",
                       target=name, detail=report.live)

    return {
        "ok": True,
        "steps": steps,
        "message": f"{name} is configured and live in this process.",
    }


def _provisioning_public_error(e: Exception) -> str:
    """What a step may say about a failure: a ProvisioningError's own
    message (written for the operator), otherwise a pointer to the log
    -- an arbitrary exception's text is not for the response (CodeQL
    py/stack-trace-exposure)."""
    from agent import provisioning  # noqa: PLC0415

    if isinstance(e, provisioning.ProvisioningError):
        return e.detail
    logger.exception("provisioning step failed")
    return "failed -- see the server log"


async def _provision_from_report(app, report, choices: dict, user: User,
                                 grant_access: bool, *,
                                 fresh_workspace: bool = False) -> tuple[bool, list[dict]]:
    """Everything after the operator's answers are validated: worktree,
    projects.json entry, in-process reload, knowledge seeding, access.

    Shared verbatim by /api/projects/provision (the wizard) and
    /api/projects/create (a repo this server just made), so a project that
    arrives by either door ends up wired identically. Returns (ok, steps);
    ok is False only when a step the project cannot exist without failed.

    `fresh_workspace` is set by the create door: that project has never run,
    so an existing workspace at its path is another project's leftover rather
    than its own, and adopting it would run every task against the wrong repo.
    """
    from agent import provisioning  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    name = report.name
    steps: list[dict] = []
    _public_error = _provisioning_public_error

    def _step(label: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": label, "ok": ok, "detail": detail})

    logger.info("onboarding: %s provisioning %s from %s", user.email, name, report.live)

    ok, detail = await asyncio.to_thread(
        functools.partial(provisioning.create_worktree, report.live, report.sandbox,
                          must_be_new=fresh_workspace))
    _step("worktree", ok, detail)
    if not ok:
        return False, steps

    entry = provisioning.config_from_choices(report.live, report.sandbox, choices)
    try:
        await asyncio.to_thread(provisioning.write_project_entry, _PROJECTS_CONFIG_PATH, name, entry)
        _step("config", True, f"wrote {name} to projects.json")
    except (provisioning.ProvisioningError, OSError, ValueError) as e:
        _step("config", False, _public_error(e))
        return False, steps

    # Load the new entry into the RUNNING process. Without this the project
    # exists in projects.json and nowhere else -- every consumer holds the
    # dict read at import, so the cartographer below would KeyError on it and
    # the project would stay invisible until a restart.
    from agent.config import reload_projects  # noqa: PLC0415

    reload_projects()
    _step("reload", True, f"{len(agent_config.PROJECTS)} projects now live in this process")

    # Knowledge seeding. Best-effort by design: a project whose map failed to
    # build is still a usable project and the operator can re-run the
    # cartographer. Never fail the whole onboarding over it.
    try:
        from agent.deep_agent import seed_memory  # noqa: PLC0415

        starter = (
            f"# {name} project memory\n\n"
            "Durable, cross-task facts about this project. The consolidator appends what "
            "it learns from completed tasks; add anything an agent must know before "
            "touching this repo.\n"
        )
        await seed_memory(name, app.state.store, starter)
        _step("memory", True, "seeded starter project memory")
    except Exception as e:  # noqa: BLE001 -- reported, never fatal
        _step("memory", False, _public_error(e))

    # Started, not awaited. Reading a whole repository is minutes of model
    # calls on anything real, and awaiting it here held the HTTP response open
    # for all of them -- past the browser's own timeout, which aborts the
    # request while the server carries on and finishes. The operator then sees
    # a failure next to a project that does in fact exist, and the obvious
    # next move is to add it again. The map is best-effort by design (a
    # project without one is a usable project), so it belongs off this path.
    live_state.spawn_background(
        cartographer.run_cartographer(app.state.config, name, app.state.store, force=True),
        f"cartographer:{name}",
    )
    _step("codebase-map", True,
          f"building in the background -- agents get it when it lands; "
          f"scripts/run_cartographer.py {name} re-runs it")

    if grant_access and user.allowed_repos is not None:
        try:
            await auth.update_user_access(app.state.auth_pool, user.id,
                                          [*user.allowed_repos, name])
            _step("access", True, f"granted {user.email} access to {name}")
        except Exception as e:  # noqa: BLE001
            _step("access", False, _public_error(e))

    return True, steps


class CreateProjectRequest(BaseModel):
    # `parent` is the directory the new repo is created UNDER, never the repo
    # path itself: the name is validated separately (one path component) and
    # the join is re-checked for containment, so a client cannot pick an
    # arbitrary location any more than the wizard's `path` can.
    name: str
    description: str = ""
    parent: str | None = None
    github: bool = False
    token_name: str | None = None


def _resolve_github_token(config, token_name: str | None) -> tuple[str, str | None]:
    """Returns (token, stored_name). A named stored token if one was asked
    for, else the GITHUB_TOKEN env fallback, else the single stored token if
    that is unambiguous. Raises the HTTP error the endpoint should answer
    with; the token itself never goes anywhere but the request headers in
    agent/github_repos.py.

    The unambiguous-stored fallback exists because the dashboard decides
    whether to offer "create a private GitHub repo" from
    GET /api/settings/github, which reports stored tokens AND the env one --
    so on a box with a token saved in Settings and no GITHUB_TOKEN (the
    normal shape, since Settings is the documented place to put it) the
    checkbox was offered and the request then died on "no GitHub token is
    configured". Stored-first also matches github_settings.token_for's own
    precedence for every other GitHub call.
    """
    tokens = github_settings.current()["tokens"]
    if token_name:
        entry = tokens.get(token_name)
        if not entry:
            raise HTTPException(400, f"no stored GitHub token named {token_name!r} (Settings -> GitHub)")
        return github_settings.decrypt_token(config, entry["enc"]), token_name
    if len(tokens) == 1:
        only = next(iter(tokens))
        return github_settings.decrypt_token(config, tokens[only]["enc"]), only
    if len(tokens) > 1 and not getattr(config, "github_token", None):
        raise HTTPException(400, (
            f"several GitHub tokens are stored ({', '.join(sorted(tokens))}) and none was chosen -- "
            "name one in the request, or set GITHUB_TOKEN"))
    if getattr(config, "github_token", None):
        return config.github_token, None
    raise HTTPException(400, "no GitHub token is configured (Settings -> GitHub)")


def _rollback_new_project(live: str, name: str, github_info: dict | None, steps: list[dict]) -> None:
    """Remove the directory this call created when a later step fails, so the
    retry the dashboard offers is a real retry.

    Without this, "create" was a one-shot: create_repository only cleans up
    after its own git failure, so a project whose detect/worktree/config step
    died left <parent>/<name> on disk, and the second attempt -- with the same
    name, from a form the UI deliberately keeps filled in -- hit "already
    exists" and could never succeed.

    Not when a GitHub repository was created: that one is not ours to throw
    away silently, and the local checkout is the only copy of its deploy-key
    config. The operator gets the path and onboards it with the wizard.
    """
    if github_info:
        steps.append({"step": "rollback", "ok": True,
                      "detail": f"kept {live} (its GitHub repo {github_info['full_name']} exists); "
                                "onboard it from Settings -> Projects, or delete both to start over"})
        return
    try:
        shutil.rmtree(live)
    except OSError as e:
        steps.append({"step": "rollback", "ok": False,
                      "detail": f"could not remove {live}: {e}"})
        return
    steps.append({"step": "rollback", "ok": True,
                  "detail": f"removed {live}, so {name} can be created again"})


@router.post("/api/projects/create")
async def create_project_endpoint(request: Request, req: CreateProjectRequest, user: User = Depends(require_full_auth)):
    """Start a project from nothing -- see _create_project, which the
    planner's confirm route (decide_planning_new_project) shares verbatim so
    a project arrives wired identically whichever door it came through."""
    auth.require_admin(user)
    return await _create_project(request.app, req, user)


async def _create_project(app, req: CreateProjectRequest, user: User) -> dict:
    """A git repo with one commit under an allowed root, optionally mirrored
    to a new PRIVATE GitHub repository, then provisioned exactly as the
    wizard would with the recommended answers. The caller has already
    checked the admin role.

    Order matters. The token is resolved before anything is created so a
    missing token is a clean 400 with no directory left behind; the GitHub
    steps run before detection so a push failure is reported alongside the
    local steps rather than losing the project; and the GitHub failure is a
    failed step, not an abort -- the repo on disk is real and usable, and
    the operator can connect it from the deploy-key panel later.
    """
    from agent import provisioning, github_repos, deploy_keys  # noqa: PLC0415

    try:
        name = provisioning.validate_project_name(req.name, list(agent_config.PROJECTS))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, e.detail)

    token: str | None = None
    stored_token_name: str | None = None
    if req.github:
        token, stored_token_name = _resolve_github_token(app.state.config, req.token_name)

    try:
        live = await asyncio.to_thread(
            provisioning.create_repository, req.parent, name,
            description=req.description, existing_names=list(agent_config.PROJECTS))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, e.detail)

    steps: list[dict] = [{"step": "repository", "ok": True,
                          "detail": f"initialised {live} with one commit on main"}]
    github_info: dict | None = None

    if req.github and token:
        # Host-side push -- see agent/github_repos.py for why this is allowed
        # here and nowhere an agent runs.
        try:
            created = await github_repos.create_private_repo(token, name, req.description)
            github_info = {"full_name": created["full_name"], "html_url": created["html_url"]}
            public_key = await asyncio.to_thread(
                github_repos.connect_origin, live, created["ssh_url"], name)
            await github_repos.add_deploy_key(token, created["full_name"],
                                              f"tektonix-{name}", public_key)
            ok, detail = await asyncio.to_thread(github_repos.push_initial, live, name)
            steps.append({"step": "github", "ok": ok,
                          "detail": f"{created['full_name']}: {detail}" if ok else detail})
        except (PermissionError, LookupError, ValueError, deploy_keys.DeployKeyError) as e:
            # These messages are written by github_repos/deploy_keys for the
            # operator and carry neither the token nor a response body.
            steps.append({"step": "github", "ok": False, "detail": str(e)[:400]})
        except httpx.HTTPError as e:
            logger.exception("github: creating the repository for %s failed", name)
            steps.append({"step": "github", "ok": False,
                          "detail": f"GitHub request failed ({type(e).__name__}) -- see the server log"})
        if stored_token_name and github_info:
            # So token_for(name) -- the inbox poller, the PR tools -- reaches
            # this repo with the same token that created it.
            try:
                await github_settings.save(app.state.store, app.state.config,
                                           {"projects": {name: {"token": stored_token_name}}})
                steps.append({"step": "github-token", "ok": True,
                              "detail": f"{name} uses the stored token {stored_token_name!r}"})
            except Exception as e:  # noqa: BLE001 -- reported, never fatal
                steps.append({"step": "github-token", "ok": False,
                              "detail": _provisioning_public_error(e)})

    try:
        report = await asyncio.to_thread(provisioning.detect_project, live,
                                         existing_names=list(agent_config.PROJECTS))
        if report.blockers:
            raise provisioning.ProvisioningError("; ".join(report.blockers))
        choices = provisioning.validate_choices(report, provisioning.recommended_choices(report))
    except provisioning.ProvisioningError as e:
        steps.append({"step": "detect", "ok": False, "detail": e.detail})
        _rollback_new_project(live, name, github_info, steps)
        return {"ok": False, "name": name, "live": live, "steps": steps, "github": github_info}
    steps.append({"step": "detect", "ok": True,
                  "detail": f"{len(report.checks)} check(s), {len(report.warnings)} warning(s)"})

    ok, provision_steps = await _provision_from_report(app, report, choices, user, True,
                                                       fresh_workspace=True)
    steps.extend(provision_steps)
    if not ok:
        # Only when nothing was registered: once the projects.json entry is
        # written the project exists, and removing its checkout underneath a
        # configured name is worse than leaving a half-provisioned one.
        if name not in agent_config.PROJECTS:
            _rollback_new_project(live, name, github_info, steps)
        return {"ok": False, "name": name, "live": live, "steps": steps, "github": github_info}

    await audit.record(audit_store(SimpleNamespace(app=app)), actor=user.email, action="project.create",
                       target=name, detail=live,
                       extra={"github": github_info["full_name"] if github_info else None})

    return {
        "ok": True,
        "name": name,
        "live": live,
        "steps": steps,
        "github": github_info,
        "message": f"{name} is created, configured and live in this process.",
    }


# ---------------------------------------------------------------------------
# Per-project deploy keys (agent/deploy_keys.py)
#
# The private half is write-only across this API: it can be installed,
# generated and replaced, and its fingerprint/public half can be read, but
# nothing here returns it. Admin-only, like every other credential surface.
# ---------------------------------------------------------------------------


class DeployKeyRequest(BaseModel):
    private_key: str


def _project_live_or_404(name: str) -> str:
    project = agent_config.PROJECTS.get(name)
    if not project:
        raise HTTPException(404, f"unknown project {name!r}")
    return project["live"]


@router.get("/api/projects/{name}/deploy-key")
async def get_deploy_key_status(name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        return (await asyncio.to_thread(deploy_keys.status, name, live)).to_dict()
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))


class MoveProjectRequest(BaseModel):
    live: str


@router.post("/api/projects/{name}/move")
async def move_project_endpoint(request: Request, name: str, req: MoveProjectRequest,
                                user: User = Depends(require_full_auth)):
    """Point a project at another checkout of the same repository.

    The path is held to the rules onboarding holds it to: absolute, under an
    allowed root, not the agent's own tree, a git checkout, and not a path
    another project already uses. When both the old and the new checkout
    name an origin, they must name the same repository; a project is its
    repository, not its folder. The operator's checkouts do not move.

    The agent's own workspace does follow. On a host install and in the
    bundle it is a git worktree of the live checkout (provisioning
    .create_worktree), and a worktree belongs to one repository: after the
    move its .git pointer still named the old checkout, the pointer check
    in tools/git.py refused every command, and nothing said why
    (2026-09-29). So a separate workspace is rebuilt against the new
    checkout. A task's own worktree and a parked task's branch live in the
    old repository the same way and cannot be carried, so the move refuses
    while any exist.
    """
    auth.require_admin(user)
    import os  # noqa: PLC0415
    from agent import deploy_keys, project_removal, provisioning, workspaces  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH, reload_projects  # noqa: PLC0415
    from agent.store_paging import all_items  # noqa: PLC0415
    from agent.tools import github_tools  # noqa: PLC0415

    entry = agent_config.PROJECTS.get(name)
    if entry is None:
        raise HTTPException(404, f"no project named {name!r}")
    busy = await _running_repos(request.app)
    if name in busy:
        raise HTTPException(409, f"{name} has work in flight -- stop it first")
    try:
        new = provisioning.assert_path_allowed(req.live.strip())
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    if not os.path.isdir(os.path.join(new, ".git")):
        raise HTTPException(400, f"{new} is not a git checkout")
    old = entry.get("live") or ""
    sandbox = entry.get("sandbox") or old
    if os.path.realpath(old) == new:
        return {"ok": True, "live": new, "sandbox": sandbox, "unchanged": True}
    for other, cfg in agent_config.PROJECTS.items():
        if other == name:
            continue
        for path in (cfg.get("live"), cfg.get("sandbox")):
            if path and os.path.realpath(path) == new:
                raise HTTPException(409, f"{new} is already {other}'s checkout")

    def origin_of(path: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", path, "config", "--local", "--get", "remote.origin.url"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return github_tools.repo_slug_from_remote(r.stdout) if r.returncode == 0 else None

    old_slug, new_slug = await asyncio.gather(asyncio.to_thread(origin_of, old), asyncio.to_thread(origin_of, new))
    if old_slug and new_slug and old_slug.lower() != new_slug.lower():
        raise HTTPException(409, f"{new} is a checkout of {new_slug}, not of {old_slug}; a project is its repository")

    separate_workspace = os.path.realpath(sandbox) != os.path.realpath(old)
    if separate_workspace:
        own = await asyncio.to_thread(workspaces.existing, name)
        if own:
            raise HTTPException(409, (
                f"{name} has {len(own)} task workspace(s) under the current checkout; "
                "finish or remove those tasks first"))
        parked = [i for i in await all_items(request.app.state.store, ("tasks", name))
                  if (i.value or {}).get("status") in ("awaiting_approval", "awaiting_merge", "escalated")]
        if parked:
            raise HTTPException(409, (
                f"{name} has {len(parked)} task(s) waiting on a decision, and their branches live in "
                "the current checkout; approve, resume or stop them first"))
        # Same path, new repository: the old registration goes through the
        # old checkout's git, the new worktree is added from the new one.
        ok, detail = await asyncio.to_thread(project_removal.remove_worktree, old, sandbox)
        if not ok:
            raise HTTPException(500, f"could not detach the workspace from {old}: {detail}")
        ok, detail = await asyncio.to_thread(provisioning.create_worktree, new, sandbox)
        if not ok:
            # Put the old one back so the project is not left without a
            # workspace; the projects file has not changed.
            await asyncio.to_thread(provisioning.create_worktree, old, sandbox)
            raise HTTPException(500, f"could not rebuild the workspace from {new}: {detail}")
    try:
        updated = provisioning.move_project_entry(_PROJECTS_CONFIG_PATH, name, new)
    except (provisioning.ProvisioningError, OSError, ValueError) as e:
        raise HTTPException(500, f"could not update the projects file: {e}")
    reload_projects()
    github_tools._slug_cache.pop(name, None)     # the answer came from the old checkout
    key_followed = await asyncio.to_thread(deploy_keys.follow_move, name, old, new)
    await audit.record(audit_store(request), actor=user.email, action="project.move", target=name,
                       detail=f"{old} -> {new}")
    return {"ok": True, "live": updated["live"], "sandbox": updated.get("sandbox"), "unchanged": False,
            "workspace_rebuilt": separate_workspace, "deploy_key_followed": key_followed}


@router.post("/api/projects/{name}/deploy-key")
async def install_deploy_key(name: str, req: DeployKeyRequest,
                             user: User = Depends(require_full_auth)):
    """Install a pasted private key for this project."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        st = await asyncio.to_thread(deploy_keys.install_key, name, live, req.private_key)
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))
    logger.info("deploy key installed for %s by %s", name, user.email)
    return st.to_dict()


@router.post("/api/projects/{name}/deploy-key/generate")
async def generate_deploy_key(request: Request, name: str, user: User = Depends(require_full_auth)):
    """Mint a fresh keypair. Preferred over pasting: the operator never
    handles the private half -- they copy the public half out of the
    response and register it on the remote."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        st = await asyncio.to_thread(deploy_keys.generate_key, name, live)
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))
    logger.info("deploy key generated for %s by %s", name, user.email)
    # A deploy key is push access to the real repository. The log line above
    # is in a file that rotates; this one is in the store.
    await audit.record(audit_store(request), actor=user.email, action="deploy_key.generate",
                       target=name, detail=st.to_dict().get("fingerprint"))
    return st.to_dict()


@router.post("/api/projects/{name}/deploy-key/test")
async def test_deploy_key(name: str, user: User = Depends(require_full_auth)):
    """Contact the remote exactly the way the post-merge push will."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    ok, detail = await asyncio.to_thread(deploy_keys.check_remote, name, live)
    return {"ok": ok, "detail": detail}


@router.delete("/api/projects/{name}/deploy-key")
async def delete_deploy_key(request: Request, name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    st = await asyncio.to_thread(deploy_keys.remove_key, name, live)
    logger.info("deploy key removed for %s by %s", name, user.email)
    await audit.record(audit_store(request), actor=user.email, action="deploy_key.delete", target=name)
    return st.to_dict()
