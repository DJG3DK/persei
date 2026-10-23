"""The review dashboard, proxied: /_review/{path}.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-27 -- the
last route seam, and the one docs/todo.md said must never share a change
with provisioning.py or history_index.py. The route is unchanged;
tests/test_route_inventory.py pins its path, methods and guard, and
tests/test_review_proxy.py drives the handler directly for the two things a
proxy must not get wrong: who gets through, and what happens to a header
the caller supplied.

The handler reads nothing off app state: its authority is the session
(admin, via require_full_auth then auth.require_admin) and the control
secret comes from the environment at call time. httpx is used as a module
attribute so a test's stand-in AsyncClient is seen.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from agent import auth
from agent.auth import User, require_full_auth

router = APIRouter(tags=["review"])


# --- the review dashboard, proxied ------------------------------------------

# The review services listen on 4100/4101 and hold the only write path into a
# live repo, so they bind loopback and publish nothing. This proxy is the one
# bridge to them, on a host install and in the container bundle alike: the
# process that is already the authenticated front door adds the shared secret
# the browser must never hold, for an admin over the console's session. nginx
# used to inject the secret on a host install; it no longer does (867db54).
# Until 2026-09-20 Check now, the diff view and the manual merge worked only
# on a host install.
_REVIEW_PROXY_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})

# The caller's credentials stop here. The session cookie and any bearer token
# authenticate the caller TO THIS PROCESS; the review service authenticates
# the proxy by the secret set below, and has no use for them. Forwarded, they
# sat in the review services' request logs and in any error they echoed
# (2026-09-29). Dropped on the way in, like the caller's own X-Review-Secret.
_REVIEW_PROXY_CALLER_CREDENTIALS = frozenset({"cookie", "authorization", "x-review-secret"})

# Merge and restart genuinely take minutes on a large project, and nginx allows
# half an hour for exactly that reason. A shorter limit here would turn a slow
# deploy into a failed one.
_REVIEW_PROXY_TIMEOUT = 1800.0


@router.api_route("/_review/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
               include_in_schema=False)
async def review_proxy(path: str, request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)

    from agent.tools.review_gate import REVIEW_SERVICE_HOST, REVIEW_SERVICE_PORT  # noqa: PLC0415

    # The secret is SET here, never forwarded. A client that sends its own
    # X-Review-Secret must not be able to influence what the review service
    # sees -- this endpoint's authority comes from the session, not the header.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _REVIEW_PROXY_HOP_BY_HOP and k.lower() not in _REVIEW_PROXY_CALLER_CREDENTIALS
    }
    secret = os.environ.get("REVIEW_CONTROL_SECRET")
    if secret:
        headers["X-Review-Secret"] = secret

    url = f"http://{REVIEW_SERVICE_HOST}:{REVIEW_SERVICE_PORT}/{path}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=_REVIEW_PROXY_TIMEOUT) as client:
            upstream = await client.request(
                request.method, url, params=request.query_params,
                content=body or None, headers=headers,
            )
    except httpx.HTTPError as e:
        # The service being down is an ordinary state -- a restart, a bundle
        # where it was not started. Say which service, because "502" from the
        # console looks like the console.
        return JSONResponse(
            {"ok": False, "error": f"the review service is not reachable: {type(e).__name__}"},
            status_code=502,
        )

    passthrough = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _REVIEW_PROXY_HOP_BY_HOP
    }
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=passthrough)
