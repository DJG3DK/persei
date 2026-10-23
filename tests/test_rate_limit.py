"""The auth rate limiter: audit N-1 (a client-supplied X-Forwarded-For must not
bypass it, its table must not grow without bound), B25 (per-account windows),
and its counters living in the database -- shared by every worker, kept
across a restart, and not undercounted by concurrent attempts.

The backend tests run on SQLite always and on Postgres when
AGENT_TEST_PG_DSN names a throwaway database (CI points it at the
postgres:16 service); each Postgres run works in a scratch schema dropped
afterwards."""
import asyncio
import os
import sqlite3
import uuid
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agent.rate_limit as rl

PG_DSN = os.environ.get("AGENT_TEST_PG_DSN")


def _req(headers: dict, peer: str = "127.0.0.1"):
    """From the host install's nginx unless `peer` says otherwise."""
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: headers.get(k.lower(), d)),
        client=SimpleNamespace(host=peer),
    )


@pytest.fixture
def clock(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(rl, "time", SimpleNamespace(time=lambda: t[0]))
    return t


async def _status(req, action="login", account=None) -> int:
    try:
        await rl.check_rate_limit(req, action, account=account)
    except HTTPException as e:
        return e.status_code
    return 200


def _sqlite_rows(path) -> dict:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(f"SELECT ip, action, account, locked_until FROM {rl.TABLE}").fetchall()
    return {(ip, action, account): locked for ip, action, account, locked in rows}


def _rows() -> dict:
    """(ip, action, account) -> locked_until, from the installed SQLite limiter."""
    return _sqlite_rows(rl.installed()._path)


# ---------------------------------------------------------------------------
# which address is counted
# ---------------------------------------------------------------------------

def test_client_ip_prefers_x_real_ip_over_forwarded_for():
    # X-Real-IP is set authoritatively by nginx; a client-supplied XFF must not win.
    req = _req({"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"})
    assert rl.client_ip(req) == "203.0.113.7"


def test_client_ip_takes_last_forwarded_hop_not_the_client_first_hop():
    # nginx appends the real peer LAST via $proxy_add_x_forwarded_for.
    req = _req({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
    assert rl.client_ip(req) == "203.0.113.7"


async def test_rotating_forwarded_for_first_hop_does_not_bypass_the_limit():
    # Same real peer (X-Real-IP), attacker rotates the spoofable XFF first hop.
    codes = [await _status(_req({"x-real-ip": "203.0.113.7", "x-forwarded-for": f"9.9.9.{i}"}))
             for i in range(20)]
    assert 429 in codes, "rotating X-Forwarded-For bypassed the limiter"


async def test_a_direct_client_s_own_x_real_ip_is_not_believed(monkeypatch):
    """The compose bundle publishes the agent's port with nothing in front of
    it. A client there setting X-Real-IP afresh per request got a fresh bucket
    per request -- no limit at all."""
    monkeypatch.delenv(rl.TRUSTED_PROXIES_ENV, raising=False)
    codes = []
    for i in range(20):
        req = _req({"x-real-ip": f"198.51.100.{i}", "x-forwarded-for": f"9.9.9.{i}"}, peer="172.18.0.1")
        assert rl.client_ip(req) == "172.18.0.1"
        codes.append(await _status(req))
    assert 429 in codes, "rotating X-Real-IP from a direct client bypassed the limiter"


def test_a_proxy_on_another_address_is_named_in_config(monkeypatch):
    monkeypatch.setenv(rl.TRUSTED_PROXIES_ENV, "10.0.0.0/24, not-an-ip")
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="10.0.0.9")) == "203.0.113.7"
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="10.0.1.9")) == "10.0.1.9"
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="127.0.0.1")) == "127.0.0.1", \
        "naming a proxy replaces the default rather than adding to it"


def test_an_ipv4_mapped_loopback_peer_is_still_loopback(monkeypatch):
    monkeypatch.delenv(rl.TRUSTED_PROXIES_ENV, raising=False)
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="::ffff:127.0.0.1")) == "203.0.113.7"


# ---------------------------------------------------------------------------
# the counters, on both backends
# ---------------------------------------------------------------------------

@pytest.fixture(params=["sqlite", "postgres"])
async def make_limiter(request, tmp_path):
    """A factory of limiters over ONE database: each call is another worker,
    or the same worker after a restart."""
    if request.param == "sqlite":
        path = tmp_path / "shared.db"
        yield lambda: rl.SqliteAttempts(path)
        return
    if not PG_DSN:
        pytest.skip("set AGENT_TEST_PG_DSN to a throwaway database")
    import psycopg  # noqa: PLC0415

    from agent import auth  # noqa: PLC0415

    schema = f"rate_limit_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    dsn = f"{PG_DSN}{sep}options=-c%20search_path%3D{schema}"
    try:
        async with AsyncExitStack() as stack:
            async def make():
                pool = await stack.enter_async_context(auth.open_auth_pool(SimpleNamespace(pg_dsn=dsn)))
                return await rl.open_for(SimpleNamespace(dsn=dsn), pool)

            # A pool opens asynchronously, so the "workers" a test may ask
            # for are opened up front: one pool each, as separate processes.
            made = [await make() for _ in range(3)]
            yield iter(made).__next__
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


async def test_two_workers_share_one_count(make_limiter):
    """With per-process counters, two workers each allowed five: ten guesses a minute."""
    a, b = make_limiter(), make_limiter()
    req = _req({"x-real-ip": "203.0.113.30"})
    codes = []
    for i in range(6):
        rl.install(a if i % 2 else b)
        codes.append(await _status(req, account="victim@b.co"))
    assert codes == [200] * 5 + [429]
    rl.install(a)
    assert await _status(req, account="victim@b.co") == 429, "the lock is seen by the other worker"


async def test_a_restart_keeps_the_count_and_the_lock(make_limiter):
    req = _req({"x-real-ip": "203.0.113.31"})
    rl.install(make_limiter())
    for _ in range(4):
        assert await _status(req) == 200
    rl.install(make_limiter())          # a deploy
    assert await _status(req) == 200
    assert await _status(req) == 429
    rl.install(make_limiter())          # and another
    assert await _status(req) == 429, "a restart must not clear a lockout"


async def test_windows_and_locks_expire(make_limiter, clock):
    rl.install(make_limiter())
    req = _req({"x-real-ip": "203.0.113.32"})
    max_attempts, window, lockout = rl._LIMITS["login"]
    for _ in range(max_attempts):
        assert await _status(req) == 200
    clock[0] += window + 1
    for _ in range(max_attempts):
        assert await _status(req) == 200, "hits older than the window still counted"
    assert await _status(req) == 429
    clock[0] += lockout - 1
    assert await _status(req) == 429, "the lock ended early"
    clock[0] += 2
    assert await _status(req) == 200, "the lock never ended"


async def test_concurrent_attempts_are_not_undercounted(make_limiter, clock):
    """Read-then-write without a lock let every attempt in a burst read the
    same count; exactly the limit must get through, whichever worker each lands on."""
    workers = [make_limiter() for _ in range(3)]
    req = _req({"x-real-ip": "203.0.113.33"})
    max_attempts, window, lockout = rl._LIMITS["login"]
    key = ("203.0.113.33", "login", "")

    async def attempt(i):
        return await workers[i % len(workers)].hit(key, max_attempts, window, lockout, clock[0])

    results = await asyncio.gather(*[attempt(i) for i in range(20)])
    assert sum(r is None for r in results) == max_attempts, results
    rl.install(workers[0])
    assert await _status(req) == 429


async def test_expired_windows_are_deleted(make_limiter, clock):
    limiter = make_limiter()
    rl.install(limiter)
    for i in range(30):
        await _status(_req({"x-real-ip": f"203.0.113.{100 + i}"}))
    rl.install(limiter)                 # resets the sweep timer
    clock[0] += 10_000
    await _status(_req({"x-real-ip": "203.0.113.250"}))
    if isinstance(limiter, rl.SqliteAttempts):
        rows = _sqlite_rows(limiter._path)
    else:
        async with limiter._pool.connection() as conn:
            cur = await conn.execute(f"SELECT ip, action, account FROM {rl.TABLE}")
            rows = {(r["ip"], r["action"], r["account"]): None for r in await cur.fetchall()}
    assert set(rows) == {("203.0.113.250", "login", "")}, "stale windows were not deleted"


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------

async def test_an_unreachable_database_refuses_with_503_and_says_so(tmp_path, caplog):
    """Fail closed: the database the brake cannot reach holds the accounts too."""
    rl.install(rl.SqliteAttempts(tmp_path))        # a directory: every open fails
    with caplog.at_level("ERROR", logger="tektonix"):
        assert await _status(_req({})) == 503
    assert "request refused with 503" in caplog.text


async def test_no_limiter_installed_refuses_with_503():
    rl.install(None)
    assert await _status(_req({})) == 503


def test_an_unreachable_database_fails_login_closed(monkeypatch, tmp_path):
    client = _login_client(monkeypatch)
    rl.install(rl.SqliteAttempts(tmp_path))
    res = client.post("/api/auth/login", json={"email": "me@b.co", "password": "my-pw"})
    assert res.status_code == 503, res.text


def test_the_fail_closed_choice_is_written_where_it_is_made():
    assert "Fails CLOSED" in rl.__doc__


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------

def test_a_successful_2fa_clears_the_verify_window(monkeypatch):
    """Login already cleared its window on success. verify-2fa did not, so a
    few typos before a good code could lock a legitimate second factor."""
    from fastapi.testclient import TestClient

    import agent.server as srv

    async def pending(pool, token):
        return {"user_id": 1}

    async def totp_ok(pool, config, uid, code):
        return True

    async def session(pool, uid):
        return "sess"

    async def user_row(pool, uid):
        return {"id": 1, "email": "a@b.co", "role": "admin", "allowed_repos": None,
                "totp_enabled": True, "must_change_password": False,
                "auto_approve_commands": False, "require_merge_review": True}

    monkeypatch.setattr(srv.auth, "resolve_pending_2fa", pending)
    monkeypatch.setattr(srv.auth, "verify_totp_or_recovery", totp_ok)
    monkeypatch.setattr(srv.auth, "create_session", session)
    monkeypatch.setattr(srv.auth, "get_user_by_id", user_row)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)

    prior = _req({"x-real-ip": "203.0.113.7"})
    asyncio.run(_status(prior, "verify-2fa", account=1))
    asyncio.run(_status(prior, "verify-2fa", account=1))
    assert ("203.0.113.7", "verify-2fa", "1") in _rows()

    res = TestClient(srv.app, client=("127.0.0.1", 50000)).post(
        "/api/auth/2fa/verify",
        json={"temp_token": "t", "code": "123456"},
        headers={"X-Real-IP": "203.0.113.7"},
    )
    assert res.status_code == 200, res.text
    assert ("203.0.113.7", "verify-2fa", "1") not in _rows()
    assert ("203.0.113.7", "verify-2fa", "*") in _rows(), "the IP-wide window is never cleared"


@pytest.mark.parametrize("path, body, wrong_status", [
    ("/api/auth/change-password", {"current_password": "nope", "new_password": "Long-enough-passw0rd!"}, 401),
    ("/api/auth/2fa/setup", {"password": "nope"}, 403),
    ("/api/auth/2fa/disable", {"password": "nope"}, 403),
])
def test_the_password_recheck_on_an_authenticated_route_is_rate_limited(monkeypatch, tmp_path, path, body, wrong_status):
    """Cursor's re-audit (2026-09-27): a stolen session could guess the
    password at full speed on these three, and the password is what turns a
    session into everything else. Same brake as login, cleared on success."""
    from fastapi.testclient import TestClient

    import agent.server as srv
    from agent.auth import User

    me = User(id=7, email="u@b.co", role="user", allowed_repos=[], totp_enabled=True, must_change_password=False)

    async def user_row(pool, uid):
        return {"id": 7, "password_hash": "hash"}

    async def nothing(*a, **k):
        return None

    async def totp(*a, **k):
        return "secret", "otpauth://x"

    monkeypatch.setattr(srv.auth, "get_user_by_id", user_row)
    monkeypatch.setattr(srv.auth, "verify_password", lambda pw, h: pw == "right")
    monkeypatch.setattr(srv.auth, "change_password", nothing)
    monkeypatch.setattr(srv.auth, "disable_totp", nothing)
    monkeypatch.setattr(srv.auth, "start_totp_setup", totp)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.get_current_user, lambda: me)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)

    client = TestClient(srv.app, client=("127.0.0.1", 50000))
    headers = {"X-Real-IP": "203.0.113.9"}
    for _ in range(5):
        assert client.post(path, json=body, headers=headers).status_code == wrong_status
    res = client.post(path, json=body, headers=headers)
    assert res.status_code == 429, res.text
    assert _rows()[("203.0.113.9", "password-recheck", "7")], "the account's window is locked"

    rl.install(rl.SqliteAttempts(tmp_path / "fresh.db"))
    for _ in range(3):
        client.post(path, json=body, headers=headers)
    ok = client.post(path, json={**body, "current_password": "right", "password": "right"}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ("203.0.113.9", "password-recheck", "7") not in _rows(), "a good password clears the window"


def _login_client(monkeypatch):
    from fastapi.testclient import TestClient

    import agent.server as srv

    rows = {
        "victim@b.co": {"id": 1, "email": "victim@b.co", "password_hash": "victim-pw"},
        "me@b.co": {"id": 2, "email": "me@b.co", "password_hash": "my-pw"},
    }
    for row in rows.values():
        row.update({"role": "user", "allowed_repos": [], "totp_enabled": False,
                    "must_change_password": False, "auto_approve_commands": False,
                    "require_merge_review": True})

    async def by_email(pool, email):
        return rows.get(email)

    async def session(pool, uid):
        return "sess"

    monkeypatch.setattr(srv.auth, "get_user_by_email", by_email)
    monkeypatch.setattr(srv.auth, "verify_password", lambda pw, h: pw == h)
    monkeypatch.setattr(srv.auth, "create_session", session)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    return TestClient(srv.app, client=("127.0.0.1", 50000))


def test_another_account_s_good_login_does_not_reset_the_victim_s_window(monkeypatch):
    """B25 (2026-10-22): four wrong passwords for the victim, one correct
    login as yourself, repeat. Clearing the whole IP's window on that login
    allowed 30 guesses with no 429."""
    client = _login_client(monkeypatch)
    headers = {"X-Real-IP": "203.0.113.20"}

    def guess():
        return client.post("/api/auth/login", json={"email": "victim@b.co", "password": "guess"},
                           headers=headers).status_code

    for _ in range(4):
        assert guess() == 401
    assert client.post("/api/auth/login", json={"email": "Me@b.co ", "password": "my-pw"},
                       headers=headers).status_code == 200
    assert guess() == 401
    assert guess() == 429, "the sixth guess at the victim must be refused"


def test_one_ip_cannot_spray_every_account_it_can_name(monkeypatch):
    client = _login_client(monkeypatch)
    headers = {"X-Real-IP": "203.0.113.21"}
    codes = [client.post("/api/auth/login", json={"email": f"u{i}@b.co", "password": "x"},
                         headers=headers).status_code for i in range(rl._LIMITS["login"][0] * rl._IP_WIDE_FACTOR + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {401}


def test_a_good_login_still_clears_the_account_s_own_misses(monkeypatch):
    client = _login_client(monkeypatch)
    headers = {"X-Real-IP": "203.0.113.22"}
    for _ in range(4):
        client.post("/api/auth/login", json={"email": "me@b.co", "password": "typo"}, headers=headers)
    assert client.post("/api/auth/login", json={"email": "me@b.co", "password": "my-pw"},
                       headers=headers).status_code == 200
    for _ in range(4):
        assert client.post("/api/auth/login", json={"email": "me@b.co", "password": "typo"},
                           headers=headers).status_code == 401


def test_a_failing_limiter_is_not_shown_as_slow_down_on_the_approve_page(monkeypatch, tmp_path):
    """The approve page renders a 429 as "Slow down"; a 503 is not a rate
    limit and must not read as one."""
    from fastapi.testclient import TestClient

    import agent.server as srv

    rl.install(rl.SqliteAttempts(tmp_path))
    res = TestClient(srv.app).post("/api/github/approve", data={"t": "x"},
                                   headers={"Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 503 and "Slow down" not in res.text
