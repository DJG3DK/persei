"""Planning spend is part of the task's cost (2026-09-28). A session's spend
is carried onto the task Build Now starts, once; Analytics counts it on the
task, and counts a plan that never became a task as its own category."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import live_state, tasks
from agent.classify import TaskClassification


class _Item:
    def __init__(self, value, key=None):
        self.value = value
        self.key = key


class _Store:
    def __init__(self, records):
        self.records = records   # {(ns, key): value}

    async def aget(self, ns, key):
        v = self.records.get((tuple(ns), key))
        return _Item(dict(v)) if v is not None else None

    async def aput(self, ns, key, value):
        self.records[(tuple(ns), key)] = dict(value)

    async def asearch(self, ns, limit=100):
        return [_Item(dict(v), k) for (n, k), v in self.records.items() if n == tuple(ns)]


def _app(store):
    async def stream(task_id, repo, goal, budget, state, **kw):
        return None

    async def classify(goal, config):
        return TaskClassification(category="feature", needs_tests=False)

    tasks.classify_task = classify
    return SimpleNamespace(state=SimpleNamespace(stream_graph=stream, config=object(), store=store))


def _start(app, session_id):
    async def go():
        out = await tasks.start_task(app, "build the thing", "proj", 3.0, "auto",
                                     auto_approve_commands=False, require_merge_review=True,
                                     planning_session_id=session_id)
        await live_state.running_tasks.pop(out["task_id"])
        return out["task_id"]
    return asyncio.run(go())


def test_the_sessions_spend_is_carried_onto_the_task_it_starts_once():
    store = _Store({(("planning", "proj"), "s1"): {"session_id": "s1", "repo": "proj", "cost_usd": 1.25}})
    app = _app(store)
    t1 = _start(app, "s1")
    meta = store.records[(("tasks", "proj"), t1)]
    assert meta["planning_cost_usd"] == 1.25 and meta["planning_session_id"] == "s1"
    sess = store.records[(("planning", "proj"), "s1")]
    assert sess["carried_cost_usd"] == 1.25 and sess["built_task_ids"] == [t1]
    # more planning, a second build: only the new spend is carried
    sess["cost_usd"] = 1.75
    t2 = _start(app, "s1")
    assert store.records[(("tasks", "proj"), t2)]["planning_cost_usd"] == 0.5
    assert store.records[(("planning", "proj"), "s1")]["carried_cost_usd"] == 1.75


def test_a_carry_that_cannot_be_recorded_carries_nothing():
    """The carry once returned the amount even when the write failed, so the
    next build from the same session carried it again (2026-09-29). Unrecorded
    means uncarried: the spend stays on the session as its own category."""
    class _WriteFails(_Store):
        async def aput(self, ns, key, value):
            raise RuntimeError("database is down")

    store = _WriteFails({(("planning", "proj"), "s-w"): {"session_id": "s-w", "repo": "proj", "cost_usd": 1.25}})
    assert asyncio.run(tasks.carry_planning_cost(store, "proj", "s-w", "t1")) == 0.0
    assert "carried_cost_usd" not in store.records[(("planning", "proj"), "s-w")]


def test_a_live_cost_mirror_during_the_carry_is_not_written_away():
    """Both the carry and server._mirror_planning_cost read, modify and write
    the whole session row. Unserialised, whichever wrote second erased the
    other's change (A7, 2026-09-29)."""
    import agent.server as srv

    class _Slow(_Store):
        async def aget(self, ns, key):
            await asyncio.sleep(0.02)      # a store round trip, long enough for the other writer to arrive
            return await super().aget(ns, key)

    store = _Slow({(("planning", "proj"), "s-m"): {"session_id": "s-m", "repo": "proj", "cost_usd": 1.25}})

    async def both():
        return await asyncio.gather(
            tasks.carry_planning_cost(store, "proj", "s-m", "t1"),
            srv._mirror_planning_cost(store, "proj", "s-m", 2.0),
        )

    carried, _ = asyncio.run(both())
    row = store.records[(("planning", "proj"), "s-m")]
    assert carried == 1.25
    assert row["carried_cost_usd"] == 1.25 and row["built_task_ids"] == ["t1"], "the mirror wrote the carry away"
    assert row["cost_usd"] == 2.0, "the carry wrote the live cost away"


def test_a_task_without_a_session_or_with_an_unknown_one_carries_nothing():
    store = _Store({})
    app = _app(store)
    t = _start(app, None)
    rec = store.records.get((("tasks", "proj"), t)) or {}
    assert "planning_cost_usd" not in rec, "no session, nothing carried, nothing written for it"
    t = _start(app, "ghost")
    assert store.records[(("tasks", "proj"), t)]["planning_cost_usd"] == 0.0


def test_analytics_counts_planning_on_the_task_and_stranded_plans_as_their_own_category(monkeypatch):
    import agent.server as srv
    from agent.auth import User
    from agent.routers import analytics as an

    store = _Store({
        (("tasks", "proj"), "t1"): {"task_id": "t1", "repo": "proj", "goal": "g", "category": "feature",
                                    "cost_so_far": 2.0, "planning_cost_usd": 0.5, "budget_usd": 5, "status": "done",
                                    "created_at": 1_790_000_000},
        (("planning", "proj"), "s1"): {"session_id": "s1", "repo": "proj", "cost_usd": 0.5, "carried_cost_usd": 0.5},
        (("planning", "proj"), "s2"): {"session_id": "s2", "repo": "proj", "cost_usd": 0.8},
    })
    monkeypatch.setattr(an, "PROJECTS", {"proj": {}})
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    body = TestClient(srv.app).get("/api/analytics").json()
    per_task = {t["task_id"]: t for t in body["per_task"]}
    assert per_task["t1"]["cost"] == 2.5 and per_task["t1"]["planning_cost"] == 0.5
    cats = {c["category"]: c for c in body["by_category"]}
    assert cats["feature"]["cost"] == 2.5, "the task's category carries the whole cost"
    assert cats["planning"] == {"category": "planning", "tasks": 1, "cost": 0.8}, "only the plan that never became a task"


def test_the_three_totals_on_the_analytics_page_agree(monkeypatch):
    """per_repo left the carried planning cost out, and the stranded plans
    were only in by_category, so the page showed three different totals for
    one set of spend (2026-09-29)."""
    import agent.server as srv
    from agent.auth import User
    from agent.routers import analytics as an

    # Days relative to now: the daily series covers the last 14 days, and
    # fixed September timestamps fell out of it on 2026-10-05.
    import time
    made_ts = int(time.time()) - 3 * 86400
    touched_ts = made_ts + 86400
    store = _Store({
        (("tasks", "proj"), "t1"): {"task_id": "t1", "repo": "proj", "goal": "g", "category": "feature",
                                    "cost_so_far": 2.0, "planning_cost_usd": 0.5, "budget_usd": 5, "status": "done",
                                    "created_at": made_ts},
        (("tasks", "other"), "t2"): {"task_id": "t2", "repo": "other", "goal": "g", "category": "bugfix",
                                     "cost_so_far": 1.0, "budget_usd": 5, "status": "done", "created_at": made_ts},
        (("planning", "proj"), "s1"): {"session_id": "s1", "repo": "proj", "cost_usd": 0.5, "carried_cost_usd": 0.5},
        (("planning", "proj"), "s2"): {"session_id": "s2", "repo": "proj", "cost_usd": 0.8, "updated_at": touched_ts},
        (("planning", "other"), "s3"): {"session_id": "s3", "repo": "other", "cost_usd": 0.3, "created_at": made_ts},
    })
    monkeypatch.setattr(an, "PROJECTS", {"proj": {}, "other": {}})
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    body = TestClient(srv.app).get("/api/analytics").json()
    expected = 2.0 + 0.5 + 1.0 + 0.8 + 0.3
    assert round(body["total_cost"], 6) == expected
    assert round(sum(c["cost"] for c in body["by_category"]), 6) == expected
    assert round(sum(r["cost"] for r in body["per_repo"].values()), 6) == expected
    assert round(body["per_repo"]["proj"]["cost"], 6) == 2.0 + 0.5 + 0.8
    assert round(body["per_repo"]["other"]["cost"], 6) == 1.0 + 0.3
    from datetime import UTC, datetime
    days = {d["date"]: d for d in body["daily"]}
    touched = datetime.fromtimestamp(touched_ts, tz=UTC).strftime("%Y-%m-%d")
    made = datetime.fromtimestamp(made_ts, tz=UTC).strftime("%Y-%m-%d")
    if touched != made:
        assert round(days[touched]["cost"], 6) == 0.8, "a stranded plan lands on the day it was last touched"



def test_analytics_counts_every_task_not_the_newest_200(monkeypatch):
    """B38 (2026-10-22): each project's tasks were read with
    asearch(limit=200), so the 201st and later were left out of every total."""
    import agent.server as srv
    from agent.auth import User
    from agent.routers import analytics as an

    class _Paged(_Store):
        async def asearch(self, ns, limit=100, offset=0):
            rows = [_Item(dict(v), k) for (n, k), v in self.records.items() if n == tuple(ns)]
            return rows[offset:offset + min(limit, 200)]

    store = _Paged({(("tasks", "proj"), f"t{n}"): {"task_id": f"t{n}", "repo": "proj", "goal": "g",
                                                   "category": "feature", "cost_so_far": 1.0, "budget_usd": 5,
                                                   "status": "done", "created_at": 1_790_000_000 + n}
                    for n in range(201)})
    monkeypatch.setattr(an, "PROJECTS", {"proj": {}})
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    body = TestClient(srv.app).get("/api/analytics").json()
    assert body["total_tasks"] == 201
    assert round(body["total_cost"], 6) == 201.0
