"""The router's privacy rule from the dashboard, and the provider picker's
facts about each host (2026-10-24)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent import metrics
from agent import model_config as mc

CONFIG = """model_list:
  - model_name: agent-coder
    params:
      model: openrouter/deepseek/deepseek-v4.1-flash-1017

router_settings:
  # tried in order when the pinned model refuses
  fallbacks:
    - agent-coder: [backup]
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    monkeypatch.setattr(mc, "ROUTER_CONFIG_PATH", path)
    return path


def test_zero_retention_is_the_default_until_set(cfg):
    assert mc.get_privacy() == "zdr"


def test_setting_it_adds_then_replaces_the_line_and_keeps_every_comment(cfg):
    assert mc.set_privacy("deny") == "deny"
    text = cfg.read_text()
    assert '  privacy: "deny"\n' in text and "# tried in order when the pinned model refuses" in text
    assert mc.set_privacy("off") == "off"
    assert cfg.read_text().count("privacy:") == 1
    assert "fallbacks:" in cfg.read_text()


def test_a_config_with_no_router_settings_block_gets_one(cfg):
    cfg.write_text(CONFIG.split("router_settings:")[0])
    assert mc.set_privacy("deny") == "deny"
    assert cfg.read_text().rstrip().endswith('router_settings:\n  privacy: "deny"')


def test_an_unknown_mode_is_refused_and_nothing_is_written(cfg):
    with pytest.raises(ValueError):
        mc.set_privacy("whatever")
    assert cfg.read_text() == CONFIG


def test_the_router_reads_what_the_dashboard_writes(cfg):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "model-router"))
    from router.config import load
    mc.set_privacy("deny")
    assert load(cfg).privacy == "deny"
    mc.set_privacy("off")
    assert load(cfg).privacy == "off"
    # A hand-written bare `off` (YAML false) is off too, on both sides.
    cfg.write_text(cfg.read_text().replace('privacy: "off"', "privacy: off"))
    assert load(cfg).privacy == "off" and mc.get_privacy() == "off"
    assert load(cfg).fallbacks == {"agent-coder": ["backup"]}


def test_the_provider_record_counts_our_week_with_each_host(tmp_path, monkeypatch):
    log = tmp_path / "routing.jsonl"
    rows = [
        {"ts": 99000, "routed_model": "m", "provider": "Fireworks", "completion_tokens": 500},
        {"ts": 99001, "routed_model": "m", "provider": "Fireworks", "error": True},
        {"ts": 99002, "routed_model": "m", "provider": "DeepInfra", "completion_tokens": 32000},
        {"ts": 99003, "routed_model": "m", "error": True},              # no host: not attributed
        {"ts": 99004, "routed_model": "other", "provider": "Fireworks"},
        {"ts": 1, "routed_model": "m", "provider": "Fireworks"},          # outside the week
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(metrics, "ROUTING_LOG", log)
    monkeypatch.setattr(metrics, "_PROVIDER_RECORD_CACHE", {})
    rec = metrics.provider_record("m", window_days=1, now=100000)
    assert rec == {"fireworks": {"calls": 2, "errors": 1, "runaways": 0},
                   "deepinfra": {"calls": 1, "errors": 0, "runaways": 1}}


@pytest.mark.asyncio
async def test_endpoints_carry_whereabouts_policy_and_record_and_survive_a_failed_lookup(monkeypatch):
    async def directory():
        return {"fireworks": {"headquarters": "US", "datacenters": ["US"], "privacy_policy_url": "https://p"}}
    monkeypatch.setattr(mc, "_provider_directory", directory)
    monkeypatch.setattr(metrics, "provider_record", lambda model: {"fireworks": {"calls": 9, "errors": 0, "runaways": 0}})
    out = await mc._with_provider_facts("m", [{"provider": "Fireworks"}, {"provider": "Novita"}])
    assert out[0]["headquarters"] == "US" and out[0]["record"]["calls"] == 9
    assert out[1]["privacy_policy_url"] is None and out[1]["record"] == {"calls": 0, "errors": 0, "runaways": 0}

    async def down():
        raise RuntimeError("directory down")
    monkeypatch.setattr(mc, "_provider_directory", down)
    out = await mc._with_provider_facts("m", [{"provider": "Fireworks"}])
    assert out[0]["provider"] == "Fireworks" and out[0]["headquarters"] is None
