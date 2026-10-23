"""Three shapes of a wrong fix the ship gate can see in the diff itself.

Two 50-task SWE-bench samples (2026-09-25) lost the same tasks the same way
while the coder's prompt said, in words, not to: an invented error message
where the tests assert the existing template; a grammar rewritten so that it
accepts what it used to reject, with tests for the parsing side only; a fix
applied to one of two functions of the same name, the sibling untouched.
A rule in a long system prompt lost to the decision in front of the model
every time. These fire at the decision point instead -- once each, as a
loop-back with the specific thing to change -- and only on a benchmark
project for now, where the hidden tests are what they are calibrated on.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_HUNK_FUNC = re.compile(r"^@@[^@\n]*@@\s*(?:async\s+)?def\s+(\w+)\s*\(", re.M)
_RAISE = re.compile(r"^\+\s*raise\s+\w[\w.]*\(", re.M)
_STRING = re.compile(r"""(?:[fFrRbBuU]{0,2})(["'])((?:(?!\1)[^\n\\]|\\.){8,}?)\1""")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}|%[-#0 +]*\d*(?:\.\d+)?[sdrfiu]|\s+")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")
_GRAMMAR_LINE = re.compile(r"re\.compile\(|\bdef p_\w+\(|\byacc\.|\blex\.|_RE\s*=|_re\s*=\s*re\.")
_GRAMMAR_FILE = re.compile(r"(grammar|parser|lexer|parsetab|lextab|/format/)", re.I)
_REJECTION = re.compile(r"pytest\.raises|assertRaises|\braises\(|xfail|\bnot\s+\w+\.match\(|is_valid\(.*\)\s*is\s+False")
_GENERIC_NAMES = {"main", "run", "get", "set", "init", "setup", "test", "call", "apply", "update", "process", "handle"}


def _norm(text: str) -> str:
    """Words only: placeholders, quotes and punctuation are not wording."""
    words = re.sub(r"[^a-z0-9 ]+", " ", _PLACEHOLDER.sub(" ", text).lower())
    return " ".join(words.split())


def _files_and_lines(diff: str) -> list[tuple[str, list[str], list[str]]]:
    """(path, added lines, removed lines) per file of a unified diff."""
    out: list[tuple[str, list[str], list[str]]] = []
    path: str | None = None
    added: list[str] = []
    removed: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            if path is not None:
                out.append((path, added, removed))
            m = _FILE.match(line)
            path, added, removed = (m.group(1) if m else line[4:]), [], []
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    if path is not None:
        out.append((path, added, removed))
    return out


def new_error_messages(diff: str, issue: str) -> list[str]:
    """Error strings this diff raises that neither the removed lines nor the
    report contain: new wording, which a hidden test written against the
    existing template rejects (astropy-13033, twice)."""
    issue_norm = _norm(issue or "")
    found: list[str] = []
    for path, added, removed in _files_and_lines(diff):
        if _TEST_PATH.search(path):
            continue
        removed_norm = _norm("\n".join(removed))
        for i, line in enumerate(added):
            if not _RAISE.match("+" + line):
                continue
            window = "\n".join(added[i:i + 3])
            for m in _STRING.finditer(window):
                text = m.group(2)
                norm = _norm(text)
                if len(norm) < 12 or not re.search(r"[a-z]{3}", norm):
                    continue
                if norm in removed_norm or norm in issue_norm:
                    continue
                if text not in found:
                    found.append(text)
                break
    return found


_HUNK_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEF_LINE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")


def _enclosing_defs(path: Path, line_no: int) -> list[str]:
    """Every function enclosing this line of the file, innermost first --
    the hunk header names only the outermost, and the function that
    changed in sphinx-7462 was `unparse` nested inside `_parse_annotation`
    (twice)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    if not lines or line_no < 1:
        return []
    i = min(line_no, len(lines)) - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    indent = len(lines[i]) - len(lines[i].lstrip()) if i >= 0 else 0
    names: list[str] = []
    for j in range(i, -1, -1):
        m = _DEF_LINE.match(lines[j])
        if m and len(m.group(1)) < indent or (m and j == i):
            names.append(m.group(2))
            indent = len(m.group(1))
            if indent == 0:
                break
    return names


def changed_functions(diff: str, repo_root: str | Path | None = None) -> list[tuple[str, str]]:
    """(path, function) for every hunk inside a Python function body: the
    enclosing functions read from the file when the tree is at hand
    (nested ones included), else the hunk header's."""
    out: list[tuple[str, str]] = []
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            m = _FILE.match(line)
            path = m.group(1) if m else line[4:]
        elif line.startswith("@@") and path and path.endswith(".py") and not _TEST_PATH.search(path):
            names: list[str] = []
            start = _HUNK_START.match(line)
            if repo_root is not None and start:
                first = int(start.group(1)) + 3   # past the hunk's leading context lines
                names = _enclosing_defs(Path(repo_root) / path, first)
            if not names:
                m = _HUNK_FUNC.match(line)
                names = [m.group(1)] if m else []
            for name in names:
                if (path, name) not in out:
                    out.append((path, name))
    return out


def sibling_definitions(repo_root: str | Path, changed: list[tuple[str, str]], limit: int = 6) -> dict[str, list[str]]:
    """Other, non-test Python files defining a function the diff changed.
    A name defined in more than `limit` places is too generic to mean a
    sibling implementation (sphinx-7462: `unparse` in domains/python.py and
    pycode/ast.py, the second never touched, twice)."""
    out: dict[str, list[str]] = {}
    for path, name in changed:
        if name.startswith("_") or len(name) < 4 or name in _GENERIC_NAMES:
            continue
        try:
            r = subprocess.run(
                ["grep", "-rlE", "--include=*.py", rf"^\s*(async\s+)?def {name}\(", "."],
                cwd=str(repo_root), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        files = sorted({f[2:] if f.startswith("./") else f for f in r.stdout.split()
                        if f and not _TEST_PATH.search(f) and "/.scratch/" not in f})
        files = [f for f in files if f != path]
        if files and len(files) <= limit:
            out[name] = files
    return out


def touches_grammar(diff: str) -> bool:
    """A parser, grammar or regular-expression change in non-test code."""
    for path, added, removed in _files_and_lines(diff):
        if _TEST_PATH.search(path) or not path.endswith(".py"):
            continue
        if _GRAMMAR_FILE.search(path) and (added or removed):
            return True
        if any(_GRAMMAR_LINE.search(ln) for ln in added + removed):
            return True
    return False


def has_rejection_test(diff: str) -> bool:
    """An added test line that asserts something is rejected."""
    for path, added, _removed in _files_and_lines(diff):
        if _TEST_PATH.search(path) and any(_REJECTION.search(ln) for ln in added):
            return True
    return False


def nudge(diff: str, issue: str, repo_root: str | Path, fired: set[str]) -> tuple[str, str] | None:
    """The first pattern not yet answered on this task: (name, feedback)."""
    if "new_error_text" not in fired:
        texts = new_error_messages(diff, issue)
        if texts:
            quoted = "; ".join(f"«{t[:160]}»" for t in texts[:3])
            return ("new_error_text",
                    f"Your change raises a new error message: {quoted}. The report does not ask for new "
                    f"wording, and the tests that grade this were written against the message the code "
                    f"already had. Keep the existing message template and change only its arguments -- "
                    f"what it reports, not how it says it -- unless the report quotes the wording it wants.")
    if "sibling_definition" not in fired:
        siblings = sibling_definitions(repo_root, changed_functions(diff, repo_root))
        if siblings:
            listed = "; ".join(f"`{name}` also in {', '.join(files)}" for name, files in siblings.items())
            return ("sibling_definition",
                    f"A function you changed is defined elsewhere too: {listed}. The same bug usually "
                    f"lives in the sibling implementation (the other parser, unparser, writer or "
                    f"formatter of the same thing). Run each sibling on the report's own input and "
                    f"compare what it RETURNS with what the report implies it should: the bug is the "
                    f"wrong output, not the crash. A sibling that returns '' or drops the input where "
                    f"the report implies something should render has the same bug even though it "
                    f"does not raise (2026-09-25: one printed '()' -> '' and was left alone as \"does "
                    f"not crash\"). Apply the same fix where the output is wrong, and say explicitly "
                    f"why not where it is right.")
    if "grammar_rejection" not in fired and touches_grammar(diff) and not has_rejection_test(diff):
        return ("grammar_rejection",
                "This change touches a grammar, parser or regular expression, and your added tests only "
                "check inputs that must parse. A parser fix must not widen what is accepted: add a test "
                "that an input which must STILL be rejected is rejected (the report's shape with the "
                "operators in another order, a trailing operator, an empty operand), and have the "
                "verifier probe the same. Prefer the smallest change to the one rule that was wrong over "
                "a rewrite of the rule set.")
    return None
