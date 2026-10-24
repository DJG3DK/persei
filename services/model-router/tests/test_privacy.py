"""router_settings.privacy: which OpenRouter providers may serve a call.
Zero retention by default; every configured model was checked to still have
a provider under it on 2026-10-24."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from router import app as app_module
from router import upstream
from router.config import Registry, load


def _cfg(tmp_path, privacy=None, extra_body=None):
    rs = {"fallbacks": []}
    if privacy is not None:
        rs["privacy"] = privacy
    params = {"model": "openrouter/deepseek/flash", **({"extra_body": extra_body} if extra_body else {})}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"model_list": [{"model_name": "agent-coder", "params": params}],
                                 "router_settings": rs}))
    return p


def test_the_default_is_zero_retention_and_a_typo_falls_back_to_it(tmp_path):
    assert load(_cfg(tmp_path)).privacy == "zdr"
    assert load(_cfg(tmp_path, "Deny")).privacy == "deny"
    assert load(_cfg(tmp_path, "off")).privacy == "off"
    assert load(_cfg(tmp_path, "nothing-stored")).privacy == "zdr"


def test_the_rule_joins_the_provider_block_without_losing_the_rest():
    eb = {"provider": {"require_parameters": True, "order": ["a"]}}
    assert upstream.with_privacy(eb, "zdr")["provider"] == {"require_parameters": True, "order": ["a"], "zdr": True}
    assert upstream.with_privacy(eb, "deny")["provider"]["data_collection"] == "deny"
    assert upstream.with_privacy(eb, "off") == eb
    assert upstream.with_privacy({}, "zdr") == {"provider": {"zdr": True}}


def test_a_role_that_sets_its_own_rule_keeps_it():
    own = {"provider": {"data_collection": "allow"}}
    assert upstream.with_privacy(own, "zdr") == own
    assert upstream.with_privacy({"provider": {"zdr": False}}, "deny") == {"provider": {"zdr": False}}


def test_the_rule_reaches_the_upstream_call(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "registry", Registry(_cfg(tmp_path, "deny")))
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-test")
    monkeypatch.setattr(app_module.ledger, "LOG_PATH", tmp_path / "ledger.jsonl")
    seen = []

    async def fake(client, api_key, body, model, extra_body, timeout_s):
        seen.append(extra_body)
        data = {"model": model, "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}}
        return upstream.Attempt(alias="", model=model, ok=True, status=200, duration_s=0.1, payload=data,
                                usage=upstream.Usage.from_payload(data))

    monkeypatch.setattr(app_module.upstream, "call_once", fake)
    with TestClient(app_module.app) as c:
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-test"},
                   json={"model": "agent-coder", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert seen[0]["provider"]["data_collection"] == "deny"
