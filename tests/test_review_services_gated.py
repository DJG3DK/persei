"""The review services answer nothing but /health without the control secret.

In the bundle both bind 0.0.0.0 on the compose network so the agent can reach
them -- and the agent's sandbox containers have network access too. Until
2026-09-23 the review dashboard's READ routes (every project's name, status
and full diff, the router's spend) were open on the reasoning that the port
is loopback, which is only true on a host install; and the commit reviewer's
/health named the projects under review. This starts both services for real,
on free ports with an empty project list, and asks them from outside the
proxy.
"""
import http.client
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from agent import paths

SECRET = "test-control-secret-0123456789abcdef"


def _agent_review_deps_installed() -> bool:
    out = subprocess.run(["node", "-e", "require.resolve('express')"],
                         cwd=str(paths.REPO_ROOT / "services" / "agent-review"), capture_output=True)
    return out.returncode == 0


# A fresh clone without the dashboard's npm dependencies skips, with the
# command that fixes it; CI installs them and sets REQUIRE_SERVICE_TESTS, so
# there a skip is a failure -- the same rule as REQUIRE_MOUNT_TESTS, because a
# gate that silently skips its only proof proves nothing.
if not _agent_review_deps_installed():
    if os.environ.get("REQUIRE_SERVICE_TESTS") == "1":
        raise RuntimeError("services/agent-review has no node_modules; "
                           "run: npm ci --prefix services/agent-review --omit=dev")
    pytest.skip("services/agent-review dependencies not installed "
                "(npm ci --prefix services/agent-review --omit=dev)", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _wait(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return                      # it answered, just not 200
        except Exception:  # noqa: BLE001 -- not up yet
            time.sleep(0.1)
    raise AssertionError(f"{url} never came up")


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {}}))
    (tmp_path / "state").mkdir()
    return {**os.environ,
            "AGENT_PROJECTS_JSON": str(projects),
            "REVIEW_ONLY_PROJECTS_JSON": "1",
            "REVIEW_STATE_DIR": str(tmp_path / "state"),
            "REVIEW_WORKTREE_ROOT": str(tmp_path / "wt"),
            "REVIEW_CONTROL_SECRET": SECRET,
            "MODEL_ROUTER_KEY": "unused",
            "REVIEW_BIND_ADDRESS": "127.0.0.1"}


@pytest.fixture
def agent_review(env):
    port = _free_port()
    proc = subprocess.Popen(["node", str(paths.REPO_ROOT / "services/agent-review/server.js")],
                            env={**env, "REVIEW_SERVICE_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait(f"{base}/health")
        yield base
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def commit_reviewer(env):
    port = _free_port()
    proc = subprocess.Popen(["node", str(paths.REPO_ROOT / "services/commit-reviewer/reviewer.js")],
                            env={**env, "REVIEW_CONTROL_PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait(f"{base}/health")
        yield base
    finally:
        proc.kill()
        proc.wait()


READS = ["/api/projects", "/api/projects/x/status", "/api/projects/x/diff",
         "/api/router/stats", "/api/router/balance", "/api/review/status"]


@pytest.mark.parametrize("route", READS)
def test_a_read_without_the_secret_is_refused(agent_review, route):
    status, _ = _get(agent_review + route)
    assert status == 401, f"{route} answered {status} with no secret"


def test_a_read_with_the_secret_is_answered(agent_review):
    """What nginx and the agent's /_review/ proxy send on every request."""
    status, body = _get(agent_review + "/api/projects", {"X-Review-Secret": SECRET})
    assert status == 200 and body == []


def test_agent_review_health_stays_open(agent_review):
    status, body = _get(agent_review + "/health")
    assert status in (200, 503) and body["service"] == "agent-review"


def test_commit_reviewer_health_counts_but_does_not_name(commit_reviewer):
    status, body = _get(commit_reviewer + "/health")
    assert body["service"] == "commit-reviewer"
    assert body["reviewing_count"] == 0
    assert "reviewing" not in body


def test_commit_reviewer_health_names_them_to_the_secret(commit_reviewer):
    _, body = _get(commit_reviewer + "/health", {"X-Review-Secret": SECRET})
    assert body["reviewing"] == []


def _raw(base, method, path, headers=None):
    """A request whose path is sent byte for byte -- urllib would tidy `//`."""
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        return conn.getresponse().status
    finally:
        conn.close()


def test_a_malformed_path_is_a_400_not_a_crash(commit_reviewer):
    """2026-10-22 audit, R17: `GET //` made `new URL` throw and the process
    exit, losing the in-flight review and the queue; `POST /check/%` did the
    same through decodeURIComponent."""
    assert _raw(commit_reviewer, "GET", "//") == 400
    assert _raw(commit_reviewer, "POST", "/check/%", {"X-Review-Secret": SECRET}) == 400
    _, body = _get(commit_reviewer + "/health")
    assert body["service"] == "commit-reviewer", "the service is still up"
