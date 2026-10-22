"""audit N-1: the auth rate limiter must not be bypassable by a client-supplied
X-Forwarded-For, and its key dicts must not grow without bound."""
from types import SimpleNamespace

import pytest

import agent.rate_limit as rl


def _req(headers: dict, peer: str = "127.0.0.1"):
    """From the host install's nginx unless `peer` says otherwise."""
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: headers.get(k.lower(), d)),
        client=SimpleNamespace(host=peer),
    )


@pytest.fixture(autouse=True)
def _clean():
    rl._attempts.clear()
    rl._locked_until.clear()
    rl._last_evict = 0.0
    yield
    rl._attempts.clear()
    rl._locked_until.clear()


def test_client_ip_prefers_x_real_ip_over_forwarded_for():
    # X-Real-IP is set authoritatively by nginx; a client-supplied XFF must not win.
    req = _req({"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"})
    assert rl.client_ip(req) == "203.0.113.7"


def test_client_ip_takes_last_forwarded_hop_not_the_client_first_hop():
    # nginx appends the real peer LAST via $proxy_add_x_forwarded_for.
    req = _req({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
    assert rl.client_ip(req) == "203.0.113.7"


def test_rotating_forwarded_for_first_hop_does_not_bypass_the_limit():
    # Same real peer (X-Real-IP), attacker rotates the spoofable XFF first hop.
    got_429 = 0
    for i in range(20):
        req = _req({"x-real-ip": "203.0.113.7", "x-forwarded-for": f"9.9.9.{i}"})
        try:
            rl.check_rate_limit(req, "login")
        except Exception as e:  # HTTPException(429)
            if getattr(e, "status_code", None) == 429:
                got_429 += 1
    assert got_429 > 0, "rotating X-Forwarded-For bypassed the limiter"


def test_a_direct_client_s_own_x_real_ip_is_not_believed(monkeypatch):
    """The compose bundle publishes the agent's port with nothing in front of
    it. A client there setting X-Real-IP afresh per request got a fresh bucket
    per request -- no limit at all."""
    monkeypatch.delenv(rl.TRUSTED_PROXIES_ENV, raising=False)
    got_429 = 0
    for i in range(20):
        req = _req({"x-real-ip": f"198.51.100.{i}", "x-forwarded-for": f"9.9.9.{i}"}, peer="172.18.0.1")
        assert rl.client_ip(req) == "172.18.0.1"
        try:
            rl.check_rate_limit(req, "login")
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 429:
                got_429 += 1
    assert got_429 > 0, "rotating X-Real-IP from a direct client bypassed the limiter"


def test_a_proxy_on_another_address_is_named_in_config(monkeypatch):
    monkeypatch.setenv(rl.TRUSTED_PROXIES_ENV, "10.0.0.0/24, not-an-ip")
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="10.0.0.9")) == "203.0.113.7"
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="10.0.1.9")) == "10.0.1.9"
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="127.0.0.1")) == "127.0.0.1", \
        "naming a proxy replaces the default rather than adding to it"


def test_an_ipv4_mapped_loopback_peer_is_still_loopback(monkeypatch):
    monkeypatch.delenv(rl.TRUSTED_PROXIES_ENV, raising=False)
    assert rl.client_ip(_req({"x-real-ip": "203.0.113.7"}, peer="::ffff:127.0.0.1")) == "203.0.113.7"


def test_eviction_bounds_the_attempts_dict(monkeypatch):
    # Distinct real peers over time should not grow the dict without bound.
    t = [1000.0]
    monkeypatch.setattr(rl.time, "time", lambda: t[0])
    for i in range(50):
        req = _req({"x-real-ip": f"203.0.113.{i}"})
        rl.check_rate_limit(req, "login")
        t[0] += 1
    before = len(rl._attempts)
    # jump far past every window + the evict interval, then one more call sweeps
    t[0] += rl._EVICT_INTERVAL + 10_000
    rl.check_rate_limit(_req({"x-real-ip": "203.0.113.250"}), "login")
    assert len(rl._attempts) < before, "stale keys were not evicted"


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
    rl.check_rate_limit(prior, "verify-2fa", account=1)
    rl.check_rate_limit(prior, "verify-2fa", account=1)
    assert ("203.0.113.7", "verify-2fa", "1") in rl._attempts

    res = TestClient(srv.app, client=("127.0.0.1", 50000)).post(
        "/api/auth/2fa/verify",
        json={"temp_token": "t", "code": "123456"},
        headers={"X-Real-IP": "203.0.113.7"},
    )
    assert res.status_code == 200, res.text
    assert ("203.0.113.7", "verify-2fa", "1") not in rl._attempts
    assert ("203.0.113.7", "verify-2fa", "1") not in rl._locked_until


def test_an_internal_failure_lets_the_request_through_and_says_so(monkeypatch, caplog):
    """Fail open, never silently: a limiter that quietly stopped limiting would
    look exactly like one that works."""
    def boom(request):
        raise RuntimeError("limiter bug")

    monkeypatch.setattr(rl, "client_ip", boom)
    with caplog.at_level("ERROR", logger="tektonix"):
        rl.check_rate_limit(_req({}), "login")          # does not raise
    assert "request allowed through unlimited" in caplog.text


def test_the_single_process_assumption_is_written_where_it_would_break():
    assert "SINGLE PROCESS IS AN ASSUMPTION" in rl.__doc__


@pytest.mark.parametrize("path, body, wrong_status", [
    ("/api/auth/change-password", {"current_password": "nope", "new_password": "Long-enough-passw0rd!"}, 401),
    ("/api/auth/2fa/setup", {"password": "nope"}, 403),
    ("/api/auth/2fa/disable", {"password": "nope"}, 403),
])
def test_the_password_recheck_on_an_authenticated_route_is_rate_limited(monkeypatch, path, body, wrong_status):
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
    assert ("203.0.113.9", "password-recheck", "7") in rl._locked_until

    rl._locked_until.clear()
    rl._attempts.clear()
    for _ in range(3):
        client.post(path, json=body, headers=headers)
    ok = client.post(path, json={**body, "current_password": "right", "password": "right"}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ("203.0.113.9", "password-recheck", "7") not in rl._attempts, "a good password clears the window"


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
