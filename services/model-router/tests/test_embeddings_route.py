"""POST /v1/embeddings: the router's first non-chat route.

It exists for one reason and every assertion here defends it. Spend in this
system is what services/model-router/logs/routing.jsonl says was billed --
never a rate table -- so an embedding that did not land in that file with the
provider's own usage.cost would be a hole in the number the operator trusts.
Calling OpenRouter straight from the agent would have been simpler and would
have opened exactly that hole.

The second reason is drift: the chat path's alias resolution, fallback chain,
retry rule and ledger line are SHARED code (app._buffered), not a copy, and
these tests pin the shared behaviour on the embedding side so a future change
cannot quietly apply to only one of them.
"""

from __future__ import annotations

import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from router import app as app_module
from router import upstream
from router.config import Registry

CONFIG = {
    "model_list": [
        # `embedder`, not `agent-embedder`: an agent-* alias has to be a
        # managed role and would appear in the dashboard's chat-seat picker.
        {"model_name": "embedder", "params": {"model": "openrouter/openai/text-embedding-3-small",
                                              "extra_body": {"dimensions": 1536}}},
        {"model_name": "embedder-backup", "params": {"model": "openrouter/other/embed-1"}},
        {"model_name": "agent-coder", "params": {"model": "openrouter/deepseek/flash"}},
    ],
    "router_settings": {"fallbacks": [{"embedder": ["embedder-backup"]}]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    monkeypatch.setattr(app_module, "registry", Registry(cfg))
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-test")
    monkeypatch.setattr(app_module.ledger, "LOG_PATH", tmp_path / "ledger.jsonl")
    with TestClient(app_module.app) as c:
        yield c


def _ok(n=1, model="text-embedding-3-small", cost=1.4e-07, dims=4):
    """The shape OpenRouter actually answers with, measured 2026-09-21.

    Note what is NOT in it: completion tokens. An embedding has no
    completions, and the ledger records that as null rather than as a zero it
    was never told.
    """
    return {"id": "gen-e1", "object": "list", "model": model, "provider": "OpenAI",
            "data": [{"object": "embedding", "index": i, "embedding": [0.1] * dims} for i in range(n)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7, "cost": cost}}


def _stub(monkeypatch, results, *, status=500):
    """results: list of (ok, payload_or_error), consumed in order. Running out
    repeats the last one -- a provider that is down stays down."""
    calls = []

    async def fake(client, api_key, body, model, extra_body, timeout_s):
        calls.append({"model": model, "body": body, "extra_body": extra_body, "timeout": timeout_s})
        ok, data = results[min(len(calls) - 1, len(results) - 1)]
        if ok:
            return upstream.Attempt(alias="", model=model, ok=True, status=200, duration_s=0.1,
                                    payload=data, usage=upstream.Usage.from_payload(data))
        return upstream.Attempt(alias="", model=model, ok=False, status=status,
                                duration_s=0.1, error=str(data))

    monkeypatch.setattr(app_module.upstream, "embed_once", fake)
    monkeypatch.setattr(app_module, "BACKOFF_S", 0.0)
    return calls


def _post(client, **over):
    body = {"model": "embedder", "input": "the health endpoint does not check the database"}
    body.update(over)
    return client.post("/v1/embeddings", json=body, headers={"Authorization": "Bearer sk-test"})


# ---------------------------------------------------------------------------
# auth and routing, the same rules the chat path has
# ---------------------------------------------------------------------------

def test_a_bad_key_is_rejected(client):
    r = client.post("/v1/embeddings", json={"model": "embedder", "input": "x"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_a_missing_key_is_rejected(client):
    assert client.post("/v1/embeddings", json={"model": "embedder", "input": "x"}).status_code == 401


def test_an_unknown_alias_is_404_not_a_wasted_upstream_call(client, monkeypatch):
    calls = _stub(monkeypatch, [])
    r = _post(client, model="no-such-alias")
    assert r.status_code == 404
    assert calls == [], "an unknown alias must not reach the provider"


def test_a_chat_alias_is_not_special_cased(client, monkeypatch):
    """The route resolves aliases, not model families. Pointing it at a chat
    alias is an operator error that belongs upstream, where the error message
    says what is actually wrong."""
    _stub(monkeypatch, [(False, "not an embedding model")], status=400)
    assert _post(client, model="agent-coder").status_code == 502


def test_the_alias_resolves_to_its_model(client, monkeypatch):
    calls = _stub(monkeypatch, [(True, _ok())])
    assert _post(client).status_code == 200
    assert calls[0]["model"] == "openai/text-embedding-3-small"


def test_the_deployments_extra_body_reaches_the_upstream_call(client, monkeypatch):
    """`dimensions` lives in config.yaml so the model can be swapped without
    touching code. If it stopped being passed, a swap to a 3072-dimension
    model would silently widen every vector."""
    calls = _stub(monkeypatch, [(True, _ok())])
    _post(client)
    # (plus the router-wide privacy rule, zero retention by default)
    assert calls[0]["extra_body"] == {"dimensions": 1536, "provider": {"zdr": True}}


def test_a_batch_is_passed_through_as_one_request(client, monkeypatch):
    """The API takes a list, which is what makes a backfill of the whole
    corpus a few requests rather than a few hundred."""
    calls = _stub(monkeypatch, [(True, _ok(n=3))])
    r = _post(client, input=["a", "b", "c"])
    assert r.status_code == 200
    assert len(calls) == 1 and calls[0]["body"]["input"] == ["a", "b", "c"]
    assert len(r.json()["data"]) == 3


def test_the_response_is_returned_verbatim(client, monkeypatch):
    _stub(monkeypatch, [(True, _ok())])
    body = _post(client).json()
    assert body["object"] == "list"
    assert body["data"][0]["embedding"] == [0.1, 0.1, 0.1, 0.1]


# ---------------------------------------------------------------------------
# the ledger, which is the whole reason this route exists
# ---------------------------------------------------------------------------

def test_the_billed_cost_is_the_providers_not_a_rate_table(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok(cost=2.4e-06))])
    _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["cost"] == 2.4e-06
    assert row["alias"] == "embedder" and row["prompt_tokens"] == 7


def test_an_embedding_reports_no_completion_tokens(client, monkeypatch, tmp_path):
    """Null, not zero. The analytics page averages completion tokens per call,
    and a zero would be a measurement rather than an absence."""
    _stub(monkeypatch, [(True, _ok())])
    _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["completion_tokens"] is None


def test_the_ledger_line_matches_the_call_id_header(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok())])
    r = _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["call_id"] == r.headers["x-router-call-id"]


def test_the_task_id_is_recorded_and_not_forwarded(client, monkeypatch, tmp_path):
    """Without it an embedding can be priced but never totalled into the task
    that caused it -- which is where the money question is actually asked."""
    calls = _stub(monkeypatch, [(True, _ok())])
    _post(client, metadata={"agent_task_id": "T1", "agent_session_id": "S1"})
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["task_id"] == "T1" and row["session_id"] == "S1"
    sent = upstream.build_body(calls[0]["body"], "m", {})
    assert "metadata" not in sent, "our routing metadata is not the provider's business"


def test_the_caller_label_is_recorded(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok())])
    _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["caller"] == "master"


def test_a_failed_embedding_is_still_a_ledger_line(client, monkeypatch, tmp_path):
    """The error rate on the Analytics page is only honest if a failure is
    written down as one."""
    _stub(monkeypatch, [(False, "boom")], status=400)
    assert _post(client).status_code == 502
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert rows and all(row["error"] is True for row in rows)


# ---------------------------------------------------------------------------
# the fallback chain, shared with the chat path
# ---------------------------------------------------------------------------

def test_a_transient_failure_retries_the_same_deployment_first(client, monkeypatch):
    calls = _stub(monkeypatch, [(False, "rate limited"), (True, _ok())], status=429)
    r = _post(client)
    assert r.status_code == 200
    assert [c["model"] for c in calls] == ["openai/text-embedding-3-small"] * 2
    assert r.headers["x-router-deployment"] == "embedder", "stayed on the pinned model"


def test_a_permanent_failure_falls_through_immediately(client, monkeypatch):
    calls = _stub(monkeypatch, [(False, "bad request"), (True, _ok())], status=400)
    r = _post(client)
    assert r.status_code == 200
    assert [c["model"] for c in calls] == ["openai/text-embedding-3-small", "other/embed-1"]
    assert r.headers["x-router-deployment"] == "embedder-backup"


def test_the_whole_chain_failing_is_a_502_with_the_chain_named(client, monkeypatch):
    _stub(monkeypatch, [(False, "a")], status=400)
    r = _post(client)
    assert r.status_code == 502
    assert r.json()["error"]["chain"] == ["embedder", "embedder-backup"]
    assert r.headers.get("x-router-call-id")


def test_one_ledger_line_per_attempt_shares_the_call_id(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(False, "a"), (True, _ok())], status=400)
    r = _post(client)
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert {row["call_id"] for row in rows} == {r.headers["x-router-call-id"]}


def test_the_embedding_alias_is_listed_by_v1_models(client):
    """agent/embeddings.py's availability probe asks this route whether the
    deployment exists, rather than reading a config file the running router
    may not have loaded."""
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
    assert "embedder" in {m["id"] for m in r.json()["data"]}


# ---------------------------------------------------------------------------
# the upstream call itself
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_embed_once_posts_to_the_embeddings_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        att = await upstream.embed_once(client, "sk-up", {"model": "embedder", "input": ["x"]},
                                        "openai/text-embedding-3-small", {"dimensions": 1536}, 30)

    assert seen["url"] == upstream.EMBEDDINGS_URL
    assert seen["body"]["model"] == "openai/text-embedding-3-small"
    assert seen["body"]["dimensions"] == 1536
    # Measured 2026-09-21: the embeddings endpoint accepts this and answers
    # with usage.cost either way, so the chat path's body builder is reused
    # rather than forked.
    assert seen["body"]["usage"] == {"include": True}
    assert "stream_options" not in seen["body"], "an embedding has no streaming form"
    assert att.ok and att.usage.cost == 1.4e-07
    assert att.usage.completion_tokens is None


@pytest.mark.anyio
async def test_an_upstream_refusal_is_a_failed_attempt_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Model does not exist", "code": 400}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        att = await upstream.embed_once(client, "sk-up", {"model": "e", "input": "x"},
                                        "openai/no-such-model", {}, 30)

    assert att.ok is False and att.status == 400
    assert "does not exist" in att.error
    assert upstream.is_transient(att.status, att.error) is False, "a 400 must not be retried"


@pytest.fixture
def anyio_backend():
    return "asyncio"
