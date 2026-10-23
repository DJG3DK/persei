"""Project onboarding: inspect a directory, propose a configuration, and
provision it as a project this agent can work in.

Why detection PROPOSES rather than decides
------------------------------------------
Everything in a project's config falls into one of three buckets:

1. Derivable with certainty (is it a git repo, which package manager,
   which npm scripts exist). Detected and applied.
2. Derivable as a candidate (which gitignored files look like secrets the
   test suite needs, which pm2 apps serve this path). Detected, PROPOSED,
   and shown to the operator to accept or reject.
3. Not derivable at all. The canonical example lives in this deployment:
   one project's `test:auth` and `test:routes` make real HTTP calls to
   a live service -- a live service of the operator's -- and `test:routes` exercises
   POST /trade/open. Running them unattended would place real orders for
   zero review signal. No static analysis reliably distinguishes "hits a
   test server" from "hits your production system", so this module FLAGS
   the suspicion and refuses to silently enable such a script.

So the wizard is deliberately detect -> confirm -> provision, never a
one-click guess. A wrong guess here doesn't produce a bad config; it
produces an unattended agent running destructive commands against
production.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from fnmatch import fnmatch
from pathlib import Path

# Files that look like credentials, are gitignored, and therefore never
# reach a worktree checkout -- the review service copies them from the live
# checkout so the suite runs the way production would.
_SECRET_NAME_HINTS = re.compile(
    r"(^|/)(\.env($|\.)|.*\.env$|secrets?\.|credentials?\.|auth\.json|keys?\.json|"
    r"token|\.pem$|\.key$|serviceaccount)",
    re.IGNORECASE,
)

# Network calls inside a test file. A match doesn't prove the test hits
# production -- it proves we cannot prove it doesn't.
#
# The idioms are listed per ecosystem rather than as one loose pattern
# because the false-negative and the false-positive cost differently. A
# missed call is the npm-script incident again; a spurious match only means
# the operator is asked about a suite the wizard would otherwise have enabled
# silently. Bare URLs are deliberately NOT matched: every Go file that cites
# pkg.go.dev in a comment would flag, and a wizard that flags everything
# teaches the operator to click through it.
_NETWORK_CALL = re.compile(
    r"(fetch\s*\(|axios|http\.request|https\.request|got\s*\(|superagent|"
    r"requests\.(get|post|put|delete)|urllib|httpx\.|aiohttp|"
    # Go
    r"http\.(Get|Post|Head|PostForm|NewRequest)\b|http\.DefaultClient|"
    r"\bnet\.Dial\b|grpc\.Dial\b|"
    # Rust
    r"\breqwest\b|\bureq\b|hyper::Client|TcpStream::connect|"
    # Ruby. URI.parse is NOT here: parsing a string opens nothing, and it
    # appears in every spec that builds a URL for a stubbed request.
    r"Net::HTTP|HTTParty|RestClient|Faraday|Excon|URI\.open\s*\(|open-uri|"
    # JVM
    r"HttpURLConnection|RestTemplate|OkHttpClient|WebClient\.|java\.net\.URL\b|"
    # PHP
    r"curl_init|GuzzleHttp|Http::(get|post|put|delete)\b|file_get_contents\s*\(\s*['\"]https?|"
    # .NET
    r"\bHttpClient\b|WebRequest\.|\bRestSharp\b|"
    # Elixir
    r"HTTPoison|\bTesla\b|\bFinch\b|:httpc\b|\bReq\.(get|post)\b|Mint\.HTTP|"
    r"127\.0\.0\.1|localhost:\d+)",
    re.IGNORECASE,
)

# Evidence that a file's HTTP goes nowhere real.
#
# The rule above is deliberately suspicious, and its cost is false positives:
# an honest Rails spec, a Go test driving httptest.NewServer, a Python test
# using `responses` -- all of them "call the network" by the letter of the
# regex and none of them can touch production. Flagging those trains the
# operator to click through the flags, which is the same failure as not
# flagging at all, arrived at more slowly.
#
# So a file that shows one of these is not flagged. Each entry either serves
# the request in-process (httptest, Rack::Test, wiremock) or intercepts it
# and refuses real connections by default (WebMock, VCR, responses, respx,
# nock, mockito). None of them is a promise the author made to us -- they are
# libraries whose whole purpose is that no packet leaves.
# Case-SENSITIVE on purpose. These are library names, and the lowercase
# English words they collide with are not: `# bypass the cache` in a comment,
# beside a real HTTPoison call, used to neutralise the whole suite. Where a
# library is genuinely written lowercase in real code -- a require path, an
# import -- that spelling is listed explicitly rather than by folding case.
_NETWORK_STUBBED = re.compile(
    r"(httptest\.|net/http/httptest|"                          # Go
    r"\bmockito\b|MockWebServer|WireMock|wiremock|"            # JVM
    r"\bWebMock\b|webmock/|\bVCR\b|vcr/|Rack::Test|"         # Ruby
    r"ActionDispatch::IntegrationTest|"
    r"\bresponses\b|\brespx\b|requests_mock|httpretty|pytest_httpserver|"  # Python
    r"\bnock\b|msw/node|setupServer\s*\(|"                   # JS
    r"Http::fake|MockHandler|createMock\s*\(|"                # PHP
    r"\bMoq\b|MockHttpMessageHandler|"                        # .NET
    r"\bBypass\b|\bMox\b|Plug\.Test|ExVCR)",                # Elixir
)

_SCRIPT_REF = re.compile(r"[\w./-]+\.(?:js|mjs|cjs|ts|tsx|py)")

# Which files hold a language's tests. Used only to decide whether a
# whole-suite check (`go test ./...`, `cargo test`, `bundle exec rspec`)
# arrives enabled: unlike npm, these ecosystems have no per-suite script
# names to flag individually, so the unit of suspicion is the suite.
_TEST_FILE_GLOBS = {
    "go": ("*_test.go",),
    "rust": ("*.rs",),
    "ruby": ("*_spec.rb", "*_test.rb"),
    "python": ("test_*.py", "*_test.py"),
    "elixir": ("*_test.exs",),
    "java": ("*Test.java", "*Tests.java", "*IT.java", "*Test.kt", "*Tests.kt"),
    "php": ("*Test.php",),
    "dotnet": ("*Test.cs", "*Tests.cs"),
}

# A suite scan reads files, so it is bounded twice: by how many files it will
# open and by how much of each it reads. A monorepo with ten thousand test
# files must not turn one wizard click into a minute of IO.
_SCAN_FILE_LIMIT = 600
_SCAN_BYTES = 200_000

TEST_TIMEOUT_MS_DEFAULT = 900_000

# Directories never worth mounting or scanning.
# Directories never worth mounting or scanning. Every entry is somebody
# else's code or a build output: a hex package's own suite calling HTTPoison
# said nothing about this repo's tests, and flagging the app's suite for it
# is how an operator learns to click through the flags.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "vendor",
    ".cache", "target", ".gradle",
    "deps", "_build",      # Elixir: hex packages and compiled beams
    "obj",                 # .NET restore output
    # NOT "packages": .NET's old-style package dir shares its name with a
    # first-party source directory in every JS monorepo, and skipping it
    # would hide the repo's own tests from the scan.
    "Pods", "elm-stuff",   # other ecosystems' vendored trees
}

CHECK_TIMEOUT_MS_DEFAULT = 600_000


class ProvisioningError(Exception):
    """Raised for an input the operator must fix (bad path, name clash).
    `detail` is the message as written for the operator; endpoints return
    that attribute rather than str(e), so only curated text leaves."""

    def __init__(self, detail: str = ""):
        super().__init__(detail)
        self.detail = detail


class PathNotAllowedError(ProvisioningError):
    """The requested path is outside every configured project root."""


# Where projects may live. Onboarding hands an agent bash and write access to
# whatever it points at, and makes the review service COPY the files listed as
# secrets into a worktree -- so "any absolute path on the host" is far too much
# authority to grant from an HTTP request, even an admin's. Containment is the
# boundary; the admin check is only who may ask.
#
# Colon-separated, like PATH. Defaults to /home, which covers the normal
# layout without reaching /root, /etc, or the agent's own credentials.
def allowed_roots() -> list[str]:
    raw = os.environ.get("AGENT_PROJECT_ROOTS", "/home")
    return [os.path.realpath(r) for r in raw.split(":") if r.strip()]


def sandbox_root() -> str:
    """Server-owned, never client-supplied: the worktree location is a write
    primitive, so it is derived from config plus the project name."""
    return os.path.realpath(os.environ.get("AGENT_SANDBOX_ROOT", "/home/agent-workspaces"))


def _agent_own_roots() -> list[str]:
    """Every path that IS this agent's own code.

    Plural because this file may be running from a git worktree of the agent
    repo (that is how its own features get developed). Checking only the
    module's parent would then guard the worktree while leaving the real
    repo onboardable -- so follow the worktree's .git pointer file back to
    the main checkout and block both.
    """
    own = os.path.realpath(Path(__file__).resolve().parent.parent)
    roots = [own]
    dotgit = os.path.join(own, ".git")
    if os.path.isfile(dotgit):
        try:
            gitdir = open(dotgit).read().strip().removeprefix("gitdir:").strip()
            marker = f"{os.sep}worktrees{os.sep}"
            if marker in gitdir:
                # <main>/.git/worktrees/<name> -> <main>/.git -> <main>
                git_dir = gitdir[: gitdir.index(marker)]
                roots.append(os.path.realpath(os.path.dirname(git_dir)
                                              if os.path.basename(git_dir) == ".git" else git_dir))
        except OSError:
            pass
    return roots


def _is_within(child: str, parent: str) -> bool:
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    if child == parent:
        return True
    # rstrip so a parent of "/" compares against "/" rather than "//", which
    # would make every path read as outside it.
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def assert_path_allowed(path: str) -> str:
    """Resolve `path` and confirm it sits inside an allowed root.

    realpath first, so a symlink pointing out of an allowed root is judged by
    where it actually lands rather than by its own name.
    """
    if not os.path.isabs(path):
        raise ProvisioningError("path must be absolute")
    real = os.path.realpath(path)
    roots = allowed_roots()
    if not any(_is_within(real, root) for root in roots):
        raise PathNotAllowedError(
            f"{real} is outside the configured project roots ({', '.join(roots)}). "
            "Set AGENT_PROJECT_ROOTS to allow it.")
    if any(_is_within(real, own) for own in _agent_own_roots()):
        raise PathNotAllowedError(
            "refusing to onboard the agent's own repository -- a task merging into it "
            "would rewrite and restart the process running that task")
    if _is_within(real, sandbox_root()):
        raise PathNotAllowedError(
            f"{real} is inside the workspace root ({sandbox_root()}); onboard the LIVE "
            "repo, not an agent worktree")
    return real


def safe_relative(rel: str, base: str, *, must_exist: bool = True) -> str:
    """A repo-relative path that cannot escape the repo.

    These strings become filenames the review service copies OUT of the live
    checkout, so `../../root/.ssh/id_rsa` must never survive this function.
    """
    if os.path.isabs(rel):
        raise ProvisioningError(f"{rel!r} must be relative to the project root")
    base_real = os.path.realpath(base)
    if rel.strip("/") in ("", "."):
        return rel.strip("/")          # the project root itself: nothing to contain
    joined = os.path.normpath(os.path.join(base_real, rel))
    # The plain normpath + startswith shape, so a static analyser sees the
    # sanitiser before the filesystem call below (_is_within on the real
    # path covers symlinks on top of it).
    if not joined.startswith(base_real + os.sep):
        raise ProvisioningError(f"{rel!r} escapes the project directory")
    if not _is_within(os.path.realpath(joined), base):
        raise ProvisioningError(f"{rel!r} escapes the project directory")
    if must_exist and not os.path.exists(joined):
        raise ProvisioningError(f"{rel!r} does not exist in the project")
    return rel.strip("/")


@dataclass
class Candidate:
    """A proposed config item the operator accepts or rejects.

    `enabled` is our RECOMMENDATION, not a decision -- the wizard sends back
    what the operator actually chose. Anything with a `warning` defaults to
    disabled: the safe default for something we cannot verify is off.
    """

    value: str
    reason: str
    enabled: bool = True
    warning: str | None = None
    # The check this candidate stands for, when enabling it means running a
    # whole suite rather than one npm script. validate_choices offers THIS
    # dict under the candidate's name, so the client still only ever sends a
    # name -- it cannot author the command that the review service executes.
    check: dict | None = None


@dataclass
class DetectionReport:
    name: str
    live: str
    sandbox: str
    is_git_repo: bool = False
    package_manager: str | None = None       # npm | pnpm | yarn
    languages: list[str] = field(default_factory=list)
    node_modules_dirs: list[str] = field(default_factory=lambda: ["."])
    # Dependency trees the review checkout needs but git does not carry, for
    # stacks that keep them inside the project instead of in a user-wide
    # cache. The reviewer binds these READ-ONLY from the live checkout --
    # see dependencyDirs in services/commit-reviewer/worktree.js. Read-only
    # because the code about to run against them has not been reviewed yet.
    dependency_dirs: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    build_steps: list[dict] = field(default_factory=list)
    pm2_apps: list[Candidate] = field(default_factory=list)
    secret_files: list[Candidate] = field(default_factory=list)
    read_only_mounts: list[Candidate] = field(default_factory=list)
    risky_scripts: list[Candidate] = field(default_factory=list)
    db_env_file: str | None = None
    # Where this project's code also lives, if anywhere. Detected from the
    # checkout rather than inferred from how it was onboarded: a project the
    # operator typed a path for is just as likely to be on GitHub as one the
    # agent cloned, and treating those as different kinds of project is a
    # distinction only this codebase could see.
    git_remote: str | None = None
    github_slug: str | None = None
    # How this machine could restart the project after a merge. Empty for a
    # project this box does not run, which is the common case for a repository
    # somebody is only sending pull requests to.
    restart_commands: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _gitignored_entries(live: Path) -> list[str]:
    """Literal (non-glob, non-negated) .gitignore entries, from the root file
    only. Globs are skipped deliberately: expanding them invites walking a
    huge tree, and a literal path is exactly the shape that names a real
    secret file or fixture directory."""
    out: list[str] = []
    gi = live / ".gitignore"
    if not gi.is_file():
        return out
    try:
        lines = gi.read_text().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if any(ch in line for ch in "*?[]"):
            continue
        out.append(line.strip("/"))
    return out


def _detect_package_manager(live: Path) -> str | None:
    if (live / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (live / "yarn.lock").is_file():
        return "yarn"
    if (live / "package.json").is_file():
        return "npm"
    return None


def _detect_languages(live: Path) -> list[str]:
    langs = []
    if (live / "package.json").is_file():
        langs.append("node")
    if any((live / f).is_file() for f in ("pyproject.toml", "requirements.txt", "pytest.ini", "setup.py")):
        langs.append("python")
    if (live / "go.mod").is_file():
        langs.append("go")
    if (live / "Cargo.toml").is_file():
        langs.append("rust")
    if any((live / f).is_file() for f in ("Gemfile", "Rakefile", ".ruby-version", "Gemfile.lock")):
        langs.append("ruby")
    if (live / "mix.exs").is_file():
        langs.append("elixir")
    if any((live / f).is_file() for f in ("pom.xml", "build.gradle", "build.gradle.kts",
                                          "settings.gradle", "settings.gradle.kts")):
        langs.append("java")
    if (live / "composer.json").is_file() or any((live / f).is_file()
                                                 for f in ("phpunit.xml", "phpunit.xml.dist")):
        langs.append("php")
    if _dotnet_project(live):
        langs.append("dotnet")
    return langs


def _dotnet_project(live: Path) -> bool:
    """A solution or project file at the root, or one level down -- the two
    layouts .NET repos actually use (`App.sln` beside `src/App/App.csproj`).
    Not a full-tree walk: this runs on every detect, and a project file eight
    directories deep is a vendored sample, not the repo's own build."""
    patterns = ("*.sln", "*.csproj", "*.fsproj", "*/*.csproj", "*/*.fsproj",
                "src/*/*.csproj", "src/*/*.fsproj")
    return any(next(live.glob(pat), None) is not None for pat in patterns)


def _workspace_dirs(live: Path, pkg: dict) -> list[str]:
    """Directories that get their own node_modules in a monorepo. Derived
    from package.json `workspaces` / pnpm-workspace.yaml, expanded only one
    level (`apps/*` -> the real dirs), never by walking the whole tree."""
    dirs = ["."]
    patterns: list[str] = []
    ws = pkg.get("workspaces")
    if isinstance(ws, dict):
        patterns = list(ws.get("packages") or [])
    elif isinstance(ws, list):
        patterns = list(ws)
    pnpm_ws = live / "pnpm-workspace.yaml"
    if pnpm_ws.is_file():
        try:
            for line in pnpm_ws.read_text().splitlines():
                m = re.match(r"\s*-\s*['\"]?([^'\"]+)['\"]?\s*$", line)
                if m:
                    patterns.append(m.group(1))
        except OSError:
            pass
    for pat in patterns:
        pat = pat.strip()
        if pat.endswith("/*"):
            base = live / pat[:-2]
            if base.is_dir():
                for child in sorted(base.iterdir()):
                    if child.is_dir() and (child / "package.json").is_file():
                        dirs.append(str(child.relative_to(live)))
        elif pat and not any(c in pat for c in "*?"):
            if (live / pat / "package.json").is_file():
                dirs.append(pat)
    # ...and standalone packages that no `workspaces` entry names. A repo made
    # of backend/, frontend/ and admin/ -- each its own npm project with its
    # own lockfile, tied together by nothing -- is every bit as common as a
    # workspace monorepo, and the patterns above cannot see it at all. Without
    # this, onboarding recorded only ".", the review service installed only at
    # a root that had no dependencies, and every check in every app failed on
    # a missing tool. services/commit-reviewer/worktree.js detectNodeModulesDirs
    # applies the SAME rule at review time; tests/test_node_modules_detection.py
    # holds the two to the same fixtures so they cannot drift apart.
    dirs.extend(_standalone_package_dirs(live))
    # "." only when the root actually needs node_modules: it declares
    # dependencies, or it is a workspace root (where the store and hoisted
    # packages live even though it declares nothing itself).
    if not (_declares_dependencies(pkg) or _is_workspace_root(live, pkg)):
        dirs = [d for d in dirs if d != "."]
    # dedupe, keep order
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# Output directories, never source: a package.json in one of these is a copy,
# not a project. Mirrors NM_SKIP in the reviewer.
_NM_SKIP = frozenset({"node_modules", ".git", "dist", "build", "coverage", ".next",
                      ".nuxt", ".turbo", ".cache", "out", "vendor", "tmp"})
_NM_MAX_DEPTH = 2


def _declares_dependencies(pkg: dict) -> bool:
    return bool((pkg.get("dependencies") or {}) or (pkg.get("devDependencies") or {}))


def _is_workspace_root(live: Path, pkg: dict) -> bool:
    return (live / "pnpm-workspace.yaml").is_file() or bool(pkg.get("workspaces"))


def _standalone_package_dirs(live: Path) -> list[str]:
    """Every directory below the root, to depth 2, whose package.json declares
    dependencies. Shallow on purpose: a poll must not turn into a tree scan."""
    found: list[str] = []

    def walk(rel: Path, depth: int) -> None:
        if depth >= _NM_MAX_DEPTH:
            return
        try:
            children = sorted((live / rel).iterdir()) if rel.parts else sorted(live.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name.startswith(".") or child.name in _NM_SKIP:
                continue
            sub = child.relative_to(live)
            if _declares_dependencies(_read_json(child / "package.json")):
                found.append(str(sub))
            walk(sub, depth + 1)

    walk(Path(), 0)
    return found


def _script_is_risky(live: Path, script_body: str) -> str | None:
    """Return a warning when a script's own body, or a file it references,
    makes network calls. See this module's docstring for why this exists."""
    if _NETWORK_CALL.search(script_body):
        return "the script command itself references a network address"
    for ref in _SCRIPT_REF.findall(script_body):
        target = live / ref
        if not target.is_file():
            continue
        try:
            body = target.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        if _NETWORK_CALL.search(body):
            return f"{ref} makes network calls -- confirm it does not target a live service"
    return None


def _detect_node_checks(live: Path, pkg: dict, pm: str) -> tuple[list[dict], list[Candidate]]:
    """Checks come from the repo's OWN scripts, never a reconstructed list --
    same principle agent/tools/checks.py already follows."""
    scripts: dict = pkg.get("scripts") or {}
    checks: list[dict] = []
    risky: list[Candidate] = []

    # A repo that declares test:review has stated which suites are safe in a
    # detached checkout. Prefer it and don't second-guess the rest.
    has_review = "test:review" in scripts
    for name in ("typecheck", "lint", "build"):
        if name in scripts:
            checks.append({"name": name, "dir": ".", "cmd": pm, "args": ["run", name],
                           "timeoutMs": CHECK_TIMEOUT_MS_DEFAULT})
    if has_review:
        checks.append({"name": "test", "dir": ".", "cmd": pm, "args": ["run", "test:review"],
                       "timeoutMs": 900_000})
    elif "test" in scripts:
        checks.append({"name": "test", "dir": ".", "cmd": pm, "args": ["run", "test"],
                       "timeoutMs": 900_000})

    for sname, body in scripts.items():
        if not sname.startswith("test") or not isinstance(body, str):
            continue
        why = _script_is_risky(live, body)
        if why:
            risky.append(Candidate(
                value=sname, reason=why, enabled=False,
                warning=("Excluded from automated review. A test that calls a live service can "
                         "act on production (this deployment learned that from a suite that "
                         "POSTed real trade orders). Enable only after reading it."),
            ))
    if risky and not has_review:
        # The repo has no curated safe-suite list AND has suspicious scripts.
        checks = [c for c in checks if c["name"] != "test"]
    return checks, risky


def _scan_for_network_tests(live: Path, lang: str) -> list[tuple[str, str]]:
    """Test files whose own text makes network calls, as (path, idiom) pairs.

    npm repos name their suites, so _detect_node_checks can flag one script
    and keep the rest. Go, Rust, Ruby and pytest have no such names -- the
    command is the whole suite -- so the suspicion has to be raised by
    reading the test files themselves. Bounded by _SCAN_FILE_LIMIT and
    _SCAN_BYTES: this runs inside a wizard click, not a batch job.
    """
    globs = _TEST_FILE_GLOBS.get(lang, ())
    if not globs:
        return []
    hits: list[tuple[str, str]] = []
    scanned = 0
    for root, dirs, files in os.walk(live):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(files):
            if not any(fnmatch(fname, g) for g in globs):
                continue
            if scanned >= _SCAN_FILE_LIMIT:
                return hits
            scanned += 1
            path = Path(root) / fname
            try:
                body = path.read_text(errors="ignore")[:_SCAN_BYTES]
            except OSError:
                continue
            # Rust keeps unit tests beside the code, so *.rs matches far more
            # than tests. Only a file that actually declares a test counts.
            if lang == "rust" and "#[test]" not in body and "#[tokio::test]" not in body \
                    and "#[actix_rt::test]" not in body:
                continue
            m = _NETWORK_CALL.search(body)
            if not m:
                continue
            # A file that stubs or serves its own HTTP is not reaching
            # anything real, whatever the idiom looked like. Judged per file,
            # not per repo: one spec wired to WebMock says nothing about the
            # smoke test three directories over.
            if _NETWORK_STUBBED.search(body):
                continue
            hits.append((str(path.relative_to(live)), m.group(0)))
    return hits


def _suite_check(name: str, cmd: str, args: list[str], *, timeout: int) -> dict:
    return {"name": name, "dir": ".", "cmd": cmd, "args": list(args), "timeoutMs": timeout}


def _guard_suite(live: Path, lang: str, full: dict, review: dict | None) -> tuple[list[dict], list[Candidate]]:
    """Apply the network-calling-tests rule to a whole-suite test command.

    Same rule the npm path has followed since the npm-script incident, with
    the same three outcomes:

    * the repo declares a curated review suite -> trust it, and offer the
      unguarded full suite separately as a flagged candidate;
    * no curated suite and the tests call the network -> the suite is NOT a
      check, it is a flagged candidate the operator may enable after reading;
    * nothing suspicious -> the suite is a check.
    """
    hits = _scan_for_network_tests(live, lang)
    if not hits:
        return [review or full], []
    where = ", ".join(f"{p} ({idiom})" for p, idiom in hits[:3])
    more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
    warning = ("Excluded from automated review. A test that calls a live service can act on "
               "production (this deployment learned that from a suite that POSTed real trade "
               "orders). Enable only after reading it.")
    if review:
        # The repo has stated which suite is safe in a detached checkout, so
        # that one is the check. The full suite stays on offer under its own
        # name -- narrowing is the operator's to do, not ours to hide.
        full_named = dict(full, name=f"{full['name']}-all")
        return [review], [Candidate(
            value=full_named["name"],
            reason=f"the full suite makes network calls: {where}{more}",
            enabled=False, warning=warning, check=full_named,
        )]
    return [], [Candidate(
        value=full["name"],
        reason=f"test files make network calls: {where}{more}",
        enabled=False, warning=warning, check=full,
    )]


def _makefile_targets(live: Path) -> set[str]:
    """Target names from a root Makefile. Deliberately shallow: no includes,
    no variable expansion, no recursion into sub-makes. This is used to spot
    a declared `test-review`, not to understand the build."""
    out: set[str] = set()
    for fname in ("Makefile", "makefile", "GNUmakefile"):
        mk = live / fname
        if not mk.is_file():
            continue
        try:
            text = mk.read_text(errors="ignore")[:_SCAN_BYTES]
        except OSError:
            return out
        for line in text.splitlines():
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*:(?!=)", line)
            if m:
                out.add(m.group(1))
        break
    return out


def _make_review_target(targets: set[str]) -> str | None:
    """The cross-language stand-in for npm's `test:review`: a Makefile target
    a repo wrote specifically so an outside reviewer could run its safe
    suites."""
    for name in ("test-review", "test_review", "review-test"):
        if name in targets:
            return name
    return None


def _detect_go_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    checks = [_suite_check("vet", "go", ["vet", "./..."], timeout=CHECK_TIMEOUT_MS_DEFAULT),
              _suite_check("build", "go", ["build", "./..."], timeout=CHECK_TIMEOUT_MS_DEFAULT)]
    warnings: list[str] = []
    if any((live / f).is_file() for f in (".golangci.yml", ".golangci.yaml", ".golangci.toml", ".golangci.json")):
        # Only when the repo configures it: proposing golangci-lint for a repo
        # that never opted in means every review fails on lints its authors
        # never agreed to. _missing_toolchain says so if it is not installed.
        checks.append(_suite_check("lint", "golangci-lint", ["run"], timeout=CHECK_TIMEOUT_MS_DEFAULT))
    target = _make_review_target(_makefile_targets(live))
    review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT) if target else None
    full = _suite_check("test", "go", ["test", "./..."], timeout=TEST_TIMEOUT_MS_DEFAULT)
    tests, risky = _guard_suite(live, "go", full, review)
    return checks + tests, risky, warnings


def _cargo_alias(live: Path, name: str) -> bool:
    """Is `name` declared as a cargo alias? The Rust equivalent of a repo
    naming its own reviewer-safe suite."""
    for rel in (".cargo/config.toml", ".cargo/config"):
        cfg = live / rel
        if not cfg.is_file():
            continue
        try:
            text = cfg.read_text(errors="ignore")[:_SCAN_BYTES]
        except OSError:
            continue
        if re.search(r"^\s*['\"]?" + re.escape(name) + r"['\"]?\s*=", text, re.MULTILINE):
            return True
    return False


def _detect_rust_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    checks = [_suite_check("build", "cargo", ["build"], timeout=TEST_TIMEOUT_MS_DEFAULT)]
    if any((live / f).is_file() for f in ("rustfmt.toml", ".rustfmt.toml")):
        checks.insert(0, _suite_check("fmt", "cargo", ["fmt", "--", "--check"],
                                      timeout=CHECK_TIMEOUT_MS_DEFAULT))
    if any((live / f).is_file() for f in ("clippy.toml", ".clippy.toml")):
        # A repo that configures clippy has opted into it; without -D
        # warnings clippy exits 0 on every lint it reports, which would make
        # the check a decoration rather than a gate.
        checks.append(_suite_check("lint", "cargo", ["clippy", "--all-targets", "--", "-D", "warnings"],
                                   timeout=TEST_TIMEOUT_MS_DEFAULT))
    review = None
    if _cargo_alias(live, "test-review"):
        review = _suite_check("test", "cargo", ["test-review"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    else:
        target = _make_review_target(_makefile_targets(live))
        if target:
            review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT)
    full = _suite_check("test", "cargo", ["test"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    tests, risky = _guard_suite(live, "rust", full, review)
    return checks + tests, risky, []


def _uses_bundler(live: Path) -> bool:
    """Bundler manages this repo, so its commands run through `bundle exec`
    and its deploy needs `bundle install`. One predicate for both, because a
    repo that needs the prefix and never gets the install fails every check
    on a missing gem."""
    return (live / "Gemfile").is_file() or (live / "Gemfile.lock").is_file()


def _rake_declares_test_review(live: Path) -> bool:
    """Does the Rakefile declare a `test:review` task? Both spellings count:
    the flat `task "test:review"` and the idiomatic `namespace :test` with a
    `:review` task inside it, which is how most repos actually write it.

    The namespace form is read as a block, not as two searches of the whole
    file: `namespace :test` near the top and an unrelated `task :review`
    under `namespace :deploy` two hundred lines down is not a declaration of
    `test:review`, and treating it as one would hand the review gate a task
    that does something else entirely. The block ends at the first `end` no
    more indented than the `namespace` line, which is how rake files are
    written and formatted; anything more exact means parsing Ruby.
    """
    rf = live / "Rakefile"
    if not rf.is_file():
        return False
    try:
        text = rf.read_text(errors="ignore")[:_SCAN_BYTES]
    except OSError:
        return False
    if re.search(r"""task\s+['"]?test:review['"]?""", text):
        return True
    lines = text.splitlines()
    for i, line in enumerate(lines):
        opener = re.match(r"(\s*)namespace\s+:?['\"]?test['\"]?\b", line)
        if not opener:
            continue
        indent = len(opener.group(1))
        for inner in lines[i + 1:]:
            closer = re.match(r"(\s*)end\b", inner)
            if closer and len(closer.group(1)) <= indent:
                break
            if re.search(r"""task\s+:?['"]?review['"]?\b""", inner):
                return True
    return False


def _detect_ruby_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    runner = ["bundle", "exec"] if _uses_bundler(live) else []

    def cmd(*parts: str) -> dict:
        args = (runner + list(parts))
        return _suite_check("", args[0], args[1:], timeout=CHECK_TIMEOUT_MS_DEFAULT)

    checks: list[dict] = []
    if (live / ".rubocop.yml").is_file() or (live / ".rubocop.yaml").is_file():
        lint = cmd("rubocop")
        lint["name"] = "lint"
        checks.append(lint)

    review = None
    if _rake_declares_test_review(live):
        review = cmd("rake", "test:review")
    else:
        target = _make_review_target(_makefile_targets(live))
        if target:
            review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT)
    if review is not None:
        review["name"] = "test"
        review["timeoutMs"] = TEST_TIMEOUT_MS_DEFAULT

    if (live / "spec").is_dir():
        full = cmd("rspec")
    elif (live / "test").is_dir() and (live / "Rakefile").is_file():
        full = cmd("rake", "test")
    elif review is not None:
        full = dict(review)
    else:
        return checks, [], []
    full["name"] = "test"
    full["timeoutMs"] = TEST_TIMEOUT_MS_DEFAULT

    tests, risky = _guard_suite(live, "ruby", full, review)
    return checks + tests, risky, []


def _mix_alias(live: Path, name: str) -> bool:
    """Is `name` declared in mix.exs's `aliases`? Elixir's answer to an npm
    script: the repo names a task and says what it runs."""
    mix = live / "mix.exs"
    if not mix.is_file():
        return False
    try:
        text = mix.read_text(errors="ignore")[:_SCAN_BYTES]
    except OSError:
        return False
    # The value may be a list OR a single string: `"test.review": "test
    # --only safe"` is as valid as the list form, and requiring `[` meant a
    # repo that declared its review suite the short way was told it had none
    # -- so its real suite arrived flagged instead.
    text = _strip_line_comments(text, ("#",))
    return bool(re.search(r"""['\"]?""" + re.escape(name) + r"""['\"]?\s*:\s*(\[|['\"])""", text))


def _detect_elixir_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    checks: list[dict] = []
    if (live / ".formatter.exs").is_file():
        checks.append(_suite_check("format", "mix", ["format", "--check-formatted"],
                                   timeout=CHECK_TIMEOUT_MS_DEFAULT))
    if any((live / f).is_file() for f in (".credo.exs", "config/.credo.exs")):
        checks.append(_suite_check("lint", "mix", ["credo", "--strict"],
                                   timeout=CHECK_TIMEOUT_MS_DEFAULT))
    review = None
    if _mix_alias(live, "test.review"):
        review = _suite_check("test", "mix", ["test.review"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    else:
        target = _make_review_target(_makefile_targets(live))
        if target:
            review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT)
    full = _suite_check("test", "mix", ["test"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    tests, risky = _guard_suite(live, "elixir", full, review)
    return checks + tests, risky, []


def _gradle_cmd(live: Path) -> str:
    """The wrapper if the repo ships one. A repo with a gradlew is pinning a
    Gradle version on purpose, and running the host's `gradle` against it is
    how a build works for the author and not for the reviewer."""
    return "./gradlew" if (live / "gradlew").is_file() else "gradle"


def _detect_java_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    maven = (live / "pom.xml").is_file()
    gradle = any((live / f).is_file() for f in ("build.gradle", "build.gradle.kts",
                                                "settings.gradle", "settings.gradle.kts"))
    checks: list[dict] = []
    warnings: list[str] = []
    review = None
    target = _make_review_target(_makefile_targets(live))

    if maven and gradle:
        # A repo mid-migration. Picking one silently means the review gate
        # builds with a tool the project may have stopped using, so say which
        # was picked rather than leaving it to be discovered.
        warnings.append(
            "both pom.xml and a Gradle build file are present -- Maven was used for the "
            "proposed checks. If the Gradle build is the authoritative one, edit the "
            "project's checks after onboarding.")

    if maven:
        # -B (batch mode) because the reviewer has no terminal: without it
        # Maven writes progress bars into the captured output and nothing
        # else. Tests are what this gate is for, so `package` skips them and
        # `test` runs them, rather than one slow `verify` doing both.
        checks.append(_suite_check("build", "mvn", ["-B", "-DskipTests", "package"],
                                   timeout=TEST_TIMEOUT_MS_DEFAULT))
        full = _suite_check("test", "mvn", ["-B", "test"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    else:
        cmd = _gradle_cmd(live)
        checks.append(_suite_check("build", cmd, ["assemble"], timeout=TEST_TIMEOUT_MS_DEFAULT))
        if _gradle_declares(live, "testReview"):
            review = _suite_check("test", cmd, ["testReview"], timeout=TEST_TIMEOUT_MS_DEFAULT)
        full = _suite_check("test", cmd, ["test"], timeout=TEST_TIMEOUT_MS_DEFAULT)
        if not gradle:
            return [], [], []
    if review is None and target:
        review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT)

    tests, risky = _guard_suite(live, "java", full, review)
    return checks + tests, risky, warnings


def _strip_line_comments(text: str, markers: tuple[str, ...] = ("//", "#")) -> str:
    """Drop `// ...` / `# ...` tails. Crude -- it does not know about strings
    -- but the question here is only "does this file DECLARE a task", and a
    comment saying `// TODO: add testReview later` is the exact false
    positive worth removing."""
    out = []
    for line in text.splitlines():
        cut = len(line)
        for marker in markers:
            i = line.find(marker)
            if i != -1:
                cut = min(cut, i)
        out.append(line[:cut])
    return "\n".join(out)


def _gradle_declares(live: Path, task: str) -> bool:
    """Is `task` actually declared, in any of the spellings Gradle accepts?

    A bare word match was enough to make `// TODO: add testReview later`
    propose `gradle testReview` as this repo's curated review suite -- the
    same shape as the rake bug, on a stack that had just learned not to do
    it. So: comments are stripped first, and what remains has to look like a
    declaration rather than a mention.
    """
    t = re.escape(task)
    patterns = (
        rf"task\s+{t}\b",                                  # task testReview(type: Test)
        rf"tasks\.register(?:<[^>]+>)?\s*\(\s*['\"]{t}['\"]",  # tasks.register("testReview")
        rf"tasks\.create\s*\(\s*['\"]{t}['\"]",            # tasks.create("testReview")
        rf"val\s+{t}\s+by\s+tasks",                        # val testReview by tasks.registering
        rf"^\s*{t}\s*\{{",                                 # testReview { ... } on its own line
    )
    for name in ("build.gradle", "build.gradle.kts"):
        f = live / name
        if not f.is_file():
            continue
        try:
            body = _strip_line_comments(f.read_text(errors="ignore")[:_SCAN_BYTES], ("//",))
        except OSError:
            continue
        if any(re.search(p, body, re.MULTILINE) for p in patterns):
            return True
    return False


def _composer_scripts(live: Path) -> dict:
    data = _read_json(live / "composer.json")
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _detect_php_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    scripts = _composer_scripts(live)
    checks: list[dict] = []

    # Static analysis and style, each only when the repo configures it: a
    # phpstan run a repo never opted into fails every review on findings its
    # authors never agreed to.
    if any((live / f).is_file() for f in ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")):
        checks.append(_suite_check("analyse", "vendor/bin/phpstan", ["analyse", "--no-progress"],
                                   timeout=TEST_TIMEOUT_MS_DEFAULT))
    if any((live / f).is_file() for f in ("phpcs.xml", "phpcs.xml.dist", ".phpcs.xml")):
        checks.append(_suite_check("lint", "vendor/bin/phpcs", ["-q"],
                                   timeout=CHECK_TIMEOUT_MS_DEFAULT))

    review = None
    if "test:review" in scripts:
        # run-script, not the bare `composer test:review` shorthand: the
        # shorthand is only reached for names that are not already composer
        # subcommands, so a script called `install` or `check` would run
        # composer's own command instead of the repo's.
        review = _suite_check("test", "composer", ["run-script", "test:review"],
                              timeout=TEST_TIMEOUT_MS_DEFAULT)
    else:
        target = _make_review_target(_makefile_targets(live))
        if target:
            review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT)

    # The repo's own script first, the framework's runner second -- same rule
    # the npm path follows.
    if "test" in scripts:
        full = _suite_check("test", "composer", ["run-script", "test"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    elif any((live / f).is_file() for f in ("phpunit.xml", "phpunit.xml.dist")):
        full = _suite_check("test", "vendor/bin/phpunit", [], timeout=TEST_TIMEOUT_MS_DEFAULT)
    elif review is not None:
        full = dict(review)
    else:
        return checks, [], []

    tests, risky = _guard_suite(live, "php", full, review)
    return checks + tests, risky, []


def _detect_dotnet_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    checks = [_suite_check("build", "dotnet", ["build", "--nologo"], timeout=TEST_TIMEOUT_MS_DEFAULT)]
    if (live / ".editorconfig").is_file():
        # `dotnet format` reads .editorconfig and nothing else; without one it
        # would enforce defaults the repo never chose.
        checks.append(_suite_check("format", "dotnet", ["format", "--verify-no-changes"],
                                   timeout=TEST_TIMEOUT_MS_DEFAULT))
    target = _make_review_target(_makefile_targets(live))
    review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT) if target else None
    full = _suite_check("test", "dotnet", ["test", "--nologo"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    tests, risky = _guard_suite(live, "dotnet", full, review)
    return checks + tests, risky, []


def _detect_make_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    """Last resort for a repo with no manifest this module recognizes. A
    Makefile is the one convention every stack shares, so a declared `test`
    or `lint` target is better evidence than guessing at the language."""
    targets = _makefile_targets(live)
    checks: list[dict] = []
    for name in ("lint", "build"):
        if name in targets:
            checks.append(_suite_check(name, "make", [name], timeout=CHECK_TIMEOUT_MS_DEFAULT))
    review_target = _make_review_target(targets)
    review = (_suite_check("test", "make", [review_target], timeout=TEST_TIMEOUT_MS_DEFAULT)
              if review_target else None)
    if "test" not in targets and review is None:
        return checks, [], []
    full = _suite_check("test", "make", ["test"], timeout=TEST_TIMEOUT_MS_DEFAULT) if "test" in targets \
        else dict(review)
    # No language is known here, so there is no test-file glob to scan; the
    # Makefile target is taken at its word. Said plainly in the warning.
    if review is not None:
        return checks + [review], [], []
    return checks + [full], [], [
        "checks come from Makefile targets, which cannot be read for network calls the way "
        "test files can -- confirm `make test` is safe to run against a detached checkout"]


def _detect_python_checks(live: Path) -> tuple[list[dict], list[Candidate], list[str]]:
    checks: list[dict] = []
    if (live / ".ruff.toml").is_file() or (live / "ruff.toml").is_file():
        checks.append(_suite_check("lint", "python", ["-m", "ruff", "check", "."],
                                   timeout=CHECK_TIMEOUT_MS_DEFAULT))
    if not ((live / "pytest.ini").is_file() or (live / "pyproject.toml").is_file()
            or (live / "tests").is_dir()):
        return checks, [], []
    target = _make_review_target(_makefile_targets(live))
    review = _suite_check("test", "make", [target], timeout=TEST_TIMEOUT_MS_DEFAULT) if target else None
    full = _suite_check("test", "python", ["-m", "pytest", "-q"], timeout=TEST_TIMEOUT_MS_DEFAULT)
    tests, risky = _guard_suite(live, "python", full, review)
    return checks + tests, risky, []


def _stack_image_keys() -> frozenset[str]:
    """Stacks that have an image of their own, from docker/stack-images.json.

    Read rather than duplicated: the same list is used by
    services/commit-reviewer/sandbox.js to pick the image and by
    scripts/verify_stack_checks.py to prove each one can run that stack's
    checks. Three copies of a list is how the review dashboard ended up
    labelling live models from an inventory deleted months earlier.
    """
    try:
        root = Path(__file__).resolve().parent.parent
        raw = (root / "docker" / "stack-images.json").read_text()
        return frozenset((json.loads(raw).get("stacks") or {}).keys())
    except (OSError, ValueError):
        # An installation missing the map stamps nothing, and every check runs
        # in the default image -- which is what happened before stacks were
        # selectable at all.
        return frozenset()


_STACK_IMAGE_KEYS = _stack_image_keys()


def _add_checks(report: DetectionReport, checks: list[dict], risky: list[Candidate],
                *, prefix: str) -> None:
    """Merge one stack's findings into the report, renaming on collision.

    The first stack to contribute keeps the plain names a single-language
    repo expects (`test`, `lint`, `build`). Every later stack is prefixed
    wholesale -- `go-test`, `go-vet`, `go-build` -- rather than only where a
    name would collide, because a mixed list (`test` from Node next to a bare
    `build` that happens to be Go's) reads like one stack's checks with a
    hole in it. Any flagged candidate that stands for a renamed check is
    renamed with it: validate_choices looks the candidate up by name, so the
    two must never drift.
    """
    # Which container each of these checks runs in. Stamped HERE, at
    # detection, because this is the only moment the stack that produced a
    # check is known: by the time projects.json is written, `go vet` is just
    # a command. Per CHECK rather than per project -- a Go backend with a
    # React frontend is an ordinary repository, and its two sets of checks
    # belong in two different images (docker/stack-images.json).
    #
    # The Node/Python stacks are deliberately left unstamped: they run in the
    # agent's own sandbox image, which is the default, and an explicit label
    # there would be a second name for the same thing.
    if prefix in _STACK_IMAGE_KEYS:
        for c in checks:
            c.setdefault("stack", prefix)

    taken = {c["name"] for c in report.checks} | {c.value for c in report.risky_scripts}
    later = bool(taken)
    rename: dict[str, str] = {}
    for check in checks:
        name = f"{prefix}-{check['name']}" if later else check["name"]
        if name in taken:
            name = f"{prefix}-{check['name']}"
        rename[check["name"]] = name
        check["name"] = name
        taken.add(name)
        report.checks.append(check)
    for cand in risky:
        name = rename.get(cand.value) or (f"{prefix}-{cand.value}" if later else cand.value)
        if name in taken:
            name = f"{prefix}-{cand.value}"
        cand.value = name
        if cand.check is not None:
            cand.check = dict(cand.check, name=name)
        taken.add(name)
        report.risky_scripts.append(cand)


# Where each stack keeps its installed dependencies, when it keeps them in
# the project at all. Go, Rust, Maven, Gradle and NuGet all use a user-wide
# cache that a worktree inherits for free, so they are deliberately absent.
# Where each stack keeps its installed dependencies, when it keeps them in
# the project at all. Go, Rust, Maven, Gradle and NuGet all use a user-wide
# cache that a worktree inherits for free, so they are deliberately absent.
#
# `_build` is NOT here, though Elixir keeps it beside deps: it is compilation
# OUTPUT, not dependencies, and `mix test` writes to it. The reviewer binds
# these read-only, so mounting _build would break every Elixir review -- and
# mounting it writable would let an unreviewed branch recompile over
# production's build. The worktree compiles its own instead.
_DEPENDENCY_DIRS = {
    "php": ("vendor",),
    "elixir": ("deps",),
    # Only when the project bundles into itself (`bundle install --path
    # vendor/bundle`); the default installs gems user-wide, which a worktree
    # already inherits.
    "ruby": ("vendor/bundle",),
}


def _dependency_dirs_for(live: Path, lang: str) -> list[str]:
    return [d for d in _DEPENDENCY_DIRS.get(lang, ()) if (live / d).is_dir()]


def _build_steps_for(live: Path, lang: str) -> list[dict]:
    """What a deploy runs in the LIVE checkout before pm2 restarts it. Only
    the compile/install step every project of that stack needs -- anything
    beyond that is a guess, and a wrong guess here runs on merge."""
    if lang == "go":
        return [{"dir": ".", "cmd": "go", "args": ["build", "./..."]}]
    if lang == "rust":
        return [{"dir": ".", "cmd": "cargo", "args": ["build", "--release"]}]
    if lang == "elixir":
        return [{"dir": ".", "cmd": "mix", "args": ["deps.get"]},
                {"dir": ".", "cmd": "mix", "args": ["compile"]}]
    if lang == "java":
        if (live / "pom.xml").is_file():
            return [{"dir": ".", "cmd": "mvn", "args": ["-B", "-DskipTests", "package"]}]
        return [{"dir": ".", "cmd": _gradle_cmd(live), "args": ["assemble"]}]
    if lang == "php" and (live / "composer.json").is_file():
        # --no-dev is deliberately absent: the review checkout runs the test
        # suite, and phpunit lives in require-dev.
        return [{"dir": ".", "cmd": "composer",
                 "args": ["install", "--no-interaction", "--no-progress"]}]
    if lang == "dotnet":
        return [{"dir": ".", "cmd": "dotnet", "args": ["build", "--nologo", "-c", "Release"]}]
    if lang == "ruby" and _uses_bundler(live):
        # The same predicate the checks use. Keying the deploy step on
        # Gemfile.lock alone left a Gemfile-only repo -- normal for a library
        # -- running `bundle exec rspec` in review against a bundle the
        # deploy never installed.
        return [{"dir": ".", "cmd": "bundle", "args": ["install"]}]
    return []


# The review service runs checks on this host, not in the task sandbox, so a
# missing toolchain is not a detection problem -- it is a check that will
# fail on every single review until someone installs it. Better said at
# onboarding than discovered on the first merge.
# Commands that a build step creates rather than a package manager installs
# system-wide. `vendor/bin/phpstan` does not exist until `composer install`
# has run, and node_modules/.bin the same -- warning that they are "not on
# PATH" describes a state the deploy is supposed to be in before the checks
# run, and sends the operator looking for an install that was never needed.
_INSTALLED_BY_BUILD = ("vendor/bin/", "node_modules/.bin/", "bin/")


def _missing_toolchain(live: Path, checks: list[dict]) -> list[str]:
    """Commands the review service will not be able to run.

    PATH is the right question only for a command that IS on PATH. A repo's
    own wrapper (`./gradlew`) lives in the repo, and looking it up on PATH
    warned that a project shipping a wrapper had no Gradle -- while the whole
    point of a wrapper is that it needs none.
    """
    missing: list[str] = []
    for check in checks:
        cmd = check["cmd"]
        # python and make are the interpreter this process already runs under
        # and a coreutils-era binary; neither is worth a warning.
        if cmd in ("python", "make") or cmd in missing:
            continue
        if cmd.startswith(_INSTALLED_BY_BUILD) or "/" in cmd:
            # Project-relative: it is present if it is in the repo, and its
            # absence is only worth mentioning when nothing will create it.
            if (live / cmd).exists() or cmd.startswith(_INSTALLED_BY_BUILD):
                continue
            missing.append(cmd)
            continue
        if not shutil.which(cmd):
            missing.append(cmd)
    return missing


def _detect_restart_commands(live: Path) -> list[Candidate]:
    """How this machine could restart this project after a merge.

    Only ever finds something for a project this box actually RUNS. A
    repository somebody is only working on -- cloned to send pull requests --
    has no compose file it owns, no unit pointing into it and no process
    running from it, so nothing is proposed and a merge stays a merge. That is
    the gate: not "is it local" (every project is), but "does this machine run
    it".

    Each candidate carries the exact command, because the wizard may only
    confirm commands this server proposed -- see validate_choices. An operator
    who needs something else edits projects.json, which is the same boundary
    every other executed command sits behind.
    """
    out: list[Candidate] = []

    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (live / name).is_file():
            out.append(Candidate(
                value="docker compose up -d --build",
                reason=f"{name} in the project root",
                check={"kind": "compose", "cmd": "docker",
                       "args": ["compose", "up", "-d", "--build"], "dir": "."},
            ))
            break

    # systemd rarely records a WorkingDirectory, so ExecStart's own path is
    # the second place to look: a unit running a binary from inside the
    # project is running this project.
    systemctl = shutil.which("systemctl")
    if systemctl:
        real_live = os.path.realpath(live)
        try:
            listing = subprocess.run(
                [systemctl, "list-units", "--type=service", "--no-legend", "--plain", "--all"],
                capture_output=True, text=True, timeout=20)
            units = [ln.split()[0] for ln in (listing.stdout or "").splitlines() if ln.split()]
        except (subprocess.SubprocessError, OSError):
            units = []
        for unit in units[:400]:
            try:
                shown = subprocess.run(
                    [systemctl, "show", unit, "-p", "WorkingDirectory", "-p", "ExecStart"],
                    capture_output=True, text=True, timeout=10).stdout or ""
            except (subprocess.SubprocessError, OSError):
                continue
            if not any(real_live in line for line in shown.splitlines()):
                continue
            out.append(Candidate(
                value=f"systemctl restart {unit}",
                reason=f"{unit} runs from inside this project",
                check={"kind": "systemd", "cmd": "systemctl",
                       "args": ["restart", unit], "dir": "."},
            ))
    return out


def _detect_pm2_apps(live: Path) -> list[Candidate]:
    """pm2 apps whose working directory is inside this project. Proposed, not
    assumed: restarting the wrong app on merge takes down an unrelated
    service."""
    out: list[Candidate] = []
    pm2 = shutil.which("pm2")
    if not pm2:
        return out
    try:
        res = subprocess.run([pm2, "jlist"], capture_output=True, text=True, timeout=20)
        apps = json.loads(res.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return out
    real_live = os.path.realpath(live)
    for app in apps if isinstance(apps, list) else []:
        env = app.get("pm2_env") or {}
        for key in ("pm_cwd", "pm_exec_path", "cwd"):
            p = env.get(key) or app.get(key)
            if not p:
                continue
            if os.path.realpath(str(p)).startswith(real_live + os.sep) or os.path.realpath(str(p)) == real_live:
                out.append(Candidate(value=app.get("name", "?"),
                                     reason=f"pm2 app running from {p}"))
                break
    return out


def _detect_secrets_and_mounts(live: Path) -> tuple[list[Candidate], list[Candidate], str | None]:
    secrets: list[Candidate] = []
    mounts: list[Candidate] = []
    db_env: str | None = None
    for entry in _gitignored_entries(live):
        target = live / entry
        if not target.exists():
            continue
        if target.is_file():
            if _SECRET_NAME_HINTS.search(entry):
                secrets.append(Candidate(
                    value=entry,
                    reason="gitignored file that looks like credentials; the review checkout needs it",
                ))
                if db_env is None:
                    try:
                        if "DATABASE_URL" in target.read_text(errors="ignore")[:20_000]:
                            db_env = entry
                    except OSError:
                        pass
        elif target.is_dir() and target.name not in _SKIP_DIRS:
            try:
                has_content = any(True for _ in target.iterdir())
            except OSError:
                has_content = False
            if has_content:
                mounts.append(Candidate(
                    value=entry, enabled=False,
                    reason="gitignored directory with content -- mount read-only if tests need these fixtures",
                ))
    return secrets, mounts, db_env


def detect_project(live_path: str, sandbox_root: str | None = None,
                   existing_names: list[str] | None = None) -> DetectionReport:
    """Inspect a directory and propose a project configuration. Read-only --
    nothing is created or modified here."""
    live = Path(assert_path_allowed(live_path))
    name = live.name

    report = DetectionReport(name=name, live=str(live),
                             sandbox=str(Path(sandbox_root or globals()["sandbox_root"]()) / name))

    if not live.is_dir():
        report.blockers.append(f"{live} is not a directory")
        return report
    if not os.access(live, os.R_OK):
        report.blockers.append(f"{live} is not readable by the agent")
        return report
    if (live / ".git").exists():
        report.is_git_repo = True
        # --local, not `remote get-url`: the latter applies url.*.insteadOf
        # from the global config, which on a box with a token helper rewrites
        # an ssh remote into an https one carrying the secret. See
        # deploy_keys._configured_origin, which learned this the same way.
        ok, remote = _run_git(["config", "--local", "--get", "remote.origin.url"], cwd=str(live))
        if ok and remote.strip():
            report.git_remote = remote.strip()
            from agent.tools import github_tools  # noqa: PLC0415

            report.github_slug = github_tools.repo_slug_from_remote(report.git_remote)
    else:
        report.blockers.append(
            f"{live} is not a git repository -- the agent works on a per-task branch in a "
            "worktree of the live repo, so git is required")
    for existing in existing_names or []:
        if existing == name:
            report.blockers.append(f"a project named {name!r} is already configured")

    report.languages = _detect_languages(live)
    if not report.languages:
        report.warnings.append(
            "no recognized project manifest (package.json, pyproject.toml, go.mod, Cargo.toml, "
            "Gemfile, mix.exs, pom.xml, build.gradle, composer.json, *.csproj) -- falling back "
            "to Makefile targets if there are any")

    pkg = _read_json(live / "package.json")
    pm = _detect_package_manager(live)
    report.package_manager = pm

    if not pm:
        # No package manager, no node_modules. Writing ["."] anyway told the
        # review service to look for a directory that a Go or Ruby project
        # will never have.
        report.node_modules_dirs = []
    if pm:
        report.node_modules_dirs = _workspace_dirs(live, pkg)
        checks, risky = _detect_node_checks(live, pkg, pm)
        _add_checks(report, checks, risky, prefix="node")
        install = {"pnpm": ["install", "--frozen-lockfile"],
                   "yarn": ["install", "--frozen-lockfile"],
                   "npm": ["install", "--no-audit", "--no-fund"]}[pm]
        report.build_steps = [{"dir": ".", "cmd": pm, "args": install}]
        if "build" in (pkg.get("scripts") or {}):
            report.build_steps.append({"dir": ".", "cmd": pm, "args": ["run", "build"]})
        for d in report.node_modules_dirs[1:]:
            sub = _read_json(live / d / "package.json")
            if "build" in (sub.get("scripts") or {}):
                report.build_steps.append({"dir": d, "cmd": pm, "args": ["run", "build"]})

    # Each stack contributes its own checks. A polyglot repo (a Go service
    # with a Node dashboard, say) gets both, and the second stack onward is
    # name-prefixed so `test` from one never silently replaces `test` from
    # the other -- check names are the reviewer's keys, and a collision would
    # drop a suite without saying so.
    for lang, detect in (("python", _detect_python_checks),
                         ("go", _detect_go_checks),
                         ("rust", _detect_rust_checks),
                         ("ruby", _detect_ruby_checks),
                         ("elixir", _detect_elixir_checks),
                         ("java", _detect_java_checks),
                         ("php", _detect_php_checks),
                         ("dotnet", _detect_dotnet_checks)):
        if lang in report.languages:
            checks, risky, warns = detect(live)
            _add_checks(report, checks, risky, prefix=lang)
            report.warnings.extend(warns)
            report.build_steps.extend(_build_steps_for(live, lang))
            report.dependency_dirs.extend(_dependency_dirs_for(live, lang))
            missing = _missing_toolchain(live, checks)
            if missing:
                report.warnings.append(
                    f"{', '.join(missing)} not on PATH -- the {lang} checks that use "
                    f"{'them' if len(missing) > 1 else 'it'} will fail until installed for the "
                    "review service, which runs checks on this host rather than in the sandbox")

    if not report.checks:
        checks, risky, warns = _detect_make_checks(live)
        _add_checks(report, checks, risky, prefix="make")
        report.warnings.extend(warns)

    if not report.checks:
        report.warnings.append(
            "no automated checks detected -- the review gate will have nothing to run, so "
            "every change ships on human review alone")

    report.pm2_apps = _detect_pm2_apps(live)
    report.restart_commands = _detect_restart_commands(live)
    if not report.pm2_apps:
        report.warnings.append(
            "no pm2 app found serving this path -- merges will build but not restart anything")

    secrets, mounts, db_env = _detect_secrets_and_mounts(live)
    report.secret_files = secrets
    report.read_only_mounts = mounts
    report.db_env_file = db_env
    if report.risky_scripts:
        report.warnings.append(
            f"{len(report.risky_scripts)} test command(s) make network calls and were left "
            "disabled -- review each one before enabling")
    return report


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str, timeout: int = 120,
             extra_env: dict | None = None) -> tuple[bool, str]:
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # Not str(e): that is the whole argv.
        return False, f"git timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def validate_choices(report: DetectionReport, choices: dict) -> dict:
    """Confirm the operator's answers are a SUBSET of what detection offered.

    The wizard is an approval step, not an authoring step. Without this, the
    `checks` and `build` arrays -- which the review and deploy services
    execute verbatim -- would be arbitrary commands supplied over HTTP, and
    `secretFiles` would be arbitrary paths the reviewer copies out of the
    live checkout. So every submitted item must match something this server
    itself proposed, and paths are re-checked for containment rather than
    trusted because they appeared in a report.

    Narrowing is always allowed; adding never is.
    """
    live = report.live
    offered_checks = {c["name"]: c for c in report.checks}
    # A flagged item becomes selectable only under its own detected name, and
    # only as the command this server proposed for it: an npm script runs
    # through the detected package manager, and a whole-suite candidate (Go,
    # Rust, Ruby, pytest -- ecosystems with no per-suite script names) carries
    # the exact check it stands for.
    for r in report.risky_scripts:
        offered_checks.setdefault(r.value, r.check or {
            "name": r.value, "dir": ".",
            "cmd": report.package_manager or "npm", "args": ["run", r.value],
            "timeoutMs": TEST_TIMEOUT_MS_DEFAULT,
        })
    offered_builds = {json.dumps(b, sort_keys=True) for b in report.build_steps}
    offered_secrets = {c.value for c in report.secret_files}
    offered_mounts = {c.value for c in report.read_only_mounts}
    offered_apps = {c.value for c in report.pm2_apps}
    # Keyed by the command's own text, so a confirmed restart is the command
    # THIS server proposed rather than one that arrived over HTTP. Same rule
    # as checks and build steps, and for the same reason: these execute.
    offered_restarts = {c.value: c.check for c in report.restart_commands if c.check}
    offered_nm = set(report.node_modules_dirs)

    clean: dict = {}

    # How the project ships. Not a detected value and not a command anything
    # executes, so it is validated by shape rather than against an offer --
    # but it is still allow-listed, because "anything the client sent" is how
    # a projects.json entry grows a key nobody audited.
    if choices.get("ship") in ("push", "pr"):
        clean["ship"] = choices["ship"]
    if choices.get("cloned_from_github"):
        clean["cloned_from_github"] = True

    checks = []
    for c in choices.get("checks") or []:
        name = (c or {}).get("name")
        if name not in offered_checks:
            raise ProvisioningError(
                f"check {name!r} was not proposed for this project -- the wizard can only "
                "confirm detected commands, not introduce new ones")
        checks.append(offered_checks[name])   # OUR version, never the client's cmd/args
    clean["checks"] = checks

    builds = []
    for b in choices.get("build_steps") or []:
        if json.dumps(b, sort_keys=True) not in offered_builds:
            raise ProvisioningError("build step was not proposed for this project")
        builds.append(b)
    clean["build_steps"] = builds

    def _subset(key: str, offered: set, *, relative_to: str | None = None) -> list[str]:
        out = []
        for v in choices.get(key) or []:
            if v not in offered:
                raise ProvisioningError(f"{v!r} was not proposed as {key.replace('_', ' ')}")
            out.append(safe_relative(v, relative_to) if relative_to else v)
        return out

    clean["secret_files"] = _subset("secret_files", offered_secrets, relative_to=live)
    clean["read_only_mounts"] = _subset("read_only_mounts", offered_mounts, relative_to=live)
    clean["pm2_apps"] = _subset("pm2_apps", offered_apps)
    restarts = []
    for value in choices.get("restart_commands") or []:
        value = str(value)
        if value not in offered_restarts:
            raise ProvisioningError(
                f"restart {value!r} was not proposed for this project -- the wizard can only "
                "confirm detected commands, not introduce new ones")
        restarts.append(offered_restarts[value])
    if restarts:
        clean["restart_commands"] = restarts
    clean["node_modules_dirs"] = _subset("node_modules_dirs", offered_nm, relative_to=live)
    clean["dependency_dirs"] = _subset("dependency_dirs", set(report.dependency_dirs),
                                       relative_to=live)

    db = choices.get("db_env_file")
    if db:
        if db != report.db_env_file:
            raise ProvisioningError("db_env_file was not the detected one")
        clean["db_env_file"] = safe_relative(db, live)
    return clean


def config_from_choices(live: str, sandbox: str, choices: dict) -> dict:
    """Build the projects.json entry from what the OPERATOR confirmed.

    `choices` is the wizard's payload, not the detection report -- the two
    are deliberately different objects so an operator's rejection can never
    be silently overwritten by a re-detection.
    """
    entry: dict = {"live": live, "sandbox": sandbox}

    # How this project ships, decided by how it ARRIVED rather than by a
    # global default.
    #
    # A path the operator already had checked out is one they own and deploy:
    # merging into its base branch is the whole point, and that stays the
    # default. A repository the agent cloned because somebody pasted a URL is
    # a different thing -- nobody asked it to own that repository, and its
    # base branch may well have other people's work on it. Pushing straight to
    # it would be the surprising answer, so a clone opens a pull request and a
    # person merges.
    #
    # Either can be changed afterwards in projects.json; this is the default,
    # not a rule.
    if choices.get("ship") in ("push", "pr"):
        entry["ship"] = choices["ship"]
    elif choices.get("cloned_from_github"):
        entry["ship"] = "pr"

    if choices.get("db_env_file"):
        entry["db_env_file"] = choices["db_env_file"]

    review: dict = {}
    if choices.get("secret_files"):
        review["secretFiles"] = list(choices["secret_files"])
    if choices.get("read_only_mounts"):
        review["readOnlyMounts"] = list(choices["read_only_mounts"])
    if choices.get("node_modules_dirs"):
        review["nodeModulesDirs"] = list(choices["node_modules_dirs"])
    if choices.get("dependency_dirs"):
        review["dependencyDirs"] = list(choices["dependency_dirs"])
    if choices.get("checks"):
        review["checks"] = list(choices["checks"])
    if review:
        entry["review"] = review

    deploy: dict = {}
    if choices.get("restart_commands"):
        # After the build steps, before nothing: this is what "deploy" means
        # for a project this machine runs but does not run under pm2.
        deploy["restart"] = list(choices["restart_commands"])
    if choices.get("pm2_apps"):
        deploy["pm2Apps"] = list(choices["pm2_apps"])
    if choices.get("build_steps"):
        deploy["build"] = list(choices["build_steps"])
    if deploy:
        entry["deploy"] = deploy
    return entry


def _rewrite_projects(projects_path: Path, mutate) -> dict:
    """Read projects.json, apply `mutate(data)`, write it back atomically.

    Atomic because this file is read at import by the API, the reviewer and
    the deploy service -- a half-written file breaks all three at once. The
    read-modify-write runs under a lock beside the file, so two requests
    (an add and a move, say) cannot each write the other's change away; the
    temp file is unique rather than a fixed `.tmp`, so they cannot share
    one either (2026-09-29).
    """
    import fcntl  # noqa: PLC0415 -- posix only, like the rest of the file locking
    import tempfile  # noqa: PLC0415

    projects_path.parent.mkdir(parents=True, exist_ok=True)
    with open(projects_path.with_name(projects_path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(projects_path.read_text()) if projects_path.exists() else {"projects": {}}
        data.setdefault("projects", {})
        result = mutate(data)
        fd, tmp = tempfile.mkstemp(dir=str(projects_path.parent), prefix=".projects.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, projects_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return result


def move_project_entry(projects_path: Path, name: str, live: str) -> dict:
    """Point an existing project at another checkout, atomically. The
    sandbox follows when it was the live path (a project that works in its
    own tree); a separate workspace path is kept -- the move route rebuilds
    that worktree against the new checkout. Returns the new entry.
    2026-09-28: the projects folder changed under the desktop app and the
    only way to follow it was to remove and re-add the project."""
    def mutate(data: dict) -> dict:
        projects = data["projects"]
        if name not in projects:
            raise ProvisioningError(f"no project named {name!r} in {projects_path}")
        entry = dict(projects[name])
        old = entry.get("live")
        entry["live"] = live
        if not entry.get("sandbox") or entry.get("sandbox") == old:
            entry["sandbox"] = live
        projects[name] = entry
        return entry

    return _rewrite_projects(projects_path, mutate)


def write_project_entry(projects_path: Path, name: str, entry: dict) -> None:
    """Atomic add of one project to projects.json (see _rewrite_projects)."""
    def mutate(data: dict) -> None:
        if name in data["projects"]:
            raise ProvisioningError(f"{name!r} is already in projects.json")
        data["projects"][name] = entry

    _rewrite_projects(projects_path, mutate)


def create_worktree(live: str, sandbox: str, branch: str = "agent-base",
                    *, must_be_new: bool = False) -> tuple[bool, str]:
    """Create the agent's workspace as a git worktree of the live repo.

    A worktree, not a clone: tasks commit to a per-task branch that is a
    plain local ref in the live repo, which is what lets the review service
    read the branch directly with no remote in between.

    `must_be_new` is for the create-a-project path. Re-onboarding an existing
    repo may legitimately find its own workspace already there, so the wizard
    adopts it -- but a project being created for the first time cannot have
    one. If a directory is sitting at that path it belongs to something else
    with the same name (a deleted project's leftovers, most likely), and
    adopting it would point every task for the new project at a worktree of a
    DIFFERENT repository.
    """
    if os.path.exists(sandbox):
        if must_be_new:
            return False, (f"{sandbox} already exists -- a new project cannot reuse a workspace. "
                           "Remove the leftover directory, or pick another name.")
        if os.path.isdir(os.path.join(sandbox, ".git")) or os.path.isfile(os.path.join(sandbox, ".git")):
            return True, f"worktree already exists at {sandbox}"
        return False, f"{sandbox} exists and is not a git worktree -- refusing to overwrite"
    os.makedirs(os.path.dirname(sandbox), exist_ok=True)
    ok, out = _run_git(["worktree", "add", sandbox, "-b", branch], cwd=live)
    if not ok and "already exists" in out:
        # Branch left behind by a previous onboarding of the same repo.
        ok, out = _run_git(["worktree", "add", sandbox, branch], cwd=live)
    return ok, out


# --------------------------------------------------------------------------
# creating a project from nothing
#
# The onboarding wizard above takes a repo that already exists. "New project"
# starts one: an empty directory that becomes a git repo with one commit,
# then goes through the SAME detect -> choices -> provision path as any
# other directory, with the recommended answers instead of a wizard.
# --------------------------------------------------------------------------

# One path component, and a valid git worktree branch prefix / key file name
# (agent/deploy_keys._SAFE_NAME accepts the same shape). Leading dot excluded
# so a project can never be a hidden directory or `..`; 64 chars because the
# name is also a projects.json key and a worktree directory name.
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Enough to keep the first `git status` clean in the stacks the wizard knows
# how to detect. Deliberately short: a project-specific .gitignore is the
# first task's job, not this template's.
_NEW_REPO_GITIGNORE = ".env\nnode_modules/\n.venv/\n__pycache__/\ndist/\n"


def validate_project_name(name: str, existing_names: list[str] | None = None) -> str:
    """The name is also a directory basename, a projects.json key and a
    deploy-key filename, so it is validated as all three at once. Returns
    the stripped name; raises ProvisioningError otherwise."""
    name = (name or "").strip()
    if not name:
        raise ProvisioningError("project name is required")
    # `..`, `.`, and anything with a separator can never match the pattern,
    # but say so explicitly: this name becomes os.path.join(parent, name),
    # and a loosened pattern must not turn that into a traversal.
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ProvisioningError("project name must be a single directory name")
    if not PROJECT_NAME_RE.match(name):
        raise ProvisioningError(
            "project name must start with a letter or digit and contain only letters, "
            "digits, '.', '_' and '-' (at most 64 characters)")
    if name in (existing_names or []):
        raise ProvisioningError(f"a project named {name!r} is already configured")
    return name


def _identity_flags(cwd: str) -> list[str]:
    """`-c user.name/user.email` for whichever half of the git identity this
    host lacks. A fresh install has neither, and `git commit` then refuses
    with "Please tell me who you are" -- but an operator who HAS set one
    must not have it overridden by a placeholder."""
    flags: list[str] = []
    for key, fallback in (("user.name", "Persei"), ("user.email", "persei@localhost")):
        ok, out = _run_git(["config", "--get", key], cwd=cwd)
        if not ok or not out.strip():
            flags += ["-c", f"{key}={fallback}"]
    return flags


def create_repository(parent: str | None, name: str, *, description: str = "",
                      existing_names: list[str] | None = None) -> str:
    """Create <parent>/<name> as a git repository with one commit on `main`.

    Returns the realpath of the new directory. Containment is checked BEFORE
    anything touches the disk: the wizard's rule that a project must sit in
    an allowed root applies just as much to a directory this server creates
    as to one it is handed.

    The initial commit is not optional. create_worktree() runs
    `git worktree add -b agent-base`, and on git 2.43 a repo with zero
    commits gives that branch no start point -- the worktree comes up on an
    ORPHAN agent-base with no history in common with main, and the first
    task's merge then has no merge base. One commit here is what makes the
    worktree a worktree of something.
    """
    name = validate_project_name(name, existing_names)
    parent = parent or allowed_roots()[0]
    if not os.path.isabs(parent):
        raise ProvisioningError("parent must be an absolute path")
    target = os.path.join(parent, name)
    real = assert_path_allowed(target)
    # lexists, not exists: a dangling symlink is still something we did not
    # create and must not replace.
    if os.path.lexists(target) or os.path.lexists(real):
        raise ProvisioningError(f"{real} already exists -- onboard it with the wizard instead")
    if not os.path.isdir(parent):
        raise ProvisioningError(f"{parent} does not exist or is not a directory")
    try:
        os.mkdir(real)
    except OSError as e:
        raise ProvisioningError(f"could not create {real}: {e.strerror or e}") from e

    def _fail(what: str, out: str) -> ProvisioningError:
        # Only the directory THIS call made. It was empty a moment ago and
        # nothing else has a reference to it yet.
        shutil.rmtree(real, ignore_errors=True)
        return ProvisioningError(f"{what} failed in {real}: {out}"[:800])

    ok, out = _run_git(["init", "-q", "-b", "main"], cwd=real)
    if not ok:
        raise _fail("git init", out)
    try:
        body = f"# {name}\n"
        if description.strip():
            body += f"\n{description.strip()}\n"
        Path(real, "README.md").write_text(body)
        Path(real, ".gitignore").write_text(_NEW_REPO_GITIGNORE)
    except OSError as e:
        raise _fail("writing README.md/.gitignore", str(e)) from e
    ok, out = _run_git(["add", "-A"], cwd=real)
    if not ok:
        raise _fail("git add", out)
    ok, out = _run_git([*_identity_flags(real), "commit", "-q", "-m", "Initial commit"], cwd=real)
    if not ok:
        raise _fail("git commit", out)
    return real


def recommended_choices(report: DetectionReport) -> dict:
    """The answers the wizard would show pre-ticked, as a `choices` payload.

    This is scripts/add_project.py's --yes rule, kept in one place so the
    headless script and the "new project" endpoint cannot drift from each
    other: every candidate detection marked `enabled` is accepted, every
    flagged one (a test script that makes network calls) stays OFF, and the
    derived-with-certainty items -- checks, build steps, dependency dirs, the
    db env file -- are taken as detected. Nothing this function returns is
    outside what validate_choices() would accept.
    """
    def _accepted(items: list[Candidate]) -> list[str]:
        return [c.value for c in items if c.enabled]

    checks = list(report.checks)
    for r in report.risky_scripts:
        if r.enabled:
            # The same command validate_choices() substitutes for a flagged
            # name, so the headless path writes exactly what the wizard would.
            checks.append(r.check or {
                "name": r.value, "dir": ".",
                "cmd": report.package_manager or "npm", "args": ["run", r.value],
                "timeoutMs": TEST_TIMEOUT_MS_DEFAULT,
            })
    return {
        "secret_files": _accepted(report.secret_files),
        "read_only_mounts": _accepted(report.read_only_mounts),
        "pm2_apps": _accepted(report.pm2_apps),
        "node_modules_dirs": list(report.node_modules_dirs),
        "dependency_dirs": list(report.dependency_dirs),
        "checks": checks,
        "build_steps": list(report.build_steps),
        "db_env_file": report.db_env_file,
    }


# What a person will paste into the wizard. Deliberately narrow: an https URL,
# an ssh URL, or owner/repo. Anything else is a path, which the wizard already
# handles, and guessing between the two is how you clone a directory name.
_GITHUB_HTTPS = re.compile(r"^https://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_GITHUB_SSH = re.compile(r"^(?:ssh://)?git@github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_GITHUB_SLUG = re.compile(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?$")


def parse_github_source(source: str, *, allow_slug: bool = True) -> tuple[str, str] | None:
    """`owner, repo` if this names a GitHub repository, else None.

    None is the answer for a filesystem path, which tells the caller to treat
    the input as a path and carry on exactly as before.

    `allow_slug=False` drops the bare `owner/repo` form, and exists because
    `owner/repo` and `relative/path` are the same string. Nothing can tell them
    apart, so the two callers answer it differently rather than guessing:
    Clone is an explicit action against an explicit input and accepts the bare
    form; Detect only recognises an unmistakable URL, so a relative path still
    gets the "must be absolute" it always got.
    """
    text = (source or "").strip()
    if not text or text.startswith(("/", ".", "~")) or "\\" in text:
        return None
    forms = (_GITHUB_HTTPS, _GITHUB_SSH, _GITHUB_SLUG) if allow_slug else (_GITHUB_HTTPS, _GITHUB_SSH)
    for rx in forms:
        m = rx.match(text)
        if m:
            owner, repo = m.group(1), m.group(2)
            if owner in (".", "..") or repo in (".", ".."):
                return None
            return owner, repo
    return None


def clone_repository(source: str, parent: str | None = None, *, name: str | None = None,
                     existing_names: list[str] | None = None, token: str | None = None) -> str:
    """Clone a GitHub repository into an allowed root and return its realpath.

    The point of the milestone this belongs to: a project that exists nowhere
    but GitHub can be onboarded without the operator checking it out first. The
    clone lands where `create_repository` would have put a new one, under the
    same containment rule, and the caller then runs the ordinary detection flow
    against the path -- nothing downstream knows or cares how the directory got
    there.

    A token is used only when one is supplied, and never written to disk or
    put on a command line: the clone URL is the plain https one, so the
    credential does not end up in .git/config for anyone to find.
    """
    parsed = parse_github_source(source)
    if not parsed:
        raise ProvisioningError(f"{source!r} is not a GitHub URL or owner/repo")
    owner, repo = parsed

    name = validate_project_name(name or repo, existing_names)
    parent = parent or allowed_roots()[0]
    if not os.path.isabs(parent):
        raise ProvisioningError("parent must be an absolute path")
    target = os.path.join(parent, name)
    real = assert_path_allowed(target)
    if os.path.lexists(target) or os.path.lexists(real):
        raise ProvisioningError(f"{real} already exists -- onboard it with the wizard instead")
    if not os.path.isdir(parent):
        raise ProvisioningError(f"{parent} does not exist or is not a directory")

    public_url = f"https://github.com/{owner}/{repo}.git"
    from agent.tools.git import token_credential_args, token_env  # noqa: PLC0415

    # The token reaches git through a credential helper reading the child's
    # environment, never the URL: an argv is readable by every local user,
    # and a URL with credentials in it is written into .git/config.
    if token:
        ok, out = _run_git([*token_credential_args(), "clone", "--quiet", public_url, real],
                           cwd=parent, extra_env=token_env(token))
    else:
        ok, out = _run_git(["clone", "--quiet", public_url, real], cwd=parent)
    if not ok:
        shutil.rmtree(real, ignore_errors=True)
        safe = out.replace(token, "***") if token else out
        raise ProvisioningError(f"clone of {owner}/{repo} failed: {safe}"[:800])

    if not os.path.isdir(os.path.join(real, ".git")):
        shutil.rmtree(real, ignore_errors=True)
        raise ProvisioningError(f"clone of {owner}/{repo} produced no git repository")
    return real
