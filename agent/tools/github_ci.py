"""The base branch moves only on a green GitHub Actions run.

After the review service passes a commit and the operator approves it, a
project that ships by pushing does not fast-forward its base branch straight
away. The task branch goes to GitHub with a pull request against the base,
which is what starts a repository's `on: pull_request` workflows, and this
waits for every workflow run on that exact commit. All green: the caller
fast-forwards the base and pushes it, and GitHub closes the pull request as
merged, because its head is now on the base. Any red: the failing jobs go back
to the agent as feedback, and nothing lands.

Skipped, never failed, where there is nothing to wait for: no workflow files in
the commit, an origin that is not GitHub, no GitHub token for the project,
`"ci_gate": false` on the project in projects.json, or a benchmark project. A repository with workflows that start
no run for this commit (path filters, a trigger on other branches) is let
through after a grace period, and the log says so.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("tektonix")

API = "https://api.github.com"
_TIMEOUT = 20
# A conclusion that does not block: a skipped job is a condition that did not
# apply, and neutral is a workflow's own way of saying "nothing to report".
_PASSING = frozenset({"success", "skipped", "neutral"})
# How long a repository with workflows gets to start a run for the commit
# before the gate decides none is coming. Runs are created the moment the
# event lands, queued or not, so this is generous.
START_GRACE_S = 180


def latest_runs(runs: list[dict]) -> list[dict]:
    """One run per workflow and event: the newest.

    A workflow run superseded by a newer one for the same commit (a re-run, or
    a `concurrency: cancel-in-progress` group cancelling the older) says
    nothing about the commit, and its `cancelled` must not read as a failure.
    """
    newest: dict[tuple, dict] = {}
    for run in runs:
        key = (run.get("workflow_id") or run.get("name"), run.get("event"))
        if key not in newest or int(run.get("id") or 0) > int(newest[key].get("id") or 0):
            newest[key] = run
    return list(newest.values())


def verdict(runs: list[dict]) -> tuple[str, list[dict]]:
    """("none" | "pending" | "failed" | "passed", the failed runs).

    Failed as soon as one run has finished red: the rest cannot make the
    commit green, and the agent can start on the fix while they finish.
    """
    runs = latest_runs(runs)
    if not runs:
        return "none", []
    failed = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in _PASSING]
    if failed:
        return "failed", failed
    if all(r.get("status") == "completed" for r in runs):
        return "passed", []
    return "pending", []


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _refused(r: httpx.Response) -> str | None:
    if r.status_code in (401, 403, 404):
        return (f"GitHub refused to show this repository's Actions runs ({r.status_code}). The project's "
                f"token needs `Actions: read` (fine-grained) or `repo` (classic): Settings > GitHub. Or set "
                f"\"ci_gate\": false on the project in projects.json to merge without waiting for CI.")
    return None


async def _runs_for(client: httpx.AsyncClient, token: str, slug: str, sha: str) -> list[dict]:
    r = await client.get(f"{API}/repos/{slug}/actions/runs", headers=_headers(token),
                         params={"head_sha": sha, "per_page": 100})
    refused = _refused(r)
    if refused:
        raise PermissionError(refused)
    r.raise_for_status()
    return r.json().get("workflow_runs") or []


async def _failed_jobs(client: httpx.AsyncClient, token: str, slug: str, run: dict) -> list[dict]:
    """The red jobs of one run and the steps that failed in each -- what the
    agent needs to reproduce the failure locally. Falls back to the run itself
    when the jobs cannot be read."""
    fallback = [{"workflow": run.get("name", ""), "job": "", "steps": [],
                 "conclusion": run.get("conclusion"), "url": run.get("html_url", "")}]
    try:
        r = await client.get(f"{API}/repos/{slug}/actions/runs/{run['id']}/jobs", headers=_headers(token),
                             params={"filter": "latest", "per_page": 100})
        r.raise_for_status()
        jobs = r.json().get("jobs") or []
    except (httpx.HTTPError, KeyError, ValueError):
        return fallback
    out = [{
        "workflow": run.get("name", ""),
        "job": j.get("name", ""),
        "steps": [s.get("name", "") for s in j.get("steps") or []
                  if s.get("conclusion") not in _PASSING and s.get("conclusion") is not None],
        "conclusion": j.get("conclusion"),
        "url": j.get("html_url", ""),
    } for j in jobs if j.get("status") == "completed" and j.get("conclusion") not in _PASSING]
    return out or fallback


def describe_failures(failed: list[dict]) -> str:
    lines = []
    for f in failed:
        name = f"{f['workflow']} / {f['job']}" if f.get("job") else f["workflow"]
        steps = f" -- failed step(s): {', '.join(f['steps'])}" if f.get("steps") else ""
        lines.append(f"- {name} ({f.get('conclusion')}){steps}\n  {f.get('url', '')}")
    return "\n".join(lines)


async def gate_on_actions(project: str, branch: str, sha: str, title: str, *, timeout: int,
                          poll_interval: int = 20, start_grace: int = START_GRACE_S) -> dict:
    """Push `branch`, open its pull request, and wait for Actions on `sha`.

    {"ok": True, "skipped": why} -- nothing to wait for;
    {"ok": True, "passed": [...], "pull_request": url} -- green;
    {"ok": False, "reason": "failed", "failed": [...], "pull_request": url} -- red;
    {"ok": False, "reason": "timeout" | "unreadable" | "push" | "pull_request", "error": ...}
    """
    from agent.config import PROJECTS, load_config  # noqa: PLC0415
    from agent import github_repos, github_settings  # noqa: PLC0415
    from agent.tools import github_tools  # noqa: PLC0415
    from agent.tools.git import _git  # noqa: PLC0415
    from agent.tools.review_gate import push_to_github  # noqa: PLC0415

    cfg = PROJECTS.get(project) or {}
    live = cfg.get("live")
    if not live:
        return {"ok": True, "skipped": "project has no live checkout"}
    if cfg.get("ci_gate") is False:
        return {"ok": True, "skipped": "ci_gate is off for this project"}
    if cfg.get("benchmark"):
        return {"ok": True, "skipped": "benchmark project"}
    base = cfg.get("base_branch") or "main"

    listed = await _git(f"ls-tree --name-only {sha} .github/workflows/", live, timeout=15)
    workflows = [n for n in (listed["output"].split() if listed["ok"] else [])
                 if n.endswith((".yml", ".yaml"))]
    if not workflows:
        return {"ok": True, "skipped": "no GitHub Actions workflows in this commit"}

    remote = await _git("config --local --get remote.origin.url", live, timeout=15)
    origin = remote["output"].strip() if remote["ok"] else ""
    slug = github_tools.repo_slug_from_remote(origin) if origin else None
    if not slug:
        return {"ok": True, "skipped": "origin is not a GitHub repository"}

    cfg_obj = load_config()
    try:
        token = github_settings.token_for(github_settings.current(), cfg_obj, project)
    except Exception:  # noqa: BLE001 -- unreadable settings must not beat the env fallback
        token = getattr(cfg_obj, "github_token", None)
    if not token:
        # Skipped, not failed: an SSH project with a deploy key and no token
        # shipped before this gate existed, and must keep shipping. The log
        # line says main was not gated and what would gate it.
        return {"ok": True, "skipped": "no GitHub token for this project, so main is not gated on Actions "
                                       "(Settings > GitHub: a token with Actions: read and Pull requests: write)"}

    # Forced: the task branch is this task's alone, and after a rebase onto a
    # moved base it no longer fast-forwards from what was pushed last time.
    pushed = await push_to_github(live, origin, slug, f"+{branch}:refs/heads/{branch}", token)
    if not pushed["ok"]:
        return {"ok": False, "reason": "push", "error": pushed["error"]}

    try:
        pr = await github_repos.open_pull_request(
            token, slug, head=branch, base=base, title=title,
            body=(f"Opened by Persei for task branch `{branch}`. The review gate and the operator "
                  f"have passed `{sha[:12]}`; Persei fast-forwards `{base}` to it once GitHub Actions "
                  f"pass, which closes this pull request as merged."),
        )
    except (PermissionError, LookupError, ValueError, httpx.HTTPError) as e:
        return {"ok": False, "reason": "pull_request", "error": f"could not open a pull request: {e}"}
    pr_url = pr.get("url", "")

    started = time.monotonic()
    # A green read is trusted only when the next poll shows the same runs
    # green again. Workflows register their runs one at a time, so the first
    # poll after the push can find one finished workflow and none of the
    # others yet, and "every run on this commit passed" was true of a set
    # that was still growing (2026-09-29).
    confirmed: list[str] | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            elapsed = time.monotonic() - started
            try:
                runs = await _runs_for(client, token, slug, sha)
            except PermissionError as e:
                return {"ok": False, "reason": "unreadable", "error": str(e), "pull_request": pr_url}
            except httpx.HTTPError as e:
                # GitHub has bad minutes; a poll that fails is "not known yet".
                logger.warning("github actions poll for %s@%s failed (will retry): %s", slug, sha[:12], e)
                runs = None
            if runs is not None:
                state, failed = verdict(runs)
                if state == "failed":
                    details = []
                    for run in failed:
                        details.extend(await _failed_jobs(client, token, slug, run))
                    return {"ok": False, "reason": "failed", "failed": details, "pull_request": pr_url}
                if state == "passed":
                    passed = sorted({r.get("name", "") for r in latest_runs(runs)})
                    if confirmed == passed:
                        return {"ok": True, "passed": passed, "pull_request": pr_url}
                    confirmed = passed      # once more after poll_interval, then believe it
                if state == "none" and elapsed >= start_grace:
                    return {"ok": True, "skipped": f"no GitHub Actions run started for {sha[:12]} "
                                                   f"within {start_grace}s", "pull_request": pr_url}
            if elapsed >= timeout:
                return {"ok": False, "reason": "timeout", "pull_request": pr_url,
                        "error": f"GitHub Actions did not finish on {sha[:12]} within {timeout}s: {pr_url}"}
            await asyncio.sleep(poll_interval)
