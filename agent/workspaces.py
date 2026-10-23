"""One workspace per task, so tasks on the same project can run side by side.

Each task gets its own worktree, on its own branch, beside the project's
`sandbox` (a git worktree of the live repo):

    <sandbox's parent>/.tasks/<sandbox's name>/<task id>

The project's own `sandbox` has two jobs: it is the TEMPLATE a task's
workspace is filled from, and it is what planning, the cartographer and memory
consolidation read. No task edits it. (Before 2026-09-23 every task took turns
in it behind the project lock; agent/tools/git.py's owner marker and stash
salvage date from then and still carry a task parked before the change.)

Filling a new worktree. A checkout has only the tracked files, and a project
does not run on those alone: node_modules, a venv, generated clients, built
workspace packages, and the review-only config the template was provisioned
with are all gitignored. So everything the template ignores is carried over:

  * dependency trees (node_modules, .venv, ...) as HARDLINKS -- instant and
    free on disk, where a copy of a 2 GB monorepo's modules per task is not.
    Package managers replace files rather than rewriting them, so a task that
    installs gets new inodes and leaves the template alone. The few files they
    DO rewrite in place (npm's hidden lockfile, pnpm's state) are copied for
    real, and caches are left out, so they are created fresh per task.
  * everything else -- build output, generated code, uploads -- as a real
    copy, because builds rewrite their output in place and one task's build
    must not change another's. Anything over COPY_LIMIT_BYTES is hardlinked
    instead: that is data, not build output, and copying it per task is what
    would fill the disk.

The hardlink half is the one place tasks still share bytes. A command that
edits a dependency file IN PLACE (a hand-patched node_modules file) changes it
for the template and every task linked to it -- which is exactly the exposure
the single shared workspace had for every file, so this narrows it rather than
opening it.

The template's hardlinks never reach the live checkout: live is production,
and a hardlink into it would let a sandboxed command rewrite production files.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tektonix")

TASKS_DIRNAME = ".tasks"

# Carried as hardlinks: large, and replaced rather than edited by the tools
# that change them.
DEPENDENCY_DIRS = frozenset({"node_modules", ".pnpm-store", ".venv", "venv", ".yarn"})
# Rewritten in place by package managers, so copied for real on top of the links.
_REWRITTEN_IN_PLACE = (".package-lock.json", ".modules.yaml", ".yarn-state.yml", ".yarn-integrity")
# Caches inside a dependency tree: written freely, never needed up front.
_CACHE_DIRS = (".cache", ".vite")
# An ignored entry this large is data, not build output: linked, not copied.
COPY_LIMIT_BYTES = 256 * 1024 * 1024


class WorkspaceError(RuntimeError):
    pass


def _projects() -> dict:
    from agent.config import PROJECTS  # noqa: PLC0415 -- config reads env at import
    return PROJECTS


def tasks_root(template: str) -> str:
    """Where a project's task workspaces live, given its template workspace."""
    template = os.path.abspath(template)
    return os.path.join(os.path.dirname(template), TASKS_DIRNAME, os.path.basename(template))


def _safe_task_dir(task_id: str) -> str:
    # Same shape task_branch_name produces; a task id is a uuid in practice,
    # and nothing that reaches a path may be anything else.
    from agent.tools.git import task_branch_name  # noqa: PLC0415
    return task_branch_name(task_id).removeprefix("agent/")


def task_workspace_path(repo: str, task_id: str) -> str:
    cfg = _projects().get(repo) or {}
    template = cfg.get("sandbox")
    if not template:
        raise WorkspaceError(f"project {repo!r} has no workspace configured")
    return os.path.join(tasks_root(template), _safe_task_dir(task_id))


def own_or_template(template: str, task_id: str | None) -> str:
    """A task's own workspace under `template` if it has one, else `template`."""
    if task_id:
        path = os.path.join(tasks_root(template), _safe_task_dir(task_id))
        if os.path.exists(os.path.join(path, ".git")):
            return path
    return template


def workspace_for(repo: str, task_id: str | None) -> str:
    """The directory a task works in: its own when it has one, else the
    project's. Without a task id -- planning, the cartographer -- always the
    project's."""
    return own_or_template(_projects()[repo]["sandbox"], task_id)


def project_for_path(path: str) -> tuple[str, dict[str, Any]] | None:
    """Which project a workspace directory belongs to -- its template or one
    of its task workspaces. None for anything else.

    Server-owned config decides this, never the directory's contents: the
    sandbox mount allow-list and git's pointer check both ask it, and both
    exist because the tree itself is agent-writable."""
    real = os.path.realpath(path)
    for name, cfg in _projects().items():
        template = cfg.get("sandbox")
        if not template:
            continue
        t = os.path.realpath(template)
        if real == t:
            return name, cfg
        root = os.path.realpath(tasks_root(template))
        if real.startswith(root + os.sep) and os.sep not in real[len(root) + 1:]:
            return name, cfg
    return None


# --- git, synchronously, in a thread ----------------------------------------

def _git(args: list[str], cwd: str, timeout: int = 120) -> tuple[bool, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd,
                           capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _worktrees(live: str) -> dict[str, str]:
    """branch name -> worktree path, for every worktree with a branch checked out."""
    ok, out = _git(["worktree", "list", "--porcelain"], live, timeout=30)
    found, path = {}, None
    for line in out.splitlines() if ok else []:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path:
            found[line[len("branch refs/heads/"):]] = path
    return found


def _base_ref(live: str) -> str:
    ok, _ = _git(["rev-parse", "--verify", "--quiet", "refs/heads/main"], live, timeout=15)
    if ok:
        return "main"
    ok, cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], live, timeout=15)
    return cur if ok and cur and cur != "HEAD" else "HEAD"


# --- filling a new worktree from the template -------------------------------

def _ignored_entries(template: str) -> list[str]:
    """What the template's git ignores, collapsed to whole directories where
    git collapses them -- relative paths, no trailing slash."""
    ok, out = _git(["status", "--ignored", "--porcelain", "--untracked-files=normal"], template, timeout=120)
    if not ok:
        raise WorkspaceError(f"cannot list the template's ignored files: {out[:300]}")
    return [ln[3:].rstrip("/") for ln in out.splitlines() if ln.startswith("!! ")]


def _size(path: str, limit: int) -> int:
    """Bytes under `path`, stopping once past `limit` -- the answer only has
    to say which side of it we are on."""
    if os.path.islink(path) or not os.path.isdir(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
            if total > limit:
                return total
    return total


def _copy(src: str, dst: str, link: bool) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    r = subprocess.run(["cp", "-a" + ("l" if link else ""), src, dst],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise WorkspaceError(f"cp {'-al' if link else '-a'} {src}: {r.stderr.strip()[:300]}")


def _unshare_dependency_tree(dst: str) -> None:
    """In a hardlinked dependency tree: real copies of the files package
    managers rewrite in place, and no caches."""
    # Both live at the top of the tree; walking a hundred thousand package
    # files to find them would cost more than the hardlinking saved.
    for d in _CACHE_DIRS:
        shutil.rmtree(os.path.join(dst, d), ignore_errors=True)
    for f in _REWRITTEN_IN_PLACE:
        p = os.path.join(dst, f)
        if os.path.isfile(p) and not os.path.islink(p):
            tmp = p + ".tektonix-unshare"
            shutil.copy2(p, tmp)
            os.replace(tmp, p)


# --- mounts inside a workspace ----------------------------------------------
#
# A template can have read-only bind mounts inside it: a project whose tests
# read a large data directory from the live checkout gets that directory
# mounted, read-only, rather than copied (the reviewer's `readOnlyMounts`
# does the same for its own worktrees). Such a directory must never be copied
# or hardlinked -- a hardlink would reach straight into production's files --
# so it is mounted again, the same way, in each task's workspace. And a
# workspace is never deleted through one: `rm -rf` across a bind mount deletes
# the files it shows.

def _unescape_mount(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mounts_under(path: str) -> list[str]:
    """Mount points strictly inside `path`, deepest first."""
    root = os.path.realpath(path) + os.sep
    found = []
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 4:
                    point = _unescape_mount(parts[4])
                    if point.startswith(root):
                        found.append(point)
    except OSError:
        return []
    return sorted(set(found), key=lambda p: p.count(os.sep), reverse=True)


def _is_mount_point(path: str) -> bool:
    """True when something is mounted AT `path`, read from mountinfo.

    Not os.path.ismount: that compares the path's device with its parent's,
    and a bind of a directory on the same filesystem (a live checkout's data
    dir bound into a workspace, both on /) has the parent's device -- so it
    reported a mounted directory as unmounted, and every workspace restore
    stacked another bind on top (seen live 2026-10-02 after a restart
    auto-resumed a task)."""
    want = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo") as fh:
            return any(len(parts) > 4 and _unescape_mount(parts[4]) == want
                       for parts in (line.split() for line in fh))
    except OSError:
        return False


def _mount_count_under(path: str) -> int:
    """Mounts strictly inside `path`, counting each one stacked on a point."""
    root = os.path.realpath(path) + os.sep
    try:
        with open("/proc/self/mountinfo") as fh:
            return sum(1 for parts in (line.split() for line in fh)
                       if len(parts) > 4 and _unescape_mount(parts[4]).startswith(root))
    except OSError:
        return 0


def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _replicate_mounts(template: str, dest: str) -> dict:
    """Mount, read-only, whatever is mounted inside the template, at the same
    place inside `dest`. Idempotent -- a workspace that already has them is
    left alone, which is what re-mounts them after a reboot."""
    done, failed = [], []
    real_template = os.path.realpath(template)
    for point in sorted(mounts_under(template), key=lambda p: p.count(os.sep)):
        rel = os.path.relpath(point, real_template)
        target = os.path.join(dest, rel)
        if _is_mount_point(target):
            continue
        os.makedirs(target, exist_ok=True)
        ok, msg = _run(["mount", "--bind", point, target])
        if ok:
            # PRIVATE before anything else. `/` is a shared mount here, and a
            # bind of a shared mount joins its peer group, so mount events then
            # propagate between this workspace and the LIVE tree. From
            # 2026-09-26 that left a read-only mount on a live checkout's own
            # data directory for days at a time (the running app's writes
            # there failed with EROFS) whenever a task or a review had its
            # copy mounted. A private mount neither sends nor receives events.
            ok, msg = _run(["mount", "--make-private", target])
            if ok:
                ok, msg = _run(["mount", "-o", "remount,ro,bind", target])
            if not ok:
                _run(["umount", target])
        (done if ok else failed).append(rel if ok else f"{rel}: {msg[:200]}")
    if failed:
        logger.warning("could not mount %s into %s: %s", ", ".join(failed), dest, failed)
    return {"mounted": done, "failed": failed}


def populate(template: str, dest: str) -> dict:
    """Carry the template's ignored files into a new task worktree."""
    started = time.monotonic()
    linked, copied = [], []
    real_template = os.path.realpath(template)
    mounted = [os.path.relpath(m, real_template) for m in mounts_under(template)]

    def under_mount(rel: str) -> bool:
        return any(rel == m or rel.startswith(m + os.sep) or m.startswith(rel + os.sep) for m in mounted)

    skipped = []
    for rel in _ignored_entries(template):
        src, dst = os.path.join(template, rel), os.path.join(dest, rel)
        if os.path.lexists(dst):
            continue
        if under_mount(rel):
            # A mount, or a directory holding one: mounted below, never copied.
            # A directory that merely CONTAINS a mount is copied around it.
            if any(m.startswith(rel + os.sep) for m in mounted):
                _copy_around_mounts(src, dst, [m for m in mounted if m.startswith(rel + os.sep)], rel)
                copied.append(rel)
            else:
                skipped.append(rel)
            continue
        name = os.path.basename(rel)
        if name in DEPENDENCY_DIRS and os.path.isdir(src) and not os.path.islink(src):
            _copy(src, dst, link=True)
            _unshare_dependency_tree(dst)
            linked.append(rel)
        elif _size(src, COPY_LIMIT_BYTES) > COPY_LIMIT_BYTES:
            _copy(src, dst, link=True)
            linked.append(rel)
        else:
            _copy(src, dst, link=False)
            copied.append(rel)
    mounts = _replicate_mounts(template, dest)
    return {"linked": linked, "copied": copied, "mounts": mounts,
            "took_s": round(time.monotonic() - started, 1)}


def _copy_around_mounts(src: str, dst: str, mounts_rel: list[str], rel: str) -> None:
    """Copy a directory that has a mount somewhere inside it, leaving the
    mounted subdirectories as empty directories for _replicate_mounts."""
    inner = {os.path.relpath(m, rel) for m in mounts_rel}
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if name in inner:
            os.makedirs(d, exist_ok=True)
            continue
        deeper = [m for m in inner if m.startswith(name + os.sep)]
        if deeper and os.path.isdir(s) and not os.path.islink(s):
            _copy_around_mounts(s, d, [os.path.join(rel, m) for m in deeper], os.path.join(rel, name))
        else:
            _copy(s, d, link=_size(s, COPY_LIMIT_BYTES) > COPY_LIMIT_BYTES)


# --- lifecycle ---------------------------------------------------------------

def _ensure(repo: str, task_id: str) -> dict:
    from agent.tools.git import task_branch_name  # noqa: PLC0415

    cfg = _projects().get(repo) or {}
    live, template = cfg.get("live"), cfg.get("sandbox")
    if not live or not template:
        raise WorkspaceError(f"project {repo!r} needs both `live` and `sandbox` configured")
    path = task_workspace_path(repo, task_id)
    if os.path.exists(os.path.join(path, ".git")):
        # After a reboot its mounts are gone; everything else is as it was --
        # except attachments, which the dashboard stores in the project's
        # workspace, and which may have arrived with a resume.
        return {"ok": True, "path": path, "created": False,
                "mounts": _replicate_mounts(template, path),
                "uploads": _new_uploads(template, path)}
    if os.path.exists(path):
        # A half-made workspace from an interrupted create. Nothing in it is
        # the task's: work only lands after the worktree exists.
        remove_sync(repo, task_id)
        if os.path.exists(path):
            raise WorkspaceError(f"a half-made workspace at {path} could not be cleared")
    _git(["worktree", "prune"], live, timeout=60)

    branch = task_branch_name(task_id)
    out: dict = {"ok": True, "path": path, "created": True, "branch": branch}
    has_branch, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], live, timeout=15)
    if has_branch:
        # A task from before per-task workspaces, or one whose workspace was
        # removed: its branch holds its commits. If the old shared workspace
        # still has that branch checked out, git will not check it out twice,
        # so that workspace lets go of it -- its uncommitted edits stashed
        # under this task's name, where reclaim_own_stash finds them.
        holder = _worktrees(live).get(branch)
        if holder and os.path.realpath(holder) != os.path.realpath(path):
            ok, status = _git(["status", "--porcelain"], holder, timeout=30)
            if ok and status.strip():
                ok, msg = _git(["stash", "push", "--include-untracked", "-m",
                                f"tektonix: workspace left dirty by {task_id}"], holder, timeout=300)
                if not ok:
                    raise WorkspaceError(f"cannot move {branch} out of {holder}: stash failed: {msg[:300]}")
                out["moved_uncommitted_work"] = True
            ok, msg = _git(["checkout", "--detach"], holder, timeout=60)
            if not ok:
                raise WorkspaceError(f"cannot release {branch} from {holder}: {msg[:300]}")
            out["moved_from"] = holder
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ok, msg = _git(["worktree", "add", path, branch], live, timeout=300)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        base = _base_ref(live)
        ok, msg = _git(["worktree", "add", "-b", branch, path, base], live, timeout=300)
        out["base"] = base
    if not ok:
        raise WorkspaceError(f"git worktree add failed: {msg[:400]}")

    try:
        out["populated"] = populate(template, path)
    except Exception:
        # A workspace without its dependencies fails every check for a reason
        # the agent cannot see. Better no workspace and a clear error.
        remove_sync(repo, task_id)
        raise
    return out


# --- generated code ----------------------------------------------------------
#
# Code generated from a tracked file into an ignored directory -- a Prisma
# client from its schema -- goes stale whenever the schema moves, and a task
# workspace copies whatever the project's workspace last generated. The
# reviewer has a rule for this per project (`generated`: dir, schemaFile,
# regenerate) and applies it to its own worktrees; the agent applies the same
# rule, fetched from the reviewer, so its workspaces agree with it. Found
# 2026-09-23: main added an enum value, the project's workspace still had the
# client generated before it, and every task would have started with a
# typecheck failure it did not cause.
_STAMP = ".tektonix-generated-from"


def _inside(root: str, rel: str) -> str | None:
    full = os.path.realpath(os.path.join(root, rel))
    real_root = os.path.realpath(root)
    return full if full == real_root or full.startswith(real_root + os.sep) else None


async def refresh_generated(root: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Regenerate each rule's output in `root` whose schema changed since it
    was last generated here, and stamp it with the schema's hash. Runs in the
    sandbox, like everything else that executes the project's code. Never
    raises; returns what it did."""
    import hashlib
    import shlex

    from agent.tools.sandbox import run_shell_sandboxed

    done = []
    for rule in rules or []:
        try:
            schema = _inside(root, str(rule["schemaFile"]))
            out_dir = _inside(root, str(rule["dir"]))
            regen = rule["regenerate"]
            regen_dir = str(regen.get("dir") or ".")
            argv = [str(regen["cmd"]), *[str(a) for a in regen.get("args") or []]]
        except (KeyError, TypeError):
            continue
        if not schema or not out_dir or not _inside(root, regen_dir) or not os.path.isfile(schema):
            continue
        digest = hashlib.sha256(open(schema, "rb").read()).hexdigest()
        stamp = os.path.join(out_dir, _STAMP)
        try:
            if open(stamp).read().strip() == digest:
                continue
        except OSError:
            pass
        # From the workspace root, so the whole tree -- and the dependency
        # symlinks that point across it -- is mounted.
        cmd = f"cd {shlex.quote(regen_dir)} && {shlex.join(argv)}"
        try:
            result = await run_shell_sandboxed(cmd, root, timeout=300,
                                               network=regen.get("network") or "none")
        except Exception as e:  # noqa: BLE001 -- a failed regenerate is reported, not fatal
            result = {"ok": False, "output": str(e)}
        if result.get("ok"):
            os.makedirs(out_dir, exist_ok=True)
            with open(stamp, "w") as fh:
                fh.write(digest + "\n")
        else:
            logger.warning("regenerating %s in %s failed: %s", rule["dir"], root,
                           (result.get("output") or "")[-500:])
        done.append({"dir": rule["dir"], "ok": bool(result.get("ok"))})
    return done


UPLOADS_DIRNAME = ".uploads"


def _new_uploads(template: str, dest: str) -> list[str]:
    """Copy upload batches the project's workspace has and this one does not.

    Files attached from the dashboard land in the project's workspace
    (server.py's upload route runs before a task exists, or while one is
    parked); a new workspace copies them with everything else, and one that
    already exists gets the new batches here."""
    src_root = os.path.join(template, UPLOADS_DIRNAME)
    if not os.path.isdir(src_root):
        return []
    copied = []
    for batch in sorted(os.listdir(src_root)):
        src, dst = os.path.join(src_root, batch), os.path.join(dest, UPLOADS_DIRNAME, batch)
        if os.path.lexists(dst) or os.path.islink(src):
            continue
        try:
            _copy(src, dst, link=False)
            copied.append(batch)
        except WorkspaceError as e:
            logger.warning("could not copy upload batch %s into %s: %s", batch, dest, e)
    return copied


def remove_sync(repo: str, task_id: str) -> dict:
    """Delete a task's workspace. Its branch -- and so its commits -- stay."""
    cfg = _projects().get(repo) or {}
    live = cfg.get("live")
    try:
        path = task_workspace_path(repo, task_id)
    except WorkspaceError as e:
        return {"ok": False, "reason": str(e)}
    if not os.path.exists(path):
        return {"ok": True, "removed": False}
    # Unmount first, and refuse to delete anything while a mount remains: the
    # delete below would go straight through a bind mount into what it shows.
    # Repeated passes, not one: mounts_under lists each point ONCE, and a
    # point can carry stacked mounts (every restore re-bound it -- five deep
    # on one task, 2026-10-02), each needing its own umount. One pass left
    # the top of the stack behind and the guard below kept the workspace
    # forever. Stop when nothing is left or a pass removes nothing.
    stack = _mount_count_under(path)
    for _ in range(64):
        points = mounts_under(path)
        if not points:
            break
        for point in points:
            ok, _ = _run(["umount", point])
            if not ok:
                _run(["umount", "-l", point])
        now = _mount_count_under(path)
        if now >= stack:
            break
        stack = now
    left = mounts_under(path)
    if left:
        logger.error("not removing %s: still mounted at %s", path, ", ".join(left))
        return {"ok": False, "reason": f"still mounted: {', '.join(left)}"}
    ok, msg = (_git(["worktree", "remove", "--force", path], live, timeout=300) if live else (False, ""))
    if not ok:
        shutil.rmtree(path, ignore_errors=True)
        if live:
            _git(["worktree", "prune"], live, timeout=60)
    return {"ok": not os.path.exists(path), "removed": True}


async def ensure(repo: str, task_id: str) -> dict[str, Any]:
    """The task's own workspace, created and filled on first use.

    Idempotent: a task that already has one gets it back untouched -- that is
    what makes a resume pick up its own uncommitted work."""
    return await asyncio.to_thread(_ensure, repo, task_id)


async def remove(repo: str, task_id: str) -> dict:
    return await asyncio.to_thread(remove_sync, repo, task_id)


def existing(repo: str) -> list[str]:
    """Task ids (as directory names) that have a workspace for this project."""
    cfg = _projects().get(repo) or {}
    template = cfg.get("sandbox")
    if not template:
        return []
    root = Path(tasks_root(template))
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
