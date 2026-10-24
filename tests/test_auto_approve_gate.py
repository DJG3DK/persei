"""The auto-approve gate (User.auto_approve_commands -> deep_agent.interrupt_on_for).

Auto mode removes the approval prompt for the sensitive-PATH class -- the
one that fires constantly on ordinary work and makes a long task need
babysitting. It deliberately does NOT remove the gate on destructive
commands, whose whole point is actions a revert can't undo, nor on
ask_user, which is the agent's question channel rather than a safety gate.

These tests pin that split. A regression here doesn't fail loudly at
runtime -- it just silently stops asking about `rm -rf`.
"""

import pytest
from types import SimpleNamespace

from agent.deep_agent import (
    INTERRUPT_ON,
    INTERRUPT_ON_AUTO_APPROVE,
    interrupt_on_for,
)


def _req(command: str):
    return SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}})


DESTRUCTIVE = ["rm -rf /tmp/x", "git push --force origin main", "sudo apt install x",
               "chmod -R 777 .", "chown -R root .", "echo x > /dev/sda", ":(){ :|:& };:"]
SENSITIVE_PATH = ["cat .env", "vim config/app.yaml", "cat .git/config",
                  "edit .github/workflows/ci.yml", "cat ~/.ssh/id_rsa"]


def test_default_gate_is_unchanged_when_auto_mode_is_off():
    assert interrupt_on_for(False) is INTERRUPT_ON


def test_auto_mode_selects_the_relaxed_gate():
    assert interrupt_on_for(True) is INTERRUPT_ON_AUTO_APPROVE


# Auto mode's gate is deletions that lose work, not every destructive marker
# (2026-09-11). bash only ever runs inside the sandbox container, which has no
# credentials, no capabilities and one writable mount, so a push cannot
# authenticate and sudo cannot escalate -- but a delete still loses the work in
# the worktree. These two lists pin that line.
LOSES_WORK = [
    "rm -rf src", "rm src/app.ts", "rm -rf .", "rm -rf /", "rm -rf ~",
    "rm -rf $HOME", "rm -rf /workspace", "rm -rf .git", "rm -rf ../other-project",
    'rm -rf "$BUILD_DIR"', "rm -rf", "git clean -fdx", 'find . -name "*.ts" -delete',
    "find src -name x -delete", "rm -rf /home/storefront",
    # cd is tracked, so the last one decides where a relative target lands
    "cd /tmp && cd /workspace && rm -rf src",
]
LOSES_NOTHING = [
    # the exact call that interrupted a task about every 30 seconds
    "cd /tmp && rm -rf u && mkdir u && cd u && npm pack undici@7.18.2 >/dev/null 2>&1",
    "rm -rf /tmp/scratch", "rm -rf dist && npm run build", "rm -rf node_modules/.vite",
    "rm -rf apps/api/dist", "rm -f coverage/lcov.info", "rm -rf .pytest_cache __pycache__",
    "cd /var/tmp/x && rm -rf *",
    # the container answers these; strict mode still asks
    "sudo apt-get install -y jq", "git push --force origin main", ":(){ :|:& };:",
    "echo x > /dev/sda", "chmod -R 777 .", "npm ci && npm test",
]


def test_auto_mode_gates_a_delete_that_loses_work():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in LOSES_WORK:
        assert when(_req(command)) is True, f"auto mode must still gate: {command}"


def test_auto_mode_runs_scratch_and_sandbox_answered_commands_unattended():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in LOSES_NOTHING:
        assert when(_req(command)) is False, f"auto mode should not prompt for: {command}"


def test_strict_mode_still_asks_about_every_destructive_marker():
    """Narrowing auto mode must not narrow the default gate with it."""
    when = INTERRUPT_ON["bash"]["when"]
    for command in DESTRUCTIVE:
        assert when(_req(command)) is True, f"default gate must still ask about: {command}"


def test_sensitive_paths_are_auto_approved_in_auto_mode():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in SENSITIVE_PATH:
        assert when(_req(command)) is False, f"auto mode should not prompt for: {command}"


def test_the_same_sensitive_paths_ARE_gated_with_auto_mode_off():
    """Guards against the relaxed predicate being wired into both slots --
    which would look fine in the tests above while quietly disabling the
    normal gate for everyone."""
    when = INTERRUPT_ON["bash"]["when"]
    for command in SENSITIVE_PATH:
        assert when(_req(command)) is True, f"default gate must still ask about: {command}"


def test_ask_user_still_interrupts_in_auto_mode():
    assert "ask_user" in INTERRUPT_ON_AUTO_APPROVE
    assert INTERRUPT_ON_AUTO_APPROVE["ask_user"] is INTERRUPT_ON["ask_user"]


def test_auto_mode_never_drops_bash_from_the_gate_entirely():
    """bash must always have SOME gate -- an empty/absent entry would mean
    every command runs unattended, destructive ones included."""
    assert "bash" in INTERRUPT_ON_AUTO_APPROVE
    assert callable(INTERRUPT_ON_AUTO_APPROVE["bash"]["when"])


def test_every_danger_marker_is_lowercase():
    """Both predicates lowercase the command before matching, so an
    uppercase character in a marker makes it dead -- it can never match.
    "chmod -R"/"chown -R" shipped that way and silently gated nothing from
    the day they were added. This asserts the invariant directly rather
    than relying on someone remembering it.
    """
    from agent.deep_agent import _DANGEROUS_COMMAND_MARKERS, _SENSITIVE_PATH_MARKERS

    for marker in _DANGEROUS_COMMAND_MARKERS + _SENSITIVE_PATH_MARKERS:
        assert marker == marker.lower(), f"marker {marker!r} can never match a lowercased command"


def test_recursive_chmod_and_chown_are_gated_in_strict_mode():
    """The specific regression: these two were dead markers (uppercase R in a
    list matched against a lowercased command). Strict mode still asks; auto
    mode does not, because a mode change inside the container cannot outlive
    the container -- only a delete costs work."""
    for command in ["chmod -R 777 .", "chown -R root:root .", "CHMOD -R 777 ."]:
        assert INTERRUPT_ON["bash"]["when"](_req(command)) is True


# audit M-7: whitespace/flag-order evasions the old substring matcher missed.
EVASIONS = [
    "rm  -rf /srv",        # two spaces
    "rm -fr /srv",         # flag order
    "rm\t-rf /srv",        # tab
    "rm -Rf /srv",         # capital R
    "find . -delete",      # no "rm" at all
    "git -c a=b push origin x --force",  # -c prefix before push
    "sudo\tid",            # tab after sudo
]


@pytest.mark.parametrize("cmd", EVASIONS)
def test_destructive_evasions_are_still_gated_in_both_modes(cmd):
    from agent.deep_agent import _bash_is_destructive, _bash_needs_approval
    assert _bash_is_destructive(_req(cmd)), f"auto-approve let {cmd!r} through"
    assert _bash_needs_approval(_req(cmd)), f"strict mode let {cmd!r} through"


@pytest.mark.parametrize("cmd", ["echo hi", "ls -la", "rm just-one-file.txt", "git push origin"])
def test_benign_or_nonforce_commands_are_not_false_flagged_as_destructive(cmd):
    from agent.deep_agent import _bash_is_destructive
    # a plain non-force `git push` is still gated (it's in the marker list) but
    # must not be misread as the always-destructive force-push subset via regex.
    if cmd.startswith("git push"):
        return
    assert not _bash_is_destructive(_req(cmd)), f"{cmd!r} wrongly flagged destructive"


# ---------------------------------------------------------------------------
# /dev/null false positive (2026-08-27)
# ---------------------------------------------------------------------------
#
# The destructive pattern `>\s*/dev/` was written for raw devices
# (> /dev/sda) but \s* matches zero spaces, so it flagged `2>/dev/null` --
# the most common idiom in shell. Every quiet command prompted for approval,
# auto-approve mode included: a live build's plain `ls ... 2>/dev/null` and
# `find ... 2>/dev/null` both gated as "destructive", and the operator asked
# why their auto-approve setting wasn't working.

from agent.deep_agent import _matches_dangerous


@pytest.mark.parametrize("benign", [
    "ls tests/ 2>/dev/null",
    "find . -name '*.test.*' 2> /dev/null | head -30",
    "cat foo > /dev/null",
    "rg -n pattern src 2>/dev/null",
    "echo hi > /dev/stdout",
    "some_tool --log /dev/stderr",
])
def test_null_and_stream_device_redirects_are_not_destructive(benign):
    assert not _matches_dangerous(benign), benign


@pytest.mark.parametrize("destructive", [
    "echo x > /dev/sda",
    "cat img >/dev/sda1",
    "dd if=backup.img of=/dev/sda",
])
def test_raw_device_writes_still_gate(destructive):
    assert _matches_dangerous(destructive), destructive


def test_dd_to_dev_null_is_fine():
    assert not _matches_dangerous("dd if=big.bin of=/dev/null bs=1M")


# ---------------------------------------------------------------------------
# The repo-aware half (2026-09-11, second report): auto mode still stopped for
# the coder's own throwaway probe files, because a command string cannot tell
# repo content from scratch. With a repo root, git answers that.
# ---------------------------------------------------------------------------

@pytest.fixture
def worktree(tmp_path):
    """A git repo with one tracked file under a tracked directory."""
    import subprocess
    root = tmp_path / "wt"
    (root / "apps" / "storefront" / "src").mkdir(parents=True)
    (root / "apps" / "storefront" / "src" / "html.ts").write_text("export const x = 1;\n")
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.email=t@t.t",
                "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)
    return str(root)


def _auto_when(repo_root=None):
    return interrupt_on_for(True, repo_root)["bash"]["when"]


def test_the_agents_own_scratch_file_is_deleted_without_asking(worktree):
    """The exact shape that interrupted a live task: write a probe, run it,
    delete it. git never heard of the probe, so removing it changes nothing
    in the diff the operator reviews."""
    probe = (
        "cd /workspace/apps/storefront && cat > scripts/probe-html.test.mjs <<'EOF'\n"
        "import { htmlToPlainText } from '../src/utils/html.ts';\n"
        "console.log('PROBE', 1);\n"
        "EOF\n"
        "npx vitest run scripts/probe-html.test.mjs 2>&1 | head -20; rm -f scripts/probe-html.test.mjs"
    )
    assert _auto_when(worktree)(_req(probe)) is False
    # ...and without a repo root the same command still asks: unknown means ask.
    assert _auto_when()(_req(probe)) is True


def test_deleting_tracked_content_still_asks(worktree):
    for command in [
        "rm -rf /workspace/apps/storefront/src",
        "cd /workspace && rm -rf apps",
        "cd /workspace/apps/storefront && rm src/html.ts",
        "cd /workspace/apps/storefront && find src -name '*.ts' -delete",
        "rm -rf .",                       # the worktree root
        "rm -rf /workspace",
    ]:
        assert _auto_when(worktree)(_req(command)) is True, command


def test_a_path_git_cannot_speak_for_always_asks(worktree):
    """.git holds no tracked files, so the tracked-content check would wave a
    history delete through. Same for a target that cannot be read at all."""
    for command in ["rm -rf .git", "rm -rf /workspace/.git", 'rm -rf "$BUILD_DIR"',
                    "rm -rf", "git clean -fdx", "rm -rf /etc", "rm -rf /home/other-project"]:
        assert _auto_when(worktree)(_req(command)) is True, command


def test_scratch_and_build_output_never_ask_even_with_a_repo(worktree):
    for command in ["cd /tmp && rm -rf u && mkdir u", "rm -rf /tmp/x", "rm -rf dist",
                    "rm -rf node_modules", "rm -rf coverage .pytest_cache",
                    "cd /workspace/apps/api && rm -f scratch-check.ts"]:
        assert _auto_when(worktree)(_req(command)) is False, command


def test_a_broken_repo_root_fails_closed(tmp_path):
    """git failing (not a repo, gone, slow) must mean ask, not run."""
    assert _auto_when(str(tmp_path / "nope"))(_req("rm -rf /workspace/src")) is True



def test_a_delete_through_a_variable_assigned_in_the_same_command_is_read(tmp_path):
    """2026-09-29: the coder kept its scratch path in `P=` and every
    `rm -f "$P"` asked for approval in auto mode, five times in one task."""
    from agent.deep_agent import _deletions_that_lose_work, _expand_local_assignments
    cmd = 'cd /workspace/frontend && P=/workspace/.scratch/PairSettingsModal.patch && cp a.ts "$P" && rm -f "$P"'
    assert 'rm -f "/workspace/.scratch/PairSettingsModal.patch"' in _expand_local_assignments(cmd)
    losses = _deletions_that_lose_work(cmd)
    assert losses == ["/workspace/.scratch/PairSettingsModal.patch"], losses
    # A tracked path through a variable is still a real deletion.
    assert _deletions_that_lose_work('F=/workspace/src/app.ts; rm "$F"') == ["/workspace/src/app.ts"]
    # A variable the command never assigned, or one holding a substitution, stays unreadable.
    assert any(t.startswith("!") or "$" in t for t in _deletions_that_lose_work('rm -rf "$UNKNOWN"'))
    assert any("$" in t or t.startswith("!") for t in _deletions_that_lose_work('D=$(mktemp -d); rm -rf "$D"'))



def test_an_assignment_after_the_delete_does_not_rewrite_its_target():
    """2026-09-29 audit, A1: `P=src; rm -rf "$P"; P=/tmp/x` was read as a
    delete of /tmp/x -- scratch -- and the delete of src went unasked."""
    from agent.deep_agent import _deletions_that_lose_work
    assert _deletions_that_lose_work('P=src; rm -rf "$P"; P=/tmp/x') == ["/workspace/src"]
    assert _deletions_that_lose_work('P=/tmp/x; rm -rf "$P"; P=src') == []
    assert _deletions_that_lose_work('P=src; P=/tmp/y; rm -rf "$P"') == [], "the assignment in force at the delete"


# 2026-09-29 audit, A2: delete forms the gate did not recognise. None of these
# is parsed; each reads as unreadable, so it asks.
HIDDEN_DELETES = [
    "env rm -rf src", "env P=1 /bin/rm -rf src", "ls src | xargs rm -rf", "find src -print0 | xargs -0 rm",
    "sh -c 'rm -rf src'", 'bash -c "cd src; rm -rf lib"', "find src -name '*.ts' -exec rm {} +",
    "find src -exec sh -c 'rm -rf $1' _ {} \\;", "timeout 5 rm -rf src", "nohup rm -rf src &",
    "git reset --hard", "git reset --hard HEAD~1", "git clean --force -d",
    # the cd ran in a subshell that has exited, so this deletes /workspace/apps
    "(cd /tmp); rm -rf apps", "(cd /tmp && ls); rm -rf apps", "(cd /tmp; (cd /var/tmp)); rm -rf apps",
]


def test_a_delete_run_through_another_command_asks():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in HIDDEN_DELETES:
        assert when(_req(command)) is True, f"auto mode must gate: {command}"


def test_a_delete_run_through_another_command_asks_whatever_git_says(worktree):
    """Unreadable means ask even when the repo could be consulted."""
    for command in HIDDEN_DELETES:
        assert _auto_when(worktree)(_req(command)) is True, command


def test_a_cd_inside_a_subshell_moves_nothing_outside_it():
    from agent.deep_agent import _deletions_that_lose_work
    assert _deletions_that_lose_work("(cd /tmp); rm -rf x") == ["/workspace/x"]
    # Inside the subshell the cd still counts.
    assert _deletions_that_lose_work("(cd /tmp && rm -rf x)") == []
    assert _deletions_that_lose_work("(cd /tmp && (cd /var/tmp) && rm -rf x)") == []
    # An unbalanced `)` cannot pop past the top level.
    assert _deletions_that_lose_work("cd /tmp ); rm -rf x") == []


def test_wrappers_that_run_no_delete_do_not_ask():
    when = INTERRUPT_ON_AUTO_APPROVE["bash"]["when"]
    for command in ["env | grep PATH", "xargs -n1 echo < list", "sh -c 'npm test'", "timeout 60 npm test",
                    "find src -name '*.ts' | xargs grep -l TODO", "find src -name '*.ts' -exec cat {} +",
                    "git reset --soft HEAD~1", "git reset HEAD -- src/x.ts"]:
        assert when(_req(command)) is False, f"auto mode should not prompt for: {command}"


def test_a_move_onto_a_tracked_file_asks_and_a_rename_to_a_new_name_does_not(worktree):
    """`mv` loses the file it lands on. A move into a tracked directory
    overwrites nothing unless the same name is already tracked there."""
    for command in ["mv x.ts apps/storefront/src/html.ts",
                    "cd /workspace/apps/storefront && mv new.ts src/html.ts",
                    "cd /workspace/apps/storefront/src && mv ../html.ts .",   # lands on src/html.ts
                    "mv -f x.ts apps/storefront/src/html.ts", "mv -t apps/storefront/src x.ts",
                    "mv x.ts /etc/x.ts", 'mv x.ts "$DEST"']:
        assert _auto_when(worktree)(_req(command)) is True, command
    for command in ["mv apps/storefront/src/html.ts apps/storefront/src/html2.ts",
                    "mv x.ts apps/storefront/src/", "mv x.ts /tmp/x.ts", "mv dist build"]:
        assert _auto_when(worktree)(_req(command)) is False, command
    # Without a repo to ask, a move inside the worktree asks: unknown means ask.
    assert _auto_when()(_req("mv x.ts apps/storefront/src/")) is True


# --- heredocs that are only written out (2026-10-21, golden suite) ----------

from agent.deep_agent import _deletions_that_lose_work as _losses  # noqa: E402

_WRITTEN_SCRIPT = ("cd /workspace && mkdir -p .scratch/mut && cat > .scratch/mut/run.sh <<'SH'\n"
                   "for m in \"$@\"; do\n  d=\".scratch/mut/$m\"\n  rm -rf \"$d\"; mkdir -p \"$d\"\ndone\nSH\n"
                   "echo written")


def test_a_delete_in_a_script_being_written_is_text_not_a_delete():
    """Two of the first five golden tasks parked on this: the coder wrote a
    throwaway script with `cat > ... <<'SH'`, and the `rm -rf "$d"` in its
    text read as a delete about to run."""
    assert _losses(_WRITTEN_SCRIPT) == []
    assert _losses(_WRITTEN_SCRIPT.replace("cat > ", "tee ")) == []
    assert _losses("cat > x.sh <<-EOF\n\trm -rf \"$x\"\n\tEOF\necho ok") == []


def test_a_heredoc_a_shell_runs_is_still_read():
    assert _losses("bash <<'EOF'\nrm -rf src\nEOF") == ["/workspace/src"]
    assert _losses("ssh box <<EOF\nrm -rf src\nEOF") == ["/workspace/src"]


def test_a_delete_outside_the_written_text_is_still_read():
    assert _losses("cat > x.sh <<EOF\nrm -rf a\nEOF\nrm -rf src") == ["/workspace/src"]
    assert _losses("rm -rf src && cat > x.sh <<'EOF'\necho hi\nEOF") == ["/workspace/src"]


# 2026-10-22 audit, A19: each of these runs `rm -rf src` in a real shell, and
# the first heredoc fix read all of them as a script being written.
HEREDOCS_THAT_RUN = [
    "cat <<'X' | bash\nrm -rf src\nX",
    "tee x <<'X' | sh\nrm -rf src\nX",
    "tee /dev/null <<'X' |& sh\nrm -rf src\nX",
    "cat > x.sh <<'X' | bash\nrm -rf src\nX",
    "cat <<'X' > >(bash)\nrm -rf src\nX",
    "bash <<A; cat > x.sh <<B\nrm -rf src\nA\necho hi\nB",   # A's body is the first one
    "cat # <<X\nrm -rf src\nX",                              # an operator in a comment is none
    "cat '<<X'\nrm -rf src\nX",                              # nor is one in quotes
    'echo "a <<X"; cat\nrm -rf src\nX',
    "cat <<X\nrm -rf src\nX",                                # to the terminal: not a write, so read
    "cat << $D > x\nrm -rf src\n$D",                        # a delimiter not read here
]


@pytest.mark.parametrize("command", HEREDOCS_THAT_RUN)
def test_a_heredoc_piped_onward_or_not_written_is_still_read(command):
    assert "/workspace/src" in _losses(command), command
    assert INTERRUPT_ON_AUTO_APPROVE["bash"]["when"](_req(command)) is True


def test_heredocs_written_out_are_still_text():
    assert _losses("cat >> x.sh <<'X'\nrm -rf src\nX") == []
    assert _losses("cat <<'X' >x.sh && echo ok\nrm -rf src\nX") == []
    assert _losses("cat >| x.sh <<'X' || exit 1\nrm -rf src\nX") == []
    assert _losses("echo hi | cat > x.sh <<'X'\nrm -rf src\nX") == []
    assert _losses("tee -a x.sh >/dev/null <<'X'\nrm -rf src\nX") == []
    # Two bodies on one line, consumed in order: the shell's is read.
    assert _losses("cat > a <<A; bash <<B\nrm -rf a\nA\nrm -rf src\nB") == ["/workspace/src"]
    assert _losses("cat > a <<A; cat > b <<B\nrm -rf a\nA\nrm -rf b\nB\nrm -rf src") == ["/workspace/src"]
    # A heredoc inside a body a shell runs is judged as part of that script.
    assert _losses("bash <<'O'\ncat > x <<'I'\nrm -rf a\nI\nrm -rf src\nO") == ["/workspace/src"]
    # A here-string is not a heredoc: nothing after it is swallowed.
    assert _losses("cat > x <<< 'hi'\nrm -rf src") == ["/workspace/src"]


# 2026-10-22 audit, A24: each of these loses uncommitted work, and neither
# mode asked. (`git checkout -- src/app.ts` was here too; since 2026-10-24
# one named file put back to HEAD is the agent's own edit undone and passes
# -- deep_agent._REVERT -- while every wholesale form below still asks.)
DISCARDS_WORK = [
    "git checkout -- .", "git checkout .", "git checkout -f HEAD",
    "git -C /workspace checkout --force main", "git restore .", "git restore --staged --worktree src",
    "git switch -f main", "git switch --discard-changes main", "git stash -u && git stash drop",
    "git stash clear", 'python3 -c "import shutil; shutil.rmtree(\'src\')"',
    "node -e \"require('fs').rmSync('src', {recursive: true})\"", "perl -e 'unlink glob \"src/*\"'",
    "ruby -e 'FileUtils.rm_rf(\"src\")'", "python3 - <<'EOF'\nimport os\nos.remove('src/app.ts')\nEOF",
    "/usr/bin/python3.12 -c 'import subprocess; subprocess.run([\"rm\", \"-rf\", \"src\"])'",
    "rsync -a --delete empty/ src/", "rsync -a --delete-after a/ src/", "truncate -s 0 apps/storefront/src/html.ts",
    "truncate --size=0 apps/storefront/src/html.ts",
]


@pytest.mark.parametrize("command", DISCARDS_WORK)
def test_a_form_that_discards_work_asks(command, worktree):
    assert INTERRUPT_ON_AUTO_APPROVE["bash"]["when"](_req(command)) is True, command
    assert _auto_when(worktree)(_req(command)) is True, command


@pytest.mark.parametrize("command", [
    "git checkout main", "git checkout -b feature", "git switch main", "git restore --staged src/app.ts",
    "git stash", "git stash -u", "git stash pop", "git stash list",
    'python3 -c "import json; print(json.load(open(\'package.json\'))[\'name\'])"',
    "node -e \"console.log(require('./package.json').version)\"", "python3 scripts/check.py",
    "rsync -a src/ /tmp/copy/", "truncate -s 0 /tmp/log.txt",
])
def test_the_same_tools_used_harmlessly_do_not_ask(command):
    assert INTERRUPT_ON_AUTO_APPROVE["bash"]["when"](_req(command)) is False, command
