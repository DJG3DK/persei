"""Reflection after a failed attempt (agent/reflection.py, verify_and_ship_node)."""

import inspect

import pytest
from langchain_core.messages import AIMessage

from agent import reflection
from agent import runtime_settings as rs
from agent.nodes import verify_and_ship as vs
from agent.outer_state import initial_state

GOOD = ("FAILED: test_studio_agent.js:80 expected 'validated: ok'\n"
        "BECAUSE: validate_code dry-runs on market data the review harness does not have\n"
        "NEXT: give the test a candle fixture instead of real data")


class _Model:
    def __init__(self, answer=GOOD, boom=None):
        self.answer, self.boom, self.seen = answer, boom, None

    async def ainvoke(self, messages):
        self.seen = messages[0].content
        if self.boom:
            raise self.boom
        return AIMessage(content=self.answer)


def _state(**over):
    s = initial_state(task_id="t1", goal="fix the alert", repo="test-repo", budget_usd=5.0)
    s.update(over)
    return s


# --- the note ---------------------------------------------------------------

def test_the_note_is_three_labelled_lines_in_order():
    assert reflection.parse(GOOD) == GOOD
    shuffled = "NEXT: c\nsome chatter\nFAILED: a\nBECAUSE: b"
    assert reflection.parse(shuffled) == "FAILED: a\nBECAUSE: b\nNEXT: c"
    assert reflection.parse("FAILED: a\nNEXT: c") is None, "a missing line is not a reflection"
    assert reflection.parse("") is None
    assert len(reflection.parse("FAILED: " + "x" * 5000 + "\nBECAUSE: b\nNEXT: c")) <= reflection.MAX_NOTE_CHARS


def test_the_prompt_carries_the_failure_the_change_and_what_already_failed():
    p = reflection.prompt("fix the alert", "review", "x" * 9000 + "REAL ERROR", "src/a.js | 4 ++", ["old one"])
    assert "independent review gate asked for fixes" in p
    assert p.rstrip().endswith("1. old one") and "do not propose the same thing again" in p
    assert "REAL ERROR" in p, "the end of a long failure is what is kept"
    assert "src/a.js | 4 ++" in p
    assert "earlier reflections" not in reflection.prompt("g", "checks", "f", "", [])


def test_the_note_leads_the_feedback_and_says_where_to_start():
    block = reflection.as_feedback(GOOD)
    assert block.startswith("=== Reflection on the last attempt") and GOOD in block and "Start from NEXT" in block


@pytest.mark.asyncio
async def test_a_failed_or_misshapen_call_is_dropped_never_raised():
    for model in (_Model(boom=RuntimeError("router down")), _Model(answer="I think the test is flaky.")):
        assert await reflection.reflect(None, task_id="t1", goal="g", kind="checks", feedback="f",
                                        touched="", earlier=[], model=model) is None
    m = _Model()
    assert await reflection.reflect(None, task_id="t1", goal="g", kind="checks", feedback="boom",
                                    touched="", earlier=[], model=m) == GOOD
    assert "boom" in m.seen


# --- in the loop --------------------------------------------------------------

@pytest.fixture
def _loop(monkeypatch):
    calls = []

    async def reflect(config, **kw):
        calls.append(kw)
        return GOOD

    async def ensure(repo, task_id):
        return {"path": "/tmp/ws"}

    async def git(cmd, root, timeout=15):
        return {"ok": True, "output": " src/studio/agent.js | 12 +++++---"}

    import agent.tools.git as g
    import agent.workspaces as ws
    monkeypatch.setattr(reflection, "reflect", reflect)
    monkeypatch.setattr(ws, "ensure", ensure)
    monkeypatch.setattr(g, "_git", git)
    monkeypatch.setattr(rs, "as_int", lambda name: 1)
    return calls


def _returns(monkeypatch, result):
    async def fake(state, config, store=None):
        return dict(result)
    monkeypatch.setattr(vs, "_verify_and_ship", fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["checks", "review", "ci"])
async def test_a_failed_attempt_is_reflected_on_before_the_next(monkeypatch, _loop, kind):
    _returns(monkeypatch, {"pending_feedback": "the checks failed: X", "reflect_on": kind,
                           "execution_log": [{"summary": "checks failed"}]})
    out = await vs.verify_and_ship_node(_state(reflections=["a", "b", "c"]), None, None)
    assert "reflect_on" not in out, "never reaches the task's state"
    assert out["pending_feedback"].startswith("=== Reflection on the last attempt")
    assert out["pending_feedback"].endswith("the checks failed: X")
    assert out["reflections"] == ["b", "c", GOOD], "the newest three"
    assert out["execution_log"][-1]["summary"] == "reflected on the failure before the next attempt"
    (call,) = _loop
    assert call["kind"] == kind and call["earlier"] == ["a", "b", "c"]
    assert "src/studio/agent.js" in call["touched"]


@pytest.mark.asyncio
async def test_a_nudge_or_a_conflict_is_not_reflected_on(monkeypatch, _loop):
    _returns(monkeypatch, {"pending_feedback": "Live moved on while you were working"})
    out = await vs.verify_and_ship_node(_state(), None, None)
    assert out["pending_feedback"] == "Live moved on while you were working" and _loop == []


@pytest.mark.asyncio
async def test_switched_off_it_is_the_old_loop(monkeypatch, _loop):
    monkeypatch.setattr(rs, "as_int", lambda name: 0)
    _returns(monkeypatch, {"pending_feedback": "f", "reflect_on": "checks"})
    out = await vs.verify_and_ship_node(_state(), None, None)
    assert out == {"pending_feedback": "f"} and _loop == []


@pytest.mark.asyncio
async def test_no_note_leaves_the_feedback_as_it_was(monkeypatch, _loop):
    async def nothing(config, **kw):
        return None
    monkeypatch.setattr(reflection, "reflect", nothing)
    _returns(monkeypatch, {"pending_feedback": "f", "reflect_on": "review"})
    out = await vs.verify_and_ship_node(_state(), None, None)
    assert out == {"pending_feedback": "f"}


def test_the_three_failure_paths_ask_for_a_reflection():
    src = inspect.getsource(vs)
    for kind in ("checks", "review", "ci"):
        assert f'"reflect_on": "{kind}"' in src, kind
    assert src.count('"reflect_on": "') == 3, "conflicts and nudges do not"


@pytest.mark.asyncio
async def test_the_episode_keeps_the_reflections(monkeypatch):
    seen = {}

    async def write(store, config, repo, record):
        seen.update(record)
    monkeypatch.setattr(vs, "write_episode", write)
    await vs._write_episode(None, _state(reflections=[GOOD], cost_so_far=1.0), {"review_gate_result": {"verdict": "READY"}}, None)
    assert seen["reflections"] == [GOOD] and seen["outcome"] == "shipped"
    seen.clear()
    await vs._write_episode(None, _state(cost_so_far=1.0), {"review_gate_result": {"verdict": "READY"}}, None)
    assert "reflections" not in seen


def test_consolidation_is_told_which_reflections_are_proven():
    from agent import consolidation
    p = consolidation.CONSOLIDATION_SYSTEM_PROMPT
    assert "only a task that went on" in p and "unproven guess" in p
    assert 'reflections={e[\'reflections\']!r}' in inspect.getsource(consolidation.run_consolidation)


def test_the_setting_exists_and_defaults_on():
    spec = rs.KNOBS["reflect_on_failure"]
    assert spec["default"] == 1.0 and (spec["min"], spec["max"]) == (0.0, 1.0)


def test_harness_facts_outrank_a_reviewers_opinion():
    """2026-10-17, first live run: the reflection repeated the reviewer's
    "your change is the prime suspect" over the harness's own mark that the
    test fails the same way on the base commit, and proposed reverting."""
    p = reflection.prompt("g", "review", "f", "", [])
    assert "a reviewer's reading of them is an opinion" in p
    assert "pre-existing is not caused by this change" in p and "never to undo the change" in p
