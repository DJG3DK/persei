"""The same behaviours, asserted against both backends -- and the places
where the two genuinely differ, asserted as differences.

A second backend is only useful if code written against one runs on the
other. The risk is not that SQLite fails loudly; it is that it succeeds
differently, and the caller that relied on the difference is somewhere else
entirely. So every test below runs twice, through the real
graph.open_store / graph.open_checkpointer, and the four known divergences
are pinned here by name rather than discovered later by a caller.

HOW THE TWO LEGS ARE OBTAINED

* SQLite: a temp file. No server, no system package, nothing to arrange --
  which is why this leg runs everywhere, including CI.
* Postgres: AGENT_TEST_PG_DSN names a database, and CI sets it to the
  postgres:16 service container in the python job. The tables are created in
  a SCRATCH SCHEMA whose name is unique to the run and dropped afterwards,
  so pointing this at a real database cannot touch its store. It is still
  not something to run against production for fun.

    AGENT_TEST_PG_DSN=postgresql://user:pw@host/db .venv/bin/pytest -q \\
        tests/test_sqlite_store_parity.py

The Postgres leg skips itself when that variable is unset, and for two years
of this file's life that was every run anywhere: the reference side of a
parity comparison had never executed, which makes "parity" a claim about one
backend. If you see those skips, the comparison did not happen -- the
SQLite assertions still stand alone, and the divergence tests still assert
the SQLite half, but nothing was compared.
"""

from __future__ import annotations

import os
import uuid
from itertools import groupby

import pytest

from agent import backends, graph, store_paging
from agent.config import load_config

PG_DSN = os.environ.get("AGENT_TEST_PG_DSN")

_BACKENDS = [
    pytest.param("sqlite", marks=pytest.mark.skipif(
        not backends.sqlite_available(),
        reason="the local backend's dependencies are absent: pip install -r requirements-cli.txt")),
    pytest.param("postgres", marks=pytest.mark.skipif(
        not PG_DSN, reason="set AGENT_TEST_PG_DSN to a throwaway database to run the Postgres leg")),
]


@pytest.fixture(scope="session")
def pg_scratch_dsn():
    """A DSN whose search_path points at a schema made for this run.

    The store creates its tables unqualified, so a search_path is the whole
    isolation: `store` and `store_migrations` land in the scratch schema and
    the database's own tables of those names are never opened, let alone
    written.
    """
    if not PG_DSN:
        yield None
        return
    import psycopg  # noqa: PLC0415

    schema = f"store_parity_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_DSN else "?"
    try:
        yield f"{PG_DSN}{sep}options=-c%20search_path%3D{schema}"
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=_BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
async def store(backend, tmp_path, pg_scratch_dsn, monkeypatch):
    # Pinned off rather than inherited. Every test here is written about an
    # UNINDEXED store, and with EMBEDDINGS_ENABLED set in the ambient
    # environment these stores would quietly open with a vector index and
    # embed on every put -- the assertions would still pass, on a different
    # object than the one they name. (They pass today only because
    # embeddings.available() then fails its router probe against conftest's
    # closed port, which is luck, not isolation.)
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "0")
    dsn = f"sqlite:///{tmp_path}/state.db" if backend == "sqlite" else pg_scratch_dsn
    async with graph.open_store(load_config(dsn=dsn)) as opened:
        assert opened.index_config is None
        yield opened


@pytest.fixture
async def saver(backend, tmp_path, pg_scratch_dsn):
    dsn = f"sqlite:///{tmp_path}/state.db" if backend == "sqlite" else pg_scratch_dsn
    async with graph.open_checkpointer(load_config(dsn=dsn)) as opened:
        yield opened


@pytest.fixture
def repo():
    """A namespace label nothing else in this run uses.

    The SQLite leg gets a fresh file per test and the Postgres leg shares
    one scratch schema for the session, so without this the two legs would
    not be running the same test: one would see leftovers from the test
    before it and the other would not. A unique label makes both legs start
    empty, which is the only way the assertions can be identical.
    """
    return f"test-repo-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def ns(repo):
    return ("tasks", repo)


# --- the store, where the two must agree ----------------------------------

async def test_put_get_and_delete_round_trip(store, ns):
    await store.aput(ns, "k1", {"goal": "build a thing", "n": 1})
    item = await store.aget(ns, "k1")
    assert item is not None
    assert item.value == {"goal": "build a thing", "n": 1}
    assert item.key == "k1"
    assert tuple(item.namespace) == ns

    await store.adelete(ns, "k1")
    assert await store.aget(ns, "k1") is None


async def test_getting_a_key_that_was_never_written_is_none_not_an_error(store, ns):
    """Callers branch on the None. A backend that raised instead would turn
    every first-run read into a crash."""
    assert await store.aget(ns, "never-written") is None


async def test_search_pages_with_limit_and_offset_cover_the_namespace_once(store, ns):
    for i in range(30):
        await store.aput(ns, f"t{i:03d}", {"n": i})

    pages = [[item.key for item in await store.asearch(ns, limit=10, offset=off)]
             for off in (0, 10, 20)]
    for page in pages:
        assert len(page) == 10
    seen = [key for page in pages for key in page]
    assert sorted(seen) == [f"t{i:03d}" for i in range(30)], \
        "three pages of ten must be the thirty rows, each exactly once"


async def test_search_filters_on_a_scalar_field(store, ns):
    await store.aput(ns, "a", {"status": "running", "n": 1})
    await store.aput(ns, "b", {"status": "done", "n": 2})
    await store.aput(ns, "c", {"status": "done", "n": 3})

    done = await store.asearch(ns, filter={"status": "done"})
    assert sorted(item.key for item in done) == ["b", "c"]

    one = await store.asearch(ns, filter={"status": "done", "n": 3})
    assert [item.key for item in one] == ["c"], "two filter fields are an AND on both backends"


async def test_the_same_filter_operators_are_unsupported_on_both(store, ns):
    """$in is the one people reach for, and NEITHER backend has it. Pinned
    because a caller that discovers it works on one would write it, and the
    other deployment would fail at runtime with a ValueError from inside the
    library."""
    await store.aput(ns, "a", {"n": 1})
    with pytest.raises(ValueError, match="Unsupported operator"):
        await store.asearch(ns, filter={"n": {"$in": [1, 2]}})

    # And the ones that do work, work on both.
    await store.aput(ns, "b", {"n": 5})
    above = await store.asearch(ns, filter={"n": {"$gt": 1}})
    assert [item.key for item in above] == ["b"]


async def test_list_namespaces_returns_what_was_written(store, repo):
    await store.aput(("tasks", repo), "k", {"n": 1})
    await store.aput(("episodes", repo), "k", {"n": 1})

    found = await store.alist_namespaces(suffix=(repo,))
    assert sorted(tuple(n) for n in found) == [("episodes", repo), ("tasks", repo)]


async def test_a_semantic_query_against_an_unindexed_store_is_ignored_not_refused(store, ns):
    """Both backends drop `query` when no vector index is configured and
    return an ordinary page. That is parity, and it is also a trap: nothing
    in either library will ever tell a caller that semantic search is not
    configured, so whatever offers semantic search has to check for itself.
    """
    for i in range(3):
        await store.aput(ns, f"k{i}", {"text": f"row {i}"})

    hits = await store.asearch(ns, query="something entirely unrelated", limit=3)
    assert len(hits) == 3


# --- the checkpointer, where the two must agree ---------------------------

async def test_checkpointer_put_get_list_and_delete_thread(saver):
    from langgraph.checkpoint.base import empty_checkpoint  # noqa: PLC0415

    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    saved = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
    assert saved["configurable"]["checkpoint_id"] == checkpoint["id"]

    found = await saver.aget_tuple(config)
    assert found is not None and found.checkpoint["id"] == checkpoint["id"]

    listed = [t.checkpoint["id"] async for t in saver.alist(config)]
    assert listed == [checkpoint["id"]]

    await saver.adelete_thread("thread-1")
    assert await saver.aget_tuple(config) is None
    assert [t async for t in saver.alist(config)] == []


# --- where they differ, said out loud -------------------------------------

async def test_item_timestamps_are_naive_on_sqlite_and_aware_on_postgres(store, ns, backend):
    """Postgres columns are TIMESTAMP WITH TIME ZONE; SQLite's are plain
    TIMESTAMP fed by CURRENT_TIMESTAMP. Arithmetic against an aware
    datetime.now(timezone.utc) therefore raises TypeError on one backend and
    not the other -- which is why agent/store_paging.py sorts on
    .timestamp() rather than on the datetimes.

    Pinned so that the day upstream makes SQLite tz-aware we hear it from
    this test instead of from a caller.
    """
    await store.aput(ns, "k", {"n": 1})
    item = await store.aget(ns, "k")

    if backend == "sqlite":
        assert item.created_at.tzinfo is None
        assert item.created_at.microsecond == 0, "SQLite's CURRENT_TIMESTAMP is second-resolution"
    else:
        assert item.created_at.tzinfo is not None


async def test_writes_sharing_a_timestamp_come_back_oldest_first_on_sqlite(store, ns, backend):
    """The behaviour change, not just a formatting one.

    Both backends ORDER BY updated_at DESC. On Postgres the microsecond
    resolution makes that a total order and `asearch(ns, limit=50)` really is
    the newest fifty. On SQLite a burst of writes shares one second, and the
    tie falls back to rowid -- ascending, so the page is the OLDEST rows of
    that second. Anything taking a bare limit for "the newest N" was
    correct only on Postgres; agent/store_paging.recent_items exists for
    that reason.
    """
    for i in range(30):
        await store.aput(ns, f"t{i:03d}", {"n": i})
    items = await store.asearch(ns, limit=30)
    keys_in_store_order = [item.key for item in items]

    groups = [[i.key for i in g] for _, g in groupby(items, key=lambda i: i.updated_at)]
    if backend == "sqlite":
        assert any(len(g) > 1 for g in groups), \
            "thirty writes did not share a second -- this test is no longer measuring the tie"
        for group in groups:
            assert group == sorted(group), "within one second SQLite returns insertion order"
        assert keys_in_store_order[0] != "t029", "the newest row is not first on SQLite"
    else:
        assert keys_in_store_order == [f"t{i:03d}" for i in range(29, -1, -1)]


async def test_recent_items_recovers_the_ordering_only_down_to_one_second(store, ns, backend):
    """What the shared pager does and does not fix.

    It pages the whole namespace and sorts by updated_at descending, so
    rows written in DIFFERENT seconds come back newest-first on both
    backends. Rows written in the SAME second carry the same timestamp, and
    no amount of sorting recovers an order the database did not record --
    the sort is stable, so they keep the store's own (ascending) order.

    That residual is measured here rather than papered over: on SQLite,
    "the newest N" is only meaningful to the second.
    """
    for i in range(6):
        await store.aput(ns, f"t{i:03d}", {"n": i})

    newest_two = [item.key for item in await store_paging.recent_items(store, ns, 2)]
    if backend == "sqlite":
        written = {f"t{i:03d}": (await store.aget(ns, f"t{i:03d}")).updated_at for i in range(6)}
        if len(set(written.values())) == 1:
            assert newest_two == ["t000", "t001"], \
                "one second of writes: the pager cannot tell them apart and keeps store order"
        else:
            # The newest second's rows come first, in store (insertion) order
            # within it: when the writes straddle a boundary early, that is
            # t001, not t004 (a CI flake, 2026-10-24).
            last = max(written.values())
            assert newest_two[0] == min(k for k, t in written.items() if t == last)
    else:
        assert newest_two == ["t005", "t004"]


async def test_the_pragmas_our_opener_sets_are_actually_on(store, backend):
    """The library sets none of these: AsyncSqliteStore.from_conn_string
    passes isolation_level and nothing else. If open_sqlite_conn were ever
    bypassed, the first symptom would be an opaque "database is locked"
    under concurrency and embeddings surviving the memory they belong to --
    neither of which points here. So assert them on the connection the real
    opener produced.
    """
    if backend != "sqlite":
        pytest.skip("pragmas are a SQLite concern; the Postgres branch is pooled instead")

    async def pragma(name):
        cur = await store.conn.execute(f"PRAGMA {name}")
        return (await cur.fetchone())[0]

    assert await pragma("journal_mode") == "wal"
    assert await pragma("busy_timeout") == 10000
    assert await pragma("foreign_keys") == 1, \
        "off by default, and store_vectors' ON DELETE CASCADE is silently ignored without it"
    # The cascade this pragma is for is proved against a real indexed store
    # in tests/test_episode_vectors.py::test_a_deleted_episode_takes_its_
    # vector_with_it -- delete the episode, count store_vectors, get zero.
    # Not duplicated here: this suite's stores are deliberately unindexed.


async def test_a_value_with_real_shape_survives_the_round_trip(store):
    """Every other value in this suite is a flat {goal, n}. An episode is
    not: it is a nested document with nulls, floats, a big integer and
    non-ASCII prose, and the round trip is where two JSON implementations
    diverge if they are going to."""
    value = {
        "goal": "réécrire le résumé — 90% done",
        "nested": {"steps": [{"cmd": "git rebase", "ok": True}, {"cmd": "npm ci", "ok": False}]},
        "escalation_reason": None,
        "cost_usd": 0.4125,
        "iteration_count": 2 ** 63 + 7,
        "empty": {},
        "list": [],
    }
    await store.aput(("episodes", "round-trip"), "k", value)
    item = await store.aget(("episodes", "round-trip"), "k")

    assert item.value == value


async def test_a_nul_in_a_value_is_where_the_two_genuinely_differ(store, backend):
    """SQLite stores a NUL inside a JSON string happily; Postgres refuses
    the whole write with UntranslatableCharacter. Pinned because episodes
    carry tool output, which is where a stray NUL comes from -- and because
    a divergence nobody has written down is one a caller discovers in
    production on one backend only."""
    value = {"goal": "captured output", "tail": "a\x00b"}

    if backend == "sqlite":
        await store.aput(("episodes", "nul"), "k", value)
        assert (await store.aget(("episodes", "nul"), "k")).value == value
        return

    import psycopg  # noqa: PLC0415

    with pytest.raises(psycopg.errors.UntranslatableCharacter):
        await store.aput(("episodes", "nul"), "k", value)


async def test_a_namespace_label_with_a_dot_is_refused_by_both(store):
    """The dot is the separator both backends encode a namespace with, and
    the shared base class refuses it before either one sees it. Asserted
    because a project name is a namespace label, and this is what stops a
    project called "a.b" from reading another project's rows."""
    from langgraph.store.base import InvalidNamespaceError  # noqa: PLC0415

    with pytest.raises(InvalidNamespaceError):
        await store.aput(("tasks", "has.a.dot"), "k", {"n": 1})


async def test_the_store_and_the_checkpointer_write_at_the_same_time(store, saver, ns):
    """Both halves are open at once for the whole life of a task, and on
    SQLite they are two connections on ONE file -- a shape Postgres never
    has, since there both are pooled against a server that expects it.

    Interleaved rather than sequential because the failure this guards is an
    interleave: a writer holding the file while the other connection tries
    to write returns SQLITE_BUSY immediately unless busy_timeout is set, and
    surfaces as "database is locked" nowhere near the cause.
    """
    from langgraph.checkpoint.base import empty_checkpoint  # noqa: PLC0415

    config = {"configurable": {"thread_id": "interleaved", "checkpoint_ns": ""}}
    for i in range(5):
        await store.aput(ns, f"k{i}", {"n": i})
        await saver.aput(config, empty_checkpoint(), {"source": "loop", "step": i}, {})

    assert len(await store.asearch(ns, limit=10)) == 5
    assert len([t async for t in saver.alist(config)]) == 5
