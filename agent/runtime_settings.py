"""Operator-tunable runtime limits, stored rather than baked into the image.

These were module constants and env vars, which meant every adjustment was a
file edit plus a restart -- and a restart is exactly what you cannot do while
the thing you want to retune is running. They now live in the same Postgres
store as skills and memory, so they survive restarts, ride along in the
backup, and take effect on the NEXT turn or task without one.

That last property is not incidental. Every value here is read at the point of
use -- inside build_planning_agent, build_deep_agent, or the verify_and_ship
node -- so a change lands on the next unit of work and never mutates something
already in flight. A task that started under a $2 ceiling finishes under it.

Deliberately NOT here: anything that changes what the system is allowed to do.
Auto-approve and merge review are per-user safety switches with their own
endpoints and their own reasoning. These are dials on how hard it tries before
giving up, which is a different kind of decision and a safe one to expose.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

NAMESPACE = ("settings",)
KEY = "runtime"

# name -> spec. `env` is the variable that seeded the old default, kept so an
# existing deployment's configuration is not silently discarded on upgrade.
KNOBS: dict[str, dict] = {
    "parallel_tasks_per_project": {
        "label": "Tasks at once per project",
        "help": (
            "How many tasks may work on one project at the same time. Each task has its "
            "own workspace, so they cannot touch each other's files; above 1 they code in "
            "parallel and take turns only for checks, review and merge. A task that merges "
            "second is rebased onto the first and reviewed again. Each running task has its "
            "own sandbox containers (2 GB, 2 CPUs each) and model calls, so raise it with the "
            "machine in mind: ten at once measured under a quarter of a 32 GB box (2026-09-25)."
        ),
        "unit": "tasks",
        # 10 since 2026-09-25: six at once used 7 GB of 31 and no CPU to speak
        # of on the benchmark box, and the router did not notice; the
        # operator asked for ten as the default.
        "default": 10,
        "min": 1,
        "max": 16,
        "env": None,
        "group": "Budgets & loop limits",
    },
    "planning_read_budget": {
        "label": "Planning reads before a draft",
        "help": (
            "How many repo file reads a planning turn may make before it must save a "
            "plan. Past it, read_project_file answers 'save the plan now' and reopens "
            "after save_plan. The draft gate: reading is never the deliverable."
        ),
        "unit": "reads",
        "default": 50,
        "min": 5,
        "max": 500,
        "env": "PLANNING_READ_BUDGET",
        "group": "Budgets & loop limits",
    },
    "planning_search_budget": {
        "label": "Planning searches per turn",
        "help": (
            "How many repo searches (search_project / find_files) one planning turn may "
            "run. Past it the tool answers 'budget spent, write the plan from what you "
            "have'. Search is cheap; what it guards against is a session that searches "
            "instead of writing."
        ),
        "unit": "searches",
        "default": 50,
        "min": 5,
        "max": 500,
        "env": "PLANNING_SEARCH_BUDGET",
        "group": "Budgets & loop limits",
    },
    "planning_turn_budget_usd": {
        "label": "Planning turn budget",
        "help": (
            "Dollar ceiling for a single planning turn, on top of what the session "
            "has already spent. Reaching it ends the turn -- the draft and the spend "
            "are both kept."
        ),
        "unit": "$",
        "default": 15.0,
        "min": 0.25,
        "max": 100.0,
        "env": "PLANNING_TURN_BUDGET_USD",
        "group": "Budgets & loop limits",
    },
    "planning_stall_timeout_s": {
        "label": "Planning stall timeout",
        "help": (
            "How long a planning turn may produce NO output before it is treated as "
            "hung. This is silence, not duration: a turn that keeps working runs as "
            "long as it needs. Raise it if a legitimate turn is ever cut off."
        ),
        "unit": "s",
        "default": 1200.0,
        "min": 120.0,
        "max": 7200.0,
        "env": "PLANNING_STALL_TIMEOUT_S",
        "group": "Budgets & loop limits",
    },
    "default_task_budget_usd": {
        "label": "Default task budget",
        "help": "Pre-filled ceiling for a new build task. Per-task, and overridable when you create one.",
        "unit": "$",
        "default": 10.0,
        "min": 0.25,
        "max": 100.0,
        "env": "DEFAULT_BUDGET_USD",
        "group": "Budgets & loop limits",
    },
    "model_call_run_limit": {
        "label": "Model calls per run",
        "help": (
            "Backstop against a runaway loop, not a normal-operation cap -- a healthy "
            "task stays far below it. Applies to the coordinator and to each subagent "
            "separately: the coordinator reaching it ends the pass as an error; a subagent "
            "reaching it ends its own run and hands the coordinator what it has."
        ),
        "unit": "calls",
        "default": 400.0,
        "min": 20.0,
        "max": 2000.0,
        "env": None,
        "group": "Budgets & loop limits",
    },
    "tool_call_run_limit": {
        "label": "Tool calls per run",
        "help": (
            "The same backstop for tool calls rather than model calls. The verifier (30) and "
            "the test-writer (60) have their own tighter caps, so this binds them only when set "
            "lower than those."
        ),
        "unit": "calls",
        "default": 500.0,
        "min": 20.0,
        "max": 3000.0,
        "env": None,
        "group": "Budgets & loop limits",
    },
    "model_call_timeout_s": {
        "label": "Model call timeout",
        "help": (
            "How long a SINGLE call to a model may take before it is abandoned. This "
            "is not the turn or task limit -- it protects against one hung request. "
            "Planning's hard model asks for longer than this on its own, because high "
            "reasoning effort genuinely needs it."
        ),
        "unit": "s",
        "default": 300.0,
        "min": 30.0,
        "max": 1800.0,
        "env": None,
        "group": "Model & sandbox timeouts",
    },
    "planning_model_call_timeout_s": {
        "label": "Planning model call timeout",
        "help": (
            "The same per-call limit for planning's high-reasoning model, which thinks "
            "for far longer per call than an interactive one. Too low and you get "
            "spurious aborts on work that was progressing normally."
        ),
        "unit": "s",
        "default": 1200.0,
        "min": 60.0,
        "max": 3600.0,
        "env": None,
        "group": "Model & sandbox timeouts",
    },
    "check_lint_timeout_s": {
        "label": "Lint timeout",
        "help": "Cap on the project's lint command inside the sandbox.",
        "unit": "s",
        "default": 600.0,
        "min": 30.0,
        "max": 3600.0,
        "env": None,
        "group": "Check timeouts",
    },
    "check_typecheck_timeout_s": {
        "label": "Typecheck timeout",
        "help": "Cap on the project's typecheck command inside the sandbox.",
        "unit": "s",
        "default": 600.0,
        "min": 30.0,
        "max": 3600.0,
        "env": None,
        "group": "Check timeouts",
    },
    "check_test_timeout_s": {
        "label": "Test suite timeout",
        "help": (
            "Cap on the project's `npm test`. Raise this for a repo whose suite is "
            "genuinely long -- a suite that chains dozens of files can exceed the "
            "default, and an abort here reads to the agent as a failing test rather "
            "than as running out of time."
        ),
        "unit": "s",
        "default": 900.0,
        "min": 60.0,
        "max": 7200.0,
        "env": None,
        "group": "Check timeouts",
    },
    "check_review_test_timeout_s": {
        "label": "Review test suite timeout",
        "help": "Cap on the fuller `test:review` suite the gate runs before a merge.",
        "unit": "s",
        "default": 900.0,
        "min": 60.0,
        "max": 7200.0,
        "env": None,
        "group": "Check timeouts",
    },
    "frontend_build_timeout_s": {
        "label": "Frontend build timeout",
        "help": "Cap on the dashboard/frontend production build inside the sandbox.",
        "unit": "s",
        "default": 600.0,
        "min": 60.0,
        "max": 3600.0,
        "env": None,
        "group": "Check timeouts",
    },
    "sandbox_command_timeout_s": {
        "label": "Default shell command timeout",
        "help": (
            "Cap on one agent-issued shell command in the sandbox, where the call site "
            "does not set its own. Installs and builds are the usual reason to raise it."
        ),
        "unit": "s",
        "default": 300.0,
        "min": 30.0,
        "max": 3600.0,
        "env": None,
        "group": "Model & sandbox timeouts",
    },
    "summarization_trigger_tokens": {
        "label": "Summarize build context at",
        "help": (
            "How large a build task's conversation may grow before it is compacted. The old "
            "fixed 80k was 7.6% of the 1,048,576-token window the coder model actually has, and "
            "on 2026-09-14 a task rode it for an hour: context climbed to 80k, compacted to "
            "~55k, climbed again -- 15 summarizer calls, 192 tool calls, zero lines written, "
            "because each compaction threw away the files it had just read and it read them "
            "again. Raise it if tasks re-read their own work; lower it if long tasks cost more "
            "than they should, since average context is what you pay for on every call."
        ),
        "unit": "tokens",
        "default": 250_000.0,
        "min": 20_000.0,
        "max": 800_000.0,
        "env": None,
        "group": "Context",
    },
    "summarization_keep_tokens": {
        "label": "Keep after summarizing",
        "help": (
            "How much of the recent conversation survives a compaction. Must stay well below "
            "the trigger: if the kept window ever approaches it, summarization fires before "
            "every model call and can never get back under, so the task pays for a summarizer "
            "each turn while making no progress. Clamped to 60% of the trigger for that reason."
        ),
        "unit": "tokens",
        "default": 90_000.0,
        "min": 8_000.0,
        "max": 400_000.0,
        "env": None,
        "group": "Context",
    },
    "reflect_on_failure": {
        "label": "Reflect after a failed attempt",
        "help": (
            "When a task's checks fail, the review gate asks for fixes, or GitHub Actions fails, "
            "one short model call writes what failed, why, and what to do differently, and the "
            "next attempt starts from it (1); or the agent goes straight back to fixing with only "
            "the failure text, as it did before 2026-10-17 (0). About a cent per failed attempt. "
            "The reflections are also kept in the task's episode for nightly memory to learn from."
        ),
        "unit": "1 = on, 0 = off",
        "default": 1.0,
        "min": 0.0,
        "max": 1.0,
        "env": None,
        "group": "Context",
    },
    "memory_progressive_disclosure": {
        "label": "Split project memory into sections",
        "help": (
            "Whether a project whose memory has been split carries the small always-on "
            "part plus an index of the rest (1), or the whole file the way it always did "
            "(0). The largest project's memory is ~10,400 tokens on every one of a median "
            "108 model calls per task, and most of it is irrelevant to any given task -- "
            "but a rule the agent never reads is a rule it breaks, so this is the switch "
            "back. It changes nothing for a project small enough that its memory was never "
            "split, and nothing is deleted either way: turning it off restores the old "
            "prompt on the next task, with no migration."
        ),
        "unit": "1 = on, 0 = off",
        "default": 1.0,
        "min": 0.0,
        "max": 1.0,
        "env": None,
        "group": "Context",
    },
    "memory_inline_token_budget": {
        "label": "Memory kept in every prompt",
        "help": (
            "How much of a split project's memory stays resident: the preamble, the "
            "sections pinned as fails-silently rules, and the index of everything else, "
            "all inside this number. Raising it pins more rules and saves fewer tokens; "
            "lowering it pins fewer, and a rule the agent never reads is a rule it "
            "breaks. Lowering it takes effect on the next prompt -- a section that no "
            "longer fits goes back to being an indexed line the agent can fetch, not "
            "something deleted. Raising it only admits more sections once "
            "scripts/migrate_memory_sections.py runs again, whose dry run prints the "
            "resulting floor per project: the number to adjust this from."
        ),
        "unit": "tokens",
        # 3,000 rather than 2,500 because of what 2,500 actually did on the
        # largest real project: it demoted the testing section to an indexed
        # line, and "a suite that never ran still passes" is the headline
        # example of the fails-silently rule this whole split exists to keep
        # resident. Buying back 400 tokens by indexing away the one rule the
        # policy was written to protect is the wrong trade -- 75% off instead
        # of 78% off, and the rule stays in front of the model.
        "default": 3_000.0,
        "min": 500.0,
        "max": 20_000.0,
        "env": None,
        "group": "Context",
    },
    "review_wait_timeout_s": {
        "label": "Review wait timeout",
        "help": (
            "How long to wait for the independent review service on one commit. A real "
            "review of a large diff has been measured near 7 minutes here and past ten in a "
            "desktop sandbox, including a full dependency install and test suite. While the "
            "service reports it is still reviewing, the wait goes on to four times this."
        ),
        "unit": "s",
        "default": 1800.0,
        "min": 120.0,
        "max": 7200.0,
        "env": None,
        "group": "Model & sandbox timeouts",
    },
    "ci_wait_timeout_s": {
        "label": "GitHub Actions wait timeout",
        "help": (
            "How long an approved commit waits for the project's GitHub Actions runs before "
            "the task escalates instead of merging. Main moves only on a green run; resuming "
            "the escalated task waits again."
        ),
        "unit": "s",
        "default": 3600.0,
        "min": 300.0,
        "max": 14400.0,
        "env": None,
        "group": "Model & sandbox timeouts",
    },
    "auto_heal_attempts": {
        # "0 = off" in the label, not only the help: the panel shows help as a
        # hover tooltip, which a phone never shows, and this is the one global
        # switch for the supervisor (2026-09-23 follow-up review, F11).
        "label": "Auto-heal attempts per task (0 = off)",
        "help": (
            "How many times the supervisor may put a task escalated by an infrastructure "
            "failure (the reviewer not answering, main moving under a merge, a dropped "
            "connection) back through the gate on its own, once the cause has cleared. "
            "Failures of the task itself are never healed, nor is anything older than a "
            "day. 0 turns healing off for every task; there is no per-task switch."
        ),
        "unit": "attempts",
        "default": 3,
        "min": 0,
        "max": 10,
        "env": "AUTO_HEAL_ATTEMPTS",
        "group": "Budgets & loop limits",
    },
}

# Seeded from env at import so a deployment that configured these the old way
# keeps its values, then overwritten by whatever the store holds.
_values: dict[str, float] = {}
for _name, _spec in KNOBS.items():
    _raw = os.environ.get(_spec["env"]) if _spec.get("env") else None
    try:
        _values[_name] = float(_raw) if _raw is not None else float(_spec["default"])
    except (TypeError, ValueError):
        _values[_name] = float(_spec["default"])


def value(name: str) -> float:
    """The current value. Sync on purpose: callers are deep inside agent
    construction, and a store round-trip per read would be a database call on
    a hot path for a number that changes a few times a year."""
    return _values.get(name, float(KNOBS[name]["default"]))


def as_int(name: str) -> int:
    return int(value(name))


def all_values() -> dict[str, float]:
    return dict(_values)


def clamp(name: str, raw: float) -> float:
    spec = KNOBS[name]
    return max(float(spec["min"]), min(float(spec["max"]), float(raw)))


async def load(store) -> None:
    """Read stored overrides into the cache. Called once at startup; a failure
    here must never stop the app booting -- the env/default seeding above is
    already a working configuration."""
    try:
        item = await store.aget(NAMESPACE, KEY)
    except Exception:  # noqa: BLE001 -- a settings read must not block startup
        logger.exception("could not load runtime settings; using defaults")
        return
    if not item or not isinstance(item.value, dict):
        return
    for name, raw in item.value.items():
        if name not in KNOBS:
            continue  # a knob removed in a later version; ignore rather than crash
        try:
            _values[name] = clamp(name, float(raw))
        except (TypeError, ValueError):
            logger.warning("ignoring unusable stored value for %s: %r", name, raw)


async def save(store, updates: dict[str, float]) -> dict[str, float]:
    """Validate, persist and apply. Unknown names are rejected rather than
    stored, so a typo cannot sit in the database looking like configuration."""
    unknown = sorted(set(updates) - set(KNOBS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    cleaned = {name: clamp(name, float(raw)) for name, raw in updates.items()}
    merged = {**_values, **cleaned}
    await store.aput(NAMESPACE, KEY, merged)
    _values.update(cleaned)
    return dict(_values)
