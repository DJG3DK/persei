import json
import logging
import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.backends import backend_for_dsn

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def apply_git_identity(env: MutableMapping[str, str] | None = None) -> None:
    """GIT_USER_NAME / GIT_USER_EMAIL (the operator's, from .env or the app's
    settings) become the author and committer of every commit the agent
    makes, unless git's own variables are already set. The bundle sets the
    git variables in compose from the same two names; a host install goes
    through here."""
    env = os.environ if env is None else env
    for src, targets in (("GIT_USER_NAME", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                         ("GIT_USER_EMAIL", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        value = (env.get(src) or "").strip()
        if not value:
            continue
        for t in targets:
            if not (env.get(t) or "").strip():
                env[t] = value


apply_git_identity()

# The variables the HTTP server needs and a local run does not. They used to
# be os.environ[...] in load_config, which gave the server its fail-fast boot
# for free -- and gave everything else a KeyError for a mail server it was
# never going to use. They are optional on the dataclass and re-required by
# require_server_config(), which the server calls on the line after
# load_config(); see that function for why one call site matters.
_SERVER_ONLY_VARS = ("AUTH_SECRET_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")

# The escape hatch for pointing a server at something other than Postgres.
# It exists so the refusal below can be overridden deliberately, in a
# sentence an operator has to type, rather than by editing this file.
ALLOW_SQLITE_SERVER_VAR = "AGENT_ALLOW_SQLITE_SERVER"


@dataclass(frozen=True)
class Config:
    # Which database this installation talks to, and therefore which backend
    # agent/graph.py opens. AGENT_DSN if it is set, otherwise the Postgres
    # DSN this has always read -- so a box that has never heard of AGENT_DSN
    # gets exactly the string it got before.
    dsn: str
    router_base_url: str
    router_api_key: str
    langsmith_tracing: bool
    auth_secret_key: str | None
    cors_allow_origins: list[str]
    smtp_host: str | None
    smtp_port: int | None
    smtp_user: str | None
    smtp_pass: str | None
    smtp_from: str | None
    admin_email: str
    # Optional: fine-grained, read-only, selected repos. Enables the GitHub
    # pull-request tools (agent/tools/github_tools.py); absent = tools absent.
    github_token: str | None = None
    # Semantic search over episodes. Off unless an operator turns it on:
    # it needs a pgvector extension on the database and an embedding model
    # on the router, and neither is present by default. The alias is the
    # router's own name for that model.
    embeddings_enabled: bool = False
    embedding_alias: str = "embedder"
    embedding_dims: int = 1536

    @property
    def pg_dsn(self) -> str:
        """The DSN, for the components that can only be Postgres.

        A property rather than a field so that raw-SQL callers -- the auth
        schema, the advisory lock, the seed scripts -- say what they need in
        the act of reading it. On a Postgres installation this returns the
        same string `dsn` holds, to the same call sites, and nothing about
        the running server changes. On any other backend it fails with a
        sentence instead of handing over a connection string that will be
        parsed by a driver that cannot use it.
        """
        if backend_for_dsn(self.dsn) != "postgres":
            raise RuntimeError(
                f"this component requires Postgres, and this installation runs on {self.dsn!r}"
            )
        return self.dsn


def load_config(dsn: str | None = None) -> Config:
    """Read the environment. `dsn` overrides it, for a caller that carries
    its own database (the local CLI) and must not depend on what happens to
    be in a .env file next to the checkout."""
    smtp_port = os.environ.get("SMTP_PORT")
    return Config(
        dsn=dsn or os.environ.get("AGENT_DSN") or os.environ["LANGGRAPH_PG_DSN"],
        router_base_url=os.environ["MODEL_ROUTER_URL"],
        router_api_key=os.environ["MODEL_ROUTER_KEY"],
        langsmith_tracing=os.environ.get("LANGSMITH_TRACING", "").lower() == "true",
        auth_secret_key=os.environ.get("AUTH_SECRET_KEY"),
        # Same-origin by default -- server.py serves the frontend itself, so
        # production needs no CORS at all. Set CORS_ALLOW_ORIGINS (comma-
        # separated) only for a split dev setup with Vite on its own port.
        cors_allow_origins=[o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()],
        smtp_host=os.environ.get("SMTP_HOST"),
        smtp_port=int(smtp_port) if smtp_port else None,
        smtp_user=os.environ.get("SMTP_USER"),
        smtp_pass=os.environ.get("SMTP_PASS"),
        smtp_from=os.environ.get("SMTP_FROM"),
        admin_email=os.environ.get("ADMIN_EMAIL", "admin@example.com"),
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        embeddings_enabled=os.environ.get("EMBEDDINGS_ENABLED", "").lower() in ("1", "true", "yes"),
        embedding_alias=os.environ.get("EMBEDDING_ALIAS") or "embedder",
        embedding_dims=int(os.environ.get("EMBEDDING_DIMS") or "1536"),
    )


def require_server_config(config: Config) -> None:
    """Everything the HTTP server must have before it serves a request.

    Two jobs, both of them about failing at boot instead of failing later.

    The first is the fail-fast the server lost when these variables became
    optional. It reports EVERY missing one at once: os.environ[] raised on
    whichever happened to be read first, so an operator with three gaps
    fixed them one restart at a time, and the third restart was the one that
    told them about the third gap.

    The second is the DSN. A stray AGENT_DSN pointing the server at a local
    file would not crash anything -- the server would come up, open an empty
    database, and show zero tasks for every project. That reads as data
    loss, and the operator's next move after seeing it is not a calm one. So
    the server refuses a non-Postgres DSN outright unless someone has
    deliberately set AGENT_ALLOW_SQLITE_SERVER=1.
    """
    # Present, not truthy. This is exactly what os.environ[NAME] used to
    # demand, and an operator who has deliberately set SMTP_USER= empty for
    # a relay that wants no credentials has configured it, not forgotten it.
    missing = [name for name in _SERVER_ONLY_VARS if name not in os.environ]
    problems = []
    if missing:
        problems.append(
            "these variables are not set in the environment or in .env: " + ", ".join(missing)
        )
    if backend_for_dsn(config.dsn) != "postgres" and os.environ.get(ALLOW_SQLITE_SERVER_VAR) != "1":
        problems.append(
            f"the server is pointed at {config.dsn!r}, which is not Postgres -- it would start "
            "against an empty database and every project would read as having no tasks. Unset "
            f"AGENT_DSN, or set {ALLOW_SQLITE_SERVER_VAR}=1 if this is really what you want."
        )
    if problems:
        raise RuntimeError("this installation cannot start a server:\n  " + "\n  ".join(problems))


# Deployment-specific: which repos this agent can target, and each one's
# workspace. Since the 2026-08-25 migration the "sandbox" key names a git
# WORKTREE of the live repo (/home/agent-workspaces/<name>), not a separate
# clone — the agent commits to a per-task branch there, which is a plain local
# ref in the live repo, and the review service reads that branch directly.
# There is no git remote between them any more; the key keeps its old name so
# existing projects.json files stay valid. One task runs at a time per project
# (the per-project lock in graph.py), so the workspace is never contended.
#
# Loaded from projects.json (gitignored, deployment-specific) with
# projects.example.json as the committed template.

# AGENT_PROJECTS_JSON is what lets the container bundle put this file on a
# shared volume: the agent writes it from onboarding and the two review
# services read it, and in the bundle those are three separate containers. The
# Node side has read the same variable since it was written; this is the half
# that was missing. Unset on a host install, where the repo-root file is right.
_PROJECTS_CONFIG_PATH = Path(
    os.environ.get("AGENT_PROJECTS_JSON")
    or Path(__file__).resolve().parent.parent / "projects.json"
)
_PROJECTS_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "projects.example.json"


# What `git check-ref-format --branch` accepts, near enough: no whitespace or
# control characters, none of git's reserved punctuation, no "..", "@{" or a
# leading "-". A project's base_branch reaches shell commands through
# tools/git.py and the review gate, and only a hand-edited projects.json
# sets it, so a bad value is dropped here (the callers fall back to main)
# rather than trusted at thirteen call sites (2026-09-29).
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*@\{)(?!.*/\.)(?!.*\.lock(/|$))[^\s~^:?*\[\\\x00-\x1f\x7f]+(?<![/.])$")


def valid_branch_name(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 255 and bool(_BRANCH_RE.match(name))


def _load_projects_config() -> dict[str, Any]:
    path = _PROJECTS_CONFIG_PATH if _PROJECTS_CONFIG_PATH.exists() else _PROJECTS_EXAMPLE_PATH
    with open(path) as f:
        data = json.load(f)
    for name, entry in (data.get("projects") or {}).items():
        if isinstance(entry, dict) and "base_branch" in entry and not valid_branch_name(entry["base_branch"]):
            logging.getLogger("tektonix").warning(
                "projects.json: %s has a base_branch that is not a branch name; using main", name)
            del entry["base_branch"]
    return data


_projects_config = _load_projects_config()
PROJECTS: dict[str, dict[str, Any]] = _projects_config["projects"]

# Per-project test-only environment overrides — for a project whose test
# suite needs a path to something outside its own sandbox checkout (for
# example, a live config file it deliberately targets against a single real
# instance). Scoped per project; not a standing grant to every task.
PROJECT_TEST_ENV: dict[str, dict[str, Any]] = _projects_config.get("test_env", {})


def reload_projects() -> dict:
    """Re-read projects.json IN PLACE, so a project added at runtime (the
    onboarding wizard) is visible without restarting the process.

    In place is the whole point: every consumer does
    `from agent.config import PROJECTS`, which binds this exact dict object.
    Rebinding the module global would leave all of them pointing at the old
    copy -- the newly onboarded project would exist in config.py and nowhere
    else. clear()+update() mutates the object they already hold.
    """
    global _projects_config
    _projects_config = _load_projects_config()
    PROJECTS.clear()
    PROJECTS.update(_projects_config["projects"])
    PROJECT_TEST_ENV.clear()
    PROJECT_TEST_ENV.update(_projects_config.get("test_env", {}))
    return PROJECTS
