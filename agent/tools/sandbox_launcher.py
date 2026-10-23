"""The agent's side of the bundle's sandbox launcher (services/sandbox-launcher).

In the compose bundle (TEKTONIX_BUNDLE=1) the agent has no Docker socket: a
socket in the agent container made a compromise of the agent process root on
the host. A separate `sandbox-launcher` service holds it and offers a narrow
API -- run an allow-listed image under its own fixed limits, and exec, kill
and read logs of the containers it started, nothing else -- authenticated
with a key generated at install (the `launcherkey` volume).

The callers (agent/tools/sandbox.py, agent/review_sandbox.py, agent/health.py)
keep building the same `docker run` argv they always have: a host install
runs it as is, and here it is translated into the launcher's request. Only
the flags those callers write are understood, and an unknown one is an error
rather than a silently dropped restriction. The limits (--memory, --cpus,
--pids-limit, --cap-drop, --security-opt) are dropped on purpose: the
launcher applies its own whatever is asked.

A refusal or an unreachable launcher is LauncherError from run(), which the
callers report as the command not having run; the smaller operations answer
the way their docker equivalents fail (exit code 125, docker's own "could not
start", or False) and log the reason.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("tektonix")

DEFAULT_URL = "http://sandbox-launcher:8300"
UNAVAILABLE = 125
# On top of a run's own timeout: the launcher kills a container that overruns
# and answers, which takes a moment.
_SLACK_S = 60


class LauncherError(RuntimeError):
    """The launcher refused, or could not be reached."""


def enabled() -> bool:
    """The bundle: every docker operation goes through the launcher. A host
    install (pm2, no container) keeps calling docker directly."""
    return os.environ.get("TEKTONIX_BUNDLE") == "1"


def _url() -> str:
    return (os.environ.get("SANDBOX_LAUNCHER_URL") or DEFAULT_URL).rstrip("/")


def _key() -> str:
    if os.environ.get("SANDBOX_LAUNCHER_KEY"):
        return os.environ["SANDBOX_LAUNCHER_KEY"].strip()
    path = os.environ.get("SANDBOX_LAUNCHER_KEY_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return ""


async def call(path: str, body: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
    """One request; the answer's JSON, or LauncherError with the reason."""
    key = _key()
    if not key:
        raise LauncherError("the sandbox launcher key is not set (SANDBOX_LAUNCHER_KEY_FILE)")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if body is None:
                r = await client.get(_url() + path, headers=headers)
            else:
                r = await client.post(_url() + path, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise LauncherError(f"the sandbox launcher did not answer: {type(e).__name__}: {e}") from e
    try:
        data: dict[str, Any] = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        raise LauncherError(f"the sandbox launcher refused ({r.status_code}): {data.get('error') or r.text[:300]}")
    return data


# --- `docker run` argv -> the launcher's request ----------------------------------

# Limits and hardening the launcher sets itself; their values here are ignored.
_FIXED_WITH_VALUE = {"--memory", "--memory-swap", "--cpus", "--pids-limit", "--cap-drop", "--security-opt"}


def spec_from_run_argv(argv: list[str]) -> dict[str, Any]:
    """The launcher request for a `docker run` argv this codebase built."""
    if argv[:2] != ["docker", "run"]:
        raise ValueError(f"not a docker run argv: {argv[:2]}")
    spec: dict[str, Any] = {"env": {}, "mounts": [], "cap_add": []}
    i = 2
    while i < len(argv):
        a = argv[i]
        if not a.startswith("-"):
            break
        if a == "--rm":
            i += 1
            continue
        if a == "-d":
            spec["detach"] = True
            i += 1
            continue
        if i + 1 >= len(argv):
            raise ValueError(f"{a} has no value")
        v = argv[i + 1]
        if a == "--name":
            spec["name"] = v
        elif a == "-v":
            spec["mounts"].append(_mount(v))
        elif a == "--network":
            spec["network"] = v
        elif a == "-w":
            spec["workdir"] = v
        elif a == "-e":
            k, _, val = v.partition("=")
            spec["env"][k] = val
        elif a == "--cap-add":
            spec["cap_add"].append(v)
        elif a == "--entrypoint":
            spec["entrypoint"] = v
        elif a == "-p":
            spec["publish"] = _publish(v)
        elif a not in _FIXED_WITH_VALUE:
            raise ValueError(f"the sandbox launcher has no equivalent of {a}")
        i += 2
    if i >= len(argv):
        raise ValueError("no image in the docker run argv")
    spec["image"] = argv[i]
    spec["argv"] = list(argv[i + 1:])
    return spec


def _mount(v: str) -> dict[str, Any]:
    readonly = v.endswith(":ro")
    if readonly:
        v = v[:-3]
    source, sep, target = v.rpartition(":")
    if not sep or not source:
        raise ValueError(f"cannot read the mount {v!r}")
    return {"source": source, "target": target, "readonly": readonly}


def _publish(v: str) -> dict[str, Any]:
    parts = v.split(":")
    if len(parts) != 3 or parts[0] != "127.0.0.1":
        raise ValueError(f"only a loopback publish is supported, not {v!r}")
    return {"host_port": int(parts[1]), "container_port": int(parts[2])}


# --- the operations the callers use ------------------------------------------------

async def run(argv: list[str], timeout_s: float) -> tuple[int, str, bool]:
    """Run to completion: (exit code, output, timed out). The launcher kills
    the container itself when it overruns. LauncherError when it refused or
    could not be reached: the command did not run."""
    spec = spec_from_run_argv(argv)
    spec["timeout_s"] = max(1, int(timeout_s))
    r = await call("/v1/run", spec, timeout=timeout_s + _SLACK_S)
    return int(r.get("exit_code", UNAVAILABLE)), str(r.get("output") or ""), bool(r.get("timed_out"))


async def run_detached(argv: list[str]) -> tuple[int, str]:
    spec = spec_from_run_argv(argv)
    spec["detach"] = True
    try:
        r = await call("/v1/run", spec, timeout=180)
    except LauncherError as e:
        return UNAVAILABLE, str(e)
    return int(r.get("exit_code", UNAVAILABLE)), str(r.get("output") or "")


async def kill(name: str) -> None:
    """Best effort, like `docker kill`: a container already gone is fine."""
    try:
        await call("/v1/containers/kill", {"name": name}, timeout=60)
    except LauncherError as e:
        logger.debug("sandbox launcher: kill %s: %s", name, e)


async def running(name: str) -> bool:
    try:
        return bool((await call("/v1/containers/state", {"name": name}, timeout=20)).get("running"))
    except LauncherError:
        return False


async def logs(name: str, tail: int) -> str:
    try:
        return str((await call("/v1/containers/logs", {"name": name, "tail": tail}, timeout=30)).get("output") or "")
    except LauncherError:
        return ""


async def exec_in(name: str, argv: list[str], timeout_s: int = 60) -> tuple[int, str]:
    try:
        r = await call("/v1/containers/exec", {"name": name, "argv": argv, "timeout_s": timeout_s},
                       timeout=timeout_s + _SLACK_S)
    except LauncherError as e:
        return UNAVAILABLE, str(e)
    return int(r.get("exit_code", UNAVAILABLE)), str(r.get("output") or "")


async def docker_version() -> tuple[bool, str]:
    try:
        r = await call("/v1/status", timeout=30)
    except LauncherError as e:
        return False, str(e)
    return bool(r.get("ok")), str(r.get("docker") or r.get("error") or "")


async def image_present(image: str) -> bool:
    try:
        return bool((await call("/v1/images/present", {"image": image}, timeout=30)).get("present"))
    except LauncherError as e:
        logger.warning("sandbox launcher: could not ask about %s: %s", image, e)
        return False


async def build_default_image() -> bool:
    """Ask for the sandbox image to be built from the launcher's own context.
    True when a build is now in progress."""
    try:
        return bool((await call("/v1/images/build", {}, timeout=30)).get("building"))
    except LauncherError as e:
        logger.warning("sandbox launcher: could not start the sandbox image build: %s", e)
        return False


async def checks_psql(sql: str, user: str, db: str, rows: bool = False) -> tuple[bool, str]:
    try:
        r = await call("/v1/checks/psql", {"sql": sql, "user": user, "db": db, "rows": rows}, timeout=90)
    except LauncherError as e:
        return False, str(e)
    return bool(r.get("ok")), str(r.get("output") or "").strip()


async def checks_redis_flush(db: int) -> tuple[bool, str]:
    try:
        r = await call("/v1/checks/redis-flush", {"db": db}, timeout=60)
    except LauncherError as e:
        return False, str(e)
    return bool(r.get("ok")), str(r.get("output") or "").strip()
