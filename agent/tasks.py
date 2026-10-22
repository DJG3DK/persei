"""Starting a build task: the one path every task comes through.

The New Task form, Build Now from a planning session and the GitHub inbox all
start tasks here, so a task is the same thing whoever started it -- same
classifier, same coder-seat decision, same budget default, same record.

Extracted from agent/server.py on 2026-09-23. It was the piece every remaining
router seam was waiting on: approving an inbox item starts a task, and so did
the task and planning routes, so none of them could leave server.py while
task creation lived there (docs/todo.md, "Split agent/server.py"). What stays
behind is the graph stream itself, `_stream_graph`, which drives the run and
publishes to the dashboard; it reaches this module's callers through
`app.state.stream_graph`, set when the app is created, rather than by import
-- the same way the routers reach server state without an import cycle.

server.py re-exports `write_task_meta` and `_attachments_note` as the SAME
objects and keeps `_start_task` as a one-line binding of `start_task` to its
app, so every existing call site and test override still lands.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

from agent import live_state, runtime_settings
from agent.classify import TEST_REMINDER_NOTE, TaskClassification, classify_task
from agent.frontend_route import classify_frontend, normalize_override
from agent.outer_state import initial_state

logger = logging.getLogger("tektonix")


async def write_task_meta(store, repo: str, task_id: str, **updates) -> dict:
    """The single writer for a task's Store record.

    Every caller used to hand-build the entire dict, so a key one site
    remembered and another forgot was silently deleted by the next write.
    That is not hypothetical:

      * audit H-20 -- the error path was a full overwrite carrying neither
        cost_so_far nor escalation_reason, so a failed task erased the spend
        already mirrored mid-pass and reported $0.00 in stats and analytics;
      * the "stopped" path dropped escalation_reason outright, so stopping a
        task that had escalated lost the reason it escalated for.

    Read-merge-write, so unspecified keys are PRESERVED and forgetting one can
    no longer delete it. Pass an explicit None to clear a field deliberately.
    task_id and repo are always stamped -- they identify the record.
    """
    existing = await store.aget(("tasks", repo), task_id)
    record = dict(existing.value) if existing else {}
    # When the task finished, stamped once as it becomes done and cleared
    # when a resume takes it out of done. The GitHub inbox times its settle
    # wait from it; it used the record's updated_at, which any later write
    # (a cost mirror, a rename) moves (audit B40, 2026-10-22).
    if "status" in updates:
        if updates["status"] == "done" and record.get("status") != "done":
            record["finished_at"] = time.time()
        elif updates["status"] != "done":
            record.pop("finished_at", None)
    record.update(updates)
    record["task_id"] = task_id
    record["repo"] = repo
    record.setdefault("created_at", time.time())
    await store.aput(("tasks", repo), task_id, record)
    return record


class AttachmentEntry(BaseModel):
    # audit M-33: attachments were `list[dict]`, entirely unvalidated, and
    # _attachments_note indexed a['path'] unconditionally -- so {"kind":"image"}
    # (no path) was an unhandled KeyError -> 500, and the raw values landed in
    # the goal text the model reads. A real model rejects a malformed entry at
    # the API boundary with a 422 instead. Shared by the task and planning
    # routes: both hand the entries to attachments_note.
    kind: str
    path: str
    pages: int | None = None
    extracted_text: str | None = None
    note: str | None = None


def attachments_note(attachments: list[dict]) -> str:
    """The goal suffix that tells the agent what was attached and how to
    consume each kind -- written for the model, not the human."""
    lines = ["", "", "--- ATTACHED FILES (operator-provided, in the repo workspace) ---"]
    for a in attachments:
        kind = a.get("kind")
        path = a.get("path", "(unknown path)")
        if kind == "image":
            lines.append(f"- {path} (image): view it with the describe_image tool -- ask it specific questions if needed.")
        elif kind == "pdf" and a.get("extracted_text"):
            lines.append(f"- {path} (PDF, {a.get('pages', '?')} pages): extracted text at {a.get('extracted_text')} -- read that with your read tool.")
        elif kind == "pdf":
            lines.append(f"- {path} (PDF): no text layer could be extracted ({a.get('note', '')}).")
        else:
            lines.append(f"- {path} ({kind}): read it directly with your read tool (or bash for large/structured files).")
    lines.append("These are reference inputs, not part of the codebase -- never commit them or copy them into the repo.")
    return "\n".join(lines)


async def carry_planning_cost(store, repo: str, session_id: str, task_id: str) -> float:
    """The planning session's spend, carried onto the build task it starts.

    Planning cost used to vanish: a session banked its turns, Build Now handed
    the plan over as a goal, and the task started from $0.00 (2026-09-28).
    A session's spend is carried once: what it has spent beyond what earlier
    builds from the same session already carried. The session records what
    was carried and to which task, so the analytics page can show planning
    spend that never became a task as its own category.

    Nothing is carried unless the carry is recorded: a task that took the
    spend while the session still said it was uncarried would be double
    counted by the next build, so a failed write means 0.0 and the spend
    stays on the session as its own category."""
    try:
        async with live_state.planning_meta_lock(session_id):    # the cost mirror writes the same row
            item = await store.aget(("planning", repo), session_id)
            if not item or (item.value or {}).get("repo") not in (None, repo):
                return 0.0
            meta = dict(item.value)
            spent = float(meta.get("cost_usd") or 0.0)
            already = float(meta.get("carried_cost_usd") or 0.0)
            carried = round(max(0.0, spent - already), 6)
            meta["carried_cost_usd"] = round(already + carried, 6)
            meta["built_task_ids"] = [*(meta.get("built_task_ids") or []), task_id]
            await store.aput(("planning", repo), session_id, meta)
    except Exception:  # noqa: BLE001 -- a store hiccup must not stop the task
        logger.exception("planning cost carry: could not record the carry on session %s", session_id)
        return 0.0
    return carried


async def run_task(
    app, task_id: str, goal: str, repo: str, budget_usd: float, category: str,
    auto_approve_commands: bool = False,
    require_merge_review: bool = True,
    route: str = "general",
    route_reason: str | None = None,
    reference_repos: list[str] | None = None,
) -> None:
    """A fresh task's whole run: its initial state, handed to the graph stream."""
    state = initial_state(
        task_id=task_id, goal=goal, repo=repo, budget_usd=budget_usd,
        auto_approve_commands=auto_approve_commands,
        require_merge_review=require_merge_review,
        reference_repos=reference_repos,
        route=route, route_reason=route_reason,
    )
    await app.state.stream_graph(task_id, repo, goal, budget_usd, state, category=category,
                                 route=route, route_reason=route_reason)


async def start_task(
    app, goal: str, repo: str, budget_usd: float | None, route: str, *,
    auto_approve_commands: bool, require_merge_review: bool,
    reference_repos: list[str] | None = None,
    attachments: list[dict] | None = None, origin: str | None = None,
    planning_session_id: str | None = None,
) -> dict:
    """Classify, route and launch a task. `origin` is recorded on the task
    meta ("github" for inbox tasks)."""
    goal = goal.strip()
    if not goal:
        raise HTTPException(422, "goal must not be empty")
    task_id = str(uuid.uuid4())
    budget = budget_usd or runtime_settings.value("default_task_budget_usd")
    # Classified on the clean, operator-typed goal -- not the attachments
    # note appended below, which is boilerplate for the model, not signal
    # about what kind of task this is.
    # audit M-34: bound how long task creation blocks on the classifier. It
    # already self-times-out at 15s, but the operator shouldn't wait that long
    # for a task id over a label that's mostly for Analytics -- fall back to the
    # neutral classification if it's slow.
    try:
        classification = await asyncio.wait_for(classify_task(goal, app.state.config), timeout=8)
    except TimeoutError:
        logger.warning("task classification exceeded 8s; starting with fallback classification")
        classification = TaskClassification(category="other", needs_tests=False)
    raw_goal = goal
    if attachments:
        goal = goal + attachments_note(attachments)
    if classification.needs_tests:
        # An explicit directive, not a hope: the coordinator's system prompt
        # already tells it to delegate test-writing "when the task calls for
        # it", but nothing enforces that judgment call -- this makes the
        # judgment call for it up front, for the one signal (new/changed
        # testable logic) classify_task can actually assess before any code
        # has been read.
        goal = goal + TEST_REMINDER_NOTE
    # Which coder seat (agent/frontend_route.py): the operator's toggle, else
    # the category, the named paths, then keywords. Decided once, here.
    decision = classify_frontend(raw_goal, classification.category, normalize_override(route))
    if origin:
        # write_task_meta merges, so this survives the stream's own first
        # write whichever lands first.
        await write_task_meta(app.state.store, repo, task_id, origin=origin)
    if planning_session_id:
        carried = await carry_planning_cost(app.state.store, repo, planning_session_id, task_id)
        await write_task_meta(app.state.store, repo, task_id,
                              planning_session_id=planning_session_id, planning_cost_usd=carried)
    live_state.running_tasks[task_id] = asyncio.create_task(
        run_task(
            app, task_id, goal, repo, budget, classification.category,
            # Snapshot of the creator's own settings -- see outer_state.py.
            auto_approve_commands=auto_approve_commands,
            require_merge_review=require_merge_review,
            reference_repos=reference_repos or [],
            route=decision.route, route_reason=decision.reason,
        )
    )
    return {"task_id": task_id, "category": classification.category, "needs_tests": classification.needs_tests,
            "route": decision.route, "route_reason": decision.reason}
