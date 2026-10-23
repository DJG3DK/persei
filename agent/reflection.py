"""Reflection after a failed attempt.

When a task's attempt fails -- the project's checks, the review gate's
NEEDS_FIXES, or GitHub Actions -- the agent used to go straight back to
fixing with nothing but the failure text. The loop counters
(agent/outer_state.py) catch the third identical fix; nothing prevented the
second. So between the failure and the next attempt, one short model call
writes what failed, why, and what to do differently, and that note leads the
next attempt's message.

Three lines, not an essay, because the next attempt reads it before anything
else:

    FAILED:  the specific failure, quoted from the output
    BECAUSE: the wrong assumption or missed detail behind it
    NEXT:    the one concrete thing to do differently

The task's earlier reflections go into the call, so a NEXT that already failed
is not proposed again. They are kept on the task (the last three) and written
into its episode, where nightly consolidation reads them: a lesson from a task
that went on to ship has been confirmed; one from a task that escalated is a
guess (agent/consolidation.py says so to its model).

Never in the way: a call that fails, times out, or answers in the wrong shape
is dropped and the loop carries on exactly as before. Off with the
`reflect_on_failure` runtime setting.

Merge conflicts and the harness's progress nudges are not reflected on: a
conflict means someone else moved main, and a nudge is about pace, not a
failed attempt.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.config import Config

logger = logging.getLogger("tektonix")

KINDS = {
    "checks": "the project's own checks (tests, lint, typecheck, build) failed",
    "review": "the independent review gate asked for fixes",
    "ci": "GitHub Actions failed on the commit",
}
KEEP = 3
MAX_NOTE_CHARS = 1200
# The role whose seat plans an approach: deciding what to do differently is
# exactly that, and it keeps the reviewer's seat independent of the agent.
ROLE = "agent-planner"
TIMEOUT_S = 90

_LINES = re.compile(r"^\s*(FAILED|BECAUSE|NEXT)\s*:\s*(.+?)\s*$", re.M)


def prompt(goal: str, kind: str, feedback: str, touched: str, earlier: list[str]) -> str:
    """The reflection call's one message."""
    parts = [
        "A coding agent's attempt at a task just failed. Write the reflection its next attempt will read "
        "first. Exactly three lines, nothing before or after:",
        "FAILED: the specific failure, quoted or closely paraphrased from the output below (one line).",
        "BECAUSE: the wrong assumption or missed detail behind it, as far as the evidence shows. If the "
        "output shows the failure is not about this change at all (it fails without it, or the environment "
        "lacks something), say exactly that.",
        "NEXT: the one concrete thing to do differently: name files, tests or functions from the output. "
        "Do not restate the feedback; diagnose it.",
        "",
        "Weigh the evidence: the harness's own results (which checks ran, PASS or FAIL, and a check marked "
        "pre-existing, meaning it fails the same way on the base commit) are facts; a reviewer's reading of "
        "them is an opinion. A failure marked pre-existing is not caused by this change, whatever the review "
        "text suggests: then NEXT is to make that test independent of what the harness lacks if that is "
        "small, or to leave it and say so, never to undo the change.",
        "",
        f"What failed: {KINDS.get(kind, kind)}.",
        "",
        "--- the task ---",
        goal.strip()[:1500],
        "",
        "--- what the attempt changed (files) ---",
        touched.strip()[:1500] or "(unknown)",
        "",
        "--- the failure, as the harness reported it ---",
        feedback.strip()[-6000:],
    ]
    if earlier:
        parts += [
            "",
            "--- earlier reflections in this task ---",
            "Their NEXT was tried and the attempt still failed: do not propose the same thing again.",
            *[f"{i}. {r}" for i, r in enumerate(earlier[-KEEP:], 1)],
        ]
    return "\n".join(parts)


def parse(text: str) -> str | None:
    """The three lines, in order, or None when the answer is not that shape."""
    found = {k: v for k, v in _LINES.findall(text or "")}
    if not all(k in found for k in ("FAILED", "BECAUSE", "NEXT")):
        return None
    note = "\n".join(f"{k}: {found[k]}" for k in ("FAILED", "BECAUSE", "NEXT"))
    return note[:MAX_NOTE_CHARS]


def as_feedback(note: str) -> str:
    """How the note leads the next attempt's message."""
    return (
        "=== Reflection on the last attempt (written before this one) ===\n"
        f"{note}\n"
        "Start from NEXT. The full feedback follows.\n"
        "=== End reflection ==="
    )


async def reflect(config: Config, *, task_id: str, goal: str, kind: str, feedback: str, touched: str,
                  earlier: list[str], model: Any = None) -> str | None:
    """One short call. None on any failure: reflection is never in the way."""
    from langchain_core.messages import HumanMessage

    try:
        if model is None:
            from agent.model_client import llm_for_role
            model = llm_for_role(config, ROLE, timeout=TIMEOUT_S, task_id=task_id, max_tokens=800,
                                 reasoning_effort="low")
        answer = await model.ainvoke([HumanMessage(content=prompt(goal, kind, feedback, touched, earlier))])
    except Exception as e:  # noqa: BLE001 -- the loop goes on without it
        logger.warning("reflection for %s skipped: %s", task_id, e)
        return None
    content = answer.content if isinstance(answer.content, str) else " ".join(
        b.get("text", "") for b in answer.content if isinstance(b, dict))
    note = parse(content)
    if note is None:
        logger.warning("reflection for %s skipped: the answer was not three lines", task_id)
    return note
