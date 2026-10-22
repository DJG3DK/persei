"""A pull request is rebased onto a moved base, like a merge is.

The merge path is `--ff-only`, so a base that moved stops it dead with
`diverged`, and verify_and_ship answers by rebasing the branch and reviewing
the new sha. That self-heal has existed for a while and works.

A pull request never fast-forwards. So it never hit the check, never reported
anything wrong, and pushed the branch exactly as it was cut -- opening a PR
that was already behind before anyone looked at it and, where the changes
overlapped, one that would not merge at all. Nothing self-healed it because
nothing said anything was wrong.

Found 2026-09-22 with five task branches open on one project, all cut from the
same tip, each shipping as a PR.
"""
import subprocess

import pytest

from agent.tools import git as git_mod
from agent.tools import review_gate as rg

pytestmark = pytest.mark.asyncio


def _run(args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env, check=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repo on main, plus a task branch cut from its tip."""
    live = tmp_path / "live"
    live.mkdir()
    _run(["init", "-q", "-b", "main"], live)
    (live / "app.py").write_text("v1\n")
    _run(["add", "-A"], live)
    _run(["commit", "-q", "-m", "one"], live)
    _run(["branch", "agent/task-1"], live)

    from agent.config import PROJECTS
    monkeypatch.setitem(PROJECTS, "demo", {"live": str(live), "sandbox": str(live), "ship": "pr"})
    from agent import github_settings
    monkeypatch.setattr(github_settings, "token_for", lambda *a, **k: "ghp_token")
    return live


async def test_a_branch_on_the_current_tip_ships_normally(project, monkeypatch):
    pushed = {}

    async def fake_git(cmd, cwd, timeout=None, extra_env=None):
        if " push " in f" {cmd} ":
            pushed["cmd"] = cmd
            return {"ok": True, "output": ""}
        if "remote get-url" in cmd:
            return {"ok": True, "output": "https://github.com/OWNER/REPO.git"}
        return await real(cmd, cwd, timeout=timeout, extra_env=extra_env)

    real = git_mod._git
    monkeypatch.setattr(git_mod, "_git", fake_git)

    async def fake_pr(*a, **k):
        return {"url": "https://github.com/OWNER/REPO/pull/1", "number": 1}
    from agent import github_repos
    monkeypatch.setattr(github_repos, "open_pull_request", fake_pr)

    tip = _run(["rev-parse", "agent/task-1"], project).stdout.strip()
    out = await rg.ship_as_pull_request("demo", "agent/task-1", tip, "t")
    assert out["ok"] is True and out["shipped"] == "pull_request"
    assert "push" in pushed["cmd"]


async def test_a_branch_whose_base_moved_reports_diverged_instead_of_pushing(project, monkeypatch):
    """The whole fix: it must NOT quietly open a PR from a stale base."""
    (project / "other.py").write_text("somebody else landed this\n")
    _run(["add", "-A"], project)
    _run(["commit", "-q", "-m", "two"], project)      # main moves, branch does not

    pushed = []

    async def fake_git(cmd, cwd, timeout=None, extra_env=None):
        if " push " in f" {cmd} ":
            pushed.append(cmd)
            return {"ok": True, "output": ""}
        if "remote get-url" in cmd:
            return {"ok": True, "output": "https://github.com/OWNER/REPO.git"}
        return await real(cmd, cwd, timeout=timeout, extra_env=extra_env)

    real = git_mod._git
    monkeypatch.setattr(git_mod, "_git", fake_git)

    out = await rg.ship_as_pull_request("demo", "agent/task-1", "deadbeef" * 5, "t")
    assert out["ok"] is False
    assert out["reason"] == "diverged"
    assert "moved on by 1 commit" in out["error"]
    assert pushed == [], "it pushed a stale branch anyway"


async def test_the_reason_is_the_one_the_existing_self_heal_already_handles():
    """Reported exactly as the merge path reports it, so the SAME branch in
    verify_and_ship rebases and re-reviews. A new reason string would have
    needed a second handler that could drift from the first."""
    import inspect

    from agent.nodes import verify_and_ship as vs

    src = inspect.getsource(vs._review_and_deploy)
    assert 'deployed.get("reason") == "diverged"' in src
    assert "rebase_onto_base" in src


async def test_a_rebase_in_progress_is_not_reported_as_a_failure():
    """It is the system fixing itself. Calling it FAILED sent an operator
    looking for a problem while it was being solved."""
    import inspect

    from agent.nodes import verify_and_ship as vs

    src = inspect.getsource(vs._review_and_deploy)
    i_div = src.index('ship_summary = "the base moved on')
    i_fail = src.index('ship_summary = "merge/deploy FAILED"')
    assert i_div < i_fail, "the failure branch still catches a diverged ship first"


async def test_an_unreadable_commit_count_does_not_block_the_ship(project, monkeypatch):
    """`int("")` is how the first version of this broke nine existing tests.

    A missing or unparseable count is not evidence the base moved, and
    refusing on "unknown" turns an unrelated git hiccup into a task that can
    never finish. It proceeds, exactly as it did before this check existed.
    """
    async def blank_git(cmd, cwd, timeout=None, extra_env=None):
        if "remote get-url" in cmd:
            return {"ok": True, "output": "https://github.com/OWNER/REPO.git"}
        if " push " in f" {cmd} ":
            return {"ok": True, "output": ""}
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": "deadbeef" * 5}
        return {"ok": True, "output": ""}      # including rev-list --count

    monkeypatch.setattr(git_mod, "_git", blank_git)
    from agent import github_repos
    monkeypatch.setattr(github_repos, "open_pull_request",
                        lambda *a, **k: _resolved({"url": "u", "number": 1}))

    out = await rg.ship_as_pull_request("demo", "agent/task-1", "deadbeef" * 5, "t")
    assert out["ok"] is True


def _resolved(value):
    async def _inner():
        return value
    return _inner()
