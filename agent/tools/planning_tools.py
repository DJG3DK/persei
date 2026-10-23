"""Tools for the planning chat agent (agent/planning_chat.py) -- a
research/consulting role, not a code-editing one. No write/edit/bash here
deliberately: the planning chat never mutates the target repo itself, it
only reads it for context and drafts a plan document that later gets handed
to a real build task (which has its own full write/edit/bash toolset). That
also means there's no INTERRUPT_ON-style approval gate needed here, unlike
investigator/test-writer -- nothing in this tool list can touch the
filesystem destructively. create_project is no exception: the tool itself
mutates nothing, it records a proposal in plan_ref; the project is created
only after an admin confirms it from the dashboard, through the same server
path as POST /api/projects/create.

Playwright (not a plain httpx GET) is the actual point of `browse_page`: a
raw HTTP fetch can't render JS-heavy pages or take a real screenshot, and
"how does this page actually look" (colors, layout, spacing) is exactly what
a design/UX planning conversation needs that a text-only fetch can't give.
Confirmed working headless on this VPS (chromium already cached under
~/.cache/ms-playwright from another project's Playwright install) with the
same --no-sandbox launch args that project already uses in production.

web_search goes through agent/tools/web_search.py: the model router's
`web-search` alias first, DuckDuckGo as a fallback, and an explicit
"unavailable" rather than a false "no results" when both fail. It scraped
Bing's HTML until 2026-09-30, when Bing started returning empty pages to
this box.
"""

import os
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool, tool

from agent.tools.egress_proxy import REFUSED_HEADER, EgressProxy
from agent.tools.url_guard import UnsafeUrlError, assert_public_url, make_route_guard

from agent import runtime_settings as _rs
from agent.config import PROJECTS
from agent.tools.files import BinaryFileError, PathEscapeError, read_file
# pyright: reportPrivateUsage=false
# files.py's path resolver is shared on purpose: same sandboxing rules, one implementation.
from agent.tools.files import _resolve, own_space_redirect
from agent.tools.tool_errors import tool_errors_to_text
from agent.tools.vision import describe_image_bytes

if TYPE_CHECKING:
    from playwright.async_api import Page, ViewportSize

# audit M-8: --ignore-certificate-errors removed -- this browser renders
# attacker-influenced pages (browse_page on a model-chosen URL, live Bing
# results) whose text enters the model context, so TLS validation must stay
# on; a network-position attacker could otherwise substitute page content.
# --no-sandbox is retained (the container/user story is tracked in M-10) but
# is no longer paired with disabled cert checks.
_LAUNCH_ARGS = ["--no-sandbox", "--disable-setuid-sandbox",
                # WebRTC's UDP does not go through an HTTP proxy.
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 20_000
_PAGE_TEXT_CAP = 6_000
_SEARCH_RESULT_CAP = 10

# How much of a file read_project_file will put in the conversation at once.
# 40_000 is read_file's own default, kept deliberately: this is the size a
# plain read has always returned, so nothing that used to fit changes shape.
# What changes is what happens ABOVE it -- see the tool.
_READ_INLINE_CAP_CHARS = 40_000
# The paging path has to be able to reach past the inline cap, so the actual
# read is bounded far higher (same ceiling agent_tools.read uses).
_READ_HARD_CAP_CHARS = 2_000_000

# A single file may be read this many times in ONE planning turn before the
# tool starts pushing back, and this many before it refuses outright.
#
# Session 0616917 (2026-08-31) read src/core/bot.js 129 times and
# config/pairs.json 103 times in one turn, burning its whole $8 ceiling
# without ever calling save_plan. The underlying cause was summarization
# evicting what had just been read (see PLANNING_SUMMARIZATION_TRIGGER), and
# that is fixed separately -- but a context window is a soft bound and a loop
# that big should not be reachable at all. This is the hard one.
#
# 14 is deliberately generous: paging a 4,000-line file in 500-line windows
# takes 8 reads, so a full legitimate page-through plus slack still fits. 129
# does not.
_READ_WARN_AT = 6
_READ_CAP = 14
# Paged reads return at least this many lines whatever `limit` asks for. The
# prompt recommends 600-800 line windows; a Kimi planning turn on 2026-09-09
# paged a 1,100-line component in 120-line windows, eight calls for one file
# it had already read the turn before, each call resending 120k tokens of
# context. A floor cuts that to two calls with no cooperation required.
_READ_MIN_WINDOW = 500


@asynccontextmanager
async def _browser_page(viewport: "ViewportSize | None" = None,
                        allow_origin: str | None = None) -> AsyncGenerator[tuple["Page", EgressProxy], None]:
    """One headless Chromium page per call -- tool calls here are
    infrequent enough (a handful per planning turn, not a hot loop) that
    launching fresh each time is simpler and safer than managing a shared
    long-lived browser instance across concurrent planning sessions.

    The browser reaches the network only through EgressProxy, which connects
    to the address it checked (agent/tools/egress_proxy.py). The proxy comes
    back beside the page so a caller can say why a load failed."""
    from playwright.async_api import async_playwright

    async with EgressProxy(allow_origin=allow_origin) as proxy, async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS, **proxy.launch_kwargs())
        try:
            page = await browser.new_page(user_agent=_USER_AGENT, viewport=viewport or {"width": 1280, "height": 800})
            yield page, proxy
        finally:
            await browser.close()


async def _run_web_search(query: str, num_results: int, session_id: str | None = None) -> str:
    """The search the planning agent (and anything importing this) runs.
    agent/tools/web_search.py: the router's web-search alias, DuckDuckGo as a
    fallback, and an explicit "unavailable" instead of a false "no results".
    Bing scraping from this box returned empty pages (2026-09-30).

    `session_id` goes on the router call's ledger line, so a session's total
    includes what its searches cost (2026-10-22 audit, A23: they landed in
    routing.jsonl with no owner)."""
    from agent.tools.web_search import web_search  # noqa: PLC0415
    metadata = {"agent_session_id": session_id} if session_id else None
    return await web_search(query, num_results, metadata=metadata)


def make_browse_page_tool():
    """`browse_page` as a standalone tool.

    It lived inside the planning toolset, which meant the agent doing the
    frontend work was the one that could not look at a page -- it could
    describe an image somebody handed it and had no way to produce one.
    """
    from agent.tools.browse_guard import BrowseGuard  # noqa: PLC0415
    from agent.tools.tool_errors import tool_errors_to_text  # noqa: PLC0415

    guard = BrowseGuard()

    @tool
    @tool_errors_to_text
    async def browse_page(url: str, screenshot: bool = False, question: str = "") -> str:
        """Load a real webpage (JS-rendered, via a real headless browser) and
        read its visible text. `screenshot=True` also describes how the page
        actually LOOKS -- layout, colour, typography, spacing -- which is the
        only way to check visual work, since the tests do not. `question`
        narrows either the reading or the description.

        Public addresses only. To look at THIS project, use preview_app.
        """
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"ERROR: {url!r} is not a valid http(s) URL"
        refusal = guard.check(url)
        if refusal:
            return refusal
        result = await _run_browse_page(url, screenshot, question)
        guard.record(url, result)
        return result

    return browse_page


async def run_browse_page_on_origin(url: str, allow_origin: str, question: str = "") -> str:
    """Render a page on ONE loopback origin this process just allocated.

    For previewing an app the agent started, where the public-address rule
    that protects every other browse would block the very thing being looked
    at. The exception is narrow on purpose: the caller supplies the origin, it
    is matched whole, and every other request the page makes still goes
    through the public check -- so a previewed page that pulls a script from
    169.254.169.254 is still refused.
    """
    return await _run_browse_page(url, True, question, allow_origin=allow_origin)


async def _run_browse_page(url: str, want_screenshot: bool, question: str,
                           allow_origin: str | None = None) -> str:

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    # SSRF guard (agent/tools/url_guard.py). Two layers, because the entry
    # check alone is not enough: Playwright follows redirects internally, so
    # a public URL that 302s to 127.0.0.1 or 169.254.169.254 would never come
    # back through here. The route guard re-checks every request the page
    # makes -- initial navigation, each redirect hop, and subresources. Both
    # resolve the name and then let Chromium resolve it again; the egress
    # proxy _browser_page puts under the browser is what makes the address
    # checked the address connected to.
    if not allow_origin:
        try:
            await assert_public_url(url)
        except UnsafeUrlError as e:
            return f"ERROR: {e}"

    blocked: list[str] = []

    async with _browser_page(allow_origin=allow_origin) as (page, egress):
        await page.route("**/*", make_route_guard(
            lambda u, why: blocked.append(f"{u} ({why})"), allow_origin=allow_origin))
        try:
            response = await page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
        except (TimeoutError, PlaywrightTimeoutError):
            # audit M-28: catch Playwright's own TimeoutError too (not an
            # asyncio.TimeoutError subclass), otherwise this was a dead branch.
            return f"ERROR: timed out loading {url!r} (over {_NAV_TIMEOUT_MS // 1000}s)"
        except Exception as e:  # noqa: BLE001 -- bad URL, DNS failure, refused connection, etc.
            if egress.blocked:
                return f"ERROR: blocked: {url!r} reached a non-public address -- {egress.blocked[0]}"
            return f"ERROR: failed to load {url!r}: {e}"

        if blocked and not page.url.startswith(("http://", "https://")):
            return f"ERROR: blocked redirect to a non-public address -- {blocked[0]}"
        if egress.blocked and response is not None and response.headers.get(REFUSED_HEADER):
            return f"ERROR: blocked: {url!r} reached a non-public address -- {egress.blocked[0]}"

        title = await page.title()
        try:
            text = (await page.inner_text("body")).strip()
        except Exception:
            text = ""
        if len(text) > _PAGE_TEXT_CAP:
            text = text[:_PAGE_TEXT_CAP] + f"\n... [truncated, {len(text) - _PAGE_TEXT_CAP} more chars]"

        status = response.status if response is not None else None
        parts = [f"# {title or url}", f"URL: {page.url}", "", text or "(no visible text extracted)"]
        if status and status >= 400:
            # First, so the browse guard and the model both read the page
            # as the error it is before any of its text.
            parts.insert(0, f"ERROR: HTTP {status} -- this page does not exist or refused the request.")
        links = await _page_links(page)
        if links:
            # The real addresses on the page, so the next page is one the site
            # links to rather than one the model imagines (2026-09-30: twenty
            # invented docs paths, nearly all "Page Not Found").
            parts.append("\n## Links on this page\n" + "\n".join(links))

        if want_screenshot:
            png_bytes = await page.screenshot(type="png")
            try:
                description = await describe_image_bytes(
                    png_bytes,
                    "image/png",
                    question.strip() or (
                        "Describe this webpage's visual design for a UI/UX planning conversation: "
                        "layout, color palette, typography style, spacing, and any notable UI patterns."
                    ),
                )
                parts.append("\n## Visual description (screenshot)\n" + description)
            except Exception as e:  # noqa: BLE001 -- text extraction above still succeeded, don't lose that
                parts.append(f"\n## Visual description (screenshot)\nERROR: vision call failed: {e}")

        return "\n".join(parts)


_LINKS_MAX = 40
_LINKS_CHARS = 2_500


async def _page_links(page: "Page") -> list[str]:
    """Up to _LINKS_MAX distinct http(s) links on the page, same site first,
    as "text -> url". Never raises: a page that will not give its links still
    gives its text."""
    try:
        raw: Any = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [(e.innerText || e.getAttribute('aria-label') || '').trim(), e.href])")
    except Exception:  # noqa: BLE001
        return []
    here = (urlparse(page.url).hostname or "").lower()
    seen: set[str] = set()
    same: list[str] = []
    other: list[str] = []
    pairs: list[Any] = raw or []
    for label, href in pairs:
        if not isinstance(href, str) or not href.startswith(("http://", "https://")):
            continue
        href = href.split("#", 1)[0]
        if href in seen or href.rstrip("/") == page.url.split("#", 1)[0].rstrip("/"):
            continue
        seen.add(href)
        line = f"- {' '.join(str(label).split())[:80] or '(link)'} -> {href}"
        (same if (urlparse(href).hostname or "").lower() == here else other).append(line)
    out: list[str] = []
    size = 0
    for line in same + other:
        if len(out) >= _LINKS_MAX or size + len(line) > _LINKS_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    return out


def _own_space_redirect(path: str) -> str | None:
    return own_space_redirect(path, "read_file")


def _reread_note(path: str, seen: int) -> str:
    """Warn before the cap bites, and say what to do instead.

    The model cannot see that its earlier reads were summarized away, so from
    inside the loop each re-read looks like a first read. Naming the count is
    the only signal it gets that it is going in circles."""
    if seen < _READ_WARN_AT:
        return ""
    return (
        f"\n\n[You have now read {path!r} {seen} times in this turn. Older tool results get "
        f"summarized out of context; your own written text does not. Record what this file told "
        f"you in your next reply instead of reading it again -- after {_READ_CAP} reads this tool "
        f"stops returning it.]"
    )

def _project_root(repo: str, allowed_repos: list[str] | None = None) -> str:
    if repo not in PROJECTS:
        raise ValueError(f"unknown repo {repo!r} -- must be one of {list(PROJECTS)}")
    # audit H-2: the repo argument is model-chosen. Without this check a
    # restricted user could open a session on a repo they ARE allowed, then
    # ask the model to read a file from one they are NOT -- the allow-list the
    # auth module documents as a per-user control was silently overridden. None
    # means "no restriction" (a full-access operator), an explicit list gates.
    if allowed_repos is not None and repo not in allowed_repos:
        raise ValueError(f"access to repo {repo!r} is not permitted for this session")
    return PROJECTS[repo]["sandbox"]


# ---------------------------------------------------------------------------
# Repo search for planning (2026-09-09). The planner had no search at all --
# the built-in grep/glob see the agent's own memory/skills space, and a
# session that kept "grepping" the wrong space looped on "No matches" until
# they were hidden. Without search, "restyle the frontend" meant opening
# every file: a Kimi turn read 250 files in 100-line windows, compacted twice,
# and spent $13 before writing a line. Kimi's own CLI finds the six files
# that matter with one grep. These are that grep, against the real repo,
# with the loop-proofing the old one lacked (see _search_gate).
# ---------------------------------------------------------------------------
_SEARCH_RESULTS_CAP = 100      # hard ceiling on max_results
_SEARCH_PER_FILE_CAP = 8       # hits shown per file (rg -m)
_SEARCH_OUTPUT_CHARS = 8_000   # what may enter the context from one call
_SEARCH_TIMEOUT_S = 20
_FIND_CAP = 200


def _rel_target(repo_root: str, path: str) -> str:
    """The repo-relative form of `path`, escape-checked. rg runs with
    cwd=repo_root so every hit comes back repo-relative."""
    target = _resolve(repo_root, path or ".")
    rel = os.path.relpath(str(target), repo_root)
    return "." if rel == "." else rel


def _rg(args: list[str], cwd: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["rg", *args], cwd=cwd, capture_output=True, text=True, timeout=_SEARCH_TIMEOUT_S)
    except FileNotFoundError:
        return 2, "ripgrep (rg) is not installed on this host"
    except subprocess.TimeoutExpired:
        return 2, f"search timed out after {_SEARCH_TIMEOUT_S}s -- narrow `path` or `glob`"
    return r.returncode, r.stdout if r.returncode in (0, 1) else (r.stderr.strip() or r.stdout)


def _files_scanned(repo_root: str, rel: str, glob: str | None) -> int:
    args = ["--files"]
    if glob:
        args += ["-g", glob]
    code, out = _rg([*args, "--", rel], repo_root)
    return len(out.splitlines()) if code == 0 else 0


def run_search(repo_root: str, pattern: str, path: str = ".", glob: str | None = None, fixed: bool = False, max_results: int = 40) -> str:
    """ripgrep over the repo, as text for the model. Zero hits come back with
    a diagnosis (files scanned, what to change) rather than a blank."""
    rel = _rel_target(repo_root, path)
    if not os.path.exists(os.path.join(repo_root, rel)):
        return f"ERROR: {path!r} does not exist in this repo -- list_project_dir(repo, '.') to see what does"
    if not pattern or not pattern.strip():
        return "ERROR: pattern is empty"
    max_results = max(1, min(int(max_results or 40), _SEARCH_RESULTS_CAP))
    args = ["-n", "--no-heading", "--color", "never", "--max-columns", "200", "--max-columns-preview",
            "-m", str(_SEARCH_PER_FILE_CAP), "--max-filesize", "2M"]
    if fixed:
        args.append("-F")
    if glob:
        args += ["-g", glob]
    args += ["-e", pattern, "--", rel]
    code, out = _rg(args, repo_root)
    if code == 2:
        if "regex parse error" in out or "error parsing" in out:
            return (
                f"ERROR: {pattern!r} is not a valid regex ({out.strip().splitlines()[-1][:120]}). "
                f"Set fixed=True to search for it literally, or escape the special characters."
            )
        return f"ERROR: search failed: {out.strip()[:300]}"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    where = f"under {rel!r}" + (f" (glob {glob!r})" if glob else "")
    if not lines:
        n = _files_scanned(repo_root, rel, glob)
        hint = (
            "Next: search from '.' instead of a subdirectory" if rel != "." else "Next: shorten the pattern"
        )
        if not fixed and any(ch in pattern for ch in "()[]{}.*+?|\\$^"):
            hint += ", or set fixed=True (your pattern contains regex characters)"
        return f"No matches for {pattern!r} {where}: {n} files scanned. {hint}, or try a different word from the brief."
    shown = lines[:max_results]
    files = len({ln.split(":", 1)[0] for ln in lines})
    head = f"{len(lines)} hit(s) for {pattern!r} {where} across {files} file(s), showing {len(shown)}"
    if len(lines) > len(shown):
        head += f"; {len(lines) - len(shown)} more not shown -- narrow with path= or glob="
    body = "\n".join(shown)
    if len(body) > _SEARCH_OUTPUT_CHARS:
        body = body[:_SEARCH_OUTPUT_CHARS] + "\n... (truncated; narrow the search)"
    return head + ":\n" + body


def run_find(repo_root: str, glob: str, path: str = ".") -> str:
    """The .gitignore-aware file list matching a glob, as text for the model."""
    rel = _rel_target(repo_root, path)
    if not os.path.exists(os.path.join(repo_root, rel)):
        return f"ERROR: {path!r} does not exist in this repo -- list_project_dir(repo, '.') to see what does"
    if not glob or not glob.strip():
        return "ERROR: glob is empty (e.g. '**/*.css' or '*Chart*.tsx')"
    code, out = _rg(["--files", "-g", glob, "--", rel], repo_root)
    if code == 2:
        return f"ERROR: find failed: {out.strip()[:300]}"
    files = sorted(ln for ln in out.splitlines() if ln.strip())
    if not files:
        return f"No files match {glob!r} under {rel!r}. Globs are matched against the path relative to the repo root; try '**/{glob.lstrip('*/')}' or a wider path."
    shown = files[:_FIND_CAP]
    head = f"{len(files)} file(s) match {glob!r} under {rel!r}"
    if len(files) > len(shown):
        head += f", showing {len(shown)}"
    return head + ":\n" + "\n".join(shown)


def make_planning_tools(
    existing_plan: str | None = None,
    allowed_repos: list[str] | None = None,
    existing_brief: dict[str, Any] | None = None,
    skills_manifest: dict[str, str] | None = None,
    is_admin: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
) -> tuple[list[BaseTool], dict[str, Any]]:
    """Returns ([web_search, browse_page, preview_app, list_project_dir,
    read_project_file, search_project, find_files, save_brief, save_plan,
    create_project], plan_ref).

    `is_admin`/`actor` are the caller's role and email, threaded in
    explicitly the way `allowed_repos` is (agent/routers/planning.py
    send_planning_message -> server.py build_planning_agent -> here). They cannot be derived from
    `allowed_repos`: it is None for admins AND for legacy unscoped accounts,
    so only an explicit flag can gate create_project the way the endpoint it
    feeds (POST /api/projects/create) is gated. Both default off, so a caller
    that builds the tools without a user gets a planner that can propose
    nothing.

    plan_ref also carries "brief": the dict save_brief last wrote (seeded from
    `existing_brief`), which BriefFirstMiddleware/PinnedBriefMiddleware read
    (agent/middleware/pinned_brief.py). `skills_manifest` ({name: description})
    is what save_brief matches the request against to tell the model which
    architecture skills to read before it opens a repo file.

    Unlike agent_tools.py's read/list tools (closure-bound to one repo_root
    at construction time, matching a build task's single-repo scope),
    list_project_dir/read_project_file take `repo` as an explicit argument --
    planning is deliberately cross-project: comparing how two of this
    operator's projects each solve something, or pulling a pattern from one
    into a plan for another, is real, common planning-conversation value that
    a single-repo-scoped tool can't provide at all.

    `plan_ref` is a mutable dict (`{"markdown": str | None}`) the caller
    reads back after each turn -- same pattern as agent_tools.py's
    last_failed_edit_ref: the tool's closure is the only thing that can see
    the model's save_plan call as it happens, so the result has to come back
    through a shared mutable reference, not a return value (tools return
    strings to the model, not structured data to the caller).
    """
    # Per-TURN read ledger: make_planning_tools is called once per turn (see
    # build_planning_agent), so this counts reads within a single turn and
    # resets naturally on the next one.
    read_counts: dict[tuple[str, str], int] = {}
    # Repo search: identical-call counter and last result (the repeat guard),
    # plus the per-turn budget. See search_project below and this module's
    # run_search/run_find for why each exists.
    search_seen: dict[tuple[str | bool, ...], int] = {}
    search_last: dict[tuple[str | bool, ...], str] = {}
    search_calls = {"n": 0}
    # The draft gate: reads since the last save_plan. Past the budget the read
    # tool closes until a plan is saved -- reading is never the deliverable,
    # and a session that reads instead of writing is the failure this exists
    # for (2026-09-09: 250 reads, two compactions, $13, no draft).
    reads = {"since_save": 0, "gated": False}

    # Seeded with whatever the session already has saved. A planning agent is
    # rebuilt from scratch on EVERY turn, so a plan_ref that always started at
    # None meant the session's own draft was invisible to the turn that came
    # after it -- and, worse, got clobbered (see run_planning_turn's caller).
    plan_ref: dict[str, Any] = {"markdown": existing_plan, "brief": existing_brief}
    skills_manifest = skills_manifest or {}
    # One per turn: this toolset is rebuilt for every planning turn.
    from agent.tools.browse_guard import BrowseGuard  # noqa: PLC0415
    browse_guard = BrowseGuard()

    @tool
    @tool_errors_to_text
    async def web_search(query: str, num_results: int = 6) -> str:
        """Search the web for research, documentation, competitor examples,
        or design/UX inspiration. Returns up to `num_results` results, each with a
        title, real URL, and snippet. Use browse_page on a promising URL to read
        the full page or see what it actually looks like."""
        num_results = max(1, min(num_results, _SEARCH_RESULT_CAP))
        try:
            return await _run_web_search(query, num_results, session_id)
        except Exception as e:  # noqa: BLE001 -- surfaced to the model as a tool result, not raised
            return f"ERROR: web search failed: {e}"

    @tool
    @tool_errors_to_text
    async def browse_page(url: str, screenshot: bool = False, question: str = "") -> str:
        """Load a real webpage (JS-rendered, via a real headless browser) and
        extract its visible text. Pass `screenshot=True` to ALSO get a visual
        description of how the page actually looks (layout, colors, typography,
        UI patterns) -- use this whenever the user is asking about a site's design
        or you want to reference/compare a competitor's UI. `question` narrows
        either the text reading or the visual description to something specific."""
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"ERROR: {url!r} is not a valid http(s) URL"
        refusal = browse_guard.check(url)
        if refusal:
            return refusal
        try:
            result = await _run_browse_page(url, screenshot, question)
        except Exception as e:  # noqa: BLE001
            result = f"ERROR: browse_page failed: {e}"
        browse_guard.record(url, result)
        return result

    @tool
    @tool_errors_to_text
    def list_project_dir(repo: str, path: str = ".") -> str:
        """List files and subdirectories at `path` (repo-relative, e.g. "src" or
        "." for the repo root) within `repo` -- one of the operator's configured
        projects, not necessarily the one this session is about. Read-only, for
        getting oriented in a project (this session's own, or another one for
        comparison/reference) before planning changes to it."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            target = _resolve(repo_root, path)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        if not target.exists():
            return f"ERROR: {path!r} does not exist in {repo!r}"
        if not target.is_dir():
            return f"ERROR: {path!r} is a file, not a directory -- use read_project_file to view it"
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError as e:
            # Not reachable via the .exists()/.is_dir() checks above (both
            # answer False rather than raising), but a directory can still be
            # unreadable at the moment we open it -- permissions, a mount that
            # went away mid-session. Name the repo-relative path, never the
            # host one (see _resolve's own comment in files.py).
            return f"ERROR: cannot list {path!r} in {repo!r}: {e.strerror or e}"
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
        return "\n".join(lines) if lines else "(empty directory)"

    @tool
    @tool_errors_to_text
    def read_project_file(repo: str, path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a file (repo-relative path) from `repo` -- one of the operator's
        configured projects, not necessarily the one this session is about.
        Read-only. Use this to understand an existing codebase/design (this
        session's own project, or another one to compare/borrow a pattern from)
        before proposing changes to it.

        For LARGE files, page through THIS tool: `offset` is the 1-based line
        to start from, `limit` the number of lines (e.g. offset=600,
        limit=600); a paged read always returns at least 500 lines, so page in
        500+ steps. Prefer search_project to find the lines you need first. A plain read of a big file returns its beginning plus the
        line count -- follow up with offset/limit rather than asking for the
        whole file again, which just returns the identical text."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        read_budget = _rs.as_int("planning_read_budget")
        if reads["since_save"] >= read_budget:
            reads["gated"] = True
            return (
                f"ERROR: {reads['since_save']} file reads since the last saved plan -- the read budget "
                f"({read_budget}) is spent. Save a DRAFT of the plan NOW with save_plan, from what you already "
                f"know, listing anything still uncertain as an open question. This is a checkpoint, not the end "
                f"of the turn: reads reopen after the save, and you then continue investigating the open "
                f"questions and save the finished plan. To find something specific, use search_project and "
                f"then read only the window it points to."
            )
        reads["since_save"] += 1
        # Counted before the read, so a refusal costs nothing. Keyed on the
        # file rather than the exact window: the loop this exists to stop
        # paged through the same file with VARYING offsets, so a same-window
        # check would not have caught it.
        seen = read_counts[(repo, path)] = read_counts.get((repo, path), 0) + 1
        if seen > _READ_CAP:
            return (
                f"ERROR: you have already read {path!r} in {repo!r} {seen - 1} times this turn. "
                f"Re-reading it is not producing new information, and this tool will not return it "
                f"again. If you are re-reading because earlier results dropped out of context, that "
                f"will keep happening -- WRITE what you learned into your reply as you go, because "
                f"your own written conclusions survive summarization and raw tool output does not. "
                f"Answer with what you have, or read a file you have not read yet."
            )

        try:
            content = read_file(repo_root, path, max_chars=_READ_HARD_CAP_CHARS)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        except BinaryFileError as e:
            return f"ERROR: {e}"
        except FileNotFoundError:
            return f"ERROR: {path!r} does not exist in {repo!r}"
        except IsADirectoryError:
            return (
                f"ERROR: {path!r} is a directory in {repo!r}, not a file -- "
                f"use list_project_dir to see what's inside it"
            )
        except NotADirectoryError:
            # The live one: every sandbox is a git worktree, whose `.git` is a
            # one-line pointer FILE, so `.git/HEAD` (a reasonable-looking guess
            # for "what branch is this on?") resolves THROUGH a file. Say that
            # plainly -- the raw errno text sends the model looking for a
            # missing file that is actually right there.
            return (
                f"ERROR: {path!r} cannot be read from {repo!r} -- a directory in that path is "
                f"actually a file (a git worktree's .git is a pointer file, not a directory, so "
                f"nothing under .git/ is readable this way)"
            )
        except OSError as e:
            return f"ERROR: cannot read {path!r} in {repo!r}: {e.strerror or e}"

        # Paging exists here for the same reason it does on agent_tools.read: a
        # model's instinct when a result comes back truncated is to ask for the
        # file again, and without a way to page that returns the IDENTICAL
        # truncated text forever. The planning agent has no bash to fall back
        # on -- read_project_file is its only route into a repo -- so a file
        # bigger than the cap was simply unreachable past its first 40k chars.
        # Confirmed live 2026-08-27: a session hit this on a 74_703-char
        # strategy file, reported "the file keeps truncating at the same
        # point", and escalated into subagent investigations to get around it.
        if offset or limit:
            lines = content.split("\n")
            start = max(0, (offset or 1) - 1)
            count = max(limit if limit and limit > 0 else 400, _READ_MIN_WINDOW)
            slice_lines = lines[start:start + count]
            if not slice_lines:
                return f"(no lines at offset {offset} -- {path!r} has {len(lines)} lines)"
            numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(slice_lines))
            remaining = len(lines) - (start + len(slice_lines))
            footer = f"\n\n[lines {start + 1}-{start + len(slice_lines)} of {len(lines)}" + (
                f"; {remaining} more after this]" if remaining > 0 else "]"
            )
            return numbered + footer + _reread_note(path, seen)

        if len(content) > _READ_INLINE_CAP_CHARS:
            head = content[:_READ_INLINE_CAP_CHARS]
            shown = head.count("\n") + 1
            total_lines = content.count("\n") + 1
            # Say plainly that repeating the call is pointless and give the
            # exact next call to make -- a bare "[truncated]" marker is what
            # the model kept walking into.
            return (
                f"{head}\n\n[TRUNCATED. {path!r} is {len(content)} chars / {total_lines} lines; "
                f"you have seen lines 1-{shown}. Reading it again the same way returns this SAME "
                f"text -- to see the rest, call read_project_file with offset/limit, e.g. "
                f"read_project_file(repo={repo!r}, path={path!r}, offset={shown}, limit=800). Use "
                f"BIG windows; 50-100 line slices just loop.]"
            ) + _reread_note(path, seen)
        return content + _reread_note(path, seen)

    @tool
    @tool_errors_to_text
    def save_plan(markdown: str) -> str:
        """Save (or replace) the current draft plan document, in Markdown. Call
        this whenever the plan is ready or has meaningfully changed -- this is
        what the user's "Build Now" button hands off to the build system, so it
        should be a complete, self-contained spec a builder could act on without
        this conversation's context: goal, key requirements/decisions gathered so
        far, and any design/UX direction. You can call this multiple times as the
        plan evolves; each call replaces the previous draft."""
        plan_ref["markdown"] = markdown
        reads["since_save"] = 0  # the draft gate reopens: reads now refine a plan that exists
        if reads["gated"]:
            # A save forced by the gate is a checkpoint. The plain "Plan saved"
            # reply read as "you're done": on 2026-09-09 a session hit the gate,
            # saved, made one more read and ended the turn with its open
            # questions unanswered. Say what happens next, explicitly.
            reads["gated"] = False
            budget = _rs.as_int("planning_read_budget")
            return (
                f"Draft saved ({len(markdown)} chars). The read budget has reset: you can read {budget} more files. "
                f"This save was forced by the budget, so treat it as a DRAFT and keep working in this same turn: "
                f"take the open questions you listed, search_project for each, read only the windows the hits point "
                f"to, and call save_plan again with the finished plan. End the turn only when the plan is complete "
                f"or you genuinely need the operator's answer to proceed."
            )
        return "Plan saved. The user can now see it and use \"Build Now\" whenever they're ready."

    def _search_gate(key: tuple[str | bool, ...]) -> str | None:
        """The two things that made the old grep loop: a budget so a turn
        cannot search instead of writing, and a repeat guard so an unchanged
        query cannot be re-run in the hope of a different answer."""
        budget = _rs.as_int("planning_search_budget")
        search_calls["n"] += 1
        if search_calls["n"] > budget:
            return (
                f"ERROR: this turn's search budget ({budget} searches) is spent. You have found what "
                f"searching will find -- save the plan now with save_plan, listing anything still open "
                f"as an open question, rather than searching further."
            )
        seen = search_seen[key] = search_seen.get(key, 0) + 1
        if seen == 2 and key in search_last:
            return (
                "IDENTICAL to a search you already ran this turn -- the repo has not changed, so the "
                "result is the same:\n" + search_last[key][:800] + "\n(Change the pattern, path or glob, "
                "or read one of the files it returned.)"
            )
        if seen > 2:
            return (
                f"ERROR: you have run this exact search {seen} times this turn. It will not answer "
                f"differently. Change the pattern, narrow or widen `path`, use `fixed=True` for a "
                f"literal, or read a file it already returned."
            )
        return None

    @tool
    @tool_errors_to_text
    def search_project(repo: str, pattern: str, path: str = ".", glob: str | None = None, fixed: bool = False, max_results: int = 40) -> str:
        """Search the real repo with ripgrep. `pattern` is a regex (set `fixed=True`
        for a literal string); `path` narrows to a directory ("frontend/src");
        `glob` narrows to files ("*.tsx", "**/*.css"). Returns `file:line: text`
        hits, capped per file and overall. SEARCH FIRST, then read only the window
        a hit points to with read_project_file(offset=..., limit=...). A search
        with no hits tells you how many files it scanned and what to change --
        do not rerun it unchanged."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        key = ("search", repo, pattern, path, glob or "", bool(fixed))
        gate = _search_gate(key)
        if gate:
            return gate
        try:
            result = run_search(repo_root, pattern, path=path, glob=glob, fixed=fixed, max_results=max_results)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        search_last[key] = result
        return result

    @tool
    @tool_errors_to_text
    def find_files(repo: str, glob: str, path: str = ".") -> str:
        """List the repo files matching a glob ("**/*.css", "*Chart*.tsx"),
        .gitignore-aware (node_modules, dist and build output never appear).
        `path` narrows the search to a directory. Cheaper than list_project_dir
        for "where are all the X files" questions."""
        redirect = _own_space_redirect(path)
        if redirect:
            return redirect
        try:
            repo_root = _project_root(repo, allowed_repos)
        except ValueError as e:
            return f"ERROR: {e}"
        key = ("find", repo, glob, path)
        gate = _search_gate(key)
        if gate:
            return gate
        try:
            result = run_find(repo_root, glob, path=path)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        search_last[key] = result
        return result

    @tool
    @tool_errors_to_text
    def save_brief(goal: str, deliverable: str, out_of_scope: str = "", needs: str = "") -> str:
        """Write the brief for this request BEFORE reading anything. `goal`: what
        the operator wants, in one or two sentences, in their terms. `deliverable`:
        what this session must produce (a plan for X; an answer to Y; a comparison
        of A and B). `out_of_scope`: what you will deliberately not touch or
        investigate. `needs`: the strategy/module/files/skills you expect to need,
        by name. The brief is pinned into your context for the rest of the
        conversation, so write it as the yardstick every later read and every
        paragraph of the plan is measured against. Call it again whenever the
        operator's request changes."""
        brief: dict[str, Any] = {
            "goal": goal.strip(),
            "deliverable": deliverable.strip(),
            "out_of_scope": out_of_scope.strip(),
            "needs": needs.strip(),
        }
        brief["matched_skills"] = match_skills(f"{goal} {deliverable} {needs}", skills_manifest)
        plan_ref["brief"] = brief
        if brief["matched_skills"]:
            listing = "\n".join(
                f"- /skills/{name}/SKILL.md -- {skills_manifest[name][:160]}" for name in brief["matched_skills"]
            )
            return (
                "Brief pinned. These registered skills match the request -- read them with read_file "
                f"BEFORE any list_project_dir/read_project_file call:\n{listing}"
            )
        return (
            "Brief pinned. No registered skill matches this request by name; start with "
            "/skills/codebase-map/SKILL.md and read only the files the goal needs."
        )

    @tool
    @tool_errors_to_text
    def create_project(name: str, description: str = "", github: bool = False) -> str:
        """Propose a NEW project for what the operator is describing -- a new
        application, not a change to a project that already exists. Call it
        ONCE, only after the operator has confirmed the project name and
        whether a private GitHub repo should be created. `name` becomes the
        directory and projects.json key (letters, digits, '.', '_', '-'; not
        starting with '.'); `description` is one line for the README and the
        GitHub repo. Nothing is created by this call: the operator sees a
        Confirm card in the dashboard and the project is created when they
        confirm it. Keep planning as if the project already exists."""
        if not is_admin:
            return ("ERROR: creating projects is admin-only; ask an admin to create it from "
                    "Settings -> Projects or the planner")
        from agent import provisioning  # noqa: PLC0415 -- server-side module, imported lazily like the endpoint does

        try:
            # The same validator the endpoint runs, against the same live
            # project list, so a name refused here is refused for the reason
            # the confirm would give -- and the model can ask for another
            # name now rather than after the operator has clicked Confirm.
            name = provisioning.validate_project_name(name, list(PROJECTS))
        except provisioning.ProvisioningError as e:
            return f"ERROR: {e.detail}"
        plan_ref["new_project"] = {
            "name": name,
            "description": (description or "").strip(),
            "github": bool(github),
            "proposed_by": actor,
        }
        return (
            f"Proposal recorded: new project {name!r}"
            + (" with a private GitHub repo" if github else "")
            + ". The operator will see a Confirm card in the dashboard; the project is created when "
            "they confirm it and this conversation moves onto it. Do not call create_project again "
            "unless the operator changes the name. Keep planning as if the project already exists."
        )

    # Standing the project up and looking at it. browse_page reaches a site
    # that is already running; this one is for a project that is not, which
    # is most of them during planning.
    from agent.tools.preview import make_project_preview_tool  # noqa: PLC0415

    preview_app = make_project_preview_tool(allowed_repos)

    return [web_search, browse_page, preview_app, list_project_dir, read_project_file,
            search_project, find_files, save_brief, save_plan, create_project], plan_ref


_STOPWORDS = frozenset("""
the and for with that this from into when what which where read before touching how works
your you are its his her they them will must should would could about after over under
also then than more most some such only into onto upon each every either both been being
have has had does did done make made take took use used using work works working file
files module modules code strategy strategies bot agent plan planning settings setting
""".split())
_SKILL_ALWAYS_EXCLUDED = frozenset({"codebase-map"})  # already mandated by the system prompt


def _keywords(text: str) -> set[str]:
    """Lowercase tokens worth matching on: words of 4+ letters that are not
    filler, plus every identifier-ish token (camelCase names, file stems) so
    `trendSignal.js` in a request meets `trendSignal.js` in a description."""
    import re

    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_./-]*", text):
        stem = tok.rsplit("/", 1)[-1]
        stem = re.sub(r"\.(js|mjs|ts|tsx|jsx|py|md|json|ya?ml)$", "", stem, flags=re.I)
        low = stem.lower()
        if len(low) >= 4 and low not in _STOPWORDS:
            out.add(low)
        # split camelCase / snake_case so "trend signal" meets "trendSignal"
        for part in re.split(r"(?<=[a-z0-9])(?=[A-Z])|[_-]", stem):
            pl = part.lower()
            if len(pl) >= 4 and pl not in _STOPWORDS:
                out.add(pl)
    return out


def match_skills(request_text: str, skills_manifest: dict[str, str], limit: int = 4) -> list[str]:
    """Registered skills whose name/description share distinctive words with
    the request, best first. Deterministic and cheap: this runs inside the
    save_brief tool, not in the model, so the routing does not depend on the
    model noticing a one-line description among a dozen."""
    want = _keywords(request_text)
    if not want or not skills_manifest:
        return []
    scored: list[tuple[int, str]] = []
    for name, description in skills_manifest.items():
        if name in _SKILL_ALWAYS_EXCLUDED:
            continue
        have = _keywords(f"{name} {description}")
        # a hit on the skill's own name is worth more than one on its description
        name_hits = len(want & _keywords(name))
        score = len(want & have) + 2 * name_hits
        if score >= 2 or name_hits:
            scored.append((score, name))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [name for _, name in scored[:limit]]
