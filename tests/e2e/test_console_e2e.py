"""The console, end to end, in a real browser.

One operator's first session: sign in with the seeded admin's one-time
password, set a new one, start a build task, watch it reach the merge
approval, approve the merge, and see it done -- with the change on the
project's live main.

Real: the agent server (uvicorn, its own process, against a real Postgres),
the built dashboard it serves, git, and the task graph with its deep agent,
middleware and tools. Stood in for (tests/e2e/fakes.py): the model router,
which plays one deterministic build, and the review services, which pass
every branch and fast-forward the live checkout on merge.

Needs a throwaway Postgres (AGENT_E2E_PG_DSN; the test creates and drops
its own database in it), frontend/dist (`npm run build` in frontend/) and
Playwright's Chromium (`playwright install chromium`). Without them it
skips, except where REQUIRE_E2E=1 (CI's e2e job) makes a skip a failure.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from agent import paths
from tests.e2e.fakes import BUILD_CONTENT, BUILD_PATH, FakeReview, FakeRouter

REPO = paths.REPO_ROOT
DIST = REPO / "frontend" / "dist" / "index.html"
PROJECT = "e2e-greeting"
ADMIN = "admin@example.com"
NEW_PASSWORD = "E2e-Operator-Passw0rd"
# 32 random bytes per run; the agent and show_initial_password.py share it.
AUTH_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
INITIAL_PASSWORD = paths.DATA_DIR / ".initial-admin-password"

BOOT_TIMEOUT_S = 90
TASK_TIMEOUT_MS = 120_000


def _missing(reason: str):
    if os.environ.get("REQUIRE_E2E") == "1":
        pytest.fail(f"REQUIRE_E2E=1 but {reason}")
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(cwd: Path, *args: str) -> str:
    # No identity on the CI runner: every commit here names its own.
    return subprocess.run(
        ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.com", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True).stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures: a database, a project, the fakes, the server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def database():
    """A database of its own inside AGENT_E2E_PG_DSN's server, dropped after."""
    dsn = os.environ.get("AGENT_E2E_PG_DSN")
    if not dsn:
        _missing("AGENT_E2E_PG_DSN is not set (a throwaway Postgres the test may create databases in)")
    import psycopg
    name = f"persei_e2e_{uuid.uuid4().hex[:10]}"
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError as e:
        _missing(f"cannot reach AGENT_E2E_PG_DSN: {e}")
    u = urlsplit(dsn)
    yield urlunsplit((u.scheme, u.netloc, f"/{name}", u.query, u.fragment))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """A live repo with one commit on main, and the sandbox worktree tasks
    are made from. The package.json names no script, so the checks need no
    sandbox container."""
    root = tmp_path_factory.mktemp("e2e-project")
    live, sandbox = root / "live" / PROJECT, root / "workspaces" / PROJECT
    live.mkdir(parents=True)
    _git(live, "init", "-q", "-b", "main")
    (live / "README.md").write_text("# A project for the end-to-end test\n")
    (live / "package.json").write_text(json.dumps({"name": PROJECT, "private": True}) + "\n")
    _git(live, "add", "-A")
    _git(live, "commit", "-q", "-m", "initial")
    sandbox.parent.mkdir(parents=True)
    _git(live, "worktree", "add", "-q", "--detach", str(sandbox), "main")
    projects_json = root / "projects.json"
    projects_json.write_text(json.dumps({"projects": {PROJECT: {
        "sandbox": str(sandbox), "live": str(live), "base_branch": "main", "ci_gate": False,
    }}, "test_env": {}}, indent=2))
    return {"live": live, "sandbox": sandbox, "projects_json": projects_json, "root": root}


@pytest.fixture(scope="module")
def fakes(project):
    router = FakeRouter().start()
    review = FakeReview(PROJECT, str(project["live"])).start()
    yield router, review
    router.stop()
    review.stop()


@pytest.fixture(scope="module")
def server(database, project, fakes):
    if not DIST.exists():
        _missing(f"{DIST.relative_to(REPO)} is missing (npm run build in frontend/)")
    router, review = fakes
    port = _free_port()
    log_path = project["root"] / "server.log"
    INITIAL_PASSWORD.unlink(missing_ok=True)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("LANGGRAPH_", "AGENT_", "REVIEW_", "MODEL_ROUTER"))},
        "LANGGRAPH_PG_DSN": database,
        "MODEL_ROUTER_URL": router.url,
        "MODEL_ROUTER_KEY": "e2e-placeholder",
        "AUTH_SECRET_KEY": AUTH_KEY,
        "SMTP_HOST": "", "SMTP_PORT": "587", "SMTP_USER": "", "SMTP_PASS": "", "SMTP_FROM": "",
        "ADMIN_EMAIL": ADMIN,
        # A loopback desktop install: two-factor setup is optional there,
        # and this test is about tasks, not TOTP.
        "TEKTONIX_DESKTOP": "1",
        "BIND_ADDRESS": "127.0.0.1",
        "AGENT_PROJECTS_JSON": str(project["projects_json"]),
        "REVIEW_SERVICE_HOST": "127.0.0.1",
        "REVIEW_SERVICE_PORT": str(review.service.port),
        "REVIEW_CONTROL_HOST": "127.0.0.1",
        "REVIEW_CONTROL_PORT": str(review.control.port),
        "REVIEW_CONTROL_SECRET": "e2e-placeholder",
        "GIT_USER_NAME": "Persei e2e",
        "GIT_USER_EMAIL": "e2e@example.com",
        "EMBEDDINGS_ENABLED": "false",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "GITHUB_TOKEN": "",
        "PYTHONUNBUFFERED": "1",
    }
    # pm2 off the PATH: the service watcher returns at once without it.
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if not (Path(p) / "pm2").exists())
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent.server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_boot(proc, base, log_path)
        yield {"base": base, "env": env, "log": log_path, "proc": proc}
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(proc.pid, signal.SIGKILL)
        log.close()
        INITIAL_PASSWORD.unlink(missing_ok=True)


def _wait_for_boot(proc, base: str, log_path: Path) -> None:
    import httpx
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the server exited {proc.returncode} at boot:\n{log_path.read_text()[-4000:]}")
        try:
            if httpx.get(f"{base}/api/auth/me", timeout=2).status_code in (200, 401):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"the server did not answer within {BOOT_TIMEOUT_S}s:\n{log_path.read_text()[-4000:]}")


def _initial_password(env: dict) -> str:
    """The seeded admin's one-time password, the way an operator reads it."""
    deadline = time.monotonic() + 30
    while not INITIAL_PASSWORD.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    proc = subprocess.run([sys.executable, "scripts/show_initial_password.py"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def browser_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _missing("playwright is not installed")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - the browser binary is missing
            _missing(f"Chromium will not launch (playwright install chromium): {e}")
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30_000)
        yield page
        context.close()
        browser.close()


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def test_an_operator_signs_in_builds_approves_the_merge_and_sees_it_done(server, project, fakes, browser_page):
    from playwright.sync_api import expect

    router, review = fakes
    page = browser_page
    base = server["base"]
    try:
        # Sign in as the seeded admin, then set a password of our own.
        page.goto(base + "/")
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        page.get_by_label("Email").fill(ADMIN)
        initial = _initial_password(server["env"])
        page.get_by_label("Password", exact=True).fill(initial)
        page.get_by_role("button", name="Sign in", exact=True).click()

        expect(page.get_by_role("heading", name="Set a new password")).to_be_visible()
        # The fields are captioned by a <span>, not a <label>.
        def field(caption):
            return page.locator(f'.form-field:has(> span:text-is("{caption}")) input')
        field("Current (temporary) password").fill(initial)
        field("New password").fill(NEW_PASSWORD)
        field("Confirm new password").fill(NEW_PASSWORD)
        page.get_by_role("button", name="Set password").click()
        expect(page.get_by_role("heading", name="Set a new password")).to_be_hidden()

        # Start a build task directly. auth.change_password revokes every
        # session, the one that made the change included, so the next load
        # may ask for the new password; either way, it has to work.
        page.goto(base + "/new")
        new_task = page.get_by_role("heading", name="What should the agent build?")
        sign_in = page.get_by_role("heading", name="Sign in")
        expect(new_task.or_(sign_in)).to_be_visible()
        if sign_in.is_visible():
            page.get_by_label("Email").fill(ADMIN)
            page.get_by_label("Password", exact=True).fill(NEW_PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
            expect(sign_in).to_be_hidden()
            page.goto(base + "/new")
        expect(new_task).to_be_visible()
        page.get_by_label("Repo").select_option(PROJECT)
        page.locator("textarea.goal-input").fill(f"Add {BUILD_PATH} with a one-line greeting.")
        page.get_by_role("button", name="Start Task").click()
        page.wait_for_url(re.compile(r"/task/[^/]+$"))
        task_id = page.url.rstrip("/").rsplit("/", 1)[-1]

        # It builds, passes review, and stops for the operator's last look.
        expect(page.get_by_text("Review diff").first).to_be_visible(timeout=TASK_TIMEOUT_MS)
        approve = page.get_by_role("button", name="Approve & merge")
        expect(approve).to_be_visible()
        expect(page.get_by_text(BUILD_PATH).first).to_be_visible()
        assert _git(project["live"], "ls-tree", "--name-only", "main").split() == ["README.md", "package.json"], \
            "live moved before the merge was approved"

        page.once("dialog", lambda d: d.accept())
        approve.click()

        expect(page.get_by_text("Done", exact=True).first).to_be_visible(timeout=TASK_TIMEOUT_MS)
    except Exception:
        _dump(page, server, router, review)
        raise

    # The change is on live main, merged from the task's branch.
    assert review.merged == [f"agent/{task_id}"], review.merged
    assert (project["live"] / BUILD_PATH).read_text() == BUILD_CONTENT
    assert BUILD_PATH in _git(project["live"], "show", "--name-only", "--format=", "main").split()
    assert router.build_calls() >= 2, router.calls


def _dump(page, server, router, review) -> None:
    """What a failed run needs to be read: the server's log, the page, and
    what the fakes were asked."""
    out = Path(os.environ.get("E2E_ARTIFACTS_DIR") or server["log"].parent)
    out.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out / "failure.png"), full_page=True)
        (out / "failure.html").write_text(page.content())
    except Exception:  # noqa: BLE001 - best effort, the assertion is what matters
        pass
    if out != server["log"].parent:
        shutil.copy(server["log"], out / "server.log")
    print("--- server log (tail) ---\n" + server["log"].read_text()[-6000:])
    print(f"--- model calls ---\n{json.dumps(router.calls, indent=1)[-3000:]}")
    print(f"--- review: checked {sorted(review.branches)}, merged {review.merged}")
