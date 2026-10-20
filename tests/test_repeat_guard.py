"""RepeatCallGuardMiddleware: the same tool call with the same result is
answered from cache on the third try and refused from the fourth
(2026-09-09: fourteen identical bash calls in a row on a Kimi build)."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

import agent.deep_agent as da
import agent.planning_chat as pc
from agent.middleware.repeat_guard import RepeatCallGuardMiddleware


def _req(name, args, i):
    return SimpleNamespace(tool_call={"name": name, "args": args, "id": f"{name}:{i}"})


class _Handler:
    def __init__(self, results=None):
        self.calls = 0
        self.results = results  # callable(i) -> content, or None for constant

    async def __call__(self, request):
        self.calls += 1
        content = self.results(self.calls) if self.results else "same output"
        return ToolMessage(content=content, tool_call_id=request.tool_call["id"])


async def test_third_identical_call_is_served_from_cache_and_fourth_refused():
    mw = RepeatCallGuardMiddleware()
    h = _Handler()
    r1 = await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, 1), h)
    r2 = await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, 2), h)
    assert h.calls == 2 and r1.content == r2.content == "same output"
    r3 = await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, 3), h)
    assert h.calls == 2, "the third identical call must not execute"
    assert r3.status == "error" and r3.content.startswith("[Persei harness] REPEATED CALL") and "same output" in r3.content
    r4 = await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, 4), h)
    assert h.calls == 2 and r4.content.startswith("[Persei harness] ERROR:") and "loop" in r4.content


async def test_a_call_whose_result_changes_is_never_blocked():
    mw = RepeatCallGuardMiddleware()
    h = _Handler(results=lambda i: f"attempt {i}")
    for i in range(1, 7):
        r = await mw.awrap_tool_call(_req("bash", {"command": "npm test"}, i), h)
        assert r.content == f"attempt {i}"
    assert h.calls == 6, "a flaky or polling command keeps running"


async def test_a_different_call_in_between_resets_the_run():
    mw = RepeatCallGuardMiddleware()
    h = _Handler()
    for i in range(2):
        await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, i), h)
    await mw.awrap_tool_call(_req("bash", {"command": "grep y"}, 10), h)
    r = await mw.awrap_tool_call(_req("bash", {"command": "grep x"}, 11), h)
    assert r.content == "same output" and h.calls == 4


@pytest.mark.parametrize("name", ["write_todos", "save_plan", "ask_user"])
async def test_non_idempotent_tools_are_exempt(name):
    mw = RepeatCallGuardMiddleware()
    h = _Handler()
    for i in range(6):
        await mw.awrap_tool_call(_req(name, {"x": 1}, i), h)
    assert h.calls == 6


def test_sync_path_matches_async_semantics():
    mw = RepeatCallGuardMiddleware()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ToolMessage(content="same", tool_call_id=request.tool_call["id"])

    for i in range(3):
        r = mw.wrap_tool_call(_req("read", {"path": "a.js"}, i), handler)
    assert calls["n"] == 2 and r.content.startswith("[Persei harness] REPEATED CALL")


def test_guard_is_attached_to_every_build_seat_and_to_planning():
    import inspect
    src = inspect.getsource(da)
    assert src.count("RepeatCallGuardMiddleware(contain=True)") == 4, "investigator + test-writer + general-purpose + verifier"
    assert src.count("RepeatCallGuardMiddleware()") == 1, "the coordinator: its loop moves the pass to the fallback seat"
    assert "RepeatCallGuardMiddleware()" in inspect.getsource(pc)


async def test_a_long_run_of_refused_calls_ends_the_pass():
    """40 refusals in a row on 2026-09-09: the guard was cheap but the model
    kept paying for each one. Past BREAK_AT refusals it raises instead."""
    import pytest
    from agent.middleware.repeat_guard import BREAK_AT, REFUSED_AT, RepeatLoopError
    mw = RepeatCallGuardMiddleware()
    h = _Handler()
    with pytest.raises(RepeatLoopError, match="stuck in a tool loop"):
        for i in range(REFUSED_AT + BREAK_AT + 1):
            await mw.awrap_tool_call(_req("bash", {"command": "pnpm config get --location"}, i), h)
    assert h.calls == 2, "nothing executed after the second identical call"


# ── a stuck subagent is ended with a report, not the whole task ─────────────

async def test_a_contained_subagent_that_loops_is_ended_with_a_report():
    """A verifier looping on `git stash` used to raise out of the whole pass
    and move the entire task to the fallback seat."""
    from agent.middleware.repeat_guard import BREAK_AT, REFUSED_AT
    g = RepeatCallGuardMiddleware(contain=True)
    h = _Handler()
    for i in range(REFUSED_AT + BREAK_AT):
        await g.awrap_tool_call(_req("bash", {"command": "git stash"}, i), h)
    assert g._stuck and "stuck in a tool loop" in g._stuck

    async def model_must_not_run(request):
        raise AssertionError("the model is not called once the subagent is stopped")
    resp = await g.awrap_model_call(object(), model_must_not_run)
    msg = resp.result[0]
    assert msg.content.startswith("[Persei harness] This subagent was stopped") and not msg.tool_calls
    with pytest.raises(Exception, match="stuck in a tool loop"):
        coord = RepeatCallGuardMiddleware()
        for i in range(REFUSED_AT + BREAK_AT):
            await coord.awrap_tool_call(_req("bash", {"command": "git stash"}, i), h)


def test_different_calls_with_the_same_long_output_are_a_loop_too():
    """probe_c9.py, probe_c10.py, ... same content, same output, 92 times."""
    from agent.middleware.repeat_guard import RepeatCallGuardMiddleware, SAME_OUTPUT_AT
    from langchain_core.messages import ToolMessage
    g = RepeatCallGuardMiddleware(contain=True)
    out = "x" * 300

    def run(i):
        return g.wrap_tool_call(_req("bash", {"command": f"python probe_c{i}.py"}, i),
                                lambda r: ToolMessage(content=out, tool_call_id=r.tool_call["id"]))
    results = [run(i) for i in range(SAME_OUTPUT_AT)]
    assert "[Persei harness] The last" not in results[SAME_OUTPUT_AT - 2].content
    assert f"The last {SAME_OUTPUT_AT} calls, each with different arguments" in results[-1].content
    short = RepeatCallGuardMiddleware()
    for i in range(20):
        r = short.wrap_tool_call(_req("edit", {"path": f"f{i}.py"}, i), lambda r: ToolMessage(content="OK", tool_call_id=r.tool_call["id"]))
    assert r.content == "OK", "a short identical result (an edit's OK) is not a loop"


# ── 2026-09-25: 352 identical bash calls whose output carried an address ──────

async def test_output_noise_does_not_hide_a_repeat():
    """The result held `<... object at 0x...>` and a duration, so no two
    results hashed equal and the guard never fired."""
    mw = RepeatCallGuardMiddleware()
    h = _Handler(results=lambda i: f"<Foo object at 0x{0x79d3b6733250 + i:x}> failed in 0.{i}s "
                                   f"2026-09-25T10:00:{i:02d} /tmp/pytest-{i}/x pid={1000 + i}")
    for i in range(1, 3):
        await mw.awrap_tool_call(_req("bash", {"command": "pytest -x"}, i), h)
    r3 = await mw.awrap_tool_call(_req("bash", {"command": "pytest -x"}, 3), h)
    assert h.calls == 2 and r3.content.startswith("[Persei harness] REPEATED CALL")
    assert "3rd identical" in r3.content, "and the ordinal is spelled right"


async def test_a_genuinely_changing_result_is_still_not_noise():
    mw = RepeatCallGuardMiddleware()
    h = _Handler(results=lambda i: f"{i} tests failed")
    for i in range(1, 5):
        await mw.awrap_tool_call(_req("bash", {"command": "pytest"}, i), h)
    assert h.calls == 4


async def test_past_hard_repeat_at_the_result_no_longer_matters():
    """A model sending the same command eight times is looping whatever the
    output says."""
    from agent.middleware.repeat_guard import BREAK_AT, HARD_REPEAT_AT, REFUSED_AT, RepeatLoopError
    mw = RepeatCallGuardMiddleware()
    h = _Handler(results=lambda i: f"run {i}: {i * 7919 % 1000} widgets")  # never equal, never noise
    results = []
    with pytest.raises(RepeatLoopError, match="stuck in a tool loop"):
        for i in range(1, REFUSED_AT + BREAK_AT + 1):
            results.append(await mw.awrap_tool_call(_req("bash", {"command": "python probe.py"}, i), h))
    assert h.calls == HARD_REPEAT_AT - 1, "executed up to the hard cap, refused after"
    assert results[HARD_REPEAT_AT - 1].status == "error" and "differs only in noise" in results[HARD_REPEAT_AT - 1].content
    assert results[HARD_REPEAT_AT - 2].content.startswith("run ")


async def test_state_is_reset_per_agent_invocation():
    """One middleware instance serves every task() call of a subagent: a
    verifier's round 2 inherited round 1's counts."""
    mw = RepeatCallGuardMiddleware(contain=True)
    h = _Handler()
    for i in range(3):
        await mw.awrap_tool_call(_req("bash", {"command": "git diff"}, i), h)
    assert mw._runs and mw._last_key
    mw._stuck = "pretend"
    await mw.abefore_agent({}, None)
    assert not mw._runs and mw._last_key is None and mw._stuck is None and mw._same["n"] == 0
    r = await mw.awrap_tool_call(_req("bash", {"command": "git diff"}, 9), h)
    assert r.content == "same output" and h.calls == 3, "round 2 starts from a clean slate"
