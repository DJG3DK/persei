"""Sandboxed shell execution for the agent's own bash/write/edit/read
tools -- not for the deterministic checks/git operations (agent/tools/
checks.py, agent/tools/git.py), which stay on the host unchanged since
those are fixed commands our code runs, never LLM-chosen, and already have
no isolation concern (nothing an LLM decided goes into them).

Running bash/write/edit directly on the same host as the agent server,
against real repo checkouts, is a real risk: a context-injected or
badly-confused agent could otherwise read or touch anything outside its
intended repo -- another project's live secrets on the same host, this
project's own .env, other tooling config directories, etc. Docker
containers, bind-mounting only the target repo's sandbox checkout, close
that specific risk. Local
containers were chosen over a remote sandbox provider specifically to avoid
a new third-party account/credentials. Network access stays enabled
(package installs still need it) -- the isolation boundary here is
filesystem, not network, which is still a meaningful improvement over no
boundary at all, not a claim of complete sandboxing.

Same {"ok", "exit_code", "output"} / ShellTimeout contract as
agent/tools/shell.py's run_shell, so agent_tools.py's tool functions barely
change to use this instead.
"""

import asyncio
from agent import runtime_settings as _rs
import logging
import os
import re
import socket
import contextlib
import uuid

from agent.tools import sandbox_launcher as launcher
from agent.tools.shell import GIT_IDENTITY_VARS, ShellTimeout

logger = logging.getLogger("tektonix")

# Every `-v` source below is resolved by the DOCKER DAEMON, not by this
# process -- which is the same thing while the agent runs directly on the
# host, and stops being the same thing the moment it runs in a container with
# the host's Docker socket mounted (docs/roadmap-packaging.md, M1). There the
# sandbox containers are SIBLINGS created by the host daemon: a `-v` naming a
# path that exists only inside the agent container silently mounts an empty
# directory, and the task reads an empty repo with no error anywhere.
#
# So the mapping is explicit. AGENT_HOST_PATH_MAP is a colon-free,
# comma-separated list of `container_prefix=host_prefix` pairs; unset (the
# normal single-host install) means the identity mapping, which is exactly
# today's behaviour.
#
# The compose bundle no longer needs it: the agent there has no socket, and
# the sandbox launcher (agent/tools/sandbox_launcher.py) is handed paths as
# the agent sees them and maps its fixed root to PROJECTS_DIR itself. So in
# the bundle host_path is the identity whatever the map says -- a host path
# would name nothing inside the launcher's root, and be refused.
#
#   AGENT_HOST_PATH_MAP=/projects=C:\dev,/workspaces=D:\work
_HOST_PATH_MAP_ENV = "AGENT_HOST_PATH_MAP"


def _host_path_map() -> list[tuple[str, str]]:
    """Parsed prefix pairs, longest container prefix first so a nested mapping
    wins over the parent it sits under."""
    raw = os.environ.get(_HOST_PATH_MAP_ENV, "").strip()
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        container, host = entry.split("=", 1)
        container, host = container.strip().rstrip("/"), host.strip()
        if container and host:
            pairs.append((container, host.rstrip("/\\")))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def host_path(path: str) -> str:
    """The path the Docker daemon should be given for a bind mount.

    Identity unless AGENT_HOST_PATH_MAP says otherwise. Only whole path
    SEGMENTS match, so a prefix of "/projects" never rewrites "/projects-old".
    """
    if not path or launcher.enabled():
        return path
    for container, host in _host_path_map():
        if path == container:
            return host
        if path.startswith(container + "/"):
            rest = path[len(container) + 1:]
            sep = "\\" if ("\\" in host or ":" in host[:2]) else "/"
            return f"{host}{sep}{rest.replace('/', sep)}"
    return path


SANDBOX_IMAGE = "tektonix-sandbox:latest"  # built from docker/agent-sandbox/Dockerfile


# Git needs an identity to commit, and a sandbox container has none of its
# own. The bundle sets these on the agent (docker-compose.yml, from
# ADMIN_EMAIL); a host install has the operator's global git config, which
# the sandbox cannot see either. Passed through when set, so `git commit`
# the model runs in the sandbox works in both. The names are shell.py's
# GIT_IDENTITY_VARS: the same four reach a host-side git command.
def git_identity_args() -> list[str]:
    args: list[str] = []
    for k in GIT_IDENTITY_VARS:
        v = os.environ.get(k, "").strip()
        if v:
            args += ["-e", f"{k}={v}"]
    return args
# The caps a check or a shell command runs under. Two cores and two
# gigabytes fit the shared host; a desktop install with a machine to itself
# sets SANDBOX_CPUS and SANDBOX_MEMORY in its .env (the app writes them from
# the machine's size). 2026-09-29: a suite that takes five minutes here took
# over ten in a two-core sandbox under Docker Desktop, past every timeout.
def _cap(env: str, default: str, pattern: str) -> str:
    value = os.environ.get(env, "").strip()
    return value if re.fullmatch(pattern, value) else default


SANDBOX_MEMORY_LIMIT = _cap("SANDBOX_MEMORY", "2g", r"[1-9]\d*[gmGM]")
SANDBOX_CPU_LIMIT = _cap("SANDBOX_CPUS", "2", r"[1-9]\d?(\.\d+)?")
# No swap on top of the memory limit. Unset, docker gives a container as much
# swap again, so an oversized test run thrashes the host's disk for minutes
# before it is killed -- 31 such kills in two benchmark runs (2026-09-24),
# with the host's IO "full" pressure at 22-35% while they lasted. Failing fast
# is kinder to everything else on the box.
SANDBOX_MEMORY_SWAP = SANDBOX_MEMORY_LIMIT
# Test runners that size their worker pool from the HOST's core count see 32
# cores inside a 2-CPU, 2 GB container: Django's runtests started 32 workers
# and ran out of memory. Told the container's own size instead -- as a whole
# number: docker takes `--cpus 1.5`, Django parses this with int() and a
# fractional cap crashed every Django run in the sandbox (2026-09-29 audit).
SANDBOX_TEST_ENV = {"DJANGO_TEST_PROCESSES": str(max(1, int(float(SANDBOX_CPU_LIMIT))))}
# audit M-10: cap process count to blunt a fork bomb, drop all Linux
# capabilities (git/npm/node need none), and forbid privilege escalation.
SANDBOX_PIDS_LIMIT = "512"


async def _kill_container(name: str) -> None:
    # `docker run --rm`'s own cleanup only fires on a normal exit -- killing
    # the `docker run` CLI process (the asyncio subprocess we actually hold
    # a handle to) does not reliably stop the container itself; without this,
    # a timed-out or cancelled command would leave the container (and
    # whatever it's running) orphaned, running detached on the host --
    # exactly the same class of bug run_shell's own _kill_process_group
    # exists to prevent, just one layer up (container instead of process
    # group). `docker kill` + `--rm` together tear it down completely.
    if launcher.enabled():
        await launcher.kill(name)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:  # noqa: BLE001 -- best-effort cleanup, never worth failing the caller over
        pass


def _mount_allow_roots(cwd: str) -> list[str]:
    """Host paths a bind mount for this workspace may point at.

    Both mount computations below read state OUT of the worktree -- a
    node_modules symlink, and the .git pointer file -- and the worktree is
    bind-mounted read-write for the agent. Left unchecked that lets the agent
    choose what the host mounts into its own container: `ln -s /home
    <ws>/node_modules` yields `-v /home:/home:ro`, and a rewritten .git
    pointer yields `-v /root:/root:ro`. That is read access to every other
    project's .env, this agent's own secrets and ~/.ssh -- exactly what the
    module docstring claims to prevent.

    So a target is only accepted if it resolves under the workspace itself or
    under the LIVE checkout of the project this workspace belongs to, both of
    which come from server-owned config rather than from the worktree.

    An unconfigured workspace (a test fixture, a one-off checkout) gets the
    workspace alone -- fail closed, since there is no trusted second path to
    compare against.
    """
    roots = [os.path.realpath(cwd)]
    found = _project_of(cwd)
    if found:
        cfg = found[1]
        live = cfg.get("live")
        if live:
            roots.append(os.path.realpath(live))
        # A task's own workspace (agent/workspaces.py) is filled from the
        # project's, and a dependency symlink copied from there may point
        # into it -- the same server-owned tree the task was made from.
        template = cfg.get("sandbox")
        if template and os.path.realpath(template) != roots[0]:
            roots.append(os.path.realpath(template))
    return roots


def _project_of(cwd: str):
    """(name, config) of the project whose workspace -- its own, or one of
    its tasks' -- `cwd` is. Decided from server-owned config only."""
    try:
        from agent.workspaces import project_for_path
        return project_for_path(cwd)
    except Exception:  # noqa: BLE001 -- config unavailable (tests); workspace-only
        return None


def _stack_images() -> dict:
    """docker/stack-images.json -- the one map of toolchain -> image the
    reviewer and onboarding already read. Missing means default only."""
    import json
    from agent import paths
    try:
        return json.loads((paths.REPO_ROOT / "docker" / "stack-images.json").read_text())
    except (OSError, ValueError):
        return {}


def sandbox_environment_for(cwd: str) -> tuple[str, dict]:
    """The image a command in this workspace runs in, and the env its
    toolchain needs.

    In order: the project's own `sandbox_image` from projects.json, then the
    image of its `stack` (docker/stack-images.json, the map the reviewer
    already runs checks with), then the shared image. A project whose
    toolchain the shared image lacks -- a Python version, a system library, a
    whole other language -- gets an environment of its own, and nothing else
    changes: same mounts, same limits, same hardening."""
    found = _project_of(cwd)
    cfg = found[1] if found else {}
    stack = (_stack_images().get("stacks") or {}).get(cfg.get("stack") or "") or {}
    image = cfg.get("sandbox_image") or stack.get("image") or SANDBOX_IMAGE
    return str(image), dict(stack.get("env") or {})


def sandbox_image_for(cwd: str) -> str:
    return sandbox_environment_for(cwd)[0]


_MOUNT_POINT = __import__("re").compile(r"^/[A-Za-z0-9._/-]{1,200}$")


def sandbox_layout_for(cwd: str) -> dict:
    """Project options for WHERE and HOW a command runs, from projects.json.

    For a project whose image expects its code somewhere in particular --
    SWE-bench's images have it installed, editable, at /testbed, with compiled
    parts built there -- and an environment to switch on first:

      sandbox_mounts:     further container paths the workspace is mounted at
                          (it is always at /workspace)
      sandbox_shell_init: run before every command, e.g. activating a conda env
      sandbox_network:    forced for every command, e.g. "none" -- for a
                          benchmark, where a shell with the internet can look
                          up the published fix
      sandbox_readonly_mounts: {container path: host path}, mounted read-only
                          -- a benchmark's untouched tree at /baseline, so an
                          agent can run the code as it was before its change
                          (the sandbox's .git is read-only: git stash cannot)

    Server-owned config, never the workspace's: a mount point is validated as
    an absolute path and may not shadow the container's own system paths."""
    found = _project_of(cwd)
    cfg = found[1] if found else {}
    forbidden = ("/", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev",
                 "/root", "/opt", "/workspace", "/tmp", "/var")
    mounts = [m for m in (cfg.get("sandbox_mounts") or [])
              if isinstance(m, str) and _MOUNT_POINT.match(m) and ".." not in m
              and m.rstrip("/") not in forbidden]
    init = cfg.get("sandbox_shell_init") or ""
    network = cfg.get("sandbox_network") or None
    ro = cfg.get("sandbox_readonly_mounts") or {}
    readonly = [(str(src), tgt) for tgt, src in (ro.items() if isinstance(ro, dict) else [])
                if isinstance(tgt, str) and _MOUNT_POINT.match(tgt) and ".." not in tgt
                and tgt.rstrip("/") not in forbidden and tgt not in mounts
                and isinstance(src, str) and os.path.isabs(src) and ".." not in src and os.path.isdir(src)]
    return {"mounts": mounts, "init": str(init) if isinstance(init, str) else "",
            "network": str(network) if network in ("none", "bridge") else None,
            "readonly": readonly}


def sandbox_owner() -> str | None:
    """`uid:gid` that files a sandbox leaves in the workspace are handed back
    to, or None for nobody.

    A sandbox runs as root, because a toolchain image installs into its own
    system paths. On a Linux host everything it creates in the bind-mounted
    worktree -- build output, node_modules, caches -- is then root's, and the
    agent, running as PUID (docker/agent/entrypoint.sh, step 5), can neither
    rewrite nor remove it: the next checkout, merge or worktree cleanup fails
    on a permission error. Docker Desktop's file sharing ignores ownership,
    so there PUID is unset and nothing here changes (2026-10-04, the Linux
    desktop app)."""
    uid = os.environ.get("PUID", "").strip()
    gid = os.environ.get("PGID", "").strip()
    if not (uid.isdigit() and gid.isdigit()) or uid == "0":
        return None
    return f"{uid}:{gid}"


def give_back_line(owner: str, root: str = "/workspace") -> str:
    """A shell line handing root's files under `root` to `owner`. Only what
    root owns is touched, so a pass over an untouched tree changes nothing."""
    return (f"find {root} -xdev \\( -uid 0 -o -gid 0 \\) "
            f"-exec chown -h {owner} {{}} + 2>/dev/null")


def owner_args(owner: str | None) -> list[str]:
    """The file capabilities a root sandbox needs in a workspace it does not
    own, only when there is an owner (a Linux host with PUID set). With all
    capabilities dropped, root is just another user to the kernel's
    permission checks: it could not create a file in the operator's own
    worktree at all (found 2026-10-04 -- every Linux bundle install with
    PUID set). DAC_OVERRIDE to write there, FOWNER to chmod the operator's
    files, CHOWN to hand its own back. All three are in Docker's default
    set; the rest stay dropped."""
    if not owner:
        return []
    return ["--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER"]


def _shell(cmd: str, owner: str | None = None) -> list[str]:
    """bash where the image has it, sh where it does not.

    The shared image has bash and every command the agent writes assumes it.
    A toolchain image from a stack may not -- the Alpine ones ship only sh --
    and `bash -c` there fails every command before it starts. sh is in every
    image, so it starts the command and hands it to bash when bash exists.

    With an owner (sandbox_owner) the command is not exec'd: sh waits for it,
    hands root's files back, and exits with the command's own code."""
    pick = 'if command -v bash >/dev/null 2>&1; then {x}bash -c "$0"; else {x}sh -c "$0"; fi'
    if not owner:
        return ["sh", "-c", pick.format(x="exec "), cmd]
    return ["sh", "-c", f"{pick.format(x='')}; rc=$?; {give_back_line(owner)}; exit $rc", cmd]


async def reclaim(cwd: str, image: str | None = None) -> None:
    """Hand root's files in `cwd` back after a command that never got to do
    it itself: killed on a timeout, or by the operator's Stop. Best effort,
    a minute at most; nothing to do without an owner."""
    owner = sandbox_owner()
    if not owner:
        return
    argv = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
            *owner_args(owner), "--security-opt", "no-new-privileges",
            "-v", f"{host_path(cwd)}:/workspace", image or sandbox_image_for(cwd),
            "sh", "-c", give_back_line(owner)]
    if launcher.enabled():
        try:
            await launcher.run(argv, 60)
        except launcher.LauncherError as e:
            logger.warning("could not hand files in %s back to %s: %s", cwd, owner, e)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=60)
    except Exception:  # noqa: BLE001 -- cleanup; the caller's own outcome stands
        logger.warning("could not hand files in %s back to %s", cwd, owner)


def _mount_target_allowed(target: str, allow_roots: list[str]) -> bool:
    real = os.path.realpath(target)
    return any(real == r or real.startswith(r + os.sep) for r in allow_roots)


def _node_modules_mounts(cwd: str) -> list[str]:
    """audit C-2: some project worktrees symlink their `node_modules` (and, in a
    monorepo, nested ones) to the LIVE checkout's node_modules OUTSIDE the
    worktree -- e.g. a Next.js project'
    `<worktree>/node_modules -> /home/a Next.js project/node_modules`. Mounting only
    the worktree leaves that symlink dangling inside the container, so `npm run
    lint` fails with "eslint: not found" even though it passes on the host. For
    each such symlink whose target lives outside `cwd`, bind-mount the target
    read-only at its own absolute path so the symlink resolves. Read-only: the
    check phase must never mutate the live install. Real (non-symlink)
    node_modules dirs are already covered by the /workspace mount and need
    nothing here.
    """
    mounts: list[str] = []
    seen: set[str] = set()
    real_cwd = os.path.realpath(cwd)
    allow_roots = _mount_allow_roots(cwd)
    for root, dirs, _files in os.walk(cwd):
        # don't descend into the store internals; one level of nesting is enough
        # for the monorepo app/package node_modules layout.
        depth = root[len(cwd):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
        if "node_modules" in dirs:
            nm = os.path.join(root, "node_modules")
            if os.path.islink(nm):
                target = os.path.realpath(nm)
                # only mount if it points OUTSIDE the worktree and actually exists
                if target and target not in seen \
                        and os.path.isdir(target) \
                        and not target.startswith(real_cwd + os.sep):
                    # audit C2: the symlink is agent-writable, so its target is
                    # an agent-chosen mount unless it is checked against
                    # server-owned paths.
                    if not _mount_target_allowed(target, allow_roots):
                        logger.warning(
                            "refusing to mount %s for %s: outside this project's "
                            "workspace and live checkout", target, cwd)
                        continue
                    seen.add(target)
                    mounts += ["-v", f"{host_path(target)}:{target}:ro"]
            # never walk into a node_modules tree
            dirs.remove("node_modules")
    return mounts


async def run_shell_sandboxed(
    cmd: str,
    cwd: str,
    # None means "use the operator-set default"; an explicit value from the
    # call site still wins, so the long-running checks keep their own caps.
    timeout: int | None = None,
    extra_env: dict | None = None,
    network: str | None = None,
) -> dict:
    """Runs `cmd` inside an ephemeral Docker container with only `cwd`
    (the repo sandbox checkout) bind-mounted, read-write, at /workspace --
    no other host path is visible inside the container. No TTY, CI=true,
    hard timeout -- same non-interactive-by-construction discipline as
    run_shell (see that module's own docstring on why: a pty-based shell tool
    can hang forever on an unanswered interactive prompt).

    `network` maps to docker's --network (e.g. "none" for full network
    isolation -- used by the deterministic check runner, whose deps are already
    installed so it needs no egress; audit C-2/M-10). None keeps the default
    bridge, which is what the interactive `bash` tool uses.

    Returns {"ok": bool, "exit_code": int, "output": str} -- same shape as
    run_shell, so callers don't need to branch on which one they used.
    """
    container_name = f"lga-{uuid.uuid4().hex[:12]}"
    image, toolchain_env = sandbox_environment_for(cwd)
    layout = sandbox_layout_for(cwd)
    if layout["network"]:
        network = layout["network"]          # the project's rule wins over the call site's
    if layout["init"]:
        cmd = f"{layout['init']}\n{cmd}"
    env_args = []
    for k, v in {"CI": "true", "DEBIAN_FRONTEND": "noninteractive", **toolchain_env,
                 **(extra_env or {})}.items():
        env_args += ["-e", f"{k}={v}"]

    # The workspace is a git WORKTREE of the live repo (see the 2026-08-25
    # workspace migration), and a worktree's .git is not a directory but a one-
    # line file: `gitdir: <live>/.git/worktrees/<name>`. Mounting only the
    # worktree therefore broke every git command inside the container — status,
    # diff, log all died with "fatal: not a git repository" because the pointer
    # target didn't exist in the mount namespace. It went unnoticed because the
    # deterministic git operations (agent/tools/git.py) run on the HOST; only
    # LLM-issued `git ...` in the bash tool was affected.
    #
    # Fix: when cwd is a worktree, also mount the live repo's .git at the SAME
    # absolute path, read-only. Same path, because that's what the pointer file
    # names; read-only, because the sandbox runs unreviewed model commands and
    # must not be able to rewrite the live repository's history — commits happen
    # on the host through verify_and_ship, never in here. Read operations work;
    # an in-sandbox `git commit` fails on the read-only mount, which is correct.
    git_mount_args: list[str] = []
    dotgit = os.path.join(cwd, ".git")
    if os.path.isfile(dotgit):
        try:
            with open(dotgit) as fh:
                gitdir = fh.read().strip().removeprefix("gitdir:").strip()
            # <live>/.git/worktrees/<name>  ->  <live>/.git
            marker = f"{os.sep}worktrees{os.sep}"
            if marker in gitdir:
                main_git = gitdir[: gitdir.index(marker)]
                # audit C2: this path comes from a file inside the worktree,
                # which the agent can rewrite -- `gitdir: /root/x` would mount
                # /root read-only into the container. Same allow-root check the
                # node_modules mounts get.
                if os.path.isdir(main_git) and _mount_target_allowed(
                        main_git, _mount_allow_roots(cwd)):
                    git_mount_args = ["-v", f"{host_path(main_git)}:{main_git}:ro"]
                elif os.path.isdir(main_git):
                    logger.warning(
                        "refusing to mount git dir %s for %s: outside this "
                        "project's workspace and live checkout", main_git, cwd)
        except OSError:
            pass  # unreadable pointer file: run without git rather than not at all

    nm_mount_args = _node_modules_mounts(cwd)
    net_args = ["--network", network] if network else []
    owner = sandbox_owner()

    docker_args = [
        "docker", "run", "--rm", "--name", container_name,
        "-v", f"{host_path(cwd)}:/workspace",
        *[a for m in layout["mounts"] for a in ("-v", f"{host_path(cwd)}:{m}")],
        *[a for src, tgt in layout.get("readonly", []) for a in ("-v", f"{host_path(src)}:{tgt}:ro")],
        *git_mount_args,
        *nm_mount_args,
        *net_args,
        "-w", "/workspace",
        "--memory", SANDBOX_MEMORY_LIMIT,
        "--memory-swap", SANDBOX_MEMORY_SWAP,
        "--cpus", SANDBOX_CPU_LIMIT,
        *[a for k, v in SANDBOX_TEST_ENV.items() for a in ("-e", f"{k}={v}")],
        *git_identity_args(),
        # audit M-10 hardening. --user is deliberately NOT set: the bind-mounted
        # worktree is root-owned on the host (pm2 runs as root), so a non-root
        # container user could not write to it. Network stays on the default
        # bridge (a dedicated network blocking link-local/RFC1918 and binding
        # the model router to 127.0.0.1 is tracked as residual infra) -- but
        # MODEL_ROUTER_KEY is not passed in, so the reachable router surface is
        # authenticated, not open.
        "--pids-limit", SANDBOX_PIDS_LIMIT,
        "--cap-drop", "ALL",
        *owner_args(owner),
        "--security-opt", "no-new-privileges",
        *env_args,
        image,
        *_shell(cmd, owner),
    ]
    _timeout = timeout if timeout is not None else _rs.as_int("sandbox_command_timeout_s")
    if launcher.enabled():
        return await _run_via_launcher(docker_args, container_name, cwd, image, cmd, _timeout)
    proc = await asyncio.create_subprocess_exec(
        *docker_args,
        stdin=asyncio.subprocess.DEVNULL,  # no stdin at all -- matches run_shell's own reasoning
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_timeout)
    except TimeoutError:
        await _kill_container(container_name)
        await proc.wait()
        await reclaim(cwd, image)
        # _timeout, not timeout: the latter is None when the caller took the
        # operator default, which would report "timed out after None seconds".
        raise ShellTimeout(cmd, _timeout)
    except asyncio.CancelledError:
        # Same reasoning as run_shell's own CancelledError handler: the
        # operator's Stop button cancels the asyncio task driving the whole
        # run, and without explicit cleanup here the container (and
        # anything running inside it) would keep running orphaned.
        await _kill_container(container_name)
        await proc.wait()
        await reclaim(cwd, image)
        raise

    output = stdout.decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output[-20_000:]}


async def _run_via_launcher(argv: list[str], name: str, cwd: str, image: str, cmd: str, timeout: int) -> dict:
    """run_shell_sandboxed's tail in the bundle: the same argv, started by the
    sandbox launcher, with the same timeout and Stop handling."""
    try:
        code, output, timed_out = await launcher.run(argv, timeout)
    except launcher.LauncherError as e:
        # Not run at all: docker's own code for a container that never started.
        return {"ok": False, "exit_code": launcher.UNAVAILABLE, "output": str(e)}
    except asyncio.CancelledError:
        await _kill_container(name)
        await reclaim(cwd, image)
        raise
    if timed_out:
        await reclaim(cwd, image)
        raise ShellTimeout(cmd, timeout)
    return {"ok": code == 0, "exit_code": code, "output": output[-20_000:]}

# --- previewing a running app ----------------------------------------------

async def start_preview_container(cmd: str, cwd: str, container_port: int,
                                  env: dict | None = None) -> dict:
    """Start `cmd` detached in a sandbox container with one port published.

    The same image, mounts and hardening every other sandboxed command gets.
    Two differences, both required to look at a running app: it is detached
    rather than waited on, and one port is published to LOOPBACK on the host
    so the browser in this process can reach it.

    Published to 127.0.0.1 specifically, on a port this function allocates:
    the model chooses what to run and which container port it listens on, and
    never which host port or interface. Nothing here is reachable from off the
    box.

    Returns {ok, container, url, port} -- the caller is responsible for
    stop_preview_container, and should use try/finally rather than trusting a
    happy path.
    """
    if not isinstance(container_port, int) or not (1 <= container_port <= 65535):
        return {"ok": False, "error": f"{container_port!r} is not a port number"}

    # Ask the OS for a free port and hand the number to Docker. There is a
    # race between closing this socket and Docker binding it; it is the same
    # race every ephemeral-port allocator has, and losing it means the run
    # fails loudly on a bind error rather than silently using somebody's port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        host_port = s.getsockname()[1]

    container_name = f"agent-preview-{uuid.uuid4().hex[:12]}"
    env_args: list[str] = []
    for k, v in (env or {}).items():
        env_args += ["-e", f"{k}={v}"]

    docker_args = [
        "docker", "run", "--rm", "-d", "--name", container_name,
        "-v", f"{host_path(cwd)}:/workspace",
        *_node_modules_mounts(cwd),
        "-p", f"127.0.0.1:{host_port}:{container_port}",
        "-w", "/workspace",
        "--memory", SANDBOX_MEMORY_LIMIT,
        "--memory-swap", SANDBOX_MEMORY_SWAP,
        "--cpus", SANDBOX_CPU_LIMIT,
        *[a for k, v in SANDBOX_TEST_ENV.items() for a in ("-e", f"{k}={v}")],
        *git_identity_args(),
        "--pids-limit", SANDBOX_PIDS_LIMIT,
        "--cap-drop", "ALL",
        # A dev server is stopped, never finishes: stop_preview_container
        # hands its files back from inside, before the kill.
        *owner_args(sandbox_owner()),
        "--security-opt", "no-new-privileges",
        "-e", "CI=true",
        *env_args,
        sandbox_image_for(cwd),
        *_shell(cmd),
    ]
    if launcher.enabled():
        code, out = await launcher.run_detached(docker_args)
        if code != 0:
            await _kill_container(container_name)
            return {"ok": False, "error": out[:600]}
        return {"ok": True, "container": container_name, "port": host_port,
                "url": f"http://127.0.0.1:{host_port}"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (TimeoutError, OSError) as e:
        await _kill_container(container_name)
        return {"ok": False, "error": f"could not start the preview: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "error": (out or b"").decode(errors="replace")[:600]}

    return {"ok": True, "container": container_name, "port": host_port,
            "url": f"http://127.0.0.1:{host_port}"}


async def wait_for_preview(host_port: int, container: str, timeout: int = 120) -> dict:
    """Wait until something answers on the port, or the container dies.

    Watching the container matters as much as watching the port: a dev server
    that exits immediately on a syntax error would otherwise be a full timeout
    of silence, and the logs that say why are gone once it is reaped.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", host_port), timeout=2)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return {"ok": True}
        except (TimeoutError, OSError):
            pass
        if not await _container_running(container):
            logs = await preview_logs(container)
            return {"ok": False, "error": "the app exited before it served anything",
                    "logs": logs}
        await asyncio.sleep(1)
    return {"ok": False, "error": f"nothing answered on the port within {timeout}s",
            "logs": await preview_logs(container)}


async def _container_running(name: str) -> bool:
    if launcher.enabled():
        return await launcher.running(name)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (TimeoutError, OSError):
        return False
    return (out or b"").decode(errors="replace").strip() == "true"


async def preview_logs(container: str, tail: int = 60) -> str:
    if launcher.enabled():
        return (await launcher.logs(container, tail))[-4000:]
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "--tail", str(tail), container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (TimeoutError, OSError):
        return ""
    return (out or b"").decode(errors="replace")[-4000:]


async def stop_preview_container(name: str) -> None:
    owner = sandbox_owner()
    if owner:
        # What the dev server wrote (.next, a cache) goes back to the owner
        # while the container still runs; after the kill there is no shell.
        if launcher.enabled():
            code, _ = await launcher.exec_in(name, ["sh", "-c", give_back_line(owner)], 60)
            if code != 0:
                logger.warning("could not hand the preview's files back in %s", name)
            await _kill_container(name)
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", name, "sh", "-c", give_back_line(owner),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:  # noqa: BLE001 -- the kill below happens regardless
            logger.warning("could not hand the preview's files back in %s", name)
    await _kill_container(name)
