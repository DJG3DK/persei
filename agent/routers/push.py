"""Web push: the four routes the Settings toggle needs.

The first seam extracted from agent/server.py, and chosen for being the
smallest true one rather than the largest saving -- its job is to prove the
router pattern against tests/test_route_inventory.py, which pins every
route's path, method and auth dependency and therefore fails loudly if a move
changes any of them.

It reaches the auth pool through `request.app.state`, not by importing `app`
from server.py, because that import is a cycle: server.py includes this
router, so this module cannot import server.py at module scope. Every later
seam has the same constraint, which is the other thing this extraction is
here to establish.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from agent import auth
from agent.auth import User, require_full_auth

router = APIRouter(prefix="/api/push", tags=["push"])


class PushSubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    label: str | None = None


class PushUnsubscribeRequest(BaseModel):
    endpoint: str
    auth: str | None = None


def _pool(request: Request):
    return request.app.state.auth_pool


@router.get("/key")
async def push_public_key(request: Request, user: User = Depends(require_full_auth)):
    """The VAPID public key the browser needs to subscribe, plus how many
    endpoints this account already has -- the Settings toggle needs both to
    decide what to render, and two round trips for one panel is a slower
    settings page for no reason."""
    from agent import push  # noqa: PLC0415

    try:
        key = push.public_key()
    except push.PushError as e:
        raise HTTPException(500, str(e))
    return {"public_key": key,
            "subscriptions": await auth.count_push_subscriptions(_pool(request), user.id)}


@router.post("/subscribe")
async def push_subscribe(req: PushSubscribeRequest, request: Request,
                         user: User = Depends(require_full_auth)):
    """Register THIS browser for push. Bound to the calling account, never to
    an id in the body: a subscription is permission to receive that account's
    alerts, and accepting a user id from the client would let any signed-in
    account subscribe itself to another's feed."""
    await auth.save_push_subscription(_pool(request), user.id, req.endpoint,
                                      req.p256dh, req.auth, req.label)
    return {"ok": True, "subscriptions": await auth.count_push_subscriptions(
        _pool(request), user.id)}


@router.post("/unsubscribe")
async def push_unsubscribe(req: PushUnsubscribeRequest, request: Request,
                           user: User = Depends(require_full_auth)):
    """Drop one endpoint: the caller's own, or any whose subscription `auth`
    secret the caller presents. A device signing out must be able to stop its
    own notifications even if the account it was bound to has since changed,
    and the browser holding the subscription holds that secret; an endpoint
    URL alone, which shows up in logs and push-service errors, is not enough."""
    await auth.delete_push_subscription(_pool(request), req.endpoint,
                                        user_id=user.id, auth_key=req.auth)
    return {"ok": True, "subscriptions": await auth.count_push_subscriptions(
        _pool(request), user.id)}


@router.post("/test")
async def push_test(request: Request, user: User = Depends(require_full_auth)):
    """Send this account's own devices a test notification.

    The same reason the Telegram panel has one: a push that silently fails --
    permission revoked in the OS, an endpoint expired, the app uninstalled --
    is indistinguishable from a quiet night, and the first time that matters
    is the escalation nobody saw.
    """
    from agent import push  # noqa: PLC0415

    pool = _pool(request)
    targets = await auth.get_push_targets(pool, user_id=user.id)
    if not targets:
        raise HTTPException(400, "no device is subscribed for this account")
    sent = 0
    for t in targets:
        ok, status = await push.send_one(
            t, "Persei", "Push is working. This is a test from Settings.",
            url="/", tag="tektonix-test")
        if ok:
            sent += 1
            await auth.mark_push_ok(pool, t["endpoint"])
        elif status in (404, 410):
            await auth.delete_push_subscription(pool, t["endpoint"])
    return {"ok": sent > 0, "sent": sent, "devices": len(targets)}
