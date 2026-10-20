"""FastAPI backend -- task creation/listing/state, WebSocket streaming of a
running task's graph execution. Checkpointer + store are opened once at
startup and shared for the app's lifetime (both are long-lived async context
managers, matching how langgraph-checkpoint-postgres expects to be used --
opening a fresh connection per request would be wasteful and race-prone).
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger("tektonix")

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.staticfiles import NotModifiedResponse
from email.utils import parsedate_to_datetime as _parsedate
from pydantic import BaseModel

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

from agent.config import PROJECTS, load_config, require_server_config
from agent.observability import install_langsmith
from agent import paths
from agent import tasks
from agent import task_runtime
from agent.outer_graph import build_outer_graph, open_checkpointer, open_store
from agent.graph import project_slot
from agent.routers import auth as auth_routes
from agent.routers import passkeys as passkeys_routes
from agent.routers import env_config as env_config_routes
from agent.routers import github as github_routes
from agent.routers import tasks as tasks_routes
from agent.routers import planning as planning_routes
from agent.routers import projects as projects_routes
from agent.routers import evals as evals_routes
from agent.routers import swebench as swebench_routes
from agent.routers import artifacts as artifact_routes
from agent.routers import settings as settings_routes
from agent.routers import model_config as model_config_routes
from agent.routers import analytics as analytics_routes
from agent.routers import push as push_routes
from agent.routers import review_proxy as review_proxy_routes
from agent.routers import review_sandbox as review_sandbox_routes
from agent.routers import jobs as jobs_routes
from agent.routers import uploads as uploads_routes
from agent.tools.model_rates import warm_rates
from agent.classify import classify_task
from agent import runtime_settings
from agent import github_inbox, github_settings
from agent import live_state
from agent import workspaces
from agent.store_paging import all_items
from agent import health as health_checks
from agent import episode_vectors, history_index
from agent import log_stream
from agent import plan_progress
from agent import planning_log
from agent.middleware.budget_guard import BudgetExceededError
from agent.frontend_route import RouteDecision, classify_frontend
from agent.planning_chat import build_planning_agent, classify_planning_difficulty, planning_thread_config, run_planning_turn
from agent import auth
from agent.auth import User
from agent.notify import notify_operators, notify_operators_bg, planning_url, task_alert, task_url, watch_services
from agent.mailer import send_plain_email

config = load_config()
# On the line after load_config, deliberately: the server-only variables are
# optional on Config so a local run does not need a mail server, and this is
# what puts the fail-fast boot back. It also refuses a DSN that is not
# Postgres -- see the function.
require_server_config(config)
install_langsmith(config)  # no-ops cleanly if LANGSMITH_TRACING isn't set -- see observability.py

# The live registries live in agent/live_state.py now: the routes that read
# them are moving into agent/routers/, and a route module cannot import back
# into this one without making the import a cycle. Aliased rather than
# re-declared so every existing reference in this file keeps working and --
# the part that matters -- keeps pointing at the SAME dict the routers hold.
_subscribers = live_state.subscribers
_running_tasks = live_state.running_tasks
_planning_subscribers = live_state.planning_subscribers
_running_planning_turns = live_state.running_planning_turns


def _log_warm_rates_failure(task: "asyncio.Task") -> None:
    """C-1: surface a warm_rates() startup failure instead of swallowing it.
    A failed rate warm means the hard budget ceiling has no data and would
    fail every task's first model call -- that must be visible in the log, not
    silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("CRITICAL: model-rate warm failed -- the budget ceiling has no rates: %r", exc)


async def _tasks_in(repo: str, statuses: tuple[str, ...]) -> list:
    """Every task record of `repo` whose status is one of `statuses`.

    The whole namespace, filtered -- NOT the newest N. The machinery that acts
    on parked or orphaned tasks (the supervisor, startup auto-resume, the
    inbox's "is this item still being handled" check) used a window of the
    newest 50 or 100 of everything, so on a project with a long history an
    escalated task just outside it was invisible: never healed, never
    resumed, and -- for the inbox -- read as finished, so its alert was
    proposed again as new work (2026-09-23 follow-up review, F5). The HTTP
    task list is a page for a person and keeps its own limit.
    """
    items = await all_items(app.state.store, ("tasks", repo))
    return [it for it in items if (it.value or {}).get("status") in statuses]


async def _supervisor_deps():
    """The supervisor's view of this server. See agent/supervisor.py."""
    from agent import supervisor

    async def list_parked(repo: str) -> list[dict]:
        # Only what the sweep acts on, from the whole namespace -- see _tasks_in.
        items = await _tasks_in(repo, ("escalated", "awaiting_merge"))
        return [{**item.value, "task_id": item.value.get("task_id") or item.key} for item in items]

    async def task_statuses(repo: str) -> dict[str, str]:
        # Every task, whatever its status -- the workspace sweep must know a
        # running task exists (supervisor.sweep_workspaces).
        items = await all_items(app.state.store, ("tasks", repo))
        return {item.key: (item.value or {}).get("status") or "unknown" for item in items}

    async def read_state(task_id: str):
        snap = await app.state.graph.aget_state({"configurable": {"thread_id": task_id}})
        if not snap or not snap.values:
            return None, ""
        return snap.values, (snap.config or {}).get("configurable", {}).get("checkpoint_id", "")

    async def apply(task_id: str, values: dict, t, start: bool) -> bool:
        # The same run-slot claim the endpoints use, so a heal and an
        # operator's own click on the same task cannot both start a run.
        try:
            with _claim_run_slot(_running_tasks, task_id, "task is already running"):
                await _apply_transition(app.state.graph, {"configurable": {"thread_id": task_id}},
                                        t.patch, t.as_node)
                if start:
                    _running_tasks[task_id] = asyncio.create_task(_stream_graph(
                        task_id, values["repo"], values["goal"], values.get("budget_usd", 0.0), None))
            return True
        except HTTPException:
            return False

    def notify(kind: str, repo: str, detail: str, values: dict) -> None:
        _notify_bg(task_alert(kind, repo, values.get("goal", ""), values.get("cost_so_far"), detail), repo=repo,
                   url=task_url(values["task_id"]) if values.get("task_id") else "/")

    return supervisor.Deps(
        projects=PROJECTS,
        list_tasks=list_parked,
        read_state=read_state,
        is_running=lambda task_id: _running_tasks.get(task_id) is not None,
        apply=apply,
        write_meta=lambda repo, task_id, **u: write_task_meta(app.state.store, repo, task_id, **u),
        notify=notify,
        reviewer_up=supervisor.review_services_up,
        live_clean=supervisor.live_is_clean,
        landed=supervisor.commit_landed,
        max_attempts=lambda: runtime_settings.as_int("auto_heal_attempts"),
        existing_workspaces=workspaces.existing,
        remove_workspace=workspaces.remove,
        task_statuses=task_statuses,
    )


async def _auto_resume_orphaned_tasks(startup_delay: float = 5.0) -> None:
    """Reconnect tasks orphaned by a restart, without operator action.

    At startup, find every task whose Store status says "running" while
    nothing drives it, and restart its driver exactly the way resume_task's
    orphan branch does (same +40 iteration headroom, no budget change -- the
    operator already approved this task's budget). Restarts are routine
    (every deploy), and until 2026-08-27 each one stranded any in-flight
    build until a human noticed the silence and clicked Resume.

    Deliberately NOT resumed:
      - escalated tasks (they are waiting for a human by design),
      - "stopped" (the operator chose that),
      - a task that made NO checkpoint progress since its last auto-resume
        (auto_resume_ckpt marker): if resuming it did nothing once, a boot
        loop of retries would amplify a poison task instead of surfacing it.
        It stays orphaned for a human, exactly as before this existed.
    """
    await asyncio.sleep(startup_delay)  # let startup settle; not correctness-critical
    graph = app.state.graph
    for repo in PROJECTS:
        try:
            # Every running/queued record, not the newest 50 -- see _tasks_in.
            items = await _tasks_in(repo, ("running", "queued"))
        except Exception:  # noqa: BLE001 -- one repo's failure must not strand the others
            logger.exception("auto-resume: task scan failed for %s", repo)
            continue
        for item in items:
            meta = item.value
            task_id = meta.get("task_id") or item.key
            # "queued" as well as "running": a task orphaned by a restart
            # while it was waiting for the project lock has exactly the same
            # problem -- a status the store believes and no process behind it.
            if meta.get("status") not in ("running", "queued") or task_id in _running_tasks:
                continue
            try:
                thread_config = {"configurable": {"thread_id": task_id}}
                checkpoint = await graph.aget_state(thread_config)
                values = checkpoint.values if checkpoint else None
                if not values or values.get("escalated"):
                    continue
                # Progress marker: outer checkpoint id PLUS the inner work
                # thread's latest checkpoint ts. The outer id alone is wrong
                # -- it stays frozen for an ENTIRE work pass (often 30+ min),
                # so two restarts inside one long pass would read as "no
                # progress" and wrongly strand a perfectly healthy task. The
                # inner thread checkpoints every step, so real work always
                # moves this marker.
                gen = values.get("inner_thread_generation", 0)
                inner_id = f"{task_id}:work:g{gen}" if gen else f"{task_id}:work"
                inner_ts = ""
                try:
                    inner_snap = await app.state.checkpointer.aget_tuple(
                        {"configurable": {"thread_id": inner_id}})
                    inner_ts = (inner_snap.checkpoint.get("ts") or "") if inner_snap else ""
                except Exception:  # noqa: BLE001 -- marker precision degrades, resume still works
                    logger.exception("auto-resume: inner-thread read failed for %s", task_id)
                outer_id = (checkpoint.config or {}).get("configurable", {}).get("checkpoint_id", "")
                ckpt_ts = f"{outer_id}|{inner_ts}"
                if meta.get("auto_resume_ckpt") == ckpt_ts:
                    logger.error(
                        "auto-resume: task %s made no progress since its last auto-resume -- "
                        "leaving it for the operator (possible poison task)", task_id)
                    continue
                await graph.aupdate_state(thread_config, {
                    "task_id": task_id,
                    "budget_usd": values["budget_usd"],
                    "max_iterations": values.get("max_iterations", 40) + 40,
                })
                await write_task_meta(app.state.store, repo, task_id,
                                      auto_resume_ckpt=ckpt_ts)
                _running_tasks[task_id] = asyncio.create_task(
                    _stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
                )
                logger.warning("auto-resume: reconnected orphaned task %s (%s) after restart", task_id, repo)
                _notify_bg(task_alert(
                    "auto_resumed", repo, values.get("goal", ""), values.get("cost_so_far"),
                    "The server restarted mid-run; the task reconnected automatically and is working again."),
                    repo=repo, url=task_url(task_id))
            except Exception:  # noqa: BLE001 -- one task's failure must not strand the others
                logger.exception("auto-resume: failed to reconnect task %s", task_id)


async def _drain_planning_turns(timeout: float = 15.0) -> None:
    """Cancel every in-flight planning turn and WAIT for its teardown.

    Called from lifespan shutdown while the store/checkpointer pools are
    still open -- the whole point. The tasks' own CancelledError handlers do
    the actual banking; this just guarantees they get to run against a live
    pool instead of racing process exit.
    """
    tasks = list(_running_planning_turns.values())
    if not tasks:
        return
    logger.info("shutdown: draining %d in-flight planning turn(s)", len(tasks))
    for t in tasks:
        t.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:  # pragma: no cover -- only a wedged teardown lands here
        logger.error("shutdown: planning turn %r did not tear down within %.0fs", t.get_name(), timeout)


# In the data directory: on a host install that is data/ beside the repo, in
# the bundle it is the agentdata volume, so the file outlives a rebuild of
# the container. It sat at the repo root before 2026-09-28, and one `up
# --build` between first boot and reading it lost the only admin password.
_INITIAL_PASSWORD_PATH = paths.DATA_DIR / ".initial-admin-password"


def _store_initial_password(password: str) -> None:
    """The first admin password, encrypted with AUTH_SECRET_KEY (the same
    AES-GCM construction as the TOTP secrets) into a 0600 file beside the
    repo. `scripts/show_initial_password.py` decrypts it once for the
    operator. Neither the log nor the disk ever holds it in clear."""
    try:
        enc = auth._encrypt_totp_secret(config, password)
        _INITIAL_PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
        _INITIAL_PASSWORD_PATH.touch(mode=0o600, exist_ok=True)
        _INITIAL_PASSWORD_PATH.chmod(0o600)
        _INITIAL_PASSWORD_PATH.write_text(enc + "\n")
    except Exception:  # noqa: BLE001 -- the account exists either way; say so in the log
        logger.exception("could not store the initial admin password at %s", _INITIAL_PASSWORD_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_checkpointer(config) as checkpointer, open_store(config) as store, auth.open_auth_pool(config) as auth_pool:
        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.auth_pool = auth_pool
        from agent import passkeys as _passkeys  # noqa: PLC0415
        await _passkeys.ensure_schema(auth_pool)
        generated_password = await auth.seed_admin_if_none(auth_pool, config.admin_email)
        if generated_password:
            # Only ever printed once, the very first time this deployment
            # has zero users -- must_change_password=True forces a real
            # password to replace this on first login, so it's not a
            # standing secret sitting in the log after that.
            # Not logged (CodeQL py/clear-text-logging-sensitive-data): logs
            # are copied, shipped and grepped, and a password in one is a
            # password in every copy. Written once to a 0600 file beside the
            # repo instead; the log says where.
            _store_initial_password(generated_password)
            logger.warning(
                "Seeded initial admin account %s. Its one-time password is stored encrypted; "
                "run `python scripts/show_initial_password.py` to read it "
                "(must be changed on first login).", config.admin_email,
            )
        # Both checkpointer and store passed to .compile() -- store isn't
        # actually read via LangGraph's own node-kwarg injection here (see
        # outer_graph.py's own comment: work_node/verify_and_ship_node use
        # app_config/pg_store, bound directly via functools.partial,
        # specifically to avoid that injection path), but passing it is
        # still correct/harmless and keeps this graph object consistent with
        # idiomatic LangGraph usage for anything else that might introspect it.
        app.state.graph = build_outer_graph(config, checkpointer, store).compile(
            checkpointer=checkpointer, store=store
        )
        # Auto-approve became per-project on 2026-09-11. An account that had
        # the switch on before that keeps the behaviour it had, with the
        # scope written down -- see backfill_auto_approve_repos for why this
        # is a backfill rather than a silent default.
        try:
            scoped = await auth.backfill_auto_approve_repos(auth_pool, list(PROJECTS))
            if scoped:
                logger.info("auto-approve: scoped %d pre-existing account(s) to %d project(s)",
                            scoped, len(PROJECTS))
        except Exception as e:  # noqa: BLE001 -- never block startup on a migration
            logger.error("auto-approve backfill failed (accounts stay unscoped): %s", e)

        # The keyword index over past tasks, on the auth pool rather than a
        # fourth pool against the same database. It migrates itself, and a
        # failure here costs history search and nothing else -- install_for
        # never raises.
        app.state.history_index = await history_index.install_for(config, pool=auth_pool)

        # The second retrieval leg, registered against the store that was
        # actually opened rather than against the config: a store opened
        # before an operator turned embeddings on carries no vector index,
        # and a leg registered over it would answer every query with the
        # most recently updated episodes while looking exactly like semantic
        # search. Nothing else changes -- search_history asks the registry.
        app.state.vector_leg = episode_vectors.install(store)

        # Stored runtime limits, before anything can build an agent with them.
        await runtime_settings.load(app.state.store)
        await github_settings.load(app.state.store)

        # No analytics pre-warm any more: those three panels read this box's
        # own logs now (agent/metrics.py), which is a file scan measured in
        # milliseconds rather than a paged LangSmith query that could exceed
        # nginx's proxy timeout. The caches, their locks and their
        # serve-stale-while-revalidate dance went with the scans.
        #
        # The rate table still warms: model_rates.estimate_cost's rate
        # table load includes a synchronous network call to OpenRouter's
        # pricing endpoint -- pre-warming it here means that blocking call
        # happens in a background thread before any real task needs a cost
        # estimate, not inline on the event loop the first time one does.
        # C-1: warm_rates() reads model-router/config.yaml; if that raises (bad
        # path, malformed yaml) the budget ceiling silently does not exist.
        # A bare create_task swallows the exception, so attach a done-callback
        # that surfaces it loudly at startup instead of at every task's first
        # model call.
        rates_warm_task = asyncio.create_task(warm_rates())
        rates_warm_task.add_done_callback(_log_warm_rates_failure)
        # Reconnect any build task a restart orphaned -- see the function's
        # own docstring. Backgrounded so startup never blocks on it.
        auto_resume_task = asyncio.create_task(_auto_resume_orphaned_tasks())
        # Heals infrastructure escalations and closes tasks whose work is
        # already on main -- agent/supervisor.py.
        from agent import supervisor
        supervisor_task = asyncio.create_task(supervisor.run_forever(await _supervisor_deps()))
        # Service-restart alerts (operator request 2026-08-28): a router or
        # bot restarting mid-task is exactly the kind of event that used to
        # be discovered by watching a silent screen. The agent backend
        # itself is excluded from the poll (its restart resets this watcher)
        # and announces itself with the startup line below instead.
        service_watch_task = asyncio.create_task(watch_services(auth_pool))
        # GitHub inbox poller (Settings -> GitHub). Sleeps until a project
        # switches a source on; see _github_poll_loop.
        github_poll_task = asyncio.create_task(_github_poll_loop())
        # The daily jobs (memory consolidation, the codebase map), run here
        # at the first quiet moment after they are due -- agent/jobs.py. The
        # host crons that used to do this are optional now, and the bundle
        # never had them.
        from agent import jobs
        jobs_task = asyncio.create_task(jobs.run_forever(app.state))
        # Throwaway databases a crashed agent left on checks-postgres --
        # agent/review_sandbox.py, sweep_orphans. A no-op without the bundle's
        # checks services, and never a startup failure.
        from agent import review_sandbox
        review_sweep_task = asyncio.create_task(review_sandbox.sweep_orphans())
        notify_operators_bg(auth_pool, "🔄 agent backend restarted (deploys land this way; "
                            "orphaned tasks auto-resume, planning turns re-send)")
        yield
        review_sweep_task.cancel()
        jobs_task.cancel()
        github_poll_task.cancel()
        service_watch_task.cancel()
        auto_resume_task.cancel()
        supervisor_task.cancel()
        # Drain in-flight planning turns BEFORE this `async with` block exits
        # and closes the Postgres pools. Left to the runtime, these tasks are
        # cancelled during asyncio.run() cleanup -- AFTER the pools are gone --
        # so the cancel-path teardown that banks the turn's plan/cost/title
        # (_bank_planning_turn) dies on its own store write. Confirmed live
        # 2026-08-27: a pm2 restart during a planning turn logged "failed to
        # persist planning progress" from exactly that ordering, and the
        # session's real spend (router-billed) was lost from the ledger.
        # Cancelling here, while the pools are still open, lets each turn's
        # CancelledError handler finish its banking write. The 15s ceiling is
        # generous -- banking is one store read + one write.
        await _drain_planning_turns(timeout=15.0)
        rates_warm_task.cancel()


app = FastAPI(lifespan=lifespan)
# On state from the moment the app exists, NOT in lifespan: the seams under
# agent/routers/ read config off the running app, and a TestClient exercises
# routes without ever entering lifespan. Setting it there would make every
# such test fail on a missing attribute rather than on anything real.
app.state.config = config

# CORS was allow_origins=["*"] with a comment claiming nginx tightened it in
# production. nginx sets no CORS headers at all, so nothing did -- the comment
# described a control that did not exist. Real exposure was limited (credentials
# were never allowed, and the session cookie is SameSite=strict so it is not
# sent cross-site anyway), but a wildcard on an authenticated app is not
# something to leave sitting behind a false comment.
#
# This app serves its own frontend, so same-origin requests do not use CORS at
# all and the correct production value is "no origins". Only a split dev setup
# (Vite on its own port) needs any, via CORS_ALLOW_ORIGINS.
if config.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Per-seam routers (agent/routers/). server.py is being split one seam at a
# time, never in one pass -- see docs/todo.md and
# docs/playbooks/README.md. tests/test_route_inventory.py is what makes each
# move safe: it pins every route's path, method and auth dependency, so a
# seam that moves either looks identical from outside or fails the snapshot.
# Included here, after the middleware and before the routes that are still in
# this file, so the order a request passes through is unchanged.
app.include_router(auth_routes.router)
app.include_router(passkeys_routes.router)
app.include_router(jobs_routes.router)
app.include_router(push_routes.router)
app.include_router(analytics_routes.router)
app.include_router(model_config_routes.router)
app.include_router(settings_routes.router)
app.include_router(env_config_routes.router)
app.include_router(github_routes.router)
app.include_router(tasks_routes.router)
app.include_router(planning_routes.router)
app.include_router(projects_routes.router)
app.include_router(evals_routes.router)
app.include_router(swebench_routes.router)
app.include_router(artifact_routes.router)
app.include_router(review_sandbox_routes.router)
app.include_router(review_proxy_routes.router)
app.include_router(uploads_routes.router)


# audit M-9: response security headers (defence-in-depth behind React's escaping,
# on an app that renders model-produced text throughout). CSRF still rests on the
# SameSite=strict session cookie -- these add the layers a grep found entirely
# missing. style-src allows 'unsafe-inline' because the Vite/React build emits
# inline styles; connect-src 'self' covers the same-origin REST + WebSocket.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


from starlette.responses import JSONResponse as _JSONResponse

# The body ceiling belongs to the upload limits it is derived from, which
# moved with the route to agent/routers/uploads.py (2026-09-27); the same
# value under the name the middleware and its test read.
REQUEST_BODY_MAX_BYTES = uploads_routes.REQUEST_BODY_MAX_BYTES
from datetime import UTC
from datetime import datetime as _dt


@app.middleware("http")
async def _body_size_limit(request, call_next):
    # audit M-13: reject an over-large request by its Content-Length before the
    # body is read, so no endpoint (uploads OR unbounded JSON goal/message/
    # attachments) can be handed an arbitrarily large body. The ceiling is sized
    # for the maximum legitimate upload batch; JSON bodies sit far below it.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > REQUEST_BODY_MAX_BYTES:
                return _JSONResponse(
                    {"detail": f"request body exceeds {REQUEST_BODY_MAX_BYTES // (1024*1024)}MB"},
                    status_code=413,
                )
        except ValueError:
            return _JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
    elif "chunked" in request.headers.get("transfer-encoding", "").lower():
        # A chunked request carries no Content-Length, so the check above sees
        # nothing and the body streams in unbounded -- one request can push this
        # single-process app into swap or OOM.
        #
        # Buffer it ourselves up to the same ceiling and replay it downstream.
        # An earlier attempt raised from inside a wrapped receive() instead;
        # that breaks the ASGI contract -- the exception surfaces from anyio as
        # "object _BodyTooLarge can't be used in 'await' expression" and takes
        # 30 unrelated tests with it. Buffering keeps the contract intact, and
        # the memory it costs is bounded by the cap, which is the whole point.
        body = b""
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return _JSONResponse({"detail": "client disconnected"}, status_code=400)
            body += message.get("body", b"")
            if len(body) > REQUEST_BODY_MAX_BYTES:
                return _JSONResponse(
                    {"detail": f"request body exceeds {REQUEST_BODY_MAX_BYTES // (1024*1024)}MB"},
                    status_code=413,
                )
            if not message.get("more_body", False):
                break

        async def _replay() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay

    return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ---------------------------------------------------------------------------
# Auth -- username/password + TOTP 2FA (agent/auth.py). Every route that
# touches a repo-scoped resource, here or in agent/routers/*, calls
# check_repo_access explicitly once `repo` is known (never uniform enough
# in shape -- query param, request body field, or an existing task/session's
# own stored repo -- for one FastAPI dependency to cover safely);
# tests/test_repo_scope.py pins each one. Analytics, Model Configuration and
# the other operator-only pages are admin-only outright (require_admin),
# not repo-scoped -- aggregate spend across every project, which LLM model
# each pinned role uses -- and tests/test_admin_only_routes.py pins those.
#
# The /api/auth/* routes themselves live in agent/routers/auth.py (2026-09-27).
# ---------------------------------------------------------------------------


# Both live in agent/auth.py now: a route module under agent/routers/ cannot
# import them from here without making the import a cycle. Re-exported rather
# than redefined so require_full_auth stays the SAME object -- the route
# inventory identifies a guard by its __name__, and every test that overrides
# it does so through app.dependency_overrides, which is keyed by identity.
_forced_screen_block = auth.forced_screen_block
require_full_auth = auth.require_full_auth


# In agent/live_state.py with the set it guards (2026-09-27): the projects
# router starts the cartographer through it and cannot import this module.
# The same function under the old name.
_spawn_background = live_state.spawn_background


@app.get("/api/health")
async def health():
    """Can this process do its job right now, and if not, which dependency is
    missing? Postgres, the router, the sandbox image, the review secret --
    each checked for real, none of them a model call (see agent/health.py).

    Public on purpose: a monitoring box, or a second person with curl, has no
    session. Nothing here is a secret -- a configured secret reports `true`,
    never its value, and the payload carries how many projects are onboarded
    but never which, since a private repo's name is the one thing in it that
    describes its owner rather than this process.

    503 when any check fails, so a probe that only reads the status code is
    still correct (until 2026-09-11 /api/health fell through to the SPA's
    index.html with a 200, which proved only that uvicorn served files).
    """
    # auth_pool is the same Postgres this deployment keeps everything in, and
    # it is a real pool with a liveness check on checkout -- so one SELECT 1
    # through it answers for the checkpointer and store too.
    payload = await health_checks.collect(
        getattr(app.state, "auth_pool", None), config.router_base_url, PROJECTS,
    )
    # How much is in flight: a count, never a name. The desktop app updates
    # the stack only when this is 0 (app/src-tauri/src/stack.rs).
    payload["busy"] = len(live_state.running_tasks) + len(live_state.running_planning_turns)
    return _JSONResponse(payload, status_code=200 if payload["ok"] else 503)


# ---------------------------------------------------------------------------
# GitHub integration: settings, inbox, approve links, poller
# (agent/github_settings.py, agent/github_inbox.py)
# ---------------------------------------------------------------------------


async def _github_open_auto_count(repo: str) -> int:
    """Auto-created tasks that are still running or parked on a human."""
    n = 0
    for it in await _tasks_in(repo, ("running", "escalated", "awaiting_approval", "awaiting_merge")):
        if it.value.get("origin") == "github":
            n += 1
    return n


# The statuses that mean an item is still being handled -- which is NOT the
# same as "a task is running right now", and getting that wrong is what put a
# finished piece of work back in the inbox asking to be done again.
#
#   running / queued / awaiting_*   -- in flight
#   escalated                       -- in the operator's list with a Resume
#                                      button; re-proposing underneath it
#                                      would queue the same work twice
#   done                            -- FINISHED. The fix is merged, or sitting
#                                      in a pull request waiting for a human.
#                                      The alert stays open on GitHub until
#                                      the scanner re-runs and says otherwise,
#                                      and that delay is not a reason to ask
#                                      for the work again.
#
# What is left -- stopped, error, or an id that no longer exists -- is a task
# that ended without delivering, and that is the only case where the item
# genuinely needs to go back in the queue.
_TASK_HANDLED_STATUSES = (
    "running", "queued", "awaiting_approval", "awaiting_merge", "escalated", "done",
)


async def _github_live_tasks(repo: str) -> set[str]:
    """Which of this project's tasks still count as handling their item.

    github_inbox.decide uses it to decide whether a `task_created` item should
    go back to the queue. Returning an empty set is a real answer ("nothing is
    handling anything"); the inbox only keeps items stuck when it is handed
    None, which is why a failure here re-raises rather than pretending.
    """
    live = set()
    # The whole namespace (see _tasks_in): a task outside a newest-N window is
    # still handling its item, and reading it as finished re-proposes the work.
    for it in await _tasks_in(repo, _TASK_HANDLED_STATUSES):
        live.add(it.value.get("task_id") or it.key)
    return live


async def _github_finished_tasks(repo: str) -> dict[str, float]:
    """This project's finished tasks and when they finished: the store's own
    last write, which is the one that set the task done."""
    out = {}
    for it in await _tasks_in(repo, ("done",)):
        stamp = it.updated_at.timestamp() if hasattr(it.updated_at, "timestamp") else float(it.updated_at or 0)
        out[it.value.get("task_id") or it.key] = stamp
    return out


async def _github_create_task(repo: str, goal: str, budget: float, route: str) -> str:
    """A task GitHub asked for, not a person.

    ONE invariant, and it is the one that matters: merge review is always
    required, whatever any user preference says. Nothing an inbox task does
    reaches the default branch without the operator approving the merge. Auto
    inbox + merge-review-off is what would turn this into an unattended merge
    bot; the README promises Auto "keeps the operator's final merge approval",
    and this line is where that promise is kept.

    Auto-approve of gated file/shell actions, by contrast, follows the
    operator's own per-project switch (auth.repo_auto_approves), not a
    hard-coded False: a dependency bump edits package.json, the lockfile and
    sometimes a workflow, and each trips the sensitive-path gate, so an
    inbox-started CRITICAL fix parked at awaiting_approval on a version
    string while the operator had Auto on for that project (2026-09-13). The
    gate that guards the repo is merge review, above.

    Scoped to admin accounts and to projects that account listed, so the
    switch still cannot be widened by a non-admin preference, and it fails
    closed if the accounts cannot be read.
    """
    auto = await auth.repo_auto_approves(app.state.auth_pool, repo)
    out = await _start_task(
        goal, repo, budget, route,
        auto_approve_commands=auto,
        # Never from a preference. See this function's docstring.
        require_merge_review=True,
        # Nobody typed this goal and nobody is watching it start, so it gets
        # the narrowest scope there is: its own repository. Reading another
        # project is for a person asking for it.
        reference_repos=[],
        origin="github",
    )
    return out["task_id"]


async def _github_notify(text: str, repo: str) -> None:
    settings = github_settings.current()
    if settings["notify"].get("telegram", True):
        await notify_operators(app.state.auth_pool, text, repo, url="/inbox")
    if settings["notify"].get("email"):
        to = settings["notify"].get("email_to") or config.admin_email
        try:
            await send_plain_email(config, to, f"[Persei] GitHub inbox: {repo}", text)
        except Exception:  # noqa: BLE001 -- best-effort, like every alert
            logger.exception("github inbox: email to %s failed", to)


# On app.state, not a module global: the GitHub settings route that sets it
# lives in agent/routers/settings.py now, and a router cannot import back
# from this module without making the import a cycle. The poller below still
# reaches it by the same name.
_github_poll_wake = asyncio.Event()
app.state.github_poll_wake = _github_poll_wake
# On app.state for the same reason as github_poll_wake: the inbox route that
# reads it lives in agent/routers/github.py now, and a router cannot import
# back from this module without making the import a cycle. The poller below
# writes both.
_github_last_poll: dict | None = None
app.state.github_last_poll = None


async def _github_poll_once() -> list[dict]:
    global _github_last_poll
    results = await github_inbox.poll_all(
        app.state.store, config,
        create_task=_github_create_task, notify=_github_notify, open_auto_count=_github_open_auto_count,
        live_tasks=_github_live_tasks, finished_tasks=_github_finished_tasks,
    )
    _github_last_poll = {"at": time.time(), "results": results}
    app.state.github_last_poll = _github_last_poll
    return results


# The poller stays in this module: it reaches _start_task, the notifier and
# the auto-count, which is most of the server. What the /api/github/poll
# route needs is only the ability to TRIGGER it, so the function is put where
# a router can find it rather than the router pulling the machinery across.
app.state.github_poll_once = _github_poll_once
# Same reasoning, narrower interface: approving an inbox item starts a real
# task, and task creation is _start_task and everything under it. The inbox
# routes get the one function rather than the machinery.
app.state.github_create_task = _github_create_task


async def _github_poll_loop(startup_delay: float = 20.0) -> None:
    """Runs forever; polls every poll_interval_min while any project has a
    source switched on, and wakes early when settings change."""
    await asyncio.sleep(startup_delay)
    while True:
        settings = github_settings.current()
        interval = max(2, int(settings.get("poll_interval_min", 10))) * 60
        if github_settings.enabled_projects(settings):
            try:
                await _github_poll_once()
            except Exception:  # noqa: BLE001 -- the loop must survive anything
                logger.exception("github inbox: poll pass failed")
        try:
            await asyncio.wait_for(_github_poll_wake.wait(), timeout=interval)
        except TimeoutError:
            pass
        _github_poll_wake.clear()


_attachments_note = tasks.attachments_note   # agent/tasks.py

# The review proxy lives in agent/routers/review_proxy.py (2026-09-27); the
# same handler under its old name, for the test that drives it directly.
review_proxy = review_proxy_routes.review_proxy


# The live run state moved to agent/task_runtime.py (2026-09-23) so the task
# and planning routes can leave this file. Same objects under the old names.
_SUBSCRIBER_QUEUE_MAX = task_runtime.SUBSCRIBER_QUEUE_MAX
_publish = task_runtime.publish
_TODO_STATUS_MAP = task_runtime.TODO_STATUS_MAP
_todos_to_plan = task_runtime.todos_to_plan
_state_snapshot_for_frontend = task_runtime.state_snapshot_for_frontend
_apply_plan_fallback = task_runtime.apply_plan_fallback
_final_status = task_runtime.final_status
_claim_run_slot = task_runtime.claim_run_slot
_read_task_meta = task_runtime.read_task_meta
_MAX_BUDGET_TOPUP_USD = task_runtime.MAX_BUDGET_TOPUP_USD
_check_budget_topup = task_runtime.check_budget_topup
_apply_transition = task_runtime.apply_transition
_LIVE_LOG_MAX_ENTRIES = task_runtime.LIVE_LOG_MAX_ENTRIES
_LIVE_LOG_MAX_KEYS = task_runtime.LIVE_LOG_MAX_KEYS
_live_task_log = task_runtime.live_task_log
_flush_task_log_bg = task_runtime.flush_task_log_bg
_task_event_seq = task_runtime.task_event_seq
_live_planning_log = task_runtime.live_planning_log
_live_log_append = task_runtime.live_log_append
_fuller_log = task_runtime.fuller_log
_task_recorders = live_state.task_recorders   # see agent/live_state.py


# Moved to agent/tasks.py with task creation; the same object, so every
# call site here and every test that patches it still lands.
write_task_meta = tasks.write_task_meta


async def _stream_graph(task_id: str, repo: str, goal: str, budget_usd: float, graph_input, category: str | None = None,
                        route: str | None = None, route_reason: str | None = None) -> None:
    """Shared by a fresh task (graph_input = the initial state) and a resume
    (graph_input = None, meaning "continue from the last checkpoint" -- the
    standard LangGraph resume pattern). Everything after that point --
    streaming updates out over the WS, updating the Store, closing out
    status -- is identical either way.

    `category` is only ever passed explicitly on a fresh task (create_task
    classifies the goal once, up front). On a resume/approve call, it's left
    None here and recovered from the task's own already-stored meta below --
    a resume must never reclassify or lose the original category.
    """
    store = app.state.store
    graph = app.state.graph

    # audit H-19: read the stored meta ONCE up front and preserve the original
    # created_at across every terminal write below. Previously each write
    # hardcoded time.time(), so a resume reset the timestamp and list_tasks /
    # get_analytics (which sort and bucket by created_at) attributed a task to
    # its completion day instead of its start day.
    existing_meta = await store.aget(("tasks", repo), task_id)
    _existing_val = existing_meta.value if existing_meta else {}
    original_created_at = _existing_val.get("created_at")
    if category is None:
        category = _existing_val.get("category") or "other"
    if route is None:
        route = _existing_val.get("route") or "general"
        route_reason = _existing_val.get("route_reason")
    # metadata/tags -- standard RunnableConfig fields LangChain's tracer
    # automatically attaches to every run generated within this invocation,
    # so a trace is filterable to this specific task in the LangSmith UI
    # rather than only to the project as a whole.
    thread_config = {
        "configurable": {"thread_id": task_id},
        "metadata": {"task_id": task_id, "repo": repo},
        "tags": [repo],
    }
    # From here on every streamed entry is also written down durably.
    _start_task_recorder(task_id, repo)

    # cost_so_far here is Store-only display state -- hardcoding 0.0
    # unconditionally meant a resume briefly showed $0.00 in the
    # sidebar/stats for real work that already cost real money, until the
    # run finished and overwrote it with the true total. graph_input is
    # None specifically on a resume (see this function's own docstring), so
    # that's exactly when to carry the existing total forward instead. Note
    # the outer checkpoint's own cost_so_far is NOT always correct: it only
    # updates when a work pass fully returns, so a task cancelled mid-pass
    # has a stale value here until the CancelledError handler below patches
    # it back to the real total.
    starting_cost = 0.0
    if graph_input is None:
        existing = await graph.aget_state(thread_config)
        if existing and existing.values:
            starting_cost = existing.values.get("cost_so_far", 0.0)

    async def _mark(status: str) -> None:
        """Write this task's status and tell anyone watching."""
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
            status=status, created_at=original_created_at or time.time(),
            cost_so_far=starting_cost, route=route, route_reason=route_reason,
        )
        _publish(task_id, {"type": "status", "status": status})

    # "queued", not "running", while this project's task slots are all taken.
    #
    # How many tasks may run on a project at once is the runtime setting
    # parallel_tasks_per_project (default 10 since 2026-09-25; each task has
    # its own workspace, agent/workspaces.py). The status is written before
    # the slot is taken and "running" only once it is held, so a waiting task
    # never reads as a working one (2026-09-22: a queued task showed
    # "Running" with an empty log and no spend).
    await _mark("queued")

    # Declared here (not just inside the loop below) so the CancelledError
    # handler can always read the latest live-tracked cost, even if
    # cancellation lands before the astream loop yields anything.
    last_meta_cost = starting_cost

    try:
        # The DSN, not pg_dsn: it is what project_slot dispatches on, and on
        # this deployment it is the same Postgres string it always was. So
        # the slots are Postgres advisory locks rather than objects in this
        # process, and a second worker or an overlapping restart still cannot
        # run more tasks on a project than the setting allows (agent/graph.py).
        async with project_slot(repo, config.dsn, slots=runtime_settings.as_int("parallel_tasks_per_project"),
                                on_wait=lambda: _mark("queued")):
            # The project is ours now, so this is the first honest moment to
            # say "running". A task that never waited passes through both
            # marks in a millisecond and the dashboard only ever sees the
            # second one.
            await _mark("running")
            # stream_mode=["updates", "custom"] (not just "updates") -- the
            # graph's own "work" node is a single StateGraph node that
            # manually drives a whole inner deep-agent run inside itself
            # (see work.py), so a plain "updates" stream would yield exactly
            # one event for the entire work pass, arriving only once it's
            # fully done. work_node's own get_stream_writer() calls emit
            # "custom" events (todos/log_entry) as each inner model turn or
            # tool call actually happens, including subagent-delegated ones
            # (tagged "work:<subagent_name>") -- these are what provide real
            # live streaming here.
            #
            # durability="sync" is the right tradeoff for this outer graph
            # specifically: "async" (the library default) checkpoints in the
            # background while the next step runs, which means a crash
            # between a step completing and its checkpoint write landing
            # could lose that last step. work<->verify_and_ship transitions
            # happen at most every few seconds to minutes (bounded by real
            # LLM/subprocess work, not by this), so the extra confirm-write
            # latency is negligible -- unlike the deep agent's own inner
            # astream_events() call (work.py), which stays on the library
            # default deliberately, since that graph's steps are much
            # higher-frequency (many rapid LLM/tool turns per single outer
            # "work" pass) and durability there isn't independently
            # controllable per-node anyway.
            async for mode, payload in graph.astream(
                graph_input, thread_config, stream_mode=["updates", "custom"], durability="sync"
            ):
                if mode == "custom":
                    if payload.get("type") == "todos":
                        # Merged against what this task has already finished,
                        # not taken as the whole truth: write_todos replaces
                        # the list, and a model updating its plan often writes
                        # only what is LEFT. Unmerged, the step strip fell
                        # from 6/12 to 0/6 mid-task while 23 files of real
                        # work sat in the worktree, and the operator read that
                        # as a loop (2026-09-12). See agent/plan_progress.py.
                        merged = plan_progress.merge_todos(
                            (_todos_meta.value or {}).get("latest_todos")
                            if (_todos_meta := await _read_task_meta(store, repo, task_id)) else None,
                            payload.get("todos"),
                        )
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "plan": _todos_to_plan(merged),
                        })
                        # Mirror into the Store meta, same pattern (and same
                        # reason) as the live cost mirror below: the outer
                        # checkpoint's latest_todos is only written when a
                        # work pass RETURNS -- a pass can run 30+ minutes, and
                        # until then the plan existed only as this ephemeral
                        # event. A refresh or task-switch mid-pass rebuilt
                        # from the checkpoint and the step strip came back
                        # empty (reported live 2026-08-28). Display state
                        # only; nothing enforcement-related reads it.
                        try:
                            if _todos_meta:
                                await store.aput(("tasks", repo), task_id,
                                                 {**_todos_meta.value, "latest_todos": merged})
                        except Exception:  # noqa: BLE001 -- a display mirror must never break the stream
                            logger.exception("todos mirror write failed for %s", task_id)
                    elif payload.get("type") == "log_entry":
                        entry = payload["entry"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": entry["node"],
                            "execution_log": [entry],
                        })
                    elif payload.get("type") == "cost":
                        # Live mid-pass cost from work.py's tracker (see
                        # _consume_values there) -- without this, cost only
                        # updates on outer node boundaries, and a single work
                        # pass can run well past ten minutes showing $0.00 the
                        # whole time. Mirrored into the Store meta too,
                        # throttled to >= $0.02 moves, so the tasks sidebar
                        # (which reads meta, not the stream) tracks as well.
                        # Display-only either way -- budget enforcement reads
                        # the tracker/checkpoint, never this.
                        live_cost = payload["cost_so_far"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "cost_so_far": live_cost,
                        })
                        if live_cost - last_meta_cost >= 0.005:
                            meta_item = await store.aget(("tasks", repo), task_id)
                            if meta_item:
                                last_meta_cost = live_cost
                                await write_task_meta(store, repo, task_id, cost_so_far=live_cost)
                    continue

                # mode == "updates": one event per outer node ("work" or
                # "verify_and_ship") once its whole pass completes -- carries
                # the fields custom events don't (cost_so_far, escalated,
                # review_gate_result). Only forward keys the node's own
                # return dict actually included (not update.get(key, default))
                # -- verify_and_ship.py's own loop_back/no-diff/escalated-guard
                # paths each return a deliberately sparse dict (e.g. a
                # checks-failed loop-back never touches "escalated" at all),
                # and defaulting a missing key to False/None here would ship
                # a stale/wrong value over the wire instead of just omitting
                # the field (which the frontend already treats as "unchanged"
                # via its own `?? previousValue` merge in useTaskStream.ts).
                for node_name, update in payload.items():
                    if not isinstance(update, dict):
                        continue
                    node_payload: dict = {"type": "node_update", "node": node_name}
                    for key in (
                        "execution_log", "cost_so_far", "escalated", "escalation_reason",
                        "review_gate_result", "pending_approval", "committed_sha",
                    ):
                        if key in update:
                            node_payload[key] = update[key]
                    if "latest_todos" in update:
                        node_payload["plan"] = _todos_to_plan(update["latest_todos"])
                    _publish(task_id, node_payload)
                    # Keep the Store meta's display cost current per pass, not
                    # just at terminal transitions -- otherwise the tasks list
                    # (which reads meta, unlike the task view's
                    # checkpoint-backed hydrate) can show cost frozen at
                    # whatever the last terminal write recorded during a long
                    # multi-round run, wrongly suggesting the budget tracker
                    # had stalled. Enforcement never reads this -- the ceiling
                    # checks the checkpoint's own cost_so_far -- this is
                    # purely so the visible number tracks reality.
                    if "cost_so_far" in update:
                        last_meta_cost = update["cost_so_far"]
                        meta_item = await store.aget(("tasks", repo), task_id)
                        if meta_item:
                            await write_task_meta(store, repo, task_id, cost_so_far=update["cost_so_far"])

        final = await graph.aget_state(thread_config)
        values = final.values
        status = _final_status(values)
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=values.get("budget_usd", budget_usd),
            category=category, status=status, created_at=original_created_at or time.time(),
            cost_so_far=values.get("cost_so_far", 0.0),
            escalation_reason=values.get("escalation_reason"),
            # Where the work went, for a project that ships as a pull request.
            # On the task record rather than only in the step log, because a
            # PR is work waiting for a person and a link nobody can find is a
            # link nobody follows.
            pull_request_url=values.get("pull_request_url"),
        )
        _publish(task_id, {
            "type": "status",
            "status": status,
            "escalation_reason": values.get("escalation_reason"),
            "pending_approval": values.get("pending_approval"),
            "pull_request_url": values.get("pull_request_url"),
        })
        # Telegram: every rest state IS the actionable moment -- escalated,
        # waiting on an approval, waiting on the merge look, or done.
        _detail = None
        if status == "escalated":
            _detail = values.get("escalation_reason")
        elif status == "awaiting_approval":
            _pa = values.get("pending_approval") or {}
            _detail = _pa.get("description") if isinstance(_pa, dict) else str(_pa)
        elif status == "awaiting_merge":
            _pm = values.get("pending_merge_approval") or {}
            _sha = str(_pm.get("sha", ""))[:12] if isinstance(_pm, dict) else ""
            _detail = f"commit {_sha} passed review -- approve the merge in the dashboard"
        _alert_task_status(task_id, status, repo, goal, values.get("cost_so_far"), _detail)
        if status == "done":
            # Finished: its work is merged, or in a pull request, or there was
            # none. The branch keeps whatever it committed; the workspace --
            # hardlinks, copies and all -- is freed now, not at the next sweep.
            removed = await workspaces.remove(repo, task_id)
            if not removed.get("ok"):
                logger.warning("task %s: workspace not removed: %s", task_id, removed.get("reason"))
    except asyncio.CancelledError:
        # The operator's Stop button (/stop below) cancels this task
        # directly. CancelledError is a BaseException, not an Exception, so
        # it never reaches the broad handler below -- without this branch a
        # stopped task's Store status would stay stuck on "running" forever,
        # identical-looking to a genuinely orphaned task with no way to tell
        # the two apart.
        #
        # cost_now uses last_meta_cost, not a fresh checkpoint read: the
        # outer checkpoint's cost_so_far only updates when a work pass fully
        # returns, so cancelling mid-pass (the common case -- Stop almost
        # always interrupts an actively-running pass) leaves it stale at
        # whatever it was before this pass started. last_meta_cost is kept
        # current throughout the pass (both from work.py's live per-call
        # "cost" events and from each node's own completed-pass update), so
        # it reflects real spend right up to the moment of cancellation.
        # Also patched back into the checkpoint itself (not just the Store
        # meta) so a future resume's BudgetTracker starts counting from the
        # real total instead of silently under-billing against the budget
        # ceiling.
        cost_now = last_meta_cost
        # audit M-34: every write in this handler is best-effort. A raising or
        # slow store.aput must NOT replace the CancelledError -- doing so 500'd a
        # Stop that actually worked and left the UI unsure whether it took. We
        # always re-raise CancelledError at the end regardless of these writes.
        try:
            await graph.aupdate_state(thread_config, {"cost_so_far": cost_now})
        except Exception:
            pass
        try:
            # escalation_reason is no longer named here -- and no longer lost.
            # The merge preserves whatever the record already carried.
            await write_task_meta(
                store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
                status="stopped", created_at=original_created_at or time.time(),
                cost_so_far=cost_now,
            )
            _publish(task_id, {"type": "status", "status": "stopped"})
        except Exception:  # noqa: BLE001
            logger.exception("stopped-status write failed for task %s (cancellation still honored)", task_id)
        raise
    except Exception as e:  # noqa: BLE001 -- deliberately broad: any failure here must still flip status away from "running"
        # str(e) alone is close to useless for some exception types -- a bare
        # KeyError's str() is just the missing key's repr with zero context
        # on where it was raised. Full traceback to the log; the Store/UI
        # still only need the short message, that's what the user sees.
        logger.error("task %s failed: %s", task_id, str(e))
        logger.error(traceback.format_exc())
        # audit H-20 fixed this path by naming cost_so_far and escalation_reason
        # explicitly after a full overwrite had erased them. The merge now makes
        # that structural rather than remembered.
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
            status="error", created_at=original_created_at or time.time(),
            error=str(e), cost_so_far=last_meta_cost,
        )
        _publish(task_id, {"type": "status", "status": "error", "error": str(e)})
        _alert_task_status(task_id, "error", repo, goal, last_meta_cost, str(e)[:400])
    finally:
        _publish(task_id, {"type": "closed"})
        _running_tasks.pop(task_id, None)
        # Land whatever is still buffered. Awaited, not backgrounded: this is
        # the last moment the tail of the run exists anywhere.
        rec = _task_recorders.pop(task_id, None)
        if rec is not None:
            try:
                await rec.flush()
            except Exception:  # noqa: BLE001 -- never fail a task over its transcript
                logger.debug("task transcript tail not written for %s", task_id)


# agent/tasks.py starts a fresh task's run through this; it is reached on
# app.state rather than imported because tasks.py cannot import this module
# (it would be a cycle), exactly as the routers reach server state.
app.state.stream_graph = _stream_graph


# The task routes live in agent/routers/tasks.py (2026-09-23). Their endpoint
# functions, request models and helpers stay importable here under the old
# names -- the same objects -- for the call sites and tests that reach for them.
from agent.routers.tasks import (  # noqa: E402,F401 -- re-exported names
    AttachmentEntry, ApprovalRequest, CreateTaskRequest, MergeDecisionRequest, OperatorEditFile,
    OperatorEditRequest, ResumeTaskRequest, SendMessageRequest, _approval_summary, _readable_repos,
    approve_task, create_task, delete_task, get_task, get_task_diff, get_task_file, list_repos,
    list_tasks, merge_decision, resume_task, send_message, stop_task, stream_task,
    submit_operator_edits,
)


def _late(name: str):
    """A callable that looks `name` up in THIS module when it is called.

    What agent/routers/planning.py reaches on app.state. Registering the
    function object itself would freeze it at import: a test that patches
    `server._find_planning_meta` (or build_planning_agent, or the turn runner)
    would change what server.py sees and not what the route calls. Looking it
    up at call time keeps one answer to "which function is this".
    """
    def call(*args, **kwargs):
        return globals()[name](*args, **kwargs)
    call.__name__ = f"late_{name}"
    return call


async def _create_project_from_fields(fields: dict, user: User) -> dict:
    """The projects router's _create_project, bound to this app, for a caller
    that cannot import CreateProjectRequest -- the planning router's
    new-project decision. Looked up on the module at call time, so a test that
    patches agent.routers.projects._create_project is honoured here too."""
    return await projects_routes._create_project(app, projects_routes.CreateProjectRequest(**fields), user)


# The planning machinery the planning router reaches (see _late).
app.state.find_planning_meta = _late("_find_planning_meta")
app.state.run_planning_turn_bg = _late("_run_planning_turn_bg")
app.state.move_planning_session = _late("_move_planning_session")
app.state.build_planning_agent = _late("build_planning_agent")
app.state.create_project_from_fields = _late("_create_project_from_fields")

# The planning routes live in agent/routers/planning.py (2026-09-23); the same
# objects under their old names, for callers and tests that reach for them.
from agent.routers.planning import (  # noqa: E402,F401 -- re-exported names
    CreatePlanningSessionRequest, NewProjectDecisionRequest, PlanningMessageRequest,
    archive_planning_session, create_planning_session, decide_planning_new_project,
    delete_planning_session, get_planning_session, list_planning_sessions, send_planning_message,
    stop_planning_turn, stream_planning_session,
)


async def _resolve_task_repo(task_id: str) -> str | None:
    """The router's lookup, bound to this app; resolved at call time so a
    patch on agent.routers.tasks._resolve_task_repo is honoured here too."""
    return await tasks_routes._resolve_task_repo(app, task_id)


# ---------------------------------------------------------------------------
# Planning chat -- the turn runner and session-meta helpers. The routes
# themselves live in agent/routers/planning.py (2026-09-23) and reach these
# through app.state (see _late above).
#
# A planning session is a conversational research/design-consulting session
# (agent/planning_chat.py), distinct from a build task: no plan/execute/
# verify graph, no budget ceiling, no write/edit/bash access to the repo.
# Its own Store namespace ("planning", repo) holds lightweight session meta
# (repo, created_at, updated_at, title, plan_markdown); the full
# conversation itself lives in the same Postgres checkpointer tasks use,
# under thread_id f"planning:{session_id}" (see planning_thread_config).
# "Build Now" in the frontend does NOT call anything here -- it just calls
# the existing POST /api/tasks with the saved plan_markdown as the goal,
# reusing the real build system entirely as-is.
# ---------------------------------------------------------------------------


def _start_task_recorder(task_id: str, repo: str) -> None:
    """Created where the repo is actually known -- _publish only has a task id,
    and a transcript filed under the wrong project is worse than none."""
    store = getattr(app.state, "store", None)
    if store is None:
        return
    _task_recorders[task_id] = planning_log.Recorder(
        repo, task_id, store, namespace=planning_log.TASK_NAMESPACE,
        detail_cap=planning_log.TASK_DETAIL_CAP)


# Last Telegram-alerted (status, detail) per task, in-process: a resumed task
# re-enters _stream_graph and re-derives the same rest state; the operator
# needs ONE phone buzz per distinct stop, not one per stream cycle.
_last_task_alert: dict[str, tuple] = {}


def _notify_bg(text: str, repo: str | None = None, url: str = "/") -> None:
    """The one door alerts leave through: resolves the auth pool defensively
    so an alert can NEVER break the code path it decorates -- app.state has
    no auth_pool during unit tests and the earliest startup moments, and
    accessing a missing State attribute raises.

    audit H1: `repo` scopes the fan-out. Alert bodies carry the repo name, a
    goal excerpt and up to 1500 characters of failure detail, so a recipient
    restricted to one project must not receive another's. repo=None means an
    infrastructure alert (a service restart) and goes to admins only.
    """
    pool = getattr(app.state, "auth_pool", None)
    if pool is None:
        return
    notify_operators_bg(pool, text, repo, url)


def _alert_task_status(task_id: str, status: str, repo: str, goal: str, cost: float | None, detail: str | None) -> None:
    """Telegram alert for a task's rest state -- deduped, best-effort."""
    if status in ("running", "queued", "stopped"):
        # running is noise; queued is the same noise arriving earlier (it is
        # a normal step on the way to running, not an event); stopped is the
        # operator's own Stop button -- alerting someone about the button
        # they just pressed is spam.
        return
    key = (status, str(detail or "")[:120])
    if _last_task_alert.get(task_id) == key:
        return
    _last_task_alert[task_id] = key
    _notify_bg(task_alert(status, repo, goal, cost, detail), repo=repo, url=task_url(task_id))


# A planning turn is bounded by SILENCE, not by duration. Every log entry and
# cost event the turn emits is a heartbeat; the watchdog below fires only when
# those stop for planning_stall_timeout_s (a runtime setting, changed without
# a restart). Duration is unbounded on purpose -- the BUDGET is the ceiling
# that stops work. A flat 30-minute ceiling killed a live turn on 2026-08-30
# while it was still streaming: $2.50 spent, no plan saved.
_STALL_POLL_S = 15.0


class PlanningStalled(Exception):
    """No output from a planning turn for the configured stall window."""


async def _await_with_stall_watchdog(task: "asyncio.Task", heartbeat: dict, stall_s: float):
    """Await `task`, cancelling it only if `heartbeat['at']` stops advancing.

    Raises PlanningStalled -- deliberately NOT CancelledError, so the caller's
    cancellation handler keeps meaning "the operator pressed Stop" and this
    lands in the error path that banks cost and the partial draft.
    """
    # Poll faster than the window rather than on a fixed tick: the interval
    # is the worst-case delay between going silent and noticing, so it should
    # scale with the window instead of being a constant that happens to suit
    # one value of it.
    poll = max(0.05, min(_STALL_POLL_S, stall_s / 4))
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll)
        if done:
            return task.result()
        idle = time.monotonic() - heartbeat["at"]
        if idle >= stall_s:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise PlanningStalled(
                f"no output for {int(idle // 60)}m — the turn produced "
                f"{heartbeat['events']} events, then went silent"
            )


# Live planning spend is mirrored into the Store as it accrues, the same way a
# build task's is. Without it the session row holds cost_usd from the LAST
# completed turn until this one banks, so any hydrate mid-turn -- a page
# reload, a reconnect -- overwrote the live figure on screen with a stale 0.00
# and left it there. Reported live 2026-08-31 on a turn that went on to spend
# $8.11 while the dashboard read $0 the whole way.
_PLANNING_COST_MIRROR_MIN_DELTA = 0.005


async def _mirror_planning_cost(store, repo: str, session_id: str, cost: float) -> None:
    """Display only. Budget enforcement reads the tracker, never this.
    Under the session's meta lock (live_state.planning_meta_lock) so it cannot
    land between the cost carry's read and write."""
    try:
        async with live_state.planning_meta_lock(session_id):  # the carry (agent/tasks.py) writes the same row
            item = await store.aget(("planning", repo), session_id)
            if item:
                await store.aput(("planning", repo), session_id, {**item.value, "cost_usd": cost})
    except Exception:  # noqa: BLE001 -- a display mirror must never break the turn
        logger.exception("could not mirror planning cost for %s", session_id)


# One per session being streamed. The live buffer above dies with the process;
# this survives it, which is the whole point (agent/planning_log.py).
_planning_recorders = live_state.planning_recorders   # see agent/live_state.py


def _publish_planning(session_id: str, event: dict) -> None:
    # Same contract as task_runtime.publish: every entry carries a
    # content-derived id and every content event a per-session seq, so the
    # browser can open its socket before hydrating and merge the two without
    # losing or doubling a line (agent/log_stream.py).
    if event.get("type") == "log_entry" and event.get("entry"):
        event["entry"] = log_stream.stamp([event["entry"]])[0]
        _live_log_append(_live_planning_log, session_id, [event["entry"]],
                         on_evict=task_runtime.planning_event_seq.forget)
        recorder = _planning_recorders.get(session_id)
        if recorder is not None and recorder.add(event["entry"]):
            # Batched: a busy turn publishes several entries a second, and a
            # store write per entry would be write amplification for
            # telemetry. Backgrounded so the stream never waits on it.
            _spawn_background(recorder.flush(), f"planning_log_flush:{session_id}")
    if event.get("type") != "ping":
        event["seq"] = task_runtime.planning_event_seq.next(session_id)
    for q, _ws in _planning_subscribers.get(session_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("dropping event for a stalled planning %s subscriber", session_id)  # audit M-34


async def _find_planning_meta(session_id: str):
    """Session meta is stored per-repo (("planning", repo)), but the id
    routes here carry no repo -- cheap enough to check the handful of
    configured repos rather than also threading repo through every URL."""
    for repo in PROJECTS:
        item = await app.state.store.aget(("planning", repo), session_id)
        if item:
            return repo, item.value
    return None, None


async def _bank_planning_turn(
    session_id: str,
    repo: str,
    plan_markdown: str | None,
    spent: float | None,
    text: str | None = None,
    outcome: str | None = None,
    outcome_detail: str | None = None,
    brief: dict | None = None,
    new_project: dict | None = None,
) -> None:
    """Persist whatever a turn earned before it ended -- used by the two
    abnormal exits (operator Stop, and an exception), which both used to
    throw work away that the session had genuinely already paid for.

    A crashed turn still HAS a plan if the model called save_plan before the
    crash: run_planning_turn returns plan_ref["markdown"], and a crash means
    it never returned, so that draft lives nowhere else. The old error path
    didn't look at it, so the operator saw a red error AND lost the plan the
    model had just written. The cancel path had the same hole.

    Same PRESERVE-never-clobber rule as the success path: a None plan leaves
    the stored one alone (a turn can add or replace a plan, never remove
    one), and cost is banked because the spend is real either way.

    `outcome` is WHY the turn ended, persisted rather than only streamed: an
    operator who was not watching that exact second, or who refreshed, was
    otherwise left with a stream that simply stopped. One of:

        completed  the turn finished and returned a plan
        stopped    the operator pressed Stop
        stalled    no output for the configured stall window
        budget     the per-turn dollar ceiling was reached
        error      anything else, with the message in outcome_detail

    `text` seeds a missing title the same way the success path does. Titles
    used to be set only on success, so a session whose FIRST turn failed sat
    in the sidebar as a permanent None -- both timed-out sessions on
    2026-08-27 did exactly that. No classify_task call here (the success path
    does one for the category): teardown after a failure is the wrong moment
    for another model call, and a title alone is what the sidebar needs.
    """
    try:
        item = await app.state.store.aget(("planning", repo), session_id)
        if not item:
            return
        meta = {**item.value, "updated_at": time.time(), "turn_active": False}
        if plan_markdown is not None:
            meta["plan_markdown"] = plan_markdown
        if spent is not None:
            meta["cost_usd"] = spent
        if brief is not None:
            meta["brief"] = brief  # same PRESERVE rule: a turn can add or replace a brief, never remove one
        if new_project is not None:
            meta["new_project"] = new_project  # a proposal made before the crash is still the operator's to answer
        if text and not meta.get("title"):
            meta["title"] = text[:60]
        if outcome is not None:
            meta["last_outcome"] = outcome
            meta["last_outcome_detail"] = outcome_detail
            meta["last_outcome_at"] = time.time()
        await app.state.store.aput(("planning", repo), session_id, meta)
    except Exception:  # noqa: BLE001 -- teardown must never raise over the failure it is cleaning up after
        logger.exception("failed to persist planning progress for session %s", session_id)
    # However the turn ended -- completed, stopped, stalled, out of budget or
    # crashed -- its last entries belong on disk. This is the one path every
    # ending goes through, which is why the flush lives here rather than in
    # each of them.
    recorder = _planning_recorders.pop(session_id, None)
    if recorder is not None:
        await recorder.flush()


async def _run_planning_turn_bg(session_id: str, repo: str, text: str, attachments: list[dict] | None = None,
                                allowed_repos: list[str] | None = None, is_admin: bool = False,
                                actor: str | None = None) -> None:
    try:
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        starting_cost = meta_item.value.get("cost_usd", 0.0) if meta_item else 0.0
        # Classified fresh per turn -- a conversation can drift from easy
        # design chat into a real bug report mid-session, and the model
        # should follow that. Classified on the clean, operator-typed text
        # -- not the attachments note appended below, same reasoning as
        # create_task's own goal/attachments split.
        #
        # STICKY UPWARD (2026-08-28): escalation is per-turn, de-escalation
        # never happens within a session. A HARD session's continuation
        # nudges are short by nature ("continue", "also check X") and
        # classify EASY on their text alone -- which flipped a session's
        # model mid-plan: half a plan was written by the HARD pin and
        # half by the EASY pin after a nudge (operator report; a restart
        # exposed it, but any short follow-up triggers the same flip). Once
        # a session has needed the hard model, its context IS the hard
        # problem -- every later turn reasons over that same context, so the
        # floor ratchets up and stays. A fresh session starts the ladder
        # over.
        difficulty = await classify_planning_difficulty(text, config)
        _prior_difficulty = (meta_item.value.get("difficulty") if meta_item else None) or "EASY"
        if _prior_difficulty == "HARD":
            difficulty = "HARD"
        # Frontend route (agent/frontend_route.py): the operator's override on
        # the session wins; otherwise decided from this message and, like
        # difficulty, sticky once a session has gone frontend -- the whole
        # context is frontend work from then on.
        #
        # The session's own category is passed in once it has one. It is the
        # strongest signal the router has (a model read the whole request),
        # and it did not reach this call at all: the route was decided on the
        # first message's keywords and then never revisited, so the storefront
        # HDR-lighting session on 2026-09-15 settled into `ui-styling` and
        # kept planning on the general seat for every turn after. A session
        # has no category until it first saves a plan (see the categorising
        # block below and its comment on why that is deliberate), so this is
        # None on turn one and real from then on -- the keywords still have to
        # carry the first turn.
        _meta_val = meta_item.value if meta_item else {}
        _route_decision = classify_frontend(text, _meta_val.get("category"), _meta_val.get("route_override"))
        if _meta_val.get("route") == "frontend" and not _meta_val.get("route_override"):
            _route_decision = RouteDecision("frontend", _meta_val.get("route_reason") or "earlier turn")
        route = _route_decision.route
        # turn_active/turn_started_at make an in-flight planning turn
        # STORE-VISIBLE, like a running task. Deploy tooling used to infer
        # idleness from tasks + router quiet, and a >90s gap inside one long
        # model call read as idle -- a restart landed mid-plan (the incident
        # that also exposed the difficulty flip above). Cleared on every
        # ending path; the timestamp lets a reader judge a marker that a
        # hard-killed process could not clear. There is no longer a turn
        # ceiling to compare against (see PLANNING_STALL_TIMEOUT_S) -- a turn
        # is bounded by silence and by budget, not by elapsed time, so treat
        # a marker as stale on the same stall basis rather than a fixed age.
        if meta_item:
            await app.state.store.aput(("planning", repo), session_id, {
                **meta_item.value, "difficulty": difficulty,
                "route": route, "route_reason": _route_decision.reason,
                "turn_active": True, "turn_started_at": time.time(),
            })
        # Seed the turn with the draft this session already has. The agent (and
        # its plan_ref) is rebuilt per turn, so without this the model cannot see
        # its own previous plan and every turn starts from a blank one.
        _prior = await app.state.store.aget(("planning", repo), session_id)
        _prior_plan = _prior.value.get("plan_markdown") if _prior else None
        # The brief too: written by save_brief on an earlier turn, it is what
        # keeps a follow-up message from forcing a fresh brief-first round.
        _prior_brief = _prior.value.get("brief") if _prior else None
        agent, plan_ref, tracker = await build_planning_agent(
            config, repo, app.state.checkpointer, app.state.store,
            starting_cost=starting_cost, difficulty=difficulty,
            existing_plan=_prior_plan,
            allowed_repos=allowed_repos,  # audit H-2
            existing_brief=_prior_brief,
            route=route,
            is_admin=is_admin,  # gates create_project; not derivable from allowed_repos
            actor=actor,
            session_id=session_id,  # joins this seat's retrieval events to the conversation
        )
        thread_config = planning_thread_config(session_id, repo)
        # Circuit breaker: llm_for_role's own per-call timeout (plus
        # ChatOpenAI's default retries) can still leave a turn hanging for
        # 10+ minutes with zero log_entry published and no exception ever
        # raised if the underlying call genuinely never returns (confirmed
        # live 2026-08-23 -- a planning turn sat with no checkpoint, no log
        # line beyond entering astream_events, and no error surfaced for
        # over 10 minutes). Without this, that reads to the operator as "the
        # agent is stuck" with nothing to even look at. agent-planning-chat's
        # own per-call timeout is 450s (reasoning_effort="high" genuinely
        # needs that long -- see llm_for_role), and a real turn can involve
        # several tool-calling round trips, so this outer ceiling has to
        # clear several such calls comfortably; it exists purely so the
        # except Exception below always fires eventually instead of never.
        message_text = text + _attachments_note(attachments) if attachments else text
        opening = log_stream.stamp([{
            "kind": "user", "summary": text[:200], "detail": text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }])[0]
        _live_log_append(_live_planning_log, session_id, [opening],
                         on_evict=task_runtime.planning_event_seq.forget)
        # The durable transcript starts here, with the operator's own message:
        # a turn read back later is unintelligible without the thing that
        # started it.
        recorder = _planning_recorders.setdefault(
            session_id, planning_log.Recorder(repo, session_id, app.state.store))
        recorder.add(opening)
        # Every published event doubles as the watchdog's heartbeat.
        _heartbeat = {"at": time.monotonic(), "events": 0}
        # Live cost is mirrored into the Store as it accrues -- see
        # _PLANNING_COST_MIRROR_MIN_DELTA for why.
        _mirror = {"cost": starting_cost}
        _mirror_tasks: set[asyncio.Task] = set()

        def _publish_and_beat(ev):
            _heartbeat["at"] = time.monotonic()
            _heartbeat["events"] += 1
            # cost events arrive pre-shaped ({"type": "cost", ...}); log
            # entries need wrapping -- route on the shape.
            is_cost = isinstance(ev, dict) and ev.get("type") == "cost"
            if isinstance(ev, dict) and ev.get("type") == "ping":
                # A heartbeat from the turn (nothing worth showing this tick).
                # The beat above is the point; forward it as a bare ping,
                # which the client already ignores, and never as a log entry
                # -- wrapped, it reached the page as an entry with no text
                # and the error boundary took the whole view down (2026-09-09).
                _publish_planning(session_id, {"type": "ping"})
                return
            _publish_planning(
                session_id,
                ev if is_cost else {"type": "log_entry", "entry": ev},
            )
            if not is_cost or not isinstance(ev.get("cost_usd"), (int, float)):
                return
            cost = float(ev["cost_usd"])
            # Throttled HERE rather than inside the coroutine: the check is
            # synchronous, so two cost events in the same tick cannot both
            # decide to write.
            if cost - _mirror["cost"] < _PLANNING_COST_MIRROR_MIN_DELTA:
                return
            _mirror["cost"] = cost
            # Fire-and-forget: a store write must never block the stream, and
            # a lost mirror costs freshness, not correctness -- the
            # authoritative figure is still banked when the turn ends. The
            # strong reference is required; asyncio holds only a weak one, so
            # a bare create_task can be collected mid-flight.
            _t = asyncio.create_task(
                _mirror_planning_cost(app.state.store, repo, session_id, cost)
            )
            _mirror_tasks.add(_t)
            _t.add_done_callback(_mirror_tasks.discard)

        _turn_task = asyncio.create_task(
            run_planning_turn(
                agent, plan_ref, thread_config, message_text,
                _publish_and_beat,
                tracker=tracker,
            )
        )
        plan_markdown = await _await_with_stall_watchdog(
            _turn_task, _heartbeat, runtime_settings.value("planning_stall_timeout_s")
        )
        # PRESERVE, never clobber. `plan_markdown` is only what THIS turn's
        # save_plan call produced, and the system prompt deliberately does not
        # ask the model to re-save every turn ("call this once the plan is
        # genuinely ready, and again any time it meaningfully changes").
        # Writing it unconditionally meant any later turn that merely discussed
        # something erased a plan the session had already earned -- the operator
        # sees several good rounds and then no plan to send to Build, with no
        # error anywhere. A turn can add or replace a plan, never remove one.
        effective_plan = plan_markdown if plan_markdown is not None else _prior_plan
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        # A create_project proposal rides the same shared plan_ref as the plan
        # and the brief: the tool can only return text to the model, so this
        # is the one way the server learns of it. Same PRESERVE rule -- a turn
        # that proposed nothing keeps the proposal the operator has not yet
        # answered (only the confirm/dismiss route clears it).
        new_project = plan_ref.get("new_project") or (meta_item.value.get("new_project") if meta_item else None)
        if meta_item:
            meta = {
                **meta_item.value, "updated_at": time.time(),
                "plan_markdown": effective_plan, "cost_usd": tracker.total_cost,
                "turn_active": False,
                "brief": plan_ref.get("brief") or meta_item.value.get("brief"),
                "new_project": new_project,
            }
            if not meta.get("title"):
                meta["title"] = text[:60]
            # Categorised when the session first has a SAVED PLAN, not on its
            # first message.
            #
            # Categorising early made the sidebar move the session mid-read:
            # the operator sent a message, the reply came back ending in a
            # question, and at that same moment the session jumped out of the
            # ungrouped list into a category group -- landing next to an older
            # session on the same subject that already had a plan. The screen
            # flicked, the question went unread, and the neighbouring "Build
            # now" button read as "my plan is ready" (operator report,
            # 2026-08-29). Nothing was actually mis-saved; the movement itself
            # was the misinformation.
            #
            # Tying the move to the plan makes it mean something: a session
            # settles into a category exactly when it becomes buildable, so
            # movement in the sidebar is a signal rather than noise. It also
            # stops paying the classifier for sessions that never produce a
            # plan. Same fixed taxonomy as a build task (agent/classify.py).
            if effective_plan and not meta.get("category"):
                classification = await classify_task(text, config)
                meta["category"] = classification.category
            await app.state.store.aput(("planning", repo), session_id, meta)
        await _bank_planning_turn(
            session_id, repo, None, None, outcome="completed"
        )
        _publish_planning(session_id, {
            "type": "turn_complete", "plan_markdown": effective_plan, "cost_usd": tracker.total_cost,
            "new_project": new_project,
        })
    except asyncio.CancelledError:
        # Operator pressed Stop, or the process is shutting down. The spend is
        # real either way, so bank it against the session rather than losing it,
        # and keep whatever plan the session already had -- a stopped turn must
        # never be the thing that erases a plan.
        logger.info("planning turn cancelled for session %s", session_id)
        # `tracker` is created partway through the try, so a cancel that lands
        # early leaves it unbound -- fall back to the cost the session already
        # had rather than raising NameError out of a cancellation handler.
        _t = locals().get("tracker")
        spent = _t.total_cost if _t is not None else locals().get("starting_cost", 0.0)
        # `plan_ref` is bound partway through the try, same as `tracker` -- a
        # cancel landing before build_planning_agent leaves both unbound.
        _ref = locals().get("plan_ref") or {}
        await _bank_planning_turn(
            session_id, repo, _ref.get("markdown"), spent, text=text, outcome="stopped",
            brief=_ref.get("brief"), new_project=_ref.get("new_project"),
        )
        _publish_planning(session_id, {"type": "stopped", "cost_usd": spent})
        raise
    except Exception as e:  # noqa: BLE001 -- must surface to the client, never die silently in the background
        logger.exception("planning turn failed for session %s", session_id)
        _t_alert = locals().get("tracker")
        _notify_bg(task_alert(
            "planning_error", repo, text, _t_alert.total_cost if _t_alert is not None else None, str(e)[:400]),
            repo=repo, url=planning_url(session_id))
        # The spend up to the failure is just as real as a cancelled turn's,
        # and this path banked NEITHER it nor the draft -- a turn that crashed
        # after save_plan lost the plan and under-reported the session's cost.
        _t = locals().get("tracker")
        _ref = locals().get("plan_ref") or {}
        # Distinguish the three ways a turn can end badly. They mean very
        # different things to an operator -- "the ceiling you set was reached"
        # is not a fault, "it went silent" is, and "it raised" is a third
        # thing again -- and lumping them under one red banner is what made
        # the last stall unreadable without opening Telegram.
        if isinstance(e, PlanningStalled):
            _outcome = "stalled"
        elif isinstance(e, BudgetExceededError):
            _outcome = "budget"
        else:
            _outcome = "error"
        await _bank_planning_turn(
            session_id, repo, _ref.get("markdown"),
            _t.total_cost if _t is not None else None,
            text=text, outcome=_outcome, outcome_detail=str(e)[:500],
            brief=_ref.get("brief"), new_project=_ref.get("new_project"),
        )
        _publish_planning(
            session_id,
            {"type": "error", "outcome": _outcome, "message": str(e)},
        )
    finally:
        _publish_planning(session_id, {"type": "closed"})
        _running_planning_turns.pop(session_id, None)


async def _move_planning_session(session_id: str, old_repo: str, new_repo: str, meta: dict) -> dict:
    """Re-home a session's two Store rows -- meta at ("planning", repo) and
    the durable transcript at ("planning_log", repo) -- under the new repo.
    The checkpoint thread id is f"planning:{session_id}" with no repo in it
    (planning_thread_config), so the conversation itself needs nothing.

    Copy first, delete after: a crash between the two leaves a duplicate the
    next confirm can overwrite, never a session with no row anywhere.
    """
    store = app.state.store
    new_meta = {**meta, "repo": new_repo, "new_project": None, "updated_at": time.time()}
    await store.aput(("planning", new_repo), session_id, new_meta)
    log_item = await store.aget((planning_log.NAMESPACE, old_repo), session_id)
    if log_item and log_item.value:
        await store.aput((planning_log.NAMESPACE, new_repo), session_id, log_item.value)
    await store.adelete(("planning", old_repo), session_id)
    await planning_log.forget(store, old_repo, session_id)
    return new_meta


async def _start_task(goal: str, repo: str, budget_usd: float | None, route: str, **kwargs) -> dict:
    """agent/tasks.py's start_task, bound to this app. Kept as a name here so
    the GitHub inbox and Build Now call sites -- and the tests that stand in
    for task creation -- are unchanged by the move."""
    return await tasks.start_task(app, goal, repo, budget_usd, route, **kwargs)


# The router credit balance, behind this app's own auth. The frontend used
# to call the commit-reviewer's dashboard API for it, and a deployment may
# put that service behind a separate reverse-proxy auth this app's users
# have no session for (this one did): the fetch 401'd, BalanceStrip.tsx
# swallows any fetch failure, and the balance just vanished from the sidebar
# (2026-08-24). It then went through this app as a proxy to the review
# service, which reads the key from a file only a host install has, so the
# bundle's card was empty while the key worked fine (2026-09-28). Now the
# route asks OpenRouter itself with the key the router bills.


@app.get("/api/router-balance")
async def get_router_balance(user: User = Depends(require_full_auth)):
    """Remaining OpenRouter credit, for the Analytics page's balance card.

    Admin-only: it is the operator's spend and remaining credit. Asked of
    OpenRouter directly with this deployment's own key -- the one the router
    bills -- and cached for a minute. It used to be proxied through the
    review service, which reads the key from a file only a host install
    has, so the bundle's card showed nothing while the key worked fine
    (2026-09-28, the first Windows install)."""
    auth.require_admin(user)
    now = time.monotonic()
    cached = _balance_cache.get("data")
    if cached is not None and now - _balance_cache["at"] < _BALANCE_CACHE_S:
        return cached
    from agent.model_config import _openrouter_key  # noqa: PLC0415
    key = _openrouter_key()
    if not key:
        # Read on every call, so no restart is involved: the next request
        # after the key is saved (Settings -> Environment) succeeds.
        raise HTTPException(503, "no OpenRouter key: set OPENROUTER_API_KEY in the router's .env")
    # An upstream that is down, slow or answering nonsense is a 502 with a
    # fixed sentence, not a 500 with a traceback in the log (2026-09-29).
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"could not reach OpenRouter for the credits request: {type(e).__name__}")
    if resp.status_code != 200:
        raise HTTPException(502, f"OpenRouter answered {resp.status_code} to the credits request")
    try:
        body = resp.json().get("data") or {}
        total, used = float(body.get("total_credits") or 0), float(body.get("total_usage") or 0)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(502, "OpenRouter's credits reply was not the expected JSON")
    data = {"totalCredits": total, "totalUsage": used, "remaining": total - used}
    _balance_cache.update(data=data, at=now)
    return data


_BALANCE_CACHE_S = 60
_balance_cache: dict = {"data": None, "at": 0.0}


class GitHubReposRequest(BaseModel):
    name: str | None = None      # a stored token, by label
    token: str | None = None     # or a pasted one, before saving


@app.post("/api/github/repos")
async def github_repos_endpoint(req: GitHubReposRequest, user: User = Depends(require_full_auth)):
    """Every repository a token can reach, and which are already onboarded.

    The token carries its own grant, so this is the honest answer to "which
    repositories are available" -- and it goes stale when the grant changes in
    GitHub rather than when somebody remembers to update something here.

    `onboarded` is matched on the remote each existing project actually has,
    not on how it was added. A project the operator typed a path for is just
    as likely to be on GitHub as one the agent cloned, and calling those
    different kinds of project is a distinction only this codebase can see.
    """
    auth.require_admin(user)
    from agent import github_repos as gh_repos, github_settings  # noqa: PLC0415

    raw = (req.token or "").strip()
    if not raw and req.name:
        settings = await github_settings.load(app.state.store)
        entry = settings["tokens"].get(req.name)
        if not entry:
            raise HTTPException(404, f"no token named {req.name!r}")
        raw = github_settings.decrypt_token(config, entry["enc"])
    if not raw:
        raise HTTPException(400, "give a stored token's name, or a token to try")

    try:
        repos = await gh_repos.list_accessible(raw)
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"could not reach GitHub: {type(e).__name__}")

    # What each configured project's checkout actually points at.
    known = await asyncio.to_thread(projects_routes._project_remote_slugs)

    # A checkout made before the repository was renamed or transferred still
    # has the old path in its origin, and GitHub keeps serving that by
    # redirect -- the remote works, the slug matches nothing below, and the
    # row offers to clone a project that is already here. Ask what each
    # unmatched one is called now. Bounded by the projects the token's own
    # list did not already account for, which is normally none.
    listed = {r["slug"].lower() for r in repos}
    for stale in [s for s in known if s not in listed]:
        current = await gh_repos.resolve_slug(raw, stale)
        if current and current.lower() != stale:
            known.setdefault(current.lower(), known[stale])

    for r in repos:
        r["onboarded_as"] = known.get(r["slug"].lower())
    return {"repos": repos, "onboarded": sorted(set(known.values()))}


def _tail_lines(path: Path, count: int, block: int = 64 * 1024) -> str:
    """Last `count` lines without reading the whole file.

    The consolidation log is appended to on every cron run and never rotated,
    so a full read grows without bound -- and it happened on the event loop.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= count:
                step = min(block, end)
                end -= step
                fh.seek(end)
                data = fh.read(step) + data
        return "\n".join(data.decode(errors="replace").splitlines()[-count:])
    except OSError as e:
        return f"(could not read {path}: {e})"


@app.get("/api/consolidation/status")
async def consolidation_status(user: User = Depends(require_full_auth)):
    """Last nightly memory-consolidation run: when, and whether it succeeded.

    Exists because a failed run used to be indistinguishable from a healthy one
    — the script printed a line and exited 0, so cron stayed quiet and a provider
    incompatibility skipped consolidation unnoticed for months. The marker file
    is written on every run, by the agent's own scheduler (agent/jobs.py) or
    by scripts/consolidation-cron.sh on a host that still runs it.
    """
    auth.require_admin(user)
    from agent import jobs
    marker = jobs.marker_path(jobs.JOBS["consolidation"])
    log = paths.DATA_DIR / "consolidation.log"

    # stale defaults False, not True: a default of True combined with a silent
    # except-pass meant any error here rendered a healthy run as STALE. That is
    # the same silent-failure shape this panel exists to catch, so failures to
    # parse are reported as `stale_error` rather than swallowed into a verdict.
    payload: dict = {"ran_at": None, "ok": None, "exit_code": None, "stale": False, "tail": ""}
    try:
        payload.update(json.loads(await asyncio.to_thread(marker.read_text)))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("consolidation status: marker unreadable: %s", e)
        payload["marker_error"] = "marker file unreadable -- see the server log"

    # Stale = no run in over 48h. The job is nightly, so one missed night is
    # worth surfacing rather than waiting for someone to read a log.
    if payload.get("ran_at"):
        try:
            ran = _dt.fromisoformat(str(payload["ran_at"]).replace("Z", "+00:00"))
            age_h = (_dt.now(UTC) - ran).total_seconds() / 3600
            payload["age_hours"] = round(age_h, 1)
            payload["stale"] = age_h > 48
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidation status: could not parse ran_at: %s", e)
            payload["stale_error"] = "could not parse ran_at -- see the server log"

    try:
        # to_thread, and only the tail: this log is never rotated, so
        # read_text() pulled the WHOLE file into memory on the event loop --
        # every other request stalled behind it, and the cost grew with the
        # file. _tail_lines seeks from the end instead.
        payload["tail"] = await asyncio.to_thread(_tail_lines, log, 40)
    except Exception:
        pass
    # What the scheduler knows: when it is next due, whether it is running
    # now, and whether a due run is waiting for the agent to go quiet.
    sched = jobs.status("consolidation")
    payload.update({k: sched[k] for k in ("due_at", "due", "running", "waiting", "trigger", "error")})
    return payload


# Static frontend, mounted last so it never shadows an /api/* route above.
# A single-page app with one real route today, but the catch-all fallback
# means adding client-side routes later won't need a matching nginx change.
#
# Cache headers are the whole reason this isn't just a bare StaticFiles
# mount. Nothing here sent any Cache-Control at all, which does NOT mean
# "don't cache" -- with only a Last-Modified to go on, browsers fall back to
# heuristic caching and are free to reuse index.html for a while. index.html
# is the file that names the content-hashed bundle, so a stale copy of it
# pins the browser to the PREVIOUS deploy's JS/CSS: new code ships, the
# server serves it correctly, and the operator still sees the old UI until
# they happen to hard-reload. Confirmed live 2026-08-23 (this exact bug: a
# rebuilt chat composer was being served and simply never appeared).
#
# The fix is the standard split for hashed-asset SPAs:
#   * index.html      -> no-store. Never reused; every load re-reads which
#                        bundle is current. It's ~400 bytes, so revalidating
#                        it on each load costs nothing.
#   * /assets/*       -> immutable, one year. Safe precisely BECAUSE the
#                        filename contains a content hash -- different
#                        content is always a different URL, so a cached copy
#                        can never be stale. This is what keeps the split
#                        cheap: the tiny file is always fetched, the 690KB
#                        one is cached hard.
class _ImmutableAssets(StaticFiles):
    """StaticFiles that marks content-hashed bundles permanently cacheable."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def _not_modified(request: Request, response: FileResponse) -> bool:
    """Answer a conditional request the way StaticFiles does.

    FileResponse sets etag and last-modified but never checks the request
    against them, so `no-cache` below would mean re-sending the whole file on
    every load rather than the 304 the browser is asking for. ETag wins over
    Last-Modified when both are present, per RFC 9110.
    """
    etag = response.headers.get("etag")
    if_none_match = request.headers.get("if-none-match")
    if etag and if_none_match:
        candidate = etag.strip('"')
        for token in if_none_match.split(","):
            token = token.strip()
            if token == "*":
                return True
            # W/ marks a weak validator; the tag itself is what compares.
            if token.removeprefix("W/").strip('"') == candidate:
                return True
        return False

    modified_since = request.headers.get("if-modified-since")
    last_modified = response.headers.get("last-modified")
    if modified_since and last_modified:
        try:
            return _parsedate(last_modified) <= _parsedate(modified_since)
        except (TypeError, ValueError):
            return False
    return False


if FRONTEND_DIST.is_dir():
    app.mount("/assets", _ImmutableAssets(directory=FRONTEND_DIST / "assets"), name="assets")

    # HEAD as well as GET: FastAPI's @app.get registers exactly the methods
    # named, unlike Starlette's plain Route, which folds HEAD in for free. A
    # bare @app.get therefore 405s every HEAD -- including the one a link-
    # preview crawler sends to size an og:image before fetching it.
    dist_root = os.path.normpath(str(FRONTEND_DIST.resolve()))

    def _dist_root_file(full_path: str) -> Path | None:
        """A real file directly inside dist/, or None. One path segment only
        (a favicon, the apple-touch icon, og-preview.png): anything with a
        separator or a dot-segment is not a root file, and the normalised
        path must still sit under dist after joining -- the check static
        analysers look for (CodeQL py/path-injection), on top of the
        is_relative_to containment."""
        if not full_path or "/" in full_path or "\\" in full_path or full_path in (".", ".."):
            return None
        joined = os.path.normpath(os.path.join(dist_root, full_path))
        if not joined.startswith(dist_root + os.sep):
            return None
        candidate = Path(joined)
        if not candidate.is_file() or candidate.parent != Path(dist_root):
            return None
        return candidate

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
    async def spa_fallback(full_path: str, request: Request):
        # Real files at the dist root — favicons, the apple-touch icon — are
        # served as themselves. Before this, ONLY /assets was mounted and every
        # other path fell through to index.html, so the favicon <link>s fetched
        # 200 text/html and the tab never showed an icon (silently: a 200 with
        # the wrong body looks fine in every log). Resolved-and-contained check
        # rather than trusting the path: `..` segments must not escape dist.
        candidate = _dist_root_file(full_path)
        if candidate is not None:
            # Stable names, so freshness has to come from revalidation
            # rather than a lifetime: FileResponse already sends etag and
            # last-modified, and a max-age here would be exactly how long a
            # replaced icon or og:image outlives its deploy. Replacing the
            # brand art on another application on 2026-08-29 hit precisely that
            # -- correct bytes on disk, a day of stale ones in every cache.
            # stat_result up front: FileResponse only sets etag and
            # last-modified when it is given one, otherwise it stats
            # lazily inside __call__ -- and the conditional check below
            # would then be comparing against headers that do not exist
            # yet, so every revalidation came back 200 with the full body.
            response = FileResponse(
                candidate,
                headers={"cache-control": "public, no-cache"},
                stat_result=candidate.stat(),
            )
            if _not_modified(request, response):
                # Starlette's own 304, not a Response carrying this one's
                # headers: those include content-length, and a 304 has no
                # body, so uvicorn raised "Response content shorter than
                # Content-Length" on every browser revalidation of an icon.
                # NotModifiedResponse keeps only what a 304 may carry.
                return NotModifiedResponse(response.headers)
            return response
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"cache-control": "no-store, must-revalidate"},
        )
