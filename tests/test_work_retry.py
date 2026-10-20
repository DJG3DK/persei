"""Covers the work-node retry policies.

Transient provider failures -- a timeout, a dropped connection, a rate limit,
a 5xx -- are retried INSIDE the pass: the inner agent resumes from its own
checkpoint (input None), so nothing it did is repeated or lost, and if the
provider is still failing after MODEL_RETRIES_PER_PASS the task is handed
back escalated, work saved, never crashed. 2026-09-24: one runaway generation
outlasting the call timeout ended whole tasks as "error".

A request the provider REJECTS (a malformed tool-call generation, surfacing
as openai.BadRequestError) is retried the same way, REJECTED_RETRIES_PER_PASS
times after a short pause, then handed back escalated. Any other exception
(a bug in our own code) escalates on the first occurrence. outer_graph.py's
RetryPolicy on "work" stays as the last net for anything raised outside the
model stream.

An empty reply -- no text, no tool call -- is asked to carry on, at most
EMPTY_REPLY_RETRIES times per pass; this is the last resort behind
EmptyReplyRetryMiddleware, which retries a length-capped empty reply on the
fallback seat inside the model call.

Drives a REAL compiled LangGraph graph (MemorySaver checkpointer) rather than
calling work_node() bare -- its own docstring explicitly warns
get_stream_writer() requires a runnable context and will raise RuntimeError
outside one.
"""

from functools import partial

import httpx
import openai
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from agent.nodes import work as work_module
from agent.outer_state import AgentState, initial_state


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


class _FakeRun:
    """Empty run -- no messages/todos, not interrupted. Enough for work_node
    to reach a normal successful return once astream_events stops raising."""

    def __init__(self):
        self.values = _AsyncIter([])
        self.subagents = _AsyncIter([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def interrupted(self):
        return False

    async def interrupts(self):
        return []


class _FakeAgent:
    def __init__(self, fail_times: int, exc_factory):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.calls = 0
        self.inputs = []

    async def astream_events(self, graph_input, config, version):
        self.calls += 1
        self.inputs.append(graph_input)
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return _FakeRun()

    async def aget_state(self, config):
        return type("FakeState", (), {"values": {}})()


class _FakeTracker:
    total_cost = 0.0


def _openai_connection_error():
    return openai.APIConnectionError(message="connection dropped", request=httpx.Request("POST", "http://test"))


def _openai_timeout():
    return openai.APITimeoutError(request=httpx.Request("POST", "http://test"))


def _bad_request():
    return openai.BadRequestError("malformed tool call args", response=httpx.Response(
        400, request=httpx.Request("POST", "http://test")), body=None)


def _build_mini_graph(checkpointer):
    # app_config/checkpointer/pg_store are bound exactly as outer_graph.py
    # binds them in production -- unused by the fake build_deep_agent below,
    # so None is fine for this test, but the WIRING (a partial, RetryPolicy
    # attached the same way) matches real production graph construction.
    builder = StateGraph(AgentState)
    builder.add_node(
        "work",
        partial(work_module.work_node, app_config=None, checkpointer=None, pg_store=None),
        retry_policy=RetryPolicy(max_attempts=3),
    )
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=checkpointer)


async def _run(monkeypatch, fail_times: int, exc_factory=_openai_connection_error):
    monkeypatch.setattr(work_module, "MODEL_RETRY_BACKOFF_S", (0,))
    fake_agent = _FakeAgent(fail_times=fail_times, exc_factory=exc_factory)
    monkeypatch.setattr(
        work_module, "build_deep_agent",
        lambda *a, **k: _fake_build_deep_agent_result(fake_agent),
    )
    checkpointer = MemorySaver()
    graph = _build_mini_graph(checkpointer)
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=10.0)
    config = {"configurable": {"thread_id": "t1"}}
    return await graph.ainvoke(state, config=config), fake_agent


async def _fake_build_deep_agent_result(fake_agent):
    return fake_agent, _FakeTracker(), {"signature": None}


@pytest.mark.parametrize("exc", [_openai_connection_error, _openai_timeout])
async def test_a_transient_failure_resumes_from_the_checkpoint(monkeypatch, exc):
    """Fails twice, then works: the pass carries on, and the retries resume
    the inner thread (input None) rather than sending the goal again."""
    result, fake_agent = await _run(monkeypatch, fail_times=2, exc_factory=exc)
    assert result["escalated"] is False
    assert fake_agent.calls == 3
    assert fake_agent.inputs[0] is not None and fake_agent.inputs[1:] == [None, None]


async def test_a_provider_that_keeps_failing_hands_the_task_back_never_crashes_it(monkeypatch):
    result, fake_agent = await _run(monkeypatch, fail_times=99, exc_factory=_openai_timeout)
    assert result["escalated"] is True
    assert "model provider kept failing" in result["escalation_reason"]
    assert "resume the task" in result["escalation_reason"]
    assert fake_agent.calls == work_module.MODEL_RETRIES_PER_PASS + 1, "one pass, not three graph retries of it"


async def test_a_rejected_request_is_retried_then_handed_back_never_crashed(monkeypatch):
    monkeypatch.setattr(work_module, "REJECTED_RETRY_BACKOFF_S", 0)
    result, fake_agent = await _run(monkeypatch, fail_times=2, exc_factory=_bad_request)
    assert result["escalated"] is False and fake_agent.calls == 3
    result, fake_agent = await _run(monkeypatch, fail_times=99, exc_factory=_bad_request)
    assert result["escalated"] is True and "rejected the request" in result["escalation_reason"]
    assert fake_agent.calls == work_module.REJECTED_RETRIES_PER_PASS + 1


async def test_non_api_error_escalates_immediately_without_retry(monkeypatch):
    """A bug in OUR OWN code (e.g. a ValueError) must still escalate
    immediately, exactly as before this fix -- only openai.APIError gets the
    new retry-then-escalate treatment. Confirms the fix is narrowly scoped,
    not "retry everything.\""""
    result, fake_agent = await _run(monkeypatch, fail_times=1, exc_factory=lambda: ValueError("real bug"))
    assert result["escalated"] is True
    assert "real bug" in result["escalation_reason"]
    assert fake_agent.calls == 1, "must NOT retry a non-API error"


# ── a model stuck in a loop hands the pass to the fallback seat ─────────────

async def test_a_loop_moves_the_pass_to_the_fallback_seat_on_the_same_conversation(monkeypatch):
    """2026-09-24: two benchmark tasks ended "escalated -- resume on a
    different seat" with nobody there to press resume."""
    from langchain_core.messages import HumanMessage
    from agent.middleware.repeat_guard import RepeatLoopError

    builds, agents = [], []

    def fake_build(*a, route="general", **k):
        builds.append(route)
        loop = route != "fallback"
        agent = _FakeAgent(fail_times=1 if loop else 0, exc_factory=lambda: RepeatLoopError(
            "stuck in a tool loop: `bash` with identical arguments requested 12 times in a row. The model is no longer steering"))
        agents.append(agent)
        return _fake_build_deep_agent_result(agent)

    monkeypatch.setattr(work_module, "build_deep_agent", fake_build)
    graph = _build_mini_graph(MemorySaver())
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=10.0)
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "t1"}})
    assert result["escalated"] is False
    assert builds == ["general", "fallback"]
    handover = agents[1].inputs[0]["messages"][0]
    assert isinstance(handover, HumanMessage) and "different model taking over" in handover.content
    assert "`bash` with identical arguments" in handover.content
    assert result.get("route", "general") != "fallback", "the next pass is back on the task's own seat"


async def test_if_the_fallback_loops_too_the_task_is_handed_back(monkeypatch):
    from agent.middleware.repeat_guard import RepeatLoopError
    builds = []

    def fake_build(*a, route="general", **k):
        builds.append(route)
        return _fake_build_deep_agent_result(_FakeAgent(fail_times=99, exc_factory=lambda: RepeatLoopError("stuck in a tool loop")))

    monkeypatch.setattr(work_module, "build_deep_agent", fake_build)
    graph = _build_mini_graph(MemorySaver())
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=10.0)
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "t1"}})
    assert builds == ["general", "fallback"]
    assert result["escalated"] is True and "fallback seat got stuck as well" in result["escalation_reason"]


# ── an empty reply is asked to carry on, inside the pass ─────────────────────

class _RunWith(_FakeRun):
    def __init__(self, messages):
        super().__init__()
        self.values = _AsyncIter([{"messages": messages}])


async def test_an_empty_reply_is_asked_to_carry_on_rather_than_ending_the_pass(monkeypatch):
    """13033 returned an empty reply on 8 of its 41 turns (2026-09-24); each
    ended a pass and spent the ship gate's nudges on nothing."""
    from langchain_core.messages import AIMessage, HumanMessage

    class _Agent(_FakeAgent):
        async def astream_events(self, graph_input, config, version):
            self.calls += 1
            self.inputs.append(graph_input)
            if self.calls <= 2:
                return _RunWith([AIMessage("", id=f"e{self.calls}")])
            return _RunWith([AIMessage("Fixed the parser and verified it on the reported case.", id="done")])

    agent = _Agent(fail_times=0, exc_factory=None)
    monkeypatch.setattr(work_module, "build_deep_agent", lambda *a, **k: _fake_build_deep_agent_result(agent))
    graph = _build_mini_graph(MemorySaver())
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=10.0)
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "t1"}})
    assert agent.calls == 3 and result["escalated"] is False
    nudge = agent.inputs[1]["messages"][0]
    assert isinstance(nudge, HumanMessage) and nudge.content.startswith("[Persei harness] Your last reply was empty")
