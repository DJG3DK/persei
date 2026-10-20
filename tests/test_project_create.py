"""Creating a project from nothing: the name rules, the containment the
wizard already enforces applied to a directory this server MAKES, the shape
of the repo it makes, and the endpoint end to end.

Two properties here are worth their own tests because they fail silently:

- the initial commit must exist before the worktree is created, or on git
  2.43 `agent-base` comes up as an ORPHAN branch with no history in common
  with main (test_worktree_of_a_new_repo_shares_history_with_main);
- the recommended answers must be the SAME rule scripts/add_project.py --yes
  applies, or a project created from the dashboard and one added headlessly
  get different configs from the same detection
  (test_recommended_choices_match_add_project_yes).
"""

import base64
import dataclasses
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import cartographer
from agent import config as agent_config
from agent import deploy_keys as dk
from agent import github_repos
from agent import github_settings as gs
from agent import provisioning as prov
from agent.auth import User
from agent.routers import projects as projects_routes

REPO_ROOT = Path(__file__).resolve().parent.parent

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)
_NON_ADMIN = User(id=2, email="dev@example.com", role="user", allowed_repos=["something"],
                  totp_enabled=True, must_change_password=False,
                  auto_approve_commands=False, require_merge_review=True)

_TOKEN = "github_pat_" + "q" * 40


@pytest.fixture(autouse=True)
def _allow_tmp_as_project_root(monkeypatch, tmp_path):
    """Same as tests/test_provisioning.py: fixtures live under tmp_path, so
    allow it explicitly rather than weakening the default root."""
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path / "workspaces"))


@pytest.fixture(autouse=True)
def _no_git_identity(monkeypatch, tmp_path):
    """A fresh host has no user.name/user.email. Every test runs that way so
    the commit's identity fallback is exercised, not the developer's config."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def keys_dir(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    monkeypatch.setattr(dk, "KEYS_DIR", d)
    return d


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


# ---------------------------------------------------------------------------
# name rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["orders", "orders-api", "a", "Shop_v2", "svc.internal", "x" * 64])
def test_valid_names_pass(name):
    assert prov.validate_project_name(name) == name


@pytest.mark.parametrize("name", [
    "", "   ", ".", "..", ".hidden", "-lead", "_lead", "a/b", "a\\b", "../x", "a b",
    "a\x00b", "x" * 65, "über",
])
def test_invalid_names_are_refused(name):
    with pytest.raises(prov.ProvisioningError):
        prov.validate_project_name(name)


def test_a_configured_name_is_refused():
    with pytest.raises(prov.ProvisioningError, match="already configured"):
        prov.validate_project_name("orders", ["orders"])


def test_name_is_stripped():
    assert prov.validate_project_name("  orders ") == "orders"


# ---------------------------------------------------------------------------
# containment: nothing is created on refusal
# ---------------------------------------------------------------------------

def test_parent_outside_the_allowed_roots_is_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(allowed))
    with pytest.raises(prov.PathNotAllowedError, match="outside the configured project roots"):
        prov.create_repository(str(outside), "svc")
    assert not (outside / "svc").exists()


def test_existing_path_is_refused_and_left_alone(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "keep").write_text("mine")
    with pytest.raises(prov.ProvisioningError, match="already exists"):
        prov.create_repository(str(tmp_path), "svc")
    assert (tmp_path / "svc" / "keep").read_text() == "mine"
    assert not (tmp_path / "svc" / ".git").exists()

    # A file, a dangling symlink: still "something we did not create".
    (tmp_path / "f").write_text("")
    with pytest.raises(prov.ProvisioningError, match="already exists"):
        prov.create_repository(str(tmp_path), "f")
    os.symlink(tmp_path / "nowhere", tmp_path / "dangling")
    with pytest.raises(prov.ProvisioningError, match="already exists"):
        prov.create_repository(str(tmp_path), "dangling")
    assert not (tmp_path / "nowhere").exists()


def test_agent_own_repo_is_refused(monkeypatch):
    """The agent's own checkout is an allowed-root child on the live box;
    the same guard the wizard applies keeps a new project out of it."""
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(REPO_ROOT.parent))
    with pytest.raises(prov.PathNotAllowedError, match="own repository"):
        prov.create_repository(str(REPO_ROOT), "brand-new-project-xyz")
    assert not (REPO_ROOT / "brand-new-project-xyz").exists()


def test_a_missing_parent_is_not_created(tmp_path):
    with pytest.raises(prov.ProvisioningError, match="does not exist"):
        prov.create_repository(str(tmp_path / "nope"), "svc")
    assert not (tmp_path / "nope").exists()


def test_relative_parent_is_refused(tmp_path):
    with pytest.raises(prov.ProvisioningError, match="absolute"):
        prov.create_repository("relative/dir", "svc")


def test_parent_defaults_to_the_first_allowed_root(tmp_path):
    live = prov.create_repository(None, "svc")
    assert live == os.path.realpath(tmp_path / "svc")


def test_a_git_failure_removes_only_the_directory_it_made(tmp_path, monkeypatch):
    real_run = prov._run_git

    def failing(args, cwd, timeout=120):
        if args and args[0] == "commit" or "commit" in args:
            return False, "simulated: commit refused"
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(prov, "_run_git", failing)
    with pytest.raises(prov.ProvisioningError, match="commit refused"):
        prov.create_repository(str(tmp_path), "svc")
    assert not (tmp_path / "svc").exists()
    assert tmp_path.is_dir(), "the parent is not ours to remove"


# ---------------------------------------------------------------------------
# the repo it makes
# ---------------------------------------------------------------------------

def test_create_repository_makes_one_commit_on_main(tmp_path):
    live = prov.create_repository(str(tmp_path), "svc", description="Orders service")
    repo = Path(live)
    assert (repo / ".git").is_dir()
    assert (repo / "README.md").read_text() == "# svc\n\nOrders service\n"
    ignored = (repo / ".gitignore").read_text().splitlines()
    assert {".env", "node_modules/", ".venv/", "__pycache__/", "dist/"} <= set(ignored)
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s") == "Initial commit"
    assert _git(repo, "status", "--porcelain") == ""
    # Committed under the fallback identity: no user.* is configured anywhere
    # in this test's environment (see _no_git_identity).
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == "Persei <tektonix@localhost>"
    assert "user.name" not in _git(repo, "config", "--local", "--list"), \
        "the fallback is per-command, never written into the repo"


def test_an_existing_identity_is_not_overridden(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Danny\n\temail = operator@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    live = prov.create_repository(str(tmp_path), "svc")
    assert _git(live, "log", "-1", "--format=%an <%ae>") == "Danny <operator@example.com>"


def test_empty_description_writes_a_bare_heading(tmp_path):
    live = prov.create_repository(str(tmp_path), "svc")
    assert Path(live, "README.md").read_text() == "# svc\n"


def test_worktree_of_a_new_repo_shares_history_with_main(tmp_path):
    """The reason the initial commit is not optional: without it
    `git worktree add -b agent-base` on git 2.43 produces an orphan branch,
    and the first task would have no merge base with main."""
    live = prov.create_repository(str(tmp_path), "svc")
    sandbox = str(tmp_path / "workspaces" / "svc")
    ok, out = prov.create_worktree(live, sandbox)
    assert ok, out
    assert _git(sandbox, "branch", "--show-current") == "agent-base"
    base = _git(live, "merge-base", "main", "agent-base")
    assert base == _git(live, "rev-parse", "main"), "agent-base must start FROM main"
    assert (Path(sandbox) / "README.md").is_file()


# ---------------------------------------------------------------------------
# recommended answers == add_project.py --yes
# ---------------------------------------------------------------------------

@pytest.fixture
def node_repo(tmp_path):
    """tests/test_provisioning.py's node fixture, plus a network-calling test
    script so the flagged-stays-off half of the rule is exercised."""
    repo = tmp_path / "shop-api"
    (repo / "tests").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({
        "name": "shop-api",
        "scripts": {
            "typecheck": "tsc --noEmit",
            "lint": "eslint src",
            "build": "vite build",
            "test": "vitest run",
            "test:smoke": "node tests/smoke.js",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    (repo / ".gitignore").write_text(".env\nconfig/keys.json\ndata/fixtures\nnode_modules\n")
    (repo / ".env").write_text("DATABASE_URL=postgres://localhost/x\n")
    (repo / "config").mkdir()
    (repo / "config" / "keys.json").write_text("{}")
    (repo / "data" / "fixtures").mkdir(parents=True)
    (repo / "data" / "fixtures" / "sample.json").write_text("[]")
    (repo / "tests" / "smoke.js").write_text(
        "await fetch('http://127.0.0.1:9000/admin/reset', {method:'POST'});\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _load_add_project():
    spec = importlib.util.spec_from_file_location("add_project", REPO_ROOT / "scripts" / "add_project.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recommended_choices_encodes_the_yes_rule(node_repo):
    report = prov.detect_project(str(node_repo))
    assert report.risky_scripts and all(not r.enabled for r in report.risky_scripts)
    assert any(not c.enabled for c in report.read_only_mounts)

    choices = prov.recommended_choices(report)
    assert choices["secret_files"] == [c.value for c in report.secret_files if c.enabled]
    assert ".env" in choices["secret_files"]
    assert choices["read_only_mounts"] == [], "fixture mounts are proposed OFF"
    assert [c["name"] for c in choices["checks"]] == [c["name"] for c in report.checks]
    assert "test:smoke" not in json.dumps(choices), "a flagged script never auto-enables"
    assert choices["build_steps"] == report.build_steps
    assert choices["node_modules_dirs"] == report.node_modules_dirs
    assert choices["dependency_dirs"] == report.dependency_dirs
    assert choices["db_env_file"] == report.db_env_file
    # And the answers are a subset of what was proposed, so the endpoint's
    # validate_choices() accepts them unchanged.
    assert prov.validate_choices(report, choices) == choices


def test_an_enabled_flagged_script_uses_the_servers_own_command(node_repo):
    report = prov.detect_project(str(node_repo))
    for r in report.risky_scripts:
        r.enabled = True
    choices = prov.recommended_choices(report)
    names = [c["name"] for c in choices["checks"]]
    assert "test:smoke" in names
    assert prov.validate_choices(report, choices)["checks"] == choices["checks"]


def test_recommended_choices_match_add_project_yes(node_repo, tmp_path, monkeypatch):
    """Drive the real script with --yes and compare the projects.json entry
    it writes against config_from_choices(recommended_choices(report))."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({"projects": {}}) + "\n")
    script = _load_add_project()
    monkeypatch.setattr(script, "_PROJECTS_CONFIG_PATH", projects_file)
    monkeypatch.setattr(script, "PROJECTS", {})
    monkeypatch.setattr(sys, "argv", ["add_project.py", str(node_repo), "--yes",
                                      "--sandbox-root", str(tmp_path / "ws-script")])
    assert script.main() == 0

    written = json.loads(projects_file.read_text())["projects"]["shop-api"]
    report = prov.detect_project(str(node_repo), sandbox_root=str(tmp_path / "ws-script"))
    expected = prov.config_from_choices(report.live, report.sandbox,
                                        prov.recommended_choices(report))
    assert written == expected
    assert written["review"]["secretFiles"] == [".env", "config/keys.json"]
    assert "readOnlyMounts" not in written["review"]
    assert "test:smoke" not in json.dumps(written)


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.data = {}

    async def asearch(self, ns, limit=100):
        return [SimpleNamespace(key=k, value=v) for (n, k), v in self.data.items() if n == ns]

    async def aput(self, ns, key, value):
        self.data[(ns, key)] = dict(value)

    async def aget(self, ns, key):
        v = self.data.get((ns, key))
        return SimpleNamespace(key=key, value=v) if v is not None else None


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """tests/test_onboarding_e2e.py's fixture plus a fake store and a config
    with no GITHUB_TOKEN, so the token path is decided by this test alone."""
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({"projects": {}}) + "\n")
    workspaces = tmp_path / "workspaces"
    store = FakeStore()
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", projects_file)
    monkeypatch.setattr(agent_config, "PROJECTS", {}, raising=False)
    monkeypatch.setattr(srv, "PROJECTS", agent_config.PROJECTS, raising=False)
    monkeypatch.setattr(srv, "config", dataclasses.replace(srv.config, auth_secret_key=key, github_token=None))
    monkeypatch.setattr(srv.app.state, "config", srv.config)
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(workspaces))
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    monkeypatch.setattr(gs, "_cache", gs.normalize(None))

    async def fake_seed_memory(repo, store, content):
        return None

    async def fake_cartographer(config, repo, store, force=False):
        return {"repo": repo, "mapped": True, "files": 2}

    import agent.deep_agent as da
    monkeypatch.setattr(da, "seed_memory", fake_seed_memory)
    monkeypatch.setattr(cartographer, "run_cartographer", fake_cartographer)
    monkeypatch.setattr(srv.app.state, "store", store, raising=False)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    return {"projects_file": projects_file, "workspaces": workspaces, "store": store,
            "root": tmp_path}


def test_create_is_admin_only(wired, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _NON_ADMIN)
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc"})
    assert res.status_code == 403
    assert not (wired["root"] / "svc").exists()


def test_create_provisions_a_local_project(wired):
    client = TestClient(srv.app)
    res = client.post("/api/projects/create",
                      json={"name": "svc", "description": "Orders service", "github": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["name"] == "svc"
    assert body["github"] is None
    live = Path(body["live"])
    assert live == (wired["root"] / "svc").resolve()

    by_step = {s["step"]: s for s in body["steps"]}
    assert [s["step"] for s in body["steps"]] == \
        ["repository", "detect", "worktree", "config", "reload", "memory", "codebase-map"]
    assert all(s["ok"] for s in body["steps"]), body["steps"]
    assert "github" not in by_step

    # The repo is real and the worktree starts from its main.
    assert (live / "README.md").read_text().startswith("# svc\n")
    sandbox = wired["workspaces"] / "svc"
    assert (sandbox / ".git").is_file(), "a worktree's .git is a pointer file"
    assert _git(live, "merge-base", "main", "agent-base") == _git(live, "rev-parse", "main")

    # Live in THIS process, and persisted in the wizard's shape.
    assert "svc" in agent_config.PROJECTS
    assert agent_config.PROJECTS["svc"]["sandbox"] == str(sandbox)
    written = json.loads(wired["projects_file"].read_text())["projects"]["svc"]
    assert written["live"] == str(live) and written["sandbox"] == str(sandbox)
    assert "review" not in written and "deploy" not in written, \
        "an empty repo has nothing to check or build yet"
    assert "svc" in client.get("/api/repos").json()

    # Audited under its own action, with the GitHub outcome.
    rows = [v for (ns, _k), v in wired["store"].data.items() if ns == ("audit",)]
    assert any(r["action"] == "project.create" and r["target"] == "svc"
               and r["detail"] == str(live) and r.get("github") is None for r in rows), rows


def test_create_refuses_a_bad_name_and_creates_nothing(wired):
    client = TestClient(srv.app)
    for bad in ("../x", ".hidden", "a/b", ""):
        res = client.post("/api/projects/create", json={"name": bad})
        assert res.status_code == 400, bad
    assert sorted(p.name for p in wired["root"].iterdir()) == ["gitconfig", "projects.json"]


def test_create_refuses_a_configured_name(wired):
    agent_config.PROJECTS["svc"] = {"live": "/nowhere", "sandbox": "/nowhere"}
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc"})
    assert res.status_code == 400 and "already configured" in res.json()["detail"]
    assert not (wired["root"] / "svc").exists()


def test_create_refuses_a_parent_outside_the_roots(wired, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(allowed))
    res = TestClient(srv.app).post("/api/projects/create",
                                   json={"name": "svc", "parent": str(tmp_path)})
    assert res.status_code == 400 and "outside the configured project roots" in res.json()["detail"]
    assert not (tmp_path / "svc").exists()


def test_github_without_a_token_is_a_400_before_anything_is_created(wired):
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc", "github": True})
    assert res.status_code == 400
    assert "no GitHub token is configured" in res.json()["detail"]
    assert not (wired["root"] / "svc").exists()
    assert "svc" not in agent_config.PROJECTS

    # An unknown stored-token name is the same refusal.
    res = TestClient(srv.app).post("/api/projects/create",
                                   json={"name": "svc", "github": True, "token_name": "nope"})
    assert res.status_code == 400 and "nope" in res.json()["detail"]
    assert not (wired["root"] / "svc").exists()


def _fake_github(monkeypatch, calls: dict):
    """The network and the SSH transport, replaced. Everything else -- the
    repo, the key on disk, core.sshCommand, `git remote add` -- is real."""
    async def fake_create(token, name, description="", org=None):
        calls["create"] = {"token": token, "name": name, "description": description, "org": org}
        return {"full_name": f"octo/{name}", "ssh_url": f"git@github.com:octo/{name}.git",
                "html_url": f"https://github.com/octo/{name}"}

    async def fake_add_key(token, full_name, title, public_key):
        calls["key"] = {"token": token, "full_name": full_name, "title": title, "public_key": public_key}

    def fake_check_remote(project, live, timeout=25):
        calls["ls-remote"] = calls.get("ls-remote", 0) + 1
        return True, "remote reachable; push will authenticate"

    real_run = prov._run_git

    def fake_run_git(args, cwd, timeout=120):
        if args[:1] == ["push"]:
            calls["push"] = list(args)
            return True, "branch 'main' set up to track 'origin/main'."
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(github_repos, "create_private_repo", fake_create)
    monkeypatch.setattr(github_repos, "add_deploy_key", fake_add_key)
    monkeypatch.setattr(dk, "check_remote", fake_check_remote)
    monkeypatch.setattr(prov, "_run_git", fake_run_git)


def test_create_with_github_uses_a_named_token_and_pushes_over_ssh(wired, monkeypatch, keys_dir):
    calls: dict = {}
    _fake_github(monkeypatch, calls)
    import asyncio
    asyncio.run(gs.save(wired["store"], srv.config, {"add_tokens": {"main": _TOKEN}}))

    client = TestClient(srv.app)
    res = client.post("/api/projects/create", json={
        "name": "svc", "description": "Orders", "github": True, "token_name": "main"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["github"] == {"full_name": "octo/svc", "html_url": "https://github.com/octo/svc"}
    by_step = {s["step"]: s for s in body["steps"]}
    assert by_step["github"]["ok"], by_step["github"]
    assert by_step["github-token"]["ok"], by_step["github-token"]
    assert by_step["worktree"]["ok"] and by_step["config"]["ok"]

    # The token reached GitHub and nowhere else.
    assert calls["create"]["token"] == _TOKEN and calls["key"]["token"] == _TOKEN
    assert _TOKEN not in res.text
    assert _TOKEN[-8:] not in res.text
    assert calls["create"]["description"] == "Orders"

    # A per-repo deploy key, registered with write access, scoped to this repo.
    live = body["live"]
    assert (keys_dir / "svc.key").is_file()
    assert calls["key"]["full_name"] == "octo/svc"
    assert calls["key"]["public_key"].startswith("ssh-ed25519 ")
    assert str(keys_dir / "svc.key") in _git(live, "config", "--get", "core.sshCommand")

    # SSH origin -- never HTTPS -- and the push went to it.
    assert _git(live, "config", "--local", "--get", "remote.origin.url") == "git@github.com:octo/svc.git"
    assert calls["push"] == ["push", "-u", "origin", "main"]
    assert calls["ls-remote"] == 1
    st = dk.status("svc", live)
    assert st.remote_kind == "ssh" and st.configured and st.installed

    # Later token_for(name) resolves to the same token that created the repo.
    assert gs.token_for(gs.current(), srv.config, "svc") == _TOKEN
    assert gs.current()["projects"]["svc"]["token"] == "main"

    rows = [v for (ns, _k), v in wired["store"].data.items() if ns == ("audit",)]
    assert any(r["action"] == "project.create" and r.get("github") == "octo/svc" for r in rows)


def test_create_with_github_falls_back_to_the_env_token(wired, monkeypatch):
    calls: dict = {}
    _fake_github(monkeypatch, calls)
    monkeypatch.setattr(srv, "config", dataclasses.replace(srv.config, github_token=_TOKEN))
    monkeypatch.setattr(srv.app.state, "config", srv.config)
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc", "github": True})
    assert res.status_code == 200, res.text
    by_step = {s["step"]: s for s in res.json()["steps"]}
    assert by_step["github"]["ok"]
    assert "github-token" not in by_step, "nothing to persist for the env fallback"
    assert calls["create"]["token"] == _TOKEN
    assert _TOKEN not in res.text


def test_a_github_failure_is_a_failed_step_not_an_abort(wired, monkeypatch):
    """The repo on disk is real either way; the operator connects it later
    from the deploy-key panel. Nothing about the local provisioning may
    depend on GitHub answering."""
    async def refused(token, name, description="", org=None):
        raise PermissionError("GitHub refused /user/repos (403): Resource not accessible by personal access token")

    monkeypatch.setattr(github_repos, "create_private_repo", refused)
    monkeypatch.setattr(srv, "config", dataclasses.replace(srv.config, github_token=_TOKEN))
    monkeypatch.setattr(srv.app.state, "config", srv.config)
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc", "github": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["github"] is None
    by_step = {s["step"]: s for s in body["steps"]}
    assert by_step["github"]["ok"] is False
    assert "403" in by_step["github"]["detail"]
    assert by_step["worktree"]["ok"] and by_step["config"]["ok"]
    assert "svc" in agent_config.PROJECTS
    assert _TOKEN not in res.text
    assert _git(body["live"], "remote") == "", "no origin was written for a repo that was not created"


def test_a_repo_name_taken_on_github_is_reported_plainly(wired, monkeypatch):
    async def taken(token, name, description="", org=None):
        raise ValueError(f"a repository named {name!r} already exists on this account")

    monkeypatch.setattr(github_repos, "create_private_repo", taken)
    monkeypatch.setattr(srv, "config", dataclasses.replace(srv.config, github_token=_TOKEN))
    monkeypatch.setattr(srv.app.state, "config", srv.config)
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc", "github": True})
    by_step = {s["step"]: s for s in res.json()["steps"]}
    assert by_step["github"]["ok"] is False and "already exists" in by_step["github"]["detail"]


# ---------------------------------------------------------------------------
# github_repos: the client and the git side
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _capture_post(monkeypatch, responses: list):
    seen = []

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            seen.append({"url": url, "headers": headers, "json": json})
            return responses.pop(0)

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", FakeClient)
    return seen


def test_create_private_repo_posts_a_private_uninitialised_repo(monkeypatch):
    import asyncio
    seen = _capture_post(monkeypatch, [_Resp(201, {
        "full_name": "octo/svc", "ssh_url": "git@github.com:octo/svc.git",
        "html_url": "https://github.com/octo/svc"})])
    out = asyncio.run(github_repos.create_private_repo(_TOKEN, "svc", "Orders"))
    assert out == {"full_name": "octo/svc", "ssh_url": "git@github.com:octo/svc.git",
                   "html_url": "https://github.com/octo/svc"}
    assert seen[0]["url"] == "https://api.github.com/user/repos"
    assert seen[0]["json"] == {"name": "svc", "description": "Orders", "private": True, "auto_init": False}
    assert seen[0]["headers"]["Authorization"] == f"Bearer {_TOKEN}"

    seen = _capture_post(monkeypatch, [_Resp(201, {"full_name": "acme/svc", "html_url": "https://github.com/acme/svc"})])
    out = asyncio.run(github_repos.create_private_repo(_TOKEN, "svc", org="acme"))
    assert seen[0]["url"] == "https://api.github.com/orgs/acme/repos"
    assert out["ssh_url"] == "git@github.com:acme/svc.git", "always the SSH form"


def test_create_private_repo_maps_errors_without_echoing_the_body(monkeypatch):
    import asyncio
    _capture_post(monkeypatch, [_Resp(422, {
        "message": "Repository creation failed.",
        "errors": [{"resource": "Repository", "code": "custom", "field": "name",
                    "message": "name already exists on this account"}],
        "documentation_url": f"https://example.invalid/?t={_TOKEN}"})])
    with pytest.raises(ValueError, match="already exists on this account"):
        asyncio.run(github_repos.create_private_repo(_TOKEN, "svc"))

    _capture_post(monkeypatch, [_Resp(403, {"message": "Resource not accessible by personal access token"})])
    with pytest.raises(PermissionError, match="403") as e:
        asyncio.run(github_repos.create_private_repo(_TOKEN, "svc"))
    assert _TOKEN not in str(e.value)

    _capture_post(monkeypatch, [_Resp(404, {"message": "Not Found"})])
    with pytest.raises(LookupError):
        asyncio.run(github_repos.add_deploy_key(_TOKEN, "octo/svc", "t", "ssh-ed25519 AAAA"))


def test_add_deploy_key_registers_a_writable_key(monkeypatch):
    import asyncio
    seen = _capture_post(monkeypatch, [_Resp(201, {"id": 1})])
    asyncio.run(github_repos.add_deploy_key(_TOKEN, "octo/svc", "tektonix-svc", "ssh-ed25519 AAAA c\n"))
    assert seen[0]["url"] == "https://api.github.com/repos/octo/svc/keys"
    assert seen[0]["json"] == {"title": "tektonix-svc", "key": "ssh-ed25519 AAAA c", "read_only": False}


def test_connect_origin_sets_the_remote_then_mints_a_scoped_key(tmp_path, keys_dir):
    """Remote first: deploy_keys.status() reports no key at all on a repo
    without an origin, so the other order returns nothing to register."""
    live = prov.create_repository(str(tmp_path), "svc")
    public_key = github_repos.connect_origin(live, "git@github.com:octo/svc.git", "svc")
    assert public_key.startswith("ssh-ed25519 ")
    assert _git(live, "config", "--local", "--get", "remote.origin.url") == "git@github.com:octo/svc.git"
    assert (keys_dir / "svc.key").is_file()
    assert str(keys_dir / "svc.key") in _git(live, "config", "--get", "core.sshCommand")
    st = dk.status("svc", live)
    assert st.remote_kind == "ssh" and st.installed and st.configured
    assert st.public_key == public_key


def test_connect_origin_refuses_an_https_origin(tmp_path, keys_dir):
    live = prov.create_repository(str(tmp_path), "svc")
    with pytest.raises(dk.DeployKeyError, match="non-SSH"):
        github_repos.connect_origin(live, "https://github.com/octo/svc.git", "svc")
    assert _git(live, "remote") == ""
    assert not (keys_dir / "svc.key").exists()


def test_push_initial_retries_ls_remote_then_pushes(tmp_path, monkeypatch):
    live = prov.create_repository(str(tmp_path), "svc")
    github_repos.connect_origin(live, "git@github.com:octo/svc.git", "svc")
    probes = []

    def flaky_check(project, live_path, timeout=25):
        probes.append(project)
        return (len(probes) >= 3), "not yet" if len(probes) < 3 else "remote reachable"

    pushes = []
    real_run = prov._run_git

    def fake_run(args, cwd, timeout=120):
        if args[:1] == ["push"]:
            pushes.append(args)
            return True, "done"
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(dk, "check_remote", flaky_check)
    monkeypatch.setattr(prov, "_run_git", fake_run)
    ok, detail = github_repos.push_initial(live, "svc", attempts=5, backoff_s=0)
    assert ok, detail
    assert "git@github.com:octo/svc.git" in detail
    assert len(probes) == 3 and pushes == [["push", "-u", "origin", "main"]]


def test_push_initial_gives_up_after_the_attempts(tmp_path, monkeypatch):
    live = prov.create_repository(str(tmp_path), "svc")
    github_repos.connect_origin(live, "git@github.com:octo/svc.git", "svc")
    monkeypatch.setattr(dk, "check_remote",
                        lambda project, live_path, timeout=25: (False, "Permission denied (publickey)"))
    ok, detail = github_repos.push_initial(live, "svc", attempts=2, backoff_s=0)
    assert not ok and "2 tries" in detail and "publickey" in detail


# ---------------------------------------------------------------------------
# scripts/new_project.py: the headless twin
# ---------------------------------------------------------------------------

def _load_new_project():
    spec = importlib.util.spec_from_file_location("new_project", REPO_ROOT / "scripts" / "new_project.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_new_project_script_creates_and_registers(tmp_path, monkeypatch, capsys):
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({"projects": {}}) + "\n")
    script = _load_new_project()
    monkeypatch.setattr(script, "_PROJECTS_CONFIG_PATH", projects_file)
    monkeypatch.setattr(script, "PROJECTS", {})
    monkeypatch.setattr(sys, "argv", ["new_project.py", "svc", "--parent", str(tmp_path),
                                      "--description", "Orders"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "repository  ok" in out and "worktree    ok" in out and "config      ok" in out

    written = json.loads(projects_file.read_text())["projects"]["svc"]
    live = Path(written["live"])
    assert live == (tmp_path / "svc").resolve()
    assert (live / "README.md").read_text() == "# svc\n\nOrders\n"
    assert (Path(written["sandbox"]) / ".git").is_file()
    assert _git(live, "merge-base", "main", "agent-base") == _git(live, "rev-parse", "main")


def test_new_project_script_refuses_github_without_a_token(tmp_path, monkeypatch, capsys):
    script = _load_new_project()
    monkeypatch.setattr(script, "PROJECTS", {})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["new_project.py", "svc", "--parent", str(tmp_path), "--github"])
    assert script.main() == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "svc").exists()


def test_new_project_script_fails_on_a_bad_name(tmp_path, monkeypatch):
    script = _load_new_project()
    monkeypatch.setattr(script, "PROJECTS", {})
    monkeypatch.setattr(sys, "argv", ["new_project.py", "../x", "--parent", str(tmp_path)])
    assert script.main() == 2


# --------------------------------------------------------------------------
# Regressions from the adversarial review of 2026-09-17. Each one is a way the
# create path could fail AFTER its happy path was already green.


def test_a_stored_token_is_used_when_no_env_token_is_set(wired, monkeypatch):
    """The dashboard offers the GitHub checkbox when Settings holds a token OR
    GITHUB_TOKEN is set, but the server used to accept only the env one -- so
    on the normal shape (a token saved in Settings, no env var) the checkbox
    was offered and the request died with 'no GitHub token is configured'."""
    monkeypatch.setattr(gs, "_cache",
                        gs.apply_patch(srv.config, gs.current(), {"add_tokens": {"main": _TOKEN}}))
    token, name = projects_routes._resolve_github_token(srv.config, None)
    assert token == _TOKEN
    assert name == "main", "the project must be bound to the token that created it"


def test_several_stored_tokens_and_no_choice_is_refused_by_name(wired, monkeypatch):
    """Picking one at random would bind the project to an arbitrary account."""
    monkeypatch.setattr(gs, "_cache", gs.apply_patch(
        srv.config, gs.current(), {"add_tokens": {"main": _TOKEN, "other": _TOKEN}}))
    with pytest.raises(srv.HTTPException) as e:
        projects_routes._resolve_github_token(srv.config, None)
    assert e.value.status_code == 400
    assert "main" in e.value.detail and "other" in e.value.detail


def test_env_token_still_wins_when_no_single_stored_token_applies(wired, monkeypatch):
    monkeypatch.setattr(srv, "config", dataclasses.replace(srv.config, github_token=_TOKEN))
    monkeypatch.setattr(srv.app.state, "config", srv.config)
    monkeypatch.setattr(gs, "_cache", gs.apply_patch(
        srv.config, gs.current(), {"add_tokens": {"main": "x" * 20, "other": "y" * 20}}))
    token, name = projects_routes._resolve_github_token(srv.config, None)
    assert token == _TOKEN
    assert name is None, "an env token is nobody's stored token"


def test_a_failed_step_removes_the_directory_so_a_retry_is_a_retry(wired, monkeypatch):
    """The dashboard keeps the form up after a failure and invites another go.
    create_repository only cleaned up after its OWN git failure, so a project
    that died at detect/worktree/config left the directory behind and every
    retry answered 'already exists' -- the offer could never be taken."""
    real_detect = prov.detect_project

    def boom(*a, **kw):
        raise prov.ProvisioningError("detection exploded")

    monkeypatch.setattr(prov, "detect_project", boom)
    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert not (wired["root"] / "svc").exists(), "the directory this call made must be gone"
    rollback = {s["step"]: s for s in body["steps"]}["rollback"]
    assert rollback["ok"] is True and "svc" in rollback["detail"]

    # ...and the proof that it is a retry: the same request now succeeds.
    # (setattr, not monkeypatch.undo(): undo would also drop the fixture's
    # auth override and answer 401 for reasons that have nothing to do with
    # what this test is about.)
    monkeypatch.setattr(prov, "detect_project", real_detect)
    again = TestClient(srv.app).post("/api/projects/create", json={"name": "svc"})
    assert again.status_code == 200 and again.json()["ok"] is True


def test_a_created_github_repo_is_never_silently_discarded(wired, monkeypatch):
    """Rollback removes a local directory; it must not throw away a repo on
    someone's GitHub account, nor the deploy-key config that is its only
    record locally."""
    async def fake_create(token, name, description="", org=None):
        return {"full_name": f"me/{name}", "ssh_url": f"git@github.com:me/{name}.git",
                "html_url": f"https://github.com/me/{name}"}

    async def fake_add_key(token, full_name, title, public_key):
        return None

    monkeypatch.setattr(github_repos, "create_private_repo", fake_create)
    monkeypatch.setattr(github_repos, "add_deploy_key", fake_add_key)
    monkeypatch.setattr(github_repos, "connect_origin", lambda live, url, name: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(github_repos, "push_initial", lambda live, name, **kw: (True, "pushed"))
    monkeypatch.setattr(prov, "detect_project",
                        lambda *a, **kw: (_ for _ in ()).throw(prov.ProvisioningError("nope")))
    monkeypatch.setattr(gs, "_cache",
                        gs.apply_patch(srv.config, gs.current(), {"add_tokens": {"main": _TOKEN}}))

    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc", "github": True})
    body = res.json()
    assert body["ok"] is False
    assert (wired["root"] / "svc").exists(), "a repo exists on GitHub; the checkout stays"
    rollback = {s["step"]: s for s in body["steps"]}["rollback"]
    assert "me/svc" in rollback["detail"]


def test_a_leftover_workspace_is_refused_rather_than_adopted(wired):
    """create_worktree adopts an existing workspace, which is right when the
    wizard re-onboards a repo it already knows. A project being created has
    never run: a directory at its workspace path belongs to something else
    with the same name, and adopting it would run every task for the new
    project against a DIFFERENT repository."""
    stale = wired["workspaces"] / "svc"
    stale.mkdir(parents=True)
    (stale / ".git").write_text("gitdir: /somewhere/else/.git\n")

    res = TestClient(srv.app).post("/api/projects/create", json={"name": "svc"})
    body = res.json()
    assert body["ok"] is False
    worktree = {s["step"]: s for s in body["steps"]}["worktree"]
    assert worktree["ok"] is False
    assert "already exists" in worktree["detail"]
    assert "svc" not in agent_config.PROJECTS, "a project must not be registered on a stale workspace"


def test_the_wizard_still_adopts_its_own_existing_workspace(tmp_path):
    """The other half of the same rule: re-onboarding an existing repo finds
    its own worktree already there and must go on using it."""
    live = tmp_path / "repo"
    live.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=live, check=True)
    (live / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=live, check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "c"],
                   cwd=live, check=True)
    sandbox = tmp_path / "ws" / "repo"

    ok, _ = prov.create_worktree(str(live), str(sandbox))
    assert ok
    ok_again, detail = prov.create_worktree(str(live), str(sandbox))
    assert ok_again and "already exists" in detail
    refused, why = prov.create_worktree(str(live), str(sandbox), must_be_new=True)
    assert refused is False and "cannot reuse a workspace" in why
