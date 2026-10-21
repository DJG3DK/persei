"""Build tasks: create, list, read, steer, approve, merge, delete, stream.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-23 (follow-up
review, F8) once what these routes share had homes that do not import the
server: task creation in agent/tasks.py, the live run state (event fan-out,
live-log buffers, run slot, snapshots) in agent/task_runtime.py, the task
lifecycle in agent/lifecycle.py. The routes are unchanged --
tests/test_route_inventory.py and tests/test_repo_scope.py pin every path,
method, guard and repo check.

What stays in server.py is the graph stream that drives a run,
`_stream_graph`; routes reach it on `request.app.state.stream_graph`, and the
config on `request.app.state.config`, looked up at call time. PROJECTS is read
off `agent.config` the same way, so a test that swaps it is seen.

The old names stay importable from server.py (the endpoint functions, the
request models, `_readable_repos`), so callers and tests that reach for them
there are unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from agent import audit, auth, history_index, lifecycle, live_state, planning_log, tasks, task_runtime
from agent import config as agent_config
from agent.routers import audit_store
from agent.auth import User, check_repo_access, require_full_auth
from agent.graph import read_with_retry
from agent.messages import add_message

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["tasks"])

# The run-state names these handlers were written against, as the SAME objects
# agent/task_runtime.py and agent/live_state.py hold.
_running_tasks = live_state.running_tasks
_subscribers = live_state.subscribers
_task_recorders = live_state.task_recorders
_claim_run_slot = task_runtime.claim_run_slot
_check_budget_topup = task_runtime.check_budget_topup
_state_snapshot_for_frontend = task_runtime.state_snapshot_for_frontend
_apply_plan_fallback = task_runtime.apply_plan_fallback
_fuller_log = task_runtime.fuller_log
_publish = task_runtime.publish
_SUBSCRIBER_QUEUE_MAX = task_runtime.SUBSCRIBER_QUEUE_MAX
_live_task_log = task_runtime.live_task_log
_task_event_seq = task_runtime.task_event_seq
write_task_meta = tasks.write_task_meta

_apply_transition = task_runtime.apply_transition


AttachmentEntry = tasks.AttachmentEntry

class CreateTaskRequest(BaseModel):
    # audit M-33: goal was accepted empty/whitespace (send_planning_message
    # already rejected that -- the two endpoints were inconsistent), and
    # budget_usd had no bounds so 0 tripped the guard on the first call and a
    # negative value was accepted straight into AgentState.
    # 80k, not 20k: a Build Now hands the whole plan document over as the goal,
    # and a full-frontend restyle plan came in at 20,364 chars on 2026-09-09 --
    # every click returned 422 and the planning view had nowhere to show it.
    # Still bounded; a plan past 80k is a document, not a task.
    goal: str = Field(min_length=1, max_length=80_000)
    repo: str
    budget_usd: float | None = Field(default=None, gt=0, le=1000)
    attachments: list[AttachmentEntry] | None = None  # manifest entries from /api/uploads
    # "auto" decides from category/paths/keywords (agent/frontend_route.py);
    # "frontend"/"general" is the operator overriding that.
    route: Literal["auto", "frontend", "general"] = "auto"
    # Build Now from a planning session: the session's spend is carried onto
    # this task, so the task's cost is the whole cost (agent/tasks.py).
    planning_session_id: str | None = Field(default=None, max_length=64)

class SendMessageRequest(BaseModel):
    text: str

class ResumeTaskRequest(BaseModel):
    additional_budget_usd: float
    message: str | None = None

class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject", "respond"]
    message: str | None = None  # only meaningful for a reject -- explains why to the model

async def _resolve_task_repo(app, task_id: str) -> str | None:
    """A handful of task endpoints (message/stop) only ever needed task_id
    before per-user repo access existed -- this looks up which repo a task
    belongs to from its own checkpoint state, the same source resume/
    approve/get/delete already read `repo` from directly."""
    checkpoint = await app.state.graph.aget_state({"configurable": {"thread_id": task_id}})
    if not checkpoint or not checkpoint.values:
        return None
    return checkpoint.values.get("repo")

@router.get("/api/repos")
def list_repos(request: Request, user: User = Depends(require_full_auth)):
    return [r for r in agent_config.PROJECTS if user.can_access(r)]

def _readable_repos(user: User) -> list[str]:
    """Which projects a task started by `user` may READ for reference.

    Their own access, resolved here rather than carried as "None means
    everything": the task stores a concrete list, so a task resumed from a
    checkpoint written before this existed falls back to its own repo alone
    instead of silently to all of them. See agent/tools/reference_tools.py.
    """
    if user.allowed_repos is None:
        return sorted(agent_config.PROJECTS)
    return sorted(r for r in user.allowed_repos if r in agent_config.PROJECTS)

@router.post("/api/tasks", status_code=201)
async def create_task(request: Request, req: CreateTaskRequest, user: User = Depends(require_full_auth)):
    if req.repo not in agent_config.PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(agent_config.PROJECTS)}")
    check_repo_access(user, req.repo)
    # audit M-33: reject a whitespace-only goal (Field min_length=1 still lets a
    # lone space through), matching send_planning_message's own check.
    return await tasks.start_task(request.app, 
        req.goal, req.repo, req.budget_usd, req.route,
        planning_session_id=req.planning_session_id,
        # Per project, not per account: see User.auto_approves.
        auto_approve_commands=user.auto_approves(req.repo),
        require_merge_review=user.require_merge_review,
        # What this task may read for reference, from the creator's own
        # access at the time -- see _readable_repos.
        reference_repos=_readable_repos(user),
        attachments=[a.model_dump() for a in req.attachments] if req.attachments else None,
    )

@router.get("/api/tasks")
async def list_tasks(request: Request, repo: str | None = None, user: User = Depends(require_full_auth)):
    store = request.app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in agent_config.PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await read_with_retry(lambda r=r: store.asearch(("tasks", r), limit=50))
        items.extend(_list_entry(item.value) for item in results)
    items.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return items


# The dashboard polls this every few seconds. With whole records it had grown
# to ~900KB (goals carry the inbox alert, earlier attempts and pasted logs;
# latest_todos the plan), and on a slow connection no poll finished before
# the next began (2026-10-21). The list shows two lines of a goal, so it
# gets a preview; the task page reads the whole record from /api/tasks/{id}.
GOAL_PREVIEW_CHARS = 600
_LIST_OMITS = ("latest_todos",)


def _list_entry(task: dict) -> dict:
    entry = {k: v for k, v in task.items() if k not in _LIST_OMITS}
    goal = entry.get("goal")
    if isinstance(goal, str) and len(goal) > GOAL_PREVIEW_CHARS:
        entry["goal"] = goal[:GOAL_PREVIEW_CHARS]
        entry["goal_truncated"] = True
    return entry

@router.post("/api/tasks/{task_id}/message")
async def send_message(request: Request, task_id: str, req: SendMessageRequest, user: User = Depends(require_full_auth)):
    if task_id not in _running_tasks:
        raise HTTPException(409, "task is not running -- nothing would read this message")
    repo = await _resolve_task_repo(request.app, task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    add_message(task_id, req.text)
    # Published immediately so the UI shows the nudge landed right away --
    # work_node (agent/nodes/work.py) drains this mailbox at the start of
    # its next pass, which can be much later than the moment this message
    # was sent (a "work" pass can span many inner turns before
    # verify_and_ship ever loops back) -- a message that visibly "goes
    # nowhere" in the meantime is worse than no feedback.
    _publish(task_id, {
        "type": "node_update",
        "node": "operator",
        "execution_log": [{
            "node": "operator",
            "step_id": None,
            "summary": "message sent -- will be picked up at the start of the next work pass",
            "detail": req.text,
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    })
    return {"ok": True}

@router.post("/api/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str, user: User = Depends(require_full_auth)):
    """Cancels the asyncio task actually driving this run and waits for it
    to finish tearing down before responding -- firing task.cancel() and
    returning immediately would tell the UI "stopped" while a shell command
    or LLM call might still be mid-flight for another few seconds, which is
    exactly the "doesn't actually stop" gap this exists to close. run_shell
    (agent/tools/shell.py) kills the whole process group on cancellation
    too, not just abandons the await -- a naive stop here would otherwise
    leave e.g. a typecheck/test subprocess and everything it forked running
    orphaned on the server. _stream_graph's own CancelledError handler
    records the real cost-so-far and flips Store status to "stopped" before
    this returns.
    """
    repo = await _resolve_task_repo(request.app, task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    task = _running_tasks.get(task_id)
    if not task:
        raise HTTPException(409, "task is not running")
    task.cancel()
    # audit M-34: bound the wait for teardown. A slow Postgres in the
    # CancelledError handler could otherwise hang this request indefinitely; the
    # cancellation itself has already been requested, so after the timeout we
    # return and let teardown finish in the background rather than block the UI.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warning("stop_task: teardown for %s still running after 30s; returning anyway", task_id)
    return {"ok": True}

@router.post("/api/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str, req: ResumeTaskRequest, user: User = Depends(require_full_auth)):
    """Escalation is a circuit breaker, not a dead end -- the graph's full
    state (plan, execution log, cost so far) is still sitting in the
    checkpointer, so an escalated or budget-exhausted task has a path back
    in: add budget and continue rather than being stuck looking abandoned.

    Also covers a task orphaned by a backend restart mid-run -- Store still
    says "running" but nothing is actually driving it (this pm2 process gets
    restarted routinely to deploy fixes; a task that happened to be running
    at that moment would otherwise look "running" forever with no way back
    in either).

    These two cases need different handling:

    - Orphaned/stopped: patch `budget_usd`/`max_iterations`/`task_id`
      without `as_node` -- LangGraph resumes at whatever node the checkpoint
      already has as `next` (either "work", if the process died mid-work-pass
      or before one ever started, or "verify_and_ship", if it died between a
      completed work pass and the gate re-running). Nothing about escalated/
      pending_feedback is touched, so routing is exactly whatever it already
      was -- no replanning, no lost progress.
    - Genuine escalation: `_route_after_verify` (outer_graph.py) checks
      `state["escalated"]` first, before pending_feedback, so simply
      patching `escalated=False` is not enough on its own -- it also needs a
      truthy `pending_feedback` to route to "work" rather than falling
      through to END. Patched `as_node="verify_and_ship"` so the conditional
      edge re-evaluates against the new state and correctly resolves to
      "work". `pending_feedback` here doubles as the actual instruction the
      deep agent sees as a new HumanMessage on its next work.py pass (case 2
      in work.py's own docstring) -- not just a routing signal, real
      content: what the escalation was and that budget was added, plus the
      operator's own resume message if they left one.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = request.app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        meta = await request.app.state.store.aget(("tasks", values["repo"]), task_id)
        store_status = meta.value.get("status") if meta else None
        # Bounds the top-up before anything else: the field is a delta, and a
        # negative or enormous one is refused rather than applied.
        _check_budget_topup(req.additional_budget_usd)
        try:
            t = lifecycle.resume(values, store_status, message=req.message,
                                 additional_budget=req.additional_budget_usd)
        except lifecycle.Refused as e:
            raise HTTPException(e.status, e.detail)
        new_budget = t.extra["new_budget_usd"]
        new_max_iterations = t.extra["new_max_iterations"]
        if t.mailbox_message:
            add_message(task_id, t.mailbox_message)
        # task_id explicitly: tasks checkpointed before the field existed.
        await _apply_transition(graph, thread_config, {**t.patch, "task_id": task_id}, t.as_node)

        _running_tasks[task_id] = asyncio.create_task(
            request.app.state.stream_graph(task_id, values["repo"], values["goal"], new_budget, None)
        )
        return {"ok": True, "new_budget_usd": new_budget, "new_max_iterations": new_max_iterations}

@router.get("/api/tasks/{task_id}/diff")
async def get_task_diff(request: Request, task_id: str, user: User = Depends(require_full_auth)):
    """The task's current diff against its branch point -- committed AND
    uncommitted work, plus untracked files. Serves both halves of the diff
    panel: polled live while the task runs (watch the agent's edits land),
    and rendered as the final look when the task parks on awaiting_merge.
    Read-only; nothing from the request reaches a command line (repo resolves
    through agent_config.PROJECTS, git output is parsed server-side)."""
    repo = await _resolve_task_repo(request.app, task_id)
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    from agent.task_diff import collect_task_diff
    from agent.tools.git import task_branch_name
    return await collect_task_diff(repo, task_branch=task_branch_name(task_id))

@router.get("/api/tasks/{task_id}/file")
async def get_task_file(request: Request, task_id: str, path: str, user: User = Depends(require_full_auth)):
    """One file as the task's branch has it, and as it was before the task --
    the two sides of the final-look editor. Read from git objects; `path` is
    validated and only ever used as an object name."""
    repo = await _resolve_task_repo(request.app, task_id)
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    from agent.task_diff import read_task_file
    from agent.tools.git import task_branch_name
    try:
        return await read_task_file(repo, task_branch_name(task_id), path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))

class OperatorEditFile(BaseModel):
    path: str
    content: str

class OperatorEditRequest(BaseModel):
    base_sha: str
    files: list[OperatorEditFile]
    note: str | None = None

@router.post("/api/tasks/{task_id}/edits")
async def submit_operator_edits(request: Request, task_id: str, req: OperatorEditRequest, user: User = Depends(require_full_auth)):
    """The operator's hand fix from the final-look panel.

    Only for a task parked on awaiting_merge, and only against the exact sha
    they were shown. Nothing is written here: the edit goes into state and the
    task's own run applies it under the project lock (see
    AgentState.operator_edits), then commits, checks, reviews and parks for
    approval again -- a hand edit passes the same gate as the agent's.
    """
    from agent.task_diff import MAX_EDIT_FILE_BYTES, MAX_EDIT_FILES, valid_edit_path

    if not req.files:
        raise HTTPException(400, "no files to save")
    if len(req.files) > MAX_EDIT_FILES:
        raise HTTPException(400, f"at most {MAX_EDIT_FILES} files per save")
    files = []
    for f in req.files:
        clean = valid_edit_path(f.path)
        if clean is None:
            raise HTTPException(400, f"that path cannot be edited: {f.path!r}")
        if len(f.content.encode("utf-8")) > MAX_EDIT_FILE_BYTES:
            raise HTTPException(400, f"{clean} is too large to save from the editor")
        files.append({"path": clean, "content": f.content})

    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = request.app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        note = (req.note or "").strip()[:500] or None
        try:
            t = lifecycle.operator_edit(values, None, base_sha=req.base_sha, files=files,
                                        note=note, by=user.email)
        except lifecycle.Refused as e:
            raise HTTPException(e.status, e.detail)
        # as_node="work": the next node is verify_and_ship, which applies the
        # edit and runs the gate -- no model call in between.
        await _apply_transition(graph, thread_config, t.patch, t.as_node)
        await audit.record(
            audit_store(request), actor=user.email, action="task.operator_edit",
            target=f'{values["repo"]}/{task_id[:8]}',
            detail=", ".join(f["path"] for f in files)[:200],
        )
        _running_tasks[task_id] = asyncio.create_task(
            request.app.state.stream_graph(task_id, values["repo"], values["goal"], values.get("budget_usd", 0.0), None)
        )
        return {"ok": True, "files": [f["path"] for f in files]}

class MergeDecisionRequest(BaseModel):
    decision: str  # "approve" | "request_changes"
    message: str | None = None
    # The commit the operator was shown. Optional for older clients; when
    # given, a mismatch with the parked commit is a 409 (see lifecycle).
    sha: str | None = None

@router.post("/api/tasks/{task_id}/merge-decision")
async def merge_decision(request: Request, task_id: str, req: MergeDecisionRequest, user: User = Depends(require_full_auth)):
    """The operator's final-look verdict on a task parked at awaiting_merge.

    approve -> patch merge_approved_sha to the EXACT sha the operator was
    shown and re-invoke the graph: _route_after_verify sees approved+
    outstanding and runs verify_and_ship again, so the one code path that
    knows how to merge/record/finalize does the merge. The sha equality in
    that gate is what makes this race-safe -- approval can never ship a
    commit the operator didn't look at.

    request_changes -> the operator's notes become pending_feedback, exactly
    the shape a review-service rejection produces, so the agent loops back
    into work with the notes as its next instruction.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = request.app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        try:
            t = lifecycle.merge_decision(values, None, req.decision, req.message, shown_sha=req.sha)
        except lifecycle.Refused as e:
            raise HTTPException(e.status, e.detail)
        pending = values["pending_merge_approval"]
        await _apply_transition(graph, thread_config, t.patch, t.as_node)
        await audit.record(
            audit_store(request), actor=user.email,
            action="merge.approve" if req.decision == "approve" else "merge.request_changes",
            target=f'{values["repo"]}/{task_id[:8]}',
            detail=pending.get("sha", "")[:12] if req.decision == "approve" else (req.message or "")[:200],
        )
        _running_tasks[task_id] = asyncio.create_task(
            request.app.state.stream_graph(task_id, values["repo"], values["goal"], values.get("budget_usd", 0.0), None)
        )
        return {"ok": True, "decision": req.decision}

def _approval_summary(action_request: dict, count: int) -> str:
    """One line naming what was approved: the tool, and the part of its
    arguments a person would recognise. Truncated hard -- this is a log
    entry, not a transcript, and a pasted file can be megabytes."""
    name = action_request.get("name") or action_request.get("action") or "?"
    args = action_request.get("args") or action_request.get("arguments") or {}
    if isinstance(args, dict):
        shown = args.get("command") or args.get("file_path") or args.get("path") or ""
    else:
        shown = str(args)
    line = f"{name}: {str(shown)[:160]}" if shown else str(name)
    return f"{line} (+{count - 1} more)" if count > 1 else line

@router.post("/api/tasks/{task_id}/approve")
async def approve_task(request: Request, task_id: str, req: ApprovalRequest, user: User = Depends(require_full_auth)):
    """Submits an operator decision on a pending human-in-the-loop approval
    request (deep_agent.py's INTERRUPT_ON -- a bash/write/edit call the
    coordinator or a subagent proposed that matched a sensitive-path/
    dangerous-command pattern). Structurally the same resume mechanism
    resume_task's escalated branch uses -- patch state `as_node=
    "verify_and_ship"` with a truthy `pending_feedback` so
    `_route_after_verify` routes to "work", and let work_node's own
    graph_input priority logic (approval_decision checked before
    pending_feedback -- see work.py's own docstring case 0) do the real
    work of constructing `Command(resume={"decisions": [...]})` against the
    same inner thread, resuming it exactly where interrupt() paused it.

    One decision applies uniformly to every action_request in this batch
    (approve-all or reject-all) -- deepagents' own protocol technically
    supports a distinct decision per action_request, but the model
    virtually always proposes one risky call at a time in practice, and a
    per-action-request UI is real added complexity for a case that's
    rare enough not to justify it in this first pass.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = request.app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        try:
            t = lifecycle.command_decision(values, None, req.decision, req.message)
        except lifecycle.Refused as e:
            raise HTTPException(e.status, e.detail)
        pending = values["pending_approval"]
        action_count = t.extra["count"]
        await _apply_transition(graph, thread_config, {**t.patch, "task_id": task_id}, t.as_node)

        # What was approved matters as much as that it was: the first action
        # request's tool and a short form of its arguments, so a later reader
        # can see which command a person let through.
        first = (pending.get("action_requests") or [{}])[0]
        await audit.record(
            audit_store(request), actor=user.email,
            action="command.approve" if req.decision == "approve" else "command.reject",
            target=f'{values["repo"]}/{task_id[:8]}',
            detail=_approval_summary(first, action_count),
        )

        _running_tasks[task_id] = asyncio.create_task(
            request.app.state.stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
        )
        return {"ok": True, "decision": req.decision}

@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str, repo: str, user: User = Depends(require_full_auth)):
    check_repo_access(user, repo)
    store = request.app.state.store
    meta = await read_with_retry(lambda: store.aget(("tasks", repo), task_id))
    if not meta:
        raise HTTPException(404, "task not found")
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await read_with_retry(lambda: request.app.state.graph.aget_state(thread_config))
    values = checkpoint.values if checkpoint else None
    # Same "orphaned" condition resume_task already accepts (store says
    # "running" but nothing is actually driving it -- e.g. a backend restart
    # mid-run) -- surfaced here too so the frontend can offer Resume instead
    # of leaving the task looking merely slow forever.
    orphaned = bool(
        meta.value.get("status") == "running"
        and task_id not in _running_tasks
        and not (values or {}).get("escalated")
    )
    # _state_snapshot_for_frontend translates latest_todos -> plan (the
    # AgentState has no `plan` key at all -- see that function's own
    # docstring); without it, a page load/reconnect would show an empty
    # plan until the next live "todos" event happened to arrive.
    snapshot = _apply_plan_fallback(_state_snapshot_for_frontend(values) if values else None, meta.value)
    if snapshot is not None:
        # Cost freshness on hydrate (2026-08-28): the checkpoint's
        # cost_so_far only advances when a work pass RETURNS, so a refresh
        # mid-pass showed a stale number until the next live tick. The meta
        # mirror tracks live spend -- take whichever is higher (cost only
        # ever grows within a run).
        _meta_cost = meta.value.get("cost_so_far")
        if isinstance(_meta_cost, (int, float)) and _meta_cost > (snapshot.get("cost_so_far") or 0):
            snapshot["cost_so_far"] = _meta_cost
        # Full detailed history across refresh/task-switch -- see the live-log
        # buffer's own comment. The checkpoint's execution_log (per-pass
        # summaries) stays the durable fallback.
        # Three sources, fullest wins: this process's live buffer, the durable
        # transcript (survives a restart -- agent/planning_log.py), and the
        # checkpoint's per-pass summaries as the last resort.
        durable = await planning_log.load(store, repo, task_id,
                                          namespace=planning_log.TASK_NAMESPACE)
        snapshot["execution_log"] = _fuller_log(
            _live_task_log.get(task_id),
            _fuller_log(durable, snapshot.get("execution_log")))
    # Where this snapshot sits in the event stream. The browser opens its
    # socket first and buffers; on replay it drops anything at or below this,
    # so an event already folded into the snapshot is not applied twice.
    return {"meta": meta.value, "state": snapshot, "orphaned": orphaned,
            "seq": _task_event_seq.current(task_id)}

@router.delete("/api/tasks/{task_id}")
async def delete_task(request: Request, task_id: str, repo: str, user: User = Depends(require_full_auth)):
    """Removes a finished task from the list entirely -- for work that was
    completed by hand (e.g. an operator finished it directly and merged/
    deployed outside the agent), where leaving it sitting as "stopped"
    forever would be misleading clutter, not a useful record. Refuses to
    delete a task that's still actively running -- stop it first, same as
    any other state-changing action here.
    """
    check_repo_access(user, repo)
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        store = request.app.state.store
        meta = await store.aget(("tasks", repo), task_id)
        if not meta:
            raise HTTPException(404, "task not found")
        # BEFORE the two deletes below, and for the same reason
        # agent/consolidation.py indexes before it prunes. A task row and
        # its build transcript are corpora of the history index with NO
        # write-time hook -- unlike an episode, they are indexed only by the
        # nightly sync_project -- so a task deleted from the dashboard
        # before the next nightly run was never indexed at all, and
        # demote_missing cannot rescue it because there is no row to demote.
        # Best-effort, exactly like episodes.write_episode's own call: a
        # stored record that is not searchable is a gap the next sync
        # closes; a deleted record that was never indexed is gone.
        log_item = await store.aget((planning_log.TASK_NAMESPACE, repo), task_id)
        await history_index.index_task(
            request.app.state.config, repo, task_id, meta.value,
            log_item.value if log_item is not None else None)
        await store.adelete(("tasks", repo), task_id)
        # Its own workspace goes with it (agent/workspaces.py). Uncommitted
        # edits in it go too -- deleting a task is the operator saying the
        # work is done with; the branch keeps anything it committed.
        from agent import workspaces
        await workspaces.remove(repo, task_id)
        # Nothing will ever stream for this task again.
        _live_task_log.pop(task_id, None)
        _task_recorders.pop(task_id, None)
        await planning_log.forget(store, repo, task_id, namespace=planning_log.TASK_NAMESPACE)
        _task_event_seq.forget(task_id)
        await request.app.state.checkpointer.adelete_thread(task_id)
        # Also delete the inner deep-agent thread's own checkpoints. Every "work"
        # pass runs the deep agent against a derived thread_id, f"{task_id}:work"
        # (see work.py's inner_thread_config), on the same checkpointer/DB as
        # the outer graph -- deleting only the outer task_id's thread would leave
        # that inner thread's checkpoints orphaned in the checkpoints table.
        await request.app.state.checkpointer.adelete_thread(f"{task_id}:work")
        # Fresh-restart generations (work.py's inner_thread_config with
        # generation > 0 -- see outer_state.py's inner_thread_generation) get
        # their own derived thread_ids too; delete those as well or they'd be
        # orphaned exactly the way the base :work thread would be. The actual
        # generation count lives in the outer thread's now-deleted checkpoint,
        # so sweep a bounded range instead -- adelete_thread on a nonexistent
        # thread is a harmless no-op, and MAX_THREAD_RESTARTS (currently 1)
        # keeps real generations far below this bound.
        for generation in range(1, 10):
            await request.app.state.checkpointer.adelete_thread(f"{task_id}:work:g{generation}")
        for _q, ws in _subscribers.pop(task_id, []):
            try:
                await ws.close(code=4000, reason="task deleted")
            except Exception:
                pass
        return {"ok": True}

@router.websocket("/api/tasks/{task_id}/stream")
async def stream_task(ws: WebSocket, task_id: str):
    user = await auth.get_user_from_ws_cookie(ws.app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: same forced-screen enforcement as the planning stream above.
    if auth.forced_screen_block(user):
        await ws.close(code=4403)
        return
    task_repo = await _resolve_task_repo(ws.app, task_id)
    # Fail CLOSED, same reasoning as the REST endpoints above: an
    # unresolvable repo previously meant "no check", which let any
    # authenticated user attach to the stream.
    if not task_repo or not user.can_access(task_repo):
        await ws.close(code=4403)
        return
    await ws.accept()

    # Every connection for this task_id gets its own queue and stays
    # subscribed for its own lifetime -- multiple viewers (different users,
    # or the same user in two tabs) are simultaneously live on the same
    # task, all receiving the same fan-out from _publish(). This used to
    # evict every prior connection the moment a new one arrived ("only the
    # newest connection should ever receive events"), which was fine when
    # this system had exactly one operator but broke outright the moment a
    # second real user existed -- whoever opened the task last would silently
    # kick everyone else's live view. Each connection's own receiver() below
    # still detects and cleans up ITS OWN disconnect independently, so a
    # genuinely stale/dead connection is removed on its own without needing
    # to evict anyone else's.
    # audit M-34: bounded, so a stalled-but-connected reader can't accumulate
    # every event of a multi-hour task in memory (_publish drops on overflow).
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
    _subscribers.setdefault(task_id, []).append((queue, ws))

    async def sender():
        while True:
            # Heartbeat (2026-08-28): a long model call can mean 60s+ of
            # total socket silence, and NAT/middleboxes between the operator
            # and this VPS kill idle TCP without telling either end -- the
            # browser's stream just goes still until a manual refresh
            # (reported live). A ping every 20s keeps bytes flowing through
            # every hop, and when the socket IS dead, send_json raises here
            # promptly so the client gets a real close event to react to
            # instead of silence. Clients ignore the "ping" type.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
            if event.get("type") == "closed":
                break

    async def receiver():
        # The client never actually sends anything on this socket -- this
        # exists purely to detect a disconnect promptly. `await queue.get()`
        # in sender() has no way to notice the client closed the connection;
        # it only finds out lazily, the next time it tries to send and that
        # fails. Between those two points the stale queue stays subscribed,
        # which is harmless now beyond a handful of buffered events never
        # being delivered anywhere -- the `finally` block below still always
        # removes exactly this one connection's own entry, never another
        # viewer's.
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        # asyncio.wait() does not propagate exceptions from the tasks it
        # waits on (unlike awaiting a task directly) -- WebSocketDisconnect
        # from receiver() would otherwise never surface anywhere and could
        # log an "exception was never retrieved" warning. Retrieving it here
        # (without re-raising -- a disconnect is the expected, normal way
        # this ends) marks it handled either way.
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _subscribers.get(task_id, []):
            _subscribers[task_id].remove(entry)
        # audit M-34: drop the now-empty list so the dict doesn't keep one stale
        # key per task forever.
        if task_id in _subscribers and not _subscribers[task_id]:
            del _subscribers[task_id]
