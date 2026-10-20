"""Everything Persei itself says to the model, or refuses it, is marked as
the harness; the operator's own words are marked as the operator's
(agent/harness_voice.py). A trajectory is read by the operator, by
leaderboard reviewers and by the model; none of them should have to guess."""
from __future__ import annotations

import inspect

import pytest

from agent.harness_voice import HARNESS, OPERATOR, harness, marked, operator


def test_marking_is_idempotent_and_keeps_the_operator_s_own_label():
    assert harness("x") == f"{HARNESS} x" and harness(harness("x")) == f"{HARNESS} x"
    assert operator(" fix the header ") == f"{OPERATOR} fix the header"
    assert marked("The review service found real issues") == f"{HARNESS} The review service found real issues"
    assert marked(f"{OPERATOR} ship it") == f"{OPERATOR} ship it"
    assert marked("[operator message] hi") == "[operator message] hi"


def test_every_refusal_and_note_the_harness_writes_carries_its_name():
    from agent.middleware.step_back import render_checkpoint
    from agent.middleware.todo_nag import render_nag
    from agent.tools import bash_advice
    from agent.tools.benchmark_guard import refusal
    from agent.tools.files import PathEscapeError, _resolve
    from agent.evals.swebench import goal_for
    assert render_checkpoint(1.0, 3.0, 50).startswith(HARNESS)
    assert render_nag([{"content": "a", "status": "pending"}], 20).startswith(HARNESS)
    assert refusal("git fsck").startswith(HARNESS)
    assert goal_for({"repo": "a/b", "problem_statement": "p"}).startswith(HARNESS)
    for note in bash_advice.NOTE_KINDS:
        assert note.startswith(HARNESS), note
    with pytest.raises(PathEscapeError) as e:
        _resolve("/tmp", "../etc/passwd")
    assert str(e.value).startswith(HARNESS)


def test_the_guards_and_handovers_are_written_in_the_harness_s_name():
    from agent import deep_agent
    from agent.middleware import hidden_tools, repeat_guard
    from agent.nodes import work
    from agent.tools import agent_tools
    assert inspect.getsource(repeat_guard).count("{HARNESS} ") == 4, "cached, refused, same-output note, stopped"
    assert "{HARNESS} ERROR: the `{name}` tool is not available" in inspect.getsource(hidden_tools)
    assert 'HARNESS + " ERROR: REFUSED without retrying' in inspect.getsource(agent_tools)
    assert '{HARNESS} (no operator response captured' in inspect.getsource(deep_agent)
    src = inspect.getsource(work)
    assert 'HARNESS + " The model working on this task got stuck' in src
    assert "HumanMessage(content=marked(pending_feedback))" in src
    assert 'workspace_note = HARNESS + " " + (' in src
    assert "[harness]" not in src


def test_a_resume_note_says_which_words_are_the_operator_s():
    from agent import lifecycle
    src = inspect.getsource(lifecycle)
    assert 'note = HARNESS + " " + (' in src and "operator(message)" in src
    assert "Operator note:" not in src
