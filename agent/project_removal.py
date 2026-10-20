"""Taking a project back off the agent, and putting it back later.

The rule this module is built around, and the reason it is not just three
lines of rmtree: **the live repository is never touched.** Removing a project
from Persei means the agent forgets it -- the worktree it built in, the
deploy key it minted, the memory it accumulated -- and means nothing at all
about the operator's own checkout, their branches, their remote, or their
code. A "remove" that deleted someone's repository because they wanted the
agent to stop watching it would be unrecoverable and entirely our fault.

The one exception is deliberate and is the opposite of destructive:
`core.sshCommand` is unset on the live repo, because the agent set it when it
minted the deploy key. Leaving it would point their git at a key file that no
longer exists and break every push they make by hand.

Archive or delete
-----------------
The agent's knowledge of a project is spread over six store namespaces: its
memory, its episodes, the skills it generated, its planning sessions, their
transcripts, and its task history. `archive` serialises all of it to one JSON
file and then removes the rows; `delete` removes them without the file.

The archive exists so that re-adding the same project later is a continuation
rather than a fresh start -- the codebase map, what the agent learned about
the project's conventions, and the history of what it already tried. It is a
plain file so an operator can see it, copy it, and delete it; the onboarding
wizard offers to restore it when a project of the same name is added again.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent import paths

logger = logging.getLogger("agent.project_removal")

ARCHIVE_DIR: Path = Path(os.environ.get("AGENT_ARCHIVE_DIR") or (paths.REPO_ROOT / "archives"))

# Every STORE namespace a project owns. Keyed by a label that survives into
# the archive file, so a restore does not depend on this tuple's order.
#
# (repo,) is the agent's memory for the project; the rest are named. If a new
# per-project namespace is ever added, it belongs here too -- tests/
# test_project_removal.py pins the list against a real provisioned project so
# a forgotten one shows up as leftovers rather than as a mystery months later.
#
# STORE namespaces only, and that word is load-bearing. A project also owns
# state that is not a store row at all, and this dict cannot reach it: rows
# in agent_history_fts (agent/history_index.py), the worktree, the deploy
# key, the reviewer's state file, its projects.json entry. Each of those has
# its own step in the removal path below or in the route that calls it. A
# future per-project TABLE will hit this same gap, so add it beside the
# history index rather than trying to express it here.
def namespaces(repo: str) -> dict[str, tuple[str, ...]]:
    return {
        "memory": (repo,),
        "episodes": ("episodes", repo),
        "skills": ("skills", repo),
        "planning": ("planning", repo),
        "planning_log": ("planning_log", repo),
        # agent/planning_log.py writes a build task's whole transcript here
        # and this list did not name it, so every removal since left the
        # transcripts behind -- 1.4 MB of them across four projects, rows
        # nothing could reach and nothing would ever clean up.
        "task_log": ("task_log", repo),
        "tasks": ("tasks", repo),
    }

# The key the history index's rows ride under in an archive document. Not a
# namespace label: those are store namespaces, and these rows are not in the
# store.
HISTORY_FTS_KEY = "history_fts"


class RemovalError(Exception):
    """Something the operator should see and can act on."""


def _safe_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise RemovalError(f"invalid project name {name!r}")
    return name


# ---------------------------------------------------------------------------
# archives
# ---------------------------------------------------------------------------

def _in_archive_dir(filename: str) -> Path:
    """A file directly inside ARCHIVE_DIR, or an error.

    paths.contained_file does the work: a single component, a normpath
    containment check, then a realpath one a symlink cannot slip past. This
    was spelled out inline with `Path.resolve()` and `.parent ==`, which reads
    the same to a person and is invisible to static analysis -- six of the
    repository's path-injection alerts were these two functions.
    """
    try:
        return paths.contained_file(ARCHIVE_DIR, filename)
    except paths.UnsafePath as e:
        raise RemovalError(f"invalid archive name {filename!r}: {e}") from e


def archive_path(repo: str, when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when if when is not None else time.time()))
    # _safe_name has already rejected separators; going through the same door
    # as the read side keeps one rule rather than two.
    name = f"{_safe_name(repo)}-{stamp}.json"
    if "/" in name or "\\" in name:
        raise RemovalError(f"invalid project name {repo!r}")
    return ARCHIVE_DIR / name


async def collect(store, repo: str, index=None) -> dict[str, Any]:
    """Read every namespace this project owns into one document.

    `index` is agent/history_index.py's, when there is one. Its rows are the
    only copy of an episode the pruner has already deleted from the store,
    so an archive without them is an archive of less history than the
    project actually had.
    """
    out: dict[str, Any] = {
        "project": repo,
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1,
        "namespaces": {},
    }
    for label, ns in namespaces(repo).items():
        rows = []
        try:
            for item in await store.asearch(ns, limit=1000):
                rows.append({"key": item.key, "value": item.value})
        except Exception:  # noqa: BLE001 -- a namespace that cannot be read is empty, not fatal
            logger.exception("archive: could not read %s for %s", label, repo)
        out["namespaces"][label] = rows
    out["item_count"] = sum(len(v) for v in out["namespaces"].values())
    out[HISTORY_FTS_KEY] = []
    if index is not None:
        # Deliberately NOT swallowed, unlike a namespace that will not read,
        # and the difference is which copy is the last one. A store
        # namespace still exists after this; these rows are the ONLY copy of
        # every episode the pruner already deleted, and purge() -- which
        # runs next -- DELETEs them. Swallowing it here wrote an archive
        # that said it was fine, reported the step ok, and walked straight
        # past the caller's refuse-to-continue guard, the one whose comment
        # says deleting it anyway is the one mistake with no undo.
        out[HISTORY_FTS_KEY] = await index.dump_project(repo)
    return out


def write_archive(doc: dict[str, Any], path: Path | None = None) -> Path:
    target = path or archive_path(doc["project"])
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic, like every other file this codebase replaces: a half-written
    # archive that a restore later reads as truth is worse than none.
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str))
    os.replace(tmp, target)
    return target


def list_archives(repo: str | None = None) -> list[dict[str, Any]]:
    """Every archive on disk, newest first. Cheap: the header is read, not the
    rows, so a panel listing them does not load megabytes of memory."""
    if not ARCHIVE_DIR.is_dir():
        return []
    out = []
    for f in sorted(ARCHIVE_DIR.glob("*.json"), reverse=True):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if repo is not None and doc.get("project") != repo:
            continue
        out.append({
            "file": f.name,
            "project": doc.get("project"),
            "archived_at": doc.get("archived_at"),
            "item_count": doc.get("item_count", 0),
        })
    return out


def read_archive(filename: str) -> dict[str, Any]:
    """Load one archive by FILENAME, never by a caller-supplied path: the name
    has to resolve to a file directly inside ARCHIVE_DIR, so a request cannot
    walk out of it."""
    target = _in_archive_dir(filename)
    if not target.is_file():
        raise RemovalError(f"no archive named {filename!r}")
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError) as e:
        raise RemovalError(f"{filename} is unreadable: {e}") from e


def delete_archive(filename: str) -> None:
    target = _in_archive_dir(filename)
    if not target.is_file():
        raise RemovalError(f"no archive named {filename!r}")
    target.unlink()


async def restore(store, repo: str, doc: dict[str, Any], index=None) -> int:
    """Write an archive's rows back, under `repo`.

    Restores under the CURRENT project name rather than the archived one, so
    re-adding a project at a different name still carries its memory across --
    and so a hand-edited archive cannot write into a namespace the operator
    did not ask for.
    """
    written = 0
    for label, ns in namespaces(repo).items():
        for row in doc.get("namespaces", {}).get(label) or []:
            key, value = row.get("key"), row.get("value")
            if not key:
                continue
            await store.aput(ns, key, value)
            written += 1
    if index is not None and doc.get(HISTORY_FTS_KEY):
        try:
            written += await index.restore_project(repo, doc[HISTORY_FTS_KEY])
        except Exception:  # noqa: BLE001 -- a project that comes back without its search index is still back
            logger.exception("restore: could not restore the history index for %s", repo)
    return written


async def purge(store, repo: str, index=None) -> int:
    """Remove every row this project owns."""
    removed = 0
    for ns in namespaces(repo).values():
        try:
            for item in await store.asearch(ns, limit=1000):
                await store.adelete(ns, item.key)
                removed += 1
        except Exception:  # noqa: BLE001 -- keep going; a stuck namespace must not strand the rest
            logger.exception("purge: could not clear %s for %s", ns, repo)
    if index is not None:
        try:
            removed += await index.forget_project(repo)
        except Exception:  # noqa: BLE001 -- same rule as a stuck namespace
            logger.exception("purge: could not clear the history index for %s", repo)
    return removed


# ---------------------------------------------------------------------------
# the on-disk half
# ---------------------------------------------------------------------------

def remove_worktree(live: str, sandbox: str) -> tuple[bool, str]:
    """Unregister the agent's workspace through git, then make sure it is gone.

    `git worktree remove` rather than rmtree because the live repo holds
    administrative files for every worktree of it; deleting the directory
    alone leaves the registration behind, and the next `worktree add` at the
    same path fails with "already registered".
    """
    if not os.path.isdir(sandbox):
        return True, "no workspace to remove"
    if os.path.isdir(live):
        r = subprocess.run(["git", "worktree", "remove", "--force", sandbox],  # noqa: S603,S607
                           cwd=live, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, f"removed {sandbox}"
        detail = (r.stderr or r.stdout).strip()[:300]
    else:
        detail = "the live repo is gone, so git cannot unregister it"
    # The live repo may itself have been moved or deleted by the operator.
    # The workspace is ours either way, so take it.
    try:
        shutil.rmtree(sandbox)
    except OSError as e:
        return False, f"{detail}; and the directory could not be removed: {e}"
    return True, f"removed {sandbox} directly ({detail})"


def clear_reviewer_state(state_path: Path, repo: str) -> bool:
    """Drop the project's row from the commit reviewer's state file.

    Left behind it is harmless but confusing: the reviewer's /projects would
    stop listing it while its last verdict sat in the file forever.
    """
    if not state_path.is_file():
        return False
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return False
    if repo not in data:
        return False
    data.pop(repo)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, state_path)
    return True


def remove_project_entry(projects_path: Path, repo: str) -> bool:
    """Take the project out of projects.json, atomically.

    The counterpart to provisioning.write_project_entry, and the step that
    actually makes the project gone: the two Node services re-read this file
    on every poll, so they stop reviewing and deploying it with no restart.
    """
    try:
        data = json.loads(projects_path.read_text())
    except (OSError, ValueError) as e:
        raise RemovalError(f"could not read {projects_path}: {e}") from e
    projects = data.get("projects") or {}
    if repo not in projects:
        return False
    projects.pop(repo)
    # test_env is keyed by project too (see projects.json's own shape).
    (data.get("test_env") or {}).pop(repo, None)
    tmp = projects_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, projects_path)
    return True


# ---------------------------------------------------------------------------
# the checkout itself
# ---------------------------------------------------------------------------
#
# Everything above leaves the live repository alone, and that is still the
# default and still the promise. This section is the one case where it is not
# what the operator wants: a repository Persei cloned by itself minutes ago,
# usually because a stale list offered to add something that was already
# there. Removing the project then leaves 16MB of clone behind and a shell is
# the only way to finish the job -- which is a manual step inside a flow the
# dashboard otherwise owns end to end.
#
# So deleting the checkout is offered, never assumed, and only when it can be
# shown that nothing would be lost: every commit is on a remote, there is
# nothing uncommitted, nothing stashed, and none of the secret files this
# project declared are sitting in it. Anything short of that is a refusal
# naming what is in the way, because the one mistake with no undo here is
# deleting a repository somebody still needed.


def _git(live: str, args: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git", *args], cwd=live,  # noqa: S603,S607
                           capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or "").strip()


def runs_on_this_box(entry: dict[str, Any] | None) -> str | None:
    """What this machine runs out of the project's checkout, if anything.

    Read from the project's own deploy config, which is the only place that
    knows. A checkout with a pm2 app or a restart command behind it is
    serving something right now: every commit in it can be on the remote and
    deleting the directory still takes a site down. "Nothing would be lost"
    and "nothing would break" are different questions, and this is the
    second one.
    """
    deploy = ((entry or {}).get("deploy") or {})
    apps = [a for a in (deploy.get("pm2Apps") or []) if a]
    if apps:
        return f"pm2 runs {', '.join(str(a) for a in apps)} from it"
    if deploy.get("restart"):
        return "this box restarts it after a merge"
    return None


def _pm2_serves(live: str) -> tuple[bool, str]:
    """Whether pm2 runs anything out of `live`. Returns (clear, reason).

    Asked of pm2 rather than of the project's config because the config is
    not where the answer lives: the projects this box has always served keep
    their pm2 apps in the review service's own JavaScript config, which the
    Python side never reads. Going by projects.json alone reported "nothing
    runs this" for exactly the checkouts whose deletion would take a site
    down.

    pm2 not being installed is a clear answer -- nothing here is run by it.
    pm2 being installed and unanswerable is not, and an unanswerable question
    before an irreversible delete is a no.
    """
    root = os.path.realpath(live)
    try:
        r = subprocess.run(["pm2", "jlist"],  # noqa: S603,S607
                           capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        return True, ""
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"pm2 could not be asked what runs from it ({type(e).__name__})"
    if r.returncode != 0:
        return False, "pm2 could not be asked what runs from it"
    try:
        apps = json.loads(r.stdout or "[]")
    except ValueError:
        return False, "pm2's reply could not be read"

    for app in apps if isinstance(apps, list) else []:
        env = app.get("pm2_env") or {}
        for key in ("pm_cwd", "pm_exec_path"):
            where = env.get(key) or ""
            if not where:
                continue
            real = os.path.realpath(where)
            if real == root or real.startswith(root + os.sep):
                name = app.get("name") or "something"
                return False, f"pm2 runs {name} from it"
    return True, ""


def checkout_disposable(live: str, secret_files: list[str] | None = None,
                        runs_here: str | None = None) -> tuple[bool, str]:
    """Whether deleting `live` would lose anything that is not also elsewhere.

    Returns (ok, reason); the reason is a sentence for the operator either
    way, because "you may not delete this" is only useful with the "because".

    The questions, in the order that makes a refusal most informative: does
    this machine run anything out of it, is it a git checkout at all, does it
    have a remote, is the tree clean, is anything stashed, is every commit on
    a local branch also on a remote, and is any file the project declared
    secret sitting in it. That last one is the gitignored `.env` case --
    `git status` will not mention it, and it is exactly the file whose loss is
    unrecoverable.
    """
    if runs_here:
        return False, f"{runs_here}, so deleting it would take that down"
    if not live or not os.path.isdir(live):
        return False, f"there is no directory at {live}"
    if not os.path.exists(os.path.join(live, ".git")):
        return False, "it is not a git checkout, so nothing in it is anywhere else"

    ok, remotes = _git(live, ["remote"])
    if not ok or not remotes:
        return False, "it has no git remote, so this checkout is the only copy"

    ok, dirty = _git(live, ["status", "--porcelain"])
    if not ok:
        return False, "git could not read its status, so it is not safe to assume anything"
    if dirty:
        n = len(dirty.splitlines())
        return False, f"it has {n} uncommitted change{'' if n == 1 else 's'}"

    _, stashed = _git(live, ["stash", "list"])
    if stashed:
        n = len(stashed.splitlines())
        return False, f"it has {n} stash{'' if n == 1 else 'es'}"

    # Commits reachable from a local branch and from no remote branch. A task
    # branch that was merged and never pushed as a branch does NOT show up
    # here -- its commits are in the branch that was pushed -- so this catches
    # real unpublished work rather than the agent's own bookkeeping.
    ok, unpushed = _git(live, ["log", "--branches", "--not", "--remotes", "--oneline"])
    if ok and unpushed:
        n = len(unpushed.splitlines())
        return False, f"it has {n} commit{'' if n == 1 else 's'} that are not on any remote"

    for rel in secret_files or []:
        if os.path.exists(os.path.join(live, rel)):
            return False, f"it holds {rel}, which git does not carry"

    # Last, because it costs a subprocess and every refusal above is cheaper.
    clear, why = _pm2_serves(live)
    if not clear:
        return False, f"{why}, so deleting it would take that down"

    return True, f"every commit is on a remote and nothing is uncommitted in {live}"


def delete_checkout(live: str, secret_files: list[str] | None = None,
                    runs_here: str | None = None) -> tuple[bool, str]:
    """Delete the live checkout, after checking again that it is safe to.

    The check runs here as well as wherever the operator was shown it: the
    answer is read off a working tree that anything could have written to in
    between, and this is the call that cannot be undone.
    """
    ok, reason = checkout_disposable(live, secret_files, runs_here)
    if not ok:
        return False, f"left {live} alone -- {reason}"
    try:
        shutil.rmtree(live)
    except OSError as e:
        return False, f"could not delete {live}: {e}"
    logger.info("removed checkout %s", live)
    return True, f"deleted {live}"
