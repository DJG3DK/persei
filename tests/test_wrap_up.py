"""WrapUpMiddleware: a bounded subagent is warned before its tool-call cap
and made to report at it (2026-09-25: 13 of 48 verifier runs in a benchmark
ended with "Tool call limit reached" and no report; a seat making two calls
per turn stepped over the equality-keyed warning)."""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middleware.wrap_up import WrapUpMiddleware


def _ok(r):
    return ToolMessage(content="ok", tool_call_id=r.tool_call["id"])


def _run(mw, i):
    return mw.wrap_tool_call(SimpleNamespace(tool_call={"id": str(i)}), _ok).content


def test_warnings_fire_on_crossing_not_equality():
    """Two parallel calls per turn: 25 -> 27 skipped the 26 warning."""
    mw = WrapUpMiddleware(limit=30)
    outs = [_run(mw, i) for i in range(1, 26)]
    assert [i + 1 for i, c in enumerate(outs) if "[Persei harness]" in c] == [20]
    # a turn with two calls lands on 27, past the 26 threshold
    mw.calls = 26
    out = _run(mw, 27)
    assert "27 of your 30 tool calls used" in out and "Send your report NOW" in out
    assert "[Persei harness]" not in _run(mw, 28), "each threshold fires once"


def test_counts_reset_per_invocation():
    """A verifier's round 2 got "26 of 30 used" after 5 calls."""
    mw = WrapUpMiddleware(limit=30)
    for i in range(26):
        _run(mw, i)
    assert mw.calls == 26 and mw._fired
    mw.before_agent({}, None)
    assert mw.calls == 0 and not mw._fired
    outs = [_run(mw, i) for i in range(20)]
    assert "[Persei harness]" not in "".join(outs[:19]) and "20 of your 30" in outs[19]


class _Req:
    def __init__(self, messages, tools):
        self.messages = messages
        self.tools = tools
        self.overrides = None

    def override(self, **kw):
        self.overrides = kw
        return SimpleNamespace(messages=kw.get("messages", self.messages), tools=kw.get("tools", self.tools))


async def test_the_last_model_call_carries_no_tools_and_the_note():
    mw = WrapUpMiddleware(limit=30)
    tools = [object(), object()]
    seen = []

    async def model(request):
        seen.append(request)
        return AIMessage(content="VERDICT: FIX HOLDS")

    mw.calls = 28
    await mw.awrap_model_call(_Req([HumanMessage("go"), AIMessage("x")], tools), model)
    assert seen[-1].tools is tools, "with calls left, the request passes through untouched"
    mw.calls = 29
    await mw.awrap_model_call(_Req([HumanMessage("go"), AIMessage("x")], tools), model)
    final = seen[-1]
    assert final.tools == []
    assert len(final.messages) == 3 and isinstance(final.messages[-1], HumanMessage)
    assert final.messages[-1].content.startswith("[Persei harness] No more tool calls are available")
    assert "VERDICT" in final.messages[-1].content
    mw.calls = 31  # past the cap (a parallel batch): still forced to text
    mw.wrap_model_call(_Req([HumanMessage("go")], tools), lambda r: seen.append(r))
    assert seen[-1].tools == []
