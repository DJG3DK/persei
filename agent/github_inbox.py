"""The GitHub inbox: what the poller finds, what policy decides, and the
approve links the operator clicks.

Flow, per project with at least one source switched on:

  discover  -- read open PRs, Dependabot alerts, CodeQL/code-scanning alerts
               (grouped per rule), CHANGES_REQUESTED reviews, and failing
               default-branch checks (check runs, or Actions workflow runs
               when the token has only Actions: read) from the GitHub API,
               each as an inbox ITEM with a stable key and a fingerprint.
  decide    -- for an item not seen before (or whose fingerprint changed):
                 off      -> "seen"      (listed, nothing else)
                 propose  -> "proposed"  (alert with an approve link)
                 auto     -> "task_created" now, within the project's caps;
                             past the cap it is proposed instead.
  approve   -- the operator clicks Approve in the dashboard, in Telegram or
               in an email; the item becomes a task. The task runs the same
               review gate and merge approval as any other.

Items live in the store under ("github_inbox", repo), keyed by their item
key, so a PR is never proposed twice and a dismissed one stays dismissed
until it changes (a new head commit is a new fingerprint).

Approve links carry a signed, expiring token instead of a session: the
operator is on their phone, reading Telegram, and must not have to log in
to say yes. The link opens a confirmation page with a button -- a GET must
never act, because Telegram and mail clients fetch links for previews --
and the POST behind that button is single-use: once an item leaves
"proposed", every token for it is spent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from collections.abc import Awaitable, Callable

import httpx

from agent import github_settings
from agent import audit
from agent import store_paging
from agent.config import Config
from agent.tools.github_tools import fetch_pull_request, format_pull_request, resolve_slug

logger = logging.getLogger("tektonix")

API = "https://api.github.com"
_TIMEOUT = 20
NAMESPACE = "github_inbox"

BOT_SUFFIX = "[bot]"
DEPENDABOT = "dependabot[bot]"

# How long an approve link stays valid. Long enough to read it in the
# morning; short enough that a leaked old message is not a standing key.
APPROVAL_TTL_S = 48 * 3600


# ---------------------------------------------------------------------------
# GitHub client -- async, thin, and the one thing tests replace
# ---------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{API}{path}", headers=self._headers, params=params)
        if r.status_code == 404:
            raise LookupError(f"GitHub 404 for {path} (no access, or it does not exist)")
        if r.status_code in (401, 403):
            raise PermissionError(f"GitHub refused {path} ({r.status_code}): {r.text[:160]}")
        r.raise_for_status()
        return r.json()

    async def repo(self, slug: str) -> dict:
        return await self.get(f"/repos/{slug}")

    async def open_prs(self, slug: str) -> list[dict]:
        return await self.get(f"/repos/{slug}/pulls", {"state": "open", "per_page": 50, "sort": "updated", "direction": "desc"})

    async def reviews(self, slug: str, number: int) -> list[dict]:
        return await self.get(f"/repos/{slug}/pulls/{number}/reviews", {"per_page": 50})

    async def dependabot_alerts(self, slug: str) -> list[dict]:
        return await self.get(f"/repos/{slug}/dependabot/alerts", {"state": "open", "per_page": 50})

    async def last_scan_at(self, slug: str) -> float | None:
        """When code scanning last analysed the default branch, or None. A
        finished task's item waits for an analysis newer than the task."""
        info = await self.repo(slug)
        ref = f"refs/heads/{info.get('default_branch') or 'main'}"
        rows = await self.get(f"/repos/{slug}/code-scanning/analyses", {"ref": ref, "per_page": 1})
        if not rows:
            return None
        stamp = rows[0].get("created_at") or ""
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    async def code_scanning_alerts(self, slug: str) -> list[dict]:
        """Open code scanning (CodeQL etc.) alerts. A first full analysis can
        produce a few hundred; three pages is the ceiling before the inbox
        stops being an inbox."""
        out: list[dict] = []
        for page in (1, 2, 3):
            batch = await self.get(f"/repos/{slug}/code-scanning/alerts", {"state": "open", "per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    async def check_runs(self, slug: str, ref: str) -> list[dict]:
        data = await self.get(f"/repos/{slug}/commits/{ref}/check-runs", {"per_page": 50})
        return data.get("check_runs") or []

    async def workflow_runs(self, slug: str, branch: str) -> list[dict]:
        """GitHub Actions runs on a branch, newest first. Needs "Actions: read"
        where check_runs needs "Checks: read"; a fine-grained token may have
        either, so ci_failures tries both."""
        data = await self.get(f"/repos/{slug}/actions/runs", {"branch": branch, "per_page": 30})
        return data.get("workflow_runs") or []


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

@dataclass
class Item:
    key: str                 # "pr:12" / "alerts:<pkg@manifest>" / "review:12" / "ci:<sha>:<name>" / "code:<rule>"
    kind: str                # a SOURCES name
    repo: str
    title: str
    url: str
    fingerprint: str         # changes when the thing itself changed (new head sha, new alert state)
    number: int | None = None
    author: str | None = None
    summary: str = ""
    state: str = "seen"
    mode: str = "off"        # policy mode that applied when the item was decided
    reason: str = ""
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    snoozed_until: float | None = None
    approval_nonce: str | None = None
    # Earlier tasks on this item that finished and merged without GitHub
    # dropping it: [{"task_id", "finished_at", "still_open"}]. Shown on the
    # item and written into the next task's goal.
    attempts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# A Persei task branch: agent/<task uuid> (agent/tools/git.py
# task_branch_name), and the pre-rebase backups beside it.
_OWN_BRANCH = re.compile(r"^agent/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-|$)")


def _opened_by_tektonix(pr: dict) -> bool:
    """A pull request Persei opened for one of its own tasks. Never an inbox
    item, whatever the author setting: with "anyone", the inbox offered each
    task's own PR back as a new task, which opened another PR for the next
    poll to offer ("Land the dependency update proposed in pull request #42
    (Land ... #37 ...)", 2026-09-30)."""
    return bool(_OWN_BRANCH.match(str((pr.get("head") or {}).get("ref") or "")))


# A pull request whose author is part of the repository. GitHub's own
# author_association; the rest (CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE...)
# is anyone with an account.
_INSIDE_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def _review_author_allowed(pr: dict, policy: str) -> bool:
    """Whose pull request a requested-changes review may turn into work.
    Review requests had no author filter: any outsider's PR with a review
    asking for changes became an inbox item, its body went into a task's
    goal, and from the episode into the consolidation prompt that writes
    every later task's memory (audit A21, 2026-10-22). The project's author
    setting applies as it does to Dependabot PRs, and the repository's own
    people are always allowed -- their PRs are what reviews are mostly on."""
    login = (pr.get("user") or {}).get("login")
    return (_author_allowed(login, policy)
            or str(pr.get("author_association") or "").upper() in _INSIDE_ASSOCIATIONS)


def _author_allowed(login: str | None, policy: str) -> bool:
    login = login or ""
    if policy == "anyone":
        return True
    if policy == "bots":
        return login.endswith(BOT_SUFFIX)
    return login == DEPENDABOT


async def discover(client: GitHubClient, repo: str, slug: str, proj: dict) -> list[Item]:
    """Every current item for the sources this project has switched on.
    One source failing (a token without alert permission, say) is logged
    and does not hide the others."""
    policies = proj["policies"]
    items: list[Item] = []
    prs: list[dict] | None = None

    async def _prs() -> list[dict]:
        nonlocal prs
        if prs is None:
            prs = await client.open_prs(slug)
        return prs

    if policies["dependabot_prs"] != "off":
        try:
            for pr in await _prs():
                login = (pr.get("user") or {}).get("login")
                if _opened_by_tektonix(pr) or not _author_allowed(login, proj.get("authors", "dependabot")):
                    continue
                head = (pr.get("head") or {}).get("sha") or ""
                items.append(Item(
                    key=f"pr:{pr['number']}", kind="dependabot_prs", repo=repo, number=pr["number"],
                    title=pr.get("title") or f"PR #{pr['number']}", url=pr.get("html_url") or "",
                    fingerprint=head[:12], author=login,
                    summary=f"{login}: {(pr.get('head') or {}).get('ref')} -> {(pr.get('base') or {}).get('ref')}",
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s dependabot_prs failed: %s", repo, e)

    if policies["review_requests"] != "off":
        try:
            for pr in await _prs():
                if not _review_author_allowed(pr, proj.get("authors", "dependabot")):
                    continue
                reviews = await client.reviews(slug, pr["number"])
                # The latest review per reviewer decides; an approval after a
                # changes-requested review cancels it.
                latest: dict[str, dict] = {}
                for r in reviews:
                    who = (r.get("user") or {}).get("login") or "?"
                    latest[who] = r
                requesting = [r for r in latest.values() if r.get("state") == "CHANGES_REQUESTED"]
                if not requesting:
                    continue
                head = (pr.get("head") or {}).get("sha") or ""
                reviewers = ", ".join(sorted((r.get("user") or {}).get("login") or "?" for r in requesting))
                items.append(Item(
                    key=f"review:{pr['number']}", kind="review_requests", repo=repo, number=pr["number"],
                    title=pr.get("title") or f"PR #{pr['number']}", url=pr.get("html_url") or "",
                    fingerprint=f"{head[:12]}:{max(r.get('id', 0) for r in requesting)}",
                    author=(pr.get("user") or {}).get("login"),
                    summary=f"changes requested by {reviewers}",
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s review_requests failed: %s", repo, e)

    if policies["security_alerts"] != "off":
        try:
            items.extend(security_alert_items(await client.dependabot_alerts(slug), repo, slug))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s security_alerts failed: %s", repo, e)

    if policies["ci_failures"] != "off":
        try:
            info = await client.repo(slug)
            branch = info.get("default_branch") or "main"
            items.extend(await _ci_failures(client, repo, slug, branch))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s ci_failures failed: %s", repo, e)

    if policies.get("code_scanning", "off") != "off":
        try:
            items.extend(code_scanning_items(await client.code_scanning_alerts(slug), repo, slug))
        except LookupError as e:
            # 404 here means code scanning has never run on this repository
            # (no workflow, or default setup still pending) -- not a fault.
            logger.info("github inbox: %s has no code scanning results yet: %s", repo, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s code_scanning failed: %s", repo, e)

    return items


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "error": 1, "warning": 2, "note": 3}
_MAX_LOCATIONS_IN_SUMMARY = 12


def _alert_severity(alert: dict) -> str:
    rule = alert.get("rule") or {}
    return str(rule.get("security_severity_level") or rule.get("severity") or "unknown").lower()


def _version_key(v: str) -> tuple:
    try:
        from packaging.version import Version  # noqa: PLC0415 -- optional in the CLI install
        return (0, Version(v))
    except Exception:  # noqa: BLE001 -- a version nobody can parse sorts last, as text
        return (1, v)


def security_alert_items(alerts: list[dict], repo: str, slug: str) -> list[Item]:
    """One item per package and manifest, not per alert. Eleven undici
    advisories on one lockfile are one bump (to the highest first-patched
    version among them), and on 2026-09-29 they became eleven tasks: one
    merge closed all eleven and the other ten rebased, re-reviewed and
    merged a lockfile change that fixed nothing. A package with a single
    alert keeps the title, number and page it always had. A group carries
    every alert number in its summary, and its fingerprint covers each
    alert's number and state, so a partial fix re-decides it and a full fix
    resolves it.

    Either way the key is `alerts:<pkg@manifest>`. A single alert used to be
    `alert:<n>`, so a second advisory on the same package changed the key:
    the new group started a second task while the first was still on
    `alert:5`, and a group shrinking to one alert started `alert:9` at once,
    with no settle wait and no record of the attempt (audit B36,
    2026-10-22). poll_project moves an old `alert:<n>` row to its new key."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for a in alerts:
        if a.get("state") not in (None, "open"):
            continue
        dep = a.get("dependency") or {}
        pkg = (dep.get("package") or {}).get("name") or "?"
        groups.setdefault((pkg, dep.get("manifest_path") or "?"), []).append(a)
    items: list[Item] = []
    for (pkg, manifest), group in groups.items():
        group.sort(key=lambda a: int(a.get("number") or 0))
        key = "alerts:" + re.sub(r"[^A-Za-z0-9._@-]", "-", f"{pkg}@{manifest}".replace("/", "."))
        if len(group) == 1:
            a = group[0]
            adv = a.get("security_advisory") or {}
            vuln = a.get("security_vulnerability") or {}
            sev = (adv.get("severity") or "?").upper()
            items.append(Item(
                key=key, kind="security_alerts", repo=repo, number=a["number"],
                title=f"[{sev}] {pkg}: {adv.get('summary') or 'security alert'}",
                url=a.get("html_url") or "", fingerprint=f"{a.get('state')}:{adv.get('ghsa_id')}",
                summary=f"{pkg} {vuln.get('vulnerable_version_range') or ''} -> patched in "
                        f"{(vuln.get('first_patched_version') or {}).get('identifier') or '?'} ({manifest})",
            ))
            continue
        sev = min((((a.get("security_advisory") or {}).get("severity") or "low").lower() for a in group),
                  key=lambda x: _SEVERITY_RANK.get(x, 9))
        patched = [((a.get("security_vulnerability") or {}).get("first_patched_version") or {}).get("identifier")
                   for a in group]
        patched = [v for v in patched if v]
        target = max(patched, key=_version_key) if patched else "?"
        lines = [f"{pkg} in {manifest}: bump to at least {target} (the highest first-patched version among "
                 f"these {len(group)} open alerts)"]
        for a in group:
            adv = a.get("security_advisory") or {}
            vuln = a.get("security_vulnerability") or {}
            lines.append(f"#{a.get('number')} [{(adv.get('severity') or '?').upper()}] "
                         f"{adv.get('summary') or 'security alert'}: {vuln.get('vulnerable_version_range') or ''} "
                         f"-> patched in {(vuln.get('first_patched_version') or {}).get('identifier') or '?'}")
        fp = hashlib.sha1("|".join(f"{a.get('number')}:{a.get('state')}:"
                                   f"{(a.get('security_advisory') or {}).get('ghsa_id')}" for a in group).encode()
                          ).hexdigest()[:12]
        items.append(Item(
            key=key, kind="security_alerts", repo=repo, number=None,
            title=f"[{sev.upper()}] {pkg}: {len(group)} open alerts in {manifest}, patched in {target}",
            url=f"https://github.com/{slug}/security/dependabot?q=is%3Aopen+package%3A{pkg}",
            fingerprint=fp, summary="\n".join(lines),
        ))
    return items


def code_scanning_items(alerts: list[dict], repo: str, slug: str) -> list[Item]:
    """One item per rule, not per alert. Five `js/double-escaping` findings
    are one piece of work (the same helper, fixed once), and a task that sees
    all the locations fixes the cause rather than the first symptom. The key
    is the rule id with '/' folded to '.', because the key travels in a URL
    path segment. The fingerprint covers every open alert's number, commit
    and location, so a partial fix re-decides the item and a complete fix
    resolves it."""
    by_rule: dict[str, list[dict]] = {}
    for a in alerts:
        if a.get("state") not in (None, "open"):
            continue
        rule_id = str((a.get("rule") or {}).get("id") or "unknown-rule")
        by_rule.setdefault(rule_id, []).append(a)
    items: list[Item] = []
    for rule_id, group in by_rule.items():
        group.sort(key=lambda a: int(a.get("number") or 0))
        first = group[0]
        rule = first.get("rule") or {}
        tool = ((first.get("tool") or {}).get("name")) or "code scanning"
        sev = min((_alert_severity(a) for a in group), key=lambda s: _SEVERITY_RANK.get(s, 9))
        locs = []
        for a in group:
            inst = a.get("most_recent_instance") or {}
            loc = inst.get("location") or {}
            msg = str((inst.get("message") or {}).get("text") or "").strip().replace("\n", " ")
            path = loc.get("path") or "?"
            line = loc.get("start_line")
            locs.append((a.get("number"), f"{path}:{line}" if line else path, msg, (inst.get("commit_sha") or "")[:12]))
        fp = hashlib.sha1("|".join(f"{n}:{where}:{sha}" for n, where, _, sha in locs).encode()).hexdigest()[:12]
        lines = [f"{tool} · {sev} · {len(group)} open alert{'s' if len(group) != 1 else ''} for rule {rule_id}"]
        for n, where, msg, _ in locs[:_MAX_LOCATIONS_IN_SUMMARY]:
            lines.append(f"#{n} {where}" + (f" — {msg[:160]}" if msg else ""))
        if len(locs) > _MAX_LOCATIONS_IN_SUMMARY:
            lines.append(f"… and {len(locs) - _MAX_LOCATIONS_IN_SUMMARY} more (see the alerts page)")
        desc = rule.get("description") or rule.get("name") or rule_id
        items.append(Item(
            key=f"code:{rule_id.replace('/', '.')}", kind="code_scanning", repo=repo, number=None,
            title=f"[{sev.upper()}] {rule_id}: {desc}" + (f" ({len(group)} locations)" if len(group) > 1 else ""),
            url=f"https://github.com/{slug}/security/code-scanning?query=is%3Aopen+rule%3A{rule_id}",
            fingerprint=fp, summary="\n".join(lines),
        ))
    items.sort(key=lambda i: (_SEVERITY_RANK.get(i.title[1:i.title.index(']')].lower(), 9), i.key))
    return items


async def _ci_failures(client: GitHubClient, repo: str, slug: str, branch: str) -> list[Item]:
    """Failed runs on the tip of `branch`: check runs when the token may read
    them, else the newest Actions workflow runs. Only the latest commit that
    has runs counts -- a failure three commits back is history, not work."""
    out: list[Item] = []
    try:
        runs = await client.check_runs(slug, branch)
        for run in runs:
            if run.get("conclusion") not in ("failure", "timed_out"):
                continue
            sha = (run.get("head_sha") or "")[:12]
            name = run.get("name") or "check"
            out.append(Item(
                key=f"ci:{sha}:{name}", kind="ci_failures", repo=repo,
                title=f"{name} failed on {branch} @ {sha[:7]}", url=run.get("html_url") or "",
                fingerprint=f"{sha}:{run.get('id')}",
                summary=(run.get("output") or {}).get("title") or run.get("conclusion") or "",
            ))
        return out
    except PermissionError as e:
        logger.info("github inbox: %s check-runs refused (%s); trying Actions runs", repo, str(e)[:80])
    runs = await client.workflow_runs(slug, branch)
    if not runs:
        return out
    # Dependabot's own version-update jobs show up as workflow runs with
    # event "dynamic" ("npm_and_yarn in /. for sharp - Update #..."). A failed
    # one means Dependabot could not produce a PR, which the alerts source
    # already covers; it is not a check on the operator's code (2026-09-10:
    # five of six "failing checks" on storefront were these).
    runs = [r for r in runs if r.get("event") != "dynamic"]
    if not runs:
        return out
    tip = runs[0].get("head_sha") or ""
    for run in runs:
        if run.get("head_sha") != tip or run.get("conclusion") not in ("failure", "timed_out"):
            continue
        sha = tip[:12]
        name = run.get("name") or "workflow"
        out.append(Item(
            key=f"ci:{sha}:{name}", kind="ci_failures", repo=repo,
            title=f"{name} failed on {branch} @ {sha[:7]}", url=run.get("html_url") or "",
            fingerprint=f"{sha}:{run.get('id')}",
            summary=f"{run.get('display_title') or ''} ({run.get('event') or 'run'} #{run.get('run_number')})".strip(),
        ))
    return out


# ---------------------------------------------------------------------------
# policy -- pure
# ---------------------------------------------------------------------------

# Said in full on the item, because "why did this only get proposed?" is asked
# at the moment the operator is deciding, not when they set the policy.
_NO_CHECKS_REASON = ("auto needs checks: this project's review gate runs nothing mechanical, "
                     "so nothing would verify the work -- proposed instead")
_UNCONFIRMED_CHECKS_REASON = ("auto held back: could not confirm this project's checks with the "
                              "review service -- proposed instead")


@dataclass
class Decision:
    item: Item
    action: str      # "none" | "propose" | "create"
    reason: str = ""


# How long a finished task's item waits for GitHub to catch up, for the
# kinds that have no scan to wait for (Dependabot re-evaluates on push, a
# check reruns). Code scanning waits for an actual analysis of the default
# branch newer than the task instead. A pull request item never settles on
# a clock: see _after_finished_task.
SETTLE_S = 45 * 60
# An analysis must start at least this long after the task finished to count:
# one already running for the commit before the fix can finish just after.
SCAN_MARGIN_S = 60


def settled(kind: str, finished_at: float, now: float, scanned_at: float | None) -> bool:
    """Has GitHub had its chance to see a finished task's fix?"""
    if kind == "code_scanning":
        return scanned_at is not None and scanned_at >= finished_at + SCAN_MARGIN_S
    return now - finished_at >= SETTLE_S


def still_open_text(item: Item | dict) -> str:
    """What GitHub still reports, in the item's own terms: '1 alert',
    '4 alerts', 'the check failing', or 'it'."""
    d = item.to_dict() if isinstance(item, Item) else item
    if d.get("kind") == "ci_failures":
        return "the check failing"
    n = len(alert_numbers(d)) if d.get("kind") in ("security_alerts", "code_scanning") else 0
    return f"{n} alert{'s' if n != 1 else ''}" if n else "it"


def _review_id(fingerprint: str | None) -> str:
    """The newest changes-requested review in a review item's fingerprint
    (`<head>:<review id>`)."""
    return str(fingerprint or "").rsplit(":", 1)[-1]


def decide(existing: dict[str, dict], found: list[Item], proj: dict, open_auto: int,
           now: float | None = None, has_checks: bool | None = True,
           live_tasks: set | None = None, finished_tasks: dict | None = None,
           scanned_at: float | None = None) -> tuple[list[Decision], list[str]]:
    """Compare what was found with what the store holds.

    Returns the decisions for items that are new or changed, and the keys of
    stored items that are no longer present on GitHub (merged, closed, fixed)
    so the caller can mark them resolved. `open_auto` is how many auto-created
    tasks are still open; the cap is enforced here.

    `has_checks` is whether this project's review gate runs anything
    mechanical (tests, lint, a build), from the review service -- None when
    that could not be confirmed. Auto needs it: starting work by itself whose
    gate runs no checks means a model's opinion is the only thing between a
    GitHub alert and a diff waiting for the operator's merge click. Without
    confirmed checks, Auto degrades to Propose, with the reason on the item.
    The operator loses one click and keeps the review that click is for.

    `finished_tasks` maps this project's done tasks to when they finished.
    A done task's item is not handled forever (2026-10-16: five code-scanning
    items sat on finished tasks all day, every alert still open after the
    fixes merged and GitHub re-scanned, with no button but "Open task"). It
    waits, as `awaiting_rescan`, until GitHub has had its chance to see the
    fix (settled()); then it either disappears from GitHub, or comes back as
    a proposal that records the attempt -- never auto-started, so a fix
    GitHub keeps rejecting cannot start task after task by itself.
    """
    now = now or time.time()
    finished = finished_tasks or {}

    def _after_finished_task(item: Item, prev: dict) -> Decision | None:
        """The decision for an item whose task is done, or None if it is not."""
        tid = prev.get("task_id")
        if not tid or tid not in finished or prev.get("state") not in ("task_created", "awaiting_rescan"):
            return None
        item.created_at = prev.get("created_at", now)
        item.task_id = tid
        # A pull request's own state is the signal, not a clock (audit B37,
        # 2026-10-22). A merged fix never clears a CHANGES_REQUESTED review,
        # and Dependabot often takes longer than SETTLE_S to close a PR the
        # fix superseded -- or rebases it onto the fix first -- so both came
        # back 45 minutes after their task, as "GitHub still reports 1 alert
        # after re-scanning". A Dependabot PR item stays with its task until
        # the PR closes (it is then gone, and resolved); a review item comes
        # back only for a new review that asks for changes again.
        if item.kind == "dependabot_prs" or (
                item.kind == "review_requests" and _review_id(item.fingerprint) == _review_id(prev.get("fingerprint"))):
            item.state = "awaiting_rescan"
            if prev.get("state") == "awaiting_rescan" and prev.get("fingerprint") == item.fingerprint:
                return Decision(item, "skip", "")
            return Decision(item, "none", "task finished; waiting for the pull request to close"
                            if item.kind == "dependabot_prs" else
                            "task finished; a new review asking for changes brings it back")
        if item.kind == "review_requests":
            item.attempts.append({"task_id": tid, "finished_at": finished[tid], "still_open": []})
            item.task_id = None
            item.state = "proposed"
            return Decision(item, "propose", f"a new review asks for changes after task {tid[:8]} finished")
        if not settled(item.kind, finished[tid], now, scanned_at):
            item.state = "awaiting_rescan"
            if prev.get("state") == "awaiting_rescan" and prev.get("fingerprint") == item.fingerprint:
                return Decision(item, "skip", "")
            return Decision(item, "none", "fix merged; waiting for GitHub to re-scan")
        item.attempts.append({"task_id": tid, "finished_at": finished[tid],
                              "still_open": alert_numbers(item.to_dict())})
        item.task_id = None
        item.state = "proposed"
        return Decision(item, "propose",
                        f"task {tid[:8]} merged its fix, but GitHub still reports {still_open_text(item)}"
                        + (" after re-scanning" if item.kind == "code_scanning" else ""))
    # None means "we could not ask", which must behave exactly as before:
    # never re-propose on a guess. An empty SET, by contrast, is a real
    # answer -- nothing is in flight.
    live = live_tasks

    def _still_being_worked(prev: dict) -> bool:
        """Is a task genuinely on this item right now?

        Asked so that `task_created` is not a one-way door: an item whose
        task is gone goes back to the queue instead of sitting with no button
        while still open on GitHub (2026-09-22: a stopped task left its alert
        unreachable).

        In flight means running, queued, or parked on a decision about THAT
        task (an approval, a merge, an escalation the operator can resume).
        Stopped, errored, finished-without-fixing-it, or a task id that no
        longer exists are all "nobody is on this", and the honest state for
        those is back in the queue.
        """
        if live is None:
            return True            # could not ask; keep the old behaviour
        return prev.get("task_id") in live

    decisions: list[Decision] = []
    found_keys = {i.key for i in found}
    budget = max(0, int(proj.get("max_open_auto", 2)) - open_auto)
    auto_allowed = has_checks is True
    for item in found:
        prev = existing.get(item.key)
        mode = proj["policies"].get(item.kind, "off")
        item.mode = mode
        if prev:
            item.attempts = list(prev.get("attempts") or [])
            after = _after_finished_task(item, prev) if mode != "off" else None
            if after is not None:
                if after.action != "skip":
                    decisions.append(after)
                continue
        if prev and prev.get("fingerprint") == item.fingerprint:
            # Unchanged. A snooze that expired is re-proposed, an item whose
            # task is no longer running comes back to the queue, and anything
            # else keeps its state.
            if prev.get("state") == "snoozed" and (prev.get("snoozed_until") or 0) <= now and mode != "off":
                item.state = "proposed"
                item.created_at = prev.get("created_at", now)
                decisions.append(Decision(item, "propose", "snooze expired"))
                continue
            # This is the branch a stranded item is actually in: the alert did
            # not change, the TASK died. Checking only the changed-fingerprint
            # path below would have left the common case stuck.
            if (prev.get("state") == "task_created" and mode != "off"
                    and not _still_being_worked(prev)):
                item.state = "proposed"
                item.created_at = prev.get("created_at", now)
                decisions.append(Decision(item, "propose", "its task ended without fixing it"))
                continue
            continue
        if prev:
            item.created_at = prev.get("created_at", now)
            if (prev.get("state") == "task_created" and prev.get("task_id")
                    and _still_being_worked(prev)):
                # The thing changed while a task is on it (the task itself
                # pushed, most likely). Keep the link; do not stack a second.
                item.state = "task_created"
                item.task_id = prev["task_id"]
                decisions.append(Decision(item, "none", "changed while a task is open"))
                continue
            # ...and if that task is gone, fall through to the normal policy
            # decision below, which proposes or creates as the project says.
        if mode == "off":
            item.state = "seen"
            decisions.append(Decision(item, "none", "source is off"))
        elif mode == "propose":
            item.state = "proposed"
            decisions.append(Decision(item, "propose", "policy: propose"))
        else:  # auto
            if item.attempts:
                # A fix already merged and GitHub kept the item: the next
                # attempt is the operator's call, not the policy's.
                item.state = "proposed"
                decisions.append(Decision(item, "propose", "an earlier fix did not clear it; waiting for you"))
            elif not auto_allowed:
                item.state = "proposed"
                decisions.append(Decision(item, "propose", _NO_CHECKS_REASON if has_checks is False else _UNCONFIRMED_CHECKS_REASON))
            elif budget > 0:
                budget -= 1
                item.state = "task_created"
                decisions.append(Decision(item, "create", "policy: auto"))
            else:
                item.state = "proposed"
                decisions.append(Decision(item, "propose", f"auto cap reached ({proj.get('max_open_auto')} open)"))
    gone = [k for k, v in existing.items() if k not in found_keys and v.get("state") not in ("resolved",)]
    return decisions, gone


# ---------------------------------------------------------------------------
# approve links -- HMAC over the auth secret, expiring, single-use by state
# ---------------------------------------------------------------------------

def _mac(config: Config, payload: bytes) -> str:
    key = base64.urlsafe_b64decode(config.auth_secret_key)
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def sign_approval(config: Config, repo: str, key: str, nonce: str, action: str = "approve", ttl_s: int = APPROVAL_TTL_S, now: float | None = None) -> str:
    payload = json.dumps({"r": repo, "k": key, "n": nonce, "a": action, "e": int((now or time.time()) + ttl_s)}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_mac(config, payload)}"


def verify_approval(config: Config, token: str, now: float | None = None) -> dict:
    """The payload, or ValueError whose message is a reason CODE (malformed,
    invalid, expired, unknown) the page maps to its own wording."""
    try:
        body, mac = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception as e:  # noqa: BLE001
        raise ValueError("malformed") from e
    if not hmac.compare_digest(mac, _mac(config, payload)):
        raise ValueError("invalid")
    data = json.loads(payload)
    if data.get("e", 0) < (now or time.time()):
        raise ValueError("expired")
    if data.get("a") not in ("approve", "dismiss"):
        raise ValueError("unknown")
    return data


def approval_url(settings: dict, token: str) -> str | None:
    base = settings.get("public_url") or ""
    return f"{base}/api/github/approve?t={token}" if base else None


# ---------------------------------------------------------------------------
# task goal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# putting the code in the goal
# ---------------------------------------------------------------------------
#
# A code-scanning goal that carries only the rule id and a file:line list
# makes the rule the subject: three times on 2026-09-22 the agent read
# CodeQL's own query source and never opened the controller it had been
# handed the line number for. Prompt guidance did not hold against that.
#
# The alert already knows the file and the line. Reading them is a file read,
# not a judgement, so the goal can simply CONTAIN the code -- and a goal with
# the code in it has nothing left to research. Deterministic on purpose: no
# model call, nothing to drift between runs, and a location that cannot be
# read is skipped rather than guessed at.

_LOC_RE = re.compile(r"^#(?P<num>\d+)\s+(?P<path>[^\s:]+):(?P<line>\d+)\s*(?:—|--)?\s*(?P<msg>.*)$")

# Context either side of a flagged line. Six is enough to see the statement,
# what feeds it and what it returns into, without pasting whole functions.
_CONTEXT_LINES = 6
# How many locations get their code inlined. The rest stay as the file:line
# list they already were -- past a dozen the goal stops being readable, and a
# rule with fifty hits is one pattern anyway.
_MAX_INLINED = 12
_MAX_SNIPPET_CHARS = 6000


def parse_locations(summary: str) -> list[tuple[str, int, str]]:
    """(path, line, message) for every `#N path:line — message` in a summary."""
    out = []
    for raw in (summary or "").splitlines():
        m = _LOC_RE.match(raw.strip())
        if m:
            out.append((m.group("path"), int(m.group("line")), m.group("msg").strip()))
    return out


def _merge_ranges(lines: list[int]) -> list[tuple[int, int]]:
    """Overlapping windows around flagged lines become one window.

    Two alerts eleven lines apart in the same function should read as one
    excerpt, not two with the same four lines printed twice.
    """
    spans = sorted((max(1, n - _CONTEXT_LINES), n + _CONTEXT_LINES) for n in lines)
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def code_for_locations(repo_root: str, summary: str) -> str:
    """The flagged code itself, grouped by file, or "" if none can be read."""
    locs = parse_locations(summary)[:_MAX_INLINED]
    if not locs or not repo_root:
        return ""
    by_file: dict[str, list[int]] = {}
    messages: dict[tuple[str, int], str] = {}
    for path, line, msg in locs:
        by_file.setdefault(path, []).append(line)
        messages[(path, line)] = msg

    blocks: list[str] = []
    total = 0
    for path, lines in by_file.items():
        # The path comes from GitHub, so it is checked before it is joined:
        # a goal builder is not a place to open an arbitrary absolute path.
        full = os.path.normpath(os.path.join(repo_root, path))
        if not full.startswith(os.path.normpath(repo_root) + os.sep) or not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                content = fh.read().splitlines()
        except OSError:
            continue
        for lo, hi in _merge_ranges(lines):
            excerpt = []
            for n in range(lo, min(hi, len(content)) + 1):
                flag = "  <-- flagged" if n in lines else ""
                excerpt.append(f"{n:>5}  {content[n - 1]}{flag}")
            if not excerpt:
                continue
            notes = sorted({messages[(path, n)] for n in lines if lo <= n <= hi and messages.get((path, n))})
            head = f"--- {path}:{lo}-{min(hi, len(content))} ---"
            block = "\n".join([head, *(f"    {t}" for t in notes), "", *excerpt])
            total += len(block)
            if total > _MAX_SNIPPET_CHARS:
                blocks.append("(further locations omitted -- the same pattern; the list above has them all)")
                return "\n\n".join(blocks)
            blocks.append(block)
    return "\n\n".join(blocks)


_GOAL_TEMPLATES = {
    "dependabot_prs": (
        "Land the dependency update proposed in GitHub pull request #{number} ({title}).\n\n"
        "Read the PR with github_pull_request first. Apply the same version bump on this branch "
        "(edit the manifest and lockfile the way the PR does; do not hand-edit a lockfile if the "
        "package manager can regenerate it), install, and run the project's full test suite and build. "
        "If a breaking change needs code updates, make them and say so in the commit message. "
        "Do not touch unrelated files.\n\nPR: {url}\n{summary}"
    ),
    "security_alerts": (
        "Fix Dependabot security {alerts}: {title}.\n\n"
        "{summary}\n\nUpgrade the affected package to a patched version in every manifest that pins "
        "it, regenerate the lockfile with the package manager, install, and run the full test suite "
        "and build. If the direct dependency must move to a new major to clear the alert, make the "
        "code changes that requires and say so in the commit message.\n\nAlert: {url}"
    ),
    "review_requests": (
        "Address the review on GitHub pull request #{number} ({title}): {summary}.\n\n"
        "Read the PR with github_pull_request, part=\"comments\" first, then part=\"diff\". Treat every "
        "review comment as a todo: fix each one on this branch, keeping the PR's intent, and note in "
        "the commit message which comments were addressed. Run the tests the PR touches.\n\nPR: {url}"
    ),
    "ci_failures": (
        "Fix the failing check on the default branch: {title}.\n\n{summary}\n\nRead the check run "
        "output at {url}, reproduce the failure locally, fix the cause (not the check), and run the "
        "full suite before finishing."
    ),
    "code_scanning": (
        "Fix the code scanning finding {title} in THIS repository.\n\n{summary}\n\n"
        "Every location above is an open alert on this repository's Security → Code scanning page ({url}); "
        "the alerts belong to this repository only — do not look for or touch other projects. "
        "THE FLAGGED CODE IS BELOW — start there. Do not download, read or reason about the "
        "analyser's own rule definitions, query source, .qll files or test fixtures: the finding is "
        "the alert plus the code, and both are in this goal. "
        "For each location: read the surrounding code, understand why the query flags it, and fix "
        "the cause (validate or constrain the input, use the safe API, or restructure the flow) with "
        "the smallest change that makes the finding untrue. Do not silence it: no lgtm/codeql "
        "suppression comments, no dismissing alerts on GitHub, no deleting the code path unless it is "
        "genuinely dead. Where several locations share a helper, fix the helper once. Keep behaviour "
        "identical for legitimate input, add or extend a test where the fix is testable, and run the "
        "project's full test suite, typecheck and lint before finishing. In the commit message list "
        "the alert numbers addressed."
    ),
}


def alert_numbers(item: dict) -> list[int]:
    """The Dependabot alert numbers an item stands for: its own, or every
    `#n` its summary lists for a grouped item."""
    if item.get("number"):
        return [int(item["number"])]
    return [int(n) for n in re.findall(r"(?m)^#(\d+) ", item.get("summary") or "")]


def alert_reference(item: dict) -> str:
    """'alert #9', or 'alerts #6, #8 and #9' for a grouped item."""
    nums = alert_numbers(item)
    if len(nums) <= 1:
        return f"alert #{nums[0]}" if nums else "alerts"
    return "alerts " + ", ".join(f"#{n}" for n in nums[:-1]) + f" and #{nums[-1]}"


# What the next task is told to look at, per kind. The scanner advice is for
# code scanning only: a Dependabot alert or a failing check that came back
# was given it too, and it sent the task looking for a type check (audit
# B37, 2026-10-22).
_ATTEMPT_ADVICE = {
    "code_scanning": (
        "Before changing anything, read what those commits changed (git log -p on the files above) and "
        "work out why GitHub still flags the code: a scanner usually wants the input checked in a form it "
        "recognises (a type check, an allow-list, a bounds check right where the value is used), not a "
        "wrapper it cannot see through. Fix the lines listed above as GitHub reports them now, and make "
        "sure the change does not introduce a new finding in the code it touches."),
    "security_alerts": (
        "Before changing anything, read what those commits changed (git log -p) and work out why Dependabot "
        "still reports the alerts: the vulnerable version is usually still resolved somewhere -- a second "
        "lockfile or manifest, a transitive dependency that pins it (an override or resolution may be "
        "needed), or a lockfile that was not regenerated. Check the resolved version in every lockfile."),
    "ci_failures": (
        "Before changing anything, read what those commits changed (git log -p) and the failing run's "
        "log as it is now: the check may fail for a different reason than the one the earlier task fixed."),
    "review_requests": (
        "Read the newest review comments first (github_pull_request, part=\"comments\"): they are what "
        "is left after the earlier task's changes."),
}


def attempts_text(d: dict) -> str:
    """What earlier tasks did and what GitHub still says, for the next task."""
    kind = d.get("kind")
    lines = ["--- earlier attempts that GitHub did not accept ---"]
    for a in d.get("attempts") or []:
        when = datetime.fromtimestamp(a.get("finished_at") or 0).strftime("%Y-%m-%d %H:%M")
        still = ", ".join(f"#{n}" for n in a.get("still_open") or []) or still_open_text({"kind": kind})
        task = str(a.get("task_id", ""))[:8]
        if kind == "review_requests":
            lines.append(f"- Task {task} finished on {when}, and a reviewer has since asked for changes again.")
        else:
            lines.append(f"- Task {task} finished and merged its fix on {when}, but GitHub still reports "
                         f"{still}{' after re-scanning' if kind == 'code_scanning' else ''}.")
    lines.append(_ATTEMPT_ADVICE.get(kind, "Before changing anything, read what those commits changed "
                                           "(git log -p) and work out why GitHub still reports the item."))
    return "\n".join(lines)


def build_goal(item: Item | dict, pr_text: str | None = None, repo_root: str | None = None) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    goal = _GOAL_TEMPLATES[d["kind"]].format(
        number=d.get("number"), title=d.get("title"), url=d.get("url"), summary=d.get("summary") or "",
        alerts=alert_reference(d))
    # The code itself, for a finding that named lines. Appended rather than
    # formatted in, so a template that never mentions it is unaffected and a
    # repo that cannot be read produces exactly the goal it produced before.
    if repo_root and d.get("kind") == "code_scanning":
        snippets = code_for_locations(repo_root, d.get("summary") or "")
        if snippets:
            goal += ("\n\n--- the flagged code, read from this repository ---\n"
                     "(line numbers as of now; the alert's own line may have moved)\n\n" + snippets)
    if d.get("attempts"):
        goal += "\n\n" + attempts_text(d)
    if pr_text:
        goal += "\n\n--- pull request as read from GitHub ---\n" + pr_text[:20_000]
    goal += "\n\n(Created from the GitHub inbox. This task goes through the normal review gate and merge approval.)"
    return goal


async def pr_text_for(token: str, slug: str, number: int) -> str | None:
    """The PR summary (no diff) for the goal, or None if it cannot be read."""
    try:
        import asyncio
        data = await asyncio.to_thread(fetch_pull_request, token, slug, number)
        return format_pull_request(data, "summary")
    except Exception as e:  # noqa: BLE001
        logger.info("github inbox: could not read PR #%s on %s for the goal: %s", number, slug, e)
        return None


# ---------------------------------------------------------------------------
# notifications -- text only; delivery is the caller's
# ---------------------------------------------------------------------------

_KIND_LABEL = {
    "dependabot_prs": "Dependabot PR", "security_alerts": "Security alert",
    "review_requests": "Review requests changes", "ci_failures": "Failing check",
    "code_scanning": "Code scanning alert",
}


def proposal_text(item: Item | dict, approve_link: str | None, dismiss_link: str | None, budget_usd: float) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    lines = [
        f"🐙 GitHub: {_KIND_LABEL.get(d['kind'], d['kind'])} on {d['repo']}",
        d["title"],
    ]
    if d.get("summary"):
        lines.append(d["summary"])
    if d.get("reason") and "cap" in d["reason"]:
        lines.append(f"(not started automatically: {d['reason']})")
    lines.append(f"Budget if approved: ${budget_usd:.2f}")
    lines.append(d.get("url") or "")
    if approve_link:
        lines += ["", f"Approve → {approve_link}"]
        if dismiss_link:
            lines.append(f"Dismiss → {dismiss_link}")
    else:
        lines += ["", "Approve or dismiss it in the dashboard's GitHub inbox (set the public URL in Settings → GitHub to get links here)."]
    return "\n".join(lines)


def created_text(item: Item | dict, task_id: str, budget_usd: float) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    return (f"🐙 GitHub: task started automatically on {d['repo']}\n{d['title']}\n"
            f"{_KIND_LABEL.get(d['kind'], d['kind'])} · budget ${budget_usd:.2f} · task {task_id[:8]}\n{d.get('url') or ''}")


# ---------------------------------------------------------------------------
# the poll -- store + GitHub + callbacks, no FastAPI
# ---------------------------------------------------------------------------

CreateTask = Callable[[str, str, float, str], Awaitable[str]]        # (repo, goal, budget, route) -> task_id
Notify = Callable[[str, str], Awaitable[None]]                        # (text, repo)
OpenAutoCount = Callable[[str], Awaitable[int]]                       # repo -> open auto tasks
LiveTasks = Callable[[str], Awaitable[set]]                           # repo -> task ids still in flight
FinishedTasks = Callable[[str], Awaitable[dict]]                      # repo -> {done task id: finished at}


# One lock per item, held by everything that reads an item, acts on it and
# writes it back: an approve link submitted twice, the dashboard button and a
# link at once, or a poll pass that read the item before either. Without it
# each saw the same "proposed" item across its own awaits and each started a
# task. One process serves the API (docker/agent/entrypoint.sh), so an asyncio
# lock is the whole of it.
_ITEM_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def item_lock(repo: str, key: str) -> asyncio.Lock:
    return _ITEM_LOCKS.setdefault((repo, key), asyncio.Lock())


async def _unchanged_since(store, repo: str, key: str, snapshot: dict | None) -> bool:
    """Whether the stored item is still the one a poll pass decided from."""
    current = await store.aget((NAMESPACE, repo), key)
    now = current.value if current is not None else None
    if snapshot is None:
        return now is None
    return now is not None and now.get("updated_at") == snapshot.get("updated_at")


async def list_items(store, repo: str) -> dict[str, dict]:
    """Every item of the project. It read `asearch(limit=200)`, the 200 most
    recently updated: nothing prunes the namespace and CI keys are per
    commit, so a long-lived project passed 200 and its older items vanished
    from the list, answered 404 to approve, dismiss and snooze, and were
    never resolved or re-proposed by the poll (audit B35, 2026-10-22)."""
    out: dict[str, dict] = {}
    for it in await store_paging.all_items(store, (NAMESPACE, repo)):
        out[it.key] = it.value
    return out


async def get_item(store, repo: str, key: str) -> dict | None:
    """One item, read by its key. An approve link minted before the
    Dependabot key change names `alert:<n>`; it finds the item now holding
    that alert."""
    got = await store.aget((NAMESPACE, repo), key)
    if got is not None and got.value is not None:
        return dict(got.value)
    m = re.fullmatch(r"alert:(\d+)", key)
    if m:
        for v in (await list_items(store, repo)).values():
            if v.get("kind") == "security_alerts" and int(m.group(1)) in alert_numbers(v):
                return dict(v)
    return None


# A resolved item is kept this long, then deleted: long enough to see what
# the inbox did last month, and the namespace no longer grows forever. Only
# resolved ones -- a dismissed item still open on GitHub must stay, or it
# would come back as new; one that closes is resolved, and goes in turn.
PRUNE_RESOLVED_AFTER_S = 30 * 86400


async def _migrate_alert_keys(store, repo: str, existing: dict[str, dict], found: list[Item]) -> dict[str, dict]:
    """Move each `alert:<n>` row to the `alerts:<pkg@manifest>` key its
    alert is found under now, keeping its state, task and attempts (B36).
    The row keeps its updated_at, so the poll's own unchanged-since check
    still matches it."""
    for item in found:
        if item.kind != "security_alerts" or item.key in existing:
            continue
        nums = set(alert_numbers(item.to_dict()))
        olds = [k for k, v in existing.items()
                if k.startswith("alert:") and v.get("state") != "resolved" and nums & set(alert_numbers(v))]
        if not olds:
            continue
        # The row a task is on wins; then the newest.
        olds.sort(key=lambda k: (not existing[k].get("task_id"), -(existing[k].get("updated_at") or 0)))
        moved = {**existing[olds[0]], "key": item.key}
        async with item_lock(repo, item.key):
            await store.aput((NAMESPACE, repo), item.key, moved)
        for k in olds:
            async with item_lock(repo, k):
                await _forget(store, repo, k, existing[k])
            existing.pop(k)
        existing[item.key] = moved
    return existing


async def _forget(store, repo: str, key: str, value: dict) -> None:
    delete = getattr(store, "adelete", None)
    if delete is not None:
        await delete((NAMESPACE, repo), key)
    else:   # a store that cannot delete: out of every list but this one
        await store.aput((NAMESPACE, repo), key, {**value, "state": "resolved", "reason": "moved"})


async def put_item(store, item: Item | dict) -> None:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    d["updated_at"] = time.time()
    await store.aput((NAMESPACE, d["repo"]), d["key"], d)


async def create_task_for_item(item: dict, settings: dict, config: Config, create_task: CreateTask) -> str:
    proj = github_settings.project_settings(settings, item["repo"])
    pr_text = None
    if item.get("number") and item["kind"] in ("dependabot_prs", "review_requests"):
        token = github_settings.token_for(settings, config, item["repo"])
        slug = await asyncio.to_thread(resolve_slug, item["repo"])
        if token and slug:
            pr_text = await pr_text_for(token, slug, int(item["number"]))
    # The live checkout, not the agent's workspace: it is the canonical state
    # of the branch the alert was raised against, and it is not being edited
    # by a task while this reads it.
    from agent.config import PROJECTS  # noqa: PLC0415
    repo_root = (PROJECTS.get(item["repo"]) or {}).get("live")
    goal = build_goal(item, pr_text, repo_root=repo_root)
    return await create_task(item["repo"], goal, float(proj["budget_usd"]), proj.get("route", "auto"))


async def poll_project(
    store, config: Config, settings: dict, repo: str, *,
    create_task: CreateTask, notify: Notify, open_auto_count: OpenAutoCount,
    client: GitHubClient | None = None, live_tasks: LiveTasks | None = None,
    finished_tasks: FinishedTasks | None = None,
) -> dict:
    """One project, one pass. Returns a small summary for the dashboard/log."""
    proj = github_settings.project_settings(settings, repo)
    token = github_settings.token_for(settings, config, repo)
    slug = await asyncio.to_thread(resolve_slug, repo)   # runs git; off the loop
    if not token or not slug:
        return {"repo": repo, "skipped": "no token" if not token else "no GitHub origin"}
    client = client or GitHubClient(token)
    found = await discover(client, repo, slug, proj)
    existing = await _migrate_alert_keys(store, repo, await list_items(store, repo), found)
    open_auto = await open_auto_count(repo)
    # Asked at poll time, not at save time only: a project's checks can be
    # emptied long after its policy was set, and the poll is what actually
    # starts work.
    from agent.tools.review_gate import project_has_checks
    has_checks = await project_has_checks(repo)
    # None when the caller did not supply a lookup: decide() then keeps every
    # task_created item exactly as it was, which is what it did before this.
    in_flight = await live_tasks(repo) if live_tasks else None
    done = await finished_tasks(repo) if finished_tasks else {}
    # When code scanning last looked at the default branch: asked only when a
    # code-scanning item is waiting on a finished task, one call per poll.
    scanned_at = None
    if any(v.get("kind") == "code_scanning" and v.get("task_id") in done
           and v.get("state") in ("task_created", "awaiting_rescan") for v in existing.values()):
        try:
            scanned_at = await client.last_scan_at(slug)
        except Exception as e:  # noqa: BLE001 -- unknown means "keep waiting", never "settled"
            logger.warning("github inbox: %s last scan time unavailable: %s", repo, e)
    decisions, gone = decide(existing, found, proj, open_auto, has_checks=has_checks,
                             live_tasks=in_flight, finished_tasks=done, scanned_at=scanned_at)
    summary = {"repo": repo, "found": len(found), "proposed": 0, "created": 0, "resolved": 0}
    for d in decisions:
        async with item_lock(repo, d.item.key):
            # An operator approved or dismissed it since `existing` was read:
            # their decision stands, and writing ours would undo it.
            if not await _unchanged_since(store, repo, d.item.key, existing.get(d.item.key)):
                summary["skipped"] = summary.get("skipped", 0) + 1
                continue
            await _apply_decision(store, config, settings, repo, proj, d, summary,
                                  create_task=create_task, notify=notify)
    for key in gone:
        async with item_lock(repo, key):
            if not await _unchanged_since(store, repo, key, existing[key]):
                summary["skipped"] = summary.get("skipped", 0) + 1
                continue
            prev = existing[key]
            prev["state"] = "resolved"
            prev["reason"] = "no longer open on GitHub"
            await put_item(store, prev)
            summary["resolved"] += 1
    cutoff = time.time() - PRUNE_RESOLVED_AFTER_S
    found_keys = {i.key for i in found}
    for key, prev in existing.items():
        if key in found_keys or key in gone or prev.get("state") != "resolved" or (prev.get("updated_at") or 0) > cutoff:
            continue
        async with item_lock(repo, key):
            if await _unchanged_since(store, repo, key, prev):
                await _forget(store, repo, key, prev)
    return summary


async def _apply_decision(store, config: Config, settings: dict, repo: str, proj: dict, d, summary: dict, *,
                          create_task: CreateTask, notify: Notify) -> None:
    item = d.item
    item.reason = d.reason
    if d.action == "create":
        try:
            item.task_id = await create_task_for_item(item.to_dict(), settings, config, create_task)
            summary["created"] += 1
            await put_item(store, item)
            # The one entry in this log with no person behind it, and the
            # one most worth having: a task appeared, nobody clicked
            # anything, and the only trace otherwise is a Telegram
            # message. The actor names the policy, not an account -- the
            # operator who set the source to Auto is already recorded
            # under settings, at the time they set it.
            await audit.record(
                store, actor="github-inbox", action="inbox.auto_start",
                target=f"{repo}/{item.key}", detail=(item.title or "")[:160],
                extra={"task_id": item.task_id},
            )
            await notify(created_text(item, item.task_id, float(proj["budget_usd"])), repo)
        except Exception as e:  # noqa: BLE001 -- fall back to a proposal rather than lose the item
            logger.exception("github inbox: auto task for %s %s failed", repo, item.key)
            item.state = "proposed"
            item.reason = f"auto start failed: {e}"
            d.action = "propose"
    if d.action == "propose":
        item.approval_nonce = secrets.token_urlsafe(12)
        await put_item(store, item)
        summary["proposed"] += 1
        approve = approval_url(settings, sign_approval(config, repo, item.key, item.approval_nonce, "approve"))
        dismiss = approval_url(settings, sign_approval(config, repo, item.key, item.approval_nonce, "dismiss"))
        await notify(proposal_text(item, approve, dismiss, float(proj["budget_usd"])), repo)
    elif d.action == "none":
        await put_item(store, item)


async def poll_all(store, config: Config, *, create_task: CreateTask, notify: Notify,
                   open_auto_count: OpenAutoCount, live_tasks: LiveTasks | None = None,
                   finished_tasks: FinishedTasks | None = None) -> list[dict]:
    settings = github_settings.current()
    out = []
    for repo in github_settings.enabled_projects(settings):
        try:
            out.append(await poll_project(store, config, settings, repo, create_task=create_task,
                                          notify=notify, open_auto_count=open_auto_count,
                                          live_tasks=live_tasks, finished_tasks=finished_tasks))
        except Exception as e:  # noqa: BLE001 -- one project's failure must not strand the others
            logger.exception("github inbox: poll failed for %s", repo)
            out.append({"repo": repo, "error": str(e)[:200]})
    return out


# ---------------------------------------------------------------------------
# token probe -- for the settings card's Test button
# ---------------------------------------------------------------------------

async def _can(client: GitHubClient, path: str, params: dict) -> bool | None:
    """True/False for a permission the token clearly has or lacks; None when
    the endpoint answered something else (an empty repo has no HEAD, say)."""
    try:
        await client.get(path, params)
        return True
    except PermissionError:
        return False
    except Exception:  # noqa: BLE001
        return None


async def _can_ci(client: GitHubClient, slug: str) -> bool | None:
    """Either "Checks: read" (check runs) or "Actions: read" (workflow runs)
    is enough for the failing-checks source."""
    checks = await _can(client, f"/repos/{slug}/commits/HEAD/check-runs", {"per_page": 1})
    if checks:
        return True
    actions = await _can(client, f"/repos/{slug}/actions/runs", {"per_page": 1})
    if actions:
        return True
    return False if (checks is False or actions is False) else None


async def probe_token(token: str, projects: dict[str, dict]) -> dict:
    """Who the token is, which configured projects it can see, and what it
    may do there. Fine-grained tokens list only their selected repos."""
    client = GitHubClient(token)
    out: dict[str, Any] = {"ok": True, "login": None, "repos": [], "matched": [], "unreached": []}
    try:
        me = await client.get("/user")
        out["login"] = me.get("login")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"token rejected: {e}"}
    # slug -> every project that is a checkout of it. Two projects can share
    # one repository (a fork onboarded twice, or a live and a staging
    # checkout); keyed the other way the later one overwrote the earlier and
    # the probe reported the wrong project reached (2026-09-29). resolve_slug
    # runs git, so it is asked off the loop.
    slugs: dict[str, list[str]] = {}
    for name in projects:
        slug = await asyncio.to_thread(resolve_slug, name)
        if slug:
            slugs.setdefault(slug, []).append(name)
    reached = {n for names in slugs.values() for n in names}
    # A project whose origin is not a GitHub URL the agent can read cannot be
    # matched by any token; say so rather than blaming the token.
    for name in projects:
        if name not in reached:
            out["unreached"].append({"slug": None, "project": name,
                                     "error": "no GitHub origin on this project's checkout, so no token can reach it"})
    try:
        repos = await client.get("/user/repos", {"per_page": 100, "sort": "updated"})
    except Exception as e:  # noqa: BLE001
        repos = []
        out["warning"] = f"could not list repositories: {e}"
    for r in repos:
        slug = r.get("full_name")
        perms = r.get("permissions") or {}
        names = slugs.get(slug) or []
        entry = {"slug": slug, "project": ", ".join(names) or None, "projects": names,
                 "push": bool(perms.get("push")), "pull": bool(perms.get("pull"))}
        out["repos"].append(entry)
        if names:
            entry["dependabot_alerts"] = await _can(client, f"/repos/{slug}/dependabot/alerts", {"per_page": 1})
            entry["code_scanning"] = await _can(client, f"/repos/{slug}/code-scanning/alerts", {"per_page": 1})
            entry["checks"] = await _can_ci(client, slug)
            out["matched"].append(entry)
    # A project whose slug the token did not list may still be reachable
    # (classic tokens list everything; fine-grained ones only selected).
    listed = {e["slug"] for e in out["repos"]}
    for slug, names in slugs.items():
        if slug in listed:
            continue
        name = ", ".join(names)
        try:
            info = await client.repo(slug)
            perms = info.get("permissions") or {}
            out["matched"].append({
                "slug": slug, "project": name, "projects": names, "push": bool(perms.get("push")), "pull": True,
                "dependabot_alerts": await _can(client, f"/repos/{slug}/dependabot/alerts", {"per_page": 1}),
                "code_scanning": await _can(client, f"/repos/{slug}/code-scanning/alerts", {"per_page": 1}),
                "checks": await _can_ci(client, slug),
            })
        except Exception as e:  # noqa: BLE001
            # The reason is the whole answer: a 404 on an organisation's
            # repository from a token whose resource owner is the person, not
            # the organisation, is the usual one (2026-09-28, first Windows
            # install: "checked on GitHub and it has access to all").
            owner = slug.split("/", 1)[0]
            hint = ""
            if out["login"] and owner.lower() != str(out["login"]).lower():
                hint = (f" {slug} belongs to {owner}, not to {out['login']}: a fine-grained token reaches an "
                        f"organisation's repositories only when the organisation is its resource owner, "
                        f"chosen when the token is created.")
            out["unreached"].append({"slug": slug, "project": name, "error": f"{e}.{hint}"[:400]})
    return out
