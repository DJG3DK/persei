"""EmptyReplyRetryMiddleware: a reply that is empty because the model spent
its whole output budget thinking is retried once on the fallback seat
(2026-09-25: fifty such replies in one SWE-bench run, finish_reason
"length", 32768 reasoning tokens each, $0.04 and 75-240 s apiece)."""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from agent.middleware.empty_reply import (
    MAX_RETRIES_PER_INVOCATION, EmptyReplyRetryMiddleware, _ai_message, is_empty_length_capped,
)
from langchain.agents.middleware.types import ModelResponse

CODER = SimpleNamespace(name="coder")
FALLBACK = SimpleNamespace(name="fallback")


def _req(model=CODER, messages=None):
    req = SimpleNamespace(model=model, messages=list(messages or [HumanMessage(content="fix the parser")]))

    def override(**kw):
        return _req(model=kw.get("model", req.model), messages=kw.get("messages", req.messages))
    req.override = override
    return req


def _capped(tokens=32768):
    return AIMessage(
        content="",
        response_metadata={"finish_reason": "length", "token_usage": {
            "completion_tokens": tokens, "completion_tokens_details": {"reasoning_tokens": tokens}}},
        usage_metadata={"input_tokens": 1000, "output_tokens": tokens, "total_tokens": 1000 + tokens},
    )


class _Handler:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return ModelResponse(result=[self.replies.pop(0)])


async def test_an_empty_length_capped_reply_is_retried_once_on_the_fallback_seat():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    real = AIMessage(content="", tool_calls=[{"name": "bash", "args": {"command": "pytest"}, "id": "c1"}])
    h = _Handler([_capped(), real])
    resp = await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 2 and resp.result[0] is real
    retry = h.requests[1]
    assert retry.model is FALLBACK
    assert isinstance(retry.messages[-1], HumanMessage)
    assert retry.messages[-1].content.startswith("[Persei harness] Your previous attempt ran out of output tokens")
    assert h.requests[0].messages[-1].content == "fix the parser", "the note is only in the retry request"
    assert mw.retries == 1


async def test_a_normal_reply_passes_through_with_one_call():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    h = _Handler([AIMessage(content="Done: fixed and verified.")])
    resp = await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 1 and resp.result[0].content == "Done: fixed and verified."


async def test_a_reply_with_tool_calls_but_no_text_is_not_retried():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    msg = AIMessage(content="", tool_calls=[{"name": "read", "args": {"path": "a.py"}, "id": "c1"}],
                    response_metadata={"finish_reason": "length"})
    h = _Handler([msg])
    resp = await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 1 and resp.result[0] is msg


async def test_an_empty_reply_that_was_not_capped_is_left_to_the_work_node():
    """A short, deliberate empty turn is the work node's nudge to handle."""
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    msg = AIMessage(content="", response_metadata={"finish_reason": "stop", "token_usage": {"completion_tokens": 3}})
    h = _Handler([msg])
    await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 1 and mw.retries == 0


def test_a_runaway_completion_without_finish_reason_counts_as_capped():
    msg = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 20000, "total_tokens": 20001})
    assert is_empty_length_capped(msg)
    assert not is_empty_length_capped(AIMessage(content=[{"type": "text", "text": "ok"}],
                                                response_metadata={"finish_reason": "length"}))
    assert is_empty_length_capped(AIMessage(content=[{"type": "reasoning", "reasoning": "..."}],
                                            response_metadata={"finish_reason": "length"}))


async def test_a_second_empty_reply_from_the_fallback_goes_through_unchanged():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    second = _capped(20000)
    h = _Handler([_capped(), second])
    resp = await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 2 and resp.result[0] is second, "work.py's nudge remains the last resort"


async def test_the_retry_counter_is_capped_per_invocation_and_reset_in_before_agent():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    for _ in range(MAX_RETRIES_PER_INVOCATION):
        h = _Handler([_capped(), AIMessage(content="ok")])
        await mw.awrap_model_call(_req(), h)
        assert len(h.requests) == 2
    assert mw.retries == MAX_RETRIES_PER_INVOCATION
    h = _Handler([_capped()])
    resp = await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 1 and resp.result[0].content == "", "past the cap the reply is handed through"
    mw.before_agent({}, None)
    assert mw.retries == 0
    h = _Handler([_capped(), AIMessage(content="ok")])
    await mw.awrap_model_call(_req(), h)
    assert len(h.requests) == 2, "a fresh invocation retries again"


def test_sync_path_matches_async_semantics():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    seen = []

    def handler(request):
        seen.append(request)
        return ModelResponse(result=[_capped() if len(seen) == 1 else AIMessage(content="ok")])
    resp = mw.wrap_model_call(_req(), handler)
    assert len(seen) == 2 and seen[1].model is FALLBACK and resp.result[0].content == "ok"


def test_the_coordinator_s_retry_sits_inside_the_plan_code_model_pick():
    """PlanCodeModelMiddleware sets the model on every call, so a retry
    outside it lost its fallback model: 17 of 19 retries went to the planner
    at full effort in the first hour of a run (2026-09-25). Inside it, the
    retry's override is the last word."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("agent/deep_agent.py").read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.keyword) and node.arg == "middleware" and isinstance(node.value, ast.List)):
            continue
        names = [getattr(getattr(e, "func", None), "id", None) for e in node.value.elts]
        if "PlanCodeModelMiddleware" in names:
            assert "EmptyReplyRetryMiddleware" in names
            assert names.index("EmptyReplyRetryMiddleware") > names.index("PlanCodeModelMiddleware")
            return
    raise AssertionError("no coordinator middleware list with PlanCodeModelMiddleware found")


# ── the same seat without reasoning is tried before the fallback seat ─────────
# 2026-09-28, the first Windows install: the test writer emptied on its own
# pin and on the fallback seat too, thirty times, for one prompt.

QUIET = SimpleNamespace(name="coder-no-reasoning")


async def test_the_first_retry_is_the_same_seat_without_reasoning():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="test-writer", quiet_model=QUIET)
    h = _Handler([_capped(16384), AIMessage(content="write the parser test", id="ok")])
    out = await mw.awrap_model_call(_req(), h)
    assert _ai_message(out).content == "write the parser test"
    assert [r.model for r in h.requests] == [CODER, QUIET]
    assert mw.retries == 1


async def test_when_quiet_empties_too_the_fallback_seat_is_next_and_then_it_goes_through():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="test-writer", quiet_model=QUIET)
    h = _Handler([_capped(16384), _capped(16384), AIMessage(content="from the fallback", id="fb")])
    out = await mw.awrap_model_call(_req(), h)
    assert _ai_message(out).content == "from the fallback"
    assert [r.model for r in h.requests] == [CODER, QUIET, FALLBACK]
    assert mw.retries == 2
    h = _Handler([_capped(16384), _capped(16384), _capped(16384)])
    out = await mw.awrap_model_call(_req(), h)
    assert is_empty_length_capped(_ai_message(out)) and len(h.requests) == 3, "three tries, then through unchanged"


def test_without_a_quiet_model_the_fallback_is_the_only_retry():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    h = _Handler([_capped(), _capped()])
    import asyncio
    asyncio.run(mw.awrap_model_call(_req(), h))
    assert [r.model for r in h.requests] == [CODER, FALLBACK]


# ── a retry that errors is dropped, never surfaced ───────────────────────────
# 2026-09-28: a coder on a model whose endpoint refuses `reasoning: false`
# answered every quiet retry with a 502, and six in a row ended the task.

class _Boom(Exception):
    pass


async def test_a_failing_quiet_retry_falls_through_to_the_fallback_seat():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder", quiet_model=QUIET)
    calls = []

    async def handler(request):
        calls.append(request.model)
        if request.model is QUIET:
            raise _Boom("Error code: 502 - Reasoning is mandatory for this endpoint and cannot be disabled.")
        if request.model is FALLBACK:
            return ModelResponse(result=[AIMessage(content="from the fallback", id="fb")])
        return ModelResponse(result=[_capped(16384)])

    out = await mw.awrap_model_call(_req(), handler)
    assert _ai_message(out).content == "from the fallback" and calls == [CODER, QUIET, FALLBACK]
    assert mw.quiet_model is None, "the seat is remembered as unable to go quiet"
    calls.clear()
    await mw.awrap_model_call(_req(), handler)
    assert calls == [CODER, FALLBACK], "and is not asked again"


async def test_when_every_retry_errors_the_original_empty_reply_goes_through():
    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder", quiet_model=QUIET)

    async def handler(request):
        if request.model is CODER:
            return ModelResponse(result=[_capped(16384)])
        raise _Boom("provider down")

    out = await mw.awrap_model_call(_req(), handler)
    assert is_empty_length_capped(_ai_message(out)), "the work node's nudge takes it from here; no error escapes"
    assert mw.quiet_model is QUIET, "a plain failure does not mark the seat"


async def test_a_budget_refusal_on_the_retry_propagates():
    """2026-09-29 audit, A3: the retry's exception handler swallowed the
    budget guard's refusal, so a task past its ceiling kept going."""
    from agent.middleware.budget_guard import BudgetExceededError
    import pytest

    class _Guarded(_Handler):
        async def __call__(self, request):
            if self.requests:
                raise BudgetExceededError(5.0, 4.0)
            return await super().__call__(request)

    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    with pytest.raises(BudgetExceededError):
        await mw.awrap_model_call(_req(), _Guarded([_capped()]))


def test_the_sync_retry_lets_a_budget_refusal_through_too():
    from agent.middleware.budget_guard import BudgetExceededError
    import pytest
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) > 1:
            raise BudgetExceededError(5.0, 4.0)
        return ModelResponse(result=[_capped()])

    mw = EmptyReplyRetryMiddleware(FALLBACK, seat="coder")
    with pytest.raises(BudgetExceededError):
        mw.wrap_model_call(_req(), handler)
