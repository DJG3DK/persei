"""Analytics computed from this box's own records, not from LangSmith.

Three panels -- per-role model usage, tool reliability, run health -- used to
be read back out of a third-party service. That made an optional, off-box
dependency load-bearing for "what is this agent doing", and the redacting
tracer needed to make those traces safe was measured burning ~100% of a core,
on the event loop (2026-09-12).

Everything they need is written here already: the router's per-call ledger and
the work node's tool-result log. The ledger is strictly better than the traces
were -- it knows what the router was BILLED and how much of each prompt came
from cache, neither of which LangSmith ever saw.
"""

from __future__ import annotations

import json
import time

import pytest

from agent import metrics, tool_events


def _routing(tmp_path, rows):
    p = tmp_path / "routing.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def _call(**over):
    row = {
        "ts": time.time(),
        "call_id": "c",
        # The field names are historically crossed: requested_model holds the
        # UNDERLYING model, routed_model holds the alias the proxy served.
        "requested_model": "deepseek/deepseek-v4.1-flash",
        "routed_model": "agent-coder",
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cached_tokens": 0,
        "cost": 0.01,
        "duration_s": 3.0,
        "task_id": "T1",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# model usage
# ---------------------------------------------------------------------------

def test_usage_is_grouped_by_role_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(), _call(),
        _call(routed_model="agent-test-writer"),
        _call(routed_model="agent-coder", requested_model="poolside/laguna-s-2.1"),
    ]))
    models = metrics.model_usage()["models"]
    # short role names: the dashboard keys its labels on these
    assert {(m["role"], m["model"], m["calls"]) for m in models} == {
        ("coder", "deepseek/deepseek-v4.1-flash", 2),
        ("test-writer", "deepseek/deepseek-v4.1-flash", 1),
        ("coder", "poolside/laguna-s-2.1", 1),
    }
    assert [m["calls"] for m in models] == sorted([m["calls"] for m in models], reverse=True)


def test_it_reports_what_the_router_was_billed(tmp_path, monkeypatch):
    """The column LangSmith could never have: the traces knew tokens, the
    router knows what OpenRouter charged for them."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(cost=0.02), _call(cost=0.03),
    ]))
    assert metrics.model_usage()["models"][0]["cost_usd"] == 0.05


def test_it_reports_how_much_of_the_prompt_was_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(prompt_tokens=1000, cached_tokens=750),
        _call(prompt_tokens=1000, cached_tokens=250),
    ]))
    m = metrics.model_usage()["models"][0]
    assert m["cached_tokens"] == 1000
    assert m["cache_hit_rate"] == pytest.approx(0.5)


def test_calls_that_are_not_this_agent_are_left_out(tmp_path, monkeypatch):
    """The router is shared -- the review service and the tier system use it
    too. Only agent-* aliases are this agent's roles."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(), _call(routed_model="smart-router"), _call(routed_model="reasoning-tier"),
    ]))
    models = metrics.model_usage()["models"]
    assert len(models) == 1 and models[0]["role"] == "coder"


def test_the_window_excludes_older_calls(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(ts=now - 2 * 86400), _call(ts=now - 40 * 86400),
    ]))
    assert metrics.model_usage(window_days=7, now=now)["models"][0]["calls"] == 1


def test_latency_is_averaged_only_over_calls_that_reported_it(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(duration_s=2.0), _call(duration_s=4.0), _call(duration_s=None),
    ]))
    assert metrics.model_usage()["models"][0]["avg_latency_s"] == pytest.approx(3.0)


def test_a_missing_or_torn_log_answers_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", tmp_path / "nope.jsonl")
    assert metrics.model_usage()["models"] == []

    p = tmp_path / "routing.jsonl"
    p.write_text(json.dumps(_call()) + "\n" + '{"ts": 1, "half\n')
    monkeypatch.setattr(metrics, "ROUTING_LOG", p)
    assert metrics.model_usage()["models"][0]["calls"] == 1


def test_the_shape_the_dashboard_expects_is_unchanged(tmp_path, monkeypatch):
    """AgentModelUsage in frontend/src/types.ts. Extra columns are fine; a
    missing one blanks the panel."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [_call()]))
    row = metrics.model_usage()["models"][0]
    for field in ("role", "model", "calls", "tokens_in", "tokens_out", "avg_latency_s"):
        assert field in row


# ---------------------------------------------------------------------------
# tool reliability
# ---------------------------------------------------------------------------

def test_tool_results_are_counted_and_failures_separated(tmp_path, monkeypatch):
    log = tmp_path / "tool_events.jsonl"
    for tool, ok in (("bash", True), ("bash", True), ("bash", False), ("edit", True)):
        tool_events.record(tool=tool, ok=ok, task_id="T1", path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)

    data = metrics.tool_reliability()
    by = {t["tool"]: t for t in data["tools"]}
    assert by["bash"]["calls"] == 3 and by["bash"]["errors"] == 1
    assert by["bash"]["error_rate"] == pytest.approx(1 / 3)
    assert by["edit"]["errors"] == 0
    assert data["daily"][-1]["errors"] == 1 and sum(d["errors"] for d in data["daily"]) == 1


def test_the_tool_log_never_carries_the_tool_output(tmp_path):
    """The output is the part that carries secrets, and it is already in the
    task stream. Only a short reason is kept, and only for a failure."""
    log = tmp_path / "tool_events.jsonl"
    tool_events.record(tool="bash", ok=True, task_id="T", detail="SECRET=hunter2", path=log)
    tool_events.record(tool="bash", ok=False, task_id="T", detail="x" * 500, path=log)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["detail"] == "SECRET=hunter2"[:200]  # caller's choice, still capped
    assert len(rows[1]["detail"]) == 200, "a failure reason is capped, never the whole output"


def test_recording_never_raises(tmp_path):
    """Telemetry must not be able to break the pass it describes."""
    tool_events.record(tool="bash", ok=True, path=tmp_path / "no" / "such" / "dir" / "x.jsonl")


def test_the_tool_log_is_trimmed_rather_than_growing(tmp_path, monkeypatch):
    log = tmp_path / "tool_events.jsonl"
    monkeypatch.setattr(tool_events, "MAX_BYTES", 4000)
    for i in range(400):
        tool_events.record(tool=f"tool{i}", ok=True, task_id="T", path=log)
    assert log.stat().st_size <= 4000 * 1.5
    assert log.read_text().splitlines(), "trimming must not empty it"


def test_tool_reliability_with_no_log_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", tmp_path / "nope.jsonl")
    data = metrics.tool_reliability()
    assert len(data["daily"]) == data["window_days"] and not any(d["errors"] for d in data["daily"])
    assert {**data, "daily": []} == {"tools": [], "daily": [], "nudges": [],
                                          "window_days": 7, "source": "tool-events"}


# ---------------------------------------------------------------------------
# nudged calls
#
# A shell command the harness pointed at a cheaper tool used to be written as
# its own event named "bash-as-read", which put a tool nobody has on the
# reliability panel and added one phantom call per flagged command. It is a
# field on the call it belongs to now.
# ---------------------------------------------------------------------------

def test_a_nudged_bash_call_is_one_bash_call(tmp_path, monkeypatch):
    log = tmp_path / "tool_events.jsonl"
    tool_events.record(tool="bash", ok=True, task_id="T1", path=log)
    tool_events.record(tool="bash", ok=True, task_id="T1", nudge="read", path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)

    data = metrics.tool_reliability()
    assert [t["tool"] for t in data["tools"]] == ["bash"], "no invented tool names"
    assert data["tools"][0]["calls"] == 2
    assert data["tools"][0]["nudged"] == 1


def test_nudges_are_counted_by_kind(tmp_path, monkeypatch):
    log = tmp_path / "tool_events.jsonl"
    for nudge in ("read", "memory-read", "memory-read", None):
        tool_events.record(tool="bash", ok=True, task_id="T1", nudge=nudge, path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)

    data = metrics.tool_reliability()
    assert data["nudges"] == [{"kind": "memory-read", "count": 2}, {"kind": "read", "count": 1}]
    assert data["tools"][0]["nudged"] == 3


def test_a_nudge_is_not_an_error(tmp_path, monkeypatch):
    """The command ran and produced output -- it was just the expensive way to
    get it. Counting it as a failure would make the habit look like breakage."""
    log = tmp_path / "tool_events.jsonl"
    tool_events.record(tool="bash", ok=True, task_id="T1", nudge="write", path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)

    data = metrics.tool_reliability()
    assert data["tools"][0]["errors"] == 0
    assert data["tools"][0]["error_rate"] == 0.0
    assert not any(d["errors"] for d in data["daily"])


# ---------------------------------------------------------------------------
# run summary
# ---------------------------------------------------------------------------

def test_the_summary_counts_tasks_and_their_wall_clock(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(ts=now - 600, task_id="T1"), _call(ts=now - 300, task_id="T1"),
        _call(ts=now - 100, task_id="T2"), _call(ts=now - 40, task_id="T2"),
    ]))
    s = metrics.run_summary(now=now)
    assert s["trace_count"] == 2, "two tasks, not four calls"
    assert s["avg_latency_s"] == pytest.approx((300 + 60) / 2)
    assert s["model_calls"] == 4
    assert s["total_input_tokens"] == 4000


def test_the_summary_reports_the_error_rate_over_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(), _call(error=True), _call(), _call(),
    ]))
    assert metrics.run_summary()["error_rate"] == pytest.approx(0.25)


def test_the_summary_keeps_the_field_names_the_dashboard_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [_call()]))
    s = metrics.run_summary()
    for field in ("trace_count", "avg_latency_s", "error_rate",
                  "total_input_tokens", "total_output_tokens"):
        assert field in s


# ---------------------------------------------------------------------------
# Attributing a call to a role
#
# The router's log has three fields that could name a model, and only one of
# them reliably names the ROLE. The old writer set kwargs["model"] to the resolved
# deployment and the response carries whatever the provider returned, so on
# most historical lines neither is an alias and the call cannot be attributed
# at all -- which silently dropped the majority of this agent's traffic out of
# the panel until the router started recording model_group as `alias`.
# ---------------------------------------------------------------------------

def test_the_recorded_alias_is_what_names_the_role(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        # the shape the router writes now: both old fields hold the raw model
        _call(alias="agent-coder",
              requested_model="deepseek/deepseek-v4.1-flash",
              routed_model="deepseek/deepseek-v4.1-flash"),
    ]))
    m = metrics.model_usage()["models"][0]
    assert m["role"] == "coder"
    assert m["model"] == "deepseek/deepseek-v4.1-flash", "the role never stands in for the model"


def test_older_lines_still_attribute_when_a_field_happens_to_carry_the_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(routed_model="agent-test-writer", requested_model="deepseek/deepseek-v4.1-flash"),
        _call(requested_model="agent-planner", routed_model="qwen/qwen3.8-max"),
    ]))
    roles = {m["role"] for m in metrics.model_usage()["models"]}
    assert roles == {"test-writer", "planner"}


def test_a_line_that_names_no_alias_is_not_guessed_at(tmp_path, monkeypatch):
    """Either another consumer of this shared router, or a line written before
    aliases were recorded. Inventing a role for it would be worse than leaving
    it out, and counting it under someone else's role worse still."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(alias=None, requested_model="poolside/laguna-s-2.1",
              routed_model="poolside/laguna-s-2.1"),
    ]))
    assert metrics.model_usage()["models"] == []


def test_a_call_with_no_underlying_model_is_labelled_unknown(tmp_path, monkeypatch):
    """A failed call never got a response, so there is no model to name. The
    live log carries 83 of these from one afternoon of 401s; printing the
    alias in the model column would read as a model called
    "agent-classifier"."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        {"ts": time.time(), "requested_model": "agent-classifier", "routed_model": None,
         "alias": None, "error": True},
    ]))
    m = metrics.model_usage()["models"][0]
    assert m["role"] == "classifier"
    assert m["model"] == "unknown"
    assert m["errors"] == 1


def test_roles_use_the_short_name_the_dashboard_keys_on(tmp_path, monkeypatch):
    """AnalyticsView renders a curated row per core role ("planner", "coder",
    ...) and appends any role it does not know. Returning the raw alias made
    every role appear TWICE: an empty curated row, and a raw "agent-coder" row
    with the numbers. Reported live within an hour of shipping it."""
    monkeypatch.setattr(metrics, "ROUTING_LOG", _routing(tmp_path, [
        _call(alias="agent-coder"), _call(alias="agent-planning-chat-hard"),
    ]))
    roles = {m["role"] for m in metrics.model_usage()["models"]}
    assert roles == {"coder", "planning-chat-hard"}
    assert not any(r.startswith("agent-") for r in roles)


def test_the_old_marker_rows_are_folded_into_bash(tmp_path, monkeypatch):
    """Events written before the nudge became a field.

    They were a second event per flagged command, named "bash-as-read". Those
    lines stay in the window for a fortnight after the change, and they say
    exactly what the field says -- so they count as nudges on bash rather than
    standing on the panel as a tool nobody has. They bring no call of their
    own: the real bash row was always written too.
    """
    log = tmp_path / "tool_events.jsonl"
    tool_events.record(tool="bash", ok=True, task_id="T1", path=log)
    tool_events.record(tool="bash-as-read", ok=True, path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)

    data = metrics.tool_reliability()
    assert [t["tool"] for t in data["tools"]] == ["bash"]
    assert data["tools"][0]["calls"] == 1, "the marker was never a call of its own"
    assert data["tools"][0]["nudged"] == 1
    assert data["nudges"] == [{"kind": "read", "count": 1}]


def test_the_daily_series_covers_every_day_of_the_window_zero_where_nothing_failed(tmp_path, monkeypatch):
    """One busy day used to be the whole chart (2026-09-26)."""
    log = tmp_path / "tool_events.jsonl"
    tool_events.record(tool="bash", ok=False, task_id="T1", path=log)
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)
    now = time.time()
    data = metrics.tool_reliability(window_days=14, now=now)
    assert len(data["daily"]) == 14
    assert data["daily"][-1]["date"] == time.strftime("%Y-%m-%d", time.gmtime(now)) and data["daily"][-1]["errors"] == 1
    assert all(d["errors"] == 0 for d in data["daily"][:-1])
    assert [d["date"] for d in data["daily"]] == sorted(d["date"] for d in data["daily"])


def test_a_redirected_process_writes_beside_its_run_not_in_production_s_log(tmp_path, monkeypatch):
    prod = tmp_path / "prod.jsonl"
    mine = tmp_path / "run" / "tool_events.jsonl"
    monkeypatch.setattr(tool_events, "LOG_PATH", prod)
    tool_events.redirect(mine)
    try:
        tool_events.record(tool="bash", ok=True, task_id="B1")
    finally:
        tool_events.redirect(None)
    assert mine.is_file() and not prod.exists()
    tool_events.record(tool="bash", ok=True, task_id="P1")
    assert prod.is_file()


def test_a_repeat_guard_row_is_filed_under_the_tool_it_blocked(tmp_path, monkeypatch):
    """Before 2026-10-01 the guard's results carried no name and the panel
    showed them as "unknown"; old rows are read back by their message."""
    import json
    from agent import metrics
    log = tmp_path / "events.jsonl"
    now = 1_800_000_000.0
    rows = [
        {"ts": now - 60, "tool": "unknown", "ok": False,
         "detail": "[Persei harness] ERROR: `bash` with these exact arguments has now been requested 4 times"},
        {"ts": now - 50, "tool": "unknown", "ok": False,
         "detail": "[Persei harness] REPEATED CALL: this is the 3rd identical `read` call in a row"},
        {"ts": now - 40, "tool": "unknown", "ok": False, "detail": "something else"},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(metrics, "TOOL_EVENTS_LOG", log)
    tools = {t["tool"]: t["errors"] for t in metrics.tool_reliability(window_days=1, now=now)["tools"]}
    assert tools == {"bash": 1, "read": 1, "unknown": 1}


def test_the_repeat_guard_names_the_call_it_answers():
    from agent.middleware.repeat_guard import RepeatCallGuardMiddleware
    guard = RepeatCallGuardMiddleware.__new__(RepeatCallGuardMiddleware)
    call = {"name": "bash", "id": "c1", "args": {}}
    from langchain_core.messages import ToolMessage
    run = {"n": 4, "last": ToolMessage(content="same", tool_call_id="c0", name="bash")}
    assert guard._refusal(call, run).name == "bash"
    assert guard._cached(call, run).name == "bash"
