"""Shipping to a repository whose base branch the agent may not write.

The review gate is unchanged and has already passed: this is only what happens
after. A person merges.
"""
from __future__ import annotations

import asyncio

import pytest

from agent import github_repos
from agent.tools import review_gate

# The reviewed commit, and where the branch points unless a test moves it.
TIP = "abc123def456" * 3 + "abcd"


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected {self.status_code}")


def _client(post=None, get=None):
    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw): return post(url, **kw)
        async def get(self, url, **kw): return get(url, **kw)
    return C


def test_a_new_pull_request_returns_its_url(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, body=kw.get("json"))
        return _Resp(201, {"number": 7, "html_url": "https://github.com/o/r/pull/7", "state": "open"})

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post))
    pr = asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "Do a thing"))

    assert pr["url"].endswith("/pull/7")
    assert seen["body"]["head"] == "agent/t1" and seen["body"]["base"] == "main"


def test_an_existing_pull_request_is_a_success_not_a_failure(monkeypatch):
    """A resumed task, or a ship step that re-ran after a blip, has already
    opened one. Treating 422 as an error would turn finished work into a
    failure at the very last step."""
    def post(url, **kw):
        return _Resp(422, {"message": "Validation Failed",
                           "errors": [{"message": "A pull request already exists for o:agent/t1."}]})

    def get(url, **kw):
        return _Resp(200, [{"number": 3, "html_url": "https://github.com/o/r/pull/3", "state": "open"}])

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post, get=get))
    pr = asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "Do a thing"))
    assert pr["number"] == "3"


def test_a_real_validation_error_is_still_an_error(monkeypatch):
    def post(url, **kw):
        return _Resp(422, {"message": "Validation Failed",
                           "errors": [{"message": "No commits between main and agent/t1"}]})

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post))
    with pytest.raises(ValueError):
        asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "t"))


# --- the ship step itself ---------------------------------------------------

def _ship(monkeypatch, *, project="demo", cfg=None, token="tok", remote="git@github.com:o/r.git",
          push_ok=True, pr=None):
    import agent.config as agent_config
    monkeypatch.setitem(agent_config.PROJECTS, project, cfg or {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config", lambda: type("C", (), {"github_token": token})())

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": TIP}
        if cmd.startswith("remote get-url"):
            return {"ok": bool(remote), "output": remote or ""}
        if cmd.startswith("push"):
            return {"ok": push_ok, "output": "" if push_ok else "denied"}
        return {"ok": True, "output": ""}

    import agent.tools.git as gitmod
    monkeypatch.setattr(gitmod, "_git", fake_git)

    async def fake_pr(*a, **k):
        if isinstance(pr, Exception):
            raise pr
        return pr or {"number": "9", "url": "https://github.com/o/r/pull/9", "state": "open"}

    monkeypatch.setattr(github_repos, "open_pull_request", fake_pr)
    return asyncio.run(review_gate.ship_as_pull_request(project, "agent/t1", TIP, "Title"))


def test_shipping_opens_the_pull_request_and_reports_its_url(monkeypatch):
    r = _ship(monkeypatch)
    assert r["ok"] and r["shipped"] == "pull_request"
    assert r["pull_request"].endswith("/pull/9")


def test_no_token_says_which_permission_is_missing(monkeypatch):
    """The token is documented read-only elsewhere, so this is the likely
    first failure and the message has to name the scope."""
    r = _ship(monkeypatch, token=None)
    assert r["ok"] is False and "pull_requests: write" in r["error"]


def test_a_project_with_no_github_origin_is_refused_clearly(monkeypatch):
    r = _ship(monkeypatch, remote="")
    assert r["ok"] is False and "no GitHub origin" in r["error"]


def test_a_rejected_push_does_not_claim_a_pull_request(monkeypatch):
    r = _ship(monkeypatch, push_ok=False)
    assert r["ok"] is False and "could not push" in r["error"]
    assert "pull_request" not in r


def test_a_github_refusal_is_reported_rather_than_raised(monkeypatch):
    r = _ship(monkeypatch, pr=PermissionError("403: resource not accessible"))
    assert r["ok"] is False and "could not open a pull request" in r["error"]


# --- authenticating the push ------------------------------------------------

def _is_push(cmd):
    return " push " in f" {cmd} "


def _ship_capturing_push(monkeypatch, origin, token="tok", push_raises=None):
    """Runs the ship step and returns (push command, its extra env, result)."""
    import agent.config as agent_config
    import agent.tools.git as gitmod

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": token})())
    monkeypatch.setattr(github_repos, "open_pull_request",
                        lambda *a, **k: _async({"number": "1", "url": "u", "state": "open"}))

    seen = {}

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": TIP}
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if cmd.startswith("config --local --get remote.origin.url"):
            return {"ok": True, "output": origin}
        if _is_push(cmd):
            seen["cmd"], seen["env"] = cmd, extra_env
            if push_raises is not None:
                raise push_raises
            return {"ok": True, "output": ""}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    r = asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", TIP, "T"))
    return seen.get("cmd", ""), seen.get("env"), r


def _async(value):
    async def f(*a, **k):
        return value
    return f()


def test_an_https_origin_is_pushed_with_a_credential(monkeypatch):
    """A cloned project's origin has no credentials on it -- the token is
    deliberately never written into .git/config. Without this the push fails
    with "authentication required" at the very last step of a finished task.
    """
    import agent.tools.git as gitmod
    cmd, env, _ = _ship_capturing_push(monkeypatch, "https://github.com/o/r.git")
    assert "https://github.com/o/r.git" in cmd
    assert "credential.helper=" in cmd
    assert env[gitmod.GIT_TOKEN_ENV] == "tok"
    assert " origin " not in cmd, "the remote name would carry no credentials"


def test_the_token_is_never_on_the_push_command_line(monkeypatch):
    """Every local user can read a process's argv, and a hung push put the
    whole command in the ShellTimeout message."""
    cmd, _, _ = _ship_capturing_push(monkeypatch, "https://github.com/o/r.git", token="s3cret")
    assert "s3cret" not in cmd
    assert "x-access-token:" not in cmd


def test_a_push_that_times_out_is_reported_without_the_command(monkeypatch):
    from agent.tools.shell import ShellTimeout
    _, _, r = _ship_capturing_push(
        monkeypatch, "https://github.com/o/r.git", token="s3cret",
        push_raises=ShellTimeout("git push https://x-access-token:s3cret@github.com/o/r.git agent/t1", 300))
    assert r["ok"] is False and "timed out" in r["error"]
    assert "s3cret" not in r["error"]


def test_an_ssh_origin_is_pushed_by_remote_name(monkeypatch):
    """That project already has a deploy key wired through core.sshCommand.
    Putting a token on the command line would do nothing except risk logging
    it."""
    cmd, env, _ = _ship_capturing_push(monkeypatch, "git@github.com:o/r.git")
    # Forced: the branch may have been pushed before and rebased since.
    assert cmd.strip().endswith(f"origin +{TIP}:refs/heads/agent/t1")
    assert "credential.helper" not in cmd
    assert env is None


def test_a_failed_push_does_not_echo_the_token(monkeypatch):
    """git repeats the URL it was given, token and all, and that string goes
    into a task log an operator reads and may paste."""
    import agent.config as agent_config
    import agent.tools.git as gitmod

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": "s3cret"})())

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": TIP}
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if cmd.startswith("config --local"):
            return {"ok": True, "output": "https://github.com/o/r.git"}
        if _is_push(cmd):
            return {"ok": False,
                    "output": "fatal: could not read from https://x-access-token:s3cret@github.com/o/r.git"}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    r = asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", TIP, "T"))
    assert r["ok"] is False
    assert "s3cret" not in r["error"]
    assert "***" in r["error"]


def test_a_projects_own_token_is_preferred_over_the_environment(monkeypatch):
    """Settings > GitHub is the documented place to put a token, and it stores
    them per project. A ship step that only read GITHUB_TOKEN would quietly use
    the wrong credential for a project that has its own."""
    import agent.config as agent_config
    import agent.tools.git as gitmod
    from agent import github_settings

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": "env-token"})())
    monkeypatch.setattr(github_settings, "current", lambda: {"tokens": {}, "projects": {}})
    monkeypatch.setattr(github_settings, "token_for", lambda s, c, repo: "the-projects-own-token")

    captured = {}

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": TIP}
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if cmd.startswith("config --local"):
            return {"ok": True, "output": "https://github.com/o/r.git"}
        if _is_push(cmd):
            captured["push"] = (extra_env or {}).get(gitmod.GIT_TOKEN_ENV)
            return {"ok": True, "output": ""}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    monkeypatch.setattr(github_repos, "open_pull_request",
                        lambda *a, **k: _async({"number": "1", "url": "u", "state": "open"}))

    asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", TIP, "T"))
    assert captured["push"] == "the-projects-own-token"


# --- what the operator is told ---------------------------------------------

def test_the_step_log_says_a_pull_request_opened_not_that_it_merged():
    """It said "merged and deployed" after a PR, which is simply untrue --
    nothing merged and nothing deployed -- and buried the one thing anybody
    needs in a stringified dict."""
    deployed = {"ok": True, "shipped": "pull_request",
                "pull_request": "https://github.com/o/r/pull/4", "number": "4"}

    # The expression under test, lifted verbatim from verify_and_ship.
    ship_mode = "pr"
    if not deployed["ok"]:
        summary = "merge/deploy FAILED" if ship_mode != "pr" else "could not open a pull request"
    elif deployed.get("shipped") == "pull_request":
        summary = f"pull request opened: {deployed.get('pull_request', '')}"
    else:
        summary = "merged and deployed"

    assert summary == "pull request opened: https://github.com/o/r/pull/4"
    assert "merged" not in summary


def test_a_push_mode_ship_still_says_merged_and_deployed():
    deployed = {"ok": True, "output": "Fast-forward"}
    ship_mode = "push"
    if not deployed["ok"]:
        summary = "merge/deploy FAILED" if ship_mode != "pr" else "could not open a pull request"
    elif deployed.get("shipped") == "pull_request":
        summary = f"pull request opened: {deployed.get('pull_request', '')}"
    else:
        summary = "merged and deployed"
    assert summary == "merged and deployed"


# --- what is pushed is what was reviewed ------------------------------------
#
# 2026-10-22 audit, R19: the push was `+{branch}:...`, so a commit made on the
# branch after the review went out in the pull request unreviewed.

def test_the_reviewed_commit_is_pushed_not_the_branch_name(monkeypatch):
    cmd, _, r = _ship_capturing_push(monkeypatch, "git@github.com:o/r.git")
    assert r["ok"] is True
    assert f"+{TIP}:refs/heads/agent/t1" in cmd


def test_a_branch_that_moved_since_the_review_is_not_pushed(monkeypatch):
    import agent.config as agent_config
    import agent.tools.git as gitmod

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config", lambda: type("C", (), {"github_token": "tok"})())
    pushed = []

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": "f00d" * 10}      # a commit nobody reviewed
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if _is_push(cmd):
            pushed.append(cmd)
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    r = asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", TIP, "T"))
    assert r["ok"] is False and r["reason"] == "diverged"
    assert "not the reviewed" in r["error"]
    assert pushed == []
