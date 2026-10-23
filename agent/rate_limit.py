"""Rate limiter for the authentication endpoints (audit H-7), counted in the
database.

A sliding window per (client-ip, action) with a lockout once the threshold is
exceeded. Where a caller names the account a guess is against (login, the
second factor, the password re-check), the window is per (client-ip, action,
account) instead, plus a looser one for the IP across every account. A
success clears only that account's window. Clearing the whole IP's window
let one account holder reset the limit on another's password: four guesses
at the victim, one good login as yourself, repeat -- 30 guesses with no 429
(audit B25, 2026-10-22).

The counters live in a table, agent_rate_limits, in the same database as the
accounts they protect. They used to live in this process's memory, so a
restart (every deploy) cleared a lockout and the effective limit multiplied
by the number of uvicorn workers. One row per window; an attempt reads and
rewrites its row under a lock (Postgres: SELECT ... FOR UPDATE; SQLite:
BEGIN IMMEDIATE), so concurrent attempts on any worker are counted one after
another, never two against the same stale count. The decision itself is one
pure function, _decide, shared by both backends. Rows past their window and
lockout are deleted by an opportunistic sweep.

Fails CLOSED: a limiter that cannot reach its database answers 503 instead
of letting the request through, and logs that it did. The database it cannot
reach is the one holding the accounts, so sign-in was already down.

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

import asyncio
import functools
import ipaddress
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException, Request

from agent.backends import backend_for_dsn, sqlite_path_from_dsn

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

UNAVAILABLE = "sign-in is unavailable right now; try again shortly"

TABLE = "agent_rate_limits"

# One row per window, keyed (ip, action, account): the account is "" for an
# action counted per IP alone and _ANY_ACCOUNT for the IP-wide window of one
# counted per account. `hits` is a JSON list of epoch seconds -- text rather
# than a Postgres array, so one decision function reads both backends' rows.
# Times are the app's clock, not the database's: the workers share a host.
_PG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ip TEXT NOT NULL,
    action TEXT NOT NULL,
    account TEXT NOT NULL,
    hits TEXT NOT NULL DEFAULT '[]',
    locked_until DOUBLE PRECISION,
    expires_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (ip, action, account)
);
CREATE INDEX IF NOT EXISTS {TABLE}_expires ON {TABLE} (expires_at)
"""

_SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    ip TEXT NOT NULL,
    action TEXT NOT NULL,
    account TEXT NOT NULL,
    hits TEXT NOT NULL DEFAULT '[]',
    locked_until REAL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (ip, action, account)
);
CREATE INDEX IF NOT EXISTS {TABLE}_expires ON {TABLE} (expires_at);
"""


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


# ---------------------------------------------------------------------------
# the decision, shared by both backends
# ---------------------------------------------------------------------------

def _decide(hits: list[float], locked_until: float | None, max_attempts: int, window: int,
            lockout: int, now: float) -> tuple[list[float], float | None, str | None]:
    """One attempt against one window: (hits, locked_until, refusal or None)."""
    if locked_until and now < locked_until:
        return hits, locked_until, f"too many attempts; try again in {int(locked_until - now)}s"
    recent = [t for t in hits if now - t < window]
    if len(recent) >= max_attempts:
        return [], now + lockout, f"too many attempts; locked for {lockout}s"
    return [*recent, now], None, None


def _expires_at(hits: list[float], locked_until: float | None, window: int) -> float:
    """When the row stops mattering: the lock is over and every hit has aged out."""
    return max(locked_until or 0.0, max(hits, default=0.0) + window)


def _load_hits(raw) -> list[float]:
    try:
        return [float(t) for t in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# the backends
# ---------------------------------------------------------------------------

class PostgresAttempts:
    """The server's counters, on the auth pool (autocommit, dict rows)."""

    def __init__(self, pool):
        self._pool = pool

    async def ensure_schema(self) -> None:
        async with self._pool.connection() as conn:
            for statement in _PG_SCHEMA.split(";"):
                if statement.strip():
                    await conn.execute(statement)

    async def hit(self, key: tuple[str, str, str], max_attempts: int, window: int,
                  lockout: int, now: float) -> str | None:
        async with self._pool.connection() as conn, conn.transaction():
            # The row has to exist before FOR UPDATE can lock it; without it,
            # two first attempts would both read "no row" and both write one hit.
            await conn.execute(
                f"INSERT INTO {TABLE} (ip, action, account, hits, expires_at) "
                "VALUES (%s, %s, %s, '[]', %s) ON CONFLICT DO NOTHING", (*key, now + window))
            cur = await conn.execute(
                f"SELECT hits, locked_until FROM {TABLE} "
                "WHERE ip = %s AND action = %s AND account = %s FOR UPDATE", key)
            row = await cur.fetchone()
            hits, locked = (_load_hits(row["hits"]), row["locked_until"]) if row else ([], None)
            hits, locked, refusal = _decide(hits, locked, max_attempts, window, lockout, now)
            # An upsert rather than an UPDATE: another worker's sweep may have
            # deleted the row between the insert and the lock.
            await conn.execute(
                f"INSERT INTO {TABLE} (ip, action, account, hits, locked_until, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ip, action, account) DO UPDATE SET "
                "hits = EXCLUDED.hits, locked_until = EXCLUDED.locked_until, expires_at = EXCLUDED.expires_at",
                (*key, json.dumps(hits), locked, _expires_at(hits, locked, window)))
        return refusal

    async def clear(self, key: tuple[str, str, str]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(f"DELETE FROM {TABLE} WHERE ip = %s AND action = %s AND account = %s", key)

    async def sweep(self, now: float) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(f"DELETE FROM {TABLE} WHERE expires_at < %s", (now,))


class SqliteAttempts:
    """The same counters in a SQLite file, for an installation on that backend.

    A connection per call, on a worker thread: BEGIN IMMEDIATE takes the
    database's write lock before the read, which is SQLite's FOR UPDATE, and
    busy_timeout makes a second writer wait for it instead of failing.
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        if not self._ready:
            conn.executescript(_SQLITE_SCHEMA)
            self._ready = True
        return conn

    def _hit(self, key, max_attempts, window, lockout, now) -> str | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT hits, locked_until FROM {TABLE} WHERE ip = ? AND action = ? AND account = ?",
                    key).fetchone()
                hits, locked = (_load_hits(row[0]), row[1]) if row else ([], None)
                hits, locked, refusal = _decide(hits, locked, max_attempts, window, lockout, now)
                conn.execute(
                    f"INSERT INTO {TABLE} (ip, action, account, hits, locked_until, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (ip, action, account) DO UPDATE SET "
                    "hits = excluded.hits, locked_until = excluded.locked_until, expires_at = excluded.expires_at",
                    (*key, json.dumps(hits), locked, _expires_at(hits, locked, window)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return refusal
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple) -> None:
        conn = self._connect()
        try:
            conn.execute(sql, params)
        finally:
            conn.close()

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(lambda: self._connect().close())

    async def hit(self, key: tuple[str, str, str], max_attempts: int, window: int,
                  lockout: int, now: float) -> str | None:
        return await asyncio.to_thread(self._hit, key, max_attempts, window, lockout, now)

    async def clear(self, key: tuple[str, str, str]) -> None:
        await asyncio.to_thread(
            self._execute, f"DELETE FROM {TABLE} WHERE ip = ? AND action = ? AND account = ?", key)

    async def sweep(self, now: float) -> None:
        await asyncio.to_thread(self._execute, f"DELETE FROM {TABLE} WHERE expires_at < ?", (now,))


# ---------------------------------------------------------------------------
# this process's limiter
# ---------------------------------------------------------------------------

_limiter: PostgresAttempts | SqliteAttempts | None = None
_last_sweep = 0.0
_SWEEP_INTERVAL = 300  # at most every 5 min per process


def install(limiter: PostgresAttempts | SqliteAttempts | None) -> None:
    """Make `limiter` the one every endpoint counts against. The server does
    this at startup; until it has, every limited endpoint answers 503."""
    global _limiter, _last_sweep
    _limiter = limiter
    _last_sweep = 0.0


def installed() -> PostgresAttempts | SqliteAttempts | None:
    return _limiter


async def open_for(config, pool=None) -> PostgresAttempts | SqliteAttempts:
    """This installation's limiter, its table created. Postgres reuses the
    auth pool; SQLite opens the DSN's own file."""
    if backend_for_dsn(config.dsn) == "postgres":
        limiter = PostgresAttempts(pool)
    else:
        limiter = SqliteAttempts(sqlite_path_from_dsn(config.dsn))
    await limiter.ensure_schema()
    return limiter


async def _maybe_sweep(limiter, now: float) -> None:
    # audit N-1: bound the table. Rotating the IP key (the very thing the
    # spoofing fix prevents, but also legitimate IP churn over time) adds a
    # row per address; rows past their window and lockout are deleted here.
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    try:
        await limiter.sweep(now)
    except Exception:  # noqa: BLE001 -- a missed sweep costs disk, not a sign-in
        logger.exception("rate limiter could not delete expired windows")


def _account_key(account: str | int | None) -> str:
    if account is None:
        return ""
    return str(account).strip().lower() or "?"


async def _hit(limiter, key: tuple[str, str, str], max_attempts: int, window: int,
               lockout: int, now: float) -> None:
    refusal = await limiter.hit(key, max_attempts, window, lockout, now)
    if refusal:
        raise HTTPException(429, refusal)


async def check_rate_limit(request: Request, action: str, account: str | int | None = None) -> None:
    """Raise 429 if this (ip, action) -- or (ip, action, account) when the
    account is named -- is over its limit or locked out, and 503 if that
    cannot be known. Call at the TOP of the endpoint, before any expensive
    work."""
    limit = _LIMITS.get(action)
    if not limit:
        return
    max_attempts, window, lockout = limit
    try:
        limiter = _limiter
        if limiter is None:
            raise RuntimeError("no rate limiter installed")
        ip = client_ip(request)
        now = time.time()
        await _maybe_sweep(limiter, now)
        if account is None:
            await _hit(limiter, (ip, action, ""), max_attempts, window, lockout, now)
            return
        await _hit(limiter, (ip, action, _ANY_ACCOUNT), max_attempts * _IP_WIDE_FACTOR, window, lockout, now)
        await _hit(limiter, (ip, action, _account_key(account)), max_attempts, window, lockout, now)
    except HTTPException:
        raise
    except Exception as e:
        # Fail CLOSED: no counter is no brake, and the database that holds it holds the accounts too.
        logger.exception("rate limiter failed for %r -- request refused with 503", action)
        raise HTTPException(503, UNAVAILABLE) from e


async def clear_rate_limit(request: Request, action: str, account: str | int | None = None) -> None:
    """Call on a SUCCESSFUL auth so a legitimate user's own earlier fat-finger
    attempts don't count toward a later lockout. Only that account's window:
    the IP-wide one is never cleared, and an action counted per IP alone
    should not call this at all (a success there proves nothing about the
    other attempts from the same address). A failure is logged and
    swallowed: the sign-in it follows has already succeeded."""
    try:
        if _limiter is not None:
            await _limiter.clear((client_ip(request), action, _account_key(account)))
    except Exception:  # noqa: BLE001
        logger.exception("rate limiter could not clear %r", action)
