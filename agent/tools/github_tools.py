"""Read-only GitHub pull-request access for the planner and the coder.

"See PR 12 and fix the audit issues" needs the agent to read a pull
request: its description, its diff, the review comments with file and line,
and the check runs. Until 2026-09-09 nothing here could: the agent knows a
project as a local checkout, and the private repos are invisible to the
planner's web browser.

Host-side on purpose. The token is a secret, and the sandbox is built so the
coder never holds one (agent/tools/sandbox.py, _safe_base_env). These tools
run in the agent process, call the API with GITHUB_TOKEN, and hand the model
TEXT -- the same contract as read_project_file. Which GitHub repo a project
maps to is read from the checkout's own `origin` remote, so nothing is
configured twice.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool
from langgraph.store.base import BaseStore

from agent.config import PROJECTS, Config
from agent.tools.tool_errors import tool_errors_to_text

logger = logging.getLogger("tektonix")

API = "https://api.github.com"
_TIMEOUT = 20
_DIFF_CAP = 60_000        # chars of diff handed to the model
_COMMENT_CAP = 40         # review comments shown
_LIST_CAP = 30            # PRs listed
# A deploy key per project means an SSH host alias per project in ~/.ssh/config
# ("git@github-storefront:owner/repo.git" -- see agent/deploy_keys.py), so any
# host containing "github" counts, not only github.com itself.
_REMOTE_RE = re.compile(
    r"(?:git@[\w.-]*github[\w.-]*:|ssh://git@[\w.-]*github[\w.-]*/|"
    r"https?://(?:[^/@]+@)?(?:www\.)?github\.com/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


def repo_slug_from_remote(url: str) -> str | None:
    """'git@github.com:owner/webapp.git' or 'https://github.com/owner/webapp' -> 'owner/webapp'."""
    m = _REMOTE_RE.match((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# Hits only. A miss used to be cached too, so a project whose origin was
# added after the first lookup (onboarding without a remote, then a push
# set up later) read as "no GitHub origin" until the process restarted
# (2026-09-29). A miss is one `git config` read; it can afford to repeat.
_slug_cache: dict[str, str] = {}


def resolve_slug(repo: str) -> str | None:
    """The GitHub owner/name for a configured project, from its checkout's
    origin. Runs git synchronously: from a coroutine, go through
    asyncio.to_thread (see github_inbox.probe_token)."""
    if repo in _slug_cache:
        return _slug_cache[repo]
    proj = PROJECTS.get(repo) or {}
    slug = None
    for path in (proj.get("live"), proj.get("sandbox")):
        if not path:
            continue
        try:
            # --local, not `git remote get-url`: insteadOf rewrites (a GitHub
            # token helper, Cursor's managed auth) turn git@github.com: into
            # https://x-access-token:...@github.com/ and the slug parser then
            # fails, or worse, the token would sit in a log. The configured
            # value is the one the operator set.
            r = subprocess.run(
                ["git", "-C", path, "config", "--local", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0:
            slug = repo_slug_from_remote(r.stdout)
            if slug:
                break
    if slug:
        _slug_cache[repo] = slug
    return slug


_SLUG_ARG = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


def project_for(name: str, allowed_repos: list[str] | None = None) -> tuple[str | None, str | None]:
    """(project, None), or (None, the message a model reads).

    `name` is a project's own name here, or its GitHub owner/repo (or the
    repository's URL), which is what a model copies out of a pull request
    link in its goal. The owner/repo is matched against each project's own
    origin remote, case-insensitively as GitHub does, so it works for any
    install without a list of names anywhere. Access is the caller's, as
    before: a repository the user cannot see is refused in either form, and
    the list of valid names names only what they can see. Two visible
    projects on one repository is ambiguous and says so.
    """
    visible = sorted(p for p in PROJECTS if allowed_repos is None or p in allowed_repos)
    listing = ", ".join(repr(p) for p in visible) or "none"
    name = (name or "").strip()
    if name in PROJECTS:
        if name in visible:
            return name, None
        return None, f"unknown or inaccessible repo {name!r}. Projects you can use: {listing}."
    m = _SLUG_ARG.match(name)
    if m:
        wanted = f"{m.group(1)}/{m.group(2)}".lower()
        hits = [p for p in visible if (resolve_slug(p) or "").lower() == wanted]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, (f"{name!r} is the repository of more than one project: "
                          f"{', '.join(repr(h) for h in hits)}. Pass the project name instead.")
    return None, (f"unknown or inaccessible repo {name!r}. Pass a project name (projects you can use: "
                  f"{listing}), or the GitHub owner/repo of one of them.")


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}


def _get(token: str, path: str, accept: str = "application/vnd.github+json",
         params: dict[str, Any] | None = None) -> Any:
    """The decoded JSON, or the text for a diff accept type."""
    r = httpx.get(f"{API}{path}", headers=_headers(token, accept), params=params, timeout=_TIMEOUT)
    if r.status_code == 404:
        raise LookupError("not found (or the token has no access to this repository)")
    if r.status_code in (401, 403):
        raise PermissionError(f"GitHub refused ({r.status_code}): the token is missing, expired, or lacks access")
    r.raise_for_status()
    return r.text if accept.endswith("diff") else r.json()


def _sub(obj: dict[str, Any], key: str) -> dict[str, Any]:
    """A nested object of an API payload, or {} when GitHub sent null."""
    return obj.get(key) or {}


def fetch_pull_request(token: str, slug: str, number: int) -> dict[str, Any]:
    """Everything the model needs about one PR, as plain data."""
    pr = _get(token, f"/repos/{slug}/pulls/{number}")
    diff = _get(token, f"/repos/{slug}/pulls/{number}", accept="application/vnd.github.diff")
    review_comments = _get(token, f"/repos/{slug}/pulls/{number}/comments", params={"per_page": 100})
    issue_comments = _get(token, f"/repos/{slug}/issues/{number}/comments", params={"per_page": 100})
    reviews = _get(token, f"/repos/{slug}/pulls/{number}/reviews", params={"per_page": 50})
    checks: list[dict[str, Any]] = []
    head_sha = _sub(pr, "head").get("sha")
    if head_sha:
        try:
            runs = _get(token, f"/repos/{slug}/commits/{head_sha}/check-runs", params={"per_page": 50})
            checks = runs.get("check_runs") or []
        except Exception as e:  # noqa: BLE001 -- checks are optional detail
            logger.info("github: check-runs unavailable for %s#%s: %s", slug, number, e)
    return {"pr": pr, "diff": diff, "review_comments": review_comments, "issue_comments": issue_comments, "reviews": reviews, "checks": checks}


def format_pull_request(data: dict[str, Any], part: str = "all") -> str:
    pr: dict[str, Any] = data["pr"]
    head = _sub(pr, "head")
    base = _sub(pr, "base")
    lines = [
        f"PR #{pr.get('number')}: {pr.get('title')}",
        f"state: {pr.get('state')}{' (draft)' if pr.get('draft') else ''} | author: {_sub(pr, 'user').get('login')} | "
        f"branch: {head.get('ref')} -> {base.get('ref')} | head: {str(head.get('sha'))[:10]} | "
        f"+{pr.get('additions')} -{pr.get('deletions')} in {pr.get('changed_files')} files",
        f"url: {pr.get('html_url')}",
    ]
    if part in ("all", "summary"):
        body = (pr.get("body") or "").strip()
        lines += ["", "DESCRIPTION:", body or "(none)"]
    if part in ("all", "checks", "summary"):
        checks: list[dict[str, Any]] = data.get("checks") or []
        if checks:
            lines += ["", "CHECKS:"]
            lines += [f"- {c.get('name')}: {c.get('status')} / {c.get('conclusion') or '-'}" for c in checks]
    if part in ("all", "comments", "summary"):
        rc: list[dict[str, Any]] = data.get("review_comments") or []
        ic: list[dict[str, Any]] = data.get("issue_comments") or []
        reviews: list[dict[str, Any]] = data.get("reviews") or []
        rv = [r for r in reviews if (r.get("body") or "").strip()]
        if rv:
            lines += ["", f"REVIEWS ({len(rv)}):"]
            lines += [f"- [{r.get('state')}] {_sub(r, 'user').get('login')}: {(r.get('body') or '').strip()[:800]}" for r in rv[:10]]
        if rc:
            lines += ["", f"REVIEW COMMENTS ON THE DIFF ({len(rc)}, showing {min(len(rc), _COMMENT_CAP)}):"]
            for c in rc[:_COMMENT_CAP]:
                where = f"{c.get('path')}:{c.get('line') or c.get('original_line') or '?'}"
                lines.append(f"- {where} ({_sub(c, 'user').get('login')}): {(c.get('body') or '').strip()[:600]}")
        if ic:
            lines += ["", f"CONVERSATION COMMENTS ({len(ic)}):"]
            lines += [f"- {_sub(c, 'user').get('login')}: {(c.get('body') or '').strip()[:600]}" for c in ic[:_COMMENT_CAP]]
        if not (rv or rc or ic):
            lines += ["", "COMMENTS: none"]
    if part in ("all", "diff"):
        diff = data.get("diff") or ""
        if len(diff) > _DIFF_CAP:
            diff = diff[:_DIFF_CAP] + f"\n... (diff truncated at {_DIFF_CAP} chars of {len(data['diff'])}; ask for part='diff' on a narrower PR, or read the changed files)"
        lines += ["", "DIFF:", diff or "(empty)"]
    return "\n".join(lines)


def format_pull_request_list(slug: str, prs: list[dict[str, Any]], state: str) -> str:
    if not prs:
        return f"No {state} pull requests on {slug}."
    lines = [f"{len(prs)} {state} pull request(s) on {slug}" + (f" (showing {_LIST_CAP})" if len(prs) > _LIST_CAP else "") + ":"]
    for p in prs[:_LIST_CAP]:
        lines.append(f"- #{p.get('number')} [{p.get('state')}{', draft' if p.get('draft') else ''}] {p.get('title')} -- {_sub(p, 'user').get('login')}, {_sub(p, 'head').get('ref')} -> {_sub(p, 'base').get('ref')}")
    return "\n".join(lines)


TokenSource = str | Callable[[str], str | None] | None


def make_github_tools(token: TokenSource, allowed_repos: list[str] | None = None) -> list[BaseTool]:
    """The two read-only PR tools, or an empty list when no token is set (the
    prompts only mention them when they exist).

    `token` is a string (the GITHUB_TOKEN env fallback) or a callable that
    returns the token for a given project -- Settings -> GitHub stores one
    per project, and the tools resolve it per call so a token added from the
    dashboard works on the next call without a restart."""
    if not token:
        return []
    def resolve_token(repo: str) -> str | None:
        return token(repo) if callable(token) else token

    def _project(repo: str) -> str:
        project, problem = project_for(repo, allowed_repos)
        if project is None:
            raise ValueError(problem)
        return project

    def _slug_for(repo: str) -> str:
        repo = _project(repo)
        slug = resolve_slug(repo)
        if not slug:
            raise ValueError(f"{repo!r} has no GitHub origin remote, so it has no pull requests here")
        return slug

    def _token_for(repo: str) -> str:
        tok = resolve_token(repo)
        if not tok:
            raise PermissionError(f"no GitHub token is configured for {repo!r} (Settings -> GitHub)")
        return tok

    @tool
    @tool_errors_to_text
    def github_pull_request(repo: str, number: int, part: str = "all") -> str:
        """Read a GitHub pull request on one of the configured projects: its
        description, check results, review comments (with file:line), and diff.
        `part` narrows the output: "summary" (description + checks + comments,
        no diff), "diff", "comments", "checks", or "all". Use this whenever a
        request names a PR ("fix the audit issues on PR 12"): the findings you
        must address are in the review comments, and the diff is the code they
        refer to.
        `repo` is the project's name, or its GitHub owner/repo as it appears in a link."""
        try:
            repo = _project(repo)
            slug = _slug_for(repo)
            data = fetch_pull_request(_token_for(repo), slug, int(number))
        except (ValueError, LookupError, PermissionError) as e:
            return f"ERROR: {e}"
        except httpx.HTTPError as e:
            return f"ERROR: GitHub request failed: {e}"
        part = part if part in ("all", "summary", "diff", "comments", "checks") else "all"
        return format_pull_request(data, part)

    @tool
    @tool_errors_to_text
    def github_pull_requests(repo: str, state: str = "open") -> str:
        """List a project's GitHub pull requests: number, state, title, author
        and branches. `state` is "open" (default), "closed" or "all". Use it
        to find the PR a request refers to by title when no number was given.
        `repo` is the project's name, or its GitHub owner/repo as it appears in a link."""
        try:
            repo = _project(repo)
            slug = _slug_for(repo)
            state = state if state in ("open", "closed", "all") else "open"
            prs = _get(_token_for(repo), f"/repos/{slug}/pulls", params={"state": state, "per_page": 50, "sort": "updated", "direction": "desc"})
        except (ValueError, LookupError, PermissionError) as e:
            return f"ERROR: {e}"
        except httpx.HTTPError as e:
            return f"ERROR: GitHub request failed: {e}"
        return format_pull_request_list(slug, prs, state)

    return [github_pull_request, github_pull_requests]


def available(config: Config) -> bool:
    """Whether the GitHub tools exist on this installation.

    The enablement rule lives here, with the tools, and is read from here by
    anything that needs to report on it -- agent/capabilities.py did briefly
    keep its own copy, which checked GITHUB_TOKEN alone and therefore told
    an operator the tools were missing on a box where they were live via a
    token stored from Settings -> GitHub.

    One caveat the caller has to know: the per-project half of the answer
    comes from github_settings' in-process cache, which is filled by
    load(store). A process that has never loaded it -- scripts/doctor.py,
    say -- sees only the environment variable.
    """
    return token_source(config) is not None


def token_source(config: Config) -> TokenSource:
    """Per-project resolver: the project's stored token from Settings ->
    GitHub, else GITHUB_TOKEN. Falsy when neither exists, so the tools are
    absent rather than present-and-broken."""
    from agent import github_settings
    settings = github_settings.current()
    if not github_settings.any_token(settings, config):
        return None
    return lambda repo: github_settings.token_for(github_settings.current(), config, repo)


_INBOX_KIND: dict[str | None, str] = {
    "dependabot_prs": "Dependabot PR", "security_alerts": "security alert",
    "review_requests": "review requesting changes", "ci_failures": "failing check",
    "code_scanning": "Code scanning alert",
}


def format_inbox(repo: str, items: list[dict[str, Any]], state: str) -> str:
    if not items:
        return f"The GitHub inbox has no {state} items for {repo}."
    lines = [f"{len(items)} {state} GitHub inbox item(s) for {repo}:"]
    for it in items:
        head = f"- [{_INBOX_KIND.get(it.get('kind'), it.get('kind'))}] "
        if it.get("number"):
            head += f"#{it['number']} "
        lines.append(head + f"{it.get('title')} -- state: {it.get('state')}")
        if it.get("summary"):
            lines.append(f"    {it['summary']}")
        if it.get("url"):
            lines.append(f"    {it['url']}")
        if it.get("task_id"):
            lines.append(f"    task: {it['task_id'][:8]}")
    return "\n".join(lines)


def make_github_inbox_tool(store: BaseStore, allowed_repos: list[str] | None = None) -> BaseTool:
    """Read the GitHub inbox (agent/github_inbox.py) for a project: what the
    poller found -- Dependabot PRs, security alerts with the patched version,
    reviews, failing checks -- and what state each is in. Host-side read of
    the store; nothing here reaches GitHub."""
    from agent import github_inbox

    @tool
    @tool_errors_to_text
    async def github_inbox_items(repo: str, state: str = "open") -> str:
        """`repo` is the project's name, or its GitHub owner/repo as it appears in a link.
        List the project's GitHub inbox: code scanning (CodeQL) alerts
        grouped one item per rule (every file:line and message), the
        Dependabot pull requests, Dependabot security alerts (package,
        vulnerable range, patched version, manifest), reviews requesting
        changes and failing checks the poller found on GitHub, each with
        its state. `state` is "open"
        (proposed, seen, snoozed or already turned into a task -- the
        default), "all", or one state name. Use it whenever a request says
        "the alerts in the inbox" or "fix what GitHub flagged": it is the
        exact list, so the plan can name every item instead of guessing
        from the lockfile."""
        project, problem = project_for(repo, allowed_repos)
        if project is None:
            return f"ERROR: {problem}"
        repo = project
        items: list[dict[str, Any]] = list((await github_inbox.list_items(store, repo)).values())
        open_states = ("proposed", "seen", "snoozed", "task_created")
        if state == "open":
            items = [i for i in items if i.get("state") in open_states]
        elif state != "all":
            items = [i for i in items if i.get("state") == state]
        order: dict[str | None, int] = {"code_scanning": 0, "security_alerts": 1, "ci_failures": 2, "review_requests": 3, "dependabot_prs": 4}
        items.sort(key=lambda i: (order.get(i.get("kind"), 9), -(i.get("updated_at") or 0)))
        return format_inbox(repo, items, state)

    return github_inbox_items
