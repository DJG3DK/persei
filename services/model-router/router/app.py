"""The router's HTTP surface.

Deliberately small:

  POST /v1/chat/completions   every model call, from every service on the box
  POST /v1/embeddings         episode embeddings, so they are billed here too
  GET  /health/liveliness     agent/health.py, unauthenticated by design
  GET  /health/readiness      503 until there are deployments and an upstream key
  GET  /v1/model/info         the deployment table, for tooling
  GET  /v1/models             the alias list
  GET  /v1/stats              per-alias latency, errors, spend, from the ledger

The compatibility contract is not a matter of taste -- each item below is
something that breaks a specific caller if it changes:

  * `x-router-call-id` on every response. agent/middleware/budget_guard.py
    reads that exact header name (CALL_ID_HEADER) to match a model call to its
    billed cost in the ledger. Renaming it silently reverts every task to
    estimated spend.
  * `metadata.agent_task_id` / `agent_session_id` in the request body, put
    there by agent/model_client.py's _call_metadata, recorded and NOT forwarded
    upstream.
    Without them the ledger can price a call but never total a task.
  * Bearer auth against the same master key the proxy used.
"""

from __future__ import annotations

import asyncio
import logging
import hmac
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router import ledger, upstream
from router import stats as stats_mod
from router.config import Registry
from router.fastest import FastestProviders, SupportedParams

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("model-router")

def _read_master_key() -> str:
    """MODEL_ROUTER_KEY, or the file MODEL_ROUTER_KEY_FILE names -- the
    bundle generates the key on first boot and hands it over as a file
    (docker/postgres/init-secrets.sh)."""
    key = (os.environ.get("MODEL_ROUTER_KEY") or "").strip()
    path = os.environ.get("MODEL_ROUTER_KEY_FILE")
    if not key and path:
        try:
            with open(path, encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError as e:
            logger.error("MODEL_ROUTER_KEY_FILE %s is unreadable: %s", path, e)
    return key


MASTER_KEY = _read_master_key()
# No key at all used to mean "open to anyone who can reach the port". That is
# now an explicit choice for a throwaway local run, never the silent result of
# a missing variable.
ALLOW_UNAUTHENTICATED = os.environ.get("MODEL_ROUTER_ALLOW_UNAUTHENTICATED") == "1"


def _parse_consumer_keys(raw: str) -> dict[str, str]:
    """`label=key,label=key` -> {key: label}.

    One key per caller, replacing a single shared master key. Without
    it every consumer on the box would have to hold a credential that can
    call any alias, and revoking one would mean rotating all of them.

    Keyed BY THE SECRET so a lookup is one dict hit and never a loop that
    leaks timing per configured label.
    """
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, key = part.split("=", 1)
        label, key = label.strip(), key.strip()
        if label and key:
            out[key] = label
    return out


CONSUMER_KEYS = _parse_consumer_keys(os.environ.get("MODEL_ROUTER_CONSUMER_KEYS", ""))
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# budget_guard matches on this by name (CALL_ID_HEADER); the two move together.
CALL_ID_HEADER = "x-router-call-id"
# Extra tries on the SAME deployment before falling back, for transient
# failures only (see upstream.is_transient).
RETRIES_PER_DEPLOYMENT = int(os.environ.get("MODEL_ROUTER_RETRIES", "2"))
BACKOFF_S = float(os.environ.get("MODEL_ROUTER_BACKOFF_S", "0.6"))

registry = Registry()
fastest = FastestProviders()
supported = SupportedParams()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client for the process. Connection reuse is most of the
    # difference between a 200ms call and a 700ms one at this volume.
    app.state.http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
        headers={"HTTP-Referer": "https://perseiagent.ai", "X-Title": "Persei"},
    )
    logger.info("model-router up: %d deployments", len(registry.table.deployments))
    yield
    await app.state.http.aclose()


app = FastAPI(title="Persei model router", lifespan=lifespan)


def _authorise(authorization: str | None) -> str:
    """Returns the CALLER LABEL, which the ledger records.

    No key configured fails closed unless MODEL_ROUTER_ALLOW_UNAUTHENTICATED=1
    says an open dev run is intended. A consumer key authorises exactly the
    same surface as the master key today -- the point of the split is
    attribution and independent revocation, not a narrower grant. Per-alias
    scoping can hang off the same label later if it is ever wanted.
    """
    if not MASTER_KEY and not CONSUMER_KEYS:
        if ALLOW_UNAUTHENTICATED:
            return "dev"
        raise HTTPException(503, "no MODEL_ROUTER_KEY configured; set one, or "
                                 "MODEL_ROUTER_ALLOW_UNAUTHENTICATED=1 for a local dev run")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if MASTER_KEY and hmac.compare_digest(token.encode("utf-8"), MASTER_KEY.encode("utf-8")):
        return "master"
    label = CONSUMER_KEYS.get(token)
    if label:
        return label
    raise HTTPException(401, "invalid token")


@app.get("/health/liveliness")
async def liveliness():
    """Unauthenticated on purpose: agent/health.py polls it with no key and no
    model call, and a health probe that needs a secret is a health probe that
    stops being run."""
    t = registry.table
    return {"status": "alive", "deployments": len(t.deployments), "config_loaded_at": t.loaded_at}


@app.get("/health/readiness")
async def readiness():
    t = registry.table
    ok = bool(t.deployments) and bool(OPENROUTER_KEY)
    return JSONResponse({"status": "ready" if ok else "degraded",
                         "deployments": len(t.deployments),
                         "upstream_key": bool(OPENROUTER_KEY)},
                        status_code=200 if ok else 503)


@app.get("/v1/model/info")
async def model_info(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"data": [
        {"model_name": d.alias,
         "params": {"model": f"openrouter/{d.model}"},
         "model_info": {"input_cost_per_token": d.input_cost_per_token,
                        "output_cost_per_token": d.output_cost_per_token,
                        "timeout_s": d.timeout_s}}
        for d in t.deployments.values()]}


@app.get("/v1/stats")
async def stats(window_minutes: int = 60, authorization: str | None = Header(default=None)):
    """What the router has been doing, read back out of its own ledger.

    Per alias: call count, error rate, p50/p95/max latency, spend, cache hit
    rate, retry count, and which providers actually served it -- none of which
    was visible before without parsing the file by hand.
    """
    _authorise(authorization)
    window = max(1, min(int(window_minutes), 24 * 60)) * 60
    return {**stats_mod.summarise(ledger.LOG_PATH, window), "fastest_providers": fastest.snapshot()}


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"object": "list",
            "data": [{"id": a, "object": "model", "owned_by": "persei"} for a in t.deployments]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    caller = _authorise(authorization)
    body = await request.json()
    table, alias, call_id, task_id, session_id = _resolve(body)
    client: httpx.AsyncClient = request.app.state.http

    if body.get("stream"):
        return await _streamed(client, table, alias, body, call_id, task_id, session_id, caller)

    return await _buffered(client, table, alias, body, upstream.call_once,
                           call_id, task_id, session_id, caller)


@app.post("/v1/embeddings")
async def embeddings(request: Request, authorization: str | None = Header(default=None)):
    """Vectors for episode recall, routed exactly like a chat call.

    It is here rather than in the agent talking to OpenRouter directly for one
    reason: spend in this system is whatever routing.jsonl says was billed,
    never a rate table, so a model call that bypassed the router would be
    money the operator's totals cannot see. Everything else -- the alias, the
    fallback chain, the retries, the ledger line -- is shared code with the
    chat path, not a parallel copy of it.

    There is no streaming branch because the endpoint has no streaming form.
    """
    caller = _authorise(authorization)
    body = await request.json()
    table, alias, call_id, task_id, session_id = _resolve(body)
    client: httpx.AsyncClient = request.app.state.http

    return await _buffered(client, table, alias, body, upstream.embed_once,
                           call_id, task_id, session_id, caller)


def _resolve(body: dict):
    """The alias and the call's identity, before anything is spent.

    An unknown alias is a 404 rather than an attempt: the old proxy reported
    a missing deployment at the moment of failure, after the latency and
    sometimes after the money.
    """
    alias = body.get("model")
    if not alias:
        raise HTTPException(400, "no model given")

    table = registry.table
    if alias not in table.deployments:
        raise HTTPException(404, f"unknown model {alias!r}")

    meta = body.get("metadata") or {}
    return table, alias, str(uuid.uuid4()), meta.get("agent_task_id"), meta.get("agent_session_id")


async def _buffered(client, table, alias, body, send, call_id, task_id, session_id, caller):
    """Walk the fallback chain, retrying TRANSIENT failures on each deployment
    before moving on. Moving to a fallback on the first 429 throws away the
    model the operator pinned because a provider asked us to wait a moment --
    and the fallback is, by definition, not their first choice.

    `send` is the upstream call: chat completions or embeddings. The chain,
    the retry rule and the ledger line are the same for both by construction,
    because an embedding that was not billed here is a hole in the one spend
    figure the operator trusts.
    """
    chain = table.chain(alias)
    last: upstream.Attempt | None = None
    attempt_no = 0
    for name in chain:
        dep = table.deployments[name]
        for retry in range(RETRIES_PER_DEPLOYMENT + 1):
            attempt_no += 1
            att = await send(client, OPENROUTER_KEY,
                             await supported.body_for(client, OPENROUTER_KEY, dep.model, dep.extra_body, body),
                             dep.model, fastest.extra_body_for(client, OPENROUTER_KEY, dep.model, dep.extra_body),
                             dep.timeout_s)
            att.alias = name
            last = att
            ledger.record(
                caller=caller,
                call_id=call_id, alias=alias, model=att.usage.model or dep.model,
                prompt_tokens=att.usage.prompt_tokens, completion_tokens=att.usage.completion_tokens,
                cached_tokens=att.usage.cached_tokens, cost=att.usage.cost,
                duration_s=att.duration_s, task_id=task_id, session_id=session_id,
                provider=att.usage.provider, attempt=attempt_no,
                error=not att.ok, error_detail=att.error,
            )
            if att.ok:
                headers = {CALL_ID_HEADER: call_id, "x-router-deployment": name,
                           "x-router-attempt": str(attempt_no)}
                return JSONResponse(att.payload, headers=headers)

            transient = upstream.is_transient(att.status, att.error)
            logger.warning("attempt %d on %s failed (%s, %s): %s", attempt_no, name,
                           att.status, "transient" if transient else "permanent", att.error)
            if not transient:
                break          # a 400 will fail the same way twice
            if retry < RETRIES_PER_DEPLOYMENT:
                # Short backoff with jitter. Long enough to clear a rate-limit
                # burst, short enough that a real outage still reaches the
                # fallback while the caller is waiting.
                await asyncio.sleep(BACKOFF_S * (2 ** retry) * (0.5 + random.random()))

    detail = _public_failure(getattr(last, "status", None) if last else None, bool(last))
    return JSONResponse({"error": {"message": detail, "type": "upstream_error",
                                   "chain": chain, "call_id": call_id}},
                        status_code=502, headers={CALL_ID_HEADER: call_id})


async def _streamed(client, table, alias, body, call_id, task_id, session_id, caller):
    """Streaming, with the fallback chain available up to the first byte.

    The rule is not "streams cannot fall back" -- it is that a response becomes
    committed the moment a byte reaches the client. An upstream that refuses
    with a 429 does so BEFORE any of its body exists, and falling back there is
    as safe as it is for a buffered call. Only a failure part-way through a
    stream is unrecoverable, because retrying would splice two different
    completions into one response.

    So: walk the chain while nothing has been emitted, and the instant the
    first chunk goes out, commit to that deployment. The roles that stream here
    -- summarizer, cartographer, consolidator -- all have fallbacks configured
    precisely because their providers have thrown 429s before.

    The first chunk is fetched BEFORE the response is built, so a chain that
    fails entirely is the buffered path's 502 and not an empty 200 stream;
    and every committed attempt is ledgered exactly once, in a finally, so a
    client that hangs up mid-stream (the stall watchdog, a cancelled task)
    still leaves the line for the tokens it spent (2026-09-29 audit, A8).
    """
    chain = table.chain(alias)
    started = time.monotonic()
    attempt_no = 0
    last_error = None
    last_status = None
    for name in chain:
        dep = table.deployments[name]
        for retry in range(RETRIES_PER_DEPLOYMENT + 1):
            attempt_no += 1
            usage = upstream.Usage()
            t0 = time.monotonic()
            extra = fastest.extra_body_for(client, OPENROUTER_KEY, dep.model, dep.extra_body)
            trimmed = await supported.body_for(client, OPENROUTER_KEY, dep.model, dep.extra_body, body)
            agen = upstream.stream_once(client, OPENROUTER_KEY, trimmed, dep.model,
                                        extra, dep.timeout_s, usage)
            try:
                first = await agen.__anext__()
            except StopAsyncIteration:
                first = None                    # an empty but successful stream
            except Exception as e:  # noqa: BLE001
                status = getattr(getattr(e, "response", None), "status_code", None)
                last_error = f"{type(e).__name__}: {str(e)[:300]}"
                last_status = status
                ledger.record(caller=caller, call_id=call_id, alias=alias, model=dep.model,
                              duration_s=time.monotonic() - t0, task_id=task_id,
                              session_id=session_id, attempt=attempt_no, error=True,
                              error_detail=last_error)
                transient = upstream.is_transient(status, str(e))
                logger.warning("stream attempt %d on %s failed before first byte (%s): %s",
                               attempt_no, name, "transient" if transient else "permanent", e)
                if not transient:
                    break               # move to the next deployment
                if retry < RETRIES_PER_DEPLOYMENT:
                    await asyncio.sleep(BACKOFF_S * (2 ** retry) * (0.5 + random.random()))
                continue
            # Committed to this deployment from here on.
            return StreamingResponse(
                _committed_body(agen, first, name, dep, usage, t0, started, attempt_no,
                                caller=caller, call_id=call_id, alias=alias,
                                task_id=task_id, session_id=session_id),
                media_type="text/event-stream",
                headers={CALL_ID_HEADER: call_id, "x-router-deployment": alias,
                         "cache-control": "no-cache"})
    logger.error("every deployment in %s failed before streaming", chain)
    return JSONResponse({"error": {"message": _public_failure(last_status, last_error is not None),
                                   "type": "upstream_error", "chain": chain, "call_id": call_id}},
                        status_code=502, headers={CALL_ID_HEADER: call_id})


def _public_failure(status: int | None, attempted: bool) -> str:
    """What a caller is told when the whole chain failed: the upstream status
    and where to look, never the exception text. The full error stays in the
    ledger and the log under the call id (code scanning, py/stack-trace-
    exposure, 2026-09-30)."""
    if not attempted:
        return "no deployment answered"
    if status:
        return f"every deployment failed; the last upstream answered {status} (details in the router ledger)"
    return "every deployment failed before answering (details in the router ledger)"


async def _committed_body(agen, first, name, dep, usage, t0, started, attempt_no, *,
                          caller, call_id, alias, task_id, session_id):
    """The body of a stream whose first chunk has arrived. One ledger line
    whatever happens: completed, failed mid-body, or closed by the client."""
    outcome = "cancelled"
    try:
        if first is not None:
            yield first
        async for chunk in agen:
            yield chunk
        outcome = "done"
    except Exception as e:  # noqa: BLE001
        # Splicing a second completion onto a partial one would be worse
        # than the truncation the client already sees.
        outcome = f"{type(e).__name__}: {str(e)[:300]}"
        logger.error("stream failed mid-body on %s, cannot retry: %s", name, e)
    finally:
        if outcome != "done":
            try:
                await agen.aclose()     # release the upstream connection
            except BaseException as e:  # noqa: BLE001 -- the ledger line matters more
                logger.debug("upstream stream close after %s: %s", outcome, e)
        if outcome == "done":
            ledger.record(
                caller=caller,
                call_id=call_id, alias=alias, model=usage.model or dep.model,
                prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_tokens, cost=usage.cost,
                duration_s=time.monotonic() - started, task_id=task_id,
                session_id=session_id, provider=usage.provider, attempt=attempt_no,
            )
        else:
            # The usage so far, when the upstream reported any before the
            # cut: the tokens were spent either way.
            ledger.record(caller=caller, call_id=call_id, alias=alias, model=usage.model or dep.model,
                          prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                          cached_tokens=usage.cached_tokens, cost=usage.cost,
                          duration_s=time.monotonic() - t0, task_id=task_id,
                          session_id=session_id, provider=usage.provider, attempt=attempt_no,
                          error=True, error_detail=outcome)
