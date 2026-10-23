"""A plan whose counter cannot go backwards.

`write_todos` REPLACES the whole list -- that is the tool's contract -- and a
model asked to update its plan often complies by writing what is LEFT. Seen
live on 2026-09-12, task 01e640ef: twelve items with six completed became a
fresh six-item list with nothing completed, because the model wrote only the
remaining work. Nothing was lost and nothing had regressed; 23 files were
already changed and 967 lines deleted.

What the operator saw was the step strip falling from 6/12 to 0/6, twice, and
concluded the task was looping. That is the expensive part: a counter that
resets is indistinguishable from lost work, and the honest state of the run
was the opposite.

There is a second, worse consequence. `latest_todos` is not decoration -- the
commit gate reads it to decide whether the plan is finished, and
`incomplete_plan_streak` escalates a task whose plan never completes. A list
that keeps coming back all-pending can hold a finished task open.

So progress is merged rather than replaced: an item that was ever completed
stays completed, and one that disappears from a later list is kept (completed)
instead of vanishing. The model stays free to re-plan -- new items arrive,
reworded items arrive, the order is the model's -- but the record of what it
finished is the system's, not a thing each rewrite can erase.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

_WS = re.compile(r"\s+")

# A repo path inside an item's text. Two items naming the same file are almost
# always the same piece of work reworded, which exact matching cannot see.
_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.\w+")

# How alike two items have to read before they count as the same one.
#
# Exact matching was the original rule and it inflated a plan without bound:
# live on 2026-09-14, task aa457790 grew from 11 items to 46 while doing the
# work correctly, because each rewrite reworded the completed items slightly
# ("Provide mtCore.js -- regime classifier..." / "Write mtCore.js -- regime
# classifier..." / "src/strategies/modules/mtCore.js -- regime classifier...")
# and every unmatched old one was kept alongside its own replacement. Of those
# 46, sixteen were near-duplicates of another.
#
# Chosen against that real list: 0.75 collapses the duplicates without merging
# genuinely different steps. Erring low is the safer direction -- too low only
# loses a completed MARKER on an item the rewrite dropped, while too high is
# what produced the 46.
SAME_ITEM_RATIO = 0.75
# Same file named in both, and the wording at least half alike: the
# "provide/write/create <path>" family, which drifts far more than 0.75 allows.
SAME_PATH_RATIO = 0.5


def _paths(text: str) -> frozenset[str]:
    return frozenset(_PATH.findall(text))


# Below this, only an exact match counts. Short labels are systematically
# similar to each other -- "item 0" and "item 1" are 83% alike by character
# ratio, and a plan of "Step 1..Step 12" would collapse to one item.
MIN_FUZZY_CHARS = 30

_DIGITS = re.compile(r"\d+")


def _same_item(a: str, b: str) -> bool:
    """Whether two normalised item texts describe the same piece of work."""
    if a == b:
        return True
    # If removing the numbers makes them identical, the numbers were the ONLY
    # difference -- "engine 1" and "engine 2" are two steps, not one reworded.
    if _DIGITS.sub("#", a) == _DIGITS.sub("#", b):
        return False
    if len(a) < MIN_FUZZY_CHARS or len(b) < MIN_FUZZY_CHARS:
        return False
    pa, pb = _paths(a), _paths(b)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if pa and pa == pb and ratio >= SAME_PATH_RATIO:
        return True
    return ratio >= SAME_ITEM_RATIO

COMPLETED = "completed"


def _key(todo: object) -> str | None:
    """Identity for matching one rewrite of the plan against the last.

    The content, whitespace-collapsed and case-folded. Not the index: a
    rewrite reorders freely. Not an id: `write_todos` has none to give.
    """
    if not isinstance(todo, dict):
        return None
    content = todo.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return _WS.sub(" ", content).strip().casefold()


def merge_todos(previous: list[Any] | None, incoming: list[Any] | None) -> list[Any] | None:
    """The list to show and to store, given what the model just wrote.

    * an incoming item that was completed before stays completed;
    * a completed item the rewrite dropped is kept, at the front, in its
      original order -- it is the work already done;
    * everything else is exactly what the model wrote, in its order.

    `incoming` of None means the model said nothing this turn, so the
    previous list stands.
    """
    if incoming is None:
        return previous
    if not isinstance(incoming, list):
        return incoming
    if not previous or not isinstance(previous, list):
        return list(incoming)

    done_before = [t for t in previous
                   if isinstance(t, dict) and t.get("status") == COMPLETED and _key(t)]
    done_keys = [_key(t) for t in done_before]
    incoming_keys = [_key(t) for t in incoming if _key(t)]

    def _present(key: str, among: list[str]) -> bool:
        return any(_same_item(key, other) for other in among)

    out: list = []
    # Completed work the rewrite forgot. Kept so the denominator cannot shrink
    # below what was planned and finished -- but matched loosely, because a
    # rewrite that rewords an item it also KEPT would otherwise leave both.
    out.extend({**t, "status": COMPLETED} for t in done_before
               if not _present(_key(t), incoming_keys))
    for todo in incoming:
        key = _key(todo)
        if key and _present(key, done_keys) and isinstance(todo, dict):
            # Reappeared as pending after being finished: the status is the
            # one thing a rewrite does not get to undo.
            out.append({**todo, "status": COMPLETED})
        else:
            out.append(todo)
    return out
