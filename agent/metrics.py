"""The dashboard's metrics, computed from this deployment's own records.

Until 2026-09-12 three Analytics panels -- per-role model usage, tool
reliability and trace health -- were read out of LangSmith. That made an optional,
third-party, off-box service load-bearing for the question "what is this agent
doing", and it cost more than it looked: profiled on 2026-09-12, the redacting
tracer that has to run for those traces to be safe was burning ~100% of a core
and doing it on the event loop.

Everything those panels need, this box already writes down:

* `services/model-router/logs/routing.jsonl` -- one line per model call, with
  the alias asked for, the model that served it, prompt and completion tokens,
  the router's BILLED cost, the duration, and (since 2026-09-12) the cached
  prompt tokens and the task the call belonged to. This is strictly better
  data than the traces were: LangSmith never knew what OpenRouter charged.
* `logs/tool_events.jsonl` -- one line per tool result, written by the work
  node as it streams (see agent/tool_events.py).
* the Store -- tasks and their outcomes.

Retention is the honest limitation. Both logs are trimmed by size, so these
numbers cover the recent past rather than all time; the window each panel
actually had is reported alongside it rather than implied.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from agent import tool_events

logger = logging.getLogger("tektonix")

ROUTING_LOG = Path(
    os.environ.get("MODEL_ROUTER_LEDGER")
    or (Path(__file__).resolve().parents[1] / "services" / "model-router" / "logs" / "routing.jsonl")
)
# The writer's own path, not a second resolution of the same env var: two
# copies of "AGENT_TOOL_EVENTS_LOG or logs/tool_events.jsonl" can drift, and a
# reader pointed somewhere the writer is not reports zero tool calls.
TOOL_EVENTS_LOG = tool_events.LOG_PATH
# Recorded before 0.9.4 the harness was "[Tektonix harness]"; old rows still parse.
_HARNESS_TOOL = re.compile(r"^\[(?:Persei|Tektonix) harness\][^`]*`([A-Za-z0-9_\-]+)`")


def _rows(path: Path, since: float) -> list[dict]:
    """Every parseable line newer than `since`. A torn line is skipped, never
    fatal: these files are appended to by a live process."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        if isinstance(ts, (int, float)) and ts >= since:
            out.append(row)
    return out


def _role_and_model(row: dict) -> tuple[str | None, str]:
    """(role alias, underlying model) for one routing line.

    `alias` is the field to trust: the alias the client actually asked for,
    recorded from the router's alias. The two older fields are crossed and
    unreliable -- the old writer recorded `requested_model` from its own kwargs
    (the RESOLVED deployment) and `routed_model` from the response (whatever
    the provider returned), so on most lines neither is an alias at all and
    the role is unrecoverable. Lines written before the router started
    recording `alias` are therefore attributable only when one of the old
    fields happens to carry it.
    """
    candidates = [row.get("alias"), row.get("routed_model"), row.get("requested_model")]
    alias = next((str(c) for c in candidates if isinstance(c, str) and c.startswith("agent-")), None)
    # "unknown", never the alias itself: a failed call has no underlying model
    # (the response never arrived), and printing "agent-classifier" in the
    # model column would read as a model by that name rather than as a gap.
    model = next((str(c) for c in (row.get("requested_model"), row.get("routed_model"))
                  if isinstance(c, str) and c and not c.startswith("agent-")), "unknown")
    if alias is None:
        # Either a call from something else on this shared router (the review
        # service, the tier system), or one logged before aliases were
        # recorded. Neither belongs in this agent's per-role breakdown.
        return None, model
    return alias, model


def model_usage(window_days: int = 7, now: float | None = None) -> dict:
    """Per (role, model): calls, tokens, latency, billed cost, cache hits.

    Same shape the LangSmith scan produced, plus the two columns it could
    never have: what the router actually charged, and how much of each prompt
    the provider served from cache.
    """
    now = now if now is not None else time.time()
    since = now - window_days * 86400
    rows = _rows(ROUTING_LOG, since)
    buckets: dict[tuple[str, str], dict] = {}
    for row in rows:
        role, model = _role_and_model(row)
        if role is None:
            continue
        b = buckets.setdefault((role, model), {
            "role": role, "model": model, "calls": 0, "tokens_in": 0, "tokens_out": 0,
            "cached_tokens": 0, "cost_usd": 0.0, "_duration": 0.0, "_timed": 0, "errors": 0,
        })
        b["calls"] += 1
        b["tokens_in"] += int(row.get("prompt_tokens") or 0)
        b["tokens_out"] += int(row.get("completion_tokens") or 0)
        cached = row.get("cached_tokens")
        if isinstance(cached, int):
            b["cached_tokens"] += cached
        cost = row.get("cost")
        if isinstance(cost, (int, float)):
            b["cost_usd"] += float(cost)
        if row.get("error"):
            b["errors"] += 1
        duration = row.get("duration_s")
        if isinstance(duration, (int, float)):
            b["_duration"] += float(duration)
            b["_timed"] += 1

    models = []
    for b in buckets.values():
        models.append({
            # The SHORT role name ("coder", not "agent-coder"). The dashboard
            # keys its curated labels and its core-role ordering on this, and
            # returning the raw alias made every role render twice: once as an
            # empty "Coder" row from the core list, and again as a raw
            # "agent-coder" row carrying the actual numbers.
            "role": b["role"].removeprefix("agent-"),
            "model": b["model"],
            "calls": b["calls"],
            "tokens_in": b["tokens_in"],
            "tokens_out": b["tokens_out"],
            "avg_latency_s": (b["_duration"] / b["_timed"]) if b["_timed"] else None,
            "cost_usd": round(b["cost_usd"], 4),
            "cached_tokens": b["cached_tokens"],
            # What share of the prompt tokens the provider served from cache.
            # None when no call reported the field at all, which is different
            # from a real zero: it means the provider says nothing.
            "cache_hit_rate": (b["cached_tokens"] / b["tokens_in"]) if b["tokens_in"] else None,
            "errors": b["errors"],
        })
    models.sort(key=lambda m: m["calls"], reverse=True)
    return {"models": models, "window_days": window_days, "source": "router-ledger"}


def tool_reliability(window_days: int = 7, now: float | None = None) -> dict:
    """Per tool: calls, errors, error rate; plus errors per day."""
    now = now if now is not None else time.time()
    since = now - window_days * 86400
    by_tool: dict[str, dict] = {}
    daily: dict[str, int] = defaultdict(int)
    nudges: dict[str, int] = defaultdict(int)
    for row in _rows(TOOL_EVENTS_LOG, since):
        name = str(row.get("tool") or "unknown")
        if name == "unknown":
            # Rows the repeat guard wrote before its results carried a name:
            # the tool is the first `quoted` word of its message.
            named = _HARNESS_TOOL.search(str(row.get("detail") or ""))
            if named:
                name = named.group(1)
        # A shell command the harness pointed at a cheaper tool. Counted on
        # the call it belongs to rather than as a tool of its own -- see
        # agent/tool_events.py -- so a flagged bash call is one bash call
        # here, and how often the habit shows up is still visible.
        nudge = row.get("nudge")
        if name.startswith("bash-as-"):
            # The old encoding: a second event whose whole content was the
            # marker. Lines written that way are still inside the window, and
            # they say the same thing -- fold them in rather than leaving a
            # tool nobody has on the panel for another fortnight. These carry
            # no call of their own (the real bash row is already counted), so
            # only the nudge crosses over.
            nudges[name.removeprefix("bash-as-")] += 1
            b = by_tool.setdefault("bash", {"tool": "bash", "calls": 0, "errors": 0, "nudged": 0})
            b["nudged"] += 1
            continue
        b = by_tool.setdefault(name, {"tool": name, "calls": 0, "errors": 0, "nudged": 0})
        b["calls"] += 1
        if isinstance(nudge, str) and nudge:
            b["nudged"] += 1
            nudges[nudge] += 1
        if row.get("ok") is False:
            b["errors"] += 1
            day = time.strftime("%Y-%m-%d", time.gmtime(row["ts"]))
            daily[day] += 1
    tools = []
    for b in by_tool.values():
        tools.append({**b, "error_rate": (b["errors"] / b["calls"]) if b["calls"] else 0.0})
    tools.sort(key=lambda t: t["calls"], reverse=True)
    # Every day of the window, zero where nothing failed: a series of only
    # the days with errors drew one point for a fortnight (2026-09-26).
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - k * 86400)) for k in range(window_days - 1, -1, -1)]
    return {
        "tools": tools,
        "daily": [{"date": d, "errors": daily.get(d, 0)} for d in days],
        "nudges": [{"kind": k, "count": n} for k, n in sorted(nudges.items(), key=lambda kv: -kv[1])],
        "window_days": window_days,
        "source": "tool-events",
    }


def run_summary(window_days: int = 7, now: float | None = None) -> dict:
    """Top-line health, per TASK rather than per traced root run.

    The LangSmith version counted root runs -- a work pass, a planning turn, a
    subagent invocation -- which is an artefact of how tracing nests rather
    than a thing an operator asks about. This counts what they do ask about:
    how many tasks spent money in the window, how long they took wall-clock,
    how many model calls failed, and what it all cost.
    """
    now = now if now is not None else time.time()
    since = now - window_days * 86400
    rows = _rows(ROUTING_LOG, since)
    spans: dict[str, list[float]] = defaultdict(list)
    calls = errors = 0
    tokens_in = tokens_out = 0
    cost = 0.0
    for row in rows:
        role, _ = _role_and_model(row)
        if role is None:
            continue
        calls += 1
        if row.get("error"):
            errors += 1
        tokens_in += int(row.get("prompt_tokens") or 0)
        tokens_out += int(row.get("completion_tokens") or 0)
        c = row.get("cost")
        if isinstance(c, (int, float)):
            cost += float(c)
        task_id = row.get("task_id")
        if task_id:
            spans[task_id].append(float(row["ts"]))
    durations = [max(ts) - min(ts) for ts in spans.values() if len(ts) > 1]
    return {
        # Kept under the old names so the dashboard does not have to change in
        # the same breath: "trace_count" is now tasks, which is the number the
        # panel was always trying to convey.
        "trace_count": len(spans),
        "avg_latency_s": (sum(durations) / len(durations)) if durations else None,
        "error_rate": (errors / calls) if calls else 0.0,
        "total_input_tokens": tokens_in,
        "total_output_tokens": tokens_out,
        "model_calls": calls,
        "cost_usd": round(cost, 4),
        "window_days": window_days,
        "source": "router-ledger",
    }


# What this router has seen from each host of one model, for the provider
# picker: OpenRouter's own numbers are the last 30 minutes of everyone's
# traffic, this is the last week of ours. Cached because the ledger is tens
# of megabytes and the picker opens per role.
_PROVIDER_RECORD_CACHE: dict[tuple[str, int], tuple[float, dict[str, dict[str, int]]]] = {}
_PROVIDER_RECORD_TTL_S = 300
RUNAWAY_TOKENS = 30_000      # as services/model-router/router/fastest.py counts one


def provider_record(model: str, window_days: int = 7, now: float | None = None) -> dict[str, dict[str, int]]:
    """provider name (lower case) -> {"calls", "errors", "runaways"} for
    `model` over the window. An error counts only when the ledger knows which
    host failed; most failures happen before one answers."""
    now = now or time.time()
    key = (model, window_days)
    hit = _PROVIDER_RECORD_CACHE.get(key)
    if hit and now - hit[0] < _PROVIDER_RECORD_TTL_S:
        return hit[1]
    out: dict[str, dict[str, int]] = {}
    for row in _rows(ROUTING_LOG, now - window_days * 86400):
        if str(row.get("routed_model") or row.get("model") or "") != model or not row.get("provider"):
            continue
        rec = out.setdefault(str(row["provider"]).lower(), {"calls": 0, "errors": 0, "runaways": 0})
        rec["calls"] += 1
        if row.get("error"):
            rec["errors"] += 1
        elif (row.get("completion_tokens") or 0) >= RUNAWAY_TOKENS:
            rec["runaways"] += 1
    _PROVIDER_RECORD_CACHE[key] = (now, out)
    return out
