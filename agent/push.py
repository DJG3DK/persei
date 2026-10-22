"""Web push: the same alerts Telegram already carries, delivered to a phone.

Why this exists alongside agent/notify.py rather than inside it: Telegram is a
third party the operator has to set up an account and a bot for, and it does
not exist on a locked phone as "the Persei app said something". Web push
does -- an installed PWA (see frontend/public/sw.js) gets a system
notification with the app's own icon, on Android and on iOS 16.4+.

The fan-out itself is NOT duplicated. notify.notify_operators sends to both
transports from one place, filtered by one copy of `_may_hear_about`, because
two fan-outs are two chances for the scoping rule to drift and start telling a
single-repo user about every project.

VAPID
-----
Push services (FCM, Mozilla, Apple) will not accept an unsigned push. VAPID is
an EC P-256 keypair the server holds: the public half goes to the browser as
`applicationServerKey` at subscribe time, and every send is signed with the
private half. The pair must be STABLE -- regenerating it silently invalidates
every existing subscription, and the symptom is "notifications just stopped"
with no error anywhere -- so it is written once to a file beside the deploy
keys (never into the repo) and read back thereafter.

What is deliberately not here
-----------------------------
No payload beyond a title, a body, a URL and a tag. The subscription endpoint
belongs to a push service run by Google, Mozilla or Apple, and the payload is
end-to-end encrypted to the browser's key -- but the SERVICE still learns
timing and size, so alert bodies stay as short as the Telegram ones and carry
no diff content.
"""
from __future__ import annotations

import asyncio
import base64
import json
import ipaddress
import logging
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent import paths

logger = logging.getLogger("agent.push")

KEYS_DIR: Path = Path(os.environ.get("AGENT_KEYS_DIR") or (paths.REPO_ROOT / "keys"))
VAPID_FILE: Path = KEYS_DIR / "vapid.json"

# Where a push service should complain to about our sends. Not a real mailbox
# on most installs, which is fine and expected -- the RFC wants a stable
# contact, not a monitored one.
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT") or "mailto:admin@perseiagent.ai"

# Push services reject anything large; keep well under the 4KB ceiling.
_MAX_BODY = 480

_cache: dict[str, str] | None = None


class PushError(Exception):
    """Configuration or send failure the caller may show to an operator."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate() -> dict[str, str]:
    """A fresh P-256 pair, in the two encodings the stack wants: the raw
    uncompressed public point for the browser, and a PEM private key for
    py_vapid's signer."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    point = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {"private_pem": pem, "public_key": _b64url(point)}


def keys(*, create: bool = True) -> dict[str, str] | None:
    """The VAPID pair, generating it once on first use.

    0600 and outside the repo, like the deploy keys: the private half can sign
    pushes to every subscription this install has ever issued.
    """
    global _cache
    if _cache is not None:
        return _cache
    if VAPID_FILE.exists():
        try:
            _cache = json.loads(VAPID_FILE.read_text())
            return _cache
        except (OSError, ValueError) as e:
            raise PushError(f"{VAPID_FILE} is unreadable: {e}") from e
    if not create:
        return None
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    pair = _generate()
    tmp = VAPID_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pair, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, VAPID_FILE)
    logger.info("push: generated a VAPID keypair at %s", VAPID_FILE)
    _cache = pair
    return _cache


def public_key() -> str:
    """What the browser passes as applicationServerKey."""
    k = keys()
    assert k is not None  # create=True never returns None
    return k["public_key"]


def configured() -> bool:
    try:
        return keys(create=False) is not None
    except PushError:
        return False


def _payload(title: str, body: str, url: str = "/", tag: str | None = None) -> str:
    return json.dumps({
        "title": title[:120],
        "body": body[:_MAX_BODY],
        "url": url,
        # A tag collapses repeats: three escalations of the same task replace
        # one another on the lock screen instead of stacking into a wall.
        "tag": tag or "tektonix",
    })


def endpoint_refusal(endpoint: str) -> str | None:
    """Why this subscription endpoint must not be stored, or None. The
    check that needs no DNS: https, a host, and not a literal private
    address or localhost. A real browser's endpoint is always a push
    service on the public internet; anything else is someone pointing the
    server's own POST at an internal port, with `sent: 1` from the test
    button as the oracle (audit B29, 2026-10-22). The name is resolved
    again at every send (_resolves_public)."""
    from agent.tools.url_guard import _is_blocked_address  # noqa: PLC0415

    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").strip("[]").lower()
    except ValueError:
        return "not a valid URL"
    if parts.scheme != "https":
        return "a push endpoint must be https"
    if not host:
        return "a push endpoint needs a host"
    if host == "localhost" or host.endswith(".localhost"):
        return "a push endpoint must be a public push service"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return "a push endpoint must be a public push service" if _is_blocked_address(host) else None


def _resolves_public(endpoint: str) -> bool:
    """Every address the endpoint's host resolves to is public, checked
    with the browse tool's own address rule."""
    from agent.tools.url_guard import _is_blocked_address  # noqa: PLC0415

    if endpoint_refusal(endpoint):
        return False
    parts = urlsplit(endpoint)
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and not any(_is_blocked_address(a) for a in addresses)


def _send_blocking(sub: dict[str, Any], data: str) -> tuple[bool, int | None]:
    """One send. Returns (ok, status). pywebpush does the RFC 8291 payload
    encryption and the VAPID signature; this wrapper exists to turn its
    exception into a status the caller can act on -- specifically 404/410,
    which is the push service saying the subscription is dead and should be
    deleted rather than retried forever."""
    from py_vapid import Vapid  # noqa: PLC0415
    from pywebpush import WebPushException, webpush  # noqa: PLC0415

    if not _resolves_public(sub["endpoint"]):
        logger.warning("push: refused to send to a non-public endpoint %.60s", sub["endpoint"])
        return False, None
    k = keys()
    assert k is not None
    info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        # A Vapid INSTANCE, not the PEM text. pywebpush accepts three things
        # for this argument and a PEM string is none of them: it takes a Vapid
        # object, a path to a key file, or raw base64url DER. Handed a PEM it
        # falls through to Vapid.from_string, which strips the newlines and
        # base64-decodes the whole thing INCLUDING the "-----BEGIN PRIVATE
        # KEY-----" header, and dies on it.
        #
        # That was live from 2026-09-17 until it was found by pressing "Send a
        # test" on a phone: every send raised, was caught below, and reported
        # as "no device accepted it" -- which reads like an expired
        # subscription rather than a key we never managed to load. Hence
        # test_push.py::test_the_stored_key_is_in_a_form_the_signer_accepts,
        # which signs with the real stored key instead of mocking the send.
        signer = Vapid.from_pem(k["private_pem"].encode())
        webpush(subscription_info=info, data=data,
                vapid_private_key=signer,
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=10)
        return True, 200
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return False, status
        logger.warning("push: send failed (%s) for %.60s", status, sub["endpoint"])
        return False, status
    except Exception:  # noqa: BLE001 -- a transport error must never raise into an alert path
        logger.exception("push: unexpected send failure")
        return False, None


async def send_one(sub: dict[str, Any], title: str, body: str,
                   url: str = "/", tag: str | None = None) -> tuple[bool, int | None]:
    """Send to one subscription, off the event loop.

    pywebpush is synchronous and does real network IO; awaiting it inline
    would block the loop that serves the dashboard and the task WebSockets for
    as long as the push service takes to answer.
    """
    data = _payload(title, body, url, tag)
    return await asyncio.to_thread(_send_blocking, sub, data)
