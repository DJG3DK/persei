"""bash is for running things; reading and editing have their own tools.

Live, 2026-09-12: the test-writer subagent made 229 bash calls against ONE
edit and ONE write. It patched JavaScript with `python3 - <<'PY' ...
open(p,'w').write(s.replace(...))` and read files with `cat` and `sed -n`,
while holding `read`, `write` and `edit` the whole time.

That is a wall-clock problem, not a style one: every bash call starts a
container, and the median gap between that subagent's model calls was 11
seconds. The in-process tools cost none of it.

A note on the result, never a refusal -- writing a scratch script to RUN is a
fair use of a shell, and no pattern can tell every case apart.
"""

import pytest

from agent.tools import bash_advice
from agent.tools.bash_advice import EDIT_NOTE, READ_NOTE, advice_for, kind


def test_the_heredoc_patch_that_started_this():
    cmd = ("cd /workspace && python3 - <<'PY'\n"
           "p='/workspace/tests/test_approach_gate_parity.js'\n"
           "s=open(p).read()\n"
           "open(p,'w').write(s.replace('a','b'))\n"
           "PY")
    assert advice_for(cmd) == EDIT_NOTE
    assert kind(cmd) == "write"


def test_every_common_way_to_write_a_file_through_a_shell():
    for cmd in (
        "cd /workspace && cat > tests/new_test.js <<'EOF'\nconsole.log(1)\nEOF",
        "cat >> src/app.js <<'EOF'\nmore\nEOF",
        "cd /workspace && sed -i 's/foo/bar/' src/core/bot.js",
        "sed -i.bak -e 's#a#b#' file.js",
        "perl -pi -e 's/x/y/' src/thing.js",
        "echo hi | tee src/notes.txt",
        "tee -a src/notes.txt < /tmp/x",
        "node -e \"require('fs').writeFileSync('src/a.js', 'x')\"",
        "python3 -c \"from pathlib import Path; Path('a.js').write_text('x')\"",
    ):
        assert advice_for(cmd) == EDIT_NOTE, cmd


def test_reading_one_file_is_pointed_at_the_read_tool():
    for cmd in (
        "cd /workspace && cat package.json",
        "cat src/core/backtester.js",
        "cd /workspace && sed -n '1,80p' src/strategies/trendSignal.js",
        "head src/app.js",
        "cd /workspace && tail tests/test_x.js",
    ):
        assert advice_for(cmd) == READ_NOTE, cmd
    assert kind("cat package.json") == "read"


def test_running_things_is_left_alone():
    """The whole point of the tool. A false nudge here trains the model to
    ignore the real ones."""
    for cmd in (
        "cd /workspace && npm test",
        "cd /workspace && node tests/test_trigger_repaint.js 2>&1 | tail -40",
        "cd /workspace && rg -n 'someSymbol' src frontend/src",
        "cd /workspace && git status --short",
        "cd /workspace && bash scripts/mutateTriggerRepaint.sh",
        "cd /workspace && ls -la src",
        "cat package.json | jq .scripts",          # a read piped into real work
        "cd /workspace && grep -c foo src/*.js",
        "python3 -c \"print(1+1)\"",
        "cd /workspace && node -e \"console.log(require('./package.json').name)\"",
    ):
        assert advice_for(cmd) is None, cmd


def test_a_write_wins_over_a_read_when_a_command_does_both():
    cmd = "cd /workspace && cat src/a.js && sed -i 's/x/y/' src/a.js"
    assert advice_for(cmd) == EDIT_NOTE


def test_empty_and_nonsense_commands_do_not_raise():
    for cmd in ("", "   ", "\n", "&&", "|||"):
        assert advice_for(cmd) is None
        assert kind(cmd) is None


def test_the_notes_say_why_not_just_what():
    """A rule with no reason gets argued with. Both notes name the container
    cost, which is the thing the model cannot see from inside, and both say
    what bash is still FOR -- an instruction that only takes things away reads
    as "avoid bash", which is wrong: bash is the only tool that can search this
    repo at all."""
    for note in (EDIT_NOTE, READ_NOTE):
        assert "container" in note
        assert "running things" in note.lower()


def test_the_read_note_says_how_to_read_several_files():
    """The gap that made the old note easy to dismiss: it answered "not like
    that" without answering "then how do I read six files". Parallel `read`
    calls in one turn already work; nothing told the model so."""
    assert "SAME TURN" in READ_NOTE
    assert "offset/limit" in READ_NOTE, "and how to read PART of a file"


# ---------------------------------------------------------------------------
# The agent's own filesystem, reached through a shell that cannot see it
#
# Live on 2026-09-12, task 279c29fd: the coder reached for /memories/AGENTS.md
# through bash twice while writing its memory at the end of a task. The path
# is not mounted in the sandbox -- verified against the image -- so both calls
# could only spend a container and come back with "No such file".
# ---------------------------------------------------------------------------


def test_reading_own_memory_through_the_shell_is_flagged():
    """The exact command from the run, pipes and all."""
    note = bash_advice.advice_for('grep -n "lgtm\\|http-to-file-access" /memories/AGENTS.md | head')
    assert note == bash_advice.MEMORY_READ_NOTE
    assert "read_file" in note


def test_the_second_form_from_the_same_run_is_flagged_too():
    note = bash_advice.advice_for('grep -n "only ways to clear it" /memories/AGENTS.md | cat -A | head -3')
    assert note == bash_advice.MEMORY_READ_NOTE


def test_skills_and_org_memory_are_the_same_filesystem():
    for path in ("/skills/webapp-testing/SKILL.md", "/org-memory/NOTES.md"):
        assert bash_advice.advice_for(f"cat {path}") == bash_advice.MEMORY_READ_NOTE


def test_writing_own_memory_is_never_sent_to_the_repo_edit_tool():
    """The whole reason the virtual-path check runs first.

    `edit` is path-guarded to the repo root and rejects /memories outright, so
    the generic write note would have sent the model from one wrong tool to
    another. It has to name write_file/edit_file instead.
    """
    note = bash_advice.advice_for("cat >> /memories/AGENTS.md <<'EOF'\nnotes\nEOF")
    assert note == bash_advice.MEMORY_WRITE_NOTE
    assert note is not bash_advice.EDIT_NOTE
    assert "write_file" in note
    # and it must say the quiet part: exit 0 did not mean it was saved
    assert "thrown away" in note


def test_a_repo_path_that_merely_contains_the_word_is_left_alone():
    """Path boundaries, not substrings: a repo file named for one of these
    concepts is an ordinary repo file."""
    assert bash_advice.advice_for("rg -n 'todo' src/memories.ts") is None
    assert bash_advice.advice_for("npm run build --prefix apps/skills") is None
    assert bash_advice.advice_for("cat docs/org-memory.md") is bash_advice.READ_NOTE


def test_running_things_is_still_never_flagged():
    """The commands from the same run that were a fair use of a shell."""
    for command in (
        'cd /workspace && timeout 20 curl -sI https://github.com 2>&1 | head -3',
        'cd /tmp && curl -sL -o b.tar.gz https://example.invalid/b.tar.gz && tar -xzf b.tar.gz',
        'npx vitest run src/components/panels.test.tsx',
        'rg -n "someSymbol" src',
    ):
        assert bash_advice.advice_for(command) is None, command


# ---------------------------------------------------------------------------
# Kinds, and reading one back off a result
# ---------------------------------------------------------------------------


def test_kind_names_each_mistake():
    assert bash_advice.kind("sed -i 's/a/b/' src/app.ts") == "write"
    assert bash_advice.kind("cat src/app.ts") == "read"
    assert bash_advice.kind("grep -n x /memories/AGENTS.md") == "memory-read"
    assert bash_advice.kind("tee /skills/x.md") == "memory-write"
    assert bash_advice.kind("pytest -q") is None


def test_the_kind_is_recoverable_from_the_result_text():
    """How the work node tags its tool event without the wrapper writing a
    second one -- the note is already on the front of the result."""
    for command in ("cat src/app.ts", "sed -i 's/a/b/' x.py", "grep x /memories/AGENTS.md"):
        note = bash_advice.advice_for(command)
        result = f"{note}\nexit_code=0\nsome output"
        assert bash_advice.kind_of_result(result) == bash_advice.kind(command)


def test_an_unflagged_result_has_no_kind():
    assert bash_advice.kind_of_result("exit_code=0\nall tests passed") is None
    assert bash_advice.kind_of_result("") is None


# ---------------------------------------------------------------------------
# Scratch space outside the checkout
#
# Found by replaying the 2026-09-12 run's real commands through this module:
# `cd /tmp && sed -n '40,110p' AlertSuppression.qll` was being nudged toward
# `read`, which is path-guarded to the repo root and cannot open /tmp at all.
# The same mistake as pointing a /memories write at `edit`, in reverse: a note
# is only worth sending when the tool it names can actually do the job.
# ---------------------------------------------------------------------------


def test_paging_a_downloaded_file_in_tmp_is_not_nudged():
    assert advice_for("cd /tmp && sed -n '40,110p' AlertSuppression.qll") is None
    assert advice_for("cat /etc/hostname") is None
    assert advice_for("cd /var/tmp && head -20 bundle.log") is None


def test_a_scratch_write_outside_the_repo_is_not_nudged():
    """Writing a scratch file to run is the fair use a shell is for."""
    assert advice_for("cat > /tmp/probe.sh <<'SH'\necho hi\nSH") is None
    assert advice_for("cd /tmp && tee out.txt") is None


def test_the_repo_is_still_nudged_from_either_spelling():
    """/workspace IS the repo root, so both spellings of the same file are
    the in-process tools' business."""
    assert advice_for("cd /workspace && cat src/app.ts") is READ_NOTE
    assert advice_for("cat /workspace/src/app.ts") is READ_NOTE
    assert advice_for("sed -i 's/a/b/' /workspace/src/app.ts") is EDIT_NOTE


def test_a_memory_write_is_flagged_even_though_it_is_not_in_the_repo():
    """The scratch-path exemption must not swallow the case it was added
    alongside: /memories is outside the repo AND unreachable from bash."""
    from agent.tools.bash_advice import MEMORY_WRITE_NOTE
    assert advice_for("tee /skills/x.md") is MEMORY_WRITE_NOTE
    assert advice_for("cat > /memories/AGENTS.md") is MEMORY_WRITE_NOTE


# ---------------------------------------------------------------------------
# Compound reads
#
# Measured on task 3ee0d030 (2026-09-14): an investigator made 219 bash calls
# and the harness flagged 17 of them -- 8%. Its `read` use fell from 46 calls
# in the first half of the run to 14 in the second while bash rose from 91 to
# 132. Every compound spelling walked past the old single-file pattern, so the
# model was being told that one spelling was wrong and twelve were fine.
#
# The line these tests hold: reading is ALL the command does -> nudge. One
# stage that filters, searches or counts -> leave it alone, because the
# built-in glob/grep cannot see the repo (HiddenToolsMiddleware) and bash is
# then the ONLY way to search it. A nudge on a search is worse than no nudge:
# it is the false positive that teaches the model to ignore the true ones.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "cat src/a.js src/b.js",
    "sed -n '1,80p' src/bot.js && sed -n '1,80p' src/gate.js",
    "head -50 src/a.js; head -50 src/b.js",
    "cat src/bot.js | head -60",
    "awk 'NR>=40 && NR<=90' src/bot.js",
    "cd /workspace && cat src/a.js && cat src/b.js",
    "tail -n 40 src/a.js",
])
def test_a_command_that_only_reads_is_flagged_however_it_is_spelled(command):
    assert advice_for(command) is READ_NOTE, command


@pytest.mark.parametrize("command", [
    "rg -n 'trendSignal' src",
    "grep -rn 'strategy' src | head -30",
    "cat src/bot.js | grep divergence",
    "find . -name '*.test.js' | head",
    "git log --oneline -20 -- src/bot.js",
    "wc -l src/a.js",
    "awk '/divergence/' src/bot.js",
    "cat src/a.js | wc -l",
    "sort src/list.txt | uniq",
    "npm test",
])
def test_searching_counting_and_running_are_never_flagged(command):
    """bash is the only tool that can search this repo. Flagging that would be
    both wrong and self-defeating."""
    assert advice_for(command) is None, command


def test_separators_inside_quotes_do_not_split_the_command():
    """`awk 'NR>=40 && NR<=90' f` contains && INSIDE the program. Splitting
    naively cut it in half and the read went unflagged."""
    assert advice_for("awk 'NR>=40 && NR<=90' src/bot.js") is READ_NOTE
    assert advice_for("grep 'a && b' src/x.js") is None


def test_a_read_of_scratch_space_is_still_left_alone():
    """The compound forms must not undo the /tmp exemption: `read` is
    path-guarded to the repo and cannot open those at all."""
    assert advice_for("cd /tmp && cat a.log && cat b.log") is None
    assert advice_for("cat /etc/hosts /etc/hostname") is None


def test_a_mixed_command_that_reads_and_searches_is_a_search():
    """One search stage anywhere means bash was the right call."""
    assert advice_for("cat src/a.js && rg -n x src") is None
    assert advice_for("rg -n x src && cat src/a.js") is None


# ---------------------------------------------------------------------------
# 2026-09-25, from the 50-task benchmark trajectories
# ---------------------------------------------------------------------------


def test_the_splitter_can_hand_back_the_separators():
    """The benchmark guard drops one segment of a compound command and joins
    the rest back into something the shell will still run, so it needs to
    know which separator followed each segment."""
    from agent.tools.bash_advice import split_top_level
    assert split_top_level("a && b ; c\nd || e", ("&&", "||", ";", "\n"), keep_seps=True) == [
        ("a ", "&&"), (" b ", ";"), (" c", "\n"), ("d ", "||"), (" e", ""),
    ]
    assert split_top_level("awk 'NR>=40 && NR<=90' f && g", ("&&",), keep_seps=True) == [
        ("awk 'NR>=40 && NR<=90' f ", "&&"), (" g", ""),
    ]
    assert split_top_level("a && b", ("&&",)) == ["a ", " b"], "the plain form is unchanged"


@pytest.mark.parametrize("command", [
    "git merge-base --is-ancestor abc HEAD",
    "git stash list",
    "git stash show -p stash@{0}",
    "git cat-file -t HEAD",
    "git show HEAD:src/a.py | head -20",
    "git status --short && git stash list",
    "git rm-check",
])
def test_read_only_git_is_not_told_it_cannot_work(command):
    """`\\bmerge\\b` matched `merge-base`, and `stash` matched `stash list`: a
    read-only question was answered with "the repository is read-only"."""
    assert advice_for(command) is None, command


@pytest.mark.parametrize("command", [
    "git merge feature", "git stash", "git stash pop", "git rm a.py", "git add .", "git -C /workspace reset --hard",
])
def test_git_writes_are_still_flagged(command):
    assert advice_for(command) is bash_advice.GIT_WRITE_NOTE, command


def test_a_benchmark_refusal_has_a_kind_the_work_node_can_read():
    """The guard's note names the refused command, so it is a prefix in the
    kinds table rather than a whole note."""
    from agent.tools.benchmark_guard import refusal, screen
    assert bash_advice.kind_of_result(refusal("git fsck") + "\n") == "benchmark-refused"
    _, note = screen("git log --all | head ; git status")
    assert bash_advice.kind_of_result(note + "\nexit_code=0\n") == "benchmark-refused"
    assert bash_advice.kind_of_result("[Persei harness] ERROR: REFUSED without retrying") is None, "the edit guard's"


def test_the_read_and_edit_notes_stop_after_two_per_workspace(tmp_path, monkeypatch):
    """155 read notes and 48 edit notes over 50 tasks changed nothing; at ~600
    chars each they were only context cost. Two says it. The memory and
    git-write notes explain a failed command and are not capped."""
    import asyncio

    from agent.tools import agent_tools

    async def _fake_sandbox(cmd, cwd, timeout=None, extra_env=None, network=None):
        return {"ok": True, "exit_code": 0, "output": "x\n"}

    monkeypatch.setattr(agent_tools, "run_shell_sandboxed", _fake_sandbox)
    monkeypatch.setattr(agent_tools, "_NOTES_SENT", {})
    bash = {t.name: t for t in agent_tools.make_agent_tools(str(tmp_path))[0]}["bash"]
    run = lambda cmd: asyncio.run(bash.ainvoke({"command": cmd}))  # noqa: E731

    assert run("cat a.py").startswith(READ_NOTE)
    assert run("cat b.py").startswith(READ_NOTE)
    assert run("cat c.py").startswith("exit_code=0"), "the third read is not nagged"
    assert run("sed -i 's/a/b/' a.py").startswith(EDIT_NOTE), "each note has its own budget"
    assert run("sed -i 's/a/b/' a.py").startswith(EDIT_NOTE)
    assert run("sed -i 's/a/b/' a.py").startswith("exit_code=0")
    for _ in range(3):
        assert run("git stash").startswith(bash_advice.GIT_WRITE_NOTE)
        assert run("cat /memories/AGENTS.md").startswith(bash_advice.MEMORY_READ_NOTE)

    # Another workspace starts fresh; the same one is still spent, even from
    # a new tool instance (the deep agent is rebuilt every pass).
    other = {t.name: t for t in agent_tools.make_agent_tools(str(tmp_path / "other"))[0]}["bash"]
    assert asyncio.run(other.ainvoke({"command": "cat a.py"})).startswith(READ_NOTE)
    again = {t.name: t for t in agent_tools.make_agent_tools(str(tmp_path))[0]}["bash"]
    assert asyncio.run(again.ainvoke({"command": "cat a.py"})).startswith("exit_code=0")


def test_the_note_budget_table_does_not_grow_without_bound(monkeypatch):
    from agent.tools import agent_tools
    monkeypatch.setattr(agent_tools, "_NOTES_SENT", {})
    for i in range(agent_tools._NOTES_SENT_MAX_ROOTS + 10):
        agent_tools._within_note_budget(f"/w/{i}", READ_NOTE)
    assert len(agent_tools._NOTES_SENT) == agent_tools._NOTES_SENT_MAX_ROOTS
    assert "/w/0" not in agent_tools._NOTES_SENT and f"/w/{agent_tools._NOTES_SENT_MAX_ROOTS + 9}" in agent_tools._NOTES_SENT
