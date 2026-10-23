"""A task's live run state: the event fan-out, the live-log buffers, the run
slot, and the snapshot the dashboard hydrates from.

Out of agent/server.py on 2026-09-23 (follow-up review, F8), so the task and
planning routes can move to agent/routers/ without importing the server --
which includes those routers, so importing it back would be a cycle. Nothing
here imports agent.server.

server.py keeps its old names (`_publish`, `_live_task_log`, ...) as aliases
of the SAME objects, so every call site there -- `_stream_graph` above all --
is unchanged. The dicts are MUTATED, never rebound, which is what lets one
object be shared; a test that wants a clean buffer replaces it HERE.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import HTTPException

from agent import live_state, log_stream, planning_log

logger = logging.getLogger("tektonix")


SUBSCRIBER_QUEUE_MAX = 2000


def publish(task_id: str, event: dict) -> None:
    # Every entry carries a content-derived id and every event a monotonic
    # seq (agent/log_stream.py). Together they let the browser open its
    # socket before hydrating, buffer what arrives meanwhile, and merge the
    # two sources afterwards without losing or duplicating a line.
    if event.get("execution_log"):
        event["execution_log"] = log_stream.stamp(event["execution_log"])
        # The event counter is per task and lives as long as the task's live
        # log does: evicting one without the other leaks a counter per task
        # for the life of the process.
        live_log_append(live_task_log, task_id, event["execution_log"],
                         on_evict=task_event_seq.forget)
        # ...and the durable copy, batched (see planning_log.Recorder).
        rec = live_state.task_recorders.get(task_id)
        if rec is not None:
            due = False
            for entry in event["execution_log"]:
                due = rec.add(entry) or due
            if due:
                flush_task_log_bg(rec)
    if event.get("type") != "ping":
        event["seq"] = task_event_seq.next(task_id)
    for q, _ws in live_state.subscribers.get(task_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # audit M-34: a reader this far behind is effectively gone; drop
            # rather than grow memory without bound. Its socket teardown will
            # remove it shortly.
            logger.warning("dropping event for a stalled task %s subscriber", task_id)


# The outer graph's own AgentState has no `plan` key at all -- write_todos (deepagents' own planning tool, living in the inner
# deep-agent thread) is the plan, and `latest_todos` (a plain snapshot copied
# into the outer state at the end of each "work" pass, see work.py) is the
# closest equivalent. Translated here into the PlanStep[] shape the
# frontend's PlanTracker renders. `result`/`verified` have no todo-level
# equivalent in this design (verify_and_ship gates the whole task, not a
# per-step independently-checked claim) -- always False/None; the frontend
# doesn't currently render either field regardless.
TODO_STATUS_MAP = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}


def todos_to_plan(todos: list | None) -> list[dict] | None:
    if todos is None:
        return None
    return [
        {
            "id": str(i),
            "description": t.get("content", ""),
            "status": TODO_STATUS_MAP.get(t.get("status"), "pending"),
            "result": None,
            "verified": False,
        }
        for i, t in enumerate(todos)
    ]


def state_snapshot_for_frontend(values: dict) -> dict:
    """Used by get_task's REST snapshot (the hydrate path useTaskStream.ts
    calls on every connect/reconnect) -- without this translation, a page
    load/reconnect would show an empty plan until the next live "todos"
    custom event happened to arrive, since the raw checkpoint dict has
    `latest_todos`, not `plan`, and the frontend only reads the latter.
    """
    # No current_step_index: it belonged to the legacy plan->execute graph,
    # was always None here, and nothing read it (removed 2026-09-23).
    return {**values, "plan": todos_to_plan(values.get("latest_todos"))}


def apply_plan_fallback(snapshot: dict | None, meta_value: dict) -> dict | None:
    """The plan strip reads the LIVE mirror when there is one. The checkpoint's
    latest_todos is written only when a work pass RETURNS; the todos handler
    mirrors every todos event into the task meta as it happens. So mid-pass
    the mirror is never older than the checkpoint, and after a pass ends the
    two agree -- there is no moment at which the checkpoint is fresher.

    Two live reports drove this. 2026-08-28: mid-pass the checkpoint had no
    list yet and the strip vanished on every refresh, so the mirror was added
    as a fallback. 2026-09-09: a resumed task's coordinator wrote a new
    3-item list, the strip showed it, and a refresh snapped back to the
    15-item list from the pass before -- because "a checkpointed plan always
    wins" preferred the stale one. Mirror first, checkpoint when there is no
    mirror (tasks from before the mirror existed)."""
    if snapshot is None:
        return None
    mirrored = meta_value.get("latest_todos")
    if mirrored:
        return {**snapshot, "plan": todos_to_plan(mirrored)}
    return snapshot


def final_status(values: dict) -> str:
    """Terminal task status once a _stream_graph run's own astream loop
    ends -- "awaiting_approval" is a real third resting state alongside
    escalated/done (see deep_agent.py's INTERRUPT_ON, outer_graph.py's
    _route_after_verify), checked before the escalated/done fallback since
    a task can be both not-escalated and not-done: paused on a human-in-
    the-loop decision.
    """
    if values.get("escalated"):
        return "escalated"
    if values.get("pending_approval"):
        return "awaiting_approval"
    if values.get("pending_merge_approval"):
        # Review READY, merge parked on the operator's final look at the diff.
        return "awaiting_merge"
    return "done"


@contextlib.contextmanager
def claim_run_slot(registry: dict, key: str, already_running: str):
    """Reserve a slot in a run registry BEFORE the handler's awaits.

    The registries below are plain dicts guarded by `if key in registry:
    raise 409`. That check sat at the top of each handler and the real
    assignment came several awaits later -- and the event loop switches at
    every await, so two concurrent requests could both pass the check and
    both create a driver for the same thread. The unconditional
    `registry.pop(key)` in the driver's finally then orphaned whichever one
    survived.

    A dict write with no await between it and the check IS atomic here, so
    the fix is to reserve immediately: place a None placeholder, then let the
    handler replace it with the real Task. Both consumers of these registries
    do `registry.get(key)` and treat a falsy value as "not running", which is
    exactly right for the reservation window -- there is genuinely nothing to
    cancel yet.

    The slot is released if the handler raises, or if it returns without ever
    assigning a task; otherwise a rejected request would strand the key and
    the task could never be started again.
    """
    if registry.get(key) is not None or key in registry:
        raise HTTPException(409, already_running)
    registry[key] = None
    try:
        yield
    except BaseException:
        registry.pop(key, None)
        raise
    if registry.get(key) is None:
        registry.pop(key, None)


async def read_task_meta(store, repo: str, task_id: str):
    """The stored meta, or None. Swallows a read failure on purpose: every
    caller here is mirroring display state, and a store hiccup must not break
    the stream it is decorating."""
    try:
        return await store.aget(("tasks", repo), task_id)
    except Exception:  # noqa: BLE001
        logger.exception("task meta read failed for %s", task_id)
        return None


MAX_BUDGET_TOPUP_USD = 100.0
def check_budget_topup(delta: float) -> None:
    """Reject a resume top-up outside [0, MAX_BUDGET_TOPUP_USD]. Zero is a
    valid delta: the dashboard's resume panel only shows the budget field when
    the task is nearly out of money and sends 0 otherwise (a merge failure or
    an operator Stop has nothing to do with cost), and the escalated branch of
    resume_task already words its note for "no budget added". Rejecting 0
    (as this did until 2026-09-09) left every such resume stuck on a 400 with
    no field on screen to fix."""
    if not (0 <= delta <= MAX_BUDGET_TOPUP_USD):
        raise HTTPException(
            400, f"additional_budget_usd must be between 0 and ${MAX_BUDGET_TOPUP_USD:.2f}")


# Live-log buffers: each publisher appends its log entries here, and the
# hydrate endpoints return whichever of this and the durable source is
# fuller. The durable sources hold much less (a task's checkpoint keeps
# per-pass summaries; a planning thread's messages get REWRITTEN by
# summarization), so without this a refresh or task switch mid-run swapped a
# rich live view for a skeleton (2026-08-28). In-process by design: lossless
# while the server lives, no store churn, and after a restart the durable
# sources are the fallback.
LIVE_LOG_MAX_ENTRIES = 3000   # matches the frontend's MAX_LOG_ENTRIES cap
LIVE_LOG_MAX_KEYS = 12        # LRU-ish: enough for every concurrently-viewed run
live_task_log: dict[str, list] = {}

# The durable half of that buffer is live_state.task_recorders (one
# planning_log.Recorder per running task): `live_task_log` dies with the
# process, and on 2026-09-14 a "lost" task's transcript had to be explained
# from a 123-character execution_log stub and a clean worktree.


def flush_task_log_bg(rec: planning_log.Recorder) -> None:
    """Fire and forget: a transcript must never delay the run it describes."""
    try:
        live_state.fire_and_forget(rec.flush())
    except Exception:  # noqa: BLE001
        logger.warning("task transcript flush could not be scheduled", exc_info=True)
# Monotonic per-task event ids for the socket-first hydrate (log_stream.py).
task_event_seq = log_stream.SeqCounter()
live_planning_log: dict[str, list] = {}
# The same, per planning session: the planning page hydrated and never
# connected, and its reconnect merged by log LENGTH (2026-09-29 audit, U4).
planning_event_seq = log_stream.SeqCounter()


def live_log_append(book: dict, key: str, entries: list, on_evict=None) -> None:
    buf = book.get(key)
    if buf is None:
        while len(book) >= LIVE_LOG_MAX_KEYS:
            evicted = next(iter(book))
            book.pop(evicted)
            if on_evict is not None:
                on_evict(evicted)
        buf = book[key] = []
    buf.extend(entries)
    if len(buf) > LIVE_LOG_MAX_ENTRIES:
        del buf[: len(buf) - LIVE_LOG_MAX_ENTRIES]


def fuller_log(buffered: list | None, durable: list | None) -> list:
    """The hydrate rule: MERGE the two sources by entry id, durable first.

    This was "whichever list is longer", which cannot merge: a durable list
    that is longer but older replaced newer live entries, and a shorter one
    was discarded even when it held entries the buffer never had (everything
    before this process started). Identity comes from the entry's own content
    -- see agent/log_stream.py."""
    return log_stream.merge(durable, buffered)


async def apply_transition(graph, thread_config: dict, patch: dict, as_node: str | None) -> None:
    """Write a lifecycle.Transition to the checkpoint. as_node=None leaves the
    checkpoint's own next node in place (see agent/lifecycle.py)."""
    if as_node is None:
        await graph.aupdate_state(thread_config, patch)
    else:
        await graph.aupdate_state(thread_config, patch, as_node=as_node)
