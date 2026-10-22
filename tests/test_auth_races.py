"""One-time credentials are consumed once, even by concurrent requests.

Each of these was a SELECT followed by a separate UPDATE, so requests racing
on the same row all read it unused (or under its attempt limit) before any of
them wrote: one recovery code logged in twice, one reset code set two
passwords, and "five guesses at a six-digit code" was five sequential guesses
plus however many arrived at once.

Real Postgres, because a race is a property of the database, not of a fake:
AGENT_TEST_PG_DSN names a throwaway database (CI points it at the postgres:16
service), and every table is created in a scratch schema dropped afterwards.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from agent import auth

PG_DSN = os.environ.get("AGENT_TEST_PG_DSN")

CONCURRENCY = 5   # the auth pool's max_size


@pytest.fixture
async def pool():
    if not PG_DSN:
        pytest.skip("set AGENT_TEST_PG_DSN to a throwaway database")
    import psycopg  # noqa: PLC0415

    schema = f"auth_races_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    dsn = f"{PG_DSN}{sep}options=-c%20search_path%3D{schema}"
    try:
        async with auth.open_auth_pool(SimpleNamespace(pg_dsn=dsn)) as p:
            yield p
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


async def _user(pool, email="a@example.com"):
    return await auth.create_user(pool, email, "old-password-123", "user", ["p"])


async def _execute(pool, sql, params=()):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else None


async def _issue_reset(pool, user_id, code):
    await _execute(pool, "INSERT INTO agent_password_resets (user_id, code_hash, expires_at) "
                         "VALUES (%s, %s, now() + interval '30 minutes')", (user_id, auth._hash_token(code)))


async def test_a_recovery_code_logs_in_once_under_concurrency(pool, monkeypatch):
    user = await _user(pool)
    await _execute(pool, "UPDATE agent_users SET totp_enabled = TRUE, totp_secret_enc = 'x' WHERE id = %s",
                   (user["id"],))
    monkeypatch.setattr(auth, "_decrypt_totp_secret", lambda config, enc: "JBSWY3DPEHPK3PXP")
    codes = [f"code-{i}" for i in range(10)]
    for c in codes:
        await _execute(pool, "INSERT INTO agent_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                       (user["id"], auth._hash_token(c)))

    for c in codes:
        results = await asyncio.gather(*[
            auth.verify_totp_or_recovery(pool, None, user["id"], c) for _ in range(CONCURRENCY)])
        assert results.count(True) == 1, f"{c} was accepted {results.count(True)} times"


async def test_concurrent_wrong_guesses_cannot_exceed_the_attempt_limit(pool):
    user = await _user(pool)
    await _issue_reset(pool, user["id"], "123456")
    guesses = [f"{n:06d}" for n in range(200000, 200000 + 4 * CONCURRENCY)]
    results = await asyncio.gather(*[
        auth.reset_password(pool, user["email"], g, "new-password-456") for g in guesses])
    assert not any(results)
    rows = await _execute(pool, "SELECT attempts FROM agent_password_resets WHERE user_id = %s", (user["id"],))
    assert rows[0]["attempts"] == auth.RESET_CODE_MAX_ATTEMPTS, "each compared guess is counted, and no more are compared"
    assert await auth.reset_password(pool, user["email"], "123456", "new-password-456") is False


async def test_a_reset_code_sets_one_password_under_concurrency(pool):
    user = await _user(pool)
    for round_ in range(10):
        code = f"{654300 + round_:06d}"
        await _issue_reset(pool, user["id"], code)
        results = await asyncio.gather(*[
            auth.reset_password(pool, user["email"], code, f"new-password-{i:03d}") for i in range(CONCURRENCY)])
        assert results.count(True) == 1, f"round {round_}: {results.count(True)} resets succeeded"


async def test_a_password_change_works_before_the_passkey_table_exists(pool):
    user = await _user(pool)
    assert await auth.change_password(pool, user["id"], "chosen-password-789") == 0


async def test_the_code_that_finishes_enrolment_cannot_be_replayed_at_login(pool, monkeypatch):
    """B30 (2026-10-22): enrolment accepted a code from the next step but
    recorded the wall clock's, so the same code passed once more at login."""
    import pyotp

    secret = "JBSWY3DPEHPK3PXP"
    user = await _user(pool)
    await _execute(pool, "UPDATE agent_users SET totp_secret_enc = 'x' WHERE id = %s", (user["id"],))
    monkeypatch.setattr(auth, "_decrypt_totp_secret", lambda config, enc: secret)
    totp = pyotp.TOTP(secret)
    next_code = totp.at((int(time.time()) // totp.interval + 1) * totp.interval)
    await auth.confirm_totp_setup(pool, None, user["id"], next_code)
    assert await auth.verify_totp_or_recovery(pool, None, user["id"], next_code) is False


async def test_a_password_change_voids_outstanding_reset_codes(pool):
    user = await _user(pool)
    await _issue_reset(pool, user["id"], "111111")
    await auth.change_password(pool, user["id"], "chosen-password-789")
    assert await auth.reset_password(pool, user["email"], "111111", "attacker-password") is False
    row = await auth.get_user_by_email(pool, user["email"])
    assert auth.verify_password("chosen-password-789", row["password_hash"])


async def test_a_push_endpoint_is_deleted_only_by_its_owner_or_its_browser(pool):
    owner = await _user(pool, "owner@example.com")
    other = await _user(pool, "other@example.com")
    await auth.save_push_subscription(pool, owner["id"], "https://push.example/abc", "p256", "secret-auth")

    await auth.delete_push_subscription(pool, "https://push.example/abc", user_id=other["id"])
    await auth.delete_push_subscription(pool, "https://push.example/abc", user_id=other["id"], auth_key="wrong")
    assert await auth.count_push_subscriptions(pool, owner["id"]) == 1, "an endpoint URL alone is not enough"

    await auth.delete_push_subscription(pool, "https://push.example/abc", user_id=other["id"],
                                        auth_key="secret-auth")
    assert await auth.count_push_subscriptions(pool, owner["id"]) == 0, "the browser holding the secret may"

    await auth.save_push_subscription(pool, owner["id"], "https://push.example/abc", "p256", "secret-auth")
    await auth.delete_push_subscription(pool, "https://push.example/abc", user_id=owner["id"])
    assert await auth.count_push_subscriptions(pool, owner["id"]) == 0, "the owner may"


async def test_a_push_endpoint_cannot_be_taken_over_by_another_account(pool):
    """The upsert replaced user_id and the keys for any caller, so another
    signed-in account could claim an endpoint and the victim's browser could
    no longer decrypt its alerts (2026-09-29). The row moves only for its
    owner, or for the browser that holds its secret."""
    owner = await _user(pool, "owner@example.com")
    other = await _user(pool, "other@example.com")
    await auth.save_push_subscription(pool, owner["id"], "https://push.example/abc", "p256", "secret-auth")

    await auth.save_push_subscription(pool, other["id"], "https://push.example/abc", "p256-x", "other-auth")
    assert await auth.count_push_subscriptions(pool, owner["id"]) == 1, "another account took the endpoint"
    assert await auth.count_push_subscriptions(pool, other["id"]) == 0

    # The same browser, now signed into the other account: it presents the
    # secret it holds, and the endpoint follows the sign-in (a shared device).
    await auth.save_push_subscription(pool, other["id"], "https://push.example/abc", "p256", "secret-auth")
    assert await auth.count_push_subscriptions(pool, other["id"]) == 1
    assert await auth.count_push_subscriptions(pool, owner["id"]) == 0


def test_the_push_upsert_is_conditioned_on_owner_or_secret():
    """Runs without a database: the shape of the statement itself."""
    import inspect
    src = inspect.getsource(auth.save_push_subscription)
    assert "ON CONFLICT (endpoint) DO UPDATE" in src
    assert "WHERE agent_push_subscriptions.user_id = EXCLUDED.user_id" in src
    assert "OR agent_push_subscriptions.auth = EXCLUDED.auth" in src


def test_the_reset_code_is_compared_in_constant_time():
    assert "hmac.compare_digest" in inspect.getsource(auth.reset_password)
