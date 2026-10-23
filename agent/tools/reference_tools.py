"""Reading another project, from inside a build task.

A build task is scoped to one repository, and that scoping is a security
boundary rather than a convenience: the sandbox bind-mount allow-list
(agent/tools/sandbox.py) exists precisely so a symlink or a rewritten .git
pointer cannot turn into `-v /home:/home:ro` and hand the task every other
project's .env, this agent's secrets and ~/.ssh.

But "use the checkout flow from the other project as a template" is an
ordinary thing to ask for, and until now the only way it reached a build task
was as prose in the plan -- the planner could read both projects, the builder
could read neither but its own. Typed straight into the task composer, it
could not be done at all.

These four tools are the narrow version of that. They are host-side and
read-only: no mount, no write path, no bash, nothing that changes the size or
shape of what the sandbox exposes. What they add is the ability to READ a
named project the task's creator was already allowed to read.

Three things keep that narrow:

  * The list of readable projects is resolved when the task is CREATED, from
    its creator's own access, and carried on the task. A task runs under the
    access its creator had at the time -- the same rule auto_approve_commands
    and require_merge_review already follow.
  * The task's OWN repo is refused here and pointed back at the normal tools.
    Two ways to read the same file with different path semantics is how a
    model ends up editing through the wrong one.
  * Reads come from each project's worktree, which is the agent's own copy,
    not the operator's live checkout.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent.config import PROJECTS
from agent.tools.files import PathEscapeError, read_file
# files.py's sandbox resolver, shared on purpose: one rule for what a path may reach.
# pyright: reportPrivateUsage=false
from agent.tools.files import _resolve
from agent.tools.planning_tools import run_find, run_search
from agent.tools.tool_errors import tool_errors_to_text

# The same inline ceiling the planner's reader uses. A reference read is for
# seeing how something was done, not for hauling a file into context whole.
_READ_CAP_CHARS = 40_000
# The paging path has to reach past that, so the underlying read is bounded
# far higher -- the same ceiling agent_tools.read uses.
_READ_HARD_CAP_CHARS = 2_000_000
# A paged read returns at least this many lines whatever `limit` asks for:
# 50-line windows just loop.
_READ_MIN_WINDOW = 500


def check_readable(repo: str, readable: list[str]) -> None:
    """Refuse a project this caller may not read, in the words it expects.

    Lifted out of _root so it is ONE rule rather than one per subsystem
    that grows a cross-project reach. agent/tools/history_tools.py searches
    other projects' history through this same check: the list is resolved
    at task creation from the creator's own access (agent/routers/tasks.py
    _readable_repos) and carried on the task, and two places deciding
    separately what that list means is how one of them ends up laxer.

    What is NOT shared is the own-repo rule. Here reading your own project
    through a second set of path semantics is a way to edit the wrong copy,
    so it is refused; history search inverts that and defaults to your own
    project, because there is no file to confuse and "have we hit this
    before" almost always means "here".
    """
    if repo not in PROJECTS:
        raise ValueError(f"unknown project {repo!r} -- must be one of {sorted(readable)}")
    if repo not in readable:
        raise ValueError(f"this task may not read {repo!r} -- it may read {sorted(readable)}")


def _root(repo: str, own_repo: str, readable: list[str]) -> str:
    """The worktree to read `repo` from, or a refusal explaining itself."""
    if repo == own_repo:
        raise ValueError(
            f"{repo!r} is the project this task is working on -- use read, bash and the "
            "built-in search for it, not the reference tools"
        )
    check_readable(repo, readable)
    root = (PROJECTS[repo] or {}).get("sandbox")
    if not root:
        raise ValueError(f"{repo!r} has no workspace to read")
    return root


def make_reference_tools(own_repo: str, readable: list[str] | None) -> list[BaseTool]:
    """Read-only tools for the OTHER projects this task may look at.

    Returns an empty list when there are none, so a deployment with one
    project does not carry four tools that can only ever refuse -- and the
    model is not invited to try.
    """
    others = sorted({r for r in (readable or []) if r != own_repo and r in PROJECTS})
    if not others:
        return []

    named = ", ".join(others)

    @tool
    @tool_errors_to_text
    def list_project_dir(repo: str, path: str = ".") -> str:
        """List a directory in ANOTHER of the operator's projects (read-only).

        For looking at how a different project of theirs does something -- its
        structure, its conventions, a pattern worth following here. `repo` is
        one of: {others}. `path` is repo-relative ("src", or "." for the root).

        This is not for the project you are working on; use `bash` and `read`
        for that.
        """
        try:
            root = _root(repo, own_repo, others)
            target = _resolve(root, path)
        except (ValueError, PathEscapeError) as e:
            return f"ERROR: {e}"
        if not target.exists():
            return f"ERROR: {path!r} does not exist in {repo!r}"
        if not target.is_dir():
            return f"ERROR: {path!r} is a file -- use read_project_file"
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError as e:
            return f"ERROR: cannot list {path!r} in {repo!r}: {e.strerror or e}"
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries]
        return "\n".join(lines) if lines else "(empty directory)"

    @tool
    @tool_errors_to_text
    def read_project_file(repo: str, path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a file from ANOTHER of the operator's projects (read-only).

        `repo` is one of: {others}. `path` is repo-relative. `offset` is the
        1-based line to start at and `limit` the number of lines, for paging
        through a large file.

        Use it to see how that project solved something before you build the
        same thing here. Search first with search_project, then read the
        window the hit points at -- a reference read is for understanding an
        approach, not for copying a file wholesale.
        """
        try:
            root = _root(repo, own_repo, others)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            content = read_file(root, path, max_chars=_READ_HARD_CAP_CHARS)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        except FileNotFoundError:
            return f"ERROR: {path!r} does not exist in {repo!r}"
        except IsADirectoryError:
            return f"ERROR: {path!r} is a directory in {repo!r} -- use list_project_dir"
        except OSError as e:
            return f"ERROR: cannot read {path!r} in {repo!r}: {e.strerror or e}"

        # Paging, for the same reason the planner's reader has it: a model
        # whose read came back truncated asks for the same file again, and
        # without a window that returns the identical text forever.
        lines = content.split("\n")
        if offset or limit:
            start = max(0, (offset or 1) - 1)
            count = max(limit if limit and limit > 0 else 400, _READ_MIN_WINDOW)
            window = lines[start:start + count]
            if not window:
                return f"(no lines at offset {offset} -- {path!r} has {len(lines)} lines)"
            numbered = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(window))
            remaining = len(lines) - (start + len(window))
            return numbered + (
                f"\n\n[lines {start + 1}-{start + len(window)} of {len(lines)}"
                + (f"; {remaining} more after this]" if remaining > 0 else "]")
            )
        if len(content) > _READ_CAP_CHARS:
            head = content[:_READ_CAP_CHARS]
            shown = head.count("\n") + 1
            return (
                f"{head}\n\n[TRUNCATED. {path!r} in {repo!r} is {len(content)} chars / "
                f"{len(lines)} lines; you have seen lines 1-{shown}. Reading it again the same "
                f"way returns this SAME text -- page with "
                f"read_project_file(repo={repo!r}, path={path!r}, offset={shown}, limit=800).]"
            )
        return content

    @tool
    @tool_errors_to_text
    def search_project(repo: str, pattern: str, path: str = ".", glob: str | None = None,
                       fixed: bool = False, max_results: int = 40) -> str:
        """Search ANOTHER of the operator's projects with ripgrep (read-only).

        `repo` is one of: {others}. `pattern` is a regex (`fixed=True` for a
        literal); `path` narrows to a directory; `glob` narrows to files
        ("*.tsx"). Returns `file:line: text`. This is the cheap way in --
        search for the thing you want to borrow, then read only what it
        points at.
        """
        try:
            root = _root(repo, own_repo, others)
            return run_search(root, pattern, path=path, glob=glob, fixed=fixed,
                              max_results=max_results)
        except (ValueError, PathEscapeError) as e:
            return f"ERROR: {e}"

    @tool
    @tool_errors_to_text
    def find_files(repo: str, glob: str, path: str = ".") -> str:
        """List files matching a glob in ANOTHER project (read-only).

        `repo` is one of: {others}. `glob` is like "**/*.css" or "*Chart*.tsx";
        .gitignore-aware, so node_modules and build output never appear.
        """
        try:
            root = _root(repo, own_repo, others)
            return run_find(root, glob, path=path)
        except (ValueError, PathEscapeError) as e:
            return f"ERROR: {e}"

    # The readable projects are named in each description rather than left to
    # the model to guess: a tool whose only failure mode is naming the wrong
    # repo should say up front which ones exist.
    tools = [list_project_dir, read_project_file, search_project, find_files]
    for t in tools:
        t.description = t.description.replace("{others}", named)
    return tools
