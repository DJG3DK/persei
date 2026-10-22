"""Username/password + TOTP 2FA login: argon2id hashing, RFC 6238 TOTP via
an encrypted-at-rest secret, one-time recovery codes -- deliberately
simplified on the session side to a single opaque, httpOnly session cookie
(hashed, stored server-side, revocable by deleting its row) rather than a
JWT access/refresh pair. An access/refresh split earns its keep at
customer-facing scale, where bounding the blast radius of a leaked token
matters more than the extra moving state; this is a 2-3-user internal tool,
and one server-side-revocable session covers the same practical need (a
cookie's contents are useless without a live database row, and logout or an
admin action can kill it immediately) with far less state to keep
consistent -- and it covers WebSocket auth for free, since a browser
attaches cookies to a WS handshake the same as any other request, where a
Bearer-header JWT would need a separate mechanism.

Schema lives in the SAME Postgres database this agent already uses for its
LangGraph checkpointer/store (config.pg_dsn) -- a real relational schema
(unique email, FK-cascaded sessions/recovery codes) rather than the
key-value Store, since uniqueness/cascade-delete are exactly the kind of
integrity a KV store can't give you for free.

Per-user project access: `allowed_repos` is NULL for a role="admin" account
(every configured repo, including ones added later, with no ACL table to
keep in sync) and an explicit list for role="user" (e.g. a restricted
account scoped to exactly one project). Checked via `check_repo_access`,
called explicitly by every repo-scoped endpoint (agent/routers/*, pinned by
tests/test_repo_scope.py) -- not wired
through FastAPI's dependency-injection, since the existing endpoints take
`repo` in enough different shapes (query param, request body field, or
implied by an existing task/session's own stored repo) that one uniform DI
signature can't cover all of them without real risk of silently missing one.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Cookie, Depends, HTTPException, Request
from psycopg_pool import AsyncConnectionPool

from agent import features
from agent.config import Config

logger = logging.getLogger("tektonix")

SESSION_COOKIE_NAME = "agent_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
# A desktop install: the single-user app on a machine with its own login.
# Only the desktop app writes TEKTONIX_DESKTOP=1, into its own .env; a host
# install and the plain compose bundle never set it and keep every rule
# below as it is. In desktop mode the first account's second factor is
# optional (the machine's own login is the boundary), and a session lasts
# ninety days so the app stays signed in between launches.
DESKTOP_SESSION_TTL_SECONDS = 90 * 24 * 3600
_LOOPBACK_BINDS = frozenset({"", "127.0.0.1", "localhost", "::1"})


def desktop_install() -> bool:
    """Whether the desktop rules apply: the flag the app writes, and the
    premise behind it still true. The premise is that the machine's own
    login is the boundary, which stops holding when the dashboard is bound
    to anything but loopback (BIND_ADDRESS sits in the same .env, and the
    docs invite changing it) or when accounts beyond the first are licensed
    (a second admin is not the machine's owner). Either way the full
    sign-in is back: forced 2FA for admins, seven-day sessions."""
    if os.environ.get("TEKTONIX_DESKTOP", "").strip() != "1":
        return False
    bind = os.environ.get("BIND_ADDRESS", "").strip().strip("[]").lower()
    if bind not in _LOOPBACK_BINDS:
        return False
    return not features.enabled(features.MULTI_USER)


def session_ttl_seconds() -> int:
    return DESKTOP_SESSION_TTL_SECONDS if desktop_install() else SESSION_TTL_SECONDS
PENDING_2FA_TTL_SECONDS = 2 * 60
RECOVERY_CODE_COUNT = 10
# A numeric code emailed rather than a link, with a 30-minute expiry and a
# hard attempt cap so a leaked/guessed-at code can't be brute-forced
# indefinitely.
RESET_CODE_TTL_SECONDS = 30 * 60
RESET_CODE_MAX_ATTEMPTS = 5

# Cost params pinned explicitly (not left to library defaults) so a future
# argon2-cffi upgrade changing its defaults can't silently reduce the real
# work factor out from under already-stored hashes.
_hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    allowed_repos TEXT[],
    totp_secret_enc TEXT,
    totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    totp_last_used_step BIGINT,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Added 2026-08-23, after agent_users already existed in production --
-- CREATE TABLE IF NOT EXISTS alone never adds a column to an existing
-- table, so this needs its own idempotent migration statement.
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS auto_approve_commands BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS require_merge_review BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS telegram_bot_token TEXT;
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT;

-- Added 2026-09-11. Auto-approve used to be one global boolean: on meant on
-- for every project the account could reach. That was written for a single
-- operator who understood the sandbox, and it does not survive a second
-- account -- a new user handed the same switch inherits it everywhere at
-- once. NULL here means "never scoped" and is treated as no projects;
-- backfill_auto_approve_repos() resolves it once, at startup, for accounts
-- that already had the switch on, so nobody's behaviour changes silently.
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS auto_approve_repos TEXT[];

-- Added 2026-09-17. A colour scheme is a per-ACCOUNT preference, not a
-- per-browser one: an operator who picks Ember on the laptop should not be
-- handed Drafting by the phone. NULL means "never chosen", which the frontend
-- renders as the default rather than writing a row for everyone at once.
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS theme TEXT;

-- Web push endpoints, one row per BROWSER rather than per account: a push
-- subscription belongs to a specific install (this phone's Chrome, that
-- laptop's Firefox) and the same person legitimately has several. The
-- endpoint is the identity the push service issues and is unique by
-- construction, so a re-subscribe from the same browser updates in place
-- instead of accumulating duplicates that would each deliver the same alert.
-- ON DELETE CASCADE: removing an account must not leave endpoints behind that
-- still receive that account's alerts.
CREATE TABLE IF NOT EXISTS agent_push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    label TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ok_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_2fa_pending (
    temp_token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_recovery_codes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_password_resets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);
"""


@dataclass
class User:
    id: int
    email: str
    role: str  # "admin" | "user"
    allowed_repos: list[str] | None  # None == every repo (admin)
    totp_enabled: bool
    must_change_password: bool
    # Admin-settable per-user toggle (Users panel): when True, a task this
    # user creates skips deep_agent.py's HITL approval gate on bash/write/
    # edit calls entirely (still asks via ask_user, which is a real question
    # channel, not a safety gate) -- for an operator trusted to run genuinely
    # hands-free. Defaults False; a brand new/existing user is never
    # auto-approved until an admin opts them in explicitly.
    auto_approve_commands: bool = False
    require_merge_review: bool = True
    # Which projects the switch above actually covers. NULL/None means it was
    # never scoped, which is treated as NONE rather than all: a switch whose
    # blast radius nobody chose should not be the widest one.
    auto_approve_repos: list[str] | None = None
    # The account's colour scheme (agent/../frontend/src/theme.css). None means
    # never chosen; the frontend maps that to the default rather than storing a
    # copy of the default in every row.
    theme: str | None = None

    def can_access(self, repo: str) -> bool:
        return self.role == "admin" or self.allowed_repos is None or repo in self.allowed_repos

    def auto_approves(self, repo: str) -> bool:
        """Does this account skip the approval gate for THIS project?

        Both halves must say yes. The boolean is the operator's intent; the
        list is where they intended it. An admin who turned Auto on for a
        sandbox project does not thereby run unattended against production.
        """
        return bool(self.auto_approve_commands) and repo in (self.auto_approve_repos or [])



def check_repo_access(user: User, repo: str) -> None:
    if not user.can_access(repo):
        raise HTTPException(403, f"you don't have access to {repo!r}")


@asynccontextmanager
async def open_auth_pool(config: Config):
    from psycopg.rows import dict_row

    pool = AsyncConnectionPool(
        config.pg_dsn,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        # audit H-6: same liveness config as the checkpointer pool (graph.py),
        # so a Postgres restart doesn't strand dead connections and take auth
        # down with it -- the README already explains why the checkpointer has
        # this; the auth pool was simply missing it.
        check=AsyncConnectionPool.check_connection,
        max_idle=300,
        max_lifetime=1800,
        open=False,
    )
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            # psycopg's execute() refuses a multi-statement string outright
            # ("cannot insert multiple commands into a prepared statement")
            # -- each CREATE TABLE has to go in its own execute() call.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    await conn.execute(statement)
        yield pool
    finally:
        await pool.close()


def _fernet_key(config: Config) -> bytes:
    # AUTH_SECRET_KEY is a urlsafe-base64-encoded 32-byte key (see .env.example's
    # own comment on how it was generated) -- AESGCM wants the raw 32 bytes, not
    # the encoded form.
    #
    # audit M-4: validate the decoded length with an actionable message. The old
    # .env.example said `openssl rand -hex 32`, whose 64 hex chars base64-decode
    # to 48 bytes -- AESGCM then raised a bare "key must be 128, 192, or 256
    # bits" only on the FIRST 2FA setup, bricking admin onboarding (require_full
    # _auth won't let an admin past without totp_enabled). Fail loudly and early
    # with the exact fix instead.
    try:
        raw = base64.urlsafe_b64decode(config.auth_secret_key)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "AUTH_SECRET_KEY is not valid urlsafe-base64. Generate one with: "
            'python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"'
        ) from e
    if len(raw) not in (16, 24, 32):
        raise RuntimeError(
            f"AUTH_SECRET_KEY decodes to {len(raw)} bytes; AESGCM needs 16, 24, or 32. "
            "If you used `openssl rand -hex 32` (48 bytes), regenerate with: "
            'python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"'
        )
    return raw


def _encrypt_totp_secret(config: Config, secret: str) -> str:
    key = _fernet_key(config)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, secret.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def _decrypt_totp_secret(config: Config, enc: str) -> str:
    key = _fernet_key(config)
    raw = base64.urlsafe_b64decode(enc)
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


# audit M-1: a fixed argon2 hash to verify against when the account does not
# exist, so login runs the same expensive KDF whether or not the email is real.
# Without this, argon2 only ran for real accounts (~18ms) and short-circuited for
# unknown ones (~1ms) -- a 15x timing oracle that enumerates valid emails.
_DUMMY_PASSWORD_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


def verify_password_absent() -> bool:
    """Burn one argon2 verification against a dummy hash and return False.
    Called on the no-such-user branch of login to equalize timing (M-1)."""
    try:
        _hasher.verify(_DUMMY_PASSWORD_HASH, "timing-equalizer-wrong-guess")
    except VerifyMismatchError:
        pass
    return False


def validate_password_strength(password: str) -> str | None:
    """Returns an error message, or None if the password is acceptable."""
    if len(password) < 12:
        return "password must be at least 12 characters"
    if not any(c.islower() for c in password):
        return "password must include a lowercase letter"
    if not any(c.isupper() for c in password):
        return "password must include an uppercase letter"
    if not any(c.isdigit() for c in password):
        return "password must include a digit"
    return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row_to_user(row: dict) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        allowed_repos=row["allowed_repos"],
        totp_enabled=row["totp_enabled"],
        must_change_password=row["must_change_password"],
        auto_approve_commands=row["auto_approve_commands"],
        require_merge_review=row["require_merge_review"],
        auto_approve_repos=row.get("auto_approve_repos"),
        theme=row.get("theme"),
    )


async def get_user_by_email(pool: AsyncConnectionPool, email: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM agent_users WHERE email = %s", (email,))
        return await cur.fetchone()


async def get_user_by_id(pool: AsyncConnectionPool, user_id: int) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM agent_users WHERE id = %s", (user_id,))
        return await cur.fetchone()


async def list_users(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM agent_users ORDER BY created_at")
        return await cur.fetchall()


async def create_user(
    pool: AsyncConnectionPool, email: str, password: str, role: str, allowed_repos: list[str] | None,
    must_change_password: bool = False,
) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO agent_users (email, password_hash, role, allowed_repos, must_change_password)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (email, hash_password(password), role, allowed_repos, must_change_password),
        )
        return await cur.fetchone()


async def seed_admin_if_none(pool: AsyncConnectionPool, email: str) -> str | None:
    """Idempotent, safe to call on every startup (matches deep_agent.py's
    seed_memory/seed_org_memory convention) -- only ever acts the very first
    time this deployment has zero users at all. Returns the generated
    initial password (must_change_password=True forces it to be replaced on
    first login) if a user was actually created, otherwise None."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM agent_users")
        row = await cur.fetchone()
    if row["n"] > 0:
        return None
    password = secrets.token_urlsafe(18)
    await create_user(pool, email, password, "admin", None, must_change_password=True)
    return password


async def repo_auto_approves(pool: AsyncConnectionPool, repo: str) -> bool:
    """Does an operator have Auto on for this project?

    For work nobody typed -- a GitHub inbox task -- there is no request and no
    session, so there is no `user` to ask. This resolves the question from the
    accounts instead: True when an ADMIN has auto mode on AND has scoped it to
    this project, by the same two-part rule as User.auto_approves.

    Admins only, deliberately. Auto on an inbox task decides what an unattended
    run may do without a human present, and a per-user toggle on a non-admin
    account must not be able to widen that. The merge gate is unaffected
    either way -- server._github_create_task keeps it on unconditionally.

    Fails CLOSED: any error reading the accounts means prompting, because the
    failure mode of guessing True is an unattended task editing files nobody
    agreed to.
    """
    try:
        rows = await list_users(pool)
    except Exception:  # noqa: BLE001
        logger.warning("could not read accounts for %s auto-approve; prompting", repo)
        return False
    for row in rows or []:
        if (row.get("role") == "admin"
                and row.get("auto_approve_commands")
                and repo in (row.get("auto_approve_repos") or [])):
            return True
    return False


async def update_user_access(pool: AsyncConnectionPool, user_id: int, allowed_repos: list[str] | None) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE agent_users SET allowed_repos = %s WHERE id = %s", (allowed_repos, user_id))


async def update_auto_approve(pool: AsyncConnectionPool, user_id: int, auto_approve_commands: bool,
                              repos: list[str] | None = None) -> None:
    """Set the switch and, when given, the projects it covers.

    `repos=None` leaves the existing scope alone, which is what a caller that
    only wants to turn the switch OFF should do -- the list is worth keeping
    so turning it back on does not mean re-picking every project.
    """
    async with pool.connection() as conn:
        if repos is None:
            await conn.execute(
                "UPDATE agent_users SET auto_approve_commands = %s WHERE id = %s",
                (auto_approve_commands, user_id),
            )
        else:
            await conn.execute(
                "UPDATE agent_users SET auto_approve_commands = %s, auto_approve_repos = %s WHERE id = %s",
                (auto_approve_commands, sorted(set(repos)), user_id),
            )


async def backfill_auto_approve_repos(pool: AsyncConnectionPool, projects: list[str]) -> int:
    """One-time, idempotent: an account that had Auto on before the column
    existed keeps working exactly as it did, with the scope written down.

    Doing nothing would have silently turned Auto off for the operator who
    already relies on it -- a safe direction, but a surprising one, and they
    would have found out from a task that stopped to ask. Doing it on every
    startup is harmless: after the first run the column is non-NULL, and only
    NULL rows are touched.
    """
    if not projects:
        return 0
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE agent_users SET auto_approve_repos = %s "
            "WHERE auto_approve_commands AND auto_approve_repos IS NULL",
            (sorted(projects),),
        )
        return cur.rowcount or 0

async def update_telegram(pool: AsyncConnectionPool, user_id: int, bot_token: str | None, chat_id: str | None) -> None:
    """Set (or clear, with Nones/empties) a user's Telegram alert target.
    The token is a real secret -- it is stored here, returned ONLY through
    get_telegram_settings' masked shape, and never placed on the User
    dataclass, so no existing endpoint that serializes users can leak it."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET telegram_bot_token = %s, telegram_chat_id = %s WHERE id = %s",
            (bot_token or None, chat_id or None, user_id),
        )


async def get_telegram_settings(pool: AsyncConnectionPool, user_id: int) -> dict:
    """Masked view for the Settings page: whether a token exists, never the
    token itself. chat_id is not meaningfully secret and is shown for
    confirmation."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT telegram_bot_token, telegram_chat_id FROM agent_users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
    if not row:
        return {"configured": False, "chat_id": None}
    token, chat_id = row["telegram_bot_token"], row["telegram_chat_id"]
    return {"configured": bool(token and chat_id), "chat_id": chat_id}


async def get_telegram_raw(pool: AsyncConnectionPool, user_id: int) -> tuple[str, str] | None:
    """(token, chat_id) for ONE user, or None if incomplete -- only the
    send-test endpoint uses this; everything user-facing stays masked."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT telegram_bot_token, telegram_chat_id FROM agent_users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
    if not row or not row["telegram_bot_token"] or not row["telegram_chat_id"]:
        return None
    return (row["telegram_bot_token"], row["telegram_chat_id"])


async def update_telegram_chat_only(pool: AsyncConnectionPool, user_id: int, chat_id: str | None) -> None:
    """Change the chat id while keeping the stored token -- the Settings page
    can never send the real token back (it only ever sees the masked view)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET telegram_chat_id = %s WHERE id = %s", (chat_id, user_id)
        )


async def get_telegram_targets(pool: AsyncConnectionPool) -> list[tuple[str, str, str, list[str] | None]]:
    """Every configured Telegram recipient, WITH the scope it may hear about.

    audit H1: this used to return only (token, chat_id), so the fan-out sent
    every alert to everyone who had saved a bot token -- and task alerts embed
    the repo name, a goal excerpt and up to 1500 characters of escalation or
    exception detail. A user restricted to one repo received a live feed of
    every other project. The role and allowed_repos come back so the caller
    can filter; see notify.notify_operators.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT telegram_bot_token, telegram_chat_id, role, allowed_repos "
            "FROM agent_users "
            "WHERE telegram_bot_token IS NOT NULL AND telegram_chat_id IS NOT NULL"
        )
        rows = await cur.fetchall()
    return [(r["telegram_bot_token"], r["telegram_chat_id"], r["role"], r["allowed_repos"])
            for r in rows]


# Every scheme that exists in frontend/src/theme.css, and nothing else. The
# column is free text, so this list is the gate: an unknown value would leave
# the dashboard with a data-theme attribute that matches no rule, i.e. the
# default, silently -- the operator would pick a colour and watch nothing
# happen. tests/test_themes.py keeps this in step with the stylesheet.
THEMES = ("drafting", "indigo", "orchid", "ember", "moss")
DEFAULT_THEME = "drafting"


async def update_theme(pool: AsyncConnectionPool, user_id: int, theme: str) -> None:
    """Set the account's colour scheme. Rejects anything not in THEMES rather
    than storing it: the failure it prevents is a saved preference that does
    nothing, which reads as a broken Save button."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; choose one of {', '.join(THEMES)}")
    async with pool.connection() as conn:
        await conn.execute("UPDATE agent_users SET theme = %s WHERE id = %s", (theme, user_id))


# ---------------------------------------------------------------------------
# web push subscriptions
# ---------------------------------------------------------------------------

async def save_push_subscription(pool: AsyncConnectionPool, user_id: int, endpoint: str,
                                 p256dh: str, auth_key: str, label: str | None = None) -> None:
    """Store (or refresh) one browser's push endpoint.

    Upsert on the endpoint, not on the user: one account has as many
    subscriptions as it has browsers, and a browser that re-subscribes gets a
    NEW endpoint from the push service while the old one keeps working until
    it expires. Re-registering the same endpoint must update rather than
    duplicate, or every alert arrives twice.

    The user_id is part of the update on purpose: if a shared device is signed
    into a different account, the endpoint moves with it instead of continuing
    to deliver the previous account's alerts.
    """
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_push_subscriptions (endpoint, user_id, p256dh, auth, label) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_id = EXCLUDED.user_id, "
            "p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, label = EXCLUDED.label "
            # ...but only for the account that owns the row, or the browser
            # that holds its secret. Any signed-in account could otherwise
            # claim another's endpoint with fresh keys, and the victim's
            # browser could no longer decrypt its own alerts (2026-09-29).
            "WHERE agent_push_subscriptions.user_id = EXCLUDED.user_id "
            "OR agent_push_subscriptions.auth = EXCLUDED.auth",
            (endpoint, user_id, p256dh, auth_key, label))


async def delete_push_subscription(pool: AsyncConnectionPool, endpoint: str, *,
                                   user_id: int | None = None, auth_key: str | None = None) -> None:
    """Unscoped when the server prunes an endpoint the push service reported
    gone. A request from a user is scoped: the row must be theirs, or they
    must hold the subscription's own `auth` secret -- which only the browser
    that subscribed has, so a device signed into another account can still
    stop its own alerts but nobody can delete someone else's by endpoint."""
    sql = "DELETE FROM agent_push_subscriptions WHERE endpoint = %s"
    params: tuple = (endpoint,)
    if user_id is not None and auth_key:
        sql += " AND (user_id = %s OR auth = %s)"
        params = (endpoint, user_id, auth_key)
    elif user_id is not None:
        sql += " AND user_id = %s"
        params = (endpoint, user_id)
    async with pool.connection() as conn:
        await conn.execute(sql, params)


async def count_push_subscriptions(pool: AsyncConnectionPool, user_id: int) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM agent_push_subscriptions WHERE user_id = %s", (user_id,))
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def get_push_targets(pool: AsyncConnectionPool,
                           user_id: int | None = None) -> list[dict]:
    """Push endpoints WITH the scope each one may hear about.

    Same shape and the same reason as get_telegram_targets: alert bodies carry
    the repo name, a goal excerpt and failure detail, so the caller has to be
    able to filter by role/allowed_repos before sending. Joined rather than
    filtered in Python so a subscription whose account was deleted simply is
    not returned.
    """
    sql = ("SELECT s.endpoint, s.p256dh, s.auth, u.role, u.allowed_repos "
           "FROM agent_push_subscriptions s JOIN agent_users u ON u.id = s.user_id")
    params: tuple = ()
    if user_id is not None:
        sql += " WHERE s.user_id = %s"
        params = (user_id,)
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_push_ok(pool: AsyncConnectionPool, endpoint: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_push_subscriptions SET last_ok_at = now() WHERE endpoint = %s",
            (endpoint,))


async def update_require_merge_review(pool: AsyncConnectionPool, user_id: int, require_merge_review: bool) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET require_merge_review = %s WHERE id = %s", (require_merge_review, user_id)
        )


async def delete_user(pool: AsyncConnectionPool, user_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM agent_users WHERE id = %s", (user_id,))


async def change_password(pool: AsyncConnectionPool, user_id: int, new_password: str) -> int:
    """Sets the password, and removes what the old one could have planted:
    reset codes, every session, every passkey. Returns how many passkeys
    went, so the caller can say so."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
            (hash_password(new_password), user_id),
        )
        # An outstanding reset code would still set a new password over the
        # one just chosen -- the same reason the sessions below go.
        await conn.execute(
            "UPDATE agent_password_resets SET used_at = now() WHERE user_id = %s AND used_at IS NULL",
            (user_id,),
        )
        # A passkey signs in on its own, so one planted with a stolen
        # session outlived the reset meant to end that session's reach
        # (audit B27, 2026-10-22). The table is absent until
        # passkeys.ensure_schema has run once.
        removed = 0
        cur = await conn.execute("SELECT to_regclass('agent_passkeys') AS t")
        if (await cur.fetchone())["t"]:
            cur = await conn.execute("DELETE FROM agent_passkeys WHERE user_id = %s", (user_id,))
            removed = cur.rowcount or 0
    # audit H-4: revoke every other session on a password change, exactly as
    # reset_password does. A password change is often a response to a suspected
    # compromise; leaving other sessions live defeats the point. This was the
    # asymmetry the audit flagged -- reset_password got it right, this did not.
    await revoke_all_sessions(pool, user_id)
    return removed


# --- sessions ---------------------------------------------------------------


async def create_session(pool: AsyncConnectionPool, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    async with pool.connection() as conn:
        # SESSION_TTL_SECONDS is a fixed module constant, never user input --
        # safe to interpolate directly; token_hash/user_id are the real
        # per-call values and stay as proper psycopg %s params.
        await conn.execute(
            f"INSERT INTO agent_sessions (token_hash, user_id, expires_at) "
            f"VALUES (%s, %s, now() + interval '{session_ttl_seconds()} seconds')",
            (_hash_token(token), user_id),
        )
    return token


async def resolve_session(pool: AsyncConnectionPool, token: str) -> User | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT u.* FROM agent_sessions s JOIN agent_users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.expires_at > now()""",
            (_hash_token(token),),
        )
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def revoke_session(pool: AsyncConnectionPool, token: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM agent_sessions WHERE token_hash = %s", (_hash_token(token),))


async def revoke_all_sessions(pool: AsyncConnectionPool, user_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM agent_sessions WHERE user_id = %s", (user_id,))


# --- 2FA ---------------------------------------------------------------


async def create_pending_2fa(pool: AsyncConnectionPool, user_id: int) -> str:
    temp_token = secrets.token_urlsafe(32)
    async with pool.connection() as conn:
        await conn.execute(
            f"INSERT INTO agent_2fa_pending (temp_token_hash, user_id, expires_at) "
            f"VALUES (%s, %s, now() + interval '{PENDING_2FA_TTL_SECONDS} seconds')",
            (_hash_token(temp_token), user_id),
        )
    return temp_token


async def resolve_pending_2fa(pool: AsyncConnectionPool, temp_token: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM agent_2fa_pending WHERE temp_token_hash = %s AND expires_at > now()",
            (_hash_token(temp_token),),
        )
        row = await cur.fetchone()
        if row:
            await conn.execute("DELETE FROM agent_2fa_pending WHERE temp_token_hash = %s", (_hash_token(temp_token),))
    return row


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Persei")


async def start_totp_setup(pool: AsyncConnectionPool, config: Config, user_id: int) -> tuple[str, str]:
    """Generates a fresh secret (not yet committed as enabled -- confirm_totp_setup
    does that), returns (secret, provisioning_uri) for the frontend to render as a QR."""
    row = await get_user_by_id(pool, user_id)
    secret = new_totp_secret()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET totp_secret_enc = %s, totp_enabled = FALSE WHERE id = %s",
            (_encrypt_totp_secret(config, secret), user_id),
        )
    return secret, totp_provisioning_uri(secret, row["email"])


async def confirm_totp_setup(pool: AsyncConnectionPool, config: Config, user_id: int, code: str) -> list[str]:
    """Verifies the enrollment code, flips totp_enabled on, and issues fresh
    recovery codes (returned in plaintext ONCE -- only the hash is kept)."""
    row = await get_user_by_id(pool, user_id)
    if not row or not row["totp_secret_enc"]:
        raise HTTPException(400, "no pending 2FA setup for this account")
    secret = _decrypt_totp_secret(config, row["totp_secret_enc"])
    totp = pyotp.TOTP(secret)
    # The step the code was minted for, not the wall clock's: a code from
    # the next step recorded as this one could be replayed once at login
    # (the M-2 bug, left here until audit B30, 2026-10-22).
    matched_step = _matched_totp_step(totp, code)
    if matched_step is None:
        raise HTTPException(400, "invalid code")
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODE_COUNT)]
    async with pool.connection() as conn:
        # Records this step as already-used -- otherwise the same code just
        # entered to complete enrollment could immediately be replayed for a
        # real login (verify_totp_or_recovery's own replay guard only ever
        # sees totp_last_used_step, which enrollment never touched before).
        await conn.execute(
            "UPDATE agent_users SET totp_enabled = TRUE, totp_last_used_step = %s WHERE id = %s",
            (matched_step, user_id),
        )
        await conn.execute("DELETE FROM agent_recovery_codes WHERE user_id = %s", (user_id,))
        for code_str in codes:
            await conn.execute(
                "INSERT INTO agent_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                (user_id, _hash_token(code_str)),
            )
    return codes


async def disable_totp(pool: AsyncConnectionPool, user_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_users SET totp_enabled = FALSE, totp_secret_enc = NULL, totp_last_used_step = NULL WHERE id = %s",
            (user_id,),
        )
        await conn.execute("DELETE FROM agent_recovery_codes WHERE user_id = %s", (user_id,))


def _matched_totp_step(totp: "pyotp.TOTP", code: str) -> int | None:
    """Return the time-step the given code was minted for (S-1, S, or S+1,
    matching valid_window=1's acceptance range), or None if it matches none.
    Each candidate is checked with valid_window=0 so the exact step is known --
    that step, not the wall-clock step, is what the replay ratchet records
    (audit M-2). Constant time over the three candidates (pyotp.verify uses an
    hmac compare); no early return on the first match.
    """
    now_step = int(time.time()) // totp.interval
    matched: int | None = None
    for step in (now_step - 1, now_step, now_step + 1):
        if totp.verify(code, for_time=step * totp.interval, valid_window=0):
            matched = step
    return matched


async def verify_totp_or_recovery(pool: AsyncConnectionPool, config: Config, user_id: int, code: str) -> bool:
    row = await get_user_by_id(pool, user_id)
    if not row or not row["totp_enabled"] or not row["totp_secret_enc"]:
        return False
    secret = _decrypt_totp_secret(config, row["totp_secret_enc"])
    totp = pyotp.TOTP(secret)
    # audit M-2: identify the step the code was MINTED for, not the step at
    # verification time. The old guard stored current_step and only rejected an
    # exact re-submit of that same step, so a code minted for step S (which
    # verify(valid_window=1) accepts at S-1, S, S+1) verified at S, stored S,
    # then replayed at S+1 computed current_step=S+1 != S and passed the guard --
    # a real replay window of ~60s. Now each candidate step is checked with
    # valid_window=0, the MATCHED step is stored, and any code whose step is
    # <= the last consumed step is rejected (monotonic ratchet).
    matched_step = _matched_totp_step(totp, code)
    if matched_step is not None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE agent_users SET totp_last_used_step = %s
                   WHERE id = %s AND (totp_last_used_step IS NULL OR totp_last_used_step < %s)""",
                (matched_step, user_id, matched_step),
            )
            return bool(cur.rowcount)
    # TOTP didn't match at all (wrong or expired code) -- fall back to a one-time recovery code.
    # One conditional UPDATE, so two logins racing with the same code cannot
    # both read it unused before either marks it.
    code_hash = _hash_token(code.strip())
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE agent_recovery_codes SET used_at = now()
               WHERE id = (SELECT id FROM agent_recovery_codes
                           WHERE user_id = %s AND code_hash = %s AND used_at IS NULL LIMIT 1)
                 AND used_at IS NULL""",
            (user_id, code_hash),
        )
        return bool(cur.rowcount)


# --- password reset ----------------------------------------------------


async def request_password_reset(pool: AsyncConnectionPool, config: Config, email: str) -> None:
    """Always succeeds from the caller's point of view (the route in
    agent/routers/auth.py returns {"ok": true} unconditionally) -- silently no-ops
    if the email doesn't match a real account, the same anti-enumeration
    behavior any password-reset flow needs. Only ever sends real email
    if a matching user actually exists.
    """
    row = await get_user_by_email(pool, email.strip().lower())
    if not row:
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with pool.connection() as conn:
        await conn.execute(
            f"INSERT INTO agent_password_resets (user_id, code_hash, expires_at) "
            f"VALUES (%s, %s, now() + interval '{RESET_CODE_TTL_SECONDS} seconds')",
            (row["id"], _hash_token(code)),
        )
    from agent.mailer import send_password_reset_email

    await send_password_reset_email(config, row["email"], code)


async def reset_password(pool: AsyncConnectionPool, email: str, code: str, new_password: str) -> bool:
    """Verifies the emailed code (single-use, ≤5 attempts, 30-minute expiry)
    and, if valid, sets the new password -- also revoking every existing
    session for this account (a reset is exactly the moment you can no
    longer assume the old password, and whatever session it created, is
    still trustworthy) and clearing must_change_password (the new password
    the operator just chose IS the real one, nothing left to force).
    Returns False for any invalid/expired/exhausted code, without
    distinguishing why (same anti-enumeration posture as the request side).
    """
    row = await get_user_by_email(pool, email.strip().lower())
    if not row:
        return False
    async with pool.connection() as conn:
        # The attempt is counted BEFORE the guess is compared, in the same
        # statement that checks the limit: a burst of concurrent guesses each
        # used to read attempts < max and all get compared, so the limit of
        # five guesses at a six-digit code was only a limit on sequential ones.
        cur = await conn.execute(
            """UPDATE agent_password_resets SET attempts = attempts + 1
               WHERE id = (SELECT id FROM agent_password_resets WHERE user_id = %s AND used_at IS NULL
                           AND expires_at > now() AND attempts < %s ORDER BY id DESC LIMIT 1)
                 AND used_at IS NULL AND attempts < %s
               RETURNING id, code_hash""",
            (row["id"], RESET_CODE_MAX_ATTEMPTS, RESET_CODE_MAX_ATTEMPTS),
        )
        reset_row = await cur.fetchone()
        if not reset_row:
            return False
        if not hmac.compare_digest(reset_row["code_hash"], _hash_token(code.strip())):
            return False
        cur = await conn.execute(
            "UPDATE agent_password_resets SET used_at = now() WHERE id = %s AND used_at IS NULL",
            (reset_row["id"],))
        if not cur.rowcount:
            return False   # a concurrent reset with the same code got there first
    await change_password(pool, row["id"], new_password)
    await revoke_all_sessions(pool, row["id"])
    return True


# --- FastAPI wiring ---------------------------------------------------------


async def get_current_user(
    request: Request,
    agent_session: str | None = Cookie(default=None),
) -> User:
    # The cookie check comes first on purpose: reading app.state before it
    # answers 401 turns a request that arrives during startup -- or any
    # unauthenticated request in a context where the pool was never attached
    # -- into a 500 with a KeyError, which reads like a broken server rather
    # than a missing login.
    if not agent_session:
        raise HTTPException(401, "not logged in")
    # ...and a request that DOES carry a cookie, arriving in the same window,
    # would have hit the same KeyError. It is not a 401 (the cookie may be
    # perfectly good) and it is not a 500 (nothing is broken) -- the database
    # pool simply is not up yet. 503 with Retry-After is the answer a browser
    # and a monitoring box both understand, and the dashboard's own reconnect
    # logic already treats it as "try again shortly".
    pool = getattr(request.app.state, "auth_pool", None)
    if pool is None:
        raise HTTPException(503, "the server is still starting up", headers={"Retry-After": "2"})
    user = await resolve_session(pool, agent_session)
    if not user:
        raise HTTPException(401, "session expired or invalid")
    return user


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "admin only")


def ws_origin_allowed(headers) -> bool:
    """Whether a WebSocket handshake comes from the console's own page.

    A WebSocket is not bound by the same-origin policy, and on loopback
    SameSite=strict does not help: cookies ignore ports, and another
    localhost port is the same site, so a dev server's page on
    127.0.0.1:5000 sent agent_session and opened a task stream (audit B31,
    2026-10-22). A browser always sends Origin on a handshake and cannot
    forge Host, so the two must name the same host -- and the same port
    when Host carries one (nginx's $host does not). No Origin is not a
    browser, and a cookie stolen outright is not what this guards."""
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True
    from urllib.parse import urlsplit  # noqa: PLC0415

    try:
        o = urlsplit(origin)
        h = urlsplit("//" + (headers.get("host") or "").strip())
        o_host, h_host = (o.hostname or "").lower(), (h.hostname or "").lower()
        o_port, h_port = o.port or (443 if o.scheme == "https" else 80), h.port
    except ValueError:
        return False
    if not o_host or o_host != h_host:
        return False
    return h_port is None or h_port == o_port


async def get_user_from_ws_cookie(pool: AsyncConnectionPool, cookies: dict) -> User | None:
    token = cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return await resolve_session(pool, token)


# ---------------------------------------------------------------------------
# the two forced screens
# ---------------------------------------------------------------------------
#
# These lived in agent/server.py until the router split started. They are pure
# policy over a User and nothing about them is server-specific -- but a route
# module under agent/routers/ cannot import them from server.py, because
# server.py includes those routers and the import is a cycle. Here they are
# reachable from both sides.
#
# server.py re-exports require_full_auth rather than redefining it, so it
# stays the SAME function object: tests/test_route_inventory.py identifies a
# route's guard by that object's __name__, and dozens of tests override it
# through app.dependency_overrides, which is keyed by identity.


def forced_screen_block(user: User) -> str | None:
    """Return a reason string if `user` is parked behind a forced screen, else
    None. Factored out (audit H-1) so the WebSocket handlers enforce the exact
    same two gates as require_full_auth -- previously they authenticated only
    the session and let a must-change-password / no-2FA account open the live
    task and planning streams and watch tool-call arguments and results.
    """
    if user.must_change_password:
        return "password change required before using this"
    if user.role == "admin" and not user.totp_enabled and not desktop_install():
        return "2FA setup required before using this"
    return None


async def require_full_auth(user: User = Depends(get_current_user)) -> User:
    # Both forced-screen flags are enforced server-side here, not only via
    # the frontend routing app.tsx does for the
    # same two flags -- a session cookie alone would otherwise be enough to
    # reach every real endpoint directly, skipping both forced screens
    # entirely (the same "nginx allows all IPs" reasoning that makes
    # frontend-only gating unsafe applies here too). 2FA is admin-only
    # (never mandatory for a role="user" account, see this module's own
    # docstring); a temporary/generated password must always be replaced
    # before anything else, regardless of role.
    blocked = forced_screen_block(user)
    if blocked:
        raise HTTPException(403, blocked)
    return user
