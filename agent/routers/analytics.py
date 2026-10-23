"""Analytics: the aggregates the dashboard's charts are drawn from.

The second seam out of agent/server.py (agent/routers/), and a deliberately
boring one: five read-only routes whose only reach outside themselves is the
store, the project list and agent/metrics.py. What it is really here to prove
is that a seam can carry its own module-level constants and its own long
comments across intact -- those comments are the record of what each number
means and why it is computed fresh rather than cached, and a split that drops
them costs more than the split saves.

`request.app.state.store` rather than an import of `app`: server.py includes
this router, so importing back from it is a cycle.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

import json
import logging

from agent import auth, benchmarks, metrics, paths, store_paging
from agent.auth import User, require_full_auth
from agent.config import PROJECTS
from agent.graph import read_with_retry

logger = logging.getLogger("tektonix")

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

@router.get("")
async def get_analytics(request: Request, user: User = Depends(require_full_auth)):
    """Chart-ready aggregates for the Analytics view, computed fresh from the
    Store on every call (freshness over running counters,
    which stays as-is for the lighter sidebar/balance uses). Sources:

    - ("tasks", repo) meta entries: per-task cost/status/created_at/budget --
      drives the daily-spend series, outcome donut, and per-task cost bars.
    - ("episodes", repo) records (verify_and_ship writes one per terminal
      outcome): iteration counts and review verdicts -- genuine depth the task
      meta alone doesn't carry.
    """
    auth.require_admin(user)
    import json as _json

    store = request.app.state.store

    tasks: list[dict] = []
    episodes: list[dict] = []
    planning_sessions: list[dict] = []
    # Every row, not `asearch(limit=200)`: totals covered the newest 200
    # tasks, episodes and sessions per project, and a busy project's spend
    # and outcomes quietly stopped growing (audit B38, 2026-10-22).
    for repo in PROJECTS:
        results = await read_with_retry(lambda repo=repo: store_paging.all_items(store, ("tasks", repo)))
        for item in results:
            tasks.append({**item.value, "repo": repo})
        try:
            for item in await read_with_retry(lambda repo=repo: store_paging.all_items(store, ("planning", repo))):
                planning_sessions.append(item.value or {})
        except Exception:  # noqa: BLE001 -- the page still answers without the planning bucket
            logger.warning("analytics: could not read planning sessions for %s", repo)
        ep_results = await read_with_retry(lambda repo=repo: store_paging.all_items(store, ("episodes", repo)))
        for item in ep_results:
            value = item.value
            # Episodes are stored as backend file entries ({"content": json-str})
            content = value.get("content") if isinstance(value, dict) else None
            if isinstance(content, str):
                try:
                    record = _json.loads(content)
                    record["repo"] = repo
                    episodes.append(record)
                except (ValueError, TypeError):
                    pass

    tasks.sort(key=lambda t: t.get("created_at", 0))

    # Daily spend series, zero-filled over the last 14 days so a young
    # deployment renders as a real chart, not one floating point on an
    # empty grid. Spend is attributed to the day it actually happened using
    # episode records: each episode carries the task's cumulative cost at
    # that terminal moment, so per-day spend for a task is the delta
    # between consecutive episodes -- accurate for tasks that span days,
    # unlike bucketing everything on the creation date. Tasks with no
    # episodes yet (still running, or pre-episode history) fall back to
    # their full current cost on their creation day.

    daily: dict[str, dict] = {}
    today = datetime.now(tz=UTC).date()
    for offset in range(13, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        daily[day] = {"date": day, "cost": 0.0, "tasks": 0}

    def _bucket(day: str):
        # Days older than the window still aggregate into its oldest day so
        # totals stay honest rather than silently dropping history.
        return daily[day] if day in daily else daily[min(daily)]

    episodes_by_task: dict[str, list[dict]] = {}
    for e in sorted(episodes, key=lambda e: e.get("timestamp") or ""):
        if e.get("task_id") and e.get("timestamp") and e.get("cost_usd") is not None:
            episodes_by_task.setdefault(e["task_id"], []).append(e)

    for task_episodes in episodes_by_task.values():
        # Running max, not last-seen: a task's episode costs are cumulative
        # but not perfectly monotonic (a node retry can resume from a
        # checkpoint whose cost predates a crashed attempt's spend), and
        # naive clamped deltas over-count on every dip. Deltas against the
        # running max telescope to exactly the task's peak cumulative cost
        # regardless of dips.
        running_max = 0.0
        for e in task_episodes:
            day = str(e["timestamp"])[:10]
            cost = float(e["cost_usd"])
            delta = max(0.0, cost - running_max)
            running_max = max(running_max, cost)
            _bucket(day)["cost"] += delta

    for t in tasks:
        ts = t.get("created_at")
        if not ts:
            continue
        day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
        _bucket(day)["tasks"] += 1
        if t.get("task_id") not in episodes_by_task:
            _bucket(day)["cost"] += float(t.get("cost_so_far") or 0.0)
        # The planning that produced the task, on the day the task started.
        _bucket(day)["cost"] += float(t.get("planning_cost_usd") or 0.0)

    daily_series = [daily[k] for k in sorted(daily)]

    per_task = [
        {
            "task_id": t.get("task_id"),
            "repo": t.get("repo"),
            "goal": (t.get("goal") or "")[:80],
            "category": t.get("category") or "other",
            # The whole cost: the build, plus the planning that produced it.
            "cost": float(t.get("cost_so_far") or 0.0) + float(t.get("planning_cost_usd") or 0.0),
            "planning_cost": float(t.get("planning_cost_usd") or 0.0),
            "budget": float(t.get("budget_usd") or 0.0),
            "status": t.get("status"),
            "created_at": t.get("created_at"),
        }
        for t in tasks
    ]

    # Cost/count grouped by the classifier's fixed taxonomy (agent/classify.py)
    # instead of one bar per individual task -- a per-task chart stops being
    # readable once there are more than a handful of tasks, and doesn't answer
    # the question that actually matters: which KIND of work is costing the
    # most. Tasks created before classification existed have no "category"
    # key at all (not even "other"), so they're grouped there via the same
    # fallback the frontend already treats as the default bucket.
    by_category: dict[str, dict] = {}
    for t in tasks:
        cat = t.get("category") or "other"
        bucket = by_category.setdefault(cat, {"category": cat, "tasks": 0, "cost": 0.0})
        bucket["tasks"] += 1
        bucket["cost"] += float(t.get("cost_so_far") or 0.0) + float(t.get("planning_cost_usd") or 0.0)
    # Planning that never became a task is spend too. Each session carries
    # its cost onto the first build started from it (agent/tasks.py); what is
    # left uncarried -- abandoned plans, plans still open -- is its own
    # category, counted in sessions rather than tasks.
    # The same spend has to land in the daily series (so total_cost sees
    # it) and on the session's project: it sat in by_category alone and the
    # three totals on the page disagreed (2026-09-29).
    stranded: list[tuple[dict, float]] = []
    for m in planning_sessions:
        left = max(0.0, float(m.get("cost_usd") or 0.0) - float(m.get("carried_cost_usd") or 0.0))
        if left > 0:
            stranded.append((m, left))
    if stranded:
        by_category["planning"] = {"category": "planning", "tasks": len(stranded),
                                   "cost": sum(c for _, c in stranded)}
    for m, left in stranded:
        when = m.get("updated_at") or m.get("created_at")
        if when:
            _bucket(datetime.fromtimestamp(float(when), tz=UTC).strftime("%Y-%m-%d"))["cost"] += left
    daily_series = [daily[k] for k in sorted(daily)]
    by_category_list = sorted(by_category.values(), key=lambda b: b["cost"], reverse=True)

    outcomes: dict[str, int] = {}
    for t in tasks:
        s = t.get("status") or "unknown"
        outcomes[s] = outcomes.get(s, 0) + 1

    per_repo = {}
    for repo in PROJECTS:
        repo_tasks = [t for t in tasks if t.get("repo") == repo]
        per_repo[repo] = {
            "tasks": len(repo_tasks),
            # A task's cost is its build plus the planning it carried, as
            # by_category and per_task count it; plus this project's plans
            # that never became a task.
            "cost": sum(float(t.get("cost_so_far") or 0.0) + float(t.get("planning_cost_usd") or 0.0)
                        for t in repo_tasks)
            + sum(c for m, c in stranded if m.get("repo") == repo),
        }

    iteration_stats = [
        {
            "task_id": e.get("task_id"),
            "repo": e.get("repo"),
            "iterations": e.get("iteration_count"),
            "outcome": e.get("outcome"),
            "cost": e.get("cost_usd"),
            "review_verdict": e.get("review_verdict"),
            "timestamp": e.get("timestamp"),
        }
        for e in episodes
        if e.get("iteration_count") is not None
    ]

    # audit M-34: _reviewer_usage reads an ever-growing JSONL whole -- off-loop.
    reviewer_usage = await asyncio.to_thread(_reviewer_usage)

    return {
        "daily": daily_series,
        "per_task": per_task,
        "by_category": by_category_list,
        "outcomes": outcomes,
        "per_repo": per_repo,
        "episodes": iteration_stats,
        # Episode-derived (the daily series' own sum), not the surviving task
        # metas' sum -- deleting a task removes its meta but its spend still
        # happened, and the two totals disagreeing on the dashboard would be
        # a visible inconsistency.
        "total_cost": sum(b["cost"] for b in daily_series),
        "total_tasks": len(tasks),
        # The reviewer's spend, which this view could not previously see at all.
        # Kept as its own block rather than folded into total_cost: the agent's
        # spend and the gate's spend are different budgets and mixing them would
        # silently change what every existing number on this page means.
        "reviewer": reviewer_usage,
    }


# ── Reviewer spend ───────────────────────────────────────────────────────────
REVIEWER_USAGE_LOG = paths.REVIEWER_USAGE_LOG


def _reviewer_usage() -> dict:
    """Aggregate the commit reviewer's own model spend.

    The reviewer is a separate service that, until 2026-08-25, called OpenRouter
    directly with a hardcoded model and never read the response's `usage` block.
    So its spend existed but appeared nowhere: Analytics is built from the
    agent's own task/episode records and structurally could not see it. That
    made "cost per task" wrong in a specific way — it excluded the review rounds
    a task actually needed, which is precisely the cost a NEEDS_FIXES loop adds.

    Returns zeros rather than raising when the log is absent (a fresh install,
    or the reviewer simply not having run yet).
    """
    out = {
        "reviews": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0,
        "per_repo": [], "daily": [], "model": None, "cost_known": True,
    }
    try:
        lines = REVIEWER_USAGE_LOG.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        # Genuinely no spend yet (fresh install / reviewer never ran) -- a true
        # zero, so cost_known stays True.
        return out
    except Exception as e:  # noqa: BLE001
        # audit M-34: a file that exists but could not be READ is NOT a known
        # zero -- returning cost_known:True here was the exact silent-zero this
        # function's docstring says it exists to eliminate. Flag it unknown.
        logger.warning("reviewer usage log unreadable: %s", e)
        out["cost_known"] = False
        return out

    by_repo: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    missing_cost = 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:  # noqa: BLE001 -- a torn line in an append-only log is skipped
            continue
        out["reviews"] += 1
        cost = r.get("cost")
        if cost is None:
            missing_cost += 1
        else:
            out["cost"] += float(cost)
        ti, to = int(r.get("prompt_tokens") or 0), int(r.get("completion_tokens") or 0)
        out["tokens_in"] += ti
        out["tokens_out"] += to
        out["model"] = r.get("model") or out["model"]

        repo = r.get("project") or "unknown"
        b = by_repo.setdefault(repo, {"repo": repo, "reviews": 0, "cost": 0.0})
        b["reviews"] += 1
        b["cost"] += float(cost or 0.0)

        day = str(r.get("at") or "")[:10]
        if day:
            d = by_day.setdefault(day, {"date": day, "cost": 0.0, "reviews": 0})
            d["cost"] += float(cost or 0.0)
            d["reviews"] += 1

    # Say so rather than quietly under-reporting: a run whose usage lacked a cost
    # field contributes tokens but not dollars.
    out["cost_known"] = missing_cost == 0
    out["reviews_missing_cost"] = missing_cost
    out["per_repo"] = sorted(by_repo.values(), key=lambda x: -x["cost"])
    out["daily"] = sorted(by_day.values(), key=lambda x: x["date"])
    return out


# How far back each Analytics panel looks. No caches, TTLs, locks or
# pre-warm any more: these are served from this box's own logs
# (agent/metrics.py), which is a local file scan rather than the paged
# LangSmith query that needed all of that machinery. The practical ceiling is
# the logs' own size trim, not these numbers.

_MODEL_USAGE_WINDOW_DAYS = 14
_TOOL_RELIABILITY_WINDOW_DAYS = 14
_TRACE_SUMMARY_WINDOW_DAYS = 14

# Consolidation status parses its marker's timestamp; this alias outlived the
# LangSmith scans it was introduced beside.


def _classify_model_usage_role(metadata: dict, alias: str | None) -> str:
    """Which agent role made this call, for the Analytics model-usage
    breakdown.

    The background consolidation agent (agent/consolidation.py) runs on its
    own thread_id scheme ("consolidation:{repo}:{timestamp}") and carries
    none of the tags below, so without checking for it explicitly its calls
    silently fell into the coordinator bucket -- and since its model
    ("reasoning-tier") never matches the agent-planner alias, they landed
    specifically in "coder", making it look like the coder role used
    multiple different models when those calls were unrelated background
    traffic. Checked first, before any of the live-task tags below.

    agent/classify.py's one-shot classification call is the same class of
    bug in a different shape: it's a bare ChatOpenAI.ainvoke() outside any
    graph, so it carries no thread_id/lc_agent_name/lc_source at all --
    caught here by alias instead, since there's no thread_id prefix to key
    off like consolidation has.
    """
    thread_id = metadata.get("thread_id") or ""
    if thread_id.startswith("consolidation:"):
        return "consolidation"
    if alias == "agent-classifier":
        return "classifier"
    if alias == "agent-vision":
        # describe_image is callable from the coordinator and from several
        # subagents (investigator/test-writer/general-purpose), so without
        # this check a vision call would inherit whichever lc_agent_name (or
        # lack of one) belongs to its caller and get folded into that role's
        # bucket instead of standing on its own. Checked by alias, same
        # reasoning as agent-classifier above.
        return "vision"
    if alias == "agent-planning-chat":
        # Checked by alias, not thread_id ("planning:{session_id}") -- the
        # planning agent also runs its own SummarizationMiddleware pinned to
        # agent-summarizer, and that call's alias won't match this branch,
        # so it correctly falls through to the lc_source check below instead
        # of getting folded into "planning-chat" the way a thread_id-first
        # check would misattribute it.
        return "planning-chat"
    # deepagents tags every subagent's runs with lc_agent_name
    # (investigator/test-writer/general-purpose); SummarizationMiddleware's
    # own calls carry lc_source=summarization; everything else is the
    # coordinator itself.
    # ANY pinned alias names its role outright -- the general form of the
    # classifier/vision special-cases above. Before this, a traced call whose
    # alias was agent-cartographer (or any future role) fell through to the
    # coordinator branch and landed in the CODER bucket: the analytics panel
    # showed mistral/haiku/gemini rows under Coder that were really
    # cartographer runs, consolidator probes and benchmarks -- "models that
    # are not in the stack", as the operator put it.
    if isinstance(alias, str) and alias.startswith("agent-"):
        role = alias.removeprefix("agent-")
        if role == "planning-chat-hard":
            role = "planning-chat"   # one bucket for both planning tiers
        return role

    role = metadata.get("lc_agent_name") or (
        "summarizer" if metadata.get("lc_source") == "summarization" else None
    )
    if role:
        return role
    # No agent tag and not a pinned alias: this is BACKGROUND traffic (direct
    # library calls, benchmarks, one-off scripts). It used to be silently
    # filed under coder via the coordinator fallback, doubling that bucket
    # with models no role ever pinned. Quarantine it instead.
    return "background"



@router.get("/models")
async def get_model_usage(request: Request, user: User = Depends(require_full_auth)):
    """Serve-stale-while-revalidate -- the LangSmith scan can exceed nginx's
    proxy timeout when run inline, and the in-memory cache dies with every
    pm2 restart. A page load right after a restart would otherwise pay the
    full cold scan, time out, and the whole model-usage section would
    silently vanish from the dashboard. Now: any cached data (even expired)
    returns instantly with a background refresh kicked off; only the very
    first request after a cold start ever blocks, and startup pre-warming
    (see lifespan) makes even that rare.
    """
    auth.require_admin(user)
    # Read from this deployment's own router ledger, not from LangSmith. The
    # ledger knows two things the traces never did -- what the router was
    # actually BILLED, and how much of each prompt the provider served from
    # cache -- and it exists whether or not tracing is on, which is the point:
    # an optional off-box service was load-bearing for "what is this agent
    # doing" (see agent/metrics.py).
    usage = await asyncio.to_thread(metrics.model_usage, _MODEL_USAGE_WINDOW_DAYS)
    return {**usage, "cached": False, "tracing_disabled": not request.app.state.config.langsmith_tracing}


@router.get("/tool-reliability")
async def get_tool_reliability(request: Request, user: User = Depends(require_full_auth)):
    """Same serve-stale-while-revalidate contract as /api/analytics/models
    -- see that endpoint's own docstring."""
    auth.require_admin(user)
    # From the work node's own tool-result log (agent/tool_events.py), not
    # from LangSmith's run_type="tool" runs.
    data = await asyncio.to_thread(metrics.tool_reliability, _TOOL_RELIABILITY_WINDOW_DAYS)
    return {**data, "cached": False, "tracing_disabled": not request.app.state.config.langsmith_tracing}


@router.get("/trace-summary")
async def get_trace_summary(request: Request, user: User = Depends(require_full_auth)):
    """Same serve-stale-while-revalidate contract as /api/analytics/models
    -- see that endpoint's own docstring."""
    auth.require_admin(user)
    # Per TASK now, not per traced root run -- see metrics.run_summary for why
    # that is the number the panel was always trying to convey.
    data = await asyncio.to_thread(metrics.run_summary, _TRACE_SUMMARY_WINDOW_DAYS)
    return {**data, "cached": False, "tracing_disabled": not request.app.state.config.langsmith_tracing}


@router.get("/benchmarks")
async def get_benchmarks(request: Request, window_days: int = 14,
                         user: User = Depends(require_full_auth)):
    """Whether a change to the agent made it better -- see agent/benchmarks.py.

    Separate from `GET ""` rather than folded into it because the two have
    different jobs: that one is the dashboard's chart data and is read on
    every page load, this one is read when somebody wants to know if last
    week's change helped, and it walks two windows of episodes to answer.

    `window_days` is a query parameter because the right window depends on
    throughput -- 14 days is the default for a handful of tasks a week, and a
    busier deployment wants it shorter so a comparison is not half stale.
    """
    auth.require_admin(user)
    # Imported here, not at module scope: agent/project_memory.py (where this
    # lives; deep_agent only re-exports it) pulls in deepagents' backends and
    # the store, and a route module that cannot be imported without them is a
    # route module that cannot be tested without them.
    from agent.project_memory import episodes_namespace

    # Clamped, not validated-and-rejected: the only callers are the dashboard
    # and somebody poking at the URL, and 400ing the second one buys nothing.
    # `int(window_days)` with no `or 14` fallback -- 0 is a value to clamp to
    # 1, and `0 or 14` would have quietly turned it into a fortnight.
    window = max(1, min(int(window_days), 90))
    return await benchmarks.benchmark_summary(
        request.app.state.store,
        list(PROJECTS),
        lambda repo: episodes_namespace(repo)(None),
        window_days=window,
    )
