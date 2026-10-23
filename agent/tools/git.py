import os
import hashlib
import re
import shlex
import asyncio
import logging
from pathlib import Path
from typing import Any

from agent.tools.shell import run_shell

logger = logging.getLogger("tektonix")


# Every git command this module runs executes ON THE HOST, with cwd set to a
# worktree the agent can write to. Two consequences drive everything below.
#
# 1. HOOKS. A worktree's `.git` is a one-line pointer FILE, not a directory,
#    and it sits inside the tree the agent writes to. Rewriting it re-points
#    git at a git dir the agent controls -- hooks included -- so the host-side
#    `git commit` would execute an agent-authored pre-commit hook as the
#    server user (root, under the shipped pm2 config). Writing the pointer
#    matched no approval marker either: the gate looked for ".git/" WITH the
#    slash, and the pointer is the bare file ".git".
#
#    `core.hooksPath=/dev/null` is set on EVERY invocation rather than only on
#    commit, because checkout, merge and rebase run hooks too, and it holds
#    regardless of which git dir the pointer resolves to.
#
# 2. IDENTITY. Disabling hooks stops code execution but not misdirection: a
#    rewritten pointer can still aim commits at a different repository. So
#    when the workspace belongs to a configured project, the pointer is
#    checked against that project's own git dir before anything runs.
_GIT = "git -c core.hooksPath=/dev/null"


def _trusted_git_dir_error(repo_root: str) -> str | None:
    """Return a message if this workspace's .git pointer looks tampered with.

    Returns None when the workspace is not a configured project sandbox (the
    test fixtures and one-off checkouts), since there is no server-owned path
    to compare against. Hook execution is already disabled unconditionally, so
    this is the integrity half, not the code-execution half.
    """
    dotgit = os.path.join(repo_root, ".git")
    if not os.path.isfile(dotgit):
        return None  # a normal repo with a real .git directory
    try:
        from agent.config import PROJECTS
    except Exception:  # noqa: BLE001 -- config unavailable (tests); nothing to compare
        return None

    # The project's own workspace or one of its tasks' (agent/workspaces.py):
    # either way a worktree of that project's live repo.
    from agent.workspaces import project_for_path
    found = project_for_path(repo_root) if PROJECTS else None
    live = found[1].get("live") if found else None
    if not live:
        return None

    try:
        pointer = open(dotgit).read().strip()
    except OSError as e:
        return f"unreadable .git pointer: {e}"
    gitdir = pointer.removeprefix("gitdir:").strip()
    expected = os.path.realpath(os.path.join(live, ".git"))
    if not os.path.realpath(gitdir).startswith(expected + os.sep):
        return (f"the workspace's .git pointer names {gitdir!r}, which is outside "
                f"{expected!r}. Refusing to run git against it -- this is what a "
                f"rewritten pointer looks like.")
    return None


async def _git(cmd: str, repo_root: str, timeout: int = 30, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run one git command with hooks disabled and the pointer verified."""
    problem = _trusted_git_dir_error(repo_root)
    if problem is not None:
        return {"ok": False, "output": f"refusing to run git: {problem}"}
    return await run_shell(f"{_GIT} {cmd}", repo_root, timeout=timeout, extra_env=extra_env)


# A GitHub token for one git command, without putting it in the command.
#
# `https://x-access-token:<token>@github.com/...` as the push/clone URL put the
# token in git's argv, where /proc/<pid>/cmdline shows it to every local user
# for the length of the push, and in ShellTimeout's message when the push hung
# -- which verify_and_ship then escalated verbatim. A `!` credential helper is
# run by git through sh, which expands the variable from the child's own
# environment, so the token is only ever in that environment.
GIT_TOKEN_ENV = "TEKTONIX_GIT_TOKEN"
_TOKEN_HELPER = ('!f() { test "$1" = get || exit 0; echo username=x-access-token; '
                 'echo "password=$' + GIT_TOKEN_ENV + '"; }; f')


def token_credential_args() -> list[str]:
    """`-c` options that make git authenticate with $TEKTONIX_GIT_TOKEN. The
    empty helper first clears any helper the host has configured."""
    return ["-c", "credential.helper=", "-c", f"credential.helper={_TOKEN_HELPER}"]


def token_env(token: str) -> dict[str, str]:
    return {GIT_TOKEN_ENV: token, "GIT_TERMINAL_PROMPT": "0"}


# Commands that walk the whole tree. Thirty seconds was enough on a local
# disk; a Windows bind mount under Docker Desktop is many times slower, and
# a timeout here escalated a task at its final commit (2026-09-29).
_TREE_TIMEOUT_S = 180


async def git_diff(repo_root: str, staged: bool = False) -> str:
    """Includes newly created (untracked) files, not just changes to tracked
    ones. Plain `git diff` silently omits untracked files entirely, which
    would make a genuinely new file look like no change happened at all.
    `git add -N` (intent-to-add: stages the path, not the content) makes new
    files show up in `git diff` normally without actually staging their
    content -- this is not intended as a real `git add`.

    Measured against HEAD, so staged work counts too. A final commit that
    fails after `git add -A` leaves the whole change in the index, where
    plain `git diff` (tree vs index) reads empty: task 41a1a0b1, 2026-09-28,
    ended "no changes needed" with 1,295 staged lines.
    """
    await _git("add -A -N", repo_root, timeout=_TREE_TIMEOUT_S)
    cmd = "diff --staged" if staged else "diff HEAD"
    r = await _git(cmd, repo_root, timeout=_TREE_TIMEOUT_S)
    if not r["ok"]:
        # git's error text is not a diff. Handed back as one, "fatal: not a
        # git repository" read as a change to review (2026-09-29).
        logger.warning("git_diff failed in %s: %s", repo_root, r["output"][:200])
        return ""
    return r["output"]


# Which task last took the workspace. Written into the worktree's OWN git
# directory rather than the tree: a marker file in the tree would show up in
# `git status`, get swept into `git add -A`, and become the very debris this
# exists to clean up.
_OWNER_MARKER = "tektonix-workspace-owner"


async def _owner_marker_path(repo_root: str) -> Path | None:
    r = await _git("rev-parse --absolute-git-dir", repo_root, timeout=15)
    if not r["ok"]:
        return None
    gitdir = r["output"].strip()
    return Path(gitdir) / _OWNER_MARKER if gitdir else None


async def _workspace_owner(repo_root: str) -> str | None:
    """The task id that last synced this workspace, or None."""
    path = await _owner_marker_path(repo_root)
    if path is None:
        return None
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


async def _claim_workspace(repo_root: str, task_id: str) -> None:
    """Never raises: failing to record ownership must not fail the task. The
    cost of losing it is one unnecessary stash next time, not damage."""
    path = await _owner_marker_path(repo_root)
    if path is None:
        return
    try:
        path.write_text(f"{task_id}\n")
    except OSError:
        import logging  # noqa: PLC0415
        logging.getLogger("tektonix").warning(
            "could not claim the workspace at %s", repo_root)


async def sync_workspace_to_base(repo_root: str, base_ref: str = "main",
                                 task_id: str | None = None) -> dict[str, Any]:
    """Move an IDLE workspace to the current live tip before a fresh task edits.

    Why this exists: the workspace worktree only ever sat where the previous
    task left it, while direct pushes land on live main continuously. A fresh
    task then edited a stale tree, and ensure_task_branch -- which runs at
    COMMIT time, when the agent's own uncommitted edits have already dirtied
    the tree -- took its dirty-tree branch-from-HEAD path every single time,
    silently pinning the task branch to the stale base. First observed live
    2026-08-26: a task branched 9 commits behind main, its (review-approved,
    operator-approved) merge then failed --ff-only with "diverging branches",
    and the agent had spent the whole task editing week-old code.

    A dirty tree used to end this function, on the reasoning that it meant a
    resumed task owned the workspace and moving the base under real work is
    silent damage. Half right, and the wrong half cost three separate
    failures on 2026-09-22:

      * a stopped task left 2,086 files of downloaded reference material in
        the tree; the next task inherited them, spent calls working out what
        they were, and would have hit the 500-file commit guard twenty
        minutes in;
      * the sync then refused BECAUSE of that debris, so the next task
        silently skipped its base sync -- the exact failure this function
        exists to prevent, reintroduced by its own safety check;
      * and the refusal returned ok=True, so nothing anywhere treated it as
        a problem.

    The missing distinction is WHOSE dirty tree it is. A task that syncs
    claims the workspace by writing its id into the worktree's git dir (not
    the tree itself, so it is never committed and never shows in status).
    After that:

      * dirty and claimed BY THIS TASK -- a resume picking up its own
        uncommitted work. Left completely alone, exactly as before.
      * dirty and claimed by anyone else, or unclaimed -- abandoned. Stashed
        (including untracked files) and then synced. `git stash` rather than
        `clean -fd` on purpose: whatever it was, it is recoverable with
        `git stash list` afterwards, and a harness that silently deletes a
        directory it does not understand is not one to trust with a repo.

    Detached checkout, because `main` itself is checked out in the live
    worktree and git (correctly) refuses to check a branch out twice.
    """
    status = await _git("status --porcelain", repo_root, timeout=15)
    if not status["ok"]:
        return {"ok": False, "synced": False, "reason": f"status failed: {status['output'][:200]}"}

    salvaged = None
    if status["output"].strip():
        owner = await _workspace_owner(repo_root)
        if task_id and owner == task_id:
            return {"ok": True, "synced": False,
                    "reason": "tree dirty -- this task's own uncommitted work"}
        whose = owner or "an unknown task"
        stash = await _git(
            f'stash push --include-untracked -m "tektonix: workspace left dirty by {whose}"',
            repo_root, timeout=120)
        if not stash["ok"]:
            # Could not salvage, so do not destroy. The old refusal is the
            # right outcome here -- it is only wrong when the alternative was
            # available.
            return {"ok": True, "synced": False,
                    "reason": f"tree dirty and could not be stashed: {stash['output'][:200]}"}
        salvaged = whose

    r = await _git(f"checkout --detach {shlex.quote(base_ref)}", repo_root, timeout=30)
    if not r["ok"]:
        return {"ok": False, "synced": False, "reason": r["output"][:300]}
    if task_id:
        await _claim_workspace(repo_root, task_id)
    sha = await _git("rev-parse --short HEAD", repo_root, timeout=15)
    out = {"ok": True, "synced": True, "base": sha["output"].strip() if sha["ok"] else base_ref}
    if salvaged:
        out["salvaged_from"] = salvaged
    return out


def task_branch_name(task_id: str) -> str:
    """`agent/<task-id>`, sanitised -- the one definition of a task's branch.

    Used to be written out inline at each place that needed it: here, in
    verify_and_ship, and in the eval harness. The review service accepts ONLY
    this shape (its TASK_BRANCH_RE), so a copy that drifted would name a branch
    the reviewer refuses to review -- silently, since the refusal just falls
    back to guessing.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(task_id)).strip("-.") or "task"
    return f"agent/{safe}"


async def ensure_task_branch(repo_root: str, task_id: str, base_ref: str = "main") -> dict[str, Any]:
    """Put the agent's workspace on a per-task branch before committing.

    The workspace is a git worktree of the live repo, so this branch is created
    directly in live's own object store -- there is no clone to keep in sync and
    no remote to fetch through. The reviewer and the merge endpoint read the
    branch as a plain local ref.

    Two things this protects against, both observed:
      * committing to `main` made one ref serve two writers (the agent, and the
        refresh cron that fast-forwarded it), and
      * without a branch there was no stable review unit, so the reviewer
        inferred one by comparing HEADs -- which produced inverted diffs and two
        false `blocking` findings on live trading safety settings.

    Idempotent: on resume the branch already exists and is checked out, and this
    returns without touching the tree.
    """
    branch = task_branch_name(task_id)

    cur = await _git("rev-parse --abbrev-ref HEAD", repo_root, timeout=15)
    if cur["ok"] and cur["output"].strip() == branch:
        return {"ok": True, "branch": branch, "switched": False}

    # A fresh task should start from the current live tip rather than from
    # wherever the previous task left the workspace. Only safe when the tree is
    # clean -- if there is uncommitted work, branch from HEAD so it survives,
    # and say so rather than discarding it.
    status = await _git("status --porcelain", repo_root, timeout=15)
    dirty = bool(status["ok"] and status["output"].strip())

    if dirty:
        cmd = f"checkout -B {branch}"
    else:
        cmd = f"checkout -B {branch} {shlex.quote(base_ref)}"

    r = await _git(cmd, repo_root, timeout=30)
    if not r["ok"] and not dirty:
        # base_ref may not exist (unusual default branch name); fall back to HEAD.
        r = await _git(f"checkout -B {branch}", repo_root, timeout=30)

    return {
        "ok": r["ok"],
        "branch": branch,
        "switched": True,
        "from_base": not dirty,
        "output": r["output"],
    }


# audit M-12: git_commit used `git add -A`, and the sandboxed bash can create
# arbitrary files in the worktree -- every one landed in the commit that goes to
# review and, on approval, into the live repo (core.excludesFile only covers
# .uploads/). These directories/suffixes are never legitimate commit content;
# their presence in the pending set is a build/dep artifact that must be cleaned
# (or gitignored) before committing, not silently shipped.
#
# Matched as path COMPONENTS, not substrings: the list used to hold
# "node_modules/" and match it anywhere in the path, so a SYMLINK named
# node_modules (no trailing slash in `git status`) passed, was committed, and
# the review sandbox followed it to wherever the agent pointed it
# (2026-09-29). A directory entry, a link or anything under one is refused;
# a plain file that merely shares the name (a `bin/build` script) is not.
_COMMIT_DENY_DIRS = (
    "node_modules", "dist", "build", ".next", ".venv", "venv",
    "__pycache__", ".pytest_cache", "coverage", ".mypy_cache",
    "target", ".turbo", ".cache",
)
_COMMIT_DENY_SUFFIXES = (".pyc", ".log", ".tmp")
_MAX_COMMIT_FILES = 500


def _is_denied_artifact(repo_root: str, pth: str) -> bool:
    if pth.endswith(_COMMIT_DENY_SUFFIXES):
        return True
    parts = [p for p in pth.split("/") if p]
    if not parts:
        return False
    if any(p in _COMMIT_DENY_DIRS for p in parts[:-1]):
        return True
    if parts[-1] not in _COMMIT_DENY_DIRS:
        return False
    # The last component carries the name: an untracked directory lists as
    # "name/", and a symlink lists bare, so ask the tree what it is.
    if pth.endswith("/"):
        return True
    full = os.path.join(repo_root, pth)
    return os.path.islink(full) or os.path.isdir(full)


def _porcelain_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # rename entries are "old -> new"; take the destination
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


async def git_commit(repo_root: str, message: str, files: list[str] | None = None) -> dict[str, Any]:
    if not files:
        # audit M-12: guard the blanket `git add -A`. Inspect what would be
        # staged and refuse obvious artifacts / a runaway file count, so the
        # agent cleans up rather than committing junk into the live repo.
        # Every untracked FILE, not a collapsed "apps/": a new directory hid
        # whatever it held -- a node_modules, a link -- behind one line.
        status = await _git("status --porcelain --untracked-files=all", repo_root,
                            timeout=_TREE_TIMEOUT_S)
        if status["ok"]:
            pending = _porcelain_paths(status["output"])
            denied = [pth for pth in pending if _is_denied_artifact(repo_root, pth)]
            if denied:
                sample = ", ".join(denied[:10])
                return {"ok": False, "output": (
                    f"refusing to commit {len(denied)} build/dependency artifact(s) that "
                    f"should be gitignored or removed first: {sample}"
                    + (" ..." if len(denied) > 10 else ""))}
            if len(pending) > _MAX_COMMIT_FILES:
                return {"ok": False, "output": (
                    f"refusing to commit {len(pending)} files at once (limit {_MAX_COMMIT_FILES}) -- "
                    "this usually means an un-ignored directory got created; clean it up or "
                    "pass an explicit file list.")}
    add_cmd = f"add {' '.join(files)}" if files else "add -A"
    add = await _git(add_cmd, repo_root, timeout=_TREE_TIMEOUT_S)
    if not add["ok"]:
        return {"ok": False, "output": add["output"]}
    # Message via a temp file, not -m "...", so multi-line messages with
    # quotes/special chars can never break the shell invocation.
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(message)
        msg_path = f.name
    try:
        commit = await _git(f"commit --no-verify -F {msg_path}", repo_root, timeout=_TREE_TIMEOUT_S)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    return {"ok": commit["ok"], "output": commit["output"]}


async def commits_ahead(repo_root: str, base_ref: str = "main") -> int:
    """How many commits HEAD has that `base_ref` does not. 0 when the branch
    has nothing of its own, or when the question cannot be answered."""
    r = await _git(f"rev-list --count {shlex.quote(base_ref)}..HEAD", repo_root, timeout=15)
    if not r["ok"]:
        return 0
    try:
        return int(str(r["output"]).strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


async def no_diff_evidence(repo_root: str, base_ref: str = "main") -> str:
    """One line on why a workspace reads as unchanged, for the record
    verify_and_ship keeps when it finds no diff: the branch, HEAD, the base,
    what `git status` sees and what is stashed. 2026-09-29, a Windows install:
    the operator saw the edits, the gate said "no diff", and the log held
    nothing to tell the two apart. Never raises."""
    parts: list[str] = []
    for label, cmd in (("branch", "rev-parse --abbrev-ref HEAD"), ("head", "rev-parse --short HEAD"),
                       (base_ref, f"rev-parse --short {shlex.quote(base_ref)}"),
                       ("ahead", f"rev-list --count {shlex.quote(base_ref)}..HEAD"),
                       ("status", "status --porcelain"), ("stash", "stash list")):
        try:
            r = await _git(cmd, repo_root, timeout=15)
        except Exception as e:  # noqa: BLE001 -- evidence, never a failure of its own
            parts.append(f"{label}=error({type(e).__name__})")
            continue
        out = (r.get("output") or "").strip()
        if label in ("status", "stash"):
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            parts.append(f"{label}={len(lines)}" + (" [" + "; ".join(lines[:5]) + "]" if lines else ""))
        elif r.get("ok"):
            parts.append(f"{label}={out.splitlines()[-1][:80] if out else '?'}")
        else:
            parts.append(f"{label}=error: {out[:120]!r}")
    return f"workspace {repo_root}: " + ", ".join(parts)


async def current_sha(repo_root: str) -> str:
    r = await _git("rev-parse HEAD", repo_root, timeout=15)
    return r["output"].strip()


async def sha_in_repo(repo_root: str, sha: str) -> bool:
    """True iff `sha` is an ancestor of (or equal to) `repo_root`'s HEAD --
    used by verify_and_ship to detect that the review service's
    auto-merge-on-READY already merged the pending commit to the live repo
    (the two are separate paths to the same merge and can race, which would
    otherwise nudge an already-finished task for a verdict that auto-merge
    had already consumed).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_root, "merge-base", "--is-ancestor", sha, "HEAD",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


async def rebase_onto_base(repo_root: str, base_ref: str = "main") -> dict[str, Any]:
    """Move the task branch onto the live tip when the tip has moved under it.

    The merge into live is `--ff-only`, deliberately: it is what guarantees the
    thing that merges is the thing that was reviewed. The cost is that a branch
    whose base moved cannot land at all -- a reviewed, approved commit with
    nowhere to go, at the end of a task somebody already paid for. The trigger
    is ordinary: a push to main, or another merge, while a task is running.

    So the branch moves instead of the rule. Called after the commit and before
    the review, which is the one moment the tree is clean AND the agent is still
    running to fix a conflict if there is one.

    `patch_identical` is the interesting part. A rebase rewrites every sha, and
    the reviewer discards its history when the sha it reviewed is no longer an
    ancestor -- so a naive rebase costs a full review round every time someone
    else pushes. Comparing the branch's own diff before and after answers
    whether that round would learn anything: if the patch is byte-identical,
    the review that exists still describes this change. Context lines shifting
    because main edited nearby is enough to make it differ, and re-reviewing
    then is the right, conservative answer.

    Returns, and never raises:
      moved=False                 the base is still where the branch forked. The
                                  common case, and it costs two rev-parses.
      moved, rebased, ok=True     moved, and the branch now sits on the new tip.
      conflicts=[...], ok=False   moved, and the two changes disagree. The
                                  rebase is ABORTED before returning, so the
                                  branch is exactly as it was and the caller can
                                  hand the file list to whoever can resolve it.
    """
    base_tip = await _git(f"rev-parse {shlex.quote(base_ref)}", repo_root, timeout=15)
    if not base_tip["ok"]:
        # No such ref. Not this function's problem to diagnose, and not a
        # reason to fail a task: the branch is fine where it is.
        return {"ok": True, "moved": False, "reason": f"{base_ref} does not resolve"}
    tip = base_tip["output"].strip()

    mb = await _git(f"merge-base HEAD {shlex.quote(base_ref)}", repo_root, timeout=15)
    if not mb["ok"]:
        return {"ok": True, "moved": False, "reason": "no merge base"}
    fork = mb["output"].strip()

    if fork == tip:
        return {"ok": True, "moved": False, "base": tip}

    before = await _git(f"diff {fork}..HEAD", repo_root, timeout=60)
    patch_before = _digest(before["output"] if before["ok"] else "")

    r = await _git(f"rebase {shlex.quote(base_ref)}", repo_root, timeout=120)
    if not r["ok"]:
        # Collect the file list BEFORE aborting -- afterwards there is nothing
        # left to ask.
        conflicted = await _git("diff --name-only --diff-filter=U", repo_root, timeout=15)
        files = [f for f in (conflicted["output"] or "").splitlines() if f.strip()]
        await _git("rebase --abort", repo_root, timeout=60)
        return {
            "ok": False,
            "moved": True,
            "rebased": False,
            "conflicts": files,
            "base": tip,
            "output": r["output"][:1000],
        }

    after = await _git(f"diff {tip}..HEAD", repo_root, timeout=60)
    patch_after = _digest(after["output"] if after["ok"] else "")

    return {
        "ok": True,
        "moved": True,
        "rebased": True,
        "patch_identical": patch_before == patch_after and bool(patch_before),
        "base": tip,
        "previous_base": fork,
    }


async def reclaim_own_stash(repo_root: str, task_id: str) -> dict[str, Any]:
    """Put back a task's own uncommitted work that another task stashed.

    The workspace is shared and the project lock is held only while a task
    RUNS. A task parked mid-turn -- waiting for an approval, most often --
    holds its edits uncommitted in the tree and holds no lock, so the next
    task's sync stashes them as "left dirty by <that task>". Recoverable, and
    until 2026-09-23 never recovered: the parked task resumed into someone
    else's tree without its own edits. Seen live that day twice in a row on
    one task, which redid its work, paused again, and lost the redo the same
    way.

    The stash's label names the owner, and its first parent is the commit the
    work was made on. So: set aside whatever is in the tree now (another
    task's, stashed the same recoverable way), go back to that commit -- on
    the task's own branch when the branch is still there -- and pop the
    newest stash this task owns. A conflicting pop leaves the stash in place
    and says so; nothing is ever dropped that was not applied.

    Never raises.
    """
    async def stashes() -> list[tuple[str, str, str]] | None:
        """(ref, commit, subject), newest first."""
        r = await _git("stash list --format=%gd%x09%H%x09%s", repo_root, timeout=15)
        if not r["ok"]:
            return None
        return [tuple(ln.split("\t", 2)) for ln in r["output"].splitlines() if ln.count("\t") >= 2]

    listed = await stashes()
    if listed is None:
        return {"ok": False, "reclaimed": False, "reason": "could not list stashes"}
    mine = [(ref, sha) for ref, sha, subject in listed if subject.endswith(f"left dirty by {task_id}")]
    if not mine:
        return {"ok": True, "reclaimed": False}
    ref, stash_sha = mine[0]           # newest first
    base = await _git(f"rev-parse {stash_sha}^1", repo_root, timeout=15)
    if not base["ok"]:
        return {"ok": False, "reclaimed": False, "reason": f"cannot read {ref}: {base['output'][:200]}"}
    base_sha = base["output"].strip()

    status = await _git("status --porcelain", repo_root, timeout=15)
    out: dict[str, Any] = {"ok": True, "stash": ref, "base": base_sha[:12]}
    if status["output"].strip():
        owner = await _workspace_owner(repo_root)
        whose = owner or "an unknown task"
        aside = await _git(
            f'stash push --include-untracked -m "tektonix: workspace left dirty by {whose}"',
            repo_root, timeout=120)
        if not aside["ok"]:
            return {**out, "ok": False, "reclaimed": False,
                    "reason": f"tree dirty and could not be stashed: {aside['output'][:200]}"}
        out["salvaged_from"] = whose
        # Setting that aside renumbered every stash; find ours again by commit.
        relisted = await stashes() or []
        ref = next((r for r, sha, _ in relisted if sha == stash_sha), None)
        if ref is None:
            return {**out, "ok": False, "reclaimed": False, "reason": "own stash vanished while reclaiming"}

    branch = task_branch_name(task_id)
    tip = await _git(f"rev-parse --verify --quiet refs/heads/{branch}", repo_root, timeout=15)
    target = branch if tip["ok"] and tip["output"].strip() == base_sha else f"--detach {base_sha}"
    co = await _git(f"checkout {target}", repo_root, timeout=30)
    if not co["ok"]:
        return {**out, "ok": False, "reclaimed": False, "reason": f"checkout failed: {co['output'][:200]}"}
    pop = await _git(f"stash pop --index {ref}", repo_root, timeout=120)
    if not pop["ok"]:
        pop = await _git(f"stash pop {ref}", repo_root, timeout=120)
    if not pop["ok"]:
        return {**out, "ok": False, "reclaimed": False,
                "reason": f"the stash did not apply cleanly and was kept: {pop['output'][:200]}"}
    await _claim_workspace(repo_root, task_id)
    return {**out, "reclaimed": True, "older_stashes": len(mine) - 1}


async def restore_task_workspace(repo_root: str, task_id: str, base_ref: str = "main",
                                 rebase: bool = True) -> dict[str, Any]:
    """Put a RESUMED task back on its own branch, on the current base, before it edits.

    sync_workspace_to_base runs only on a task's first pass. A task resumed
    after other tasks have used the workspace found it wherever the last one
    left it -- on 2026-09-23 a resumed task woke on a detached, three-hour-old
    main, could not check anything out (the agent's git dir is read-only), and
    hand-copied main's files over the stale tree to do its work.

    In order:
      * a dirty tree that is not this task's own uncommitted work on its own
        branch is stashed, recoverably, exactly as the first-pass sync does;
      * a task with no branch yet has never committed, so it syncs like a
        fresh pass;
      * otherwise its branch is checked out and rebased onto the base. If
        that conflicts, the branch is reset onto the base and the old commit
        kept as `<branch>-pre-rebase-backup`: the agent cannot resolve a
        rebase (no git writes), but it can re-apply its change to the current
        code, reading the old version with `git show`. Leaving the branch on
        the stale base instead makes every later rebase conflict again.

    rebase=False is the ship step's use (verify_and_ship_node): put the task
    back on its own branch and stop there. That node rebases at its own
    moments, and a branch with no commit yet is left exactly as it is.

    Never raises.
    """
    branch = task_branch_name(task_id)
    exists = (await _git(f"rev-parse --verify --quiet refs/heads/{branch}", repo_root, timeout=15))["ok"]
    cur = await _git("rev-parse --abbrev-ref HEAD", repo_root, timeout=15)
    on_branch = cur["ok"] and cur["output"].strip() == branch
    status = await _git("status --porcelain", repo_root, timeout=15)
    if not status["ok"]:
        return {"ok": False, "reason": f"status failed: {status['output'][:200]}"}

    out: dict[str, Any] = {"ok": True, "branch": branch}
    if status["output"].strip():
        owner = await _workspace_owner(repo_root)
        if owner == task_id and (on_branch or not exists):
            return {**out, "restored": False, "reason": "tree dirty -- this task's own uncommitted work"}
        whose = owner or "an unknown task"
        stash = await _git(
            f'stash push --include-untracked -m "tektonix: workspace left dirty by {whose}"',
            repo_root, timeout=120)
        if not stash["ok"]:
            return {"ok": False, "reason": f"tree dirty and could not be stashed: {stash['output'][:200]}"}
        out["salvaged_from"] = whose

    if not exists:
        if not rebase:
            return {**out, "restored": False, "reason": "no task branch yet"}
        return {**out, **await sync_workspace_to_base(repo_root, base_ref, task_id=task_id)}

    if not on_branch:
        r = await _git(f"checkout {branch}", repo_root, timeout=30)
        if not r["ok"]:
            return {**out, "ok": False, "reason": f"could not check out {branch}: {r['output'][:200]}"}
    await _claim_workspace(repo_root, task_id)
    # Its own work, stashed by another task while it was parked, comes back
    # before anything moves the branch (reclaim_own_stash).
    reclaimed = await reclaim_own_stash(repo_root, task_id)
    if reclaimed.get("reclaimed"):
        out["reclaimed"] = reclaimed
        return {**out, "restored": True}   # dirty again, and the task's own: nothing to rebase
    if not rebase:
        return {**out, "restored": True}

    rb = await rebase_onto_base(repo_root, base_ref)
    if not rb.get("conflicts"):
        return {**out, "restored": True, "rebase": rb}

    old = (await _git("rev-parse HEAD", repo_root, timeout=15))["output"].strip()
    backup = f"{branch}-pre-rebase-backup"
    await _git(f"branch -f {backup} {old}", repo_root, timeout=15)
    r = await _git(f"checkout -B {branch} {shlex.quote(base_ref)}", repo_root, timeout=30)
    if not r["ok"]:
        return {**out, "ok": False, "reason": f"could not reset {branch} onto {base_ref}: {r['output'][:200]}"}
    return {**out, "restored": True, "reset_onto_base": True, "conflicts": rb["conflicts"],
            "backup": backup, "previous_sha": old}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


async def fetch_base_from_origin(live_root: str, base_ref: str = "main") -> dict[str, Any]:
    """Bring the live checkout's base branch up to date with `origin`.

    The local copy is a cache of GitHub, not the source of truth. Without this
    a task branches from whatever the machine last saw, does good work against
    it, and finds out at merge time -- which is the expensive end. The rebase
    path handles that when it happens; this makes it happen less.

    Fast-forward only, and deliberately: if local `main` has commits that are
    not on the remote, somebody committed here directly and this function has
    no business rewriting or merging that. It says so and leaves it, and the
    task proceeds from the local tip exactly as before.

    Called against the LIVE repo rather than the workspace, because the
    workspace is a worktree of it and shares its refs -- fetching once updates
    both. Never raises: no remote, no network and an unreachable host are all
    ordinary, and none of them is a reason to fail a task that could run
    offline perfectly well.
    """
    remotes = await _git("remote", live_root, timeout=15)
    if not remotes["ok"] or "origin" not in remotes["output"].split():
        return {"ok": True, "fetched": False, "reason": "no origin remote"}

    f = await _git(f"fetch --quiet origin {shlex.quote(base_ref)}", live_root, timeout=120)
    if not f["ok"]:
        return {"ok": True, "fetched": False,
                "reason": f"fetch failed: {f['output'][:200]}"}

    local = await _git(f"rev-parse {shlex.quote(base_ref)}", live_root, timeout=15)
    remote = await _git("rev-parse FETCH_HEAD", live_root, timeout=15)
    if not (local["ok"] and remote["ok"]):
        return {"ok": True, "fetched": True, "advanced": False, "reason": "could not compare"}
    local_sha, remote_sha = local["output"].strip(), remote["output"].strip()
    if local_sha == remote_sha:
        return {"ok": True, "fetched": True, "advanced": False, "base": local_sha}

    # Only when the local branch is strictly behind. `--is-ancestor` answers
    # exactly that, and answers it about the commit graph rather than about
    # timestamps or counts.
    anc = await _git(f"merge-base --is-ancestor {shlex.quote(base_ref)} FETCH_HEAD", live_root, timeout=15)
    if not anc["ok"]:
        return {"ok": True, "fetched": True, "advanced": False,
                "diverged": True,
                "reason": f"local {base_ref} has commits origin does not; leaving it alone"}

    # Fast-forward the branch ref without touching the working tree: `main` is
    # checked out in the live worktree, and a task may be running in another
    # worktree of the same repo. `update-ref` moves the pointer only.
    head = await _git("rev-parse --abbrev-ref HEAD", live_root, timeout=15)
    if head["ok"] and head["output"].strip() == base_ref:
        up = await _git("merge --ff-only FETCH_HEAD", live_root, timeout=60)
    else:
        up = await _git(f"update-ref refs/heads/{shlex.quote(base_ref)} {remote_sha}", live_root, timeout=15)
    if not up["ok"]:
        return {"ok": True, "fetched": True, "advanced": False,
                "reason": f"could not fast-forward: {up['output'][:200]}"}
    return {"ok": True, "fetched": True, "advanced": True,
            "base": remote_sha, "previous": local_sha}
