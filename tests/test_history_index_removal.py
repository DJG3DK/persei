"""Removing a project has to take its searchable history with it.

The history index is a TABLE, not a store namespace, so
project_removal.namespaces() cannot reach it and the archive/purge loop that
walks that dict walks straight past it. A removed project whose rows stay
behind is not merely untidy: those rows hold the text of episodes the
pruner already deleted from the store, so they are the only remaining copy
of work that belongs to somebody who asked for it to be gone.

The same file pins the leak that was already there before any of this: the
build transcripts agent/planning_log.py writes were never in that dict
either.
"""
from __future__ import annotations

import pytest

from agent import project_removal as pr
from tests.test_project_removal import FakeStore


class FakeIndex:
    """The four calls the removal path makes of an index."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.forgotten: list[str] = []
        self.restored: list[tuple[str, int]] = []

    async def dump_project(self, repo):
        return [r for r in self.rows if r["repo"] == repo]

    async def forget_project(self, repo):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["repo"] != repo]
        self.forgotten.append(repo)
        return before - len(self.rows)

    async def restore_project(self, repo, rows):
        self.restored.append((repo, len(rows)))
        self.rows.extend({**r, "repo": repo} for r in rows)
        return len(rows)


def _row(repo, item_key="/episodes/a.json", **over):
    row = {"corpus": "episode", "repo": repo, "item_key": item_key, "chunk_no": 0,
           "occurred_at": "2026-07-02T11:04:18Z", "task_id": "t1", "session_id": None,
           "outcome": "escalated", "label": "escalated Fix the merge",
           "err": "a phrase that appears nowhere else", "body": "goal text",
           "source_live": False}
    row.update(over)
    return row


@pytest.fixture
def store():
    s = FakeStore()
    for label, ns in pr.namespaces("demo").items():
        s.data[ns] = {f"{label}-key": {"label": label}}
    for ns in pr.namespaces("keeper").values():
        s.data[ns] = {"k": {"label": "keeper"}}
    return s


# ---------------------------------------------------------------------------
# the leak that was already there
# ---------------------------------------------------------------------------

def test_a_project_s_build_transcripts_are_part_of_what_it_owns():
    """agent/planning_log.py writes a task's whole transcript to this
    namespace and the canonical list did not name it, so every removal since
    left the transcripts behind -- megabytes of rows nothing could reach and
    nothing would ever clean up."""
    assert pr.namespaces("demo")["task_log"] == ("task_log", "demo")


async def test_removing_a_project_clears_its_transcripts_too(store):
    await pr.purge(store, "demo")

    assert store.data[("task_log", "demo")] == {}
    assert store.data[("task_log", "keeper")] == {"k": {"label": "keeper"}}


# ---------------------------------------------------------------------------
# the index, which is not a namespace
# ---------------------------------------------------------------------------

async def test_removing_a_project_leaves_no_row_in_the_history_index(store):
    index = FakeIndex([_row("demo"), _row("keeper")])

    await pr.purge(store, "demo", index)

    assert index.forgotten == ["demo"]
    assert [r["repo"] for r in index.rows] == ["keeper"]


async def test_an_archive_carries_the_index_rows_the_store_no_longer_has(store, tmp_path, monkeypatch):
    """A demoted row is the only copy of an episode the pruner deleted. An
    archive without it archives less history than the project had."""
    monkeypatch.setattr(pr, "ARCHIVE_DIR", tmp_path)
    index = FakeIndex([_row("demo"), _row("keeper")])

    doc = await pr.collect(store, "demo", index)

    assert len(doc[pr.HISTORY_FTS_KEY]) == 1
    assert doc[pr.HISTORY_FTS_KEY][0]["err"] == "a phrase that appears nowhere else"


async def test_archive_purge_restore_is_a_round_trip_for_the_index_too(store, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ARCHIVE_DIR", tmp_path)
    index = FakeIndex([_row("demo")])

    doc = await pr.collect(store, "demo", index)
    await pr.purge(store, "demo", index)
    assert index.rows == []

    await pr.restore(store, "demo", doc, index)

    assert index.restored == [("demo", 1)]
    assert [r["item_key"] for r in index.rows] == ["/episodes/a.json"]


async def test_an_archive_restores_the_index_under_the_new_name(store, tmp_path, monkeypatch):
    """Under the CURRENT project name, for the reason the store half works
    that way: a hand-edited archive must not write into a project nobody
    asked for."""
    monkeypatch.setattr(pr, "ARCHIVE_DIR", tmp_path)
    doc = await pr.collect(store, "demo", FakeIndex([_row("demo")]))
    index = FakeIndex()

    await pr.restore(store, "renamed", doc, index)

    assert [r["repo"] for r in index.rows] == ["renamed"]


async def test_an_index_that_will_not_answer_does_not_strand_the_removal(store):
    """The store half has already been cleared by the time the index is
    asked. Raising here would leave the project half-removed."""
    class Broken(FakeIndex):
        async def forget_project(self, repo):
            raise RuntimeError("no database")

    await pr.purge(store, "demo", Broken())

    assert store.data[("episodes", "demo")] == {}


async def test_an_installation_with_no_index_removes_a_project_exactly_as_before(store):
    """None is a supported state everywhere in this subsystem."""
    removed = await pr.purge(store, "demo", None)

    assert removed == len(pr.namespaces("demo"))


# ---------------------------------------------------------------------------
# the archive and the purge are a pair, and the delete is the second half
# ---------------------------------------------------------------------------

async def test_an_archive_that_could_not_read_the_index_is_not_an_archive(store):
    """The same defect shape as sync-then-prune, one level up: the delete was
    not conditional on the copy having succeeded.

    collect() wrapped dump_project in its own `except Exception: logger.
    exception` and left the key an empty list, so the archive was written
    and the step reported ok -- and the route's refuse-to-continue guard,
    the one whose comment says deleting it anyway is the one mistake with no
    undo, never fired, because the failure had already been swallowed a
    level down. purge() then deleted every one of those rows, and for any
    episode already demoted to source_live = FALSE the index row was the
    only copy left.
    """
    class Broken(FakeIndex):
        async def dump_project(self, repo):
            raise RuntimeError("no database")

    with pytest.raises(RuntimeError):
        await pr.collect(store, "demo", Broken())


async def test_a_namespace_that_will_not_read_fails_the_archive(store):
    """Same rule as the index: purge() runs next and deletes the namespace,
    so an archive that recorded it as empty was the last copy, empty."""
    class Unreadable:
        data = {}

        async def asearch(self, ns, limit=100, offset=0):
            raise RuntimeError("the database went away")

    with pytest.raises(RuntimeError):
        await pr.collect(Unreadable(), "demo", FakeIndex())
