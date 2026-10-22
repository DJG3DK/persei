"""build_deep_agent -- per-project, per-task factory for the deepagents-based
work engine. Returns a CompiledStateGraph, invoked manually (not nested as a
native subgraph -- the outer AgentState and this graph's own state share no
keys) by the outer "work" node.
"""

import logging
import os
import re
import subprocess
import warnings
from pathlib import Path

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.tools import tool
from langgraph.store.base import BaseStore

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import StoreBackend
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from agent.config import Config, PROJECTS
from agent.frontend_route import CODER_ROLE
from agent.tools.github_tools import make_github_inbox_tool, make_github_tools, token_source
from agent import new_files

# langchain-openai cannot attach response headers on the structured-output
# stream path (with_structured_output sets response_format) and warns on every
# such call. Those calls are metered at their estimate, which is the documented
# fallback -- the warning adds nothing per call.
warnings.filterwarnings(
    "ignore", message="Cannot currently include response headers when response_format is specified"
)
from agent import runtime_settings as _rs
from agent.middleware.empty_reply import EmptyReplyRetryMiddleware
from agent.middleware.hidden_tools import HiddenToolsMiddleware
from agent.middleware.repeat_guard import RepeatCallGuardMiddleware
from agent.middleware.sanitize_tool_calls import SanitizeToolCallsMiddleware
from agent.middleware.budget_guard import BudgetMeterCallback, BudgetGuardMiddleware, BudgetTracker
from agent.middleware.model_pin import PlanCodeModelMiddleware
from agent.middleware.todo_nag import StaleTodoMiddleware
from agent.middleware.step_back import StepBackMiddleware
from agent.middleware.wrap_up import WrapUpMiddleware
from agent.harness_voice import HARNESS
from agent.model_client import (  # noqa: F401 -- re-exported: `agent.deep_agent` stays the import path
    COORDINATOR_MAX_TOKENS,
    SEAT_MAX_TOKENS,
    _call_metadata,
    coder_reasoning,
    llm_for_role,
)
from agent.deep_agent_prompts import (  # noqa: F401 -- re-exported: `agent.deep_agent` stays the import path
    COORDINATOR_SYSTEM_PROMPT_TEMPLATE,
    INVESTIGATOR_SYSTEM_PROMPT,
    TEST_WRITER_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
    _FILESYSTEM_GUIDANCE,
    _FINDING_GUIDANCE,
    _LOGO_GUIDANCE,
    _VISUAL_GUIDANCE,
    _report_note,
)
from agent.project_memory import (  # noqa: F401 -- re-exported: `agent.deep_agent` stays the import path
    EPISODES_ROUTE,
    MEMORY_PATH,
    ORG_MEMORY_PATH,
    ORG_NAMESPACE,
    SECTIONS_CORE_PATH,
    SECTIONS_INDEX_PATH,
    SKILLS_MANIFEST_PATH,
    SKILLS_ROUTE,
    ProjectMemory,
    _sections_match_source,
    _whole_memory,
    build_memory_backend,
    episodes_namespace,
    gather_memory_sections,
    load_memory_index,
    load_project_memory,
    load_skills_manifest,
    load_skills_summary,
    org_namespace,
    project_namespace,
    read_memory_or_empty,
    resplit_memory_sections,
    route_local_path,
    seed_memory,
    seed_org_memory,
    seed_skill,
    skills_namespace,
    unregister_skill,
)
from agent.tools.agent_tools import make_agent_tools
from agent.tools.project_db import make_project_db_tool
from agent.tools.checks import run_all_checks

logger = logging.getLogger("tektonix")

# Real per-call cost via BudgetGuardMiddleware makes SummarizationMiddleware's
# own trigger about context quality, not cost -- the budget ceiling already
# owns cost. Trigger well before a model's real context limit so summarization
# is a normal, unremarkable event, not a last-resort save. `keep` mirrors the
# library default (last 20 messages kept verbatim after summarizing).
# Tuned alongside READ_INLINE_CAP_CHARS (agent_tools.py): a whole large source
# file can arrive inline, so the trigger needs enough headroom to admit one
# such read without churning the summarizer immediately afterward. Every
# pinned model has >=262K context, so trading some headroom for stability is
# cheap -- context quality owns this trigger, cost never did (BudgetGuard
# owns cost).
# Tokens ONLY. There used to be an OR'd ("messages", 120) clause, and it is the
# exact degenerate case described below: `keep` is 30k TOKENS, and 30k tokens
# of short tool calls is easily more than 120 messages. Observed 2026-09-09 on
# task 828ca1d9 (webapp): every compaction preserved 139 messages, so the
# message clause was true again on the very next call, and summarization fired
# before EVERY model call for over an hour -- a 3.5k-token summary regenerated
# each step, plus a failed primary-summarizer attempt each step, while the
# coordinator's own context never dropped below ~50k. Progress continued, at
# roughly twice the cost and latency per step. A message-count clause can only
# be safe against a message-count keep; with a token keep it must not exist.
# Operator-tunable since 2026-09-14 (runtime_settings' "Context" group). The
# fixed 80_000 below was 7.6% of the 1,048,576-token window the coder model
# actually has, and a real task rode it for an hour -- climb to 80k, compact to
# ~55k, climb again, 15 summarizer calls and zero lines written, because each
# compaction discarded the files it had just read. The knobs default to
# 250k/90k (runtime_settings). The two module constants below drive nothing
# live any more: they are the pair tests/test_summarization_keep.py and
# test_summarization_settings.py pin the unit rules against, and everything
# below about why the units must match and why keep must stay well under
# trigger still applies to the knobs.
def summarization_trigger() -> list[tuple[str, int]]:
    return [("tokens", _rs.as_int("summarization_trigger_tokens"))]


def summarization_keep() -> tuple[str, int]:
    """Clamped to 60% of the trigger.

    If the kept window ever approaches the trigger, summarization fires before
    EVERY model call and can never get back under it -- the task keeps paying
    for a summarizer each turn while making no progress. That is a degenerate
    state a pair of independently-set knobs can reach by accident, so it is
    forbidden here rather than documented.
    """
    trigger = _rs.as_int("summarization_trigger_tokens")
    keep = _rs.as_int("summarization_keep_tokens")
    return ("tokens", min(keep, int(trigger * 0.6)))


SUMMARIZATION_TRIGGER = [("tokens", 80_000)]
# Retention is expressed in TOKENS, deliberately matching the unit the trigger
# above uses. It was ("messages", 20), and a message-count keep against a
# token-count trigger is a unit mismatch with no relationship between the two:
# nothing bounds how much a summarization actually reclaims, because 20
# messages can be 8k tokens or 70k depending purely on how big the recent tool
# results happen to be.
#
# That is not theoretical. Measured on a real planning session (2026-08-27) at
# the exact state that fired summarization: 28 messages / 73_068 tokens, of
# which the six largest were ~10k tokens each (inline file reads). Keeping 20
# messages preserved 61_695 tokens -- it reclaimed 11k of 73k, 15%, and left
# 18k of headroom under an 80k trigger. Roughly two more file reads and it
# fired again. The session's observed cycle was: summarize, 2-3 tool calls,
# summarize, 2-3 tool calls. Two summarizations inside twelve minutes, both
# doing real work and neither buying meaningful room.
#
# The degenerate end of that mismatch is worse than churn: if the kept window
# ever exceeds the trigger on its own, summarization fires before EVERY model
# call and can never get back under, so the conversation stops making progress
# while still paying for a summarizer call each turn.
#
# 30k against an 80k trigger guarantees >=50k reclaimed every time (measured on
# the same state: 12 messages / 26_163 tokens kept, 53_837 of headroom -- about
# eight large reads instead of two). It also lowers the average context a
# planning call carries, which is where the real money goes: kimi-k3 input is
# $3/M, so every 10k tokens of avoided context is ~$0.03 off every single call
# in the conversation.
#
# Caveat worth knowing: the library keeps at least one message, so a SINGLE
# message larger than this budget still lands above it (the binary search in
# _find_token_based_cutoff cannot cut inside a message). Planning reads are
# capped at 40k chars (~12k tokens) so they cannot breach it; agent_tools.read
# offloads above READ_INLINE_CAP_CHARS for the same reason.
SUMMARIZATION_KEEP = ("tokens", 30_000)

# Planning gets a much larger window than a build task, because it is a
# READ-heavy investigation loop rather than an edit loop, and the 80k/30k pair
# above turned out to be actively harmful there.
#
# Measured on session 0616917 (2026-08-31), a HARD planning turn that ran
# 1h52m and died on its $8 ceiling having produced NO plan at all:
#
#   123 model calls, 183 tool calls, 184 summarizations
#   1,080 file reads across 99 DISTINCT files
#   src/core/bot.js read 129 times, config/pairs.json 103, gridBot.js 69
#
# That is one summarization per ~1.5 model calls. The mechanism is the gap
# between the two numbers, not either one alone: 80k trigger minus 30k keep is
# 50k of headroom, and a planning read is capped at 40k chars (~12k tokens),
# so THREE big reads refill it. The summarizer then drops the file contents,
# the model no longer has what it just read, and it reads it again -- forever,
# at ~$0.12 a round, until the budget guard stops it.
#
# The question that turn was asked spanned a strategy module, an API route and
# two frontend components. It could never hold all four at once, so it could
# never answer. 170k/70k gives 100k of headroom (~8 large reads) and leaves
# ~90k of slack under the >=262K context every pinned model has -- enough for
# the triggering call and its response. Cost per call rises with context; that
# is the intended trade, and it is far cheaper than reading bot.js 129 times.
PLANNING_SUMMARIZATION_TRIGGER = [("tokens", 170_000)]  # tokens only -- see SUMMARIZATION_TRIGGER
PLANNING_SUMMARIZATION_KEEP = ("tokens", 70_000)
# The library default (4000) is tuned for ordinary back-and-forth chat, where a
# HumanMessage recurs often. Our conversations are tool-call-heavy: one
# HumanMessage with the goal, then dozens of AIMessage/ToolMessage pairs before
# the next one. SummarizationMiddleware's own trim step (trim_messages with
# strategy="last", start_on="human") requires a HumanMessage to anchor the
# trimmed batch -- with a small token budget, a batch that's mostly tool pairs
# can have no HumanMessage in that window, so trim_messages returns an empty
# list. The library's own fallback for that case is to silently return the
# literal string "Previous conversation was too long to summarize." as the
# entire prior context, with no retry and no error -- confirmed to wipe the
# goal and prior findings from context on a real task.
#
# A bigger trim budget doesn't fix this: a regression test
# (tests/test_summarization_trim.py) confirms that raising it still produces
# an empty trim on the same failure shape, because the sole HumanMessage can
# sit further back than any finite budget reaches if the tool-heavy stretch
# after it is long enough. The trim step exists to bound the summarizer
# call's own input size -- but the trigger already bounds the untrimmed batch
# to roughly its own threshold (it is tokens-only, see summarization_trigger,
# and fires the moment the token count crosses it, so total tokens at fire
# time can't run away past that), comfortably inside any modern model's
# context window. That bound depends on the trigger staying tokens-only: the
# message-count clause removed after task 828ca1d9 must not return. So the
# trim step is redundant for our shape and only adds a failure mode --
# disable it outright (None skips trimming entirely) rather than trying to
# out-guess a budget that has no safe value for an unbounded-distance-to-
# last-HumanMessage conversation shape.
SUMMARIZATION_TRIM_TOKENS = None

# Defense-in-depth against a runaway loop that BudgetGuardMiddleware alone
# wouldn't catch -- a stuck loop making many near-zero-cost calls (routed to
# a cheap model tier) could burn enormous wall-clock/API-call-count before
# ever crossing the dollar ceiling. Deliberately generous (well above any
# normal task's real call count) so this never fires on legitimate work,
# only genuine runaway pathology. `run_limit` (per single astream_events()
# invocation, i.e. per outer "work" pass), not `thread_limit` -- a
# thread_limit would persist across every resume of a long task's whole
# lifetime, which doesn't map onto anything meaningful here the way it would
# for a genuinely single-shot agent. On the coordinator exit_behavior="error"
# so this surfaces as a real exception routed through work_node's generic
# `except Exception` handler -> a clear escalation, not a silently-truncated
# response that could get misread as a normal completion. On every subagent
# it is "end" (2026-09-25): a test-writer that hit 400 model calls after its
# tests were written took the whole pass down with it, where ending its run
# hands the coordinator what it has. The verifier and the test-writer also
# carry their own, tighter tool-call caps (VERIFIER_TOOL_CALLS,
# TEST_WRITER_TOOL_CALLS) with a WrapUpMiddleware countdown in front.
# The numbers themselves live in runtime_settings (Settings -> Runtime
# limits) so they can be retuned without an edit and a restart; the reasoning
# above is why they are shaped this way, and still applies. Read via
# _rs.as_int("model_call_run_limit") / ("tool_call_run_limit") at the point of
# use -- deliberately not re-declared here as constants, because a constant
# that no longer drives anything is a trap for the next person to edit it.

# Human-in-the-loop pre-execution approval gate. This system's only other
# safety layers are the budget ceiling and the post-hoc review-service gate
# (after code already ran). deepagents' own HumanInTheLoopMiddleware (wired
# in below via create_deep_agent's `interrupt_on=` param, backed by
# LangGraph's native interrupt()/Command(resume=...)) pauses before a
# specific tool call executes, when it matches a `when` predicate here.
#
# Deliberately narrow, not "approve every tool call" (that would make this
# system unusable -- a real task makes dozens of tool calls). Scoped to
# what the sandboxing fix (agent/tools/sandbox.py) does not already cover:
# Docker isolation bounds `bash`'s blast radius to the repo checkout, but
# genuinely sensitive files (a project's own auth/config, .env, .git
# internals, CI/deploy config) live inside that same checkout -- the sandbox
# boundary doesn't protect a repo from itself. Also gates recognizably
# destructive git/shell patterns (force-push, rm -rf, sudo) regardless of
# path, since those are dangerous even scoped to one repo.
#
# Applied to the coordinator and every subagent (below) -- not just the
# coordinator -- because investigator, despite its own system prompt's
# "read-only" framing, is given the full `bash` tool (needed for real
# find/grep-across-the-tree exploration; there's no separate read-only-
# shell primitive in this system yet) and could otherwise run something
# destructive with zero gate at all. Two decisions only (approve/reject),
# not edit/respond -- keeps the operator-facing payload and the dashboard
# UI simple; a rejected call gets a clear message back to the model instead
# of silently vanishing.
_SENSITIVE_PATH_MARKERS = (
    "config/", "auth.json", ".env", ".git/", "secret", "credential",
    "deploy", ".github/workflows", ".ssh",
    # audit C-2: the check runner executes `npm run <script>` and the reviewer
    # runs install lifecycle scripts, all defined in these files -- which the
    # agent can rewrite with its own `write`/`edit` tool. Until now that write
    # was ungated (matched no marker), so a rewritten test/build/install script
    # was an unreviewed path to host code execution. Editing any of them now
    # requires operator approval in strict mode, the same as touching a .env.
    # Lowercase (both predicates lowercase before matching).
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pnpm-workspace.yaml", "vitest.config", "jest.config", "playwright.config",
    "tsconfig", ".npmrc", "makefile", "dockerfile",
)
# MUST be lowercase: both predicates below lowercase the command before
# matching, so an uppercase character in a marker can never match anything.
# "chmod -R"/"chown -R" were written with a capital R and therefore silently
# never gated a single recursive chmod/chown from the day they were added --
# found 2026-08-24 by the auto-approve gate's own tests. Keep this list
# lowercase, and let tests/test_auto_approve_gate.py catch it if that slips.
_DANGEROUS_COMMAND_MARKERS = (
    # "> /dev/" removed from this substring list -- the regex below owns that
    # case now, because a substring cannot express the needed exception.
    "rm -rf", "git push", "sudo ", "chmod -r", "chown -r", ":(){ ",
)


# audit M-7: the substring matchers below were trivially evadable -- `rm  -rf`
# (two spaces), `rm -fr` (flag order), a tab instead of a space all slipped past.
# The list can't be made sound by extension, and it no longer needs to be: C-2
# made the Docker sandbox the real boundary (bash runs in an isolated container
# that cannot see the host, other repos, or any secret, and whose only mutable
# state is a throwaway worktree copy). This gate is now a footgun safety-net --
# it catches the obvious destructive command so an operator gets a confirm
# prompt -- not the security boundary. Normalizing whitespace and matching a few
# regexes for flag-order variants makes the net catch the demonstrated evasions.
_WS_RUN = __import__("re").compile(r"\s+")


def _normalize_command(command: str) -> str:
    return _WS_RUN.sub(" ", command.lower()).strip()


import re as _re
_DANGEROUS_COMMAND_PATTERNS = (
    _re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"),  # rm -rf / -fr / -Rf ...
    _re.compile(r"\bgit\b.*\bpush\b.*(--force|-f\b|\+)"),  # force push (any -c ... prefix)
    _re.compile(r"\bsudo\b"),
    _re.compile(r"\bchmod\s+-[a-z]*r|\bchown\s+-[a-z]*r"),  # recursive chmod/chown
    # Writing to a raw DEVICE (> /dev/sda) is destructive. Redirecting to the
    # null/stream devices is not -- and `2>/dev/null` is the single most
    # common idiom in shell, so matching it made EVERY quiet command prompt
    # for approval, auto-approve mode included (live 2026-08-27: a build's
    # plain `ls ... 2>/dev/null` and `find ... 2>/dev/null` both gated as
    # "destructive"; the operator asked why auto-approve wasn't working).
    _re.compile(r">\s*/dev/(?!null\b|zero\b|stdout\b|stderr\b|tty\b|fd/)"),
    # dd writing to a raw device -- was never caught (no `>` involved), found
    # while fixing the redirect pattern above.
    _re.compile(r"\bof=/dev/(?!null\b|zero\b)"),
    _re.compile(r":\(\)\s*\{"),  # fork bomb
    _re.compile(r"\bfind\b.*-delete"),
)

# `ln -s`, `ln --symbolic`, and the same via env/xargs. Word-boundary anchored
# so "align -symbolic" or a filename containing "ln -s" does not trip it.
_SYMLINK_RE = _re.compile(r"(^|[;&|]\s*|\s)ln\s+(-[a-zA-Z]*s[a-zA-Z]*|--symbolic)\b")


def _matches_dangerous(command: str) -> bool:
    norm = _normalize_command(command)
    if any(marker in norm for marker in _DANGEROUS_COMMAND_MARKERS):
        return True
    return any(p.search(norm) for p in _DANGEROUS_COMMAND_PATTERNS)


def _bash_needs_approval(req) -> bool:
    raw = str(req.tool_call["args"].get("command", ""))
    norm = _normalize_command(raw)
    if any(marker in norm for marker in _SENSITIVE_PATH_MARKERS):
        return True
    if _bash_creates_a_symlink(norm):
        return True
    return _matches_dangerous(raw)


def _bash_creates_a_symlink(command: str) -> bool:
    """audit C2/H2: `ln -s <anything> node_modules` inside the worktree decides
    what the HOST bind-mounts into the next container. sandbox.py now refuses
    targets outside the project's own paths, so this is defence in depth --
    but a symlink whose target the agent chose is worth surfacing, and it
    matched no marker before."""
    return bool(_SYMLINK_RE.search(command))


# ---------------------------------------------------------------------------
# What auto mode still stops for: a deletion that loses real work.
#
# 2026-09-11, operator report: auto mode interrupted about every 30 seconds.
# The predicate it used (_bash_is_destructive) fires on any `rm -rf`, and the
# call that kept firing was `cd /tmp && rm -rf u && mkdir u && ...` -- scratch
# space inside a container, thrown away when the command ends. A prompt for
# that is noise, and noise is what makes an operator stop reading prompts.
#
# bash NEVER runs on the host: agent_tools.py sends every command through
# run_shell_sandboxed -- a per-command Docker container with all Linux
# capabilities dropped, no-new-privileges, a pid cap, no credentials in its
# environment (no SSH key, no router key, no GitHub token), and exactly one
# writable mount: the task's throwaway worktree at /workspace. sudo cannot
# escalate there, a push cannot authenticate, a fork bomb hits the pid cap and
# a raw-device write has no device to reach. Those markers still gate in
# strict mode, where they read as "look at this"; in auto mode the container
# has already answered them.
#
# What the container does not answer is the loss of work: the worktree holds
# the task's own edits, and a delete that reaches the repo -- or anything
# outside scratch -- throws away state no revert brings back. That is the one
# class the operator asked to keep, and it is the only one auto mode stops for.
_SCRATCH_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm")

# Names whose contents a build regenerates. Deleting one is a clean step, not
# a loss: `rm -rf dist && npm run build` is ordinary work between check runs.
_REGENERABLE_NAMES = frozenset({
    "node_modules", "dist", "build", "out", ".next", ".nuxt", ".svelte-kit",
    ".cache", ".turbo", ".parcel-cache", ".vite", "coverage", ".nyc_output",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".eggs", ".gradle", ".venv", "venv", "tmp", ".tmp", ".output",
    "tsconfig.tsbuildinfo", ".ds_store",
})

# sandbox.py runs every command with `-w /workspace` (the worktree mount), so
# a relative target resolves against that unless the command cd's first.
_SANDBOX_CWD = "/workspace"

_SEGMENT_SPLIT = _re.compile(r"(\|\||&&|[;|\n()])")   # separators kept: `(` and `)` mark a subshell
_DELETE_COMMANDS = ("rm", "rmdir", "shred", "unlink")
# Commands that run another command they are given: `env rm`, `xargs rm`,
# `sh -c 'rm …'`, `find … -exec rm`. What they run is not parsed here; a
# delete word among their arguments makes the segment unreadable, so it
# asks (2026-09-29 audit, A2).
_WRAPPER_COMMANDS = frozenset({
    "env", "xargs", "sh", "bash", "dash", "zsh", "busybox", "nice", "nohup", "time", "timeout",
    "sudo", "doas", "exec", "command", "builtin", "eval", "find", "stdbuf", "ionice", "setsid",
    "chroot", "parallel", "watch",
})
_DELETE_WORD_RE = _re.compile(r"""(^|[\s;&|('"])(rm|rmdir|shred|unlink)(\s|$)""")
# Marks a loss entry that is not a path to look up: a shell variable, an
# unparseable segment, `rm` with only flags, a wholesale `git clean -f`.
# Those always ask, whatever git says about the worktree.
_ALWAYS = "\x00"
_UNPARSEABLE = _ALWAYS + "unparseable"
# Marks a path a `mv` lands on. Only a tracked FILE at that exact path is a
# loss: `mv new.ts src/` into a tracked directory overwrites nothing.
_OVERWRITE = "\x01"

# 2026-10-22 audit, A24: delete forms neither mode asked about. An inline
# script (`python3 -c`, `node -e`, a heredoc fed to an interpreter) is not
# parsed: when its text names a delete or a process call, it is unreadable
# and asks. Not every inline script -- `python3 -c` and `node -e` are how the
# coder probes, and a prompt for each one is the noise of 2026-09-11.
_INTERPRETER_INLINE_RE = _re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:python[0-9.]*|node|nodejs|perl|ruby|bun|deno)\b[^\n;&|]*?"
    r"(?:\s(?:-c|-e|-E|-ne|-pe|--eval|-p|--print|eval)(?=[\s'\"])|<<)")
_INLINE_DELETE_RE = _re.compile(
    r"rmtree|unlink|rmdir|rmSync|\brm\s*\(|\.rm\b|\bremove\w*\s*\(|truncate|\bsystem\s*\(|subprocess|"
    r"child_process|\bexec\w*\s*\(|\bspawn\w*\s*\(|\bpopen\b|FileUtils|File\.delete|shutil\.move|\brename\w*\s*\(")


def _segments(command: str) -> list[tuple[int, list[str]]]:
    """The command split into simple segments, each tokenised, with the
    subshell depth it runs at. A segment that will not tokenise comes back
    as one unparseable token, so a caller fails closed on it instead of
    skipping past it."""
    import shlex
    out: list[tuple[int, list[str]]] = []
    depth = 0
    for raw in _SEGMENT_SPLIT.split(command):
        if raw == "(":
            depth += 1
            continue
        if raw == ")":
            depth = max(0, depth - 1)
            continue
        raw = raw.strip()
        if not raw or raw in ("||", "&&", ";", "|"):
            continue
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = [_UNPARSEABLE, raw]                      # the text kept, so a delete in it is seen
        if tokens:
            out.append((depth, tokens))
    return out


def _hides_a_delete(tokens: list[str]) -> bool:
    """A delete command among a wrapper's arguments -- as its own word
    (`xargs rm`), a path (`env /bin/rm`), or inside a string it hands to a
    shell (`sh -c 'cd x; rm -rf y'`)."""
    return any(tok.rsplit("/", 1)[-1] in _DELETE_COMMANDS or _DELETE_WORD_RE.search(tok)
               for tok in tokens[1:])


def _resolve(target: str, cwd: str) -> str:
    import posixpath
    if target.startswith("~"):
        return posixpath.normpath("/root" + target[1:])      # $HOME in the image
    if not posixpath.isabs(target):
        target = posixpath.join(cwd, target)
    return posixpath.normpath(target)


def _target_is_scratch(target: str, cwd: str) -> bool:
    """True when deleting this path loses nothing -- container scratch space,
    or something a build regenerates. Anything uncertain is not scratch."""
    if not target or any(ch in target for ch in "$`"):
        return False                                         # a variable: cannot judge it
    resolved = _resolve(target, cwd)
    segments = [seg.lower() for seg in resolved.split("/") if seg not in ("", ".")]
    if not segments or ".." in segments:
        return False                                         # "/" itself, or a path climbing out
    if ".git" in segments:
        return False                                         # history is work
    for root in _SCRATCH_ROOTS:
        root_segments = [seg for seg in root.split("/") if seg]
        if segments[:len(root_segments)] == root_segments and len(segments) > len(root_segments):
            return True                                      # inside scratch, never the root itself
    return any(seg in _REGENERABLE_NAMES for seg in segments)


_ASSIGNMENT_RE = re.compile(
    r"""(?:^|[;&|(]\s*|\s)([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"$`]*)"|'([^']*)'|([^\s;&|"'$`()]+))(?=\s|;|&|\||$)""")


def _expand_local_assignments(command: str) -> str:
    """`P=/workspace/.scratch/x.patch && cp a "$P" && rm -f "$P"` deletes a
    path the command itself spelled out. Substituting the plain assignments
    made earlier in the same command lets the target be read. Only literal
    values count: a value holding another variable, a substitution or a
    quote stays unreadable, and the delete still asks. 2026-09-29: the coder
    kept a scratch path in a variable and every cleanup asked for approval,
    five times in one task, with auto mode on."""
    # Left to right, so a reference sees only the assignments BEFORE it:
    # `P=src; rm "$P"; P=/tmp/x` deletes src, whatever P becomes later.
    # (2026-09-29 audit, A1: the first version let the last assignment win.)
    assignments = [(m.start(), m.group(1), next((g for g in m.groups()[1:] if g is not None), None))
                   for m in _ASSIGNMENT_RE.finditer(command)]
    if not any(v is not None for _, _, v in assignments):
        return command

    def _sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        value = None
        for start, n, v in assignments:
            if start < m.start() and n == name and v is not None:
                value = v
        return value if value is not None else m.group(0)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", _sub, command)


# A heredoc fed to one of these, and written to a file, only becomes that
# file's contents: nothing in its body runs. Fed to anything else -- a shell,
# an interpreter, ssh -- or piped onward, the body may well run, so it stays
# in for the gate.
_HEREDOC_TEXT_SINKS = frozenset({"cat", "tee"})
_HEREDOC_RE = re.compile(r"<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_HEREDOC_OP_RE = re.compile(r"(?<!<)<<(?!<)")                 # not a `<<<` here-string
# What ends a simple command, read on a masked line. `>|` is a clobber
# redirect, and `>&` and `&>` are redirects, not a pipe or a background `&`.
_COMMAND_END_RE = re.compile(r"\|\||&&|;|(?<!>)\|(?!\|)|(?<![<>])&(?!>)|[()]")
_STDOUT_REDIRECT_RE = re.compile(r"(?<![0-9<>&])(?:1|&)?>>?\|?\s*(?=[^\s&|;<>()])")
_ANY_REDIRECT_RE = re.compile(r"(?<![<>&])\d*&?[<>]{1,2}[|&]?\s*[^\s&|;<>()]+")


def _masked_line(command: str, start: int) -> tuple[str, int]:
    """The logical line from `start` with quoted text and comments blanked to
    the same length, and the index it ends at: the first newline outside
    quotes that no backslash continues. A `<<` in quotes or after `#` is then
    no operator, and an index into the mask is an index into the command."""
    out: list[str] = []
    i, n, quote = start, len(command), None
    while i < n:
        c = command[i]
        if quote is None and c == "\n":
            break
        if quote != "'" and c == "\\" and i + 1 < n:
            out.append("__")
            i += 2
            continue
        if quote:
            out.append(c if c == quote else "_")
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (i == start or command[i - 1] in " \t;&|()"):
            stop = command.find("\n", i)
            stop = n if stop < 0 else stop
            out.append(" " * (stop - i))
            i = stop
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out), i


def _heredoc_is_inert(masked: str, raw: str, at: int) -> bool:
    """Is the heredoc whose `<<` is at `at` only written to a file? Its
    command is `cat` with a stdout redirect or `tee` with a file, and its
    output goes nowhere else: no pipe before the command ends, and no process
    substitution on the line."""
    if ">(" in masked or "<(" in masked:
        return False
    before = list(_COMMAND_END_RE.finditer(masked, 0, at))
    begin = before[-1].end() if before else 0
    after = _COMMAND_END_RE.search(masked, at)
    if after and after.group() == "|":
        return False                                          # `cat <<X | bash` runs the body
    stop = after.start() if after else len(masked)
    words = raw[begin:stop].split()
    head = words[0].rsplit("/", 1)[-1] if words else ""
    if head not in _HEREDOC_TEXT_SINKS:
        return False
    simple = _HEREDOC_RE.sub(lambda m: " " * len(m.group()), masked[begin:stop])
    if _STDOUT_REDIRECT_RE.search(simple):
        return True
    if head == "cat":
        return False                                          # to the terminal, or to whatever reads it
    return any(not w.startswith("-") for w in _ANY_REDIRECT_RE.sub(" ", simple).split()[1:])


def _strip_inert_heredocs(command: str) -> str:
    """The command without the bodies of heredocs that are only written out.

    2026-10-21, the golden suite: the coder wrote a throwaway mutation-test
    script with `cat > .scratch/run.sh <<'SH' ... rm -rf "$d" ... SH`, and the
    gate read the `rm` in the script's TEXT as a delete about to run, could
    not resolve `$d`, and parked the task on an approval no one was there to
    give. Two of the first five tasks blocked that way. Writing a script runs
    none of it; running it later is a separate command, judged then.

    2026-10-22 audit, A19: the first version judged a heredoc by the command
    before `<<` alone, so `cat <<'X' | bash` -- a body that runs -- was
    stripped too, and so was a `<<` in quotes or after `#`. Bodies are now
    consumed in the order their operators appear, a body something may run is
    kept (and read as a script itself), and an operator that will not parse
    leaves the rest of the command as it is.
    """
    out: list[str] = []
    pos, n = 0, len(command)
    while pos < n:
        masked, end = _masked_line(command, pos)
        raw = command[pos:end]
        out.append(raw)
        heredocs = []
        for op in _HEREDOC_OP_RE.finditer(masked):
            m = _HEREDOC_RE.match(raw, op.start())
            if m is None:
                if end < n:
                    out.append(command[end + 1:])             # bodies unknown: strip nothing more
                return "\n".join(out)
            heredocs.append((m.group(2) or m.group(3) or m.group(4), m.group(1) == "-",
                             _heredoc_is_inert(masked, raw, op.start())))
        pos = end + 1
        for delimiter, strip_tabs, inert in heredocs:
            body: list[str] = []
            closing = None
            while pos <= n:
                nl = command.find("\n", pos)
                nl = n if nl < 0 else nl
                line = command[pos:nl]
                pos = nl + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    closing = line
                    break
                body.append(line)
            if not inert:
                if body:
                    out.append(_strip_inert_heredocs("\n".join(body)))
                if closing is not None:
                    out.append(closing)
    return "\n".join(out)


def _git_discards_edits(tokens: list[str]) -> bool:
    """`git checkout -- .`, `git checkout -f`, `git restore` (but not a
    `--staged`-only unstage), `git switch -f` and `git stash drop|clear`:
    each throws away uncommitted work, as `git reset --hard` does. A plain
    `git checkout <branch>` refuses to, so it does not ask."""
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    if i >= len(tokens):
        return False
    sub, args = tokens[i], tokens[i + 1:]
    force = any(a in ("-f", "--force", "--discard-changes") for a in args)
    if sub == "checkout":
        return force or "--" in args or "." in args
    if sub == "switch":
        return force
    if sub == "restore":
        staged_only = any(a in ("-S", "--staged") for a in args) and not any(
            a in ("-W", "--worktree") for a in args)
        return not staged_only
    if sub == "stash":
        return bool(args) and args[0] in ("drop", "clear")
    return False


def _deletions_that_lose_work(command: str) -> list[str]:
    """Targets of this command's deletions that are not scratch. Empty means
    nothing of value is being deleted. A target that cannot be read (a shell
    variable, an unparseable segment, `rm` with only flags) is reported, so
    the caller asks about it rather than assuming."""
    command = _expand_local_assignments(_strip_inert_heredocs(command))
    cwd = _SANDBOX_CWD
    # The cwd outside each open subshell: `(cd /tmp); rm -rf x` deletes
    # /workspace/x, because the cd ran in a child that has since exited.
    outer_cwds: list[str] = []
    losses: list[str] = []

    def _record(targets: list[str], prefix: str = "") -> None:
        for t in targets:
            if any(ch in t for ch in "$`") or t.startswith(_ALWAYS):
                losses.append(_ALWAYS + t)                    # unreadable: ask, never resolve
                continue
            if _target_is_scratch(t, cwd):
                continue
            resolved = _resolve(t, cwd)
            if ".git" in resolved.lower().split("/"):
                # git cannot report on its own directory -- ls-files lists
                # nothing under .git, so the tracked-content check would wave
                # a `rm -rf .git` straight through. Always ask.
                losses.append(_ALWAYS + resolved)
            else:
                losses.append(prefix + resolved)

    inline = _INTERPRETER_INLINE_RE.search(command)
    if inline and _INLINE_DELETE_RE.search(command, inline.start()):
        losses.append(_ALWAYS + "an inline script that deletes or runs something")

    for depth, tokens in _segments(command):
        while len(outer_cwds) < depth:
            outer_cwds.append(cwd)                            # entering a subshell
        while len(outer_cwds) > depth:
            cwd = outer_cwds.pop()                            # leaving one: its cd is gone
        if tokens[0] == _UNPARSEABLE:
            # `bash -c "cd src; rm -rf lib"` splits at the `;` into two halves
            # that will not tokenise. The half with the delete word asks; a
            # heredoc line with a stray apostrophe does not (2026-09-29).
            if _DELETE_WORD_RE.search(tokens[1]):
                losses.append(_ALWAYS + tokens[1].strip())
            continue
        head = tokens[0].rsplit("/", 1)[-1]
        if head == "cd" and len(tokens) > 1 and not any(ch in tokens[1] for ch in "$`"):
            cwd = _resolve(tokens[1], cwd)
            continue
        if head == "git" and "clean" in tokens[1:4] and any(
                (t.startswith("-") and not t.startswith("--") and "f" in t) or t == "--force"
                for t in tokens):
            losses.append(_ALWAYS + "git clean -f")           # removes untracked work wholesale
            continue
        if head == "git" and _git_discards_edits(tokens):
            losses.append(_ALWAYS + " ".join(tokens))         # uncommitted edits, gone
            continue
        if head == "rsync" and any(t.startswith(("--del", "--remove-source-files")) for t in tokens):
            losses.append(_ALWAYS + " ".join(tokens))         # what it removes depends on both trees
            continue
        if head == "truncate":
            targets, skip = [], False
            for t in tokens[1:]:
                if skip:
                    skip = False
                elif t in ("-s", "-r", "--size", "--reference"):
                    skip = True                               # the option's value, not a file
                elif not t.startswith("-"):
                    targets.append(t)
            if not targets:
                losses.append(_ALWAYS + " ".join(tokens))
                continue
            _record(targets)
            continue
        if head == "git" and "reset" in tokens[1:4] and "--hard" in tokens:
            losses.append(_ALWAYS + "git reset --hard")       # throws away every uncommitted edit
            continue
        if head == "find" and "-delete" in tokens:
            paths = []
            for tok in tokens[1:]:
                if tok.startswith("-"):
                    break                                     # find's paths come before its tests
                paths.append(tok)
            _record(paths or ["."])
            continue
        if head in _WRAPPER_COMMANDS and _hides_a_delete(tokens):
            losses.append(_ALWAYS + " ".join(tokens))         # a delete run by something else: ask
            continue
        if head == "mv":
            args = [t for t in tokens[1:] if not t.startswith("-")]
            if len(args) < 2 or any(t == "-t" or t.startswith("--target-directory") for t in tokens):
                losses.append(_ALWAYS + " ".join(tokens))     # a form not read here: ask
                continue
            dest, sources = args[-1], args[:-1]
            # The file `mv` lands on, or the file it becomes inside a directory.
            _record([dest, *(dest.rstrip("/") + "/" + s.rstrip("/").rsplit("/", 1)[-1] for s in sources)],
                    prefix=_OVERWRITE)
            continue
        if head in _DELETE_COMMANDS:
            targets = [t for t in tokens[1:] if not t.startswith("-")]
            if not targets:
                losses.append(_ALWAYS + " ".join(tokens))     # flags only: unreadable, so ask
                continue
            _record(targets)
    return losses


def _is_tracked_file(repo_root: str, target: str) -> bool:
    """Is there a tracked file at exactly this path? The `mv` question: a
    move onto a tracked file overwrites it, a move into a tracked directory
    does not. Unknown means ask, as in _covers_tracked_files."""
    rel = target[len(_SANDBOX_CWD) + 1:] if target.startswith(_SANDBOX_CWD + "/") else target
    if not rel or rel == _SANDBOX_CWD:
        return True
    try:
        r = subprocess.run(["git", "-C", repo_root, "ls-files", "--", rel],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 -- unknown means ask
        return True
    if r.returncode != 0:
        return True
    return rel in r.stdout.splitlines()


def _covers_tracked_files(repo_root: str, target: str) -> bool:
    """Does this delete target cover anything git tracks in the worktree?

    The second half of "loses work", and the half a command string cannot
    answer on its own (2026-09-11, second report: the coder writes a probe
    script, runs it, and deletes it -- `cat > scripts/probe.mjs <<EOF ... EOF;
    node scripts/probe.mjs; rm -f scripts/probe.mjs` -- and every cleanup
    asked for approval). A file git never heard of is the agent's own
    scratch: deleting it changes nothing in the diff the operator reviews.
    A tracked path is repo content, and removing it is exactly the deletion
    worth stopping for.

    `git ls-files -- <path>` lists tracked paths under a directory as well as
    a file, so a bare `src` or `.` answers correctly. Anything that fails --
    no repo, a timeout, a path git refuses -- returns True: unknown means ask.
    """
    rel = target[len(_SANDBOX_CWD) + 1:] if target.startswith(_SANDBOX_CWD + "/") else target
    if not rel or rel == _SANDBOX_CWD:
        return True                                          # the worktree root itself
    try:
        r = subprocess.run(["git", "-C", repo_root, "ls-files", "--", rel],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 -- unknown means ask
        return True
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def _bash_deletes_real_work(req, repo_root: str | None = None) -> bool:
    """Auto mode's only bash gate -- see the block comment above.

    With `repo_root`, a delete inside the worktree is judged against git:
    tracked content asks, the agent's own untracked scratch does not. Without
    it (the module-level default, and what the tests pin) every non-scratch
    delete asks, which is the safe direction to be wrong in.
    """
    targets = _deletions_that_lose_work(str(req.tool_call["args"].get("command", "")))
    if not targets or repo_root is None:
        return bool(targets)
    for target in targets:
        if target.startswith(_ALWAYS):
            return True                                      # unreadable, or a wholesale clean
        overwrite = target.startswith(_OVERWRITE)
        target = target[len(_OVERWRITE):] if overwrite else target
        if not (target == _SANDBOX_CWD or target.startswith(_SANDBOX_CWD + "/")):
            return True                                      # outside the worktree: always ask
        if overwrite:
            if _is_tracked_file(repo_root, target):
                return True                                  # a mv onto tracked content
            continue
        if _covers_tracked_files(repo_root, target):
            return True
    return False


def _bash_is_destructive(req) -> bool:
    """The recognisably destructive shape: rm -rf, a force push, sudo, a fork
    bomb, a raw-device write.

    Deliberately NOT the same predicate as _bash_needs_approval: the two
    marker lists guard genuinely different risks. A sensitive PATH match
    (reading .env, editing .github/workflows) is about touching something
    that deserves a second look, and it's the case that fires constantly on
    ordinary work -- that's the noise auto-approve exists to remove.

    Auto mode used this as its gate until 2026-09-11 and it was too wide:
    it cannot tell `rm -rf /tmp/scratch` from `rm -rf src`, and the sandbox
    already neutralises everything here except the loss of work. Auto mode
    asks _bash_deletes_real_work instead; this stays as strict mode's notion
    of "destructive", which is also what the tests pin.
    """
    return _matches_dangerous(str(req.tool_call["args"].get("command", "")))


def _writes_a_git_dir(path: str) -> bool:
    """A write that lands the worktree's `.git` pointer, or anything that looks
    like a git dir or a hook.

    audit H2: the sensitive-path list matches ".git/" WITH the slash, so it
    caught `.git/hooks/pre-commit` but not the bare pointer FILE `.git` --
    and rewriting that pointer is what re-aims every host-side git command at
    an agent-controlled git dir (see agent/tools/git.py). Hooks are disabled
    unconditionally there now, so this is the second lock rather than the
    only one, but the write itself still deserves a prompt.
    """
    norm = path.replace("\\", "/").lower().rstrip("/")
    base = norm.rsplit("/", 1)[-1]
    if base == ".git":                      # the pointer file itself
        return True
    if "/hooks/" in norm or base == "hooks":  # any hooks directory
        return True
    # a bare git dir the agent is assembling under another name
    return base in ("head", "config") and "/objects/" not in norm and (
        norm.endswith("/head") and "/.git/" not in norm)


def _file_op_needs_approval(req) -> bool:
    args = req.tool_call["args"]
    path = str(args.get("path", "")).lower()
    if any(marker in path for marker in _SENSITIVE_PATH_MARKERS):
        return True
    return _writes_a_git_dir(path)


def _describe_bash(tool_call, state, runtime) -> str:
    return f"Approval needed: run this shell command?\n\n{tool_call['args'].get('command', '')}"


def _describe_file_op(tool_call, state, runtime) -> str:
    name = tool_call["name"]
    path = tool_call["args"].get("path", "")
    return f"Approval needed: {name} a sensitive-looking path?\n\npath: {path}"


def _describe_ask_user(tool_call, state, runtime) -> str:
    # Same (tool_call, state, runtime) signature as _describe_bash -- a bare
    # single-arg lambda here crashes the work node the moment ask_user fires.
    args = tool_call.get("args", {}) or {}
    text = "QUESTION FROM THE AGENT:\n" + str(args.get("question", ""))
    if args.get("options"):
        text += "\n\nOptions:\n" + str(args["options"])
    return text


def _make_ask_user_tool():
    """ask_user -- the agent's clarification channel to the operator.

    Exists so an ambiguous goal (two reasonable readings that lead to
    materially different work) gets resolved by asking rather than guessed
    silently. The tool never executes: the HITL middleware interrupts it and
    the operator's typed answer becomes the ToolMessage via the native
    `respond` decision -- the library's own documented "ask user"-style-tool
    pattern.
    """

    @tool
    def ask_user(question: str, options: str = "") -> str:
        """Ask the OPERATOR one focused clarifying question and wait for
        their answer. Use BEFORE starting large work when the goal has a
        consequential ambiguity -- two reasonable readings that lead to
        materially different implementations. Give concrete options when
        they exist (e.g. "A) paginate the list  B) optimize image caching").
        Do NOT use for anything you can verify yourself in the repo, and do
        not ask more than one question at a time. The tool result is the
        operator's own reply -- treat it as authoritative direction."""
        return (
            f"{HARNESS} (no operator response captured -- proceed with your best "
            "judgment and state the assumption you are making)"
        )

    return ask_user


# Strict mode -- the default, auto mode off -- gates on the marker lists
# above and does NOT run the loss-of-work check auto mode uses. So the
# stricter-sounding mode is the weaker one for ordinary deletes: `rm -r src`
# (no -f), `rm src/app.ts`, `git reset --hard`, `git clean -fdx` and the
# A24 forms run unasked here and ask in auto mode (2026-10-22 audit, A25).
# Known and left as it is: the markers are a footgun net, not a list to
# grow, and the sandbox is the boundary.
INTERRUPT_ON = {
    "ask_user": {
        "allowed_decisions": ["respond"],
        # No `when` predicate: every call interrupts -- the whole point is
        # a human answer; the tool body is only a fallback if something
        # ever bypasses the interrupt.
        "description": _describe_ask_user,
    },
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        "when": _bash_needs_approval,
        "description": _describe_bash,
    },
    "write": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
    "edit": {
        "allowed_decisions": ["approve", "reject"],
        "when": _file_op_needs_approval,
        "description": _describe_file_op,
    },
}

# Auto-approve mode (per-user opt-in, User.auto_approve_commands, set from
# the dashboard's Settings tab). Removes the approval prompt for the
# sensitive-PATH class -- reading a config file, editing a workflow -- which
# is the case that fires constantly during ordinary work and is what makes
# a long task need babysitting.
#
# What it deliberately does NOT remove:
#   * a deletion that loses real work (_bash_deletes_real_work) stays gated,
#     always: the repo worktree, a path outside the container's scratch
#     dirs, anything under .git, or a target too unreadable to judge. Those
#     are the actions a revert can't undo, and a preference toggle is the
#     wrong instrument for switching them off. Scratch and build-output
#     deletes go through, because losing them costs nothing.
#   * ask_user stays interrupting. It's the agent's question channel, not a
#     safety gate -- auto-approving it would just feed every clarifying
#     question the generic "use your best judgment" fallback instead of the
#     operator's real answer, which makes the agent worse, not faster.
#
# This grants no capability the operator doesn't already have: they could
# approve each of these by hand today. It only removes the clicking.
INTERRUPT_ON_AUTO_APPROVE = {
    "ask_user": INTERRUPT_ON["ask_user"],
    "bash": {
        "allowed_decisions": ["approve", "reject"],
        # Deletions that lose real work, NOT every destructive marker: the
        # sandbox already answers sudo, a push, a fork bomb and a device
        # write, and gating scratch cleanup made auto mode interrupt every
        # half minute. See _bash_deletes_real_work's own block comment.
        "when": _bash_deletes_real_work,
        "description": _describe_bash,
    },
}


def interrupt_on_for(auto_approve_commands: bool, repo_root: str | None = None) -> dict:
    """The gate map for this task. In auto mode, `repo_root` lets the bash
    predicate ask git whether a delete target is repo content or the agent's
    own scratch; without it every non-scratch delete asks."""
    if not auto_approve_commands:
        return INTERRUPT_ON
    if repo_root is None:
        return INTERRUPT_ON_AUTO_APPROVE
    return {
        **INTERRUPT_ON_AUTO_APPROVE,
        "bash": {
            **INTERRUPT_ON_AUTO_APPROVE["bash"],
            "when": lambda req: _bash_deletes_real_work(req, repo_root),
        },
    }


SCRATCH_DIR = ".scratch"


def exclude_scratch(repo_root: str | None) -> None:
    """Keep /workspace/.scratch/ out of git for this repository -- the prompts
    send probe scripts there. Written to the repo's own .git/info/exclude,
    which every task worktree shares and no commit contains. Never raises."""
    if not repo_root:
        return
    try:
        common = subprocess.run(["git", "-C", repo_root, "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if not common:
            return
        exclude = Path(common if os.path.isabs(common) else os.path.join(repo_root, common)) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        line = f"/{SCRATCH_DIR}/"
        existing = exclude.read_text() if exclude.exists() else ""
        if line not in existing.split("\n"):
            with exclude.open("a") as fh:
                fh.write(("" if existing.endswith("\n") or not existing else "\n")
                         + f"# the agent's throwaway probes (agent/deep_agent.py)\n{line}\n")
    except Exception:  # noqa: BLE001 -- a missing exclude is a hygiene loss, never a failed task
        logger.warning("could not exclude %s in %s", SCRATCH_DIR, repo_root, exc_info=True)


def approval_gates(repo: str, auto_approve_commands: bool, repo_root: str | None = None) -> dict:
    """What stops for a person in this task.

    A benchmark project has nobody to ask: no approvals at all -- not a
    deletion, not a question (ask_user then answers with its own "proceed on
    your best judgment" fallback). Its workspace is a throwaway copy and every
    change is in the diff the reviewer reads. Only benchmark projects: every
    other project keeps its gates, whatever auto-approve says (operator's
    rule, 2026-09-24)."""
    if (PROJECTS.get(repo) or {}).get("benchmark"):
        return {}
    return interrupt_on_for(auto_approve_commands, repo_root)



def _make_run_checks_tool(repo_root: str, repo: str):
    @tool
    async def run_checks() -> str:
        """Run this project's REAL typecheck, lint, and test suite (the
        exact same commands the outer verification gate will run after you
        report done). Call this yourself before claiming a step or the
        whole task is complete -- it's much cheaper and faster than finding
        out from the outer gate that something you thought was fixed
        actually isn't. The outer gate re-runs these checks itself
        regardless of what you report; this tool exists purely so you get
        the feedback sooner, not as a substitute for that gate.
        """
        result = await run_all_checks(repo_root, repo)
        return result["summary"]

    return run_checks


# Tool-call caps for the two seats that check a fix, tighter than the
# runtime knob: a targeted check, not a sweep (2026-09-25: one verifier ran
# 49 calls on a fix that was already right; one test-writer ran 352 identical
# commands). Each has a WrapUpMiddleware countdown in front of it.
VERIFIER_TOOL_CALLS = 30
TEST_WRITER_TOOL_CALLS = 60


async def build_deep_agent(
    config: Config,
    repo: str,
    budget_usd: float,
    checkpointer,
    store: BaseStore,
    starting_cost: float = 0.0,
    starting_last_failed_edit: str | None = None,
    auto_approve_commands: bool = False,
    route: str = "general",
    task_id: str | None = None,
    goal: str = "",
    reference_repos: list[str] | None = None,
):
    """NOTE: async, unlike a typical factory -- it needs to `await` reading
    both memory files before constructing the agent. This is a deliberate
    workaround, not the deepagents-native path: `create_deep_agent`'s own
    `memory=[...]` parameter (backed by MemoryMiddleware) is broken for this
    system specifically -- confirmed empirically that MemoryMiddleware's
    `download_files`/`adownload_files` call the store synchronously
    (`store.get(...)`), and AsyncPostgresStore explicitly raises
    `InvalidStateError` on synchronous calls from within a running event loop
    ("Synchronous calls to AsyncPostgresStore detected in the main event
    loop... replace `store.get(...)` with `await store.aget(...)`"). The
    result was a silent `(No memory loaded)` in the real system prompt -- no
    exception surfaced. Reading memory ourselves via the same async-safe
    `aread()` path agent/project_memory.py already uses sidesteps the bug
    entirely and produces the same end result (memory content in the system
    prompt) via a confirmed-working path instead of a confirmed-broken one.
    """
    # The task's own workspace (agent/workspaces.py); the project's when there
    # is no task -- or, for a task whose workspace does not exist yet, which
    # the work node creates before it gets here.
    from agent.workspaces import own_or_template  # noqa: PLC0415
    repo_root = own_or_template(PROJECTS[repo]["sandbox"], task_id)
    # Which of the paths this task names are not on disk. Appended to the
    # coordinator's prompt AND to every subagent's, because the subagents are
    # what go looking: on task 3ee0d030 seven delegations each searched for a
    # file the task existed in order to create, and the coordinator read the
    # resulting "it does not exist" as a blocker. See agent/new_files.py.
    absent_files = new_files.guidance(repo_root, goal)
    # One gate, shared by the coordinator and every subagent -- investigator
    # carries the full bash tool too, so a laxer gate there would be a hole.
    # repo_root so auto mode can tell repo content from the agent's own
    # scratch when it judges a delete (see _bash_deletes_real_work).
    interrupt_on = approval_gates(repo, auto_approve_commands, repo_root)
    exclude_scratch(repo_root)
    tracker = BudgetTracker(budget_usd=budget_usd, starting_cost=starting_cost)
    backend = build_memory_backend(repo, store)

    project_tools, last_failed_edit_ref = make_agent_tools(
        repo_root, backend=backend, initial_last_failed_edit=starting_last_failed_edit,
        benchmark=bool((PROJECTS.get(repo) or {}).get("benchmark")),
    )
    # Read-only SQL against this project's own application database, when it
    # has one. The codebase describes the schema; only this shows what's
    # actually IN the database -- see project_db.py for why it lives here
    # rather than inside the sandbox container.
    db_tool = make_project_db_tool(repo)
    if db_tool is not None:
        project_tools = [*project_tools, db_tool]
    tool_by_name = {t.name: t for t in project_tools}
    # No write/edit for investigator -- read/bash/describe_image are all
    # read-only against the real repo. describe_image is required, not
    # optional: without it, an investigator asked to look at an uploaded
    # image has no way to actually see one -- `read` returns raw bytes as
    # text (not a description), and the built-in read_file tool can't reach
    # the real repo filesystem at all (see _FILESYSTEM_GUIDANCE).
    # GitHub PR tools (read-only, host-side, only when a token is configured):
    # the coordinator, the investigator and the general-purpose seat all read
    # PRs; the test-writer has no use for them.
    # A benchmark project (projects.json `benchmark: true`) runs with nothing
    # that reaches outside its own repository: no GitHub, no web pages, no
    # other projects. A benchmark task's real fix is public -- on GitHub, in
    # the upstream repo -- and a result reached by looking it up is not a
    # result. Its sandbox has no network either (sandbox_network).
    benchmark = bool((PROJECTS.get(repo) or {}).get("benchmark"))
    github_tools = [] if benchmark else make_github_tools(token_source(config))
    github_tools = github_tools if benchmark else [*github_tools, make_github_inbox_tool(store)]
    project_tools = [*project_tools, *github_tools]

    # Seeing the work. A frontend task used to be done blind -- edit the CSS,
    # run the tests, and neither the agent nor the reviewer ever looked at the
    # page. browse_page renders any public URL (a design reference, a staging
    # deploy, a project that runs somewhere else); preview_app starts THIS
    # project in the sandbox and renders that. Both come back as visible text
    # plus a description of how it actually looks.
    from agent.tools.planning_tools import make_browse_page_tool  # noqa: PLC0415
    from agent.tools.preview import make_preview_tool  # noqa: PLC0415

    visual_tools = [] if benchmark else [make_browse_page_tool(), make_preview_tool(lambda: repo_root)]
    project_tools = [*project_tools, *visual_tools]

    # Reading the operator's OTHER projects, for "use the one in X as a
    # template". Host-side and read-only -- no mount, no write path, no bash
    # -- so the sandbox boundary is exactly where it was; what changes is
    # that a pattern from another project can reach a build task as something
    # other than prose in the plan. Empty when there is nothing else this
    # task may read, so the seat is not offered tools that can only refuse.
    from agent.tools.reference_tools import make_reference_tools  # noqa: PLC0415

    reference_tools = [] if benchmark else make_reference_tools(repo, reference_repos)
    project_tools = [*project_tools, *reference_tools]

    # Designing a mark and exporting a brand kit (agent/tools/logo_tools.py).
    # The coordinator only: it is the seat that writes files, and an export
    # puts two dozen binary assets in the repo.
    from agent.tools.logo_tools import make_logo_tools, new_show_state  # noqa: PLC0415

    show_state = new_show_state()
    logo_tools = [] if benchmark else make_logo_tools(lambda: repo_root, show_state=show_state, repo=repo)
    # Showing the operator an image -- a render, a screenshot, the exported
    # logo -- in the task's log (agent/tools/show_tools.py).
    from agent.tools.show_tools import make_show_images_tool  # noqa: PLC0415
    project_tools = [*project_tools, *logo_tools,
                     *([] if benchmark else [make_show_images_tool(repo, lambda: repo_root, show_state=show_state)])]

    # What past tasks ran into (agent/tools/history_tools.py). Empty when the
    # installation has no history index, so no seat carries a pair of tools
    # that could only report that search is unavailable. The gate is the
    # task's own reference_repos -- the same list the reference tools use,
    # through the same check -- except that this task's OWN project is always
    # in it and is the default.
    from agent.tools.history_tools import guidance as history_guidance  # noqa: PLC0415
    from agent.tools.history_tools import make_history_tools  # noqa: PLC0415

    history_tools = make_history_tools(repo, reference_repos, store, task_id=task_id)
    project_tools = [*project_tools, *history_tools]
    history_note = history_guidance(repo, reference_repos)

    # What the test-writer seat does NOT get, named once. A list rather than
    # a set because a langchain tool is a pydantic model and unhashable;
    # membership here is the same `==` the comprehension below always used.
    # The point of collecting it is that the next seat-specific tool is one
    # name added here, not another clause in a comprehension three
    # subsystems are all editing.
    # history_tools is here for the same reason the other two are: the
    # test-writer writes tests for this repo against this repo's suite, and
    # what some other task escalated over in June changes nothing about that.
    test_writer_excluded = [*reference_tools, *logo_tools, *history_tools]
    # Named in the prompt, not just discoverable in the tool list: a model
    # asked to "do it like the other project does" will otherwise say it has
    # no way to see that project, which is what it used to have to say.
    reference_note = ""
    if reference_tools:
        others = sorted({r for r in (reference_repos or []) if r != repo and r in PROJECTS})
        reference_note = (
            "\n\nANOTHER PROJECT AS A REFERENCE:\n\n"
            f"You can READ the operator's other projects: {', '.join(others)}. "
            "`search_project(repo, pattern)`, `read_project_file(repo, path)`, "
            "`list_project_dir(repo, path)` and `find_files(repo, glob)` take the project "
            "name as their first argument.\n\n"
            "Use them when the task points at one -- \"like the one in X\", \"the same "
            "approach as X\", \"match X's look\". Search for the thing first, then read "
            "only what the hit points at. They are READ-ONLY and they are for the OTHER "
            f"projects: {repo} itself is this task's repo, and you reach it with `read`, "
            "`bash` and `edit` as usual.\n\n"
            "Borrow the approach, not the file. Another project's code was written against "
            "its own conventions, its own dependencies and its own data -- copying it across "
            "wholesale is how you get an import that does not resolve and a pattern nobody "
            f"else in {repo} follows."
        )
    # The investigator gets both. It was given browse_page alone on the
    # grounds that starting the project is not read-only -- but it already has
    # `bash` in the same sandbox, so it could always start a server; what it
    # could not do was SEE one, and it is the seat that gets sent to find out
    # what a page currently does. preview_app is also the tidier way to do it:
    # bash leaves a dev server running, this tears the container down in a
    # finally.
    # history_tools reads a table and writes nothing, and the investigator is
    # the seat that gets sent to find things out -- history is another place
    # to look.
    read_only_tools = [tool_by_name["read"], tool_by_name["bash"], tool_by_name["describe_image"],
                       *github_tools, *visual_tools, *reference_tools, *history_tools]
    if db_tool is not None:
        read_only_tools.append(db_tool)
    run_checks_tool = _make_run_checks_tool(repo_root, repo)

    # Pinned per-role models: one fixed model per role rather than a
    # smart-router pool/classifier, so cost and accuracy are directly
    # attributable per model. The aliases live in the LLM router's own
    # config: to swap a role's model, edit the router config, not this file.
    # Coordinator gets two models -- planner for the first turn of a thread
    # (the one that writes the todo plan), coder for every turn after -- via
    # PlanCodeModelMiddleware below.
    # Frontend route (agent/frontend_route.py): the coordinator that writes
    # the edits AND the investigator that reads for it move to the frontend
    # coder alias; the test-writer stays general by the operator's call
    # (2026-09-09), and the todo planner is shape-neutral either way.
    coder_role = CODER_ROLE.get(route, CODER_ROLE["general"])
    # Every seat carries the task id, so the router's ledger can total a task
    # rather than only price a call -- subagents included, since their spend is
    # the task's spend.
    coordinator_model = llm_for_role(config, coder_role, task_id=task_id, max_tokens=COORDINATOR_MAX_TOKENS,
                                     reasoning=coder_reasoning())
    planner_model = llm_for_role(config, "agent-planner", task_id=task_id, max_tokens=SEAT_MAX_TOKENS)
    investigator_alias = coder_role if route in ("frontend", "fallback") else "agent-investigator"
    investigator_model = llm_for_role(config, investigator_alias, task_id=task_id, max_tokens=SEAT_MAX_TOKENS)
    # On the loop fallback every seat that acts moves: the loop that ended the
    # pass was as often in a subagent as in the coordinator.
    test_writer_alias = coder_role if route == "fallback" else "agent-test-writer"
    test_writer_model = llm_for_role(config, test_writer_alias, task_id=task_id, max_tokens=SEAT_MAX_TOKENS)
    # Its own alias, so the router's ledger bills the verifier as the verifier
    # and not as the test-writer whose model it shared (2026-09-25).
    verifier_alias = coder_role if route == "fallback" else "agent-verifier"
    verifier_model = llm_for_role(config, verifier_alias, task_id=task_id, max_tokens=SEAT_MAX_TOKENS)
    # One low-effort fallback for the seats' empty-reply retry (empty_reply.py),
    # and each seat's own model with its chain of thought switched off, which
    # that retry tries first: a blowout is pure reasoning, and the same model
    # told not to reason answers the prompt that emptied it (2026-09-28).
    empty_reply_model = llm_for_role(config, "agent-coder-fallback", reasoning_effort="low", task_id=task_id)

    def quiet(alias: str, cap: int):
        return llm_for_role(config, alias, task_id=task_id, max_tokens=cap, reasoning=False)

    quiet_models = {
        "coordinator": quiet(coder_role, COORDINATOR_MAX_TOKENS),
        "investigator": quiet(investigator_alias, SEAT_MAX_TOKENS),
        "test-writer": quiet(test_writer_alias, SEAT_MAX_TOKENS),
        "verifier": quiet(verifier_alias, SEAT_MAX_TOKENS),
    }
    quiet_models["general-purpose"] = quiet_models["coordinator"]
    # The report, verbatim, for the seats that check the fix: a one-line
    # task() description was all a verifier had of a multi-paragraph report
    # (2026-09-25).
    report_note = _report_note(goal)

    project_memory = await load_project_memory(repo, store, task_id=task_id)
    project_memory_content = project_memory.content
    # Stripped keys (route_local_path), not the full agent-visible paths --
    # this read must land on the same key the agent's own file tools write
    # to (via the composite's route stripping), or agent-written memory
    # updates are invisible to every future task's prompt.
    org_memory_backend = StoreBackend(namespace=org_namespace, store=store)
    org_memory_content = await read_memory_or_empty(org_memory_backend, route_local_path("/org-memory/", ORG_MEMORY_PATH))
    skills_summary = await load_skills_summary(repo, store)

    # Bound to this seat, and to the planning chat, and to nothing else: they
    # are the two whose prompt carries the memory index, and a tool no prompt
    # mentions is how the investigator came to describe a preview_app it did
    # not have. Empty for a project whose memory was never split.
    from agent.tools.memory_tools import make_memory_tools  # noqa: PLC0415

    memory_tools = make_memory_tools(repo, store, project_memory.entries, task_id=task_id)

    # skills=[SKILLS_ROUTE] on each subagent: per deepagents' own docs, only
    # the general-purpose subagent automatically inherits main-agent skills
    # -- custom subagents require an explicit skills parameter. Without this,
    # investigator (the subagent doing exactly the deep multi-file
    # exploration a domain skill matters most for) never saw the skills
    # manifest at all. Uses the native deepagents SkillsMiddleware here
    # (unlike the coordinator's own manual-injection workaround below) --
    # confirmed to correctly await backend.als()/adownload_files() and load
    # real data against our AsyncPostgresStore-backed CompositeBackend with
    # no error. MemoryMiddleware (the `memory=` param) is still broken the
    # same way it always was, so this fix is scoped to skills specifically,
    # not a signal to also switch the coordinator's own proven-working
    # memory/skills prompt injection over to native params.
    investigator = {
        "name": "investigator",
        "description": (
            "Delegate read-only research/exploration here: mapping out how something works across "
            "multiple files, finding every call site of something, or answering a question that "
            "needs digging before any change can be made. Cannot write or edit files."
        ),
        "system_prompt": INVESTIGATOR_SYSTEM_PROMPT + absent_files + reference_note + history_note,
        "tools": read_only_tools,
        "model": investigator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead. delete likewise: it
            # sees the agent's own file space, never the repo, so a coder
            # cleaning up dead modules gets "not found" four times before it
            # thinks of `rm` (observed 2026-09-08). bash rm is the real one.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            EmptyReplyRetryMiddleware(empty_reply_model, "investigator", quiet_model=quiet_models["investigator"]),
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(contain=True),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            # "end", not "error", on every subagent: the coordinator gets a
            # report instead of the whole pass dying (2026-09-25).
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        # investigator has the full `bash` tool despite its own "read-only"
        # framing (needed for real find/grep-across-the-tree exploration --
        # see INTERRUPT_ON's own comment) -- this is what actually gates a
        # destructive command from it, since nothing else does at the code
        # level.
        "interrupt_on": interrupt_on,
    }

    test_writer = {
        "name": "test-writer",
        "description": (
            "Delegate here to write or harden tests, especially for anything that moves money, "
            "mutates external state, or touches a third-party API. Must produce real behavioral "
            "coverage, never source-inspection-only tests."
        ),
        "system_prompt": TEST_WRITER_SYSTEM_PROMPT + absent_files + report_note,
        # test_writer_excluded, not a clause per subsystem: it writes tests
        # for THIS repo against this repo's suite, and nothing in its prompt
        # tells it another project exists. Tools a seat was never told about
        # are how the investigator ended up with a prompt describing
        # preview_app it did not have.
        "tools": [*[t for t in project_tools if t not in test_writer_excluded], run_checks_tool],
        "model": test_writer_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            EmptyReplyRetryMiddleware(empty_reply_model, "test-writer", quiet_model=quiet_models["test-writer"]),
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(contain=True),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            # "end": one test-writer hit 400 model calls after its tests were
            # already written, and "error" killed the whole pass (2026-09-25).
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="end"),
            # Same cap and countdown as the verifier: one test-writer ran 352
            # identical commands with nothing bounding it (2026-09-25). ONE
            # ToolCallLimitMiddleware per stack (see the verifier's).
            ToolCallLimitMiddleware(run_limit=min(TEST_WRITER_TOOL_CALLS, _rs.as_int("tool_call_run_limit")),
                                    exit_behavior="end"),
            WrapUpMiddleware(limit=min(TEST_WRITER_TOOL_CALLS, _rs.as_int("tool_call_run_limit"))),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    # The verifier: an independent seat that tries to break a bug fix before it
    # ships -- the reproduction and its neighbours, run, not read. It reads
    # and runs but cannot edit source, and runs on its own alias
    # (agent-verifier), a different model from the coder that wrote the fix
    # (2026-09-24: two half-fixes passed a review that only read the diff).
    verifier = {
        "name": "verifier",
        "description": (
            "Delegate here after fixing a bug, BEFORE finishing: give it the bug report verbatim and a "
            "short summary of your change. It independently tries to break the fix -- the reported "
            "case, its neighbours, the module's existing tests -- and reports what still fails. It "
            "changes no source."
        ),
        "system_prompt": VERIFIER_SYSTEM_PROMPT + "\n\n" + _FILESYSTEM_GUIDANCE + report_note,
        "tools": [tool_by_name["read"], tool_by_name["bash"], run_checks_tool],
        "model": verifier_model,
        "middleware": [
            SanitizeToolCallsMiddleware(),
            EmptyReplyRetryMiddleware(empty_reply_model, "verifier", quiet_model=quiet_models["verifier"]),
            # write_file/edit_file reach the agent's memory space, never the
            # repo; a verifier reaching for them to write a probe died on it.
            HiddenToolsMiddleware("glob", "grep", "execute", "delete", "write_file", "edit_file"),
            RepeatCallGuardMiddleware(contain=True),
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="end"),
            # ONE tool-call limit (LangChain refuses two instances of a
            # middleware class on an agent -- a second one here stopped every
            # task at build, 2026-09-25), at the tighter of the two caps, and
            # ending the run rather than raising: a targeted check, not a
            # sweep. One verifier ran 49 calls and 20 minutes on a fix that was
            # already right.
            ToolCallLimitMiddleware(run_limit=min(VERIFIER_TOOL_CALLS, _rs.as_int("tool_call_run_limit")),
                                    exit_behavior="end"),
            # A countdown before that limit, so a verdict comes back instead
            # of "Tool call limit reached" and nothing else (2026-09-25).
            WrapUpMiddleware(limit=min(VERIFIER_TOOL_CALLS, _rs.as_int("tool_call_run_limit"))),
        ],
        "interrupt_on": interrupt_on,
    }

    # Explicit general-purpose subagent: unless a spec with this exact name
    # exists, create_deep_agent auto-adds its own general-purpose subagent
    # carrying the coordinator's full tools and model but not its custom
    # middleware (deepagents' graph.py deliberately inherits only middleware
    # that overrides a default GP slot, without carrying over middleware
    # that's specific to the main agent). That means any work the
    # coordinator delegated to general-purpose would run with no
    # BudgetGuardMiddleware (its LLM spend invisible to the hard dollar
    # ceiling -- one of this system's two non-negotiable code-enforced
    # guards) and no call-limit backstops. Defining it inline with the same
    # name suppresses the auto-add (graph.py checks by name) while the
    # subagent factory still attaches its default slots (filesystem/
    # summarization/skills), same as investigator/test-writer get -- so
    # this keeps the capability and closes the enforcement hole.
    general_purpose = {
        **GENERAL_PURPOSE_SUBAGENT,  # canonical name/description/system_prompt from the lib
        # history_note as well, because this seat is handed search_history
        # and read_history by *project_tools below. Given but not described
        # is the half of the bug the feature's own comment argues against: a
        # model not told a capability exists says it cannot do the thing and
        # works around it. (reference_note is deliberately NOT here -- that
        # tool set refuses this task's own repo, and a delegated sub-task
        # reaching for another project is a scope decision the coordinator
        # makes, not this seat.)
        "system_prompt": (GENERAL_PURPOSE_SUBAGENT["system_prompt"] + "\n\n"
                          + _FILESYSTEM_GUIDANCE + absent_files + history_note),
        "tools": [*project_tools, run_checks_tool],
        "model": coordinator_model,
        "middleware": [
            # Same trap removal as planning_chat (2026-08-27): built-in
            # glob/grep search the memory/skills space, never the repo, and a
            # live build coder looped grep('noFetch'/'skipFetch'/...) against
            # repo paths -- "No matches found" on strings that DO exist,
            # risking a build that concludes the code it must fix is absent.
            # bash rg covers repo search strictly better; read_file/ls still
            # cover skills/memory. execute goes too -- `bash` is the real
            # shell here, and built-in execute has no sandbox behind this
            # backend, so it can only error or mislead.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            EmptyReplyRetryMiddleware(empty_reply_model, "general-purpose", quiet_model=quiet_models["general-purpose"]),
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            RepeatCallGuardMiddleware(contain=True),  # the same call with the same result is not run a third time (2026-09-09)
            BudgetGuardMiddleware(tracker),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        "skills": [SKILLS_ROUTE],
        "interrupt_on": interrupt_on,
    }

    agent = create_deep_agent(
        model=coordinator_model,
        tools=[*project_tools, run_checks_tool, _make_ask_user_tool(), *memory_tools],
        system_prompt=COORDINATOR_SYSTEM_PROMPT_TEMPLATE.format(
            # No repo_root here -- the prompt says "/workspace" literally
            # (see _FILESYSTEM_GUIDANCE): the real host repo_root path is
            # meaningless to the model now that `bash` runs sandboxed --
            # that path doesn't exist inside the container at all.
            repo=repo,
            memory_path=MEMORY_PATH,
            org_memory_path=ORG_MEMORY_PATH,
            project_memory_content=project_memory_content,
            org_memory_content=org_memory_content,
            skills_summary=skills_summary,
        ) + absent_files + reference_note + history_note + ("\n\n" + _LOGO_GUIDANCE if logo_tools else ""),
        middleware=[
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider (2026-09-09)
            HiddenToolsMiddleware("glob", "grep", "execute", "delete"),
            # The same call with the same result is not run a third time
            # (2026-09-09). Not contained: a coordinator that is stuck ends
            # the pass with RepeatLoopError and the work node moves the task
            # to the fallback seat.
            RepeatCallGuardMiddleware(),
            # Planner on the thread's first turn, coder after -- see model_pin.py.
            PlanCodeModelMiddleware(planner_model, coordinator_model),
            # INSIDE the plan/code pick, not outside it: that middleware sets
            # the model on every call, so an outer retry's fallback model was
            # replaced by its choice -- and the retry's note, a HumanMessage,
            # made it a "planning turn", so 17 of 19 retries went to the
            # planner at full effort (2026-09-25, first hour of a run).
            EmptyReplyRetryMiddleware(empty_reply_model, "coordinator", quiet_model=quiet_models["coordinator"]),
            # Inside the retry, as on every subagent: outside it the guard saw
            # only the reply the retry kept, so the discarded empty call --
            # 32k reasoning tokens -- was never charged against the ceiling.
            BudgetGuardMiddleware(tracker),
            SummarizationMiddleware(
                # Meter callback, not middleware: this model is ainvoke()d
                # directly by SummarizationMiddleware, a path no agent
                # middleware wraps -- see BudgetMeterCallback.
                model=llm_for_role(config, "agent-summarizer", callbacks=[BudgetMeterCallback(tracker)],
                                   task_id=task_id),
                trigger=summarization_trigger(),
                keep=summarization_keep(),
                trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
            ),
            # Not included by default for a custom model like ours --
            # TodoListMiddleware is only auto-added for specific built-in
            # harness profiles, not universally, so it's added explicitly
            # here.
            TodoListMiddleware(),
            # ...and a reminder when the model stops maintaining the list it
            # just wrote -- the 0/12-until-done plan strip of 2026-09-11.
            StaleTodoMiddleware(),
            # Checkpoints at a third and two-thirds of the budget and 45/90
            # minutes in: restate the goal and the evidence, or finish.
            StepBackMiddleware(tracker),
            # Defense-in-depth backstop against a runaway loop -- see the
            # run-limit comment below SUMMARIZATION_TRIM_TOKENS for why these
            # are generous limits, not a normal-operation cap, and why the
            # coordinator alone gets "error".
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        subagents=[general_purpose, investigator, test_writer, verifier],
        interrupt_on=interrupt_on,
        # No `memory=[...]` here -- see build_deep_agent's own docstring for
        # why: MemoryMiddleware's automatic loading is broken against
        # AsyncPostgresStore specifically. Content is already embedded
        # directly in system_prompt above via the confirmed-working async
        # read path instead.
        backend=backend,
        # Code-level enforcement, not a prompt instruction: org-memory is
        # populated by application code only (seed_org_memory) -- one
        # task's agent must never be able to alter what every other
        # project's agent believes is settled policy. Skills get the same
        # treatment -- curated reference material (seed_skill), not
        # something to drift via ad hoc mid-task edits the way /memories/
        # is deliberately allowed to.
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/org-memory/*"], mode="deny"),
            FilesystemPermission(operations=["write"], paths=["/skills/*"], mode="deny"),
        ],
        checkpointer=checkpointer,
        store=store,
    )

    return agent, tracker, last_failed_edit_ref
