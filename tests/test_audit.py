"""The audit log: what it records, what it refuses to break, and who reads it.

The premise is in agent/audit.py -- Telegram is a notification channel, not a
record. These hold the three properties that make the log worth trusting:
every action in the registry is actually recorded somewhere, a failing write
never breaks the thing being audited, and the page is admin-only because it
names accounts.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from agent import audit

REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeStore:
    """The three methods audit.py uses, plus a switch for making them fail."""

    def __init__(self, fail: set[str] | None = None):
        self.rows: dict[str, dict] = {}
        self.fail = fail or set()

    async def aput(self, ns, key, value):
        if "put" in self.fail:
            raise RuntimeError("database is down")
        self.rows[key] = dict(value)

    async def asearch(self, ns, limit=100):
        if "search" in self.fail:
            raise RuntimeError("database is down")
        items = [type("Item", (), {"key": k, "value": v})() for k, v in self.rows.items()]
        return items[:limit]

    async def adelete(self, ns, key):
        self.rows.pop(key, None)


def test_a_record_carries_who_what_and_when():
    store = FakeStore()
    entry = asyncio.run(audit.record(store, actor="operator@example.com", action="project.onboard",
                                     target="storefront", detail="/home/storefront"))
    assert entry["actor"] == "operator@example.com"
    assert entry["action"] == "project.onboard"
    assert entry["target"] == "storefront"
    assert entry["ts"] > 0
    assert len(store.rows) == 1


def test_an_unknown_action_is_refused_rather_than_stored():
    """A typo'd action has no label in the UI and nothing searches for it.
    Better to lose it loudly here than to store a row nobody will find."""
    store = FakeStore()
    assert asyncio.run(audit.record(store, actor="a@b.c", action="settings.auto_aprove")) is None
    assert store.rows == {}


def test_a_failing_store_never_breaks_the_action_being_audited():
    """An approval that fails because the log write failed would make the log
    a new way to break the system. The trade is stated in audit.py."""
    store = FakeStore(fail={"put"})
    assert asyncio.run(audit.record(store, actor="a@b.c", action="command.approve")) is None


def test_no_store_at_all_is_not_an_error():
    assert asyncio.run(audit.record(None, actor="a@b.c", action="command.approve")) is None
    assert asyncio.run(audit.recent(None)) == []


def test_recent_is_newest_first_and_labelled():
    store = FakeStore()
    for i, action in enumerate(["project.onboard", "command.approve", "deploy_key.generate"]):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action=action))
    rows = asyncio.run(audit.recent(store, limit=10))
    assert [r["action"] for r in rows] == ["deploy_key.generate", "command.approve", "project.onboard"]
    assert rows[0]["label"] == audit.ACTIONS["deploy_key.generate"]


def test_recent_survives_a_malformed_row():
    """One bad row must not blank the page."""
    store = FakeStore()
    asyncio.run(audit.record(store, actor="a@b.c", action="command.approve"))
    store.rows["junk"] = {"not": "an entry"}
    store.rows["alsojunk"] = None
    rows = asyncio.run(audit.recent(store))
    assert len(rows) == 1


def test_recent_survives_an_unreadable_store():
    store = FakeStore(fail={"search"})
    assert asyncio.run(audit.recent(store)) == []


def test_keys_sort_in_time_order():
    """The store has no ORDER BY for us to use, so the key has to carry it."""
    keys = [audit._key(t) for t in (1.0, 2.5, 1000.0, 1789000000.123)]
    assert keys == sorted(keys)


def test_keys_written_in_the_same_instant_still_order():
    """A burst is exactly when the trim runs, and at millisecond resolution a
    burst shared a key prefix -- leaving a random suffix to decide which
    records counted as newest."""
    now = 1789000000.000001
    keys = [audit._key(now) for _ in range(50)]
    assert keys == sorted(keys), "records written in one instant must still order by write order"
    assert len(set(keys)) == 50


def test_the_log_is_trimmed_rather_than_growing_without_bound(monkeypatch):
    monkeypatch.setattr(audit, "MAX_RECORDS", 5)
    monkeypatch.setattr(audit, "_TRIM_EVERY", 3)
    monkeypatch.setattr(audit, "_writes_since_trim", 0)
    store = FakeStore()
    for i in range(24):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action="command.approve", detail=str(i)))
    assert len(store.rows) <= audit.MAX_RECORDS * 2
    # and what survives is the newest
    kept = sorted((r["detail"] for r in store.rows.values()), key=int)
    assert kept[-1] == "23"


def _agent_sources() -> str:
    return "\n".join(p.read_text() for p in (REPO / "agent").rglob("*.py")
                     if p.name != "audit.py")


@pytest.mark.parametrize("action", sorted(audit.ACTIONS))
def test_every_registered_action_is_recorded_somewhere(action):
    """An action in the table that no call site ever writes is a promise the
    page cannot keep: the operator reads the log, sees no entry, and concludes
    it did not happen.

    A call site may build the name -- `f"inbox.{action}"` covers approve,
    dismiss and snooze in one place -- so a matching prefix counts. The
    unknown-action guard in audit.py is what catches a family member that
    does not exist; this catches one nobody writes at all.
    """
    sources = _agent_sources()
    prefix = action.split(".")[0]
    assert f'"{action}"' in sources or f'action=f"{prefix}.{{' in sources, \
        f"{action} is in ACTIONS and nothing records it"


def test_every_recorded_action_is_in_the_registry():
    """The other direction: a call site using a name the registry does not
    know is dropped at write time (see the unknown-action test), so this is
    the test that catches it before an operator loses a record."""
    used = set()
    for path in (REPO / "agent").rglob("*.py"):
        if path.name == "audit.py":
            continue
        for m in re.finditer(r'action="([a-z0-9_]+\.[a-z0-9_]+)"', path.read_text()):
            used.add(m.group(1))
    unknown = used - set(audit.ACTIONS)
    assert not unknown, f"recorded but not in ACTIONS (they would be dropped): {sorted(unknown)}"

    # And every dynamic family has at least one member registered, so
    # `f"inbox.{action}"` cannot survive the whole family being deleted.
    for m in re.finditer(r'action=f"([a-z_]+)\.\{', _agent_sources()):
        prefix = m.group(1)
        assert any(a.startswith(prefix + ".") for a in audit.ACTIONS), \
            f"{prefix}.* is recorded and no such action is registered"


# ---------------------------------------------------------------------------
# Reading and trimming must not depend on the order rows come back in
# ---------------------------------------------------------------------------

class ShuffledStore(FakeStore):
    """A store that returns rows in an order nobody promised -- reversed, and
    windowed. The real one makes no ordering guarantee either; this one just
    stops the tests from accidentally relying on insertion order."""

    def __init__(self, page: int = 3):
        super().__init__()
        self.page = page

    async def asearch(self, ns, limit=100, offset=0):
        items = [type("Item", (), {"key": k, "value": v})()
                 for k, v in reversed(list(self.rows.items()))]
        window = items[offset:offset + min(limit, self.page)]
        return window


def test_trimming_keeps_the_newest_even_when_the_store_pages_and_shuffles():
    """The first version asked for a window and deleted the oldest rows IN
    THAT WINDOW. With an arbitrary order that deletes recent entries while
    genuinely old ones sit outside the window forever."""
    store = ShuffledStore(page=3)
    for i in range(30):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action="command.approve", detail=str(i)))

    import agent.audit as mod
    old_max, old_every = mod.MAX_RECORDS, mod._TRIM_EVERY
    try:
        mod.MAX_RECORDS, mod._TRIM_EVERY, mod._writes_since_trim = 10, 1, 0
        asyncio.run(audit.record(store, actor="last@x", action="command.approve", detail="30"))
        kept = sorted(int(v["detail"]) for v in store.rows.values())
        assert len(kept) == 10
        assert kept == list(range(21, 31)), f"the newest ten should survive, got {kept}"
    finally:
        mod.MAX_RECORDS, mod._TRIM_EVERY = old_max, old_every


def test_recent_sees_every_page_not_just_the_first():
    store = ShuffledStore(page=2)
    for i in range(9):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action="command.approve", detail=str(i)))
    rows = asyncio.run(audit.recent(store, limit=100))
    assert len(rows) == 9, "a paging store must not hide older entries from the page"
    assert rows[0]["detail"] == "8", "still newest first"


def test_a_store_without_offset_support_still_works():
    """FakeStore's asearch takes no offset -- the older shape. Reading must
    fall back to one call rather than raising TypeError at the operator."""
    store = FakeStore()
    for i in range(4):
        asyncio.run(audit.record(store, actor=f"u{i}@x", action="command.approve", detail=str(i)))
    assert len(asyncio.run(audit.recent(store))) == 4


class LimitRecordingStore(FakeStore):
    """No `offset`, and it honours `limit` exactly -- which is the whole
    point: the fallback read is the only read there will be, so whatever it
    asks for is all the trim will ever get to see."""

    def __init__(self):
        super().__init__()
        self.limits: list[int] = []

    async def asearch(self, ns, limit=100):
        self.limits.append(limit)
        items = [type("Item", (), {"key": k, "value": v})() for k, v in sorted(self.rows.items())]
        return items[:limit]


def test_trimming_still_happens_on_a_store_that_has_no_offset():
    """The trim spots an overflowing log by reading PAST the cap.

    Lifting the paging loop out of this module nearly cost that: the
    fallback read for a store with no `offset` became a constant that
    happened to equal MAX_RECORDS, so the trim would have been handed
    exactly MAX_RECORDS rows for ever, concluded the log was not over its
    cap, and never deleted another row.
    """
    store = LimitRecordingStore()
    for i in range(audit.MAX_RECORDS + 50):
        store.rows[audit._key(1_700_000_000.0 + i)] = {
            "ts": 1_700_000_000.0 + i, "actor": "u@x", "action": "command.approve", "detail": str(i),
        }

    import agent.audit as mod
    old_every = mod._TRIM_EVERY
    try:
        mod._TRIM_EVERY, mod._writes_since_trim = 1, 0
        asyncio.run(audit.record(store, actor="last@x", action="command.approve", detail="new"))
    finally:
        mod._TRIM_EVERY = old_every

    assert store.limits, "the trim never read the log"
    assert min(store.limits) > audit.MAX_RECORDS, (
        "the fallback read has to clear the cap it is being compared against, "
        f"asked for {min(store.limits)} with a cap of {audit.MAX_RECORDS}"
    )
    assert len(store.rows) == audit.MAX_RECORDS



# ---------------------------------------------------------------------------
# Account and access changes are recorded (audit B33, 2026-10-22)
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(monkeypatch):
    from fastapi.testclient import TestClient

    import agent.server as srv
    from agent.auth import User
    from agent.routers import auth as auth_routes
    from agent.routers import env_config as env_routes

    me = User(id=1, email="admin@b.co", role="admin", allowed_repos=None, totp_enabled=True,
              must_change_password=False)
    recorded = []

    async def record(store, **kw):
        recorded.append(kw)

    monkeypatch.setattr(auth_routes.audit, "record", record)
    monkeypatch.setattr(env_routes.audit, "record", record)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.get_current_user, lambda: me)
    client = TestClient(srv.app)
    client.recorded = recorded
    return client


def test_creating_changing_and_deleting_an_account_are_recorded(admin_client, monkeypatch):
    import agent.server as srv
    from agent import config as agent_config
    from agent import features

    row = {"id": 2, "email": "sam@b.co", "role": "user", "allowed_repos": ["proj"], "totp_enabled": False,
           "must_change_password": True, "auto_approve_commands": False, "require_merge_review": True}

    async def none(*a, **k):
        return None

    async def created(*a, **k):
        return row

    async def by_id(pool, uid):
        return row

    monkeypatch.setattr(features, "enabled", lambda name: True)
    monkeypatch.setattr(agent_config, "PROJECTS", {"proj": {}, "other": {}})
    monkeypatch.setattr(srv.auth, "get_user_by_email", none)
    monkeypatch.setattr(srv.auth, "create_user", created)
    monkeypatch.setattr(srv.auth, "get_user_by_id", by_id)
    monkeypatch.setattr(srv.auth, "update_user_access", none)
    monkeypatch.setattr(srv.auth, "delete_user", none)

    r = admin_client.post("/api/auth/users", json={"email": "sam@b.co", "password": "Long-enough-passw0rd!",
                                                   "role": "user", "allowed_repos": ["proj"]})
    assert r.status_code == 201, r.text
    assert admin_client.patch("/api/auth/users/2", json={"allowed_repos": ["proj", "other"]}).status_code == 200
    assert admin_client.delete("/api/auth/users/2").status_code == 200
    got = [(e["action"], e["actor"], e["target"], e.get("detail")) for e in admin_client.recorded]
    assert got == [
        ("auth.user_create", "admin@b.co", "sam@b.co", "user with proj"),
        ("auth.user_access", "admin@b.co", "sam@b.co", "other, proj"),
        ("auth.user_delete", "admin@b.co", "sam@b.co", None),
    ]


def test_turning_off_2fa_is_recorded(admin_client, monkeypatch):
    import agent.server as srv
    from agent.auth import User

    me = User(id=3, email="u@b.co", role="user", allowed_repos=[], totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)

    async def user_row(pool, uid):
        return {"id": 3, "password_hash": "right"}

    async def none(*a, **k):
        return None

    monkeypatch.setattr(srv.auth, "get_user_by_id", user_row)
    monkeypatch.setattr(srv.auth, "verify_password", lambda pw, h: pw == h)
    monkeypatch.setattr(srv.auth, "disable_totp", none)
    assert admin_client.post("/api/auth/2fa/disable", json={"password": "right"}).status_code == 200
    assert [(e["action"], e["target"]) for e in admin_client.recorded] == [("auth.2fa_disable", "u@b.co")]


def test_an_env_change_records_the_key_names_and_never_a_value(admin_client, monkeypatch):
    from agent import env_config

    monkeypatch.setattr(env_config, "set_keys",
                        lambda updates: {"updated": sorted(updates), "restart_required": []})
    r = admin_client.post("/api/env-config", json={"updates": {"OPENROUTER_API_KEY": "sk-secret-value"}})
    assert r.status_code == 200, r.text
    (entry,) = admin_client.recorded
    assert entry["action"] == "env.update" and entry["detail"] == "OPENROUTER_API_KEY"
    assert "sk-secret-value" not in repr(entry)
