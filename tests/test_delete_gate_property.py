"""Auto mode's delete gate, checked against a real shell.

Three audits in a row (2026-09-29 A1/A2, 2026-10-22 A19/A24) each found a
command shape that deleted tracked work while `_bash_deletes_real_work` said
nothing. Each fix pinned the shapes the audit listed, and the next audit found
the next shape. This suite stops guessing at shapes: hypothesis composes shell
commands from a grammar (rm, mv, cp, the git verbs that discard work, truncate,
`>`, find -delete, xargs, sh -c, eval, subshells, heredocs, variables, globs,
quoting, cd), and each one RUNS in a fresh scratch git repo. The property:

    tracked work was lost  =>  the gate asked.

"Lost" is judged from the files, not the command: a tracked file that is gone
or no longer a file, a non-empty tracked file left empty, an uncommitted edit
reverted to HEAD without a stash that keeps it, or the repo's .git gone. An
edit that replaces content (sed, `cat > f <<EOF` with a body) is the coder's
ordinary work and is not a loss.

The gate is read with a repo root, which is how auto mode wires it
(interrupt_on_for(True, repo_root)). Strict mode's marker list is weaker for
ordinary deletes on purpose (2026-10-22 audit, A25) and is not this property.

Safety: commands use relative paths only -- the generator never writes an
absolute path or a `..` (asserted per example) -- run with the scratch repo as
cwd, HOME inside the tmp dir, git's ceiling at the tmp dir, no stdin and a
timeout that kills the process group.

Runs: CI uses the derandomized "ci" profile (300 examples, the same ones every
run). DELETE_GATE_EXAMPLES=20000 runs a long, randomized local search.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from agent.deep_agent import interrupt_on_for  # noqa: E402

settings.register_profile(
    "ci", max_examples=300, derandomize=True, deadline=None, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    print_blob=True)
settings.register_profile(
    "long", max_examples=int(os.environ.get("DELETE_GATE_EXAMPLES") or 300), derandomize=False,
    deadline=None, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    print_blob=True)
_PROFILE = "long" if os.environ.get("DELETE_GATE_EXAMPLES") else "ci"

# The scratch repo. Two commits, so HEAD~1 exists and differs; uncommitted
# edits on two files; untracked scratch beside them; and a tracked file under a
# name the gate treats as build output (`dist/`), since real repos commit those.
_FIRST = {
    "README.md": "readme v1\n",
    "src/app.py": "print('app v1')\n",
    "src/util.py": "def util():\n    return 1\n",
    "src/lib/core.py": "CORE = 1\n",
    "docs/guide.md": "guide\n",
    "dist/bundle.js": "bundle()\n",
}
_SECOND = {"README.md": "readme v2\n", "src/app.py": "print('app v2')\n"}
_EDITS = {"src/app.py": "print('app v2')\n# an uncommitted edit\n",
          "src/lib/core.py": "CORE = 2  # an uncommitted edit\n"}
_UNTRACKED = {"notes.txt": "scratch notes\n", "src/scratch.log": "log\n", "tmp/x.txt": "x\n",
              "empty.txt": ""}
_HEAD = {**_FIRST, **_SECOND}

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
}


def _write(root: str, files: dict) -> None:
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    base = tmp_path_factory.mktemp("delete-gate")
    home = base / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
           "GIT_CEILING_DIRECTORIES": str(base), **_GIT_ENV}
    repo = str(base / "template")
    os.makedirs(repo)

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    _write(repo, _FIRST)
    git("add", "-A")
    git("commit", "-qm", "first")
    _write(repo, _SECOND)
    git("commit", "-qam", "second")
    _write(repo, _EDITS)
    _write(repo, _UNTRACKED)
    return SimpleNamespace(base=str(base), repo=repo, env=env)


# --- the grammar ---------------------------------------------------------------

_TRACKED_FILES = list(_HEAD)
_PATHS = [
    *_TRACKED_FILES, "src", "src/lib", "docs", "dist", ".", "./src", "src/",
    "notes.txt", "src/scratch.log", "tmp", "tmp/x.txt", "empty.txt", "new.txt", "other",
    "lib", "app.py", "core.py",                       # meaningful after `cd src`
    "*", "src/*", "*.md", "src/*.py", "s*", "src/l*", "*/*.py", "d*",
]
_DIRS = ["src", "src/lib", "docs", "dist", "tmp", "lib"]


@st.composite
def _path(draw, assigns: list) -> str:
    """A path token: bare, quoted, or through a variable the command assigns."""
    p = draw(st.sampled_from(_PATHS))
    glob = any(ch in p for ch in "*?[")
    form = draw(st.sampled_from(["bare", "bare", "single", "double", "var", "var_braced"]))
    if form == "bare":
        return p
    if form == "single":
        return shlex.quote(p) if not glob else p
    if form == "double":
        return f'"{p}"' if not glob else p
    name = draw(st.sampled_from(["P", "T", "F"]))
    how = draw(st.sampled_from(["plain", "export", "for", "subst", "split"]))
    if how == "split" and "/" in p and not glob:
        head, tail = p.split("/", 1)
        assigns.append(f"{name}={shlex.quote(head)}")
        return f'"${{{name}}}/{tail}"'
    if how == "for":
        assigns.append(("for", name, p))
    elif how == "export":
        assigns.append(f"export {name}={p}")
    elif how == "subst":
        assigns.append(f"{name}=$(echo {p})")
    else:
        assigns.append(f"{name}={p}")
    ref = f"${{{name}}}" if form == "var_braced" else f"${name}"
    return ref if glob else f'"{ref}"'


def _wrap_assigns(assigns: list, body: str) -> str:
    out = body
    for a in reversed(assigns):
        if isinstance(a, tuple):
            _, name, value = a
            out = f"for {name} in {value}; do {out}; done"
        else:
            out = f"{a}; {out}"
    return out


@st.composite
def _simple(draw) -> str:
    assigns: list = []

    def p():
        return draw(_path(assigns))

    kind = draw(st.sampled_from([
        "rm", "rm", "unlink", "rmdir", "mv", "cp", "truncate", "redirect", "redirect",
        "find", "xargs", "git_checkout", "git_reset", "git_clean", "git_restore", "git_stash",
        "git_rm", "git_mv", "git_other", "harmless",
    ]))
    if kind == "rm":
        flags = draw(st.sampled_from(["", "-f", "-r", "-rf", "-fr", "-R", "-r -f", "--recursive --force", "-v"]))
        targets = " ".join(p() for _ in range(draw(st.integers(1, 2))))
        cmd = f"rm {flags} {targets}".replace("  ", " ")
    elif kind == "unlink":
        cmd = f"unlink {p()}"
    elif kind == "rmdir":
        cmd = f"rmdir {p()}"
    elif kind == "mv":
        flags = draw(st.sampled_from(["", "-f", "-n", "-T"]))
        cmd = f"mv {flags} {p()} {p()}".replace("  ", " ")
    elif kind == "cp":
        flags = draw(st.sampled_from(["", "-f", "-r"]))
        cmd = f"cp {flags} {p()} {p()}".replace("  ", " ")
    elif kind == "truncate":
        size = draw(st.sampled_from(["-s 0", "-s0", "--size=0", "-s 3", "-s +5", "-r empty.txt"]))
        cmd = f"truncate {size} {p()}"
    elif kind == "redirect":
        producer = draw(st.sampled_from([
            "", ":", "true", "echo -n", "printf ''", "cat empty.txt", "echo hi", "grep zzz README.md",
            "git show HEAD:src/app.py", "git show HEAD~1:README.md", "cat /dev/null"]))
        op = draw(st.sampled_from([">", ">", ">|", "&>", ">>", "1>"]))
        cmd = f"{producer} {op} {p()}".strip()
    elif kind == "find":
        start = p()
        test = draw(st.sampled_from(["", "-name '*.py'", "-type f", "-name 'app*'", "-maxdepth 1"]))
        action = draw(st.sampled_from(["-delete", "-exec rm {} +", "-exec rm -rf {} \\;",
                                       "-type f -delete", "-print"]))
        cmd = f"find {start} {test} {action}".replace("  ", " ")
    elif kind == "xargs":
        feeder = draw(st.sampled_from(["echo", "printf '%s\\n'", "ls -d"]))
        runner = draw(st.sampled_from(["xargs rm -f", "xargs rm -rf", "xargs -I{} rm {}",
                                       "xargs -n1 truncate -s0", "xargs git checkout --"]))
        cmd = f"{feeder} {p()} | {runner}"
    elif kind == "git_checkout":
        cmd = draw(st.sampled_from([
            f"git checkout -- {p()}", f"git checkout {p()}", "git checkout .", "git checkout -f",
            "git checkout -f HEAD", f"git checkout HEAD -- {p()}", f"git checkout HEAD~1 -- {p()}",
            "git checkout -q HEAD~1", "git switch -f main", f"git checkout --theirs {p()}",
            f"git checkout-index -f {p()}", "git checkout-index -a -f",
        ]))
    elif kind == "git_reset":
        cmd = draw(st.sampled_from([
            "git reset --hard", "git reset --hard HEAD~1", "git reset --soft HEAD~1", "git reset",
            "git reset HEAD~1", "git reset --keep HEAD~1", "git reset --merge HEAD~1",
            f"git reset -q -- {p()}", "git reset --hard -q", "git -c x.y=1 reset --hard",
            "git read-tree -u --reset HEAD", "git read-tree -m -u HEAD~1",
        ]))
    elif kind == "git_clean":
        cmd = draw(st.sampled_from(["git clean -f", "git clean -fd", "git clean -fdx", "git clean -n",
                                    "git clean -xf", "git clean --force -d", f"git clean -f {p()}"]))
    elif kind == "git_restore":
        cmd = draw(st.sampled_from([
            f"git restore {p()}", f"git restore --staged {p()}", f"git restore -W {p()}",
            f"git restore --source=HEAD~1 {p()}", "git restore .", f"git restore -s HEAD {p()}",
        ]))
    elif kind == "git_stash":
        cmd = draw(st.sampled_from([
            "git stash", "git stash -u", "git stash push -q", "git stash && git stash drop",
            "git stash -u && git stash drop", "git stash && git stash clear", "git stash list",
            f"git stash push -- {p()}", "git stash -q; git stash drop -q", "git stash pop",
            "git stash save x && git stash drop",
        ]))
    elif kind == "git_rm":
        cmd = draw(st.sampled_from([f"git rm {p()}", f"git rm -f {p()}", f"git rm -r -f {p()}",
                                    f"git rm -rq {p()}", f"git rm --cached {p()}", f"git rm -r --cached {p()}"]))
    elif kind == "git_mv":
        cmd = draw(st.sampled_from([f"git mv {p()} {p()}", f"git mv -f {p()} {p()}", f"git mv -k {p()} {p()}"]))
    elif kind == "git_other":
        cmd = draw(st.sampled_from([
            "git status --short", "git diff --stat", "git add -A", "git commit -qam wip",
            "git apply -R --index /dev/null", "git rev-parse HEAD", "git log --oneline -1",
        ]))
    else:
        cmd = draw(st.sampled_from(["ls", "echo ok", "cat README.md", "mkdir -p tmp/y", "touch new.txt",
                                    f"wc -l {p()}", "pwd"]))
    return _wrap_assigns(assigns, cmd)


def _wrap(inner: str, how: str, depth: int) -> str:
    delim = f"EOF{depth}"
    if how == "sh -c":
        return f"sh -c {shlex.quote(inner)}"
    if how == "bash -c":
        return f"bash -c {shlex.quote(inner)}"
    if how == "eval":
        return f"eval {shlex.quote(inner)}"
    if how == "eval_dq":
        return 'eval "' + inner.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    if how == "subshell":
        return f"( {inner} )"
    if how == "group":
        return f"{{ {inner}; }}"
    if how == "subst":
        return f"echo $( {inner} )"
    if how == "heredoc_bash":
        return f"bash <<'{delim}'\n{inner}\n{delim}"
    if how == "heredoc_pipe":
        return f"cat <<'{delim}' | sh\n{inner}\n{delim}"
    if how == "script_file":
        return f"cat > run{depth}.sh <<'{delim}'\n{inner}\n{delim}\nsh run{depth}.sh"
    if how == "script_echo":
        return f"echo {shlex.quote(inner)} > run{depth}.sh && bash run{depth}.sh"
    if how == "cd":
        return f"cd {{d}} && {inner}"
    if how == "cd_sub":
        return f"(cd {{d}}) ; {inner}"
    if how == "env":
        return f"env X=1 {inner}" if not inner.startswith(("(", "{", "for ", "export", "cd ")) and "=" not in inner.split(" ")[0] else inner
    if how == "timeout":
        return f"timeout 5 {inner}" if not inner.startswith(("(", "{", "for ", "export", "cd ")) and "=" not in inner.split(" ")[0] else inner
    if how == "if":
        return f"if true; then {inner}; fi"
    if how == "fn":
        return f"f() {{ {inner}; }}; f"
    raise AssertionError(how)


_WRAPS = ["sh -c", "bash -c", "eval", "eval_dq", "subshell", "group", "subst", "heredoc_bash",
          "heredoc_pipe", "script_file", "script_echo", "cd", "cd", "cd_sub", "env", "timeout", "if", "fn"]


@st.composite
def _command(draw, depth: int = 0) -> str:
    choice = draw(st.sampled_from(["simple", "simple", "wrap", "seq"] if depth < 2 else ["simple"]))
    if choice == "simple":
        return draw(_simple())
    if choice == "wrap":
        how = draw(st.sampled_from(_WRAPS))
        out = _wrap(draw(_command(depth + 1)), how, depth)
        return out.replace("{d}", draw(st.sampled_from(_DIRS))) if how in ("cd", "cd_sub") else out
    sep = draw(st.sampled_from([" && ", "; ", " || ", " | ", "\n"]))
    left, right = draw(_command(depth + 1)), draw(_command(depth + 1))
    if "\n" in left and sep != "\n":
        sep = "\n"                       # a heredoc must close before the next command starts
    return f"{left}{sep}{right}"


# --- running one command -------------------------------------------------------

_ABSOLUTE = re.compile(r"(^|[\s'\"=(<>|;&])/(?!dev/null\b)")


def _snapshot(repo: str) -> dict:
    """Every file in the worktree (not .git), path -> content."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        if dirpath == repo and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                with open(path, errors="replace") as f:
                    out[os.path.relpath(path, repo)] = f.read()
            except OSError:
                pass
    return out


def _stashed(repo: str, env: dict, rel: str, text: str) -> bool:
    """Is this content still held by a stash entry?"""
    r = subprocess.run(["git", "stash", "list", "--format=%H"], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=10)
    for sha in r.stdout.split():
        shown = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=10)
        if shown.returncode == 0 and shown.stdout == text:
            return True
    return False


def _losses(before: dict, after: dict, repo: str, env: dict) -> list[str]:
    """What tracked work the command lost, judged from the files.

    Content that still exists at some path was moved or copied, not lost, and
    content replaced by new text is an edit. Lost: a tracked file gone with its
    content nowhere, emptied, overwritten with another file's content (a mv or
    cp onto it), or an uncommitted edit reverted to HEAD with no stash keeping
    it -- unless that one file is all the command touched: putting a single
    file back to its committed version is the agent undoing its own edit,
    which the gate lets through (deep_agent._REVERT, 2026-10-24). Both edits
    reverted (`.`, `src/`, a glob) is wholesale and still counts."""
    lost = []
    if not os.path.isdir(os.path.join(repo, ".git")):
        lost.append(".git is gone")
    surviving = set(after.values())
    for rel in _TRACKED_FILES:
        old, new = before.get(rel), after.get(rel)
        if old is None or old == new or old in surviving:
            continue
        if rel in _EDITS and old == _EDITS[rel] and _stashed(repo, env, rel, old):
            continue                                  # `git stash pop` brings it back
        if new is None:
            lost.append(f"{rel} is gone")
        elif not new:
            lost.append(f"{rel} was emptied")
        elif rel in _EDITS and old == _EDITS[rel] and new == _HEAD[rel]:
            lost.append(f"{rel}: its uncommitted edit was reverted")
        elif any(new == text for other, text in before.items() if other != rel):
            lost.append(f"{rel} was overwritten by another file")
    if len(lost) == 1 and lost[0].endswith("its uncommitted edit was reverted"):
        return []
    return lost


def _run_case(template, command: str) -> None:
    assert ".." not in command, command
    assert not _ABSOLUTE.search(command), command
    work = tempfile.mkdtemp(dir=template.base, prefix="case-")
    repo = os.path.join(work, "repo")
    try:
        shutil.copytree(template.repo, repo, symlinks=True)
        asked = interrupt_on_for(True, repo)["bash"]["when"](
            SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}}))
        before = _snapshot(repo)
        proc = subprocess.Popen(["bash", "-c", command], cwd=repo, env=template.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        lost = _losses(before, _snapshot(repo), repo, template.env)
        assert asked or not lost, f"the gate did not ask, and the command lost {lost}:\n{command}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@settings(settings.get_profile(_PROFILE))
@given(command=_command())
def test_a_command_that_loses_tracked_work_is_always_asked_about(template, command):
    _run_case(template, command)


# --- what the search found -------------------------------------------------------
#
# Each of these lost tracked work in a real shell while the gate said nothing,
# until the fix in the same commit. They run on every push, whatever the
# derandomized search happens to reach.
FOUND = [
    # a compound command's opening word hid the command behind it
    "{ { rm src/util.py; }; }",
    "if true; then unlink src/util.py; fi",
    "f() { rm src/util.py; }; f",
    "for P in src/; do find \"$P\" -name 'app*' -delete; done",
    # a variable rebound by a loop, and an escaped `\$F` expanded too early
    "export P=notes.txt; for P in src/l*; do rm --recursive --force $P; done",
    'eval "export F=src/lib/core.py; grep zzz README.md &> \\"\\$F\\""',
    # git verbs that delete or overwrite tracked files
    'git rm -rq "docs"',
    "git mv -f src/lib/core.py docs/guide.md",
    "git checkout --theirs src/",
    "git checkout-index -f src/app.py",
    "git read-tree -u --reset HEAD",
    "echo src/app.py | xargs git checkout --",
    # tracked content under a build-output name
    "rm -f dist/bundle.js",
    # redirects, cp and xargs onto tracked files
    "> *.md",
    "true &> src/util.py",
    "grep zzz README.md > README.md",
    "sh -c \"printf '' > src/util.py\"",
    "echo README.md | xargs -n1 truncate -s0",
    "cp empty.txt README.md",
    "cp -r tmp/x.txt src/util.py",
    # where the shell really is: a sibling subshell's cd, a cd that failed
    "(cd src) ; ( unlink docs/guide.md )",
    "cd lib; rm -f README.md",
    "echo x | cd dist && rm -fr */*.py",              # a cd in a pipeline stage moves nothing
    # a tracked file moved, then deleted at its new name
    "git mv README.md docs && cd docs && rm README.md",
    "mv src/util.py tmp/ && rm -rf tmp",
    # committed, then the commit dropped with the files
    "git commit -qam wip && git reset --keep HEAD~1",
    # a script written, then run
    "echo 'rm * *' > run0.sh && bash run0.sh",
    "cat > run0.sh <<'EOF0'\nrm -rf src\nEOF0\nsh run0.sh",
]


@pytest.mark.parametrize("command", FOUND)
def test_a_shape_the_search_found_is_asked_about(template, command):
    _run_case(template, command)


def test_the_harness_sees_a_loss_when_the_gate_is_bypassed(template, monkeypatch):
    """The property is only as good as its oracle: with the gate forced to
    say no, a plain `rm src/util.py` must fail the check."""
    import agent.deep_agent as deep_agent
    monkeypatch.setattr(deep_agent, "_bash_deletes_real_work", lambda req, repo_root=None: False)
    with pytest.raises(AssertionError, match="src/util.py is gone"):
        _run_case(template, "rm src/util.py")
    with pytest.raises(AssertionError, match="uncommitted edit was reverted"):
        _run_case(template, "git checkout -- .")
    with pytest.raises(AssertionError, match="README.md was emptied"):
        _run_case(template, "truncate -s 0 README.md")


@pytest.mark.parametrize("command", [
    "rm -f notes.txt", "rm -rf tmp", "echo hi >> README.md", "cat > new.txt <<'X'\nrm -rf src\nX",
    "git stash", "git reset --soft HEAD~1", "rm -rf src/scratch.log", "sed -i s/v2/v3/ README.md",
    # one named file put back to HEAD: the agent undoing its own edit
    "git checkout -- src/app.py", "git restore src/app.py", "git show HEAD:src/app.py > src/app.py",
])
def test_ordinary_work_is_not_a_loss(template, command, monkeypatch):
    """...and it does not cry wolf: these keep every tracked byte (a stash
    keeps the edit), so they pass even with the gate forced to say no."""
    import agent.deep_agent as deep_agent
    monkeypatch.setattr(deep_agent, "_bash_deletes_real_work", lambda req, repo_root=None: False)
    _run_case(template, command)


@pytest.mark.parametrize("command", [
    "git checkout -- src/app.py", "git checkout HEAD -- src/app.py", "git restore src/app.py",
    "git restore --source=HEAD src/app.py", "git show HEAD:src/app.py > src/app.py",
    "cd src && git checkout -- app.py",
])
def test_one_named_file_back_to_head_does_not_ask(template, command):
    """2026-10-24: the agent putting package.json back after an npm install
    asked three times on one task. One tracked file, back to its committed
    text, is its own edit undone."""
    from agent.deep_agent import interrupt_on_for
    from types import SimpleNamespace
    when = interrupt_on_for(True, template.repo)["bash"]["when"]
    assert not when(SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}}))


@pytest.mark.parametrize("command", [
    "git checkout -- .", "git checkout -- src/", "git checkout -- 'src/*.py'", "git checkout -f -- src/app.py",
    "git checkout HEAD~1 -- src/app.py", "git restore .", "git restore --source=HEAD~1 src/app.py",
    "git show HEAD:README.md > src/app.py", "git show HEAD~1:src/app.py > src/app.py",
])
def test_wholesale_or_other_reverts_still_ask(template, command):
    from agent.deep_agent import interrupt_on_for
    from types import SimpleNamespace
    when = interrupt_on_for(True, template.repo)["bash"]["when"]
    assert when(SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}}))


@pytest.mark.parametrize("command", [
    "cp README.md tmp/b && sed -i s/v2/v9/ README.md && cp tmp/b README.md",
    "cd src && cp app.py lib/app.bak && sed -i s/v2/v9/ app.py && cp lib/app.bak app.py",
    "cd . && cp src/app.py tmp/b; cp tmp/b src/app.py",
])
def test_a_file_put_back_from_its_own_backup_does_not_ask(template, command):
    """2026-10-24: try a change, then restore the file from a backup made
    earlier in the same command -- the agent's mutation checks asked each
    time. The restore is the file's own bytes; the run must lose nothing."""
    from agent.deep_agent import interrupt_on_for
    from types import SimpleNamespace
    when = interrupt_on_for(True, template.repo)["bash"]["when"]
    assert not when(SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}}))
    _run_case(template, command)


@pytest.mark.parametrize("command", [
    "cp README.md tmp/b && echo x > tmp/b && cp tmp/b README.md",
    "cp README.md tmp/b && cp src/util.py tmp/b && cp tmp/b README.md",
    "cp src/util.py tmp/b && cp tmp/b README.md",
    "B=tmp/b; cp README.md $B && cp $B README.md && cp src/util.py tmp/b && cp tmp/b README.md",
])
def test_a_backup_touched_in_between_or_from_another_file_still_asks(template, command):
    _run_case(template, command)
    from agent.deep_agent import interrupt_on_for
    from types import SimpleNamespace
    when = interrupt_on_for(True, template.repo)["bash"]["when"]
    assert when(SimpleNamespace(tool_call={"name": "bash", "args": {"command": command}}))
