"""Editing the credentials the deployment runs on, from the Settings page.

Why this is narrow on purpose
-----------------------------
This writes secrets to disk from an HTTP request, so the surface is kept as
small as it can be while still doing the job:

* **An allow-list, not arbitrary keys.** Only the entries in MANAGED_KEYS can be
  read or written. A caller cannot invent a variable name and have it land in a
  .env, and cannot reach a key this module does not know about — notably
  AUTH_SECRET_KEY, the AES-GCM key encrypting TOTP 2FA secrets at rest (whose
  rotation makes every stored 2FA secret permanently undecryptable, locking
  every 2FA user out), and LANGGRAPH_PG_DSN, where a wrong value takes the whole
  agent down rather than degrading it.
* **Values are never returned.** Reads give a masked hint (last four characters)
  and whether the key is set. There is no endpoint that hands back a secret, so
  a compromised session cannot exfiltrate one it did not already have.
* **Never logged.** Errors quote the key NAME only.
* **Atomic writes.** Write a temp file in the same directory, fsync, rename.
  A crash mid-write leaves the old file intact rather than a truncated .env that
  would fail to parse and take the service down on next start.
* **Permissions preserved.** The .env keeps its existing mode (0600), and a
  newly created one is created 0600 rather than inheriting a default umask.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

AGENT_ENV = Path(__file__).resolve().parent.parent / ".env"
ROUTER_ENV = Path(__file__).resolve().parent.parent / "services" / "model-router" / ".env"


@dataclass(frozen=True)
class ManagedKey:
    key: str
    path: Path
    label: str
    help: str
    group: str
    # Which services must restart for a change to take effect. Surfaced to the
    # user rather than silently applied: restarting the router interrupts every
    # in-flight model call, so it is their call when that happens.
    restarts: tuple[str, ...]
    secret: bool = True
    # The row's identity on the wire. Defaults to the key name; only a key
    # that appears in two files needs its own. MODEL_ROUTER_KEY is in both
    # .envs, and a lookup keyed by name kept whichever row came last, so
    # saving the "Router master key" wrote the agent's file and the router
    # never saw the change (2026-09-29).
    id: str = ""

    @property
    def row_id(self) -> str:
        return self.id or self.key


MANAGED_KEYS: tuple[ManagedKey, ...] = (
    ManagedKey(
        "OPENROUTER_API_KEY", ROUTER_ENV, "OpenRouter API key",
        "Pays for every model call. The router resolves each role's alias to a model and bills this key.",
        "Models", ("model-router",),
    ),
    ManagedKey(
        "MODEL_ROUTER_KEY", ROUTER_ENV, "Router master key",
        "The router's own auth. Every service that calls it presents this. Changing it requires updating "
        "MODEL_ROUTER_KEY below to match, or the agent and reviewer lose access.",
        "Models", ("model-router", "tektonix", "commit-reviewer", "agent-review"),
    ),
    ManagedKey(
        "MODEL_ROUTER_KEY", AGENT_ENV, "Router key (agent side)",
        "What the agent presents to the router. Must equal the master key above.",
        "Models", ("tektonix",), id="MODEL_ROUTER_KEY.agent",
    ),
    ManagedKey(
        "LANGSMITH_API_KEY", AGENT_ENV, "LangSmith API key",
        "Tracing. Optional — leave empty to disable. Traces are redacted before they leave the box.",
        "Tracing", ("tektonix",),
    ),
    ManagedKey(
        "LANGSMITH_PROJECT", AGENT_ENV, "LangSmith project",
        "Which LangSmith project traces are filed under.",
        "Tracing", ("tektonix",), secret=False,
    ),
    ManagedKey(
        "LANGSMITH_TRACING", AGENT_ENV, "Tracing enabled",
        "true or false. Off means no trace data leaves this machine at all.",
        "Tracing", ("tektonix",), secret=False,
    ),
    ManagedKey("SMTP_HOST", AGENT_ENV, "SMTP host", "Outbound mail for password resets.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_PORT", AGENT_ENV, "SMTP port", "Usually 587 for STARTTLS.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_USER", AGENT_ENV, "SMTP user", "Usually the sending address.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_PASS", AGENT_ENV, "SMTP password", "App password, not the account password.", "Email", ("tektonix",)),
    ManagedKey("SMTP_FROM", AGENT_ENV, "From address", "What recipients see.", "Email", ("tektonix",), secret=False),
    ManagedKey(
        "GIT_USER_NAME", AGENT_ENV, "Commit author name",
        "Who the agent's commits are from. Blank: this machine's git config (host) or \"Persei\" (bundle).",
        "Git", ("tektonix",), secret=False,
    ),
    ManagedKey(
        "GIT_USER_EMAIL", AGENT_ENV, "Commit author email",
        "The address your GitHub account knows, or its noreply one (ID+user@users.noreply.github.com), "
        "so the commits count as yours.",
        "Git", ("tektonix",), secret=False,
    ),
    # The one switch that decides whether episode recall has a semantic leg
    # at all. Managed here rather than left to a hand-edited .env because
    # that leg costs money on every episode write and its value on this
    # corpus is still being measured -- an operator who wants it off must be
    # able to turn it off from the page they are already looking at, without
    # a shell. Off is the default and stays the default; see docs/todo.md.
    ManagedKey(
        "EMBEDDINGS_ENABLED", AGENT_ENV, "Semantic episode recall",
        "1 or empty. On means episode recall also searches by meaning, not only by words, and "
        "every episode written pays for one embedding. Needs an `embedder` deployment in the "
        "router's config.yaml and pgvector on the database; run scripts/doctor.py to check.",
        "Models", ("tektonix",), secret=False,
    ),
)

def _rows_by_id() -> dict[str, ManagedKey]:
    """Built from MANAGED_KEYS at call time, so a test that points the table
    at temporary files cannot leave a cached lookup aimed at the real ones."""
    rows = {k.row_id: k for k in MANAGED_KEYS}
    if len(rows) != len(MANAGED_KEYS):
        raise RuntimeError("MANAGED_KEYS has two rows with one id")
    return rows


# In the compose bundle none of these files exist: every value comes from the
# .env beside docker-compose.yml on the host, interpolated by compose, and a
# change there is applied by `docker compose up -d`. The page shows what the
# process was given and says so; a write is refused with the same sentence
# (2026-09-28, the first Windows install: "could not write the env file").
COMPOSE_NOTE = ("In the compose bundle these live in the .env file beside docker-compose.yml on the host. "
                "Edit it there and run `docker compose up -d` to apply.")


def managed_by_compose() -> bool:
    return os.environ.get("TEKTONIX_BUNDLE") == "1"


class ReadOnlyError(Exception):
    pass


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def _mask(value: str) -> str:
    """A hint, not the secret. Short values reveal nothing at all rather than
    most of themselves — masking a 6-character value as ``ab…ef`` would give
    away two thirds of it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]}"


def list_keys() -> list[dict]:
    """Masked view for the Settings page. Never returns a value."""
    out = []
    compose = managed_by_compose()
    for mk in MANAGED_KEYS:
        raw = os.environ.get(mk.key, "") if compose else _read_env(mk.path).get(mk.key, "")
        out.append({
            # `id` names the row when saving; `key` is the variable's name.
            # They differ only for a key that lives in two files.
            "id": mk.row_id,
            "key": mk.key,
            "label": mk.label,
            "help": mk.help,
            "group": mk.group,
            "secret": mk.secret,
            "is_set": bool(raw),
            # Non-secret values (host, port, project name) are shown in full —
            # masking a hostname helps nobody and makes the page unusable.
            "display": _mask(raw) if mk.secret else raw,
            "restarts": list(mk.restarts),
            "file": "the host's .env (docker compose)" if compose else str(mk.path),
        })
    return out


class InvalidValueError(Exception):
    pass


def _format_value(value: str) -> str:
    """Render a value for a .env line. audit M-3: reject embedded newlines /
    carriage returns / NULs (which previously let a managed, non-secret key's
    value smuggle a second `KEY=...` line -- e.g. AUTH_SECRET_KEY or
    LANGGRAPH_PG_DSN -- past the key allow-list, since dotenv resolves the
    last occurrence). Quote and escape anything with shell/dotenv-significant
    characters so the written line round-trips to exactly this value.
    """
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise InvalidValueError("value may not contain newlines or NUL")
    needs_quote = value == "" or any(c in value for c in (' ', '\t', '#', '"', "'", '=', '$', '`', '\\'))
    if not needs_quote:
        return value
    # `$` or a backtick goes in single quotes: literal to a shell that
    # sources the file AND to dotenv, which takes single-quoted values as
    # written. In double quotes a `$(...)` saved from the Settings page ran
    # the moment a cron job did `. ./.env`, and there is no double-quoted
    # spelling both readers agree on -- dotenv keeps `\$` as two characters.
    # A value with both `$` and `'` has no shell-safe spelling dotenv reads
    # back correctly; it takes the double-quoted form, and the scripts that
    # read this file no longer source it (scripts/env_value.py).
    if any(c in value for c in ('$', '`')) and "'" not in value:
        return f"'{value}'"
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Rewrite `path` with `updates` applied, preserving order, comments and mode."""
    try:
        original = path.read_text()
        existed = True
    except FileNotFoundError:
        original = ""
        existed = False

    lines = original.splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k = stripped.partition("=")[0].strip()
        if k in updates:
            lines[i] = f"{k}={_format_value(updates[k])}"
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={_format_value(v)}")

    body = "\n".join(lines).rstrip("\n") + "\n"

    # Same directory so the rename is atomic (a cross-filesystem rename is a
    # copy, which reintroduces the truncated-file window this avoids).
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, os.stat(path).st_mode & 0o777 if existed else 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class UnknownKeyError(Exception):
    pass


def set_keys(updates: dict[str, str]) -> dict:
    """Apply updates, keyed by row id (see ManagedKey.id). Returns which
    services need restarting.

    Raises UnknownKeyError naming the key if anything outside the allow-list is
    passed — the name is safe to echo, the value never is.
    """
    by_id = _rows_by_id()
    unknown = [k for k in updates if k not in by_id]
    if unknown:
        raise UnknownKeyError(f"not editable here: {', '.join(sorted(unknown))}")
    if managed_by_compose():
        raise ReadOnlyError(COMPOSE_NOTE)

    # audit M-3: validate every value BEFORE writing any file, so one bad value
    # can't leave a partial multi-file update behind.
    for k, v in updates.items():
        if any(c in v for c in ("\n", "\r", "\x00")):
            raise InvalidValueError(f"value for {k} may not contain newlines or NUL")

    by_file: dict[Path, dict[str, str]] = {}
    restarts: set[str] = set()
    for k, v in updates.items():
        mk = by_id[k]
        by_file.setdefault(mk.path, {})[mk.key] = v
        restarts.update(mk.restarts)

    for path, vals in by_file.items():
        _write_env(path, vals)

    return {"updated": sorted(updates), "restart_required": sorted(restarts)}
