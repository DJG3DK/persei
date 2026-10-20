"""Removing a project, and getting it back.

The property every test here exists to protect: **the live repository is not
touched**. This control sits in a list of the operator's own repositories, one
row apart from the next one, and it is the only place in the product where a
wrong click could plausibly be read as "delete my code". It does not delete
their code, and these tests are what keeps that true.

The rest is the archive contract. `archive` has to be complete (a namespace
added later and forgotten here would be silently dropped on the next removal)
and it has to be restorable, because an archive nothing can read back is a
promise the Remove dialog makes and the product does not keep.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent import project_removal as pr


# ---------------------------------------------------------------------------
# a fake store with the two methods the module uses
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.data: dict[tuple, dict] = {}

    async def aput(self, ns, key, value):
        self.data.setdefault(tuple(ns), {})[key] = value

    async def asearch(self, ns, limit=100):
        items = self.data.get(tuple(ns), {})
        return [type("Item", (), {"key": k, "value": v})() for k, v in list(items.items())[:limit]]

    async def adelete(self, ns, key):
        self.data.get(tuple(ns), {}).pop(key, None)


@pytest.fixture
def store():
    s = FakeStore()
    # One row in every namespace a project owns, so an omission shows up.
    for label, ns in pr.namespaces("demo").items():
        s.data[ns] = {f"{label}-key": {"label": label}}
    # ...and a second project that must survive every operation below.
    for ns in pr.namespaces("keeper").values():
        s.data[ns] = {"k": {"label": "keeper"}}
    return s


@pytest.fixture
def archives(tmp_path, monkeypatch):
    d = tmp_path / "archives"
    monkeypatch.setattr(pr, "ARCHIVE_DIR", d)
    return d


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "app.py").write_text("print('the operator's code')\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "work"],
                   cwd=path, check=True)


# ---------------------------------------------------------------------------
# the promise
# ---------------------------------------------------------------------------

def test_removing_the_workspace_leaves_the_live_repo_and_its_history(tmp_path):
    """The whole feature in one test. Remove takes the agent's workspace; the
    operator's repository, its files, its commits and its branches stay."""
    live = tmp_path / "myproject"
    _git_repo(live)
    subprocess.run(["git", "branch", "agent/some-task"], cwd=live, check=True)
    sandbox = tmp_path / "workspaces" / "myproject"
    subprocess.run(["git", "worktree", "add", "-q", str(sandbox), "-b", "agent-base"],
                   cwd=live, check=True)
    assert sandbox.is_dir()

    ok, detail = pr.remove_worktree(str(live), str(sandbox))

    assert ok, detail
    assert not sandbox.exists(), "the agent's workspace should be gone"
    assert (live / "app.py").read_text().startswith("print("), "the operator's file was touched"
    log = subprocess.run(["git", "log", "--oneline"], cwd=live, capture_output=True, text=True)
    assert "work" in log.stdout, "the operator's history was touched"
    branches = subprocess.run(["git", "branch"], cwd=live, capture_output=True, text=True).stdout
    assert "agent/some-task" in branches, "a branch the agent had pushed was deleted"
    # git must no longer think the worktree exists, or the next add at that
    # path fails with "already registered".
    wt = subprocess.run(["git", "worktree", "list"], cwd=live, capture_output=True, text=True).stdout
    assert str(sandbox) not in wt


def test_a_workspace_that_is_already_gone_is_not_an_error(tmp_path):
    live = tmp_path / "p"
    _git_repo(live)
    ok, detail = pr.remove_worktree(str(live), str(tmp_path / "nothing-here"))
    assert ok and "no workspace" in detail


def test_the_workspace_still_goes_when_the_live_repo_has_been_moved_away(tmp_path):
    """An operator who has already deleted or moved their checkout must still
    be able to get the agent's leftovers off the disk."""
    sandbox = tmp_path / "orphan"
    sandbox.mkdir()
    (sandbox / "f").write_text("x")
    ok, _ = pr.remove_worktree(str(tmp_path / "gone"), str(sandbox))
    assert ok and not sandbox.exists()


# ---------------------------------------------------------------------------
# archive and restore
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_archive_covers_every_namespace_a_project_owns(store, archives):
    doc = await pr.collect(store, "demo")
    assert set(doc["namespaces"]) == set(pr.namespaces("demo")), \
        "a namespace was added to the project without being added to the archive"
    assert doc["item_count"] == len(pr.namespaces("demo"))
    for label, rows in doc["namespaces"].items():
        assert rows and rows[0]["value"]["label"] == label


@pytest.mark.asyncio
async def test_archive_then_purge_then_restore_is_a_round_trip(store, archives):
    doc = await pr.collect(store, "demo")
    path = pr.write_archive(doc)
    removed = await pr.purge(store, "demo")

    assert removed == len(pr.namespaces("demo"))
    assert all(not store.data.get(ns) for ns in pr.namespaces("demo").values()), "purge missed a namespace"

    written = await pr.restore(store, "demo", pr.read_archive(path.name))
    assert written == removed
    after = await pr.collect(store, "demo")
    assert after["namespaces"] == doc["namespaces"], "what came back is not what went in"


@pytest.mark.asyncio
async def test_purge_touches_only_the_project_named(store, archives):
    await pr.purge(store, "demo")
    for ns in pr.namespaces("keeper").values():
        assert store.data[ns], f"{ns} belonged to another project and was cleared"


@pytest.mark.asyncio
async def test_an_archive_restores_under_the_new_name_not_the_old_one(store, archives):
    """Re-adding a project at a different name still carries its memory, and a
    hand-edited archive cannot write into a namespace nobody asked for."""
    doc = await pr.collect(store, "demo")
    doc["project"] = "somewhere-else"
    await pr.restore(store, "renamed", doc)
    assert store.data[("renamed",)], "nothing landed under the requested name"
    assert not store.data.get(("somewhere-else",)), "the archive chose its own namespace"


@pytest.mark.asyncio
async def test_the_archive_file_is_written_atomically(store, archives):
    doc = await pr.collect(store, "demo")
    pr.write_archive(doc)
    assert not list(archives.glob("*.tmp")), "a temp file was left behind"
    assert len(list(archives.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_archives_are_listed_newest_first_and_filtered_by_project(store, archives):
    a = pr.write_archive(await pr.collect(store, "demo"), archives / "demo-20260101T000000Z.json")
    b = pr.write_archive(await pr.collect(store, "demo"), archives / "demo-20260202T000000Z.json")
    other = await pr.collect(store, "keeper")
    pr.write_archive(other, archives / "keeper-20260303T000000Z.json")

    names = [x["file"] for x in pr.list_archives()]
    assert names.index(b.name) < names.index(a.name), "older archive listed first"
    assert [x["file"] for x in pr.list_archives("keeper")] == ["keeper-20260303T000000Z.json"]


@pytest.mark.parametrize("bad", ["../secrets.json", "a/b.json", "..", "/etc/passwd"])
def test_an_archive_name_cannot_walk_out_of_the_archive_directory(archives, bad):
    archives.mkdir(parents=True, exist_ok=True)
    with pytest.raises(pr.RemovalError):
        pr.read_archive(bad)
    with pytest.raises(pr.RemovalError):
        pr.delete_archive(bad)


# ---------------------------------------------------------------------------
# the config files
# ---------------------------------------------------------------------------

def test_removing_the_entry_leaves_every_other_project_intact(tmp_path):
    f = tmp_path / "projects.json"
    f.write_text(json.dumps({
        "projects": {"a": {"live": "/a", "sandbox": "/wa"}, "b": {"live": "/b", "sandbox": "/wb"}},
        "test_env": {"a": {"K": "v"}, "b": {"K": "v"}},
    }))
    assert pr.remove_project_entry(f, "a") is True
    d = json.loads(f.read_text())
    assert list(d["projects"]) == ["b"]
    assert list(d["test_env"]) == ["b"], "the project's test_env block was left behind"
    assert not list(tmp_path.glob("*.tmp"))
    assert pr.remove_project_entry(f, "a") is False, "a second removal should be a no-op"


def test_clearing_the_reviewer_state_leaves_other_projects(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"a": {"verdict": "READY"}, "b": {"verdict": "READY"}}))
    assert pr.clear_reviewer_state(f, "a") is True
    assert list(json.loads(f.read_text())) == ["b"]
    assert pr.clear_reviewer_state(f, "a") is False
    assert pr.clear_reviewer_state(tmp_path / "missing.json", "a") is False


@pytest.mark.parametrize("bad", ["", "a/b", "../x", ".hidden"])
def test_an_invalid_project_name_is_refused_before_any_path_is_built(bad):
    with pytest.raises(pr.RemovalError):
        pr.archive_path(bad)


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------

import agent.server as srv  # noqa: E402
from agent import config as agent_config  # noqa: E402
from agent import history_index  # noqa: E402
from agent import live_state  # noqa: E402
from agent import paths  # noqa: E402
from agent.routers import projects as projects_routes  # noqa: E402
from agent.auth import User  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _HistoryIndex:
    """The three calls the removal path makes of an index."""

    def __init__(self, rows=None, dump_raises=False):
        self.rows = list(rows or [])
        self.dump_raises = dump_raises
        self.forgotten: list[str] = []

    async def dump_project(self, repo):
        if self.dump_raises:
            raise RuntimeError("no database")
        return [r for r in self.rows if r.get("repo") == repo]

    async def forget_project(self, repo):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.get("repo") != repo]
        self.forgotten.append(repo)
        return before - len(self.rows)

    async def restore_project(self, repo, rows):
        self.rows.extend({**r, "repo": repo} for r in rows)
        return len(rows)

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)
_USER = User(id=2, email="dev@example.com", role="user", allowed_repos=["demo"],
             totp_enabled=True, must_change_password=False,
             auto_approve_commands=False, require_merge_review=True)


@pytest.fixture
def wired(tmp_path, monkeypatch, store, archives):
    """One configured project, a real live repo, and a fake store."""
    live = tmp_path / "demo"
    _git_repo(live)
    sandbox = tmp_path / "workspaces" / "demo"
    subprocess.run(["git", "worktree", "add", "-q", str(sandbox), "-b", "agent-base"],
                   cwd=live, check=True)

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps(
        {"projects": {"demo": {"live": str(live), "sandbox": str(sandbox)}}}) + "\n")
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", projects_file)
    projects = {"demo": {"live": str(live), "sandbox": str(sandbox)}}
    monkeypatch.setattr(agent_config, "PROJECTS", projects, raising=False)
    monkeypatch.setattr(srv, "PROJECTS", projects, raising=False)
    def reload():
        # The real one re-reads projects.json and mutates PROJECTS IN PLACE
        # (agent/config.py). A stub that just returned the dict would leave the
        # in-process map holding a project the file no longer has -- and the
        # test would pass while the endpoint did nothing.
        projects.clear()
        projects.update(json.loads(projects_file.read_text())["projects"])
        return projects

    monkeypatch.setattr(agent_config, "reload_projects", reload)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path, raising=False)
    # A server that booted properly HAS a history index -- the lifespan
    # installs one -- and the removal route now refuses to archive-and-purge
    # a Postgres installation without it, because the index rows are the only
    # copy of every already-pruned episode and purge() deletes them. Installed
    # here so the rest of this file exercises the normal path; the refusal has
    # its own test below.
    history_index.install(_HistoryIndex())
    try:
        yield {"live": live, "sandbox": sandbox, "projects_file": projects_file}
    finally:
        history_index.install(None)


def test_remove_is_admin_only(wired, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _USER)
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 403
    assert "demo" in agent_config.PROJECTS, "a non-admin removed a project"


def test_removing_an_unknown_project_is_a_404(wired):
    res = TestClient(srv.app).request("DELETE", "/api/projects/nope", json={"memory": "delete"})
    assert res.status_code == 404


def test_a_project_with_work_in_flight_is_refused(wired, monkeypatch):
    """Pulling the workspace out from under a running task would leave a
    half-finished branch nobody owns."""
    async def busy(app):
        return {"demo"}

    monkeypatch.setattr(projects_routes, "_running_repos", busy)
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 409
    assert "in flight" in res.json()["detail"]
    assert "demo" in agent_config.PROJECTS
    assert wired["sandbox"].is_dir(), "the workspace was removed despite the refusal"


def test_a_project_with_a_planning_turn_open_is_refused(wired, monkeypatch):
    """The real _running_repos, not the stand-in the test above patches in.
    find_planning_meta returns a (repo, meta) tuple; reading it as a dict
    raised AttributeError and made every removal and move a 500 whenever
    any planning turn was open, for every project (2026-09-29)."""
    import asyncio

    async def planning_meta(session_id):
        return ("demo", {"repo": "demo", "id": session_id})

    monkeypatch.setattr(srv.app.state, "find_planning_meta", planning_meta, raising=False)
    monkeypatch.setitem(live_state.running_planning_turns, "sess-1", object())
    assert asyncio.run(projects_routes._running_repos(srv.app)) == {"demo"}
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 409, res.text
    assert "in flight" in res.json()["detail"]
    assert "demo" in agent_config.PROJECTS


def test_a_planning_turn_on_another_project_does_not_block_removal(wired, monkeypatch):
    import asyncio

    async def planning_meta(session_id):
        return ("other", {"repo": "other"})

    monkeypatch.setattr(srv.app.state, "find_planning_meta", planning_meta, raising=False)
    monkeypatch.setitem(live_state.running_planning_turns, "sess-2", object())
    assert asyncio.run(projects_routes._running_repos(srv.app)) == {"other"}
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 200, res.text


def test_a_full_removal_archives_and_leaves_the_repository_alone(wired, archives, store):
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["archive"], "archive mode returned no archive name"

    # gone from the agent
    assert "demo" not in agent_config.PROJECTS
    assert "demo" not in json.loads(wired["projects_file"].read_text())["projects"]
    assert not wired["sandbox"].exists()
    assert all(not store.data.get(ns) for ns in pr.namespaces("demo").values())

    # ...and the operator's repository is exactly as it was
    assert (wired["live"] / "app.py").is_file()
    log = subprocess.run(["git", "log", "--oneline"], cwd=wired["live"],
                         capture_output=True, text=True).stdout
    assert "work" in log
    assert body["live_untouched"] == str(wired["live"])

    # the archive is real and restorable
    doc = pr.read_archive(body["archive"])
    assert doc["item_count"] == len(pr.namespaces("demo"))


def test_delete_mode_writes_no_archive(wired, archives, store):
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "delete"})
    assert res.status_code == 200
    assert res.json()["archive"] is None
    assert not list(archives.glob("*.json")) if archives.exists() else True
    assert all(not store.data.get(ns) for ns in pr.namespaces("demo").values())


def test_a_failed_archive_removes_nothing(wired, monkeypatch, store):
    """The operator asked to keep this. Deleting it anyway is the one mistake
    here with no undo, so the whole removal refuses instead."""
    def boom(doc, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(pr, "write_archive", boom)
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 500
    assert "nothing was removed" in res.json()["detail"]
    assert "demo" in agent_config.PROJECTS
    assert wired["sandbox"].is_dir()
    assert store.data[("demo",)], "memory was purged after the archive failed"


# ---------------------------------------------------------------------------
# deleting the checkout too
# ---------------------------------------------------------------------------
#
# The default above is still "the repository is never touched". This is the
# opt-in for the one case where that leaves the operator in a shell: a
# repository Persei cloned by itself and nobody wanted.

def _no_pm2(monkeypatch) -> None:
    """Answer the "what does this box run from it" probe without depending on
    whether the machine running the tests happens to have pm2, or a daemon."""
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "pm2":
            raise FileNotFoundError("pm2")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(pr.subprocess, "run", fake_run)


def _publish(live: Path, tmp_path: Path) -> Path:
    """Give `live` a remote that has every one of its commits."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)],
                   check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=live, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=live, check=True)
    return origin


def test_the_checkout_route_reports_why_it_cannot_go(wired):
    """A refusal is only useful with the because, so the panel is told it
    before the operator picks rather than after they type the name."""
    res = TestClient(srv.app).get("/api/projects/demo/checkout")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["live"] == str(wired["live"])
    assert body["removable"] is False
    assert "only copy" in body["reason"], "this repo has no remote"


def test_the_checkout_route_says_yes_for_a_fully_pushed_clone(wired, tmp_path, monkeypatch):
    _no_pm2(monkeypatch)
    _publish(wired["live"], tmp_path)
    body = TestClient(srv.app).get("/api/projects/demo/checkout").json()
    assert body["removable"] is True


def test_the_checkout_route_is_admin_only(wired, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _USER)
    assert TestClient(srv.app).get("/api/projects/demo/checkout").status_code == 403


def test_deleting_a_checkout_that_is_the_only_copy_is_refused_before_anything_happens(wired):
    """And nothing else is removed either: a refusal that landed after the
    memory was archived and the workspace was gone would be a half-removed
    project and an operator with no idea which half."""
    res = TestClient(srv.app).request(
        "DELETE", "/api/projects/demo", json={"memory": "archive", "files": "delete"})
    assert res.status_code == 409
    assert "only copy" in res.json()["detail"]
    assert wired["live"].is_dir()
    assert wired["sandbox"].is_dir(), "the workspace went despite the refusal"
    assert "demo" in agent_config.PROJECTS


def test_a_fully_pushed_checkout_is_deleted_when_asked(wired, tmp_path, archives, store, monkeypatch):
    _no_pm2(monkeypatch)
    _publish(wired["live"], tmp_path)
    res = TestClient(srv.app).request(
        "DELETE", "/api/projects/demo", json={"memory": "archive", "files": "delete"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert not wired["live"].exists(), "the checkout is still there"
    assert body["live_removed"] == str(wired["live"])
    assert body["live_untouched"] is None
    step = {s["step"]: s for s in body["steps"]}["checkout"]
    assert step["ok"] and "deleted" in step["detail"]


def test_the_default_still_leaves_the_checkout_alone(wired, tmp_path, archives, store):
    """`files` is opt-in. A client that does not send it -- which is every
    client written before this existed -- must keep the old promise."""
    _publish(wired["live"], tmp_path)
    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})
    assert res.status_code == 200, res.text
    assert wired["live"].is_dir()
    assert res.json()["live_untouched"] == str(wired["live"])
    assert res.json()["live_removed"] is None


def test_a_checkout_this_box_serves_is_refused_even_when_fully_pushed(wired, tmp_path, monkeypatch):
    _publish(wired["live"], tmp_path)
    srv.PROJECTS["demo"]["deploy"] = {"pm2Apps": ["demo-api"]}
    res = TestClient(srv.app).request(
        "DELETE", "/api/projects/demo", json={"memory": "archive", "files": "delete"})
    assert res.status_code == 409
    assert "demo-api" in res.json()["detail"]
    assert wired["live"].is_dir()


# ---------------------------------------------------------------------------
# the index is where the only copy of a pruned episode lives
# ---------------------------------------------------------------------------

def test_a_removal_is_refused_when_the_history_index_is_not_open(wired, store):
    """Both halves fail silently without it, and in opposite directions.

    With no index object in this process the archive omits every row -- and
    those rows are the only copy of each episode the pruner already deleted
    -- while the purge leaves every one of the project's rows behind in
    agent_history_fts, which is the project's own goal text surviving its
    own removal. Refusing costs a restart. Continuing costs both.
    """
    history_index.install(None)

    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})

    assert res.status_code == 503
    assert "demo" in agent_config.PROJECTS, "a project was removed with no index open"
    assert store.data[("episodes", "demo")], "memory was purged anyway"
    assert wired["sandbox"].is_dir()


def test_an_archive_that_cannot_read_the_index_removes_nothing(wired, store, monkeypatch):
    """collect() used to swallow this one level down, so the archive was
    written, the step reported ok, and the guard below it never fired."""
    history_index.install(_HistoryIndex(dump_raises=True))

    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "archive"})

    assert res.status_code == 500
    assert "nothing was removed" in res.json()["detail"]
    assert "demo" in agent_config.PROJECTS
    assert store.data[("episodes", "demo")], "memory was purged after the archive failed"


def test_a_removal_takes_the_project_s_index_rows_with_it(wired):
    index = _HistoryIndex([{"repo": "demo", "corpus": "episode", "item_key": "/e.json"},
                           {"repo": "keeper", "corpus": "episode", "item_key": "/e.json"}])
    history_index.install(index)

    res = TestClient(srv.app).request("DELETE", "/api/projects/demo", json={"memory": "delete"})

    assert res.status_code == 200, res.text
    assert index.forgotten == ["demo"]
    assert [r["repo"] for r in index.rows] == ["keeper"]
