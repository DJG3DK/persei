"""agent/tasks.py: task creation, out of server.py and usable without it.

The extraction every remaining router seam was waiting on (docs/todo.md,
"Split agent/server.py"). These pin what makes it a real seam rather than a
move: the module does not import the server, the server's names for these are
the same objects, and a started task reaches the graph stream through
app.state with the state it should have.
"""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import agent.server as srv
from agent import live_state, tasks
from agent.classify import TaskClassification


import pytest


@pytest.mark.parametrize("module", ["agent.tasks", "agent.task_runtime",
                                    "agent.routers.tasks", "agent.routers.planning"])
def test_the_seam_modules_do_not_import_the_server(module):
    """If one did, a router importing it would be the import cycle the seams
    exist to avoid."""
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print('agent.server' in sys.modules)"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_the_servers_run_state_names_are_task_runtimes_objects():
    from agent import task_runtime
    assert srv._publish is task_runtime.publish
    assert srv._live_task_log is task_runtime.live_task_log
    assert srv._task_event_seq is task_runtime.task_event_seq
    assert srv._claim_run_slot is task_runtime.claim_run_slot


def test_the_servers_names_are_the_same_objects():
    assert srv.write_task_meta is tasks.write_task_meta
    assert srv._attachments_note is tasks.attachments_note
    assert srv.app.state.stream_graph is srv._stream_graph


def test_a_started_task_reaches_the_stream_with_its_initial_state(monkeypatch):
    seen = {}

    async def stream(task_id, repo, goal, budget, state, **kw):
        seen.update(task_id=task_id, repo=repo, goal=goal, budget=budget, state=state, **kw)

    async def classify(goal, config):
        return TaskClassification(category="bug-fix", needs_tests=False)

    class _Store:
        async def aget(self, ns, key):
            return None

        async def aput(self, ns, key, value):
            seen["meta"] = value

    app = SimpleNamespace(state=SimpleNamespace(stream_graph=stream, config=object(), store=_Store()))
    monkeypatch.setattr(tasks, "classify_task", classify)

    async def go():
        out = await tasks.start_task(
            app, "  fix the login bug  ", "proj", 3.0, "auto",
            auto_approve_commands=False, require_merge_review=True,
            attachments=[{"kind": "image", "path": ".uploads/ab/shot.png"}], origin="github")
        await live_state.running_tasks.pop(out["task_id"])
        return out

    out = asyncio.run(go())
    assert out["category"] == "bug-fix"
    assert seen["task_id"] == out["task_id"] and seen["repo"] == "proj" and seen["budget"] == 3.0
    assert seen["state"]["goal"].startswith("fix the login bug")
    assert "shot.png" in seen["state"]["goal"]              # the attachments note reached the model
    assert seen["state"]["require_merge_review"] is True
    assert seen["meta"]["origin"] == "github"


def test_an_empty_goal_is_refused_before_anything_starts():
    import pytest
    from fastapi import HTTPException

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as e:
        asyncio.run(tasks.start_task(app, "   ", "proj", None, "auto",
                                     auto_approve_commands=False, require_merge_review=True))
    assert e.value.status_code == 422



class _MetaStore:
    def __init__(self):
        self.rows = {}

    async def aget(self, ns, key):
        v = self.rows.get((ns, key))
        return SimpleNamespace(key=key, value=v, updated_at=None) if v is not None else None

    async def aput(self, ns, key, value):
        self.rows[(ns, key)] = dict(value)


def test_finished_at_is_stamped_once_as_a_task_becomes_done(monkeypatch):
    """B40 (2026-10-22): the inbox timed its settle wait from the record's
    updated_at, which any later write moves."""
    store, t = _MetaStore(), [1000.0]
    monkeypatch.setattr(tasks.time, "time", lambda: t[0])

    async def go():
        await tasks.write_task_meta(store, "proj", "t1", status="running")
        assert "finished_at" not in store.rows[(("tasks", "proj"), "t1")]
        t[0] = 2000.0
        await tasks.write_task_meta(store, "proj", "t1", status="done")
        t[0] = 3000.0
        await tasks.write_task_meta(store, "proj", "t1", status="done", cost_so_far=1.0)
        await tasks.write_task_meta(store, "proj", "t1", cost_so_far=2.0)
        assert store.rows[(("tasks", "proj"), "t1")]["finished_at"] == 2000.0
        await tasks.write_task_meta(store, "proj", "t1", status="running")
        assert "finished_at" not in store.rows[(("tasks", "proj"), "t1")], "a resume is not finished"
    asyncio.run(go())


def test_the_inbox_reads_when_a_task_finished_not_when_it_was_last_written(monkeypatch):
    from datetime import UTC, datetime

    late = datetime.fromtimestamp(9000.0, tz=UTC)

    async def tasks_in(repo, statuses):
        return [SimpleNamespace(key="t1", value={"task_id": "t1", "finished_at": 2000.0}, updated_at=late),
                SimpleNamespace(key="t0", value={"task_id": "t0"}, updated_at=late)]

    monkeypatch.setattr(srv, "_tasks_in", tasks_in)
    assert asyncio.run(srv._github_finished_tasks("proj")) == {"t1": 2000.0, "t0": 9000.0}
