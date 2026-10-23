"""Comments and docstrings must not point at things that no longer exist.

A comment that says "see agent/foo.py" or "`config.load_settings` decides" is
a map; after a rename or a delete it sends the reader somewhere that is not
there. This scans the comments and docstrings of agent/, services/,
frontend/src and scripts/ for these pointers:

* repo file paths -- ``agent/foo.py``, ``nodes/work.py``, ``scripts/x.sh``, a
  bare ``foo.py``. A path resolves if it exists from the repo root, from the
  commenting file's directory, from a source root (agent/, frontend/src/,
  a service's directory), as a path suffix of a tracked file, as the local
  copy of a tracked ``.example``, or -- for a bare filename -- as the
  basename of any tracked file. URLs, absolute paths and placeholders are
  not pointers.
* qualified names in backticks -- ``module.func`` / ``module.Class.method``,
  optionally with ``()``. Checked when the first part is a module in the
  commenting file's own tree (agent/ + scripts/, the service, or
  frontend/src) and every other part is a module or defined name; the last
  part must be defined in the repo (a def, class, assignment, import, string
  key, or a JS/TS declaration or key) and, when the prefix is a .py module,
  in that module (or in the commenting file itself).
* a file and a name in it -- ``outer_graph.py's _route_after_verify`` (code-
  shaped names only) and ``agent/tools/github_tools._REMOTE_RE``.

The agent's comments are full of OTHER projects' files -- a target repo's
``src/core/bot.js``, a SWE-bench ``runtests.py`` -- so an unresolved path only
fails when it is clearly this repo's: rooted at agent/, services/ or
frontend/, a miscased spelling of a tracked file (``app.tsx``), or a path
(or bare filename) this repo's git history once held.
A rename or a delete is how a pointer goes stale, and history is the record
of exactly that. A shallow clone has no history and checks less; the rooted
rule still holds there. docs/ (audit reports included) and CHANGELOG are not
scanned: they record what was true when written.
"""
from __future__ import annotations

import ast
import io
import posixpath
import re
import subprocess
import tokenize
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("agent", "services", "frontend/src", "scripts")
SOURCE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh"}
# Extensions that name source in this repo; data/config names (json, yaml,
# jsonl, env) are mostly runtime files the agent writes, not repo files.
POINTER_EXT = ("py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "sh", "ps1", "css", "html", "md")
# Extensions a backticked dotted word may end in when it is a filename, not a name.
FILE_EXT = set(POINTER_EXT) | {"json", "jsonl", "yaml", "yml", "toml", "txt", "lock", "cfg", "ini",
                               "env", "log", "sql", "svg", "png", "sln", "csproj", "example", "local", "csv",
                               "pdf", "zip", "gz", "tar", "xml", "db"}
# Source roots a path may be written relative to.
SOURCE_ROOTS = ("", "agent", "frontend/src", "frontend", "app", "app/src-tauri", "site",
                "services/model-router", "services/agent-review", "services/commit-reviewer",
                "services/logoloom", "services/shared", "tests", "scripts", "docker")
# A path starting here is this repo's whether or not history knows it.
OWN_ROOTS = ("agent/", "services/", "frontend/")


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


_SKIP_DIRS = {".git", ".claude", ".venv", "node_modules", "dist", "build", "__pycache__", "target"}


@cache
def tracked() -> tuple[str, ...]:
    listed = _git("ls-files").splitlines()
    if not listed:  # no git, or one that refuses this checkout: walk it instead
        listed = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                  if p.is_file() and not _SKIP_DIRS & set(p.relative_to(ROOT).parts)]
    return tuple(p for p in listed if not p.startswith(".claude/") and (ROOT / p).exists())


@cache
def _tracked_set() -> frozenset[str]:
    s = set(tracked())
    for p in tracked():  # directories too, so `agent/nodes/` resolves
        parts = p.split("/")
        for i in range(1, len(parts)):
            s.add("/".join(parts[:i]))
    s.update(p[: -len(".example")] for p in tracked() if p.endswith(".example"))
    return frozenset(s)


@cache
def _basenames() -> frozenset[str]:
    return frozenset(p.rsplit("/", 1)[-1] for p in _tracked_set())


@cache
def _history() -> tuple[frozenset[str], frozenset[str]]:
    """(paths, basenames) every commit reachable from HEAD ever touched."""
    paths = {p for p in _git("log", "--name-only", "--format=", "HEAD").splitlines() if p}
    return frozenset(paths), frozenset(p.rsplit("/", 1)[-1] for p in paths)


def scanned_files() -> list[Path]:
    return [ROOT / p for p in tracked()
            if p.startswith(tuple(d + "/" for d in SCAN_DIRS)) and Path(p).suffix in SOURCE_EXT]


# --------------------------------------------------------------------------
# comment extraction -> [(lineno, text)]
# --------------------------------------------------------------------------

def _py_comments(src: str) -> list[tuple[int, str]]:
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError):
        pass
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                first = body[0].lineno
                for i, line in enumerate(body[0].value.value.splitlines()):
                    out.append((first + i, line))
    return out


_JS_COMMENT = re.compile(
    r"""(?P<str>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)"""
    r"""|(?P<line>//[^\n]*)|(?P<block>/\*.*?\*/)""",
    re.S,
)


def _js_comments(src: str) -> list[tuple[int, str]]:
    out = []
    for m in _JS_COMMENT.finditer(src):
        if m.group("str"):
            continue
        start = src.count("\n", 0, m.start()) + 1
        for i, line in enumerate(m.group(0).splitlines()):
            out.append((start + i, line))
    return out


def _sh_comments(src: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.lstrip()
        if s.startswith("#") and not s.startswith("#!"):
            out.append((i, s))
        elif " # " in line:
            out.append((i, line.split(" # ", 1)[1]))
    return out


def comments(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        return _py_comments(src)
    if path.suffix == ".sh":
        return _sh_comments(src)
    return _js_comments(src)


# --------------------------------------------------------------------------
# file pointers
# --------------------------------------------------------------------------

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+|\b[\w.-]+\.(?:com|io|ai|org|net|dev)/\S*", re.I)
_PATH = re.compile(
    r"(?<![\w/.~$<{@-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:" + "|".join(POINTER_EXT) + r"))(?!\.?[\w/-])"
)


def resolve_files(ref: str, source: Path) -> list[str]:
    """The tracked files `ref`, written in `source`, can mean (most specific first)."""
    ts = _tracked_set()
    rel_dir = source.parent.relative_to(ROOT).as_posix()
    for base in (rel_dir, *SOURCE_ROOTS):
        cand = posixpath.normpath(posixpath.join(base, ref))
        if cand in ts:
            return [cand]
    if "/" in ref:
        return [p for p in ts if p.endswith("/" + ref)]
    return [p for p in ts if p.rsplit("/", 1)[-1] == ref]


def resolves(ref: str, source: Path) -> bool:
    return bool(resolve_files(ref, source))


def is_own(ref: str) -> bool:
    """Is an unresolved `ref` this repo's (so it is stale), or someone else's?"""
    if ref.startswith(OWN_ROOTS):
        return True
    low = ref.lower()  # `app.tsx` for App.tsx: this repo's file, misspelled
    if any(p.lower() == low or p.lower().endswith("/" + low) for p in _tracked_set()):
        return True
    paths, names = _history()
    if "/" in ref:
        return ref in paths or any(p.endswith("/" + ref) for p in paths)
    return ref in names


# --------------------------------------------------------------------------
# qualified names
# --------------------------------------------------------------------------

_BACKTICK = re.compile(r"`([A-Za-z_][\w.]*\.[A-Za-z_]\w*)(?:\(\))?`")


def module_scope(source: Path) -> tuple[str, ...]:
    """Where a dotted name written in `source` can live: the agent's modules
    from agent/ and scripts/, a service's own modules (and the shared ones)
    from inside it. `app.state` in agent/ is FastAPI's, not the router's app.py."""
    rel = source.relative_to(ROOT).as_posix()
    if rel.startswith("services/"):
        return ("/".join(rel.split("/")[:2]) + "/", "services/shared/")
    if rel.startswith("frontend/"):
        return ("frontend/src/",)
    return ("agent/", "scripts/")


@cache
def repo_modules(scope: tuple[str, ...]) -> frozenset[str]:
    mods = set()
    for p in tracked():
        if not p.startswith(scope):
            continue
        if p.endswith(".py"):
            mods.update(p[:-3].split("/"))  # package dirs and the module stem
        elif p.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")):
            mods.add(Path(p).stem)
    return frozenset(m for m in mods if m.isidentifier())


_JS_DEF = re.compile(
    r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:async\s+|static\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*[(:=]"
    r"|([A-Za-z_$][\w$]*)\s*:"
    r"|\.([A-Za-z_$][\w$]*)\s*=[^=]",
    re.M,
)


@cache
def file_names(p: str) -> frozenset[str]:
    """Every name the tracked file `p` defines, imports, assigns or keys."""
    names: set[str] = {Path(p).stem}
    path = ROOT / p
    if p.endswith(".py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            return frozenset(names)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
                names.add(n.attr)
            elif isinstance(n, ast.alias):
                names.add((n.asname or n.name).split(".")[-1])
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, ast.keyword) and n.arg:
                names.add(n.arg)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                names.add(n.value)  # a dict key / getattr name is a definition of sorts
    elif p.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in _JS_DEF.finditer(src):
            names.update(g for g in m.groups() if g)
    return frozenset(names)


@cache
def defined_names() -> frozenset[str]:
    return frozenset().union(*(file_names(p) for p in tracked()))


def _module_files(parts: list[str], scope: tuple[str, ...]) -> list[str]:
    tail = "/".join(parts) + ".py"
    return [p for p in tracked() if p.startswith(scope) and (p == tail or p.endswith("/" + tail))]


def _classes(p: str) -> frozenset[str]:
    tree = ast.parse((ROOT / p).read_text(encoding="utf-8"))
    return frozenset(n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef))


def stale_name(dotted: str, source: Path) -> bool:
    *heads, last = dotted.split(".")
    scope = module_scope(source)
    mods = repo_modules(scope)
    if last in FILE_EXT or heads[0] not in mods:
        return False
    if not all(h in mods or h in defined_names() for h in heads[1:]):
        return False  # an external package's path that shares a first word with a module here
    if last not in defined_names():
        return True
    # `config.load` / `config.Settings.load` name a module: the rest must be
    # defined in it, not merely somewhere -- unless the commenting file is the
    # one that defines it (a JSON key it reads, say).
    for k in range(len(heads), 0, -1):
        files = _module_files(heads[:k], scope)
        if not files:
            continue
        rest = heads[k:]
        if len(rest) > 1 or (rest and not any(rest[0] in _classes(f) for f in files)):
            return False  # module.attr.attr: an object's attribute, out of reach of a static look
        here = frozenset().union(*(file_names(f) for f in files))
        mine = file_names(source.relative_to(ROOT).as_posix())
        return not all(n in here or n in mine for n in [*rest, last])
    return False


# "deep_agent.py's INTERRUPT_ON", "outer_graph.py's `_route_after_verify`":
# a file and a name in it. Only code-shaped names (snake_case with an
# underscore, or camelCase) -- "server.py's own comment" and "its HITL gate"
# are prose.
_FILES_NAME = re.compile(
    r"((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|js|mjs|cjs|ts|tsx|jsx|sh))'s\s+`?([A-Za-z_]\w*)(?:\(\))?`?"
)


# "agent/tools/github_tools._REMOTE_RE": a module path and a name in it.
_PATH_NAME = re.compile(r"(?<![\w/.~$<{@-])((?:[\w-]+/)+\w+)\.([A-Za-z_]\w*)\b(?![./])")


def _code_shaped(name: str) -> bool:
    return "_" in name or bool(re.search(r"[a-z][A-Z]", name))


def problems(text: str, path: Path) -> list[str]:
    """What is stale in one comment line `text` of the file `path`."""
    clean = _URL.sub(" ", text)
    out = []
    for m in _PATH.finditer(clean):
        if not resolves(m.group(1), path) and is_own(m.group(1)):
            out.append(f"file `{m.group(1)}` is not in the repo")
    for m in _BACKTICK.finditer(clean):
        if stale_name(m.group(1), path):
            out.append(f"`{m.group(1)}` is not defined there")
    for m in _FILES_NAME.finditer(clean):
        ref, name = m.groups()
        files = resolve_files(ref, path)
        if files and _code_shaped(name) and not any(name in file_names(f) for f in files):
            out.append(f"`{name}` is not in {ref}")
    for m in _PATH_NAME.finditer(clean):
        mod, name = m.groups()
        files = resolve_files(mod + ".py", path) if name not in FILE_EXT else []
        if files and not any(name in file_names(f) for f in files):
            out.append(f"`{name}` is not in {mod}.py")
    return out


def stale_pointers() -> list[str]:
    out = []
    for path in scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, text in comments(path):
            out += [f"{rel}:{lineno}: {p} -- {text.strip()[:140]}" for p in problems(text, path)]
    return out


def test_comments_point_at_things_that_exist():
    assert len(scanned_files()) > 100, "the scan found almost nothing to read -- the file listing is broken"
    bad = stale_pointers()
    assert not bad, "comment points at a file or name that is not in the repo:\n  " + "\n  ".join(bad)


def test_path_rules():
    src = ROOT / "agent" / "server.py"
    assert resolves("agent/server.py", src)
    assert resolves("nodes/work.py", src)           # suffix of a tracked path
    assert resolves("server.py", src)               # bare basename
    assert resolves("../frontend/src/main.tsx", src)  # relative to the commenting file
    assert not resolves("agent/no_such_module_xyz.py", src)
    assert is_own("agent/no_such_module_xyz.py")    # rooted here: stale, whatever history says
    assert not is_own("src/core/no_such_bot_xyz.js")  # a target project's file
    assert is_own("app.tsx") and not resolves("app.tsx", src)  # App.tsx, miscased
    assert not problems("# see https://example.com/agent/gone_xyz.py", src)  # a URL


def test_line_rules():
    src = ROOT / "agent" / "server.py"
    assert problems("# lives in agent/gone_xyz.py now", src)
    assert not problems("# lives in agent/server.py now", src)
    assert not problems("# a target repo's src/core/gone_xyz.js", src)
    assert problems("# agent/tools/github_tools._GONE_XYZ decides", src)
    assert not problems("# agent/tools/github_tools._REMOTE_RE decides", src)
    assert problems("# see server.py's _gone_xyz", src)
    assert not problems("# see server.py's _stream_graph", src)
    assert not problems("# server.py's own comment", src)


def test_name_rules():
    src = ROOT / "agent" / "server.py"
    assert not stale_name("asyncio.to_thread", src)     # not a repo module
    assert not stale_name("App.sln", src)               # a filename
    assert not stale_name("app.state", src)             # FastAPI's, not services/'s app.py
    assert stale_name("config.no_such_function_xyz", src)
    assert stale_name("config.stale_name", src)         # defined, but not in agent/config.py


if __name__ == "__main__":
    for line in stale_pointers():
        print(line)
