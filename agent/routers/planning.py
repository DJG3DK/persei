"""Planning sessions: create, list, read, archive, delete, stop, message,
the new-project decision, and the session stream.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-23 (follow-up
review, F8) after the tasks seam. The routes are unchanged --
tests/test_route_inventory.py and tests/test_repo_scope.py pin every path,
method, guard and repo check.

The planning TURN machinery stays in server.py: the background turn runner,
project creation, moving a session to a new project, the session lookup and
the agent builder. Routes reach them on `app.state` -- through wrappers the
server registers that look the name up in server.py at CALL time, so a test
that patches `server.build_planning_agent` or `server._find_planning_meta` is
still seen by a route here. `config` comes off app.state and PROJECTS off
agent.config, both at call time. Nothing here imports agent.server.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel

from agent import auth, log_stream, live_state, planning_log, task_runtime
from agent import config as agent_config
from agent.auth import User, check_repo_access, require_full_auth
from agent.frontend_route import normalize_override
from agent.graph import read_with_retry
from agent.planning_chat import planning_thread_config
from agent.planning_chat import _translate_message as _translate_planning_message
from agent.store_paging import recent_items
from agent.tasks import AttachmentEntry

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["planning"])

# The shared run state, as the SAME objects agent/live_state.py and
# agent/task_runtime.py hold.
_planning_subscribers = live_state.planning_subscribers
_running_planning_turns = live_state.running_planning_turns
_planning_recorders = live_state.planning_recorders
_claim_run_slot = task_runtime.claim_run_slot
_fuller_log = task_runtime.fuller_log
_live_planning_log = task_runtime.live_planning_log
_SUBSCRIBER_QUEUE_MAX = task_runtime.SUBSCRIBER_QUEUE_MAX


class CreatePlanningSessionRequest(BaseModel):
    repo: str
    route: Literal["auto", "frontend", "general"] = "auto"

class PlanningMessageRequest(BaseModel):
    text: str
    attachments: list[AttachmentEntry] | None = None  # manifest entries from /api/uploads

@router.post("/api/planning/sessions", status_code=201)
async def create_planning_session(request: Request, req: CreatePlanningSessionRequest, user: User = Depends(require_full_auth)):
    if req.repo not in agent_config.PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(agent_config.PROJECTS)}")
    check_repo_access(user, req.repo)
    session_id = uuid.uuid4().hex
    await request.app.state.store.aput(("planning", req.repo), session_id, {
        "session_id": session_id, "repo": req.repo, "created_at": time.time(),
        "updated_at": time.time(), "title": None, "plan_markdown": None, "cost_usd": 0.0,
        "archived": False, "category": None,
        "route_override": normalize_override(req.route),
    })
    return {"session_id": session_id, "repo": req.repo}

@router.get("/api/planning/sessions")
async def list_planning_sessions(request: Request, repo: str | None = None, user: User = Depends(require_full_auth)):
    store = request.app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in agent_config.PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await read_with_retry(lambda r=r: recent_items(store, ("planning", r), 100))
        items.extend(item.value for item in results)
    items.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return items

@router.get("/api/planning/sessions/{session_id}")
async def get_planning_session(request: Request, session_id: str, user: User = Depends(require_full_auth)):
    repo, meta = await request.app.state.find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    agent, _plan_ref, _tracker = await request.app.state.build_planning_agent(
        request.app.state.config, repo, request.app.state.checkpointer, request.app.state.store, starting_cost=meta.get("cost_usd", 0.0),
        session_id=session_id,
    )
    thread_config = planning_thread_config(session_id, repo)
    checkpoint = await agent.aget_state(thread_config)
    messages = (checkpoint.values.get("messages") or []) if checkpoint and checkpoint.values else []
    translated = [e for e in (_translate_planning_message(m) for m in messages) if e]
    # Three sources, each lossy in its own way. The checkpoint loses whatever
    # summarization compacted away; the live buffer loses everything when the
    # process exits; the durable transcript loses only what fell off its cap.
    # The transcript and the buffer share entry ids and merge by them; the
    # checkpoint's translation is stamped with the read's own time, so it is
    # matched by content and only fills what the other two lack (2026-09-26:
    # unioning it by id doubled every entry of every session).
    # The live buffer and the stream position are read together, before the
    # await: `seq` says which events this snapshot already contains, so it
    # must not name an event the copied buffer does not have.
    live = list(_live_planning_log.get(session_id) or [])
    seq = task_runtime.planning_event_seq.current(session_id)
    recorded = _fuller_log(live, await planning_log.load(request.app.state.store, repo, session_id))
    log = log_stream.fill_gaps(recorded, translated)
    return {"meta": meta, "log": log, "running": session_id in _running_planning_turns, "seq": seq}

@router.post("/api/planning/sessions/{session_id}/archive")
async def archive_planning_session(request: Request, session_id: str, user: User = Depends(require_full_auth)):
    """Closes out a planning conversation without deleting it -- its full
    history/plan stays reachable (same as an "archived" task), it just drops
    out of the sidebar's default active list. Hit from the "New Plan"
    button once the operator is done with the current plan, whether or not
    they actually built from it."""
    repo, meta = await request.app.state.find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    await request.app.state.store.aput(("planning", repo), session_id, {**meta, "archived": True})
    return {"ok": True}

@router.delete("/api/planning/sessions/{session_id}")
async def delete_planning_session(request: Request, session_id: str, user: User = Depends(require_full_auth)):
    """Remove a planning conversation entirely -- history, plan and all.

    Archiving keeps a session reachable, which is right for one you might
    revisit. This is for the ones you would not: a conversation abandoned
    part-way, or work that was done another way, where there is nothing worth
    keeping and leaving it listed is clutter. Mirrors DELETE /api/tasks/{id},
    including refusing while a turn is in flight -- stop it first, same as
    every other state-changing action here.
    """
    repo, meta = await request.app.state.find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)

    with _claim_run_slot(_running_planning_turns, session_id,
                         "planning session is processing a message"):
        await request.app.state.store.adelete(("planning", repo), session_id)
        # The conversation itself lives in the shared checkpointer under a
        # namespaced thread id (planning_thread_config), not under the bare
        # session id -- deleting only the Store record would leave every
        # message orphaned in the checkpoints table.
        await request.app.state.checkpointer.adelete_thread(f"planning:{session_id}")
        # The durable transcript too: a delete means gone, not gone from the
        # two lossy copies while the readable one survives.
        await planning_log.forget(request.app.state.store, repo, session_id)
        # In-process mirrors, or a later session reusing the id would inherit
        # this one's log.
        _live_planning_log.pop(session_id, None)
        task_runtime.planning_event_seq.forget(session_id)
        _planning_recorders.pop(session_id, None)
        for _q, ws in _planning_subscribers.pop(session_id, []):
            try:
                await ws.close(code=4000, reason="planning session deleted")
            except Exception:  # noqa: BLE001 -- a dead socket must not fail the delete
                pass
    return {"ok": True}

@router.post("/api/planning/sessions/{session_id}/stop")
async def stop_planning_turn(request: Request, session_id: str, user: User = Depends(require_full_auth)):
    """Cancels the in-flight planning turn and waits for teardown before
    responding -- same contract as stop_task, and for the same reason: firing
    cancel() and returning immediately would report "stopped" to the UI while a
    web_search or a 450s reasoning call is still in flight.

    A planning turn can legitimately run for many minutes (agent-planning-chat
    runs at reasoning_effort=high), so without this the only way to end one was
    to wait it out or restart the service -- and restarting kills the turn with
    no record, leaving the UI waiting on a turn that no longer exists.

    Fails CLOSED on repo resolution, same as stop_task: an authorization check
    that no-ops when it cannot reach its input is not a check.
    """
    repo, _meta = await request.app.state.find_planning_meta(session_id)
    if not repo:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    turn = _running_planning_turns.get(session_id)
    if not turn:
        raise HTTPException(409, "planning session is not processing a message")
    turn.cancel()
    try:
        await turn
    except asyncio.CancelledError:
        pass
    return {"ok": True}

@router.post("/api/planning/sessions/{session_id}/message", status_code=202)
async def send_planning_message(request: Request, session_id: str, req: PlanningMessageRequest, user: User = Depends(require_full_auth)):
    with _claim_run_slot(_running_planning_turns, session_id, "planning session is already processing a message"):
        repo, meta = await request.app.state.find_planning_meta(session_id)
        if not meta:
            raise HTTPException(404, "planning session not found")
        check_repo_access(user, repo)
        if not req.text.strip():
            raise HTTPException(400, "message text is required")
        _running_planning_turns[session_id] = asyncio.create_task(
            request.app.state.run_planning_turn_bg(session_id, repo, req.text.strip(),
                                  [a.model_dump() for a in req.attachments] if req.attachments else None,
                                  allowed_repos=user.allowed_repos,
                                  # role, not allowed_repos: None there means admin OR legacy unscoped
                                  is_admin=user.role == "admin", actor=user.email)
        )
        return {"ok": True}

class NewProjectDecisionRequest(BaseModel):
    decision: Literal["confirm", "dismiss"]
    # Overrides the proposal's `github` when the operator changes the box on
    # the confirm card; None keeps what the agent recorded.
    github: bool | None = None
    token_name: str | None = None

@router.post("/api/planning/sessions/{session_id}/new-project")
async def decide_planning_new_project(request: Request, session_id: str, req: NewProjectDecisionRequest,
                                      user: User = Depends(require_full_auth)):
    """Answer the confirm card a create_project tool call put on the session.

    The planner cannot create anything itself (agent/tools/planning_tools.py):
    it records a proposal in the session meta and the operator answers it
    here. Confirm runs the SAME code as POST /api/projects/create -- one
    provisioning path, whichever door the project came through -- and then
    moves the session onto the new repo so the conversation continues there
    with the new project's memory and sandbox. A failed create leaves the
    session where it is and answers 200 with the step list, so the card can
    show which step died and stay up for a retry.
    """
    auth.require_admin(user)
    repo, meta = await request.app.state.find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    if req.decision == "dismiss":
        # A plain meta write; a turn ending later re-reads the row and
        # preserves what it finds, so this needs no run slot.
        meta = {**meta, "new_project": None, "updated_at": time.time()}
        await request.app.state.store.aput(("planning", repo), session_id, meta)
        return {"project": None, "session": meta}

    proposal = meta.get("new_project")
    if not proposal:
        raise HTTPException(409, "this session has no proposed project to confirm")
    # The move rewrites the rows a running turn writes back at its end, so it
    # is refused mid-turn the same way delete is.
    with _claim_run_slot(_running_planning_turns, session_id,
                         "planning session is processing a message"):
        create_req = dict(
            name=proposal["name"],
            description=proposal.get("description") or "",
            github=bool(proposal.get("github")) if req.github is None else req.github,
            token_name=req.token_name,
        )
        result = await request.app.state.create_project_from_fields(create_req, user)
        if not result.get("ok"):
            return {"project": result, "session": meta}
        # reload_projects() ran inside _create_project, so the new repo is in
        # agent_config.PROJECTS and _find_planning_meta can see the moved row.
        new_meta = await request.app.state.move_planning_session(session_id, repo, result["name"], meta)
    return {"project": result, "session": new_meta}

@router.websocket("/api/planning/sessions/{session_id}/stream")
async def stream_planning_session(ws: WebSocket, session_id: str):
    # WebSocket.cookies is populated from the handshake's headers before
    # accept() is ever called -- validate first and close outright (never
    # accept then immediately drop) for an unauthorized or repo-mismatched
    # connection attempt.
    if not auth.ws_origin_allowed(ws.headers):
        await ws.close(code=4403)
        return
    user = await auth.get_user_from_ws_cookie(ws.app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: enforce the same forced-screen gates as require_full_auth. A
    # valid session cookie alone must not open the live stream while the user
    # is parked behind the forced password-change / 2FA-setup screen.
    if auth.forced_screen_block(user):
        await ws.close(code=4403)
        return
    repo, meta = await ws.app.state.find_planning_meta(session_id)
    if not meta or not user.can_access(repo):
        await ws.close(code=4403)
        return
    await ws.accept()
    # No eviction of prior connections -- see stream_task's own comment on
    # this exact same pattern. Multiple viewers on the same planning session
    # (different users, or the same user in two tabs) all stay live
    # simultaneously; each connection's own receiver() below cleans up only
    # its own entry when it actually disconnects.
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)  # audit M-34
    _planning_subscribers.setdefault(session_id, []).append((queue, ws))

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
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _planning_subscribers.get(session_id, []):
            _planning_subscribers[session_id].remove(entry)
        if session_id in _planning_subscribers and not _planning_subscribers[session_id]:
            del _planning_subscribers[session_id]  # audit M-34
