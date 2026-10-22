"""Passkeys on the agent's sign-in (agent/passkeys.py, agent/routers/passkeys.py).

A software authenticator below makes real P-256 credentials and real
signatures, so these drive the library's actual verification end to end:
register, sign in, the counter, replay, a wrong site, a wrong account's
challenge, and the password asked for before a passkey is added.

The routes run against a throwaway Postgres schema (AGENT_TEST_PG_DSN, as
tests/test_auth_races.py); the origin and rp-id rules run without one.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import uuid
from types import SimpleNamespace

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from agent import auth, passkeys

PG_DSN = os.environ.get("AGENT_TEST_PG_DSN")
ORIGIN = "https://agent.example.com"


# ---------------------------------------------------------------------------
# a software authenticator
# ---------------------------------------------------------------------------

class SoftAuthenticator:
    """One platform authenticator holding discoverable credentials."""

    def __init__(self):
        self.creds: dict[bytes, dict] = {}

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str = ORIGIN, *, uv: bool = True) -> dict:
        key = ec.generate_private_key(ec.SECP256R1())
        nums = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        cred_id = os.urandom(32)
        rp_id = options["rp"]["id"]
        flags = 0x01 | (0x04 if uv else 0) | 0x40           # UP, UV, AT
        auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
                     + b"\x00" * 16 + struct.pack(">H", len(cred_id)) + cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        self.creds[cred_id] = {"key": key, "rp_id": rp_id, "count": 0,
                               "handle": base64url_to_bytes(options["user"]["id"])}
        cdj = self._client_data("webauthn.create", options["challenge"], origin)
        return {"id": bytes_to_base64url(cred_id), "rawId": bytes_to_base64url(cred_id), "type": "public-key",
                "response": {"clientDataJSON": bytes_to_base64url(cdj), "attestationObject": bytes_to_base64url(att),
                             "transports": ["internal", "hybrid"]},
                "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = ORIGIN, *, cred_id: bytes | None = None, uv: bool = True,
            counter: int | None = None) -> dict:
        cred_id = cred_id or next(c for c, v in self.creds.items() if v["rp_id"] == options["rpId"])
        c = self.creds[cred_id]
        c["count"] = c["count"] + 1 if counter is None else counter
        flags = 0x01 | (0x04 if uv else 0)
        auth_data = hashlib.sha256(options["rpId"].encode()).digest() + bytes([flags]) + struct.pack(">I", c["count"])
        cdj = self._client_data("webauthn.get", options["challenge"], origin)
        sig = c["key"].sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(cred_id), "rawId": bytes_to_base64url(cred_id), "type": "public-key",
                "response": {"clientDataJSON": bytes_to_base64url(cdj), "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig), "userHandle": bytes_to_base64url(c["handle"])},
                "clientExtensionResults": {}}


# ---------------------------------------------------------------------------
# the site rules, no database needed
# ---------------------------------------------------------------------------

def test_the_rp_id_is_the_pages_host(monkeypatch):
    monkeypatch.delenv("WEBAUTHN_RP_ID", raising=False)
    rp = passkeys.relying_party("https://agent.example.com")
    assert rp == passkeys.RelyingParty("agent.example.com", "https://agent.example.com")
    assert passkeys.relying_party("http://localhost:8100", local_console="localhost:8100") == \
        passkeys.RelyingParty("localhost", "http://localhost:8100")


@pytest.mark.parametrize("origin, why", [
    (None, "no Origin"), ("http://agent.example.com", "https"), ("https://10.0.0.5", "IP address"),
    ("https://[::1]", "IP address"), ("https://agent.example.com/path", "unexpected"),
])
def test_origins_a_passkey_cannot_work_on_are_refused_plainly(monkeypatch, origin, why):
    monkeypatch.delenv("WEBAUTHN_RP_ID", raising=False)
    with pytest.raises(passkeys.PasskeyError) as e:
        passkeys.relying_party(origin)
    assert why.lower() in str(e.value).lower()


def test_a_configured_parent_domain_is_shared_by_its_subdomains_only(monkeypatch):
    monkeypatch.setenv("WEBAUTHN_RP_ID", "example.com")
    monkeypatch.setenv("WEBAUTHN_ORIGINS", "https://agent.example.com, https://example.com")
    assert passkeys.relying_party("https://agent.example.com").rp_id == "example.com"
    assert passkeys.relying_party("https://example.com").rp_id == "example.com"
    with pytest.raises(passkeys.PasskeyError):
        passkeys.relying_party("https://badexample.com")


def test_a_sibling_under_a_parent_rp_id_is_not_the_console(monkeypatch):
    """B26 (2026-10-22): with a parent rp id, any subdomain's Origin was
    taken as the expected one, so a page on chat.example.com could run the
    ceremony and relay the assertion with that Origin."""
    monkeypatch.setenv("WEBAUTHN_RP_ID", "example.com")
    monkeypatch.setenv("WEBAUTHN_ORIGINS", "https://agent.example.com")
    with pytest.raises(passkeys.PasskeyError, match="WEBAUTHN_ORIGINS"):
        passkeys.relying_party("https://chat.example.com")
    monkeypatch.delenv("WEBAUTHN_ORIGINS")
    with pytest.raises(passkeys.PasskeyError, match="WEBAUTHN_ORIGINS"):
        passkeys.relying_party("https://agent.example.com")


@pytest.mark.parametrize("origin, console", [
    ("http://localhost:5173", "localhost:8100"),
    ("http://localhost:8100", None),
    ("http://localhost", "localhost:8100"),
])
def test_another_localhost_port_is_not_the_console(monkeypatch, origin, console):
    monkeypatch.delenv("WEBAUTHN_RP_ID", raising=False)
    monkeypatch.delenv("WEBAUTHN_ORIGINS", raising=False)
    with pytest.raises(passkeys.PasskeyError, match="not the dashboard"):
        passkeys.relying_party(origin, local_console=console)


@pytest.mark.parametrize("host, server, bind, expected", [
    ("localhost:8100", ("127.0.0.1", 8100), "", "localhost:8100"),
    ("localhost:5173", ("127.0.0.1", 8100), "", None),
    ("localhost:8200", ("172.18.0.4", 8100), "127.0.0.1", "localhost:8200"),
    ("localhost:8200", ("172.18.0.4", 8100), "0.0.0.0", None),
    ("persei.localhost:8100", ("127.0.0.1", 8100), "", "persei.localhost:8100"),
    ("agent.example.com", ("127.0.0.1", 8100), "", None),
])
def test_the_local_console_comes_from_the_server_side(monkeypatch, host, server, bind, expected):
    from agent.routers import passkeys as routes
    monkeypatch.setenv("BIND_ADDRESS", bind)
    req = SimpleNamespace(headers={"host": host}, scope={"server": server})
    assert routes._local_console(req) == expected


def test_a_passkey_name_is_trimmed_and_never_empty():
    assert passkeys.clean_name("  Danny's   phone ") == "Danny's phone"
    assert passkeys.clean_name("") == "Passkey"
    assert len(passkeys.clean_name("x" * 500)) == passkeys.NAME_MAX


# ---------------------------------------------------------------------------
# the routes, against a throwaway schema
# ---------------------------------------------------------------------------

@pytest.fixture
async def app_client(monkeypatch):
    if not PG_DSN:
        pytest.skip("set AGENT_TEST_PG_DSN to a throwaway database")
    import httpx
    import psycopg
    from fastapi import FastAPI

    from agent import rate_limit
    from agent.routers import auth as auth_routes
    from agent.routers import passkeys as passkey_routes

    monkeypatch.delenv("WEBAUTHN_RP_ID", raising=False)
    monkeypatch.delenv("WEBAUTHN_ORIGINS", raising=False)
    rate_limit.reset_rate_limits()
    schema = f"passkeys_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    dsn = f"{PG_DSN}{sep}options=-c%20search_path%3D{schema}"
    recorded = []

    async def record(store, **kw):
        recorded.append(kw)
    monkeypatch.setattr(passkey_routes.audit, "record", record)
    monkeypatch.setattr(passkey_routes, "audit_store", lambda request: None)
    try:
        async with auth.open_auth_pool(SimpleNamespace(pg_dsn=dsn)) as pool:
            await passkeys.ensure_schema(pool)
            app = FastAPI()
            app.state.auth_pool = pool
            app.state.config = SimpleNamespace()
            app.include_router(auth_routes.router)
            app.include_router(passkey_routes.router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
                c.pool, c.recorded = pool, recorded
                yield c
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


async def _signed_in(c, email="danny@example.com", password="Correct-Horse-9"):
    row = await auth.create_user(c.pool, email, password, "user", [])
    token = await auth.create_session(c.pool, row["id"])
    c.cookies.set(auth.SESSION_COOKIE_NAME, token)
    return row


async def _add_passkey(c, authn, password="Correct-Horse-9", name="Danny's phone", origin=ORIGIN):
    r = await c.post("/api/auth/passkeys/register/options", json={"password": password})
    assert r.status_code == 200, r.text
    opts = r.json()
    cred = authn.create(opts["options"], origin)
    r = await c.post("/api/auth/passkeys/register/verify",
                     json={"challenge_id": opts["challenge_id"], "credential": cred, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def _passkey_login(c, authn, **kw):
    c.cookies.clear()
    r = await c.post("/api/auth/passkeys/login/options")
    assert r.status_code == 200, r.text
    opts = r.json()
    cred = authn.get(opts["options"], **kw)
    return await c.post("/api/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"], "credential": cred})


async def test_add_a_passkey_then_sign_in_with_it_alone(app_client):
    c, authn = app_client, SoftAuthenticator()
    user = await _signed_in(c)
    saved = await _add_passkey(c, authn)
    assert saved["name"] == "Danny's phone"
    listed = (await c.get("/api/auth/passkeys")).json()["passkeys"]
    assert [(p["name"], p["site"], p["last_used_at"]) for p in listed] == [("Danny's phone", "agent.example.com", None)]
    assert c.recorded[-1]["action"] == "auth.passkey_add"

    r = await _passkey_login(c, authn)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == user["email"] and r.json()["requires_2fa"] is False
    assert auth.SESSION_COOKIE_NAME in r.cookies, "a session, exactly as the password login sets one"
    c.cookies.set(auth.SESSION_COOKIE_NAME, r.cookies[auth.SESSION_COOKIE_NAME])
    assert (await c.get("/api/auth/passkeys")).json()["passkeys"][0]["last_used_at"]


async def test_adding_a_passkey_needs_the_password_first(app_client):
    c = app_client
    await _signed_in(c)
    r = await c.post("/api/auth/passkeys/register/options", json={"password": "wrong"})
    assert r.status_code == 403
    r = await c.post("/api/auth/passkeys/register/options", json={})
    assert r.status_code == 403
    c.cookies.clear()
    r = await c.post("/api/auth/passkeys/register/options", json={"password": "Correct-Horse-9"})
    assert r.status_code == 401, "not signed in at all"


async def test_a_sign_in_without_user_verification_is_refused(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    r = await _passkey_login(c, authn, uv=False)
    assert r.status_code == 401 and "not accepted" in r.json()["detail"]


async def test_a_challenge_answers_once(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    opts = (await c.post("/api/auth/passkeys/login/options")).json()
    body = {"challenge_id": opts["challenge_id"], "credential": authn.get(opts["options"])}
    assert (await c.post("/api/auth/passkeys/login/verify", json=body)).status_code == 200
    c.cookies.clear()
    again = await c.post("/api/auth/passkeys/login/verify", json=body)
    assert again.status_code == 401 and "expired" in again.json()["detail"]


async def test_a_counter_that_goes_backwards_is_a_cloned_key(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    assert (await _passkey_login(c, authn, counter=5)).status_code == 200
    assert (await _passkey_login(c, authn, counter=3)).status_code == 401


async def test_a_signature_made_for_another_site_is_refused(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    opts = (await c.post("/api/auth/passkeys/login/options")).json()
    cred = authn.get(opts["options"], origin="https://evil.example.net")
    r = await c.post("/api/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"], "credential": cred})
    assert r.status_code == 401


async def test_a_challenge_from_another_page_origin_does_not_carry_over(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    opts = (await c.post("/api/auth/passkeys/login/options")).json()
    cred = authn.get(opts["options"])
    r = await c.post("/api/auth/passkeys/login/verify", headers={"Origin": "https://other.example.com"},
                     json={"challenge_id": opts["challenge_id"], "credential": cred})
    assert r.status_code == 401 and "another sign-in" in r.json()["detail"]


async def test_an_unknown_credential_gets_the_same_answer_as_a_bad_one(app_client):
    c = app_client
    await _signed_in(c)
    stranger = SoftAuthenticator()
    stranger.create({"rp": {"id": "agent.example.com"}, "user": {"id": bytes_to_base64url(b"x" * 32)},
                     "challenge": bytes_to_base64url(b"c" * 32)})
    r = await _passkey_login(c, stranger)
    assert r.status_code == 401 and r.json()["detail"] == "that passkey was not accepted"


async def test_a_malformed_user_handle_is_refused_not_a_crash(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    opts = (await c.post("/api/auth/passkeys/login/options")).json()
    cred = authn.get(opts["options"])
    cred["response"]["userHandle"] = "!!not base64!!"
    r = await c.post("/api/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"], "credential": cred})
    assert r.status_code == 401 and r.json()["detail"] == "that passkey was not accepted"


async def test_one_account_cannot_rename_or_remove_anothers_passkey(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    saved = await _add_passkey(c, authn)
    other = await auth.create_user(c.pool, "sarah@example.com", "Another-Pass-77", "user", [])
    c.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session(c.pool, other["id"]))
    assert (await c.patch(f"/api/auth/passkeys/{saved['id']}", json={"name": "mine now"})).status_code == 404
    assert (await c.delete(f"/api/auth/passkeys/{saved['id']}")).status_code == 404
    assert (await c.get("/api/auth/passkeys")).json()["passkeys"] == []


async def test_rename_and_remove_your_own(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    saved = await _add_passkey(c, authn)
    r = await c.patch(f"/api/auth/passkeys/{saved['id']}", json={"name": "  Work laptop "})
    assert r.json()["name"] == "Work laptop"
    assert (await c.delete(f"/api/auth/passkeys/{saved['id']}")).json() == {"ok": True}
    assert c.recorded[-1]["action"] == "auth.passkey_remove"
    assert (await _passkey_login(c, authn)).status_code == 401, "a removed passkey no longer signs in"


async def test_a_password_reset_removes_every_passkey(app_client):
    """B27 (2026-10-22): sessions were revoked on a reset, passkeys were
    not, so one planted with a stolen session still signed in after the
    reset meant to end that session's reach."""
    c, authn = app_client, SoftAuthenticator()
    user = await _signed_in(c)
    await _add_passkey(c, authn)
    async with c.pool.connection() as conn:
        await conn.execute("INSERT INTO agent_password_resets (user_id, code_hash, expires_at) "
                           "VALUES (%s, %s, now() + interval '30 minutes')", (user["id"], auth._hash_token("123456")))
    assert await auth.reset_password(c.pool, user["email"], "123456", "Fresh-Horse-10")
    assert (await _passkey_login(c, authn)).status_code == 401


async def test_a_password_change_removes_every_passkey_and_says_so(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    r = await c.post("/api/auth/change-password",
                     json={"current_password": "Correct-Horse-9", "new_password": "Fresh-Horse-10-x"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "passkeys_removed": 1}
    assert (await _passkey_login(c, authn)).status_code == 401


async def test_the_password_and_code_sign_in_still_works_beside_passkeys(app_client):
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    r = await c.post("/api/auth/login", json={"email": "danny@example.com", "password": "Correct-Horse-9"})
    assert r.status_code == 200 and r.json()["user"]["email"] == "danny@example.com"


async def test_an_assertion_made_on_a_sibling_origin_does_not_sign_in(app_client, monkeypatch):
    """B26: rp id example.com, console on agent.example.com, a hostile page
    on chat.example.com. Asking with its own Origin is refused outright, and
    an assertion it made for a challenge the console handed out does not
    verify either."""
    monkeypatch.setenv("WEBAUTHN_RP_ID", "example.com")
    monkeypatch.setenv("WEBAUTHN_ORIGINS", ORIGIN)
    c, authn = app_client, SoftAuthenticator()
    await _signed_in(c)
    await _add_passkey(c, authn)
    c.cookies.clear()
    sibling = {"Origin": "https://chat.example.com"}
    assert (await c.post("/api/auth/passkeys/login/options", headers=sibling)).status_code == 400

    opts = (await c.post("/api/auth/passkeys/login/options")).json()
    cred = authn.get(opts["options"], origin="https://chat.example.com")
    for headers in (sibling, {}):
        r = await c.post("/api/auth/passkeys/login/verify", headers=headers,
                         json={"challenge_id": opts["challenge_id"], "credential": cred})
        assert r.status_code in (400, 401) and auth.SESSION_COOKIE_NAME not in r.cookies
    assert (await _passkey_login(c, authn)).status_code == 200, "the console itself still signs in"


async def test_an_assertion_made_on_another_localhost_port_does_not_sign_in(app_client):
    """B26: the desktop app's console on localhost:8100, a dev server on
    localhost:5173 in the same browser. Same rp id, different program."""
    import httpx
    c, authn = app_client, SoftAuthenticator()
    local = "http://localhost:8100"
    async with httpx.AsyncClient(transport=c._transport, base_url=local, headers={"Origin": local}) as lc:
        lc.pool = c.pool
        await _signed_in(lc)
        await _add_passkey(lc, authn, origin=local)
        lc.cookies.clear()
        r = await lc.post("/api/auth/passkeys/login/options", headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 400
        opts = (await lc.post("/api/auth/passkeys/login/options")).json()
        cred = authn.get(opts["options"], origin="http://localhost:5173")
        r = await lc.post("/api/auth/passkeys/login/verify", json={"challenge_id": opts["challenge_id"], "credential": cred})
        assert r.status_code == 401
        assert (await _passkey_login(lc, authn, origin=local)).status_code == 200, "the desktop app's own sign-in still works"
