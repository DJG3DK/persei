"""Running the review service's checks in a sandbox, for the compose bundle.

On a host install the commit reviewer starts its own sandbox containers
(services/commit-reviewer/sandbox.js). In the bundle it cannot: it runs in a
container without the Docker socket, and giving it the socket would make it
host-root equivalent. Until this module existed it ran the checks IN ITS OWN
PROCESS instead -- agent-authored test files, executing in the container that
holds the review-control secret and can reach the merge endpoint. A test that
read /app/data/review_control_secret and POSTed `{"force": true}` merged its
own branch past the gate.

So the reviewer asks the agent, which already runs the same commands for its
own `run_checks` tool, to start the container for it -- through the sandbox
launcher, the one bundle service that holds the socket (agent/tools/
sandbox_launcher.py), which applies its own limits and allow-lists on top.
The agent does not take the reviewer's word for anything that decides what
the container can see:

  * the worktree must sit directly under REVIEW_WORKTREE_ROOT, be named for
    the project, and be a git worktree of THAT project's live checkout;
  * every extra mount must come from inside the live checkout, is forced
    read-only, and may only land where the reviewer's own layout puts it
    (the agent's own workspace template was allowed for a day, 2026-09-29,
    and is not: every task shares its files by hardlink);
  * the image is chosen here, from server-owned config;
  * the hardening flags are this module's, never the request's.

The request is authenticated with the review-control secret, which the
reviewer already holds and the checks it starts never see.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field

from agent import paths

from agent.tools import sandbox as sb
from agent.tools import sandbox_launcher as launcher

WORKTREE_ROOT_ENV = "REVIEW_WORKTREE_ROOT"

MAX_TIMEOUT_MS = 30 * 60 * 1000
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
MAX_ARGS = 256

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BASE_ENV = {"CI": "true", "DEBIAN_FRONTEND": "noninteractive", "LANG": "C.UTF-8"}


class RejectedRequest(ValueError):
    """The request asks for something this module will not start."""


@dataclass
class CheckRequest:
    project: str
    worktree: str
    cmd: str
    args: list[str] = field(default_factory=list)
    rel_dir: str = "."
    env: dict[str, str] = field(default_factory=dict)
    network: str = "none"
    stack: str | None = None
    mounts: list[tuple[str, str]] = field(default_factory=list)
    timeout_ms: int = 300_000


# --- the database checks' own network and services (2026-09-27) -----------------
#
# db:drift, db:seed and test:e2e need a Postgres and a Redis. A host install
# runs them OUTSIDE the sandbox against the machine's loopback, the one place
# agent-authored code still runs as root on the box (SECURITY.md). The bundle
# used to refuse them instead. Now the bundle gives them a throwaway
# checks-postgres and checks-redis on a private, internal compose network,
# and the three commands run in the same hardened container as every other
# check, joined to that network alone. The agent is NOT on that network: it
# sets the run up over `docker exec` into the two service containers (psql
# and redis-cli on their own sockets; in the bundle, the sandbox launcher's
# two fixed operations that do exactly that), so nothing a check can reach has a
# route to the agent, the router, the reviewer or the internet. Each run
# gets its own plain role owning its own database (CONNECT revoked from
# everyone else), and its own Redis database from a pool, so runs neither
# see each other nor the server's superuser; what a crashed agent leaves
# behind is dropped at the next start (sweep_orphans). The Redis side is a
# convention with the sharpest tools taken away -- docker-compose.yml denies
# FLUSHALL, CONFIG and friends to the one user -- because Redis cannot bind
# a user to a database index. The names below are server config; a request
# never chooses the network, the containers or the DSN.
CHECKS_NETWORK = "checks"          # the request-side name; mapped to the real network here
CHECKS_NETWORK_ENV = "REVIEW_CHECKS_NETWORK"
CHECKS_POSTGRES_ENV = "REVIEW_CHECKS_POSTGRES_URL"   # postgresql://<superuser>@host:port/postgres, as a check sees it; no password
CHECKS_REDIS_ENV = "REVIEW_CHECKS_REDIS_URL"         # redis://host:port
CHECKS_POSTGRES_CONTAINER_ENV = "REVIEW_CHECKS_POSTGRES_CONTAINER"   # the container `docker exec psql` runs in
CHECKS_REDIS_CONTAINER_ENV = "REVIEW_CHECKS_REDIS_CONTAINER"         # the container `docker exec redis-cli` runs in
CHECKS_REDIS_DATABASES = 16        # redis-server's default; one per concurrent run
_CHECKS_ENVS = (CHECKS_NETWORK_ENV, CHECKS_POSTGRES_ENV, CHECKS_REDIS_ENV,
                CHECKS_POSTGRES_CONTAINER_ENV, CHECKS_REDIS_CONTAINER_ENV)
_DB_STEPS = (("db-drift", "driftCmd", 120_000), ("db-seed", "seedCmd", 60_000), ("e2e", "e2eCmd", 300_000))


def checks_network() -> str | None:
    return os.environ.get(CHECKS_NETWORK_ENV) or None


def db_checks_enabled() -> bool:
    return all(os.environ.get(k) for k in _CHECKS_ENVS)


def worktree_root() -> str | None:
    raw = os.environ.get(WORKTREE_ROOT_ENV, "").strip()
    return os.path.realpath(raw) if raw else None


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _no_nul(value: str, what: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise RejectedRequest(f"{what} must be a string without NUL bytes")
    return value


def _project_live(project: str) -> tuple[str, dict]:
    from agent.config import PROJECTS  # noqa: PLC0415 -- the live dict, reloaded in place
    cfg = PROJECTS.get(project)
    if not isinstance(cfg, dict) or not cfg.get("live"):
        raise RejectedRequest(f"unknown project {project!r}")
    return os.path.realpath(cfg["live"]), cfg


def _checked_worktree(req: CheckRequest, live: str) -> str:
    root = worktree_root()
    if not root:
        raise RejectedRequest(f"{WORKTREE_ROOT_ENV} is not set on the agent")
    wt = os.path.realpath(_no_nul(req.worktree, "worktree"))
    if os.path.dirname(wt) != root:
        raise RejectedRequest("worktree is not directly inside the review worktree root")
    if not os.path.basename(wt).startswith(f"{req.project}-"):
        raise RejectedRequest("worktree is not named for this project")

    dotgit = os.path.join(wt, ".git")
    if os.path.islink(dotgit) or not os.path.isfile(dotgit):
        raise RejectedRequest("worktree has no git pointer file")
    try:
        with open(dotgit, encoding="utf-8") as fh:
            gitdir = fh.read(4096).strip().removeprefix("gitdir:").strip()
    except OSError as e:
        raise RejectedRequest(f"worktree git pointer is unreadable: {e}") from e
    worktrees = os.path.join(live, ".git", "worktrees")
    if not gitdir or not _inside(os.path.realpath(gitdir), worktrees) \
            or os.path.realpath(gitdir) == worktrees:
        raise RejectedRequest("worktree is not a git worktree of this project's live checkout")
    return wt


def _checked_rel_dir(rel_dir: str) -> str:
    rel = os.path.normpath(_no_nul(rel_dir or ".", "relDir")).replace(os.sep, "/")
    if rel.startswith("/") or rel == ".." or rel.startswith("../"):
        raise RejectedRequest("relDir must stay inside the worktree")
    return rel


def _generated_dirs(project: str) -> list[str]:
    """The project's generated-code directories from its review rule
    (projects.json, review.generated[].dir): server-owned, never the
    request's word for it."""
    try:
        from agent.config import _load_projects_config  # noqa: PLC0415
        entry = ((_load_projects_config().get("projects") or {}).get(project) or {})
        rules = (entry.get("review") or {}).get("generated") or []
        return [str(g.get("dir")) for g in rules if isinstance(g, dict) and g.get("dir")]
    except Exception:  # noqa: BLE001 -- no rule, no extra mount
        return []


def _checked_mounts(mounts: list[tuple[str, str]], live: str,
                    generated: list[str] | None = None) -> list[tuple[str, str]]:
    """(real source, container target) for every extra mount, or refuse.

    Two shapes, the only two sandbox.js's mountArgs produces:

      * `<live>/<rel>` at `/workspace/<rel>` -- a declared dependency or
        read-only data directory, laid over the worktree's copy;
      * a path at its own absolute path -- live's .git (the worktree pointer
        names it) or a node_modules tree a worktree symlink resolves into.

    Anything else is a mount the reviewer never asks for, so it is refused
    rather than interpreted.
    """
    out: list[tuple[str, str]] = []
    live_git = os.path.join(live, ".git")
    for src, dst in mounts:
        _no_nul(src, "mount source")
        _no_nul(dst, "mount target")
        if not os.path.isabs(src) or not dst.startswith("/"):
            raise RejectedRequest("mount paths must be absolute")
        if any(c in s for s in (src, dst) for c in (":", ",")):
            raise RejectedRequest("mount paths may not contain ':' or ','")
        real = os.path.realpath(src)
        if not os.path.exists(real):
            continue
        # Live's tree and nothing else. The agent's workspace template was
        # accepted for node_modules for a day (2026-09-29): every task
        # workspace hardlinks it, so a task could rewrite the tools a review
        # then ran. When live cannot lend, the reviewer installs its own.
        if real == live or not _inside(real, live):
            raise RejectedRequest(f"mount source {src!r} is not inside the project's live checkout")
        norm_dst = os.path.normpath(dst)
        if norm_dst.startswith("/workspace/"):
            rel = norm_dst[len("/workspace/"):]
            if not rel or rel.startswith(".."):
                raise RejectedRequest(f"mount target {dst!r} is not inside /workspace")
            if os.path.realpath(os.path.join(live, rel)) != real:
                raise RejectedRequest(f"mount target {dst!r} does not match its source")
        elif norm_dst == real:
            generated_real = {os.path.realpath(os.path.join(live, g)) for g in (generated or [])}
            if real != live_git and "node_modules" not in real.split(os.sep) and real not in generated_real:
                raise RejectedRequest(f"same-path mount {src!r} is neither live's .git, node_modules nor generated code")
        else:
            raise RejectedRequest(f"mount target {dst!r} is not a location the reviewer uses")
        out.append((real, norm_dst))
    return out


def _image_and_env(req: CheckRequest, cfg: dict) -> tuple[str, dict]:
    """Same order as sandbox.js dockerArgs: the check's own stack, then the
    project's sandbox_image, then the project's stack, then the default."""
    stacks = sb._stack_images().get("stacks") or {}
    if req.stack:
        entry = stacks.get(req.stack) or {}
        return str(entry.get("image") or sb.SANDBOX_IMAGE), dict(entry.get("env") or {})
    entry = stacks.get(cfg.get("stack") or "") or {}
    image = cfg.get("sandbox_image") or entry.get("image") or sb.SANDBOX_IMAGE
    return str(image), dict(entry.get("env") or {})


async def _reclaim(req: CheckRequest, image: str) -> None:
    """A killed check never reached its own hand-back (build_docker_argv);
    do it for the worktree from outside."""
    try:
        wt = _checked_worktree(req, _project_live(req.project)[0])
    except Exception:  # noqa: BLE001 -- the request was valid once; nothing to clean if not
        return
    await sb.reclaim(wt, image)


def build_docker_argv(req: CheckRequest, container_name: str) -> tuple[list[str], str]:
    """The full `docker run` argv for one check, and the image it runs in.

    Pure apart from reading the filesystem to validate paths, so the
    hardening is asserted in tests rather than grepped for."""
    live, cfg = _project_live(_no_nul(req.project, "project"))
    wt = _checked_worktree(req, live)
    rel = _checked_rel_dir(req.rel_dir)

    cmd = _no_nul(req.cmd, "cmd")
    if not cmd or cmd.startswith("-") or len(cmd) > 256:
        raise RejectedRequest("cmd must be a program name")
    if not isinstance(req.args, list) or len(req.args) > MAX_ARGS:
        raise RejectedRequest("args must be a list of at most 256 strings")
    args = [_no_nul(a, "arg") for a in req.args]
    if req.network not in ("none", "bridge", CHECKS_NETWORK):
        raise RejectedRequest("network must be 'none' or 'bridge'")

    image, toolchain_env = _image_and_env(req, cfg)
    env: dict[str, str] = {**_BASE_ENV, **toolchain_env}
    for k, v in (req.env or {}).items():
        if not isinstance(k, str) or not _ENV_KEY.match(k):
            raise RejectedRequest(f"env name {k!r} is not a plain identifier")
        env[k] = _no_nul(v, f"env {k}")

    mounts = _checked_mounts(req.mounts or [], live, _generated_dirs(req.project))

    # Files the check leaves in the worktree go back to the agent's user on
    # a Linux host (sandbox.sandbox_owner): sh runs the command, hands
    # root's files back, and exits with the command's code.
    owner = sb.sandbox_owner()
    entry = ["--entrypoint", cmd]
    run_args = list(args)
    if owner:
        entry = ["--entrypoint", "sh"]
        run_args = ["-c", f'"$0" "$@"; rc=$?; {sb.give_back_line(owner)}; exit $rc', cmd, *args]
    argv = [
        "docker", "run", "--rm", "--name", container_name,
        "-v", f"{sb.host_path(wt)}:/workspace",
        *[a for src, dst in mounts for a in ("-v", f"{sb.host_path(src)}:{dst}:ro")],
        "--network", checks_network() if req.network == CHECKS_NETWORK else req.network,
        "-w", "/workspace" if rel == "." else f"/workspace/{rel}",
        "--memory", sb.SANDBOX_MEMORY_LIMIT,
        "--memory-swap", sb.SANDBOX_MEMORY_SWAP,
        "--cpus", sb.SANDBOX_CPU_LIMIT,
        "--pids-limit", sb.SANDBOX_PIDS_LIMIT,
        "--cap-drop", "ALL",
        *sb.owner_args(owner),
        "--security-opt", "no-new-privileges",
        *entry,
        *[a for k, v in env.items() for a in ("-e", f"{k}={v}")],
        image,
        *run_args,
    ]
    return argv, image


def clamp_timeout_ms(value) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = 300_000
    return max(1_000, min(ms, MAX_TIMEOUT_MS))


# --- the probe: what the reviewer asks before it trusts this path (2026-09-27) ---
#
# A host install's reviewer probes docker and the image itself before every
# review and refuses with a SETUP result when either is missing. The delegated
# path had no probe: a missing image became docker's "Unable to find image"
# on the check's own output, which matched no infrastructure pattern, failed
# identically on the base commit, and was filed as pre-existing -- the check
# silently never ran. Now the reviewer asks here first, and a run without the
# image is a refusal, not a check result. The image is built here on demand
# (the same build the entrypoint does at boot) so one failed boot build does
# not leave every review refused until a restart.

logger = logging.getLogger("tektonix")

SANDBOX_CONTEXT = paths.REPO_ROOT / "docker" / "agent-sandbox"
_BUILD_TIMEOUT_S = 1800
_builds: dict[str, asyncio.Task] = {}


async def _docker(*args: str, timeout_s: float = 30, stdin: bytes | None = None) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        result = await asyncio.wait_for(proc.communicate(stdin) if stdin is not None else proc.communicate(),
                                        timeout=timeout_s)
    except (OSError, TimeoutError) as e:
        return False, str(e)
    out = result[0] if result else b""
    return proc.returncode == 0, (out or b"").decode("utf-8", errors="replace").strip()


async def image_present(image: str) -> bool:
    if launcher.enabled():
        return await launcher.image_present(image)
    ok, _ = await _docker("image", "inspect", image, "--format", "{{.Id}}")
    return ok


def _building(image: str) -> bool:
    task = _builds.get(image)
    return task is not None and not task.done()


async def _build(image: str) -> None:
    if launcher.enabled():
        # The launcher builds from the context baked into its own image, in
        # the background; this only asks.
        if await launcher.build_default_image():
            logger.info("review sandbox: the sandbox launcher is building %s", image)
        return
    ok, out = await _docker("build", "-q", "-t", image, str(SANDBOX_CONTEXT), timeout_s=_BUILD_TIMEOUT_S)
    if ok:
        logger.info("review sandbox: built %s", image)
    else:
        logger.error("review sandbox: building %s failed: %s", image, out[-500:])


def start_build(image: str) -> bool:
    """Kick off one build of `image` from the sandbox context, if there is one
    and none is running. True when a build is now in progress."""
    if _building(image):
        return True
    if image != sb.SANDBOX_IMAGE or not (launcher.enabled() or (SANDBOX_CONTEXT / "Dockerfile").is_file()):
        return False
    _builds[image] = asyncio.get_running_loop().create_task(_build(image))
    return True


async def probe(image: str | None = None) -> dict:
    """Whether a check could run here right now: docker reachable and the
    image present. {ok, mode, image, docker, reason} in sandbox.js's terms."""
    image = image or sb.SANDBOX_IMAGE
    if launcher.enabled():
        ok, version = await launcher.docker_version()
    else:
        ok, version = await _docker("version", "--format", "{{.Server.Version}}")
    if not ok:
        return {"ok": False, "mode": "unavailable", "image": image, "docker": None,
                "reason": f"docker is not usable from the agent: {version[:200]}"}
    if not await image_present(image):
        building = start_build(image)
        reason = (f"the sandbox image {image} is not built; the agent is building it now, so try again "
                  f"in a few minutes" if building else
                  f"the sandbox image {image} is not built, and the agent has no docker/agent-sandbox "
                  f"context to build it from")
        return {"ok": False, "mode": "unavailable", "image": image, "docker": version, "reason": reason}
    return {"ok": True, "mode": "delegated", "image": image, "docker": version,
            "reason": f"{image} on docker {version}"}


def _refusal(image: str, why: str) -> dict:
    return {"ok": False, "code": 1, "infrastructure": True, "image": image,
            "output": f"SETUP: this check could not be sandboxed by the agent -- {why}. It was not run, "
                      f"and nothing about the code under review is known either way."}


async def run_check(req: CheckRequest) -> dict:
    """Run one check; {ok, code, output, image} in sandbox.js's own shape."""
    name = f"rvw-{uuid.uuid4().hex[:12]}"
    argv, image = build_docker_argv(req, name)
    timeout_s = clamp_timeout_ms(req.timeout_ms) / 1000
    logging.getLogger("tektonix").info("review-sandbox: running %s for %s in %s (%ds allowed)",
                                       " ".join([req.cmd, *req.args])[:120], req.project, name, int(timeout_s))
    if not await image_present(image):
        building = start_build(image)
        return _refusal(image, f"the sandbox image {image} is not built"
                        + (" (the agent is building it now)" if building else ""))
    if launcher.enabled():
        return await _run_check_via_launcher(req, argv, name, image, timeout_s)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return {"ok": False, "code": 1, "infrastructure": True, "image": image,
                "output": f"SETUP: the agent could not start docker: {e}"}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        await sb._kill_container(name)
        await proc.wait()
        await _reclaim(req, image)
        logging.getLogger("tektonix").warning("review-sandbox: %s %s timed out after %ds", req.project, req.cmd, int(timeout_s))
        # Flagged as the harness's failure: a check that did not finish says
        # nothing about the code, and counted as a plain failure it was
        # re-run on the base commit, timed out there too, and waved through
        # as pre-existing (2026-09-29).
        return {"ok": False, "code": 124, "image": image, "infrastructure": True,
                "output": f"SETUP: the check timed out after {int(timeout_s)}s and was stopped; nothing is known "
                          f"about the code either way. Raise this check's timeoutMs in the project's review "
                          f"settings if the suite genuinely needs longer."}
    except asyncio.CancelledError:
        await sb._kill_container(name)
        await proc.wait()
        await _reclaim(req, image)
        raise
    text = (out or b"").decode("utf-8", errors="replace")[-MAX_OUTPUT_CHARS:]
    return {"ok": proc.returncode == 0, "code": proc.returncode, "output": text, "image": image}


async def _run_check_via_launcher(req: CheckRequest, argv: list[str], name: str, image: str,
                                  timeout_s: float) -> dict:
    """run_check's tail in the bundle: the same argv, started by the sandbox
    launcher, with the same timeout and cancellation handling."""
    try:
        code, text, timed_out = await launcher.run(argv, timeout_s)
    except launcher.LauncherError as e:
        return {"ok": False, "code": 1, "infrastructure": True, "image": image, "output": f"SETUP: {e}"}
    except asyncio.CancelledError:
        await sb._kill_container(name)
        await _reclaim(req, image)
        raise
    if timed_out:
        await _reclaim(req, image)
        logger.warning("review-sandbox: %s %s timed out after %ds", req.project, req.cmd, int(timeout_s))
        return {"ok": False, "code": 124, "image": image, "infrastructure": True,
                "output": f"SETUP: the check timed out after {int(timeout_s)}s and was stopped; nothing is known "
                          f"about the code either way. Raise this check's timeoutMs in the project's review "
                          f"settings if the suite genuinely needs longer."}
    return {"ok": code == 0, "code": code, "output": text[-MAX_OUTPUT_CHARS:], "image": image}


# --- running a project's database checks in the sandbox ----------------------------

@dataclass
class DbCheckRequest:
    project: str
    worktree: str
    mounts: list[tuple[str, str]] = field(default_factory=list)
    stack: str | None = None


def validate_database_check(req: DbCheckRequest) -> None:
    """The refusals a caller can be told about before the answer starts:
    the project and worktree checks run_database_check would make first.
    The route calls this so a bad request is still a 400, not a streamed
    setup row (2026-09-29 audit, R5)."""
    live, _cfg = _project_live(_no_nul(req.project, "project"))
    _checked_worktree(req, live)


def _parse_dsn(url: str) -> dict:
    """user, password, host, port of a postgresql:// URL. No library: the URL
    is server config and the pieces go into a URL the checks are given."""
    m = re.match(r"^postgres(?:ql)?://([^:/@]+)(?::([^@]*))?@([^:/]+)(?::(\d+))?(?:/([^?]*))?", url)
    if not m:
        raise RejectedRequest(f"{CHECKS_POSTGRES_ENV} is not a postgresql:// URL")
    return {"user": m.group(1), "password": m.group(2) or "", "host": m.group(3),
            "port": int(m.group(4) or 5432), "db": m.group(5) or "postgres"}


def check_env(throwaway_db: str, role: str, password: str, redis_db: int) -> dict[str, str]:
    """What the three commands see: the throwaway DSN as its own plain role,
    the run's Redis database and freshly generated secrets -- built for the
    run, as the host path builds its own (services/commit-reviewer/checks.js),
    never inherited. The server's superuser and its password never appear."""
    pg = _parse_dsn(os.environ[CHECKS_POSTGRES_ENV])
    redis = os.environ[CHECKS_REDIS_ENV].rstrip("/")
    return {
        "DATABASE_URL": f"postgresql://{role}:{password}@{pg['host']}:{pg['port']}/{throwaway_db}?schema=public",
        "REDIS_URL": f"{redis}/{redis_db}",
        "JWT_ACCESS_SECRET": secrets.token_hex(32),
        "SECRETS_ENCRYPTION_KEY": secrets.token_hex(32),
        "CORS_ORIGIN_STOREFRONT": "http://localhost:5173",
        "CORS_ORIGIN_ADMIN": "http://localhost:5174",
        "ORDER_NOTIFY_EMAIL": "orders@example.test",
    }


async def _admin_sql(sql: str) -> None:
    """One statement on the checks server's maintenance database, run by
    psql INSIDE the postgres container over `docker exec`: the superuser on
    its own unix socket, which is trusted locally. The agent opens no
    connection and holds no password, and needs no route to the server."""
    pg = _parse_dsn(os.environ[CHECKS_POSTGRES_ENV])
    if launcher.enabled():
        ok, out = await launcher.checks_psql(sql, pg["user"], pg["db"])
    else:
        ok, out = await _docker("exec", "-i", os.environ[CHECKS_POSTGRES_CONTAINER_ENV],
                                "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", pg["user"], "-d", pg["db"],
                                stdin=sql.encode(), timeout_s=60)
    if not ok:
        raise RuntimeError(out[-300:] or "psql failed")


async def _admin_rows(sql: str) -> list[str]:
    """One query the same way, its rows back one per line (psql -At)."""
    pg = _parse_dsn(os.environ[CHECKS_POSTGRES_ENV])
    if launcher.enabled():
        ok, out = await launcher.checks_psql(sql, pg["user"], pg["db"], rows=True)
    else:
        ok, out = await _docker("exec", "-i", os.environ[CHECKS_POSTGRES_CONTAINER_ENV],
                                "psql", "-v", "ON_ERROR_STOP=1", "-At", "-U", pg["user"], "-d", pg["db"],
                                stdin=sql.encode(), timeout_s=60)
    if not ok:
        raise RuntimeError(out[-300:] or "psql failed")
    return [line.strip() for line in out.splitlines() if line.strip()]


# The throwaway role and database of every run in flight, so a sweep never
# drops what a check is using.
_THROWAWAY_PREFIX = "tektonix_ci_review_"
_THROWAWAY_RE = re.compile(r"^tektonix_ci_review_[0-9a-f]{8}$")
_in_flight: set[str] = set()


async def sweep_orphans() -> list[str]:
    """Drop the throwaway roles and databases a crashed agent left behind.

    Each run drops its own in a `finally`, but an agent that dies mid-run
    (a restart, an OOM) never reaches it, and until checks-postgres itself
    restarted the leftovers stayed: connectable by later runs' roles, and
    named for nothing. Run at startup, when nothing this process started can
    be in flight; a name in `_in_flight` is spared all the same. Never
    raises: an unreachable checks server is the probe's problem."""
    if not db_checks_enabled():
        return []
    swept: list[str] = []
    try:
        dbs = await _admin_rows(f"SELECT datname FROM pg_database WHERE datname LIKE '{_THROWAWAY_PREFIX}%';")
        roles = await _admin_rows(f"SELECT rolname FROM pg_roles WHERE rolname LIKE '{_THROWAWAY_PREFIX}%';")
    except Exception as e:  # noqa: BLE001 -- reported, never raised at startup
        logger.warning("review db check: could not list leftover throwaway databases: %s", e)
        return []
    for name in [*dbs, *roles]:
        # Hex names only, made here: anything else is not ours to drop and
        # would not be safe to splice into SQL.
        if not _THROWAWAY_RE.match(name) or name in _in_flight or name in swept:
            continue
        try:
            await _admin_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")
            await _admin_sql(f"DROP ROLE IF EXISTS {name};")
            swept.append(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("review db check: leftover throwaway %s not dropped: %s", name, e)
    if swept:
        logger.info("review db check: dropped %d throwaway database(s) left by an earlier run", len(swept))
    return swept


async def _flush_redis(db: int) -> None:
    """FLUSHDB on the run's database, by redis-cli inside the redis container."""
    if launcher.enabled():
        ok, out = await launcher.checks_redis_flush(db)
    else:
        ok, out = await _docker("exec", os.environ[CHECKS_REDIS_CONTAINER_ENV], "redis-cli", "-n", str(db),
                                "FLUSHDB", timeout_s=30)
    if not ok or out.strip() != "OK":
        raise RuntimeError(f"redis answered {out[-200:]!r}")


# One Redis database per run in flight. Two reviews at once used to share
# database 15: one's flush emptied the other's keys mid-run. The pool is
# this process's; the agent is the only thing that starts these runs.
_redis_pool: dict = {"loop": None, "cond": None, "free": set(range(CHECKS_REDIS_DATABASES))}


def _redis_cond() -> asyncio.Condition:
    loop = asyncio.get_running_loop()
    if _redis_pool["loop"] is not loop:
        _redis_pool["loop"], _redis_pool["cond"] = loop, asyncio.Condition()
    return _redis_pool["cond"]


async def _take_redis_db() -> int:
    cond = _redis_cond()
    async with cond:
        while not _redis_pool["free"]:
            await cond.wait()
        return _redis_pool["free"].pop()


async def _give_back_redis_db(db: int) -> None:
    cond = _redis_cond()
    async with cond:
        _redis_pool["free"].add(db)
        cond.notify()


def _setup_failure(what: str) -> list[dict]:
    return [{"name": "db-setup", "ok": False, "infrastructure": True,
             "output": f"SETUP: the database checks could not be set up by the agent -- {what}. They were not "
                       f"run, and nothing about the code under review is known either way."}]


async def run_database_check(req: DbCheckRequest) -> list[dict]:
    """The host path's runDatabaseCheck, in the sandbox: a plain role and a
    database it owns created on checks-postgres, the run's Redis database
    flushed, then db-drift, db-seed and e2e in order in the checks container,
    stopping at the first failure, and the database and role dropped whatever
    happened. Rows in the same shape the host path returns."""
    if not db_checks_enabled():
        return _setup_failure(", ".join(_CHECKS_ENVS) + " are not all set")
    live, cfg = _project_live(req.project)
    dc = cfg.get("databaseCheck")
    if not dc:
        return []
    worktree = _checked_worktree(CheckRequest(project=req.project, worktree=req.worktree, cmd="x"), live)
    api_dir = _checked_rel_dir(str(dc.get("apiDir") or "."))
    steps = []
    for name, key, timeout_ms in _DB_STEPS:
        spec = dc.get(key) or {}
        cmd = _no_nul(str(spec.get("cmd") or ""), key)
        if not cmd:
            raise RejectedRequest(f"databaseCheck.{key} has no cmd")
        steps.append((name, cmd, [str(a) for a in (spec.get("args") or [])], timeout_ms))
    image = None
    # One name for the role and its database; hex only, so it needs no quoting
    # in SQL and the password (hex too) needs none in the DSN.
    throwaway = f"tektonix_ci_review_{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    redis_db = await _take_redis_db()
    env = check_env(throwaway, throwaway, password, redis_db)
    results: list[dict] = []
    _in_flight.add(throwaway)
    try:
        try:
            await _admin_sql(f"CREATE ROLE {throwaway} LOGIN PASSWORD '{password}' "
                             f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;")
            await _admin_sql(f"CREATE DATABASE {throwaway} OWNER {throwaway} TEMPLATE template0;")
            # Postgres grants CONNECT on every new database to PUBLIC, so one
            # run's role could open another run's database (and read nothing
            # it does not own, but a connection is a foothold). Only the
            # owner keeps it.
            await _admin_sql(f"REVOKE CONNECT ON DATABASE {throwaway} FROM PUBLIC;")
        except Exception as e:  # noqa: BLE001 -- the reviewer needs the reason, not a stack trace
            return _setup_failure(f"could not create the throwaway database: {str(e)[:300]}")
        try:
            await _flush_redis(redis_db)
        except Exception as e:  # noqa: BLE001
            return _setup_failure(f"could not reach the checks redis: {str(e)[:300]}")
        for name, cmd, args, timeout_ms in steps:
            r = await run_check(CheckRequest(
                project=req.project, worktree=worktree, cmd=cmd, args=args, rel_dir=api_dir,
                env=env, network=CHECKS_NETWORK, stack=req.stack, mounts=list(req.mounts),
                timeout_ms=timeout_ms))
            image = r.get("image", image)
            row = {"name": name, "ok": bool(r.get("ok")), "code": r.get("code"),
                   "output": str(r.get("output") or "")[-4000:]}
            if r.get("infrastructure"):
                row["infrastructure"] = True
            results.append(row)
            if not row["ok"]:
                break
        return results
    finally:
        try:
            await _admin_sql(f"DROP DATABASE IF EXISTS {throwaway} WITH (FORCE);")
            await _admin_sql(f"DROP ROLE IF EXISTS {throwaway};")
        except Exception as e:  # noqa: BLE001 -- a leaked throwaway is logged, never raised over a result
            logger.warning("review db check: throwaway database %s not dropped: %s", throwaway, e)
        _in_flight.discard(throwaway)
        await _give_back_redis_db(redis_db)
