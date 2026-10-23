"""Reading a whole store namespace, and reading the newest N of one.

There were four copies of this loop in the tree and each one had picked a
different subset of the hazards. This is the copy from agent/audit.py, which
had all of them, lifted so there is one.

The hazards, because they are not obvious from the call:

* `asearch(ns, limit=N)` is not "the whole namespace" unless N happens to be
  larger than the namespace. It is a page.
* A short page does not mean the last page. A store may cap a page below
  whatever was asked for, and treating a short page as the end made a log
  that had hundreds of rows read as two rows long.
* A store need not support `offset` at all, and one that accepts it is not
  obliged to honour it. Paging with offset against a store that ignores it
  returns page one forever.

And the one that arrives with a second backend: SQLite's `updated_at` has
one-second resolution where Postgres has microseconds. A burst of writes
therefore shares a timestamp, and `ORDER BY updated_at DESC` falls back to
insertion order within the tie -- oldest first, the exact reverse of what
Postgres returns. Anything that took a bare `limit=N` as "the newest N" was
silently correct only on Postgres. `recent_items` is that call, written so
it is correct on both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore, SearchItem

# One page of a store read. Deliberately large so a normal namespace is one
# call, with paging as the safety net rather than the common path.
_PAGE = 500

# A page that returns nothing new ends the read. Deliberately NOT "a page
# shorter than the limit ends the read": a store is free to cap a page below
# whatever was asked for, and treating a short page as the last one made the
# whole log look two rows long.
_MAX_PAGES = 64

# The default single call made to a store that rejects `offset`. Generous,
# because for such a store this is the only read there will be.
#
# A parameter rather than a fixed constant, because "generous" is relative
# to what the caller does with the answer. agent/audit.py trims its log at
# MAX_RECORDS and detects the overflow by reading PAST that cap; a fallback
# limit equal to its cap would hand it exactly MAX_RECORDS rows forever, it
# would take that for "not over the cap yet", and the log would never be
# trimmed again. Any caller with a threshold of its own passes a limit that
# clears it.
_NO_OFFSET_LIMIT = _PAGE * 4


def _identity(item) -> tuple:
    """What makes a row the same row across two overlapping pages.

    The namespace as well as the key: `ns` is a PREFIX, so a store may
    return rows from several sub-namespaces under it, and two sub-namespaces
    are entitled to hold the same key. Keying on the key alone silently kept
    one of the two.
    """
    return (tuple(getattr(item, "namespace", ()) or ()), item.key)


async def all_items(store: BaseStore, ns: tuple[str, ...], *,
                    no_offset_limit: int = _NO_OFFSET_LIMIT) -> list[SearchItem]:
    """Every record under the namespace prefix `ns`, however many pages that takes.

    `no_offset_limit` is the size of the one read made to a store that has
    no `offset` at all -- see the constant for why a caller that counts the
    result has to choose it.
    """
    seen: dict[tuple[Any, ...], SearchItem] = {}
    offset = 0
    for _ in range(_MAX_PAGES):
        try:
            page = await store.asearch(ns, limit=_PAGE, offset=offset)
        except TypeError:
            # A store without offset support: one call is all there is.
            page = await store.asearch(ns, limit=no_offset_limit)
            for item in page:
                seen.setdefault(_identity(item), item)
            break
        fresh = [i for i in page if _identity(i) not in seen]
        for item in fresh:
            seen[_identity(item)] = item
        # No page at all, or nothing this read had not already seen: done.
        # The second condition is also what stops a store that ignores
        # `offset` from spinning here forever.
        if not page or not fresh:
            break
        offset += len(page)
    return list(seen.values())


def _updated_at_key(item) -> float:
    """A sortable number for an item's `updated_at`.

    A number rather than the datetime itself: Postgres hands back an aware
    datetime and SQLite a naive one, and comparing the two raises TypeError
    rather than sorting wrongly -- so a list that ever mixed them would take
    the whole page down. Anything without a usable timestamp sorts oldest,
    which is where an item carrying no ordering information belongs.
    """
    ts = getattr(item, "updated_at", None)
    try:
        return ts.timestamp()
    except AttributeError:
        return 0.0


async def recent_items(store, ns, limit: int) -> list:
    """The `limit` most recently updated records under the prefix `ns`, newest first.

    Pages the whole namespace and sorts it here rather than trusting the
    store's own ordering to survive a tie -- see this module's docstring.
    On Postgres the result is the same rows in the same order that
    `asearch(ns, limit=limit)` returns today.
    """
    items = await all_items(store, ns)
    items.sort(key=_updated_at_key, reverse=True)
    return items[:limit]
