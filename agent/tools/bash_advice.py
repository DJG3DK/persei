"""Noticing when a shell command was really a file edit or a file read.

2026-09-12, task 01e640ef: the test-writer made 229 bash calls against one
edit and one write, patching files through Python heredocs and reading them
with `cat` and `sed -n`. Every bash call starts a container (the median gap
between that subagent's model calls was 11 s, nearly all of it startup and
teardown); `read`/`write`/`edit` run in-process, are path-guarded to the
repo, and `edit` catches a repeated failed edit. So a shell read or write
gets a one-line note on its result pointing at the cheaper tool.

2026-09-12, task 279c29fd: the coder reached for /memories/AGENTS.md through
bash. That path is the agent's own store-backed filesystem, mounted nowhere
in the container, so the command can only fail -- and a WRITE there fails
silently: the bytes land in a container that is thrown away, the shell says
exit 0, and the generic write note would point at `edit`, which rejects
/memories outright. That check runs first and never points at `edit`.

Every note here is advice, never a refusal: a scratch script written to RUN
is a fair use of a shell, and the harness cannot tell every case apart. The
read and edit notes are capped per workspace (agent_tools.py NOTE_BUDGET;
2026-09-25, 155 read notes over 50 tasks changed nothing); the memory and
git-write notes explain a command that failed and are not. Refusing is the
benchmark guard's job (agent/tools/benchmark_guard.py), which uses the
splitter and the note prefix below.
"""

from __future__ import annotations

from agent.harness_voice import HARNESS

import re
from typing import Any, Literal, overload

# The agent's own filesystem: store-backed, and mounted nowhere in the sandbox.
# Matched on a path boundary so a repo file that merely mentions one of these
# words ("src/skills.ts", "docs/memories.md") is left alone.
_VIRTUAL_PATH = re.compile(r"(?<![\w.-])/(?:memories|skills|org-memory)(?:/|\b)")

# Writing a file through the shell.
_WRITE_PATTERNS = (
    # cat > file <<EOF  /  cat >> file
    re.compile(r"(?:^|[|;&]\s*)cat\s+>{1,2}\s*[\"']?([\w./-]+)", re.M),
    # tee file / tee -a file
    re.compile(r"\btee\s+(?:-a\s+)?[\"']?([\w./-]+)"),
    # in-place stream editors
    re.compile(r"\bsed\s+(?:-[a-zA-Z]*\s+)*-i\b"),
    re.compile(r"\bperl\s+-[a-zA-Z]*i"),
    # a python/node heredoc that opens a file for writing
    re.compile(r"open\s*\([^)]*['\"][wa]\+?['\"]\s*\)"),
    re.compile(r"\bwriteFileSync\s*\("),
    re.compile(r"\.write_text\s*\("),
)

# Reading files through the shell.
#
# The first version of this matched only a command that did NOTHING but read
# ONE file. Measured on task 3ee0d030 (2026-09-14): an investigator made 219
# bash calls and 17 of them were flagged -- 8%. Every compound spelling walked
# straight past: `cat a b c`, `sed -n '1,80p' a && sed -n '1,80p' b`,
# `cat x | head -60`, `awk 'NR>=40 && NR<=90' x`. Its `read` use fell from 46
# calls in the first half of the run to 14 in the second while bash rose from
# 91 to 132, and the honest reading is not defiance: a harness that objects to
# one spelling and says nothing about twelve others is telling the model that
# the twelve are fine.
#
# So a command counts as a read when EVERY stage of it is a read. The stages
# below are the ones that only move bytes out of a file; anything that filters,
# searches, counts or transforms (grep, rg, sort, wc, jq, an awk PATTERN rather
# than a line range) makes the command a search, and a search is what bash is
# genuinely for -- the built-in glob/grep tools are hidden because they cannot
# see the repo at all (deep_agent.py's HiddenToolsMiddleware), so bash is the
# ONLY way to search it. Flagging that would be wrong, and would teach the
# model to ignore this the way the narrow version did.

# `sed -n '10,40p'` / `sed -n 40p` -- a line range, not a pattern.
_SED_RANGE = r"sed\s+-n\s+['\"]?[\d,$]+p?['\"]?"
# `awk 'NR>=40 && NR<=90'` -- a line range too. An awk with a /pattern/ is a
# search and deliberately does not match.
_AWK_RANGE = r"awk\s+['\"][^'\"]*NR[^'\"]*['\"]"
# Openers that take file arguments.
_OPENERS = rf"(?:cat|head|tail|{_SED_RANGE}|{_AWK_RANGE})"
# Limiters that take no file and just cut the stream shorter.
_LIMITERS = re.compile(r"^\s*(?:head|tail|cat)(?:\s+-\w+)*(?:\s+\d+)?\s*$")

_READ_STAGE = re.compile(
    rf"^\s*{_OPENERS}(?:\s+-\w+)*\s+(?P<files>[^|<>]+?)\s*$"
)
# A path that looks like a file rather than a flag or a glob into the unknown.
_FILEISH = re.compile(r"^[\w./~-]+$")


def _is_read_pipeline(segment: str) -> list[str] | None:
    """The files a segment reads, or None if it is doing anything else."""
    stages = [st.strip() for st in split_top_level(segment, ("|",))]
    if not stages or not stages[0]:
        return None
    first = _READ_STAGE.match(stages[0])
    if not first:
        return None
    files = [f for f in first.group("files").split() if not f.startswith("-")]
    if not files or not all(_FILEISH.match(f) for f in files):
        return None
    # Everything downstream must only be trimming the stream. One `grep` here
    # and this is a search.
    for st in stages[1:]:
        if not _LIMITERS.match(st):
            return None
    return files


@overload
def split_top_level(text: str, seps: tuple[str, ...], keep_seps: Literal[False] = False) -> list[str]: ...
@overload
def split_top_level(text: str, seps: tuple[str, ...], keep_seps: Literal[True]) -> list[tuple[str, str]]: ...
def split_top_level(text: str, seps: tuple[str, ...], keep_seps: bool = False) -> list[str] | list[tuple[str, str]]:
    """Split on separators that are not inside quotes.

    A plain re.split cut `awk 'NR>=40 && NR<=90' file` in half, because the
    `&&` is part of the awk program. Quotes have to be respected or the
    matcher's answer depends on what a command happens to contain.

    With `keep_seps`, each segment comes with the separator that followed it
    (the last one with ""), so a caller can drop a segment and join the rest
    back into a command the shell will still run -- the benchmark guard does
    that (agent/tools/benchmark_guard.py, 2026-09-25).
    """
    out: list[Any] = []
    buf: list[str] = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        hit = next((sep for sep in seps if text.startswith(sep, i)), None)
        if hit:
            out.append(("".join(buf), hit) if keep_seps else "".join(buf))
            buf = []
            i += len(hit)
            continue
        buf.append(ch)
        i += 1
    out.append(("".join(buf), "") if keep_seps else "".join(buf))
    return out


def _read_targets(text: str) -> list[str] | None:
    """Every file the command reads, when reading is ALL it does."""
    segments = split_top_level(text, ("&&", "||", ";"))
    out: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        seg = re.sub(r"^cd\s+\S+\s*$", "", seg).strip()
        if not seg:      # a bare `cd`, which is setup rather than work
            continue
        files = _is_read_pipeline(seg)
        if files is None:
            return None
        out.extend(files)
    return out or None

# A command that plainly works somewhere other than the repo checkout.
_LEADING_CD = re.compile(r"^\s*cd\s+[\"']?(/[\w./-]+)")

EDIT_NOTE = (
    HARNESS + " That wrote a file through the shell. Use the `edit` tool (or `write` for a new "
    "file) instead: it runs in-process, while every bash call starts a container -- the shell "
    "route is ~10x the wall-clock for the same change, and `edit` is path-guarded and catches a "
    "repeated failed edit. Keep bash for RUNNING things: tests, builds, rg, git status."
)
MEMORY_READ_NOTE = (
    HARNESS + " That path is not in this container. /memories, /skills and /org-memory are your "
    "OWN filesystem, not the repo's -- bash cannot see them at all, so the command above could "
    "only fail. Read them with your built-in `read_file` (and `ls`/`glob`) instead. The repo is "
    "the other filesystem: /workspace in bash, relative paths for `read`/`write`/`edit`."
)
MEMORY_WRITE_NOTE = (
    HARNESS + " That tried to WRITE your own memory through the shell, and it did not work even "
    "if the exit code said 0: /memories, /skills and /org-memory are not mounted in this "
    "container, so the bytes went into a sandbox that is thrown away when the command ends. Use "
    "your built-in `write_file` / `edit_file` -- those are the only tools that reach it. (Not "
    "`edit`: that one is path-guarded to the repo and will reject the path.)"
)
GIT_WRITE_NOTE = (
    HARNESS + " That git command changes the repository, and it cannot work here: the sandbox's "
    ".git is read-only, so stash, checkout, restore, reset, commit and the rest fail (often with "
    "no message). To run or read the code as it was BEFORE your change: prefer /baseline (the "
    "untouched tree, read-only; for an editable install run it with PYTHONPATH=/baseline or the "
    "package still imports from /workspace). Or `git show <base>:<path>`, where <base> is the "
    "commit before your changes -- `git log --oneline -3` shows it; NOT `HEAD`, which moves once "
    "the ship gate has committed your work. To see your change, `git diff <base>`."
)
# git subcommands that write the repository. The sandbox mounts .git
# read-only, and on 2026-09-24 agents retried `git stash` dozens of times to
# see the code before their change -- each failing with no stderr.
#
# The subcommand has to end at whitespace or the end of the segment: with a
# plain \b, `merge` matched `git merge-base --is-ancestor` (2026-09-25, a
# read-only question answered with "that cannot work here"). `stash list` and
# `stash show` only read the stash and are not writes either.
_GIT_WRITE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:stash(?!\s+(?:list|show)(?=\s|$))|checkout|restore|reset|"
                        r"commit|switch|apply|cherry-pick|revert|rebase|merge|am|add|rm|mv|clean)(?=\s|$)")
# How every refusal from agent/tools/benchmark_guard.py begins, whole or
# partial. Kept here so the guard and the telemetry agree on the spelling.
BENCHMARK_REFUSED_PREFIX = HARNESS + " REFUSED"
READ_NOTE = (
    HARNESS + " That read files through the shell. Use the `read` tool instead -- same content, "
    "measured at 0.1ms against 389ms for a bash call, because `read` runs in-process and every "
    "bash call starts a container. Reading SEVERAL files is still `read`: issue one `read` call "
    "per file IN THE SAME TURN and they all run together (five of them measured at 0.3ms total, "
    "against 389ms for one `cat a b c`). For part of a file use `read` with offset/limit rather "
    "than sed/head/awk. Keep bash for what only bash can do here: rg/grep searches, find, git, "
    "and running things."
)


def _in_repo(path: str) -> bool:
    """True for a path the in-process tools can actually open.

    They are path-guarded to the repo root, which bash sees as /workspace. A
    relative path is the repo's by default; an absolute one is only the repo's
    under /workspace.
    """
    return not path.startswith("/") or path == "/workspace" or path.startswith("/workspace/")


def _outside_repo(text: str) -> bool:
    """True when the command starts by changing to somewhere outside the repo.

    Caught on the 2026-09-12 replay: `cd /tmp && sed -n '40,110p'
    AlertSuppression.qll` was being nudged toward `read`, which is path-guarded
    to the repo and cannot open a scratch file in /tmp at all -- the same
    mistake as pointing a /memories write at `edit`, in the other direction.
    Downloading something to /tmp and paging through it is a fair use of a
    shell, and the in-process tools are not an alternative there.
    """
    m = _LEADING_CD.match(text)
    return bool(m) and not _in_repo(m.group(1))


def _writes(text: str) -> bool:
    for pattern in _WRITE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        # Patterns that name their target (`cat > x`, `tee x`) let a scratch
        # file outside the repo through; the rest (sed -i, an open() heredoc)
        # capture nothing, and the leading-cd check above is what covers them.
        target = m.group(1) if m.groups() else None
        # A virtual path is still a write -- the caller needs "write" to pick
        # the note that says so, and /memories is exactly where a shell write
        # does the most damage by appearing to succeed.
        if target and not _in_repo(target) and not _VIRTUAL_PATH.search(target):
            continue
        return True
    return False


def advice_for(command: str) -> str | None:
    """A one-line nudge, or None when the command is a fair use of a shell."""
    if not command or not command.strip():
        return None
    text = command.strip()

    # First, because this one is not a preference: the path is absent from the
    # container, so the command cannot work however it is spelled -- including
    # the compound and piped forms the checks below deliberately leave alone.
    if _VIRTUAL_PATH.search(text):
        return MEMORY_WRITE_NOTE if _writes(text) else MEMORY_READ_NOTE

    # Also not a preference: the repository is read-only to git in here.
    if _GIT_WRITE.search(text):
        return GIT_WRITE_NOTE

    # Scratch space outside the checkout: a shell is the only tool that
    # reaches it, so there is nothing cheaper to point at.
    if _outside_repo(text):
        return None

    # A write is the expensive mistake, so it wins when a command does both --
    # except a probe script into .scratch/, which is what that space is for
    # and which the verifier, having no write tool, can only reach this way.
    if _writes(text):
        return None if ".scratch/" in text else EDIT_NOTE

    # Reading is ALL it does -- one file or six, one stage or a pipeline that
    # only trims. `cat x | grep y` is a search and is left alone, because bash
    # is the only tool that can search the repo at all.
    targets = _read_targets(text)
    if targets and all(_in_repo(f) for f in targets):
        return READ_NOTE
    return None


# Which note means what, for telemetry. Keyed by the note text so the same
# table answers both "what kind of mistake was this command" and "what kind of
# nudge is on the front of this result" -- the latter is how the work node
# tags the tool event without the tool wrapper having to tell it.
NOTE_KINDS = {
    EDIT_NOTE: "write",
    READ_NOTE: "read",
    MEMORY_READ_NOTE: "memory-read",
    MEMORY_WRITE_NOTE: "memory-write",
    GIT_WRITE_NOTE: "git-write",
    # The benchmark guard's note names the refused command, so it is a
    # prefix here rather than a whole note (agent/tools/benchmark_guard.py).
    BENCHMARK_REFUSED_PREFIX: "benchmark-refused",
}


def kind(command: str) -> str | None:
    """What a command would be nudged for, or None. For tool telemetry."""
    return NOTE_KINDS.get(advice_for(command) or "")


def kind_of_result(text: str) -> str | None:
    """The same answer, read back off a tool RESULT that carries a note.

    The nudge is prepended to the bash result, so the work node can tag its
    tool event from the text it already has rather than the wrapper writing a
    second event of its own -- which put "bash-as-read" on the reliability
    panel looking like a tool, and counted one extra call per flagged command.
    """
    if not text:
        return None
    for note, name in NOTE_KINDS.items():
        if text.startswith(note):
            return name
    return None
