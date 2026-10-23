"""How long this project's check suite actually takes.

The dashboard's idle banner needs to tell "quiet because a test suite is
grinding" apart from "quiet because something is wedged", and the honest
answer is per project: webapp's curated review suite is 111 tests and runs
307-311s, while a small repo's is twenty seconds. A fixed threshold is wrong
for one of them whichever number is picked.

So the gate measures its own runs and remembers the recent ones, and the
announcement it streams carries the estimate. A median, not a mean: one
pathological run (a cold dependency install, a machine under load) should not
move the number that decides whether a normal run looks broken.

Telemetry, so every failure here is swallowed -- a task must never fail
because we could not write down how long something took.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger("tektonix")

NAMESPACE = ("check_timing",)
# Enough to smooth a bad run, few enough that the number follows a suite that
# has genuinely grown rather than averaging over its whole history.
KEEP = 10


async def record(store: BaseStore | None, repo: str, seconds: float) -> None:
    """Remember one completed check run."""
    if store is None or not repo or seconds <= 0:
        return
    try:
        item = await store.aget(NAMESPACE, repo)
        runs = list((item.value or {}).get("runs") or []) if item else []
        runs.append(round(float(seconds), 1))
        await store.aput(NAMESPACE, repo, {"runs": runs[-KEEP:], "updated_at": time.time()})
    except Exception as e:  # noqa: BLE001
        logger.debug("check timing not recorded for %s: %s", repo, e)


async def expected_seconds(store: BaseStore | None, repo: str) -> float | None:
    """The median of recent runs, or None before there are any.

    None is a real answer and the caller must handle it: the first run on a
    new project has nothing to go on, and inventing a number there would be
    the same mistake as the fixed threshold this replaces.
    """
    if store is None or not repo:
        return None
    try:
        item = await store.aget(NAMESPACE, repo)
    except Exception as e:  # noqa: BLE001
        logger.debug("check timing unreadable for %s: %s", repo, e)
        return None
    runs = [float(r) for r in ((item.value or {}).get("runs") or []) if isinstance(r, (int, float))] if item else []
    return round(statistics.median(runs), 1) if runs else None
