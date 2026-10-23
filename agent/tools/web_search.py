"""Web search, through the router.

The planning agent and the chat used to scrape Bing with a headless browser.
From a datacenter address Bing answers a headless browser with an empty page
("no results" for "simplewebauthn"), and in the box's own locale at that; a
planning turn then spent twenty calls guessing documentation URLs
(2026-09-30). So the search is OpenRouter's web plugin, reached through the
router's `web-search` alias: a real index, cited results with a snippet each,
and the cost on the router ledger like every other call.

Order of attempts:
  1. the router's `web-search` alias (OpenRouter web plugin);
  2. DuckDuckGo's plain HTML endpoint (no browser; rate-limited, so a
     fallback, not a primary);
  3. a message that says search is unavailable and not to guess URLs --
     never a bare "no results", which reads to a model as "the thing does
     not exist".
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("tektonix")

ALIAS = "web-search"
MAX_RESULTS = 10
_SNIPPET_CHARS = 400
_ROUTER_TIMEOUT_S = 45
_DDG_URL = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

UNAVAILABLE = (
    "Web search is unavailable right now ({why}). This is an outage, not an answer: it does not "
    "mean nothing exists. Do not guess page addresses. Use a URL you already have from an earlier "
    "result or a page you loaded, work from what you already know, or say plainly that you could "
    "not check the web."
)


@dataclass
class Result:
    title: str
    url: str
    snippet: str


def format_results(query: str, results: list[Result], source: str) -> str:
    if not results:
        return (f"No results for {query!r} ({source}). The search itself worked; try different words "
                f"rather than guessing page addresses.")
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        snippet = " ".join(r.snippet.split())
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS].rstrip() + "…"
        lines.append(f"{i}. {r.title or '(untitled)'}\n   {r.url}" + (f"\n   {snippet}" if snippet else ""))
    return "\n\n".join(lines)


def _router() -> tuple[str, str]:
    return (os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:4001/v1").rstrip("/"),
            os.environ.get("MODEL_ROUTER_KEY", ""))


def parse_citations(payload: dict[str, Any], limit: int) -> list[Result]:
    """OpenRouter's `url_citation` annotations, de-duplicated by URL."""
    out: list[Result] = []
    seen: set[str] = set()
    choices: list[Any] = payload.get("choices") or []
    for choice in choices:
        message: dict[str, Any] = choice.get("message") or {}
        annotations: list[Any] = message.get("annotations") or []
        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            c = cast(dict[str, Any], ann).get("url_citation")
            if not isinstance(c, dict):
                continue
            c = cast(dict[str, Any], c)
            url = str(c.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            out.append(Result(str(c.get("title") or "").strip(), url, str(c.get("content") or "")))
            if len(out) >= limit:
                return out
    return out


async def _via_router(query: str, n: int, metadata: dict[str, Any] | None) -> list[Result]:
    base, key = _router()
    if not key:
        raise RuntimeError("no router key")
    body = {
        "model": ALIAS,
        "plugins": [{"id": "web", "max_results": n}],
        "messages": [{"role": "user", "content": f"Search the web for: {query}\nList the sources you found, one URL per line."}],
        "max_tokens": 300,
        "metadata": metadata or {},
    }
    async with httpx.AsyncClient(timeout=_ROUTER_TIMEOUT_S) as client:
        r = await client.post(f"{base}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise RuntimeError(f"router answered {r.status_code}")
    return parse_citations(r.json(), n)


def _is_ddg_host(hostname: str | None) -> bool:
    """duckduckgo.com or a subdomain of it -- never a name that only ends in
    the same letters (evilduckduckgo.com). Code scanning #85."""
    host = (hostname or "").lower().rstrip(".")
    return host == "duckduckgo.com" or host.endswith(".duckduckgo.com")


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<url>."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if _is_ddg_host(parsed.hostname) and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def _is_ddg_ad(url: str) -> bool:
    """DuckDuckGo's sponsored results point at duckduckgo.com/y.js. Judged on
    the parsed host and path, not a substring: "duckduckgo.com/y.js" inside
    some other site's URL is that site's page, not an ad."""
    parsed = urlparse(url)
    return _is_ddg_host(parsed.hostname) and parsed.path == "/y.js"


_DDG_RESULT = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?', re.S)


def parse_ddg(page: str, limit: int) -> list[Result]:
    out: list[Result] = []
    for m in _DDG_RESULT.finditer(page):
        url = _ddg_target(m.group("href"))
        if not url.startswith(("http://", "https://")) or _is_ddg_ad(url):
            continue
        strip: Callable[[str | None], str] = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()  # noqa: E731
        out.append(Result(strip(m.group("title")), url, strip(m.group("snippet"))))
        if len(out) >= limit:
            break
    return out


async def _via_duckduckgo(query: str, n: int) -> list[Result]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}) as client:
        r = await client.post(_DDG_URL, data={"q": query, "kl": "us-en"})
    if r.status_code != 200:
        # 202 is DuckDuckGo's "slow down" page, not an empty result.
        raise RuntimeError(f"duckduckgo answered {r.status_code}")
    results = parse_ddg(r.text, n)
    if not results and "result__a" not in r.text:
        raise RuntimeError("duckduckgo returned a page without results markup")
    return results


async def web_search(query: str, num_results: int = 6, *, metadata: dict[str, Any] | None = None) -> str:
    """Search the web; the text a model reads. Never raises."""
    query = (query or "").strip()
    if not query:
        return "ERROR: an empty query"
    n = max(1, min(int(num_results or 6), MAX_RESULTS))
    reasons: list[str] = []
    try:
        results = await _via_router(query, n, metadata)
        if results:
            return format_results(query, results, "web")
        reasons.append("the search index returned nothing")
    except Exception as e:  # noqa: BLE001 -- fall through to the next source
        logger.warning("web_search: router search failed for %r: %s", query[:80], e)
        reasons.append(f"search service: {type(e).__name__}")
    try:
        results = await _via_duckduckgo(query, n)
        return format_results(query, results, "duckduckgo")
    except Exception as e:  # noqa: BLE001
        logger.warning("web_search: duckduckgo fallback failed for %r: %s", query[:80], e)
        reasons.append(f"fallback: {e}")
    return UNAVAILABLE.format(why="; ".join(reasons))
