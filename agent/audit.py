"""Who changed what, kept where it survives a restart.

This system was built for one operator, and for one operator the answer to
"who approved that?" is always the same person. The moment a second account
exists, it stops being: an admin flips another user's auto-approve, someone
sets a GitHub source to Auto, someone generates a deploy key, and a week
later the only record is a Telegram message in a chat that may have been
cleared -- if notifications were even switched on.

Telegram is not an audit log. It is a notification channel: best-effort,
unordered, per-recipient, and deleted at the whim of whoever owns the chat.
This is the log: append-only in the same Postgres store that holds tasks and
memory, so it rides along in the backup and outlives every restart.

What goes in here is deliberately narrow. Not "everything that happened" --
every task and every model call is already recorded elsewhere -- but the
decisions that *move a control*: who was granted access to skip a gate, who
onboarded a project, who approved a specific gated command, who let an inbox
source create tasks on its own, who minted a key that can push.

Writing a record never blocks the thing being recorded. An approval that
fails because the log write failed would make the log a new way to break the
system; instead a failure is logged at ERROR and the action proceeds. The
trade is deliberate and worth knowing about when reading this page as
evidence: it is a very good record of what happened, not a court exhibit.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from agent.store_paging import all_items

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

logger = logging.getLogger(__name__)

NAMESPACE = ("audit",)

# How many records the store keeps. Each is a few hundred bytes, so this is
# small; the cap exists so an automated toggler cannot grow the table without
# bound, not to save space.
MAX_RECORDS = 2000

# Trimming reads the whole namespace, so it is not done on every write.
_TRIM_EVERY = 100
_writes_since_trim = 0

# The actions worth a record, with the sentence the dashboard shows. Adding
# one here and forgetting the call site is the obvious failure, so
# tests/test_audit.py asserts every action in this table is actually recorded
# somewhere in agent/.
ACTIONS: dict[str, str] = {
    "project.onboard": "onboarded a project",
    "project.move": "pointed a project at another checkout",
    "project.remove": "removed a project from the agent",
    "project.create": "created a project",
    "command.approve": "approved a gated command",
    "command.reject": "rejected a gated command",
    "merge.approve": "approved a merge",
    "merge.request_changes": "sent a merge back for changes",
    "task.operator_edit": "edited a task's files by hand before merge",
    "evals.run": "started a golden eval run",
    "evals.stop": "stopped a golden eval run",
    "swebench.run": "started a SWE-bench run",
    "jobs.run": "ran a daily job now",
    "swebench.stop": "stopped a SWE-bench run",
    "settings.auto_approve": "changed auto-approve of commands",
    "settings.merge_review": "changed the final merge review",
    "auth.passkey_add": "added a passkey to their account",
    "auth.passkey_remove": "removed a passkey from their account",
    # Account and access changes (audit B33, 2026-10-22): the most
    # consequential things on the Users and Environment pages left no trace.
    "auth.user_create": "created an account",
    "auth.user_access": "changed which projects an account can reach",
    "auth.user_delete": "deleted an account",
    "auth.2fa_disable": "turned off two-factor sign-in",
    "env.update": "changed deployment settings (.env)",
    "settings.auto_approve_repos": "changed which projects auto-approve covers",
    "github.source_policy": "changed a GitHub inbox source",
    "inbox.auto_start": "started a task from the inbox automatically (policy: auto)",
    "inbox.approve": "approved an inbox item (a task was started)",
    "inbox.dismiss": "dismissed an inbox item",
    "inbox.snooze": "snoozed an inbox item",
    "deploy_key.generate": "generated a deploy key",
    "deploy_key.delete": "deleted a deploy key",
}


# Bumped per record, so two writes inside the same microsecond still order.
_sequence = 0


def _key(ts: float) -> str:
    """Sortable by time, unique under concurrency. Zero-padded so string
    order is time order -- the store has no ORDER BY for us to use, and the
    trim decides what "newest" means from these keys alone.

    Microseconds, not milliseconds, plus a per-process counter. At
    millisecond resolution a burst of records shared a key prefix and the
    random suffix decided their order -- which meant the trim, whose whole
    job is to keep the newest, kept an arbitrary subset. A burst is exactly
    when trimming happens.
    """
    global _sequence
    _sequence = (_sequence + 1) % 1_000_000
    return f"{int(ts * 1_000_000):018d}-{_sequence:06d}-{uuid.uuid4().hex[:6]}"


async def record(store: BaseStore | None, *, actor: str, action: str, target: str | None = None,
                 detail: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Append one record. Returns it, or None if it could not be written.

    `actor` is the account that made the decision -- an email, or a name like
    "github-inbox" for something the system did on a policy the operator set
    earlier. Never a session token, never an IP: this page is read by people
    and shown in a dashboard.

    One actor is deliberately not an account: a signed approve link carries
    no session, so clicking one in Telegram or email is recorded as
    "signed link" rather than as whoever happens to hold the mailbox. That is
    the honest answer -- the link is what authorised it -- and it is exactly
    why those clicks belong in the log at all.
    """
    if action not in ACTIONS:
        # A typo'd action would be invisible in the UI (no label) and
        # unsearchable. Better to notice in the log than to store it.
        logger.error("audit: unknown action %r -- not recorded", action)
        return None
    if store is None:
        return None
    ts = time.time()
    entry = {
        "ts": ts,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail,
        **(extra or {}),
    }
    try:
        await store.aput(NAMESPACE, _key(ts), entry)
    except Exception as e:  # noqa: BLE001 -- never block the audited action
        logger.error("audit: could not record %s by %s: %s", action, actor, e)
        return None
    await _maybe_trim(store)
    return entry


async def _maybe_trim(store) -> None:
    """Keep the newest MAX_RECORDS, and no more.

    The store returns no promised order, and asking it for a window of
    MAX_RECORDS*2 rows then deleting "the oldest of those" is only correct if
    that window happens to contain the oldest rows -- with an arbitrary
    order it could delete recent entries while genuinely old ones sit
    outside the window forever. So this reads the whole namespace, in pages,
    and decides on the full set. The keys are time-ordered by construction
    (see _key), which is what makes "newest" answerable without trusting the
    order rows arrive in.
    """
    global _writes_since_trim
    _writes_since_trim += 1
    if _writes_since_trim < _TRIM_EVERY:
        return
    _writes_since_trim = 0
    try:
        # MAX_RECORDS * 4, not the helper's own default: overflow is
        # detected by reading past the cap, so a read that stopped AT the
        # cap would report "not over it yet" on every call and the log would
        # grow for ever. Only a store with no `offset` ever sees this number.
        items = await all_items(store, NAMESPACE, no_offset_limit=MAX_RECORDS * 4)
        if len(items) <= MAX_RECORDS:
            return
        for key in sorted(i.key for i in items)[:len(items) - MAX_RECORDS]:
            await store.adelete(NAMESPACE, key)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit: trim failed: %s", e)


async def recent(store, limit: int = 100) -> list[dict]:
    """Newest first. Anything unreadable is skipped rather than raising: a
    single malformed row must not blank the whole page."""
    if store is None:
        return []
    try:
        items = await all_items(store, NAMESPACE, no_offset_limit=MAX_RECORDS * 4)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit: could not read the log: %s", e)
        return []
    rows = []
    for item in items:
        value = item.value if isinstance(item.value, dict) else None
        if not value or "action" not in value:
            continue
        rows.append({**value, "label": ACTIONS.get(value["action"], value["action"])})
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows[:limit]
