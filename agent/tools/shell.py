"""Shell execution — deliberately non-interactive by construction.

A bash tool that runs commands against a real pty can hang forever waiting
on an unanswered interactive prompt (e.g. a package manager's
unapproved-build-scripts confirmation) -- a real outage risk for anything
running unattended. asyncio.subprocess with PIPE (no pty) and CI=true means
any tool that checks for an interactive terminal or the CI env var takes its
non-interactive path instead of prompting, and a hard timeout means even a
tool that ignores both fails loudly instead of hanging silently.
"""

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any

# POSIX puts a child and everything it forks in one process group, which is
# what makes a single killpg enough. Windows has no equivalent primitive, so
# the two halves -- how the child is launched, and how it is killed -- have to
# differ together. Kept next to each other so they cannot drift apart.
_WINDOWS = sys.platform == "win32"


class ShellTimeout(Exception):
    def __init__(self, cmd: str, timeout: int):
        super().__init__(f"command timed out after {timeout}s: {cmd}")
        self.cmd = cmd
        self.timeout = timeout


def _spawn_kwargs() -> dict[str, Any]:
    """Launch flags that make the child killable as a tree."""
    # The same test as _WINDOWS, spelled out so the type checker knows the
    # Windows-only constant below exists on this branch.
    if sys.platform == "win32":
        # The Windows equivalent of a session leader: the child becomes its own
        # process-group root, which is what `taskkill /T` walks.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    # proc.kill() alone only signals the shell itself (`/bin/sh -c cmd`), not
    # whatever cmd went on to fork (pnpm spawning tsc, which spawns workers)
    # — those get reparented to init and keep running detached. Since
    # start_new_session=True below makes this process its own session/group
    # leader, its pid doubles as the process group id, so one killpg reaches
    # the whole tree in a single signal.
    if _WINDOWS:
        # No killpg here. `taskkill /T` is the only thing that reaches the
        # whole tree, and it is a separate process, so this stays best-effort
        # and synchronous -- the caller is already in a timeout or cancellation
        # path and must not be able to hang a second time.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False)
        except Exception:  # noqa: BLE001 -- killing is best-effort by nature
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # already exited


# Git's identity: the one set of host variables a shell command and a sandbox
# container both carry. Not a secret, and every commit needs it. One tuple,
# imported by sandbox.py, so a variable added here reaches both (2026-09-29
# audit, A15: the four names were spelled out in each file).
GIT_IDENTITY_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


# The ONLY host env vars a shell command gets by default. Deliberately does NOT
# include os.environ (audit C-2): agent-authored code runs through this path
# (checks.py executes `npm run <script>`, and package.json is a file the agent
# can rewrite), and inheriting os.environ handed it AUTH_SECRET_KEY,
# MODEL_ROUTER_KEY, the Postgres DSN, SMTP_PASS and LANGSMITH_API_KEY --
# reproduced live: a rewritten test script printed the signing key and the DSN
# and the gate still returned all_ok. A shell command needs PATH to find its
# binaries and HOME for per-user tool config (git, npm), plus the locale, CI
# and DEBIAN_FRONTEND set below, and git's identity, which is load-bearing in
# the compose bundle (2026-09-29, see the loop). Nothing else from the host
# environment. Anything a specific caller genuinely needs is passed
# explicitly via extra_env.
def _safe_base_env() -> dict[str, str]:
    base = {
        "CI": "true",
        "DEBIAN_FRONTEND": "noninteractive",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    # Git's identity is not a secret and every commit and rebase this helper
    # runs needs it. On a host install the home directory's git config filled
    # the gap; in the compose bundle there is none, and the scrub above ate
    # the identity compose had set: "Please tell me who you are" at the final
    # commit, twice, on the first Windows install (2026-09-29).
    for key in GIT_IDENTITY_VARS:
        value = os.environ.get(key, "").strip()
        if value:
            base[key] = value
    return base


async def run_shell(
    cmd: str, cwd: str, timeout: int = 120, extra_env: dict[str, str] | None = None,
    inherit_env: bool = False,
) -> dict[str, Any]:
    """Runs `cmd` via a shell, no TTY, CI=true, hard timeout.

    Env is a MINIMAL allow-list by default (see _safe_base_env), NOT the host
    environment -- this path runs agent-authored code and must not leak
    secrets (audit C-2). `inherit_env=True` opts a trusted caller back into the
    full os.environ; nothing does today, and no caller running agent-authored
    input ever should.

    Returns {"ok": bool, "exit_code": int, "output": str} -- output is
    stdout+stderr combined (matches how the tool-calling model actually needs
    to reason about failures: it rarely matters which stream a message came
    from, and combining avoids the model having to check two fields).
    """
    env = {**os.environ} if inherit_env else _safe_base_env()
    env.update({"CI": "true", "DEBIAN_FRONTEND": "noninteractive", **(extra_env or {})})
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # no stdin at all — an interactive prompt gets EOF immediately, never blocks
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_spawn_kwargs(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_process_group(proc)
        await proc.wait()
        raise ShellTimeout(cmd, timeout)
    except asyncio.CancelledError:
        # The operator's Stop button (POST /api/tasks/{id}/stop, agent/routers/tasks.py) cancels the
        # asyncio task driving the whole run. Without this handler that just
        # abandons this await — the subprocess (and anything it forked) keeps
        # running orphaned on the server, which defeats the entire point of a
        # stop button that's supposed to "really work." Re-raise after
        # cleanup so cancellation still propagates normally.
        _kill_process_group(proc)
        await proc.wait()
        raise

    output = stdout.decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output[-20_000:]}
