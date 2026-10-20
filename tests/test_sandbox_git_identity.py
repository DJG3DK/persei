"""A sandbox container gets the git identity the agent process has, when it
has one: the bundle sets it from ADMIN_EMAIL, and `git commit` the model
runs in the sandbox then works there as it does on a host whose global
config the container cannot see (2026-09-28)."""
import re

from agent import paths
from agent.tools import sandbox


def test_the_identity_is_passed_through_only_when_set(monkeypatch):
    for k in sandbox.GIT_IDENTITY_VARS:
        monkeypatch.delenv(k, raising=False)
    assert sandbox.git_identity_args() == []
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Persei")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ops@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "  ")
    assert sandbox.git_identity_args() == ["-e", "GIT_AUTHOR_NAME=Persei", "-e", "GIT_AUTHOR_EMAIL=ops@example.test"]


def test_both_docker_run_argument_lists_include_it():
    src = (paths.REPO_ROOT / "agent/tools/sandbox.py").read_text()
    assert len(re.findall(r"^\s+\*git_identity_args\(\),$", src, re.M)) == 2, "the task sandbox and the check sandbox"
