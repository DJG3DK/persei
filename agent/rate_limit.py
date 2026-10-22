"""Minimal in-memory rate limiter for authentication endpoints (audit H-7).

No new dependency and no external store: a single-process FastAPI app with one
uvicorn worker (how this deploys) can hold attempt counters in a dict. A sliding
window per (client-ip, action) with a lockout once the threshold is exceeded.

Where a caller names the account a guess is against (login, the second
factor, the password re-check), the window is per (client-ip, action,
account) instead, plus a looser one for the IP across every account. A
success clears only that account's window. Clearing the whole IP's window
let one account holder reset the limit on another's password: four guesses
at the victim, one good login as yourself, repeat -- 30 guesses with no 429
(audit B25, 2026-10-22).
Deliberately fails OPEN on its own errors -- a bug here must never lock every
user out of their own dashboard -- but that is the only case it permits through,
and it says so in the log every time (a limiter that silently stops limiting is
indistinguishable from one that works).

SINGLE PROCESS IS AN ASSUMPTION, not a detail. The counters live in this
process's memory: they reset on every restart (a deploy clears a lockout), and
with more than one uvicorn worker each worker would count on its own, so the
effective limit would multiply by the worker count. The app runs one worker
(pm2 on a host install, one uvicorn in the bundle). Raising that means moving
these counters into Postgres, or failing closed -- in the same change.

Keyed on the client IP. audit N-1: the IP is taken from X-Real-IP (which nginx
sets authoritatively from $remote_addr, OVERWRITING any client-supplied value),
not from the first hop of X-Forwarded-For -- nginx uses
$proxy_add_x_forwarded_for, which APPENDS the real peer, so the client-supplied
first hop is attacker-controlled and rotating it defeated the limiter entirely.
Falls back to the LAST hop of XFF (the one our own proxy appended) and finally
the socket peer. Not perfect against a botnet, but it turns the measured
"25 password guesses in a few seconds, all processed" into five a minute per
account, then a five-minute lock.

Both headers are read only when the socket peer is a trusted proxy
(AGENT_TRUSTED_PROXIES, loopback by default). From anyone else they are the
client's own words, and the socket peer is the key.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger("tektonix")

# action -> (max_attempts, window_seconds, lockout_seconds)
_LIMITS = {
    "login": (5, 60, 300),
    "verify-2fa": (5, 60, 300),
    "reset-password": (5, 300, 900),
    "reset-request": (3, 300, 900),
    # The approve link's POST (agent/routers/github.py): unauthenticated by design --
    # the token is the credential -- so it gets the same brake as a password
    # guess. Each token is single-use; this bounds how fast anyone can try.
    "github-approve": (10, 60, 300),
    # The password re-check on an authenticated route (change-password, and
    # 2FA setup/disable): a stolen session could otherwise guess the password
    # at full speed, and the password is what turns that session into
    # everything else. Same brake as login.
    "password-recheck": (5, 60, 300),
    # Passkey sign-in (agent/routers/passkeys.py): each try needs the
    # device's own unlock, so guessing is not the threat a password brake is
    # for; this bounds the options-and-verify pairs anyone can churn through.
    "passkey-login": (10, 60, 300),
}

# An action counted per account also has an IP-wide window, this many times
# the per-account limit and never cleared by a success: without it, one IP
# could try five passwords a minute against every account it can name.
_IP_WIDE_FACTOR = 4
_ANY_ACCOUNT = "*"

# (ip, action, account) -> list[timestamp]  and  -> locked_until. The account
# is "" for an action counted per IP alone, and _ANY_ACCOUNT for the IP-wide
# window of one counted per account.
_attempts: dict[tuple[str, str, str], list[float]] = defaultdict(list)
_locked_until: dict[tuple[str, str, str], float] = {}


# Peers whose X-Real-IP / X-Forwarded-For are believed. Loopback by default:
# the host install's nginx runs on the same machine. The compose bundle
# publishes the agent's port with nothing in front of it, so there a client's
# own X-Real-IP reached here unchallenged and a new value per request was a
# fresh bucket per request. A proxy on another address is named here.
TRUSTED_PROXIES_ENV = "AGENT_TRUSTED_PROXIES"
_DEFAULT_TRUSTED = "127.0.0.1/32,::1/128"


@functools.lru_cache(maxsize=8)
def _parse_trusted(raw: str) -> tuple:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("AGENT_TRUSTED_PROXIES: ignoring an entry that is not an address or network")
    return tuple(nets)


def _peer_is_trusted_proxy(peer: str | None) -> bool:
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    nets = _parse_trusted(os.environ.get(TRUSTED_PROXIES_ENV, _DEFAULT_TRUSTED))
    return any(ip.version == n.version and ip in n for n in nets)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    if not _peer_is_trusted_proxy(peer):
        return peer or "unknown"
    # audit N-1: X-Real-IP is set by nginx to $remote_addr and overwrites any
    # header the client sent, so it is the real peer and cannot be spoofed
    # through the proxy. Prefer it.
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    # Fall back to the LAST hop of X-Forwarded-For -- with nginx's
    # $proxy_add_x_forwarded_for the real peer is appended LAST, so the last
    # entry is the one our proxy added (the first is client-controllable).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return peer


_last_evict = 0.0
_EVICT_INTERVAL = 300  # sweep at most every 5 min


def _maybe_evict(now: float) -> None:
    # audit N-1: bound the dicts. Without this, rotating the IP key (the very
    # thing the spoofing fix prevents, but also legitimate IP churn over time)
    # grew _attempts / _locked_until without limit -- the same unbounded-growth
    # shape flagged elsewhere. Sweep expired entries opportunistically.
    global _last_evict
    if now - _last_evict < _EVICT_INTERVAL:
        return
    _last_evict = now
    for key in list(_attempts.keys()):
        action = key[1]
        limit = _LIMITS.get(action)
        window = limit[1] if limit else 900
        if not any(now - t < window for t in _attempts[key]):
            _attempts.pop(key, None)
    for key in list(_locked_until.keys()):
        if now >= _locked_until[key]:
            _locked_until.pop(key, None)


def _account_key(account: str | int | None) -> str:
    if account is None:
        return ""
    return str(account).strip().lower() or "?"


def _hit(key: tuple[str, str, str], max_attempts: int, window: int, lockout: int, now: float) -> None:
    locked = _locked_until.get(key)
    if locked and now < locked:
        raise HTTPException(429, f"too many attempts; try again in {int(locked - now)}s")

    recent = [t for t in _attempts[key] if now - t < window]
    if len(recent) >= max_attempts:
        _locked_until[key] = now + lockout
        _attempts[key] = []
        raise HTTPException(429, f"too many attempts; locked for {lockout}s")

    recent.append(now)
    _attempts[key] = recent


def check_rate_limit(request: Request, action: str, account: str | int | None = None) -> None:
    """Raise 429 if this (ip, action) -- or (ip, action, account) when the
    account is named -- is over its limit or locked out. Call at the TOP of
    the endpoint, before any expensive work."""
    try:
        limit = _LIMITS.get(action)
        if not limit:
            return
        max_attempts, window, lockout = limit
        ip = client_ip(request)
        now = time.time()
        _maybe_evict(now)

        if account is None:
            _hit((ip, action, ""), max_attempts, window, lockout, now)
            return
        _hit((ip, action, _ANY_ACCOUNT), max_attempts * _IP_WIDE_FACTOR, window, lockout, now)
        _hit((ip, action, _account_key(account)), max_attempts, window, lockout, now)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- fail OPEN on an internal limiter bug
        logger.exception("rate limiter failed for %r -- request allowed through unlimited", action)
        return


def clear_rate_limit(request: Request, action: str, account: str | int | None = None) -> None:
    """Call on a SUCCESSFUL auth so a legitimate user's own earlier fat-finger
    attempts don't count toward a later lockout. Only that account's window:
    the IP-wide one is never cleared, and an action counted per IP alone
    should not call this at all (a success there proves nothing about the
    other attempts from the same address)."""
    try:
        key = (client_ip(request), action, _account_key(account))
        _attempts.pop(key, None)
        _locked_until.pop(key, None)
    except Exception:  # noqa: BLE001
        logger.exception("rate limiter could not clear %r", action)
        return


def reset_rate_limits() -> None:
    """Forget every window and lock. For tests; a restart does the same."""
    _attempts.clear()
    _locked_until.clear()
