"""Deleting a project's checkout: offered, never assumed, and refused loudly.

Removing a project has always left the live repository alone, and that is
still the default. The gap this closes is the one case where it is wrong: a
repository Persei cloned by itself -- usually because a stale list offered
to add something already onboarded -- leaves a clone behind that only a shell
can remove, which is a manual step inside a flow the dashboard otherwise owns
end to end.

So the rule is inverted carefully: the checkout goes only when it can be shown
that nothing would be lost AND nothing would break. Every test here is a way
that could be wrong.
"""

import subprocess
from pathlib import Path

import pytest

from agent import project_removal as pr


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pushed_clone(tmp_path):
    """A clone whose every commit is on its origin -- the disposable case."""
    origin = tmp_path / "origin.git"
    _run(tmp_path, "git", "init", "-q", "--bare", "--initial-branch=main", str(origin))
    work = tmp_path / "work"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(work))
    _run(work, "git", "config", "user.email", "t@example.com")
    _run(work, "git", "config", "user.name", "T")
    _run(work, "git", "checkout", "-qB", "main")
    (work / "README.md").write_text("hello\n")
    _run(work, "git", "add", "-A")
    _run(work, "git", "commit", "-qm", "first")
    _run(work, "git", "push", "-q", "-u", "origin", "main")
    return work


def test_a_fully_pushed_clone_is_disposable(pushed_clone):
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert ok, reason
    assert str(pushed_clone) in reason


def test_an_uncommitted_change_stops_it(pushed_clone):
    (pushed_clone / "README.md").write_text("edited\n")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "1 uncommitted change" in reason


def test_an_untracked_file_stops_it(pushed_clone):
    (pushed_clone / "notes.txt").write_text("mine\n")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "uncommitted" in reason


def test_a_commit_that_is_on_no_remote_stops_it(pushed_clone):
    (pushed_clone / "README.md").write_text("more\n")
    _run(pushed_clone, "git", "commit", "-aqm", "unpushed")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "not on any remote" in reason


def test_work_on_an_unpushed_branch_stops_it(pushed_clone):
    """The dangerous near-miss: main is clean and fully pushed, and the work
    is sitting on a branch nobody published."""
    _run(pushed_clone, "git", "checkout", "-qb", "wip")
    (pushed_clone / "draft.txt").write_text("half an idea\n")
    _run(pushed_clone, "git", "add", "-A")
    _run(pushed_clone, "git", "commit", "-qm", "wip")
    _run(pushed_clone, "git", "checkout", "-q", "main")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "not on any remote" in reason


def test_a_stash_stops_it(pushed_clone):
    (pushed_clone / "README.md").write_text("stashed\n")
    _run(pushed_clone, "git", "stash", "-q")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "stash" in reason


def test_a_declared_secret_file_stops_it(pushed_clone):
    """git does not carry it and git status does not mention it, because it
    is gitignored -- and it is the file whose loss has no undo."""
    (pushed_clone / ".gitignore").write_text(".env\n")
    _run(pushed_clone, "git", "add", "-A")
    _run(pushed_clone, "git", "commit", "-qm", "ignore env")
    _run(pushed_clone, "git", "push", "-q", "origin", "main")
    (pushed_clone / ".env").write_text("SECRET=1\n")

    assert pr.checkout_disposable(str(pushed_clone))[0], "gitignored, so git sees nothing"
    ok, reason = pr.checkout_disposable(str(pushed_clone), [".env"])
    assert not ok and ".env" in reason


def test_a_repo_with_no_remote_is_never_disposable(tmp_path):
    solo = tmp_path / "solo"
    solo.mkdir()
    _run(solo, "git", "init", "-q")
    ok, reason = pr.checkout_disposable(str(solo))
    assert not ok and "only copy" in reason


def test_a_directory_that_is_not_a_checkout_is_never_disposable(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x")
    ok, reason = pr.checkout_disposable(str(plain))
    assert not ok and "not a git checkout" in reason


def test_a_missing_directory_is_reported_not_crashed(tmp_path):
    ok, reason = pr.checkout_disposable(str(tmp_path / "gone"))
    assert not ok and "no directory" in reason


# --- what this box serves -------------------------------------------------

def test_a_project_this_box_runs_is_never_disposable(pushed_clone):
    """Everything can be on the remote and deleting it still takes a site
    down. "Nothing would be lost" and "nothing would break" are different
    questions and both have to be yes."""
    runs = pr.runs_on_this_box({"deploy": {"pm2Apps": ["shop-api"]}})
    assert runs and "shop-api" in runs
    ok, reason = pr.checkout_disposable(str(pushed_clone), None, runs)
    assert not ok and "shop-api" in reason and "take that down" in reason


def test_a_restart_command_counts_as_running_it_too():
    runs = pr.runs_on_this_box({"deploy": {"restart": [{"cmd": "docker", "args": ["compose"]}]}})
    assert runs and "restarts it" in runs


def test_a_project_this_box_does_not_run_says_so():
    assert pr.runs_on_this_box({"deploy": {"build": [{"cmd": "npm"}]}}) is None
    assert pr.runs_on_this_box({}) is None
    assert pr.runs_on_this_box(None) is None


# --- the deletion itself --------------------------------------------------

def test_delete_checkout_removes_a_disposable_one(pushed_clone):
    ok, detail = pr.delete_checkout(str(pushed_clone))
    assert ok and "deleted" in detail
    assert not Path(pushed_clone).exists()


def test_delete_checkout_checks_again_rather_than_trusting_the_caller(pushed_clone):
    """The verdict the operator was shown was read off a working tree that
    anything could have written to since."""
    (pushed_clone / "appeared-since.txt").write_text("later\n")
    ok, detail = pr.delete_checkout(str(pushed_clone))
    assert not ok and "left" in detail
    assert Path(pushed_clone).exists(), "and it is still there"


def test_delete_checkout_refuses_a_project_this_box_runs(pushed_clone):
    ok, detail = pr.delete_checkout(str(pushed_clone), None, "pm2 runs api from it")
    assert not ok and Path(pushed_clone).exists()


# --- what pm2 is running --------------------------------------------------
#
# The config is not where this answer lives: the projects this box has always
# served keep their pm2 apps in the review service's own JavaScript config,
# which Python never reads. Going by projects.json alone called two live
# production checkouts deletable.

def _fake_pm2(monkeypatch, apps, *, returncode=0, missing=False, stdout=None):
    import subprocess as sp

    def fake_run(cmd, *a, **kw):
        if cmd[0] != "pm2":
            return _real_run(cmd, *a, **kw)
        if missing:
            raise FileNotFoundError("pm2")
        import json as j
        out = stdout if stdout is not None else j.dumps(apps)
        return sp.CompletedProcess(cmd, returncode, out, "")

    _real_run = sp.run
    monkeypatch.setattr(pr.subprocess, "run", fake_run)


def test_a_checkout_pm2_runs_from_is_refused_by_name(pushed_clone, monkeypatch):
    _fake_pm2(monkeypatch, [{"name": "shop-api", "pm2_env": {"pm_cwd": str(pushed_clone)}}])
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "shop-api" in reason and "take that down" in reason


def test_an_app_running_from_a_subdirectory_counts(pushed_clone, monkeypatch):
    _fake_pm2(monkeypatch, [
        {"name": "api", "pm2_env": {"pm_cwd": str(pushed_clone / "apps" / "api")}},
    ])
    assert not pr.checkout_disposable(str(pushed_clone))[0]


def test_an_app_running_from_somewhere_else_does_not(pushed_clone, monkeypatch):
    _fake_pm2(monkeypatch, [{"name": "other", "pm2_env": {"pm_cwd": "/srv/unrelated"}}])
    assert pr.checkout_disposable(str(pushed_clone))[0]


def test_a_path_that_merely_shares_a_prefix_does_not(pushed_clone, monkeypatch):
    # /home/thing-old must not match /home/thing.
    _fake_pm2(monkeypatch, [{"name": "near", "pm2_env": {"pm_cwd": f"{pushed_clone}-old"}}])
    assert pr.checkout_disposable(str(pushed_clone))[0]


def test_no_pm2_on_the_box_is_a_clear_answer(pushed_clone, monkeypatch):
    _fake_pm2(monkeypatch, [], missing=True)
    assert pr.checkout_disposable(str(pushed_clone))[0], \
        "nothing is run by a pm2 that is not installed"


def test_a_pm2_that_cannot_be_asked_is_a_refusal(pushed_clone, monkeypatch):
    """An unanswerable question before an irreversible delete is a no."""
    _fake_pm2(monkeypatch, [], returncode=1)
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "could not be asked" in reason


def test_a_pm2_reply_that_is_not_json_is_a_refusal(pushed_clone, monkeypatch):
    _fake_pm2(monkeypatch, [], stdout="not json at all")
    ok, reason = pr.checkout_disposable(str(pushed_clone))
    assert not ok and "could not be read" in reason
