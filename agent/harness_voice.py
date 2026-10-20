"""Who is speaking, in the agent's own conversation.

Everything Persei itself says to the model or refuses it -- a guard's
refusal, a checkpoint, the ship gate's feedback, a handover to another seat,
the framing of a benchmark task -- is marked as the harness, and what the
operator wrote is marked as the operator. A trajectory is read by the
operator, by leaderboard reviewers, and by the model itself; none of them
should have to guess which words were the system's (operator's rule,
2026-09-24).
"""
from __future__ import annotations

HARNESS = "[Persei harness]"
OPERATOR = "[operator]"


def harness(text: str) -> str:
    """`text`, marked as the harness's -- once."""
    text = text or ""
    return text if text.startswith(HARNESS) else f"{HARNESS} {text}"


def operator(text: str) -> str:
    text = (text or "").strip()
    return text if text.startswith(OPERATOR) else f"{OPERATOR} {text}"


def marked(text: str) -> str:
    """Feedback on its way into the conversation: anything not already
    attributed to the operator or the harness is the harness's."""
    if (text or "").startswith((HARNESS, OPERATOR, "[operator ")):
        return text
    return harness(text)
