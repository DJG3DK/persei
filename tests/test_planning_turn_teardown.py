"""A planning turn that ends abnormally must not throw away what it earned.

Two exits end a turn without reaching the success path: the operator pressing
Stop (asyncio.CancelledError) and an exception. Both used to discard a plan the
model had ALREADY saved during that turn -- run_planning_turn returns
plan_ref["markdown"], so a turn that dies never returns it, and plan_ref is the
only place that draft exists. The error path additionally banked no cost at
all, under-reporting a session's real spend every time a turn failed.

Live trigger (2026-08-27): a tool raised NotADirectoryError on a git worktree's
`.git/HEAD`, langgraph re-raised it (see agent/tools/tool_errors.py), and the
whole turn died -- red error in the UI, and any plan written during it gone.
"""
from __future__ import annotations

import asyncio

import pytest

import agent.server as server
from agent.classify import TaskClassification


class _Item:
    def __init__(self, value: dict):
        self.value = value


class _FakeStore:
    """Just the methods _bank_planning_turn and the turn handler use.

    Namespace-aware, and it has to be: a turn now also writes its durable
    transcript (agent/planning_log.py) to this same store under a different
    namespace. A single-slot fake let that write land on top of the session
    metadata every assertion here reads, which failed this whole file for a
    reason that existed only in the fake.
    """

    def __init__(self, value: dict | None = None):
        # Keyed by the namespace root ("planning", "planning_log"), which is
        # all these tests need to keep apart.
        self._slots: dict[str, dict] = {"planning": dict(value)} if value is not None else {}
        self.puts: list[dict] = []

    @property
    def value(self) -> dict | None:
        """The session metadata slot -- what this file asserts about."""
        return self._slots.get("planning")

    async def aget(self, ns, _key):
        slot = self._slots.get(ns[0])
        return _Item(dict(slot)) if slot is not None else None

    async def aput(self, ns, _key, value):
        self._slots[ns[0]] = dict(value)
        if ns[0] == "planning":
            self.puts.append(dict(value))

    async def adelete(self, ns, _key):
        self._slots.pop(ns[0], None)


class _Tracker:
    def __init__(self, total_cost: float):
        self.total_cost = total_cost


@pytest.fixture(autouse=True)
def _no_classifier_call(monkeypatch):
    """A turn that ends with a plan and no category classifies the session
    once -- a real model call, through the real router, in tests that are
    about teardown and difficulty. Stubbed here rather than in each test so
    a new test in this file cannot reintroduce it."""
    async def _classify(_text, _config):
        return TaskClassification(category="other", needs_tests=False)

    monkeypatch.setattr(server, "classify_task", _classify)


@pytest.fixture
def store(monkeypatch):
    s = _FakeStore({"plan_markdown": "# Plan v1", "cost_usd": 0.25, "title": "t"})
    monkeypatch.setattr(server.app.state, "store", s, raising=False)
    # build_planning_agent's arguments are evaluated before the stub runs, so
    # app.state.checkpointer has to exist even though nothing reads it here.
    monkeypatch.setattr(server.app.state, "checkpointer", object(), raising=False)
    return s


# ---------------------------------------------------------------------------
# _bank_planning_turn
# ---------------------------------------------------------------------------


async def test_banking_a_draft_saved_mid_turn_stores_it(store):
    await server._bank_planning_turn("s1", "demo", "# Plan v2", 0.40)
    assert store.value["plan_markdown"] == "# Plan v2"
    assert store.value["cost_usd"] == 0.40


async def test_banking_none_preserves_the_existing_plan(store):
    """PRESERVE, never clobber -- the same rule the success path follows."""
    await server._bank_planning_turn("s1", "demo", None, 0.40)
    assert store.value["plan_markdown"] == "# Plan v1"
    assert store.value["cost_usd"] == 0.40


async def test_banking_an_empty_plan_is_a_deliberate_replacement(store):
    await server._bank_planning_turn("s1", "demo", "", 0.40)
    assert store.value["plan_markdown"] == ""


async def test_banking_leaves_unrelated_metadata_alone(store):
    await server._bank_planning_turn("s1", "demo", "# Plan v2", 0.40)
    assert store.value["title"] == "t"
    assert store.value["updated_at"] > 0


async def test_banking_a_session_that_no_longer_exists_is_a_no_op(monkeypatch):
    empty = _FakeStore(None)
    monkeypatch.setattr(server.app.state, "store", empty, raising=False)
    await server._bank_planning_turn("gone", "demo", "# Plan", 1.0)
    assert empty.puts == []


async def test_a_failing_store_does_not_raise_over_the_original_failure(monkeypatch):
    class _Broken(_FakeStore):
        async def aput(self, *_a, **_kw):
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(server.app.state, "store", _Broken({"plan_markdown": None}), raising=False)
    await server._bank_planning_turn("s1", "demo", "# Plan", 1.0)  # must not raise


# ---------------------------------------------------------------------------
# the turn handler's two abnormal exits
# ---------------------------------------------------------------------------


def _stub_turn(monkeypatch, store, *, saves: str | None, then):
    """Wires _run_planning_turn_bg up to a fake agent whose turn saves `saves`
    into plan_ref and then raises `then`."""
    published: list[dict] = []
    plan_ref: dict = {"markdown": None}

    async def _difficulty(_text, _config):
        return "easy"

    async def _build(*_a, **_kw):
        return object(), plan_ref, _Tracker(0.75)

    async def _run(_agent, ref, _thread, _text, _publish, tracker=None):
        if saves is not None:
            ref["markdown"] = saves
        raise then

    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "build_planning_agent", _build)
    monkeypatch.setattr(server, "run_planning_turn", _run)
    monkeypatch.setattr(server, "_publish_planning", lambda _sid, e: published.append(e))
    return published


async def test_a_crashed_turn_keeps_the_plan_it_saved_before_crashing(monkeypatch, store):
    published = _stub_turn(monkeypatch, store, saves="# Plan v2", then=RuntimeError("boom"))
    await server._run_planning_turn_bg("s1", "demo", "hello")
    assert store.value["plan_markdown"] == "# Plan v2"
    assert [e["type"] for e in published] == ["error", "closed"]


async def test_a_crashed_turn_banks_the_spend_it_incurred(monkeypatch, store):
    _stub_turn(monkeypatch, store, saves=None, then=RuntimeError("boom"))
    await server._run_planning_turn_bg("s1", "demo", "hello")
    assert store.value["cost_usd"] == 0.75


async def test_a_crashed_turn_that_saved_nothing_keeps_the_earlier_plan(monkeypatch, store):
    _stub_turn(monkeypatch, store, saves=None, then=RuntimeError("boom"))
    await server._run_planning_turn_bg("s1", "demo", "hello")
    assert store.value["plan_markdown"] == "# Plan v1"


async def test_a_stopped_turn_keeps_the_plan_it_saved_before_stopping(monkeypatch, store):
    published = _stub_turn(monkeypatch, store, saves="# Plan v2", then=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await server._run_planning_turn_bg("s1", "demo", "hello")
    assert store.value["plan_markdown"] == "# Plan v2"
    assert store.value["cost_usd"] == 0.75
    assert [e["type"] for e in published] == ["stopped", "closed"]


async def test_a_failed_first_turn_still_titles_the_session(monkeypatch, store):
    """Titles were set only on the success path, so a session whose first turn
    failed sat in the sidebar as a permanent None -- both timed-out sessions
    on 2026-08-27 did. The bank helper now seeds it from the turn's text."""
    store.value.pop("title", None)
    _stub_turn(monkeypatch, store, saves=None, then=RuntimeError("boom"))
    await server._run_planning_turn_bg("s1", "demo", "debug why the screener disagrees with the strategy")
    assert store.value["title"] == "debug why the screener disagrees with the strategy"[:60]


async def test_a_failed_turn_never_overwrites_an_existing_title(monkeypatch, store):
    _stub_turn(monkeypatch, store, saves=None, then=RuntimeError("boom"))
    await server._run_planning_turn_bg("s1", "demo", "some new message")
    assert store.value["title"] == "t"


async def test_a_cancel_before_the_agent_exists_still_tears_down_cleanly(monkeypatch, store):
    async def _difficulty(_text, _config):
        raise asyncio.CancelledError()

    published: list[dict] = []
    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "_publish_planning", lambda _sid, e: published.append(e))
    with pytest.raises(asyncio.CancelledError):
        await server._run_planning_turn_bg("s1", "demo", "hello")
    # tracker/plan_ref never got bound -- the handler must not NameError on them
    assert store.value["plan_markdown"] == "# Plan v1"
    assert [e["type"] for e in published] == ["stopped", "closed"]


# ---------------------------------------------------------------------------
# shutdown drain -- banking must beat the pool teardown
# ---------------------------------------------------------------------------


async def test_shutdown_drain_waits_for_cancel_teardown_to_finish():
    """A pm2 restart used to cancel planning turns AFTER the store pools
    closed, so the cancel-path banking write itself died ("failed to persist
    planning progress", 2026-08-27) and the turn's real spend vanished from
    the ledger. The lifespan drain cancels each turn while the pools are
    still open and WAITS for its teardown."""
    banked = []

    async def fake_turn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)   # the banking write, mid-teardown
            banked.append("cost")
            raise

    t = asyncio.create_task(fake_turn())
    await asyncio.sleep(0)  # let it start
    server._running_planning_turns["s-drain"] = t
    try:
        await server._drain_planning_turns(timeout=5.0)
    finally:
        server._running_planning_turns.pop("s-drain", None)
    assert banked == ["cost"], "drain returned before the teardown banking ran"
    assert t.cancelled() or t.done()


async def test_shutdown_drain_with_no_turns_is_a_no_op():
    await server._drain_planning_turns(timeout=0.1)


async def test_shutdown_drain_does_not_hang_on_a_slow_teardown():
    """The drain must give up at its timeout, not block shutdown forever."""
    async def slow():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # teardown far slower than the drain timeout
            raise

    t = asyncio.create_task(slow())
    await asyncio.sleep(0)
    server._running_planning_turns["s-slow"] = t
    try:
        t0 = asyncio.get_event_loop().time()
        await server._drain_planning_turns(timeout=0.2)
        assert asyncio.get_event_loop().time() - t0 < 2.0
    finally:
        server._running_planning_turns.pop("s-slow", None)
        t.cancel()  # second cancel interrupts the 30s sleep so the loop can close
        await asyncio.gather(t, return_exceptions=True)


# ---------------------------------------------------------------------------
# sticky-upward difficulty + store-visible turns (2026-08-28)
# ---------------------------------------------------------------------------


async def test_a_hard_session_never_downgrades_on_a_short_nudge(monkeypatch, store):
    """A continuation nudge classifies EASY on its own text, which flipped a
    session's model mid-plan (half qwen, half deepseek). Once HARD, stays
    HARD for the session."""
    store.value["difficulty"] = "HARD"
    seen = {}

    async def _difficulty(_text, _config):
        return "EASY"  # what a short "continue" classifies as

    async def _build(*_a, difficulty="EASY", **_kw):
        seen["difficulty"] = difficulty
        return object(), {"markdown": None}, _Tracker(0.1)

    async def _run(*_a, **_kw):
        return None

    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "build_planning_agent", _build)
    monkeypatch.setattr(server, "run_planning_turn", _run)
    monkeypatch.setattr(server, "_publish_planning", lambda *_a: None)
    await server._run_planning_turn_bg("s1", "demo", "continue")
    assert seen["difficulty"] == "HARD"


async def test_an_easy_session_still_escalates_to_hard(monkeypatch, store):
    store.value["difficulty"] = "EASY"
    seen = {}

    async def _difficulty(_text, _config):
        return "HARD"

    async def _build(*_a, difficulty="EASY", **_kw):
        seen["difficulty"] = difficulty
        return object(), {"markdown": None}, _Tracker(0.1)

    async def _run(*_a, **_kw):
        return None

    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "build_planning_agent", _build)
    monkeypatch.setattr(server, "run_planning_turn", _run)
    monkeypatch.setattr(server, "_publish_planning", lambda *_a: None)
    await server._run_planning_turn_bg("s1", "demo", "this is a complex bug")
    assert seen["difficulty"] == "HARD"
    assert store.value["difficulty"] == "HARD"  # ratchet persisted


async def test_turn_active_marks_the_store_during_and_after(monkeypatch, store):
    """In-flight planning turns are store-visible so deploy tooling can see
    them the way it sees running tasks -- a restart once landed mid-plan
    because a long model call read as idle."""
    states = []

    async def _difficulty(_text, _config):
        return "EASY"

    async def _build(*_a, **_kw):
        states.append(("during", store.value.get("turn_active"), "turn_started_at" in store.value))
        return object(), {"markdown": None}, _Tracker(0.1)

    async def _run(*_a, **_kw):
        return None

    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "build_planning_agent", _build)
    monkeypatch.setattr(server, "run_planning_turn", _run)
    monkeypatch.setattr(server, "_publish_planning", lambda *_a: None)
    await server._run_planning_turn_bg("s1", "demo", "go")
    assert states == [("during", True, True)]
    assert store.value["turn_active"] is False



async def test_a_build_now_carry_during_the_end_of_turn_classifier_is_kept(monkeypatch, store):
    """B41 (2026-10-22): the end-of-turn write built its row from a read
    taken before `await classify_task`, without the meta lock, so a Build
    Now carry landing during that call lost carried_cost_usd and
    built_task_ids."""
    from agent import tasks

    plan_ref: dict = {"markdown": None}

    async def _difficulty(_text, _config):
        return "easy"

    async def _build(*_a, **_kw):
        return object(), plan_ref, _Tracker(0.75)

    async def _run(_agent, ref, _thread, _text, _publish, tracker=None):
        ref["markdown"] = "# Plan v2"
        return "# Plan v2"

    async def _classify(_text, _config):
        await tasks.carry_planning_cost(store, "demo", "s1", "t9")    # Build Now, mid-call
        return TaskClassification(category="feature", needs_tests=False)

    monkeypatch.setattr(server, "classify_planning_difficulty", _difficulty)
    monkeypatch.setattr(server, "build_planning_agent", _build)
    monkeypatch.setattr(server, "run_planning_turn", _run)
    monkeypatch.setattr(server, "classify_task", _classify)
    monkeypatch.setattr(server, "_publish_planning", lambda *_a: None)
    await server._run_planning_turn_bg("s1", "demo", "go")
    assert store.value["built_task_ids"] == ["t9"] and store.value["carried_cost_usd"] == 0.25
    assert store.value["category"] == "feature" and store.value["plan_markdown"] == "# Plan v2"


async def test_a_carry_during_teardown_is_kept(monkeypatch, store):
    """B41: _bank_planning_turn read and wrote the row outside the lock."""
    from agent import tasks

    real_aget, real_aput = store.aget, store.aput

    async def slow_aget(ns, key):
        await asyncio.sleep(0.02)
        return await real_aget(ns, key)

    async def slow_teardown_write(ns, key, value):
        if "turn_active" in value:          # the teardown's write lands after the carry's
            await asyncio.sleep(0.05)
        await real_aput(ns, key, value)

    monkeypatch.setattr(store, "aget", slow_aget)
    monkeypatch.setattr(store, "aput", slow_teardown_write)
    await asyncio.gather(server._bank_planning_turn("s1", "demo", None, 0.40),
                         tasks.carry_planning_cost(store, "demo", "s1", "t9"))
    assert store.value["built_task_ids"] == ["t9"], "the teardown wrote the carry away"
