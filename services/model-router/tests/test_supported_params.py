"""A model whose providers don't take a sampling setting gets the call
without it (2026-10-24): Kimi K3.1 Pro's one provider takes neither
temperature, top_p nor parallel_tool_calls, and with require_parameters every
agent call was refused and ran on the fallback instead."""

from __future__ import annotations

import asyncio

import pytest

from router import fastest
from router.fastest import SupportedParams, supported_union, trim

KIMI = ["frequency_penalty", "include_reasoning", "max_tokens", "presence_penalty", "reasoning",
        "reasoning_effort", "response_format", "stop", "structured_outputs", "tool_choice", "tools"]
BODY = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2, "top_p": 0.9,
        "parallel_tool_calls": False, "tools": [{"type": "function"}], "tool_choice": "auto",
        "max_completion_tokens": 4000}


def test_only_sampling_knobs_no_provider_takes_are_dropped():
    out, dropped = trim(BODY, set(KIMI))
    assert sorted(d.split(" ")[0] for d in dropped) == ["max_completion_tokens", "parallel_tool_calls",
                                                        "temperature", "top_p"]
    assert out["tools"] and out["tool_choice"] == "auto", "meaning-bearing settings stay"
    assert out["max_tokens"] == 4000 and "max_completion_tokens" not in out
    assert "temperature" not in out and "top_p" not in out and "parallel_tool_calls" not in out


def test_tools_are_never_dropped_even_when_no_provider_takes_them():
    out, dropped = trim({"messages": [], "tools": [1], "tool_choice": "required"}, {"max_tokens"})
    assert out["tools"] == [1] and out["tool_choice"] == "required" and dropped == []


def test_unknown_support_changes_nothing():
    assert trim(BODY, None) == (BODY, []) and trim(BODY, set()) == (BODY, [])


def test_the_union_counts_any_usable_provider_and_skips_ignored_or_degraded_ones():
    eps = [{"tag": "a/x", "provider_name": "A", "status": 0, "supported_parameters": ["tools"]},
           {"tag": "b/x", "provider_name": "B", "status": 0, "supported_parameters": ["temperature"]},
           {"tag": "c/x", "provider_name": "C", "status": -1, "supported_parameters": ["top_p"]}]
    assert supported_union(eps) == {"tools", "temperature"}
    assert supported_union(eps, ["b"]) == {"tools"}


class _Resp:
    def __init__(self, params, status=200):
        self.params, self.status_code = params, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return {"data": {"endpoints": [{"tag": "moonshotai", "provider_name": "Moonshot AI", "status": 0,
                                        "supported_parameters": self.params}]}}


class _Client:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    async def get(self, url, headers=None, timeout=None):
        self.calls += 1
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.mark.asyncio
async def test_the_first_call_waits_for_the_catalogue_and_later_ones_use_the_cache():
    sp, client = SupportedParams(), _Client(_Resp(KIMI))
    out = await sp.body_for(client, "k", "moonshotai/kimi-k3.1-pro", {}, BODY)
    assert "temperature" not in out and out["tools"]
    await sp.body_for(client, "k", "moonshotai/kimi-k3.1-pro", {}, BODY)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_catalogue_that_cannot_be_read_sends_the_call_as_configured():
    sp = SupportedParams()
    assert await sp.body_for(_Client(RuntimeError("down")), "k", "m", {}, BODY) == BODY
    assert await sp.body_for(_Client(_Resp([], 500)), "k", "m2", {}, BODY) == BODY


@pytest.mark.asyncio
async def test_a_slow_catalogue_does_not_hold_the_call(monkeypatch):
    monkeypatch.setattr(fastest, "FIRST_FETCH_TIMEOUT_S", 0.05)

    class Slow(_Client):
        async def get(self, url, headers=None, timeout=None):
            await asyncio.sleep(1)

    assert await SupportedParams().body_for(Slow(None), "k", "m", {}, BODY) == BODY


@pytest.mark.asyncio
async def test_embeddings_are_left_alone():
    body = {"input": "x", "temperature": 1}
    assert await SupportedParams().body_for(_Client(_Resp(KIMI)), "k", "m", {}, body) == body
