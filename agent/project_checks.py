"""Checks for a project that shipped without any.

A brand-new project (the dashboard's "New project" flow, or an operator who
skipped the wizard's check step) has nothing for the reviewer to run. Its
first merge is therefore a review in name only -- and the moment that merge
lands is the first time the repository has real code to inspect, so it is
also the first moment detection can propose anything. This module runs that
detection once, right after a merge, for exactly the projects the REVIEWER
says have no checks, and writes the recommended set into projects.json.

Two things deliberately shape the code here:

* "Has checks" is the reviewer's answer, never projects.json's. The
  reviewer's built-in config (services/commit-reviewer/builtin-projects.local.js)
  wins key-by-key over projects.json, so a project can have checks the
  agent's config file cannot see. Asking projects.json would write a second,
  competing set over a hand-tuned one.
* Nothing in here may raise. The post-ship hook runs inside
  verify_and_ship's outer try/except, which turns ANY exception into an
  escalation of a task that has already shipped -- the same discipline
  tests/test_post_ship_cartographer.py pins for _refresh_codebase_map.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from agent import config as _config
from agent.config import PROJECTS, reload_projects
from agent.provisioning import ProvisioningError, detect_project
from agent.tools import review_gate as _review_gate
from agent.tools.review_gate import project_checks

logger = logging.getLogger(__name__)


def set_project_checks(projects_path: Path, name: str, checks: list[dict]) -> None:
    """Atomic update of ONE project's review.checks in projects.json.

    Same tmp + os.replace shape as provisioning.write_project_entry, for the
    same reason: the file is read by the API, the reviewer and the deploy
    service, and a half-written file breaks all three at once. Refuses when
    the entry already has checks -- this is a one-shot fill for an empty
    slot, never an overwrite of something an operator chose. Every other key
    of the entry is left exactly as it was.
    """
    if not checks:
        raise ProvisioningError(f"no checks to write for {name!r}")
    if not projects_path.exists():
        raise ProvisioningError(f"{projects_path} does not exist")
    data = json.loads(projects_path.read_text())
    projects = data.get("projects") or {}
    entry = projects.get(name)
    if entry is None:
        raise ProvisioningError(f"{name!r} is not in projects.json")
    review = entry.get("review")
    if not isinstance(review, dict):
        review = {}
        entry["review"] = review
    if review.get("checks"):
        raise ProvisioningError(
            f"{name!r} already has {len(review['checks'])} check(s) configured -- refusing to overwrite")
    review["checks"] = [dict(c) for c in checks]
    tmp = projects_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, projects_path)


def _log_entry(summary: str, detail: str) -> dict:
    # Shape matches verify_and_ship's own entries; log_stream dedupes on
    # (timestamp, node, step_id, summary, detail, cost_usd), and the summary
    # here never collides with the deploy entry's.
    return {
        "node": "verify_and_ship",
        "step_id": None,
        "summary": summary,
        "detail": detail[:2000],
        "cost_usd": 0.0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def _autodetect(repo: str) -> dict | None:
    # force=True: the 60s cache could still hold the answer from before the
    # wizard wrote this project, and a stale "has checks" here would skip
    # the one moment we can fill the gap; a stale "no checks" would attempt a
    # second write, which set_project_checks refuses anyway.
    projects = await project_checks(force=True)
    if not projects:
        return None  # reviewer unreachable: cannot confirm, so do nothing
    entry = projects.get(repo)
    if entry is None or entry.get("checks"):
        return None

    live = PROJECTS[repo]["live"]
    # existing_names=[] on purpose: the project IS configured, and passing
    # the configured names would make detection report its own name as a
    # duplicate blocker.
    report = await asyncio.to_thread(detect_project, live, existing_names=[])
    warnings = "; ".join(report.warnings) if report.warnings else "none"
    if report.blockers or not report.checks:
        why = "; ".join(report.blockers) if report.blockers else warnings
        return _log_entry(
            f"no checks were configured for {repo} and none are detectable yet",
            f"no checks were configured for {repo} and none are detectable yet: {why}",
        )

    # report.checks IS the recommended set: risky/network suites arrive with
    # enabled=False by construction (provisioning._add_checks) and are never
    # promoted here.
    set_project_checks(_config._PROJECTS_CONFIG_PATH, repo, report.checks)
    reload_projects()
    # project_checks(force=True) above refreshed the 60s cache with the
    # PRE-write answer ("no checks"), so without this every project_has_checks
    # caller -- the GitHub inbox's Auto gate most of all -- would keep reading
    # "this project verifies nothing" for a minute after we gave it checks.
    _review_gate._CHECKS_CACHE.pop("all", None)
    names = [c.get("name", "?") for c in report.checks]
    lines = [
        f"{c.get('name', '?')}: {c.get('cmd', '')} {' '.join(c.get('args') or [])}".rstrip()
        + ("" if c.get("enabled", True) else "  (disabled: needs operator review)")
        for c in report.checks
    ]
    logger.info("configured detected checks for %s after its first merge: %s", repo, ", ".join(names))
    return _log_entry(
        f"configured detected checks for {repo}: {', '.join(names)}",
        "\n".join(lines) + f"\nwarnings: {warnings}",
    )


async def autodetect_checks_if_none(repo: str) -> dict[str, Any] | None:
    """After a merge: if the reviewer runs no checks for `repo`, detect and
    write the recommended ones. Returns an execution_log entry describing
    what happened, or None when there was nothing to do or it could not be
    confirmed. Never raises -- see the module docstring."""
    try:
        return await _autodetect(repo)
    except Exception:  # noqa: BLE001 -- best-effort by design
        logger.warning("post-merge check detection failed for %s", repo, exc_info=True)
        return None
