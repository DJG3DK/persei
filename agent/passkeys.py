"""Passkeys (WebAuthn) for the agent's sign-in.

A passkey is a second way in beside email + password + authenticator code,
not a replacement for it: the code stays as the backup the operator asked
for (2026-10-01). A passkey sign-in requires user verification -- the
device's own fingerprint, face or PIN -- so on its own it carries both
factors, and replaces password plus authenticator code for any account,
admins included. It is bound to its relying-party id, so a page on another
site cannot use it; a page on a sibling of the console under that id can,
which is why the expected origin is the console's own and not whatever the
request says.

The relying-party id is the host the browser is on (agent.example.com, or
localhost for the desktop app), unless WEBAUTHN_RP_ID names a parent domain
to share passkeys across subdomains. The expected origin is the console's
own address: WEBAUTHN_ORIGINS when set; otherwise the page's host must be
the rp id itself, and on localhost the port must be the one the console
answers on. Until 2026-10-22 it was the request's Origin header, checked
only against the rp id: a hostile page on chat.example.com (rp id
example.com) or on localhost:5173 could run the ceremony and relay the
assertion with a forged Origin, and was signed in without password or code
(audit B26). A credential also only answers for the rp id it was
registered under, which is stored with it and checked again here.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from psycopg_pool import AsyncConnectionPool
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from agent.auth import _hash_token

RP_NAME = "Persei"
CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS_PER_USER = 10
NAME_MAX = 60

SCHEMA = """
ALTER TABLE agent_users ADD COLUMN IF NOT EXISTS webauthn_handle BYTEA;

CREATE TABLE IF NOT EXISTS agent_passkeys (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES agent_users(id) ON DELETE CASCADE,
    credential_id BYTEA UNIQUE NOT NULL,
    public_key BYTEA NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    transports TEXT[],
    rp_id TEXT NOT NULL,
    name TEXT NOT NULL,
    backed_up BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_webauthn_challenges (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES agent_users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('register', 'login')),
    challenge BYTEA NOT NULL,
    rp_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
)
"""


class PasskeyError(ValueError):
    """A refusal whose text is safe to show the person signing in."""


@dataclass(frozen=True)
class RelyingParty:
    rp_id: str
    origin: str


_LOCAL_HOSTS = {"localhost"}


def is_local_host(host: str) -> bool:
    """localhost, or a name under it (persei.localhost, which INSTALL §3a
    suggests so the console's cookie is not shared with every port)."""
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def configured_origins() -> frozenset[str]:
    """WEBAUTHN_ORIGINS: the addresses the console is served at, as
    scheme://host[:port], comma-separated."""
    out = set()
    for part in (os.environ.get("WEBAUTHN_ORIGINS") or "").split(","):
        parts = urlsplit(part.strip())
        if parts.scheme and parts.netloc:
            out.add(f"{parts.scheme.lower()}://{parts.netloc.lower()}")
    return frozenset(out)


def relying_party(origin_header: str | None, *, local_console: str | None = None) -> RelyingParty:
    """The rp id and expected origin for this request. The browser's Origin
    header says which page asked; it is accepted only when that page is the
    console's own (see the module docstring). `local_console` is the
    host:port the console answers on locally, when the caller knows it from
    the server's side; without it no localhost page is the console.
    An IP address cannot be an rp id, and a plain-http origin is only a
    secure context on localhost."""
    if not origin_header:
        raise PasskeyError("the browser sent no Origin; passkeys need a normal page load")
    parts = urlsplit(origin_header)
    host = (parts.hostname or "").lower()
    if not host or parts.path not in ("", "/") or parts.query:
        raise PasskeyError("unexpected Origin")
    if host.replace(".", "").isdigit() or ":" in host:
        raise PasskeyError("passkeys need the dashboard on a host name, not an IP address")
    if parts.scheme != "https" and not (parts.scheme == "http" and is_local_host(host)):
        raise PasskeyError("passkeys need https (or localhost)")
    configured = (os.environ.get("WEBAUTHN_RP_ID") or "").strip().lower()
    if configured:
        if host != configured and not host.endswith("." + configured):
            raise PasskeyError(f"this page is not on {configured}, which passkeys here are made for")
        rp_id = configured
    else:
        rp_id = host
    origin = f"{parts.scheme}://{parts.netloc.lower()}"
    allowed = configured_origins()
    if allowed:
        if origin not in allowed:
            raise PasskeyError("this page is not the dashboard's address (WEBAUTHN_ORIGINS)")
    elif is_local_host(host):
        # rp ids ignore ports: every program on this machine serving a page
        # on localhost shares this one.
        if not local_console or parts.netloc.lower() != local_console.lower():
            raise PasskeyError("this page is not the dashboard; passkeys work only on the dashboard's own address")
    elif host != rp_id:
        raise PasskeyError(f"WEBAUTHN_RP_ID shares passkeys across {rp_id}; name the dashboard's own address "
                           f"in WEBAUTHN_ORIGINS")
    return RelyingParty(rp_id=rp_id, origin=origin)


async def ensure_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                await conn.execute(statement)


async def _user_handle(pool: AsyncConnectionPool, user_id: int) -> bytes:
    """An opaque per-account handle for the authenticator, made once."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT webauthn_handle FROM agent_users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
        handle = row and row["webauthn_handle"]
        if handle:
            return bytes(handle)
        handle = secrets.token_bytes(32)
        await conn.execute(
            "UPDATE agent_users SET webauthn_handle = %s WHERE id = %s AND webauthn_handle IS NULL",
            (handle, user_id))
        cur = await conn.execute("SELECT webauthn_handle FROM agent_users WHERE id = %s", (user_id,))
        return bytes((await cur.fetchone())["webauthn_handle"])


async def _store_challenge(pool: AsyncConnectionPool, *, kind: str, challenge: bytes, rp: RelyingParty,
                           user_id: int | None) -> str:
    challenge_id = secrets.token_urlsafe(24)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM agent_webauthn_challenges WHERE expires_at < now()")
        await conn.execute(
            f"INSERT INTO agent_webauthn_challenges (id_hash, user_id, kind, challenge, rp_id, origin, expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, now() + interval '{CHALLENGE_TTL_SECONDS} seconds')",
            (_hash_token(challenge_id), user_id, kind, challenge, rp.rp_id, rp.origin))
    return challenge_id


async def _take_challenge(pool: AsyncConnectionPool, challenge_id: str, kind: str) -> dict:
    """The challenge, deleted as it is read: each one answers exactly once."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_webauthn_challenges WHERE id_hash = %s AND kind = %s AND expires_at > now() "
            "RETURNING user_id, challenge, rp_id, origin",
            (_hash_token(challenge_id or ""), kind))
        row = await cur.fetchone()
    if not row:
        raise PasskeyError("that passkey request expired; try again")
    return row


# --- registration ------------------------------------------------------------

async def registration_options(pool: AsyncConnectionPool, rp: RelyingParty, user_id: int, email: str) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT credential_id FROM agent_passkeys WHERE user_id = %s AND rp_id = %s", (user_id, rp.rp_id))
        existing = [bytes(r["credential_id"]) for r in await cur.fetchall()]
    if len(existing) >= MAX_PASSKEYS_PER_USER:
        raise PasskeyError(f"an account can hold {MAX_PASSKEYS_PER_USER} passkeys; remove one first")
    options = generate_registration_options(
        rp_id=rp.rp_id, rp_name=RP_NAME, user_id=await _user_handle(pool, user_id),
        user_name=email, user_display_name=email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        # The same device twice is refused by the browser itself.
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in existing],
    )
    challenge_id = await _store_challenge(pool, kind="register", challenge=options.challenge, rp=rp, user_id=user_id)
    return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}


def clean_name(name: str | None) -> str:
    name = " ".join(str(name or "").split())[:NAME_MAX]
    return name or "Passkey"


async def register(pool: AsyncConnectionPool, rp: RelyingParty, user_id: int, challenge_id: str,
                   credential: dict, name: str | None) -> dict:
    row = await _take_challenge(pool, challenge_id, "register")
    if row["user_id"] != user_id or row["rp_id"] != rp.rp_id or row["origin"] != rp.origin:
        raise PasskeyError("that passkey request belongs to another sign-in")
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=bytes(row["challenge"]),
            expected_rp_id=rp.rp_id, expected_origin=rp.origin, require_user_verification=True)
    except Exception as e:  # noqa: BLE001 -- the library's own messages are specific and safe
        raise PasskeyError(f"the passkey could not be verified: {e}") from e
    transports = ((credential.get("response") or {}).get("transports") or []) if isinstance(credential, dict) else []
    transports = [str(t)[:20] for t in transports if isinstance(t, str)][:6]
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                "INSERT INTO agent_passkeys (user_id, credential_id, public_key, sign_count, transports, rp_id, "
                "name, backed_up) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, name, created_at",
                (user_id, verified.credential_id, verified.credential_public_key, verified.sign_count,
                 transports, rp.rp_id, clean_name(name), bool(verified.credential_backed_up)))
        except Exception as e:  # noqa: BLE001 -- a unique violation: this device is already registered
            raise PasskeyError("that passkey is already registered") from e
        saved = await cur.fetchone()
    return {"id": saved["id"], "name": saved["name"], "created_at": saved["created_at"].isoformat()}


# --- sign-in ----------------------------------------------------------------

async def login_options(pool: AsyncConnectionPool, rp: RelyingParty) -> dict:
    """No account named: the browser offers whichever passkey for this site
    the person has (a discoverable credential), so nothing here reveals which
    emails exist."""
    options = generate_authentication_options(
        rp_id=rp.rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    challenge_id = await _store_challenge(pool, kind="login", challenge=options.challenge, rp=rp, user_id=None)
    return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}


async def login(pool: AsyncConnectionPool, rp: RelyingParty, challenge_id: str, credential: dict) -> int:
    """The signed-in account's id, or PasskeyError. Same message for every
    failure an outsider could probe, so it is no oracle for which
    credentials exist."""
    row = await _take_challenge(pool, challenge_id, "login")
    if row["rp_id"] != rp.rp_id or row["origin"] != rp.origin:
        raise PasskeyError("that passkey request belongs to another sign-in")
    refused = PasskeyError("that passkey was not accepted")
    try:
        raw_id = base64url_to_bytes(str(credential.get("rawId") or credential.get("id") or ""))
    except Exception as e:  # noqa: BLE001
        raise refused from e
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT p.id, p.user_id, p.public_key, p.sign_count, u.webauthn_handle FROM agent_passkeys p "
            "JOIN agent_users u ON u.id = p.user_id WHERE p.credential_id = %s AND p.rp_id = %s",
            (raw_id, rp.rp_id))
        key = await cur.fetchone()
    if not key:
        raise refused
    handle = ((credential.get("response") or {}).get("userHandle"))
    try:
        mismatched = bool(handle and key["webauthn_handle"]
                          and base64url_to_bytes(str(handle)) != bytes(key["webauthn_handle"]))
    except Exception as e:  # noqa: BLE001 -- a malformed handle was a 500 (audit B34)
        raise refused from e
    if mismatched:
        raise refused
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=bytes(row["challenge"]), expected_rp_id=rp.rp_id,
            expected_origin=rp.origin, credential_public_key=bytes(key["public_key"]),
            credential_current_sign_count=int(key["sign_count"]), require_user_verification=True)
    except Exception as e:  # noqa: BLE001 -- includes a sign counter that went backwards (a cloned key)
        raise refused from e
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_passkeys SET sign_count = %s, last_used_at = now() WHERE id = %s",
            (verified.new_sign_count, key["id"]))
    return int(key["user_id"])


# --- management --------------------------------------------------------------

async def list_for(pool: AsyncConnectionPool, user_id: int) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, rp_id, backed_up, created_at, last_used_at FROM agent_passkeys "
            "WHERE user_id = %s ORDER BY created_at", (user_id,))
        rows = await cur.fetchall()
    return [{"id": r["id"], "name": r["name"], "site": r["rp_id"], "synced": r["backed_up"],
             "created_at": r["created_at"].isoformat(),
             "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None} for r in rows]


async def remove(pool: AsyncConnectionPool, user_id: int, passkey_id: int) -> str | None:
    """The removed passkey's name, or None when this account has no such key."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_passkeys WHERE id = %s AND user_id = %s RETURNING name", (passkey_id, user_id))
        row = await cur.fetchone()
    return row["name"] if row else None


async def rename(pool: AsyncConnectionPool, user_id: int, passkey_id: int, name: str) -> str | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE agent_passkeys SET name = %s WHERE id = %s AND user_id = %s RETURNING name",
            (clean_name(name), passkey_id, user_id))
        row = await cur.fetchone()
    return row["name"] if row else None


__all__ = ["PasskeyError", "RelyingParty", "relying_party", "ensure_schema", "registration_options", "register",
           "login_options", "login", "list_for", "remove", "rename", "bytes_to_base64url"]
