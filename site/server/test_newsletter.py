"""The signup endpoint.

Everything here follows from one fact: the landing page ships no JavaScript,
so this is a plain HTML form POST. A browser, not a client library, is what
sends it and what reads the answer -- which is why every outcome has to be a
redirect to a page a person can read, and why a bad address must not produce a
422 nobody sees.

Run: site/server/.venv/bin/python -m pytest site/server/test_newsletter.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
newsletter = importlib.import_module("newsletter")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, store, fail=False, regs=None):
        self.store, self.fail = store, fail
        self.regs = regs if regs is not None else []

    async def execute(self, sql, params=None):
        if self.fail and "INSERT" in sql:
            import psycopg
            raise psycopg.OperationalError("no")
        if "download_registrations" in sql:
            name, email, platform, wants = params
            self.regs.append({"name": name, "email": email, "platform": platform, "updates": wants})
            return FakeCursor([(len(self.regs),)])
        if "INSERT" in sql:
            email, name, token, source = params
            # Models the statement's own conflict rule: DO NOTHING leaves an
            # existing row alone.
            if email in self.store and "DO NOTHING" in sql:
                return FakeCursor([])
            self.store[email] = {"name": name, "token": token, "source": source}
        if "count(*)" in sql:
            return FakeCursor([(len(self.store),)])
        return FakeCursor([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, fail=False):
        self.store: dict[str, dict] = {}
        self.regs: list[dict] = []
        self.fail = fail

    def connection(self):
        return FakeConn(self.store, self.fail, self.regs)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    # The limiter is process-global. Without a reset, a test that posts
    # five times poisons every later test that shares the TestClient IP.
    newsletter.reset_subscribe_limiter()
    yield
    newsletter.reset_subscribe_limiter()


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    # The download page resolves GitHub's file URL itself (S4), so a test that
    # does not bring its own release must not reach the real API.
    newsletter.reset_download_cache()
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", _FakeGitHub(fail=True))
    yield
    newsletter.reset_download_cache()


def _post(client, **form):
    # follow_redirects off: the redirect IS the behaviour under test.
    return client.post("/subscribe", data=form, follow_redirects=False)


def test_a_good_signup_is_stored_and_lands_on_a_real_page(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada Lovelace", email="Ada@Example.COM")

    assert r.status_code == 303, "a browser sent this; it needs somewhere to go"
    assert r.headers["location"] == newsletter.OK_URL
    # Lower-cased on the way in, or the same person signs up twice.
    assert "ada@example.com" in pool.store
    assert pool.store["ada@example.com"]["name"] == "Ada Lovelace"
    assert len(pool.store["ada@example.com"]["token"]) > 20, "no unsubscribe token minted"


@pytest.mark.parametrize("email", ["", "nope", "no@domain", "a b@c.com", "@example.com"])
def test_an_address_that_is_not_one_is_refused_to_a_page(monkeypatch, email):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada", email=email)

    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL
    assert pool.store == {}, "a bad address was stored anyway"


def test_a_missing_name_is_refused(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="   ", email="ada@example.com")
    assert r.headers["location"] == newsletter.FAIL_URL
    assert pool.store == {}


def test_nothing_is_ever_answered_with_a_bare_status(monkeypatch):
    """The form has no JavaScript behind it. Any answer that is not a redirect
    leaves somebody looking at a JSON blob or a blank page."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    for form in ({"name": "A", "email": "a@b.co"}, {"name": "", "email": ""},
                 {"name": "A", "email": "bad"}):
        r = _post(TestClient(newsletter.app), **form)
        assert r.status_code == 303, f"{form} answered {r.status_code}"
        assert r.headers["location"].startswith("https://"), "redirect must be absolute"


def test_a_database_failure_is_a_page_not_a_500(monkeypatch):
    monkeypatch.setattr(newsletter, "pool", FakePool(fail=True))
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL


def test_no_database_at_all_is_a_page_not_a_crash(monkeypatch):
    monkeypatch.setattr(newsletter, "pool", None)
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL


def test_the_insert_treats_a_repeat_as_a_success_that_changes_nothing(monkeypatch):
    """Already on the list is what the person wanted. An error would send them
    away believing it had not worked. But it is a no-op: the upsert used to
    overwrite the name and clear unsubscribed_at, and with no double opt-in
    that let anyone re-subscribe or rename anyone (2026-09-29 audit, S1)."""
    captured = {}

    class Recording(FakePool):
        def connection(self):
            outer = self

            class C(FakeConn):
                async def execute(self, sql, params=None):
                    if "INSERT" in sql:
                        captured["sql"] = sql
                    return await super().execute(sql, params)

            return C(outer.store)

    pool = Recording()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert r.headers["location"] == newsletter.OK_URL
    assert "ON CONFLICT (email) DO NOTHING" in captured["sql"]
    assert "DO UPDATE" not in captured["sql"] and "unsubscribed_at" not in captured["sql"]


def test_an_unsubscribed_address_stays_unsubscribed_and_keeps_its_name(monkeypatch):
    """The README's promise for unsubscribed_at: a later signup does not
    silently re-add somebody who left. The old upsert broke it."""
    pool = FakePool()
    pool.store["ada@example.com"] = {"name": "Ada Lovelace", "token": "t" * 32, "source": "landing-page",
                                     "unsubscribed_at": "2026-09-01T00:00:00Z"}
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Somebody Else", email="ada@example.com")
    assert r.status_code == 303 and r.headers["location"] == newsletter.OK_URL, "still a success to the browser"
    row = pool.store["ada@example.com"]
    assert row["name"] == "Ada Lovelace"
    assert row["unsubscribed_at"] == "2026-09-01T00:00:00Z"


def test_an_address_never_reaches_the_log(monkeypatch, caplog):
    """A log line is the easiest way for a mailing list to leak, into a file
    people tail over each other's shoulders."""
    monkeypatch.setattr(newsletter, "pool", FakePool())
    with caplog.at_level("INFO"):
        _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
        _post(TestClient(newsletter.app), name="Ada", email="rubbish")
    assert "ada@example.com" not in caplog.text
    assert "rubbish" not in caplog.text


def test_oversized_input_is_truncated_rather_than_rejected(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    _post(TestClient(newsletter.app), name="A" * 500, email="ada@example.com")
    assert len(pool.store["ada@example.com"]["name"]) == 120


def test_a_sixth_signup_from_one_ip_is_a_page_not_a_store(monkeypatch):
    """The landing page is a public form with no CAPTCHA. The sixth POST
    in a minute is refused the same way a bad address is -- a 303 to the
    failure page -- so a browser still has somewhere to go."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    client = TestClient(newsletter.app)
    for i in range(newsletter._SUBSCRIBE_MAX):
        r = _post(client, name="Ada", email=f"ada{i}@example.com")
        assert r.headers["location"] == newsletter.OK_URL
    assert len(pool.store) == newsletter._SUBSCRIBE_MAX
    r = _post(client, name="Ada", email="one-more@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL
    assert "one-more@example.com" not in pool.store


def test_a_post_from_another_site_is_refused(monkeypatch):
    """A page elsewhere posting to this form is somebody signing up an
    address that is not theirs. The browser says where a POST came from;
    a header naming another site is refused, a missing one is not evidence
    (2026-09-29 audit, S2)."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    client = TestClient(newsletter.app)
    for headers in ({"origin": "https://evil.example"},
                    {"sec-fetch-site": "cross-site"},
                    {"origin": newsletter.SITE_URL, "sec-fetch-site": "cross-site"}):
        r = client.post("/subscribe", data={"name": "Ada", "email": "ada@example.com"},
                        headers=headers, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == newsletter.FAIL_URL, headers
    assert pool.store == {}
    newsletter.reset_subscribe_limiter()   # the refusals above count against the per-IP window
    for headers in ({"origin": newsletter.SITE_URL, "sec-fetch-site": "same-origin"},
                    {"origin": newsletter.SITE_URL.upper() + "/"},
                    {}):
        r = client.post("/subscribe", data={"name": "Ada", "email": "ada@example.com"},
                        headers=headers, follow_redirects=False)
        assert r.headers["location"] == newsletter.OK_URL, headers
    assert "ada@example.com" in pool.store


def test_a_filled_honeypot_is_dropped_but_told_success(monkeypatch):
    """The field is hidden from people (site/src/LandingPage.tsx); something
    that filled it read the markup. It is not stored, and it is not told so."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com",
              **{newsletter.HONEYPOT_FIELD: "https://spam.example"})
    assert r.status_code == 303 and r.headers["location"] == newsletter.OK_URL
    assert pool.store == {}


def test_x_real_ip_is_the_rate_limit_key(monkeypatch):
    """nginx sets X-Real-IP from $remote_addr and overwrites anything the
    client sent. Two people behind the same TestClient socket are not one
    person, and a client that forges X-Forwarded-For is not a new bucket."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    client = TestClient(newsletter.app)
    for i in range(newsletter._SUBSCRIBE_MAX):
        _post(client, name="Ada", email=f"a{i}@example.com")
    # Same socket, different real IP: a new window.
    r = client.post(
        "/subscribe",
        data={"name": "Ada", "email": "other@example.com"},
        headers={"X-Real-IP": "203.0.113.9"},
        follow_redirects=False,
    )
    assert r.headers["location"] == newsletter.OK_URL
    assert "other@example.com" in pool.store
    # Forged XFF must not open a new window on the TestClient IP.
    r = client.post(
        "/subscribe",
        data={"name": "Ada", "email": "forged@example.com"},
        headers={"X-Forwarded-For": "198.51.100.1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == newsletter.FAIL_URL
    assert "forged@example.com" not in pool.store


# --- the limiter's own memory -----------------------------------------------

def test_the_limiter_does_not_grow_one_entry_per_address_forever():
    """A public form with no CAPTCHA, on a process that runs for weeks. An IP
    is only pruned when it comes back, so without a sweep the table keeps one
    entry for every address that ever submitted -- and rotating the source
    address is the cheapest thing an abuser does."""
    from newsletter import _SWEEP_AT, _SUBSCRIBE_WINDOW_S, _subscribe_allowed, _subscribe_hits, reset_subscribe_limiter

    reset_subscribe_limiter()
    now = 1_000_000.0
    for i in range(_SWEEP_AT + 5):
        _subscribe_allowed(f"10.0.{i // 256}.{i % 256}", now=now)

    assert len(_subscribe_hits) > _SWEEP_AT, "nothing to sweep yet; the test is not exercising it"
    # One more, a full window later: everything above is now stale.
    _subscribe_allowed("10.9.9.9", now=now + _SUBSCRIBE_WINDOW_S + 1)
    assert len(_subscribe_hits) == 1, f"the sweep left {len(_subscribe_hits)} entries"
    reset_subscribe_limiter()


def test_a_sweep_never_drops_an_address_still_inside_its_window():
    """The sweep must not hand someone a fresh allowance by forgetting them
    mid-window -- that would make the limiter bypassable by waiting for a
    busy moment rather than by waiting out the window."""
    from newsletter import _SUBSCRIBE_MAX, _SUBSCRIBE_WINDOW_S, _subscribe_allowed, _sweep, reset_subscribe_limiter

    reset_subscribe_limiter()
    now = 2_000_000.0
    for _ in range(_SUBSCRIBE_MAX):
        assert _subscribe_allowed("198.51.100.7", now=now) is True
    assert _subscribe_allowed("198.51.100.7", now=now) is False

    _sweep(now + _SUBSCRIBE_WINDOW_S - 1)
    assert _subscribe_allowed("198.51.100.7", now=now) is False, "the sweep reset a live window"

    _sweep(now + _SUBSCRIBE_WINDOW_S + 1)
    assert _subscribe_allowed("198.51.100.7", now=now + _SUBSCRIBE_WINDOW_S + 1) is True
    reset_subscribe_limiter()


class _FakeGitHub:
    def __init__(self, release=None, fail=False):
        self.release, self.fail, self.calls = release, fail, 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self.calls += 1
        if self.fail:
            raise RuntimeError("github down")
        release = self.release

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return release
        return _R()


def test_the_windows_download_follows_the_latest_full_release(monkeypatch):
    """The installer's name carries its version; the page links here instead
    and this sends the browser to whatever release is latest now."""
    newsletter.reset_download_cache()
    fake = _FakeGitHub({"assets": [
        {"name": "latest.json", "browser_download_url": "https://github.com/DJG3DK/persei/releases/download/v0.9.0/latest.json"},
        {"name": "Persei_0.9.0_x64-setup.exe", "browser_download_url": "https://github.com/DJG3DK/persei/releases/download/v0.9.0/Persei_0.9.0_x64-setup.exe"},
        {"name": "Persei_0.9.0_x64-setup.exe.sig", "browser_download_url": "https://github.com/DJG3DK/persei/releases/download/v0.9.0/Persei_0.9.0_x64-setup.exe.sig"},
    ]})
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", fake)
    with TestClient(newsletter.app) as c:
        _register(c)
        r = c.get("/download/windows", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].endswith("/v0.9.0/Persei_0.9.0_x64-setup.exe")
        c.get("/download/windows", follow_redirects=False)
    assert fake.calls == 1, "cached"


def test_the_windows_download_falls_back_to_the_release_page(monkeypatch):
    newsletter.reset_download_cache()
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", _FakeGitHub(fail=True))
    with TestClient(newsletter.app) as c:
        _register(c)
        r = c.get("/download/windows", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == newsletter.RELEASES_PAGE
    newsletter.reset_download_cache()
    other = _FakeGitHub({"assets": [{"name": "Persei_0.9.0_x64-setup.exe",
                                     "browser_download_url": "https://evil.example/x.exe"}]})
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", other)
    with TestClient(newsletter.app) as c:
        _register(c)
        r = c.get("/download/windows", follow_redirects=False)
    assert r.headers["location"] == newsletter.RELEASES_PAGE, "only this repository's release URLs"


def test_the_linux_downloads_follow_the_latest_full_release(monkeypatch):
    base = "https://github.com/DJG3DK/persei/releases/download/v0.9.2/"
    newsletter.reset_download_cache()
    fake = _FakeGitHub({"assets": [
        {"name": n, "browser_download_url": base + n} for n in (
            "Persei_0.9.2_x64-setup.exe", "Persei_0.9.2_amd64.AppImage",
            "Persei_0.9.2_amd64.AppImage.sig", "Persei_0.9.2_amd64.deb", "latest.json")
    ]})
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", fake)
    with TestClient(newsletter.app) as c:
        _register(c)
        assert c.get("/download/linux", follow_redirects=False).headers["location"] == base + "Persei_0.9.2_amd64.AppImage"
        assert c.get("/download/linux-deb", follow_redirects=False).headers["location"] == base + "Persei_0.9.2_amd64.deb"
        assert c.get("/download/windows", follow_redirects=False).headers["location"] == base + "Persei_0.9.2_x64-setup.exe"


def test_a_release_without_a_linux_app_sends_linux_to_the_release_page(monkeypatch):
    newsletter.reset_download_cache()
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", _FakeGitHub({"assets": [
        {"name": "Persei_0.9.1_x64-setup.exe",
         "browser_download_url": "https://github.com/DJG3DK/persei/releases/download/v0.9.1/Persei_0.9.1_x64-setup.exe"}]}))
    with TestClient(newsletter.app) as c:
        _register(c)
        r = c.get("/download/linux", follow_redirects=False)
    assert r.headers["location"] == newsletter.RELEASES_PAGE


# ── Downloads behind a name and an address (2026-10-21) ──────────────────────

def _register(client, reg_id: int = 1):
    """A browser that has registered already: it carries the signed cookie."""
    client.cookies.set(newsletter.COOKIE, newsletter._sign(reg_id))


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(newsletter, "pool", fake)
    return fake


def _client():
    # No lifespan: the pool is the fake one the fixture put in place.
    return TestClient(newsletter.app, base_url="https://perseiagent.ai")


def test_a_download_link_without_registering_goes_to_the_form():
    c = _client()
    for kind in ("windows", "linux", "linux-deb"):
        r = c.get(f"/download/{kind}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/get/{kind}"


def test_the_form_asks_for_a_name_and_an_email_and_leaves_updates_unticked():
    r = _client().get("/get/windows")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    page = r.text
    assert 'name="name"' in page and 'name="email"' in page and 'action="/get/windows"' in page
    assert 'name="updates"' in page and "checked" not in page, "opting in is the visitor's choice"
    assert "<script" not in page, "the site ships no JavaScript"
    assert "danny@perseiagent.ai" in page, "how to have the data deleted"


_LINUX_BASE = "https://github.com/DJG3DK/persei/releases/download/v0.9.4/"
_LINUX_RELEASE = {"assets": [{"name": n, "browser_download_url": _LINUX_BASE + n}
                             for n in ("Persei_0.9.4_amd64.AppImage", "Persei_0.9.4_amd64.deb")]}


def test_registering_stores_the_row_sets_the_cookie_and_starts_the_download(pool, monkeypatch):
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", _FakeGitHub(_LINUX_RELEASE))
    c = _client()
    r = c.post("/get/linux", data={"name": "  Ada   Lovelace ", "email": " Ada@Example.com "},
               follow_redirects=False)
    assert r.status_code == 200
    assert pool.regs == [{"name": "Ada Lovelace", "email": "ada@example.com", "platform": "linux", "updates": False}]
    assert pool.store == {}, "not on the newsletter without the box ticked"
    cookie = r.headers["set-cookie"]
    assert newsletter.COOKIE in cookie and "HttpOnly" in cookie and "Secure" in cookie
    for page in (r.text, c.get("/get/linux").text):
        assert f'http-equiv="refresh" content="1;url={_LINUX_BASE}Persei_0.9.4_amd64.AppImage"' in page
        assert f'href="{_LINUX_BASE}Persei_0.9.4_amd64.deb"' in page
        assert "install-linux.sh | sh" in page, "the one-liner shows once registered"
        assert "<script" not in page
    assert c.get("/download/linux", follow_redirects=False).status_code == 302


def test_a_browser_that_blocks_cookies_still_gets_its_download_and_one_row(pool, monkeypatch):
    """The POST used to 303 to GET /get/<kind>, which asks for the cookie it
    had just set: without cookies that was the form again, and every lap
    stored another row (2026-10-22, audit S4). The answer is now the download
    page itself, pointing at the file."""
    monkeypatch.setattr(newsletter.httpx, "AsyncClient", _FakeGitHub(_LINUX_RELEASE))
    c = _client()
    r = c.post("/get/linux-deb", data={"name": "Ada", "email": "ada@example.com"}, follow_redirects=False)
    c.cookies.clear()
    assert r.status_code == 200 and "Your download is starting" in r.text
    assert f'content="1;url={_LINUX_BASE}Persei_0.9.4_amd64.deb"' in r.text
    assert '"/download/' not in r.text and "url=/download/" not in r.text, "no link that asks for the cookie"
    assert len(pool.regs) == 1


def test_the_download_page_falls_back_to_the_release_page(pool):
    r = _client().post("/get/windows", data={"name": "Ada", "email": "ada@example.com"}, follow_redirects=False)
    assert r.status_code == 200
    assert f'content="1;url={newsletter.RELEASES_PAGE}"' in r.text


def test_ticking_updates_adds_them_to_the_newsletter(pool):
    _client().post("/get/windows", data={"name": "Ada", "email": "ada@example.com", "updates": "1"},
                   follow_redirects=False)
    assert pool.regs[0]["updates"] is True
    assert pool.store["ada@example.com"]["source"] == "download"


def test_a_bad_entry_shows_the_form_again_with_what_was_typed(pool):
    r = _client().post("/get/windows", data={"name": "Ada", "email": "not-an-address"})
    assert r.status_code == 400 and "doesn&#x27;t look right" in r.text
    assert 'value="not-an-address"' in r.text and pool.regs == []
    r = _client().post("/get/windows", data={"name": "", "email": "ada@example.com"})
    assert r.status_code == 400 and pool.regs == []


def test_a_typed_value_cannot_break_out_of_the_page(pool):
    r = _client().post("/get/windows", data={"name": '"><script>alert(1)</script>', "email": "x"})
    assert "<script>alert" not in r.text


def test_bots_and_other_sites_get_nothing_stored(pool):
    c = _client()
    r = c.post("/get/windows", data={"name": "Bot", "email": "b@example.com", newsletter.HONEYPOT_FIELD: "x"},
               follow_redirects=False)
    assert r.status_code == 303 and pool.regs == [] and newsletter.COOKIE not in r.headers.get("set-cookie", "")
    r = c.post("/get/windows", data={"name": "Ada", "email": "ada@example.com"},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 400 and pool.regs == []


def test_a_forged_or_unknown_cookie_does_not_count():
    c = _client()
    c.cookies.set(newsletter.COOKIE, "1.1792584140.0000000000000000000000000000000")
    assert c.get("/download/windows", follow_redirects=False).headers["location"] == "/get/windows"
    assert c.get("/get/nonsense", follow_redirects=False).status_code == 303


def test_the_env_example_names_every_setting_the_service_reads():
    """DOWNLOAD_COOKIE_SECRET was missing, so a deploy from the example had a
    random key per process and every pm2 restart dropped every cookie
    (2026-10-22, audit S5). LANGGRAPH_PG_DSN is the agent's own fallback."""
    import re

    here = Path(__file__).resolve().parent
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"\)', (here / "newsletter.py").read_text()))
    example = (here / ".env.example").read_text()
    missing = sorted(k for k in read - {"LANGGRAPH_PG_DSN"} if not re.search(rf"^{k}=", example, re.M))
    assert "DOWNLOAD_COOKIE_SECRET" in read
    assert missing == []


# ── What the privacy page promises (2026-10-22, audit S3) ────────────────────

PRIVACY_PAGE = Path(__file__).resolve().parent.parent / "public" / "privacy" / "index.html"


def test_the_privacy_page_names_the_download_cookie_its_lifetime_and_the_retention():
    """The page said "no cookies" while the download form set a year-long one,
    and it gave no retention period while both tables were kept forever."""
    page = PRIVACY_PAGE.read_text(encoding="utf-8")
    assert newsletter.COOKIE in page
    assert f"{newsletter._COOKIE_MAX_AGE // 86400} days" in page
    assert f"{newsletter.RETENTION_MONTHS} months" in page
    assert "(no cookies" not in page, "the old blanket claim"
    assert newsletter._COOKIE_MAX_AGE < newsletter.RETENTION_MONTHS * 30 * 86400, \
        "a live cookie must not outlive its registration"


class _RecordingPool(FakePool):
    def __init__(self, fail=False):
        super().__init__()
        self.sql: list[tuple[str, tuple]] = []
        self.purge_fail = fail

    def connection(self):
        outer = self

        class C(FakeConn):
            async def execute(self, sql, params=None):
                if outer.purge_fail:
                    import psycopg
                    raise psycopg.OperationalError("down")
                outer.sql.append((" ".join(sql.split()), params))
                return FakeCursor([])

        return C(self.store)


def test_the_purge_deletes_old_registrations_and_long_unsubscribed_rows(monkeypatch):
    import asyncio

    pool = _RecordingPool()
    monkeypatch.setattr(newsletter, "pool", pool)
    asyncio.run(newsletter.purge_expired())
    statements = [s for s, _ in pool.sql]
    assert any(s.startswith("DELETE FROM download_registrations WHERE created_at <") for s in statements)
    subs = [s for s in statements if s.startswith("DELETE FROM newsletter_subscribers")]
    assert subs and "unsubscribed_at IS NOT NULL" in subs[0], "a current subscriber is never purged"
    assert all(p == (newsletter.RETENTION_MONTHS,) for _, p in pool.sql)


def test_a_failed_purge_is_logged_not_raised(monkeypatch, caplog):
    import asyncio

    monkeypatch.setattr(newsletter, "pool", _RecordingPool(fail=True))
    with caplog.at_level("ERROR"):
        asyncio.run(newsletter.purge_expired())
    assert "purge" in caplog.text
