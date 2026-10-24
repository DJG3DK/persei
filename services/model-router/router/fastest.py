"""The fastest providers of each pinned model, kept current.

93-99% of an agent task is spent waiting on the model (measured on SWE-bench
tasks, 2026-09-24), and one model runs at very different speeds on different
hosts: 9 tok/s on one provider, 104 on another, for the same weights. Unasked,
OpenRouter balances by price. The operator controls cost by the MODEL they
pin; which host serves it is ours to choose, and we choose speed.

OpenRouter's own `sort: "throughput"` is not used for this: on 2026-09-24 it
sent a model to a provider its own endpoint stats listed at a quarter of the
speed of the top three. So the router ranks the endpoints itself, from those
stats (`/models/<model>/endpoints`, last 30 minutes), and names them in order
with fallbacks allowed -- the fast hosts' shared pools do throw 429s, and the
next one on the list is the answer to that, not the slowest.

Speed is not only tokens per second. On 2026-09-24 the three fastest DeepSeek
hosts ran to the output ceiling on 2-3% of calls -- seven-plus minutes of
nothing, each -- while the slower ones never did, and a 50-task benchmark sat
on two tasks for forty minutes. So each host is also charged its measured
runaway rate, from this router's own ledger, times what a runaway costs at the
output cap: the expected time of a call, not the typical one.

Never on the request path: a call uses whatever ranking is cached, and a stale
or missing one is refreshed in the background. Until the first ranking lands,
build_body's `sort: "throughput"` default stands in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from router import ledger
from router.upstream import DEFAULT_MAX_OUTPUT_TOKENS

logger = logging.getLogger("model-router")

ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
REFRESH_S = 600
TOP_N = 6
# A typical agent answer, for the time estimate: the delay before the first
# token plus this many tokens at the endpoint's median speed.
TYPICAL_OUTPUT_TOKENS = 1000
MIN_UPTIME_PCT = 98.0
# A call that produced this many tokens is counted as a runaway. Real agent
# answers stay under ~15k; runaways end at the cap.
RUNAWAY_TOKENS = 30_000
LEDGER_WINDOW_S = 7 * 86400
# Every host starts as if it had made PRIOR_CALLS calls at the rate measured
# across all hosts before the problem was seen (0.2%), so one unlucky call
# does not condemn a new host and a few lucky ones do not clear a bad one.
PRIOR_CALLS, PRIOR_RUNAWAYS = 200, 0.4


def _p50(stat) -> float | None:
    if isinstance(stat, dict):
        v = stat.get("p50")
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    return None


def runaway_rate(stats: dict | None, model: str, provider_name: str) -> float:
    calls, runaways = (stats or {}).get((model, provider_name.lower()), (0, 0))
    return (runaways + PRIOR_RUNAWAYS) / (calls + PRIOR_CALLS)


def ledger_stats(path: Path | None = None, now: float | None = None) -> dict[tuple[str, str], tuple[int, int]]:
    """(model, provider) -> (calls, runaways) over the last week of this
    router's own ledger. Read in a thread; never raises."""
    path = path or ledger.LOG_PATH
    since = (now or time.time()) - LEDGER_WINDOW_S
    out: dict[tuple[str, str], list[int]] = {}
    try:
        with open(path) as fh:
            for line in fh:
                if '"provider"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("error") or not r.get("provider") or (r.get("ts") or 0) < since:
                    continue
                key = (str(r.get("routed_model") or r.get("model") or ""), str(r["provider"]).lower())
                row = out.setdefault(key, [0, 0])
                row[0] += 1
                if (r.get("completion_tokens") or 0) >= RUNAWAY_TOKENS:
                    row[1] += 1
    except OSError:
        return {}
    return {k: (v[0], v[1]) for k, v in out.items()}


def rank(endpoints: list[dict], ignore: list[str] | None = None, model: str = "",
         stats: dict | None = None) -> list[str]:
    """Endpoint tags, fastest first, by the expected time of a call: the delay
    before the first token, a typical answer at the median speed, and the
    host's measured chance of a runaway times a runaway's length at the cap.

    Out: anything OpenRouter marks degraded (status < 0), under 98% uptime,
    without speed stats, or in the deployment's `ignore` list."""
    ignored = {s.lower() for s in (ignore or [])}
    scored = []
    for e in endpoints:
        tag = e.get("tag") or ""
        slug = tag.split("/")[0].lower()
        name = (e.get("provider_name") or "").lower()
        if not tag or slug in ignored or name in ignored or tag.lower() in ignored:
            continue
        if (e.get("status") or 0) < 0:
            continue
        uptime = e.get("uptime_last_30m")
        if isinstance(uptime, (int, float)) and uptime < MIN_UPTIME_PCT:
            continue
        tp, lat_ms = _p50(e.get("throughput_last_30m")), _p50(e.get("latency_last_30m"))
        if tp is None or lat_ms is None:
            continue
        runaway = runaway_rate(stats, model, e.get("provider_name") or slug)
        scored.append((lat_ms / 1000 + (TYPICAL_OUTPUT_TOKENS + runaway * DEFAULT_MAX_OUTPUT_TOKENS) / tp, tag))
    scored.sort()
    out: list[str] = []
    for _, tag in scored:
        if tag not in out:
            out.append(tag)
    return out[:TOP_N]


class FastestProviders:
    def __init__(self) -> None:
        # Keyed by model AND its ignore list: two deployments of one model
        # can exclude different hosts.
        self._order: dict[tuple, list[str]] = {}
        self._fetched: dict[tuple, float] = {}
        self._refreshing: set[tuple] = set()
        self._tasks: set[asyncio.Task] = set()     # held, or a pending refresh can be collected

    def extra_body_for(self, client: httpx.AsyncClient | None, api_key: str,
                       model: str, extra_body: dict) -> dict:
        """The deployment's extra_body with the fastest providers named first.

        A deployment that already says how to choose (`order`, `only` or
        `sort`) is left exactly as configured."""
        provider = extra_body.get("provider") if isinstance(extra_body.get("provider"), dict) else {}
        if any(k in provider for k in ("order", "only", "sort")):
            return extra_body
        ignore = tuple(sorted(provider.get("ignore") or []))
        key = (model, ignore)
        if client is not None and time.monotonic() - self._fetched.get(key, -1e9) > REFRESH_S \
                and key not in self._refreshing:
            self._refreshing.add(key)
            task = asyncio.get_running_loop().create_task(self._refresh(client, api_key, key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        order = self._order.get(key)
        if not order:
            return extra_body
        return {**extra_body, "provider": {**provider, "order": order, "allow_fallbacks": True}}

    async def _refresh(self, client: httpx.AsyncClient, api_key: str, key: tuple) -> None:
        model, ignore = key
        try:
            r = await client.get(ENDPOINTS_URL.format(model=model),
                                 headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
            r.raise_for_status()
            stats = await asyncio.to_thread(ledger_stats)
            order = rank((r.json().get("data") or {}).get("endpoints") or [], list(ignore), model, stats)
            if order:
                if order != self._order.get(key):
                    logger.info("fastest providers for %s: %s", model, ", ".join(order))
                self._order[key] = order
        except Exception as e:  # noqa: BLE001 -- a stats outage must never touch a call
            logger.warning("provider ranking for %s not refreshed: %s", model, e)
        finally:
            # Also after a failure: retry on the next refresh, not on every call.
            self._fetched[key] = time.monotonic()
            self._refreshing.discard(key)

    def snapshot(self) -> dict[str, list[str]]:
        return {m + (f" (ignore {', '.join(i)})" if i else ""): o for (m, i), o in self._order.items()}


# -- settings a model's providers do not take (2026-10-24) -------------------
#
# Kimi K3.1 Pro has one provider, and it takes neither temperature, top_p nor
# parallel_tool_calls. With `require_parameters` (set on every agent role, so
# a provider that would silently ignore `tools` is never picked) OpenRouter
# answered every agent call 404 "No endpoints found that can handle the
# requested parameters", and the role ran on its fallback all along.
#
# Only sampling knobs are dropped: they change how an answer is worded, not
# what the call means. tools, tool_choice and response_format never are -- a
# model that cannot take those should still fail and fall back, as before.
DROPPABLE = frozenset({
    "temperature", "top_p", "top_k", "min_p", "top_a", "frequency_penalty", "presence_penalty",
    "repetition_penalty", "seed", "logit_bias", "logprobs", "top_logprobs", "parallel_tool_calls",
})
FIRST_FETCH_TIMEOUT_S = 8.0


def supported_union(endpoints: list[dict], ignore: list[str] | None = None) -> set[str]:
    """What at least one usable provider takes: OpenRouter can route to that one."""
    ignored = {s.lower() for s in (ignore or [])}
    out: set[str] = set()
    for e in endpoints:
        tag = (e.get("tag") or "").lower()
        if tag.split("/")[0] in ignored or (e.get("provider_name") or "").lower() in ignored or tag in ignored:
            continue
        if (e.get("status") or 0) < 0:
            continue
        out.update(e.get("supported_parameters") or [])
    return out


def trim(body: dict, supported: set[str] | None) -> tuple[dict, list[str]]:
    """The body without the droppable settings no provider takes, and their
    names. `max_completion_tokens` becomes `max_tokens` where only that is
    taken. Unknown support (None) changes nothing."""
    if not supported:
        return body, []
    dropped = [k for k in body if k in DROPPABLE and k not in supported]
    out = {k: v for k, v in body.items() if k not in dropped}
    if "max_completion_tokens" in out and "max_completion_tokens" not in supported \
            and "max_tokens" in supported and "max_tokens" not in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
        dropped.append("max_completion_tokens (sent as max_tokens)")
    return out, dropped


class SupportedParams:
    """Per model (and ignore list): the settings its providers take. The first
    call for a model waits for one fetch, briefly; later ones refresh in the
    background. A failed fetch means "unknown", and nothing is dropped."""

    def __init__(self) -> None:
        self._known: dict[tuple, set[str]] = {}
        self._fetched: dict[tuple, float] = {}
        self._refreshing: set[tuple] = set()
        self._tasks: set[asyncio.Task] = set()
        self._logged: set[tuple] = set()

    async def body_for(self, client: httpx.AsyncClient | None, api_key: str, model: str,
                       extra_body: dict, body: dict) -> dict:
        if client is None or "messages" not in body:
            return body                               # embeddings, and tests without a client
        provider = extra_body.get("provider") if isinstance(extra_body.get("provider"), dict) else {}
        key = (model, tuple(sorted(provider.get("ignore") or [])))
        if key not in self._fetched:
            try:
                await asyncio.wait_for(self._refresh(client, api_key, key), FIRST_FETCH_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- a timeout or a failed fetch: unknown, send as is
                self._fetched.setdefault(key, time.monotonic())
        elif time.monotonic() - self._fetched[key] > REFRESH_S and key not in self._refreshing:
            self._refreshing.add(key)
            task = asyncio.get_running_loop().create_task(self._refresh(client, api_key, key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        out, dropped = trim(body, self._known.get(key))
        if dropped and (key, tuple(dropped)) not in self._logged:
            self._logged.add((key, tuple(dropped)))
            logger.info("%s: no provider takes %s; dropped from its calls", model, ", ".join(dropped))
        return out

    async def _refresh(self, client: httpx.AsyncClient, api_key: str, key: tuple) -> None:
        model, ignore = key
        try:
            r = await client.get(ENDPOINTS_URL.format(model=model),
                                 headers={"Authorization": f"Bearer {api_key}"}, timeout=FIRST_FETCH_TIMEOUT_S)
            r.raise_for_status()
            found = supported_union((r.json().get("data") or {}).get("endpoints") or [], list(ignore))
            if found:
                self._known[key] = found
        except Exception as e:  # noqa: BLE001 -- unknown means send as configured
            logger.warning("supported parameters for %s not refreshed: %s", model, e)
        finally:
            self._fetched[key] = time.monotonic()
            self._refreshing.discard(key)
