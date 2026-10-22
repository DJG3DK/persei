"""Two properties of the repository itself that nothing else would notice.

Both of these were reported by a reviewer rather than by any check:

* CONTRIBUTING.md promises "the exact five jobs CI runs, in order" and then
  drifted from .github/workflows/ci.yml -- it named `tsc -b --noEmit` while
  CI ran `tsc --noEmit -p tsconfig.app.json`, and two whole CI steps were
  missing from it. A contributor who follows a stale list and then watches CI
  fail stops trusting the document, which is worse than having no list.
* A raw NUL byte in a TypeScript source made git classify the module as
  binary, so every diff of it read "Binary files differ" and no reviewer ever
  saw a line of it change.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"

# Source extensions worth asserting are text. Binary fixtures (images, the
# screenshots under docs/) are deliberately not in this list.
_TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html",
                  ".md", ".json", ".yml", ".yaml", ".sh", ".toml"}
_SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "__pycache__", "backups",
              "logs", ".pytest_cache", ".mypy_cache", "coverage"}


def _ci_run_lines() -> list[str]:
    """Every shell line CI actually executes, flattened across steps."""
    doc = yaml.safe_load(CI.read_text())
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if not run:
                continue
            for raw in str(run).splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    return lines


def _contributing_block() -> list[str]:
    """The commands inside CONTRIBUTING's 'exactly what CI does' block."""
    text = CONTRIBUTING.read_text()
    marker = "The exact five jobs CI runs"
    assert marker in text, "CONTRIBUTING no longer claims to mirror CI -- update this test with it"
    block = text.split(marker, 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    # unwrap `\`-continued lines so a wrapped command reads as one command
    block = block.replace("\\\n", " ")
    return [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# The commands a contributor most needs to get right: each is a gate whose
# local spelling must match CI's, or the contributor's green run means
# nothing. Matched as substrings of a CI line so env prefixes do not matter.
_MUST_MIRROR = [
    "pytest -q",
    "ruff check .",
    "pytest -q services/model-router/tests",
    "ruff check services/model-router",
    "npx tsc --noEmit -p tsconfig.app.json",
    "npm run lint",
    "npm run build",
    "test_newsletter.py",
    "node --check",
    "scripts/doctor.py --quiet",
    "install.sh --dry-run --yes",
    "docker compose config --quiet",
    "scripts/verify_stack_checks.py --no-docker",
    "REQUIRE_MOUNT_TESTS=1 node tests/test_reviewer_dependency_dirs.js",
]


@pytest.mark.parametrize("command", _MUST_MIRROR)
def test_contributing_names_the_command_ci_actually_runs(command):
    ci = "\n".join(_ci_run_lines())
    assert command in ci, f"{command!r} is no longer in ci.yml -- update CONTRIBUTING and this list"
    assert any(command in line for line in _contributing_block()), \
        f"CI runs {command!r} but CONTRIBUTING's block does not"


def test_every_node_test_ci_runs_is_listed_for_contributors():
    """The node suites are the easiest thing to add to CI and forget to
    document: they are one line each, and a contributor who never runs them
    only finds out from a red push."""
    in_ci = {re.search(r"node (tests/\S+\.js)", line).group(1)
             for line in _ci_run_lines() if re.search(r"node tests/\S+\.js", line)}
    assert in_ci, "no node tests in ci.yml -- did the step move?"
    listed = "\n".join(_contributing_block())
    missing = sorted(t for t in in_ci if t not in listed)
    assert not missing, f"CI runs these and CONTRIBUTING does not list them: {missing}"


def test_contributing_does_not_promise_a_command_ci_skips():
    """The other direction: a contributor running something CI does not is
    harmless, but a command that no longer exists anywhere is a trap."""
    ci = "\n".join(_ci_run_lines())
    for line in _contributing_block():
        m = re.match(r"node (tests/\S+\.js)", line)
        if m:
            assert m.group(1) in ci, f"CONTRIBUTING lists {m.group(1)}, which CI does not run"
            assert (REPO / m.group(1)).is_file(), f"{m.group(1)} does not exist"


def _source_files():
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        yield path


def test_no_source_file_contains_a_raw_nul_byte():
    """One NUL is enough for git to treat a whole file as binary: no diff in
    review, no `git diff` locally, no blame that means anything. Write the
    escape (`\\u0000`, `\\0`) instead -- the runtime value is identical."""
    offenders = [str(p.relative_to(REPO)) for p in _source_files() if b"\x00" in p.read_bytes()]
    assert not offenders, f"raw NUL byte in: {offenders}"


def test_the_landing_page_is_not_part_of_an_installation():
    """site/ is perseiagent.ai's public page: marketing, not product.

    Somebody self-hosting the agent wants the console, not a page selling it
    to them, so the release tarball drops the directory and install.sh never
    looks at it. Both halves are checked because either one alone would let it
    back in: the packager could stop deleting it, or the installer could start
    building it.
    """
    packager = (REPO / "scripts" / "package_release.sh").read_text()
    assert 'rm -rf "$STAGE/$NAME/site"' in packager, (
        "package_release.sh no longer drops site/ from the tarball")

    installer = (REPO / "install.sh").read_text()
    for line in installer.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "site/" not in stripped and "cd site" not in stripped, (
            f"install.sh references the landing page: {stripped!r}")


def test_the_console_and_the_landing_page_do_not_share_source():
    """The split is only real if neither build reaches into the other.

    frontend/ importing from site/ would put marketing code in the console's
    bundle; site/ importing from frontend/ would mean the landing page cannot
    be lifted out and hosted on its own, which is half the point.
    """
    for src, other in (("frontend", "site"), ("site", "frontend")):
        root = REPO / src / "src"
        for f in list(root.rglob("*.ts")) + list(root.rglob("*.tsx")) + list(root.rglob("*.css")):
            text = f.read_text()
            assert f"../../{other}/" not in text and f"/{other}/src/" not in text, (
                f"{src}/{f.relative_to(root)} reaches into {other}/")


# The maintainer's own software must not appear in the public tree.
#
# Each needle is assembled from halves so that this file, which is itself
# scanned, does not trip its own check. That is not cleverness for its own
# sake: an exclusion list is a hole, and the one file guaranteed to contain
# every banned string should not be the one file nobody checks.
_FORBIDDEN = [
    ("3D", "Steals"),
    ("3d-", "bot"),
    ("3dweb", "catchers"),
    ("3dcrypto", "bots"),
    ("agent", "Email"),
    ("Agent_", "Email"),
    ("mail-", "chat"),
    ("mail-", "triage"),
    ("trade-", "gate"),
    ("trading ", "bot"),
    ("trading-", "bot"),
]

# Where the agent's own name legitimately appears, and the one place history is
# allowed to keep its own wording.
_SCAN_SKIP_PREFIXES = ("docs/history/",)


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True)
    for rel in out.stdout.split():
        if rel.startswith(_SCAN_SKIP_PREFIXES):
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            yield rel, p.read_text()
        except (UnicodeDecodeError, OSError):
            continue


def test_no_private_project_names_anywhere_in_the_public_tree():
    """This repository is public, and everything in it ships.

    The seed router config carried three aliases belonging to two applications
    that are not this product, with comments naming their checkouts, and the
    Models page introduced them by name to every installation. Somebody reading
    a fresh install was looking at an inventory of one maintainer's other
    software.

    A grep is the whole defence. Nothing else catches a name that arrives in a
    comment written to explain a real incident, which is exactly how these got
    in -- each one was true, useful, and nobody's business.
    """
    hits = []
    for rel, text in _tracked_text_files():
        low = text.lower()
        for a, b in _FORBIDDEN:
            needle = (a + b).lower()
            if needle in low:
                for n, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not hits, (
        "the maintainer's own projects appear in the public tree:\n  "
        + "\n  ".join(hits[:20])
    )


# --- one answer to "which database is this" -------------------------------

# The scheme tests that mean "postgres" or "sqlite". Written split so this
# file's own rule does not trip over the strings it is looking for.
_DSN_SCHEME_TESTS = [s + t for s, t in (
    ('.startswith("sql', 'ite'), ('.startswith("post', 'gres'),
    (".startswith('sql", "ite"), (".startswith('post", "gres"),
)]

# agent/backends.py IS the classifier. tests/ is allowed to assert on it.
_DSN_CLASSIFIER = "agent/backends.py"


def test_only_one_module_decides_which_database_a_dsn_names():
    """Three copies of this two-line check is how a local installation ends
    up with half its subsystems believing they are talking to Postgres.

    The failure is not a crash. It is a deployment where the store opens,
    the search index does not, the embedding probe thinks it is fine, and
    nothing anywhere says why. agent/backends.py exists so there is one
    answer; this is what stops a second one being written in the module
    that happens to need it next.
    """
    hits = []
    for rel, text in _tracked_text_files():
        if not rel.startswith("agent/") or rel == _DSN_CLASSIFIER:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if any(test in line for test in _DSN_SCHEME_TESTS):
                hits.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not hits, (
        "a DSN is classified by agent.backends.backend_for_dsn and nowhere else; also here:\n  "
        + "\n  ".join(hits)
    )


# --- what may enter a docker build context --------------------------------

def _dockerignore_matcher():
    """A small approximation of Docker's .dockerignore matching.

    Docker uses Go's filepath.Match plus `**`. Enough of it is implemented here
    to answer the only question this test asks: would this path be sent to the
    daemon?
    """
    patterns = []
    for raw in (REPO / ".dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        line = line.strip("/")
        rx = ""
        i = 0
        while i < len(line):
            if line.startswith("**/", i):
                rx += "(?:.*/)?"
                i += 3
            elif line.startswith("**", i):
                rx += ".*"
                i += 2
            elif line[i] == "*":
                rx += "[^/]*"
                i += 1
            elif line[i] == "?":
                rx += "[^/]"
                i += 1
            else:
                rx += re.escape(line[i])
                i += 1
        patterns.append((negated, re.compile(rf"^{rx}(?:/.*)?$")))

    def excluded(path: str) -> bool:
        verdict = False
        for negated, rx in patterns:
            if rx.match(path):
                verdict = not negated
        return verdict

    return excluded


def test_the_docker_build_context_excludes_every_kind_of_secret():
    """`docker build` sends the whole directory to the daemon, and .gitignore
    has no say in it.

    Found by building the review image and reading it back: with no
    .dockerignore, a COPY of a services/ subtree baked in a directory of live
    project credentials -- real .env, auth.json and keys.json, put there so the
    reviewer can run tests that need them. An image is a tarball anybody can
    unpack, and a pushed one is public.
    """
    excluded = _dockerignore_matcher()
    must_not_ship = [
        ".env",
        "services/shared/.env",
        "services/commit-reviewer/review-secrets/anyproject/.env",
        "services/commit-reviewer/review-secrets/anyproject/config/keys.json",
        "services/commit-reviewer/builtin-projects.local.js",
        "services/model-router/config.yaml",
        "projects.json",
        "keys/vapid.json",
        "logs/routing.jsonl",
        "services/commit-reviewer/state.json",
        "services/commit-reviewer/worktrees/p-abc/src/index.js",
        ".git/config",
        "frontend/node_modules/x/index.js",
    ]
    leaked = [p for p in must_not_ship if not excluded(p)]
    assert not leaked, "these would be sent to the docker daemon:\n  " + "\n  ".join(leaked)


def test_the_docker_build_context_still_contains_what_the_images_need():
    """An over-broad rule is its own outage: the image builds and then the
    service cannot start because the file it needs was filtered out."""
    excluded = _dockerignore_matcher()
    must_ship = [
        "agent/server.py",
        "services/commit-reviewer/reviewer.js",
        "services/agent-review/server.js",
        "services/shared/service-env.js",
        "services/model-router/config.example.yaml",
        "docker/.env.example",
        "projects.example.json",
        "requirements.txt",
        "frontend/src/main.tsx",
        "frontend/package.json",
    ]
    dropped = [p for p in must_ship if excluded(p)]
    assert not dropped, "the images need these and .dockerignore drops them:\n  " + "\n  ".join(dropped)


def test_the_bundle_never_bind_mounts_a_path_a_fresh_install_lacks():
    """`docker compose up` has to work on a tree nobody has configured yet.

    A bind mount whose source does not exist does not fail politely: Docker
    creates a DIRECTORY at the missing path, and the container then dies on
    "are you trying to mount a directory onto a file". This shipped -- the
    router mounted config.yaml, which is gitignored and therefore absent from
    every fresh clone and every release tarball, so the bundle could not start
    for anyone who had not already been running it.
    """
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    tracked = set(
        subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    )
    offenders = []
    for name, svc in compose.get("services", {}).items():
        for vol in svc.get("volumes", []) or []:
            if not isinstance(vol, str):
                continue
            src = vol.split(":", 1)[0]
            # Only relative paths inside the repo are this test's business.
            # ${VAR} sources are the operator's to point somewhere real, and a
            # ${VAR:-default} is judged on its default.
            if src.startswith("${"):
                if ":-" not in src:
                    continue
                src = src.split(":-", 1)[1].rstrip("}")
            if not src.startswith("./"):
                continue
            rel = src[2:]
            if rel in tracked:
                continue
            if (REPO / rel).is_dir():
                continue
            offenders.append(f"{name}: {vol}")
    assert not offenders, (
        "these mount a source that a fresh checkout does not have, so the "
        "container dies on a type mismatch:\n  " + "\n  ".join(offenders)
    )


def test_the_bundle_waits_for_the_router_before_the_agent_is_ready():
    """A first task that starts while the router is still booting escalates
    with 'peer closed connection'. The image HEALTHCHECK already probes the
    router; compose also has to refuse to mark the agent started until that
    probe can succeed, and the reviewers until the agent has written the
    control secret.
    """
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    services = compose["services"]

    def _healthy_dep(svc: str, dep: str) -> None:
        raw = services[svc].get("depends_on") or {}
        assert isinstance(raw, dict), (
            f"{svc}.depends_on must be the long form so it can wait on health, "
            f"not a list of names"
        )
        assert dep in raw, f"{svc} does not wait for {dep}"
        assert raw[dep].get("condition") == "service_healthy", (
            f"{svc} waits for {dep} to start, not to be healthy"
        )

    _healthy_dep("agent", "postgres")
    _healthy_dep("agent", "router")
    _healthy_dep("agent-review", "agent")
    _healthy_dep("agent-review", "router")
    _healthy_dep("commit-reviewer", "agent")
    _healthy_dep("commit-reviewer", "router")

    for name, port_path in (
        ("router", "http://127.0.0.1:4001/health/liveliness"),
        ("agent-review", "http://127.0.0.1:4100/health"),
        ("commit-reviewer", "http://127.0.0.1:4101/health"),
    ):
        check = services[name].get("healthcheck") or {}
        test = check.get("test") or []
        joined = " ".join(test) if isinstance(test, list) else str(test)
        assert port_path in joined, f"{name} healthcheck does not probe {port_path}"


def test_the_bundle_sets_variable_names_the_code_actually_reads():
    """An env var set in a Dockerfile and read nowhere is invisible: the
    default silently applies and the failure surfaces somewhere unrelated.

    AGENT_PROJECTS_ROOT was set in the agent image; provisioning.py reads
    AGENT_PROJECT_ROOTS. The allow-list therefore stayed at its /home default
    and onboarding refused every path under /projects -- the only place the
    bundle puts anything -- with a message about allowed roots that named a
    directory the container does not use.
    """
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    dockerfiles = list((REPO / "docker").rglob("Dockerfile"))
    assert dockerfiles, "no Dockerfiles found"

    declared = set()
    for df in dockerfiles:
        for m in re.finditer(r"^\s*(?:ENV\s+)?(AGENT_[A-Z0-9_]+)=", df.read_text(), re.M):
            declared.add(m.group(1))
    for svc in compose.get("services", {}).values():
        env = svc.get("environment") or {}
        names = env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
        declared.update(n for n in names if n.startswith("AGENT_"))

    # Everything the Python and JS actually consult.
    read = set()
    for path in list((REPO / "agent").rglob("*.py")) + list((REPO / "services").rglob("*.js")):
        if "node_modules" in str(path):
            continue
        text = path.read_text(errors="ignore")
        # Any mention at all, including assignment to a constant that is read
        # later (_HOST_PATH_MAP_ENV = "AGENT_HOST_PATH_MAP"). Deliberately
        # permissive: the bug worth catching is a name that appears NOWHERE,
        # and being clever about indirection only produces false alarms.
        read.update(re.findall(r"AGENT_[A-Z0-9_]+", text))

    unread = sorted(declared - read)
    assert not unread, (
        "the bundle sets these and nothing reads them, so their default silently "
        "applies:\n  " + "\n  ".join(unread)
    )


def test_the_checks_network_holds_the_two_throwaway_services_and_nothing_that_matters():
    """A check container joins the `checks` network alone. The agent used to
    be on it too (its API listens on 0.0.0.0), one hop from every check. Now
    the only members are the two throwaway services; the agent sets a run up
    over `docker exec` into them by fixed container names, hands each run its
    own plain role rather than the superuser (whose bootstrap password the
    init script replaces at start), and the env the agent reads names all of
    that. Cursor's re-audit, 2026-09-27."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    services = compose["services"]
    # Named outright: the agent attaches by this string, and compose's
    # default (<project>_checks) broke under the legacy project name.
    assert compose["networks"]["checks"] == {"name": "tektonix_checks", "internal": True}
    assert services["agent"]["environment"]["REVIEW_CHECKS_NETWORK"] == "tektonix_checks"
    on_checks = {name for name, svc in services.items() if "checks" in (svc.get("networks") or [])}
    assert on_checks == {"checks-postgres", "checks-redis"}, on_checks
    for name in on_checks:
        assert services[name].get("networks") == ["checks"], f"{name} must be on the checks network only"
    env = services["agent"]["environment"]
    assert env["REVIEW_CHECKS_POSTGRES_CONTAINER"] == services["checks-postgres"]["container_name"]
    assert env["REVIEW_CHECKS_REDIS_CONTAINER"] == services["checks-redis"]["container_name"]
    assert "@checks-postgres:" in env["REVIEW_CHECKS_POSTGRES_URL"] and ":checks@" not in env["REVIEW_CHECKS_POSTGRES_URL"], (
        "the agent holds no superuser password; a check gets its own role")
    init = "./docker/checks-postgres/init.sh:/docker-entrypoint-initdb.d/init.sh:ro"
    assert init in (services["checks-postgres"].get("volumes") or []), "the bootstrap superuser password must be rotated at start"
    assert "ALTER USER" in (REPO / "docker/checks-postgres/init.sh").read_text()


def test_the_bundle_is_named_tektonix_and_builds_the_sandbox_image_before_the_agent():
    """2026-09-28, the first Windows install: containers came up as
    three-d-agent-*, and the agent was unhealthy because its entrypoint was
    still building the sandbox image when the health window closed."""
    raw = (REPO / "docker-compose.yml").read_text()
    compose = yaml.safe_load(raw)
    assert compose["name"] == "tektonix"
    code = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "three-d-agent" not in code and "three_d_agent" not in code, "the old name survives outside a comment"
    services = compose["services"]
    assert services["agent"]["depends_on"]["sandbox-image"] == {"condition": "service_completed_successfully"}
    assert services["sandbox-image"]["image"] == "tektonix-sandbox:latest"
    assert services["sandbox-image"]["build"] == {"context": "./docker/agent-sandbox"}
    assert services["agent"]["environment"]["REVIEW_CHECKS_NETWORK"] == "tektonix_checks"


def test_the_bundle_gives_every_committing_container_a_git_identity():
    """The first Windows install's first task ended at its final commit with
    "Please tell me who you are": a container has no git identity, where a
    host install has the operator's global config. The agent commits, the
    commit-reviewer merges; both get the operator's address."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for svc in ("agent", "commit-reviewer", "agent-review"):
        env = compose["services"][svc]["environment"]
        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            assert key in env, f"{svc} lacks {key}"
        assert env["GIT_AUTHOR_EMAIL"] == "${GIT_USER_EMAIL:-${ADMIN_EMAIL:-admin@example.com}}", "the operator's own, else the sign-in address"
        assert env["GIT_AUTHOR_NAME"] == "${GIT_USER_NAME:-Persei}"
    agent = compose["services"]["agent"]["environment"]
    assert agent["GIT_USER_NAME"] == "${GIT_USER_NAME:-}" and agent["GIT_USER_EMAIL"] == "${GIT_USER_EMAIL:-}", (
        "the Environment page reads the container's environment for these two names")


def test_every_built_service_builds_rather_than_pulling_its_local_name():
    """`up -d` on a source install tried to pull tektonix-reviewer:latest
    from Docker Hub before building it, and printed "pull access denied"
    (2026-09-28). pull_policy: build means a plain `up -d` builds what
    changed and never asks a registry for a local name."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for name, svc in compose["services"].items():
        if "build" in svc:
            assert svc.get("pull_policy") == "build", f"{name} needs pull_policy: build"


def test_the_checks_redis_denies_the_modules_redis_8_loads():
    """I25, 2026-10-22: redis:8's image loads search, JSON, time series and
    bloom, and +@all granted their commands; FT.CONFIG SET reaches every
    run like CONFIG, and MODULE LOAD runs any library. Their categories
    are denied, and the image is pinned to the one this was checked on."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    svc = compose["services"]["checks-redis"]
    assert re.fullmatch(r"redis:8-alpine@sha256:[0-9a-f]{64}", svc["image"])
    rule = svc["command"][svc["command"].index("--user") + 1:]
    for denied in ("-module", "-@search", "-@json", "-@timeseries", "-@bloom",
                   "-@cuckoo", "-@cms", "-@topk", "-@tdigest"):
        assert denied in rule, denied
    assert rule.index("+@all") < min(rule.index(d) for d in ("-module", "-@search")), "a deny after the grant"
