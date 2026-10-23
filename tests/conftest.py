"""Test runs must never trace to the production LangSmith project -- a
synthetic fixture conversation landing in the same project the live agent
traces to is indistinguishable from real pathology at a glance, and it also
skews the analytics scan's usage numbers.

Set before any langchain import (conftest is imported first), and
load_dotenv never overrides already-set env vars, so .env can't re-enable
it mid-suite.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# audit H4: agent/config.py calls load_config() at import time and reads
# un-defaulted variables with os.environ[...], so on a checkout without a
# .env, 13 of 30 test files died during COLLECTION with KeyError:
# 'LANGGRAPH_PG_DSN' -- `pytest` did not run at all for a new contributor, and
# the failure looked like a broken suite rather than a missing file.
#
# setdefault, never assignment: a real .env or a real environment still wins,
# so this cannot mask a misconfigured deployment. The values are deliberately
# obvious placeholders -- nothing in the suite connects to Postgres or the
# network, and anything that tried would fail loudly against these rather than
# silently reaching a real service.
#
# Which is why the router placeholder is a CLOSED port. It used to be
# 127.0.0.1:4000, the real router's address -- harmless on a contributor's
# laptop, and not harmless on the one machine that also runs the router,
# where three planning tests were quietly issuing live classifier calls on
# every `pytest` run (rejected on the placeholder key, so they showed up only
# as 401s in routing.jsonl). A port nothing listens on makes the claim above
# true everywhere.
for _key, _placeholder in {
    "LANGGRAPH_PG_DSN": "postgresql://test:test@127.0.0.1:5432/test_placeholder",
    "MODEL_ROUTER_URL": "http://127.0.0.1:9",
    "MODEL_ROUTER_KEY": "test-placeholder",
    "AUTH_SECRET_KEY": "dGVzdC1wbGFjZWhvbGRlci0zMi1ieXRlcy1rZXktMDAwMA==",
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASS": "",
    "SMTP_FROM": "",
}.items():
    os.environ.setdefault(_key, _placeholder)

import pytest

from agent.config import PROJECTS


@pytest.fixture(autouse=True)
def _test_repo_project(tmp_path_factory):
    """Ensures a "test-repo" entry exists in PROJECTS for the duration of
    each test, so unit tests can use a fixture repo name without depending
    on this deployment's real projects.json contents. Restored afterward so
    tests don't leak state into each other."""
    had_entry = "test-repo" in PROJECTS
    previous = PROJECTS.get("test-repo")
    sandbox = tmp_path_factory.mktemp("test-repo-sandbox")
    PROJECTS["test-repo"] = {"sandbox": str(sandbox), "live": str(sandbox)}
    yield
    if had_entry:
        PROJECTS["test-repo"] = previous
    else:
        PROJECTS.pop("test-repo", None)


@pytest.fixture(autouse=True)
def _advisory_lock_without_postgres(monkeypatch):
    """project_lock takes a Postgres advisory lock so two processes cannot run
    one project (agent/graph.py). CI has no Postgres, and a test that drives
    _stream_graph should not need one, so the connection is faked here: the
    lock's own code path still runs, and its real behaviour -- including what
    happens when another process holds it -- is covered against a fake
    connection in tests/test_project_lock.py.
    """
    from agent import graph as _graph

    class _Cursor:
        async def fetchone(self):
            return (True,)          # always free: no other process in a test

    class _Conn:
        async def execute(self, sql, params=None):
            return _Cursor()

        async def close(self):
            return None

    async def _connect(dsn, autocommit=True):
        return _Conn()

    monkeypatch.setattr(_graph, "_connect", _connect)


@pytest.fixture(autouse=True)
def _task_workspace_is_the_project_workspace(request, monkeypatch):
    """Each task gets its own git worktree (agent/workspaces.py), made with
    real git against the project's live repo. Most unit tests drive the work
    and ship nodes against a fake project whose paths are not repositories,
    with git mocked out -- the one-workspace-per-project model they were
    written for. For them the task's workspace IS the project's, which is
    exactly the behaviour they pin.

    Tests of the workspaces themselves -- and anything else that wants real
    worktrees -- opt out with @pytest.mark.real_workspaces.
    """
    if request.node.get_closest_marker("real_workspaces"):
        return
    from agent import workspaces as _ws

    async def _ensure(repo, task_id):
        cfg = _ws._projects().get(repo) or {}
        try:
            from agent.nodes import verify_and_ship as _vs
            cfg = (_vs.PROJECTS.get(repo) or cfg)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "path": cfg.get("sandbox", ""), "created": False}

    async def _remove(repo, task_id):
        return {"ok": True, "removed": False}

    async def _refresh_generated(root, rules):
        return []

    monkeypatch.setattr(_ws, "ensure", _ensure)
    monkeypatch.setattr(_ws, "remove", _remove)
    # Asks the live reviewer for its rules and runs the sandbox; not here.
    monkeypatch.setattr(_ws, "refresh_generated", _refresh_generated)


@pytest.fixture(autouse=True)
def _retrieval_log_in_tmp(monkeypatch, tmp_path):
    """The memory-index and recall telemetry (agent/episode_recall.py)
    defaults to the live log. Tests that build a prompt with the memory
    index were writing into it: two bursts of offers and reads with one
    timestamp on 2026-09-29, indistinguishable from a task's until read
    closely. Every test writes to its own scratch file instead."""
    from agent import episode_recall
    monkeypatch.setattr(episode_recall, "LOG_PATH", tmp_path / "retrieval_events.jsonl")


@pytest.fixture(autouse=True)
def _tool_events_off_the_live_log(tmp_path, monkeypatch):
    """Every tool result a test drives through the work node is logged by
    agent/tool_events.py. Without this they landed in the production log the
    reliability panel reads: one full run added 66 nameless "t1" rows
    (2026-10-01). A test that wants its own file still sets LOG_PATH itself."""
    from agent import tool_events
    monkeypatch.setattr(tool_events, "LOG_PATH", tmp_path / "tool_events.jsonl")


@pytest.fixture(autouse=True)
def _rate_limiter_in_tmp(tmp_path):
    """The sign-in brake counts in the database (agent/rate_limit.py) and
    refuses with 503 when none is installed. The server installs one at
    startup, which a TestClient without a lifespan never reaches; every test
    gets a fresh SQLite one instead, so no count leaks between tests."""
    from agent import rate_limit
    previous = rate_limit.installed()
    rate_limit.install(rate_limit.SqliteAttempts(tmp_path / "rate_limits.db"))
    yield
    rate_limit.install(previous)
