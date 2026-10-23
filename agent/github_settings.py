"""GitHub integration settings: tokens, per-project policies, notification
targets. Stored in the same Postgres store as the runtime limits, so they
survive restarts and ride along in the backup.

Tokens are the one secret here. They are encrypted at rest with the same
AES-GCM key that protects TOTP secrets (AUTH_SECRET_KEY, see agent/auth.py),
never returned by the read endpoint (a name, a creation date and the last
four characters are enough to tell tokens apart), and only ever decrypted on
the host -- the sandbox is built so the coder never holds one.

Policy is per project and per SOURCE, because "auto" is a very different
decision for a Dependabot bump than for a security alert. Each source is
one of:

  off      ignore it (the inbox still lists what was seen)
  propose  put it in the inbox, alert the operator with an approve link
  auto     create the task immediately, within the project's caps

Auto never bypasses anything: a task created this way runs the same review
gate and the same merge approval as one typed into the New Task form. The
only thing "auto" removes is the click that starts it.
"""

from __future__ import annotations

import base64
import copy
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, cast

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.config import Config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.store.base import BaseStore

logger = logging.getLogger("tektonix")

NAMESPACE = ("settings",)
KEY = "github"

MODES = ("off", "propose", "auto")

# What the poller looks for. Order is the order the settings card shows.
SOURCES: dict[str, dict[str, str]] = {
    "dependabot_prs": {
        "label": "Dependabot pull requests",
        "help": "Open PRs authored by dependabot[bot] (and other bots when the author filter allows). "
                "The task reads the PR, applies the bump on a task branch, runs the suite and lands it through the gate.",
    },
    "security_alerts": {
        "label": "Dependabot security alerts",
        "help": "Open alerts from the repository's Dependabot alerts page. Needs the token's "
                "'Dependabot alerts: read' permission. The task upgrades the affected package.",
    },
    "review_requests": {
        "label": "Review comments requesting changes",
        "help": "Open PRs where a reviewer left a CHANGES_REQUESTED review. The task reads the review "
                "comments and addresses each one.",
    },
    "ci_failures": {
        "label": "Failing checks on the default branch",
        "help": "A check run that concluded failure on the tip of the default branch. The task "
                "reads the failing check and fixes what broke.",
    },
    "code_scanning": {
        "label": "Code scanning alerts (CodeQL)",
        "help": "Open alerts from the repository's Security → Code scanning page, one inbox item per "
                "rule so a task fixes every location of the same finding together. Needs the token's "
                "'Code scanning alerts: read' permission. The task fixes the cause in this repository "
                "only; it never dismisses the alert on GitHub.",
    },
}

AUTHOR_FILTERS = ("dependabot", "bots", "anyone")

DEFAULT_PROJECT: dict[str, Any] = {
    "token": None,                 # name of a stored token; None = the GITHUB_TOKEN env fallback
    "policies": {name: "off" for name in SOURCES},
    "budget_usd": 3.0,             # per task created from the inbox
    "max_open_auto": 2,            # auto tasks that may be running/awaiting at once
    "authors": "dependabot",       # which PR authors count for dependabot_prs
    "route": "auto",
}

DEFAULTS: dict[str, Any] = {
    "poll_interval_min": 10,
    "public_url": "",              # e.g. https://agent.example.com -- used to build approve links
    "notify": {"telegram": True, "email": False, "email_to": ""},
    "tokens": {},                  # name -> {"enc": ..., "hint": "…abcd", "created_at": ts}
    "projects": {},                # repo -> DEFAULT_PROJECT shape
}

_BUDGET_MIN, _BUDGET_MAX = 0.25, 200.0
_MAX_OPEN_MIN, _MAX_OPEN_MAX = 1, 20
_POLL_MIN, _POLL_MAX = 2, 24 * 60

_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# encryption -- same key and construction as the TOTP secrets in agent/auth.py
# ---------------------------------------------------------------------------

# The one key both modules encrypt with; auth.py owns it.
# pyright: reportPrivateUsage=false
def _key(config: Config) -> bytes:
    from agent.auth import _fernet_key
    return _fernet_key(config)


def encrypt_token(config: Config, token: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_key(config)).encrypt(nonce, token.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_token(config: Config, enc: str) -> str:
    raw = base64.urlsafe_b64decode(enc)
    return AESGCM(_key(config)).decrypt(raw[:12], raw[12:], None).decode()


def token_hint(token: str) -> str:
    return "…" + token[-4:] if len(token) >= 8 else "…"


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------

def _merge_project(raw: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_PROJECT)
    raw = raw or {}
    for k in ("token", "budget_usd", "max_open_auto", "authors", "route"):
        if k in raw:
            out[k] = raw[k]
    pol: dict[str, Any] = raw.get("policies") or {}
    for name in SOURCES:
        if pol.get(name) in MODES:
            out["policies"][name] = pol[name]
    # code_scanning arrived after projects were configured. A project that
    # already lets the inbox handle Dependabot alerts gets the same mode for
    # CodeQL alerts until the operator sets it explicitly -- they are the same
    # kind of work (a security finding on this repository) and the operator
    # asked for the ability on every repo. Persisted on the next save.
    if "code_scanning" not in pol and pol.get("security_alerts") in MODES:
        out["policies"]["code_scanning"] = pol["security_alerts"]
    return out


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Fill every gap with a default so callers never branch on absence."""
    raw = raw or {}
    out = copy.deepcopy(DEFAULTS)
    if isinstance(raw.get("poll_interval_min"), (int, float)):
        out["poll_interval_min"] = int(raw["poll_interval_min"])
    if isinstance(raw.get("public_url"), str):
        out["public_url"] = raw["public_url"].strip().rstrip("/")
    notify: dict[str, Any] = raw.get("notify") or {}
    out["notify"] = {
        "telegram": bool(notify.get("telegram", True)),
        "email": bool(notify.get("email", False)),
        "email_to": str(notify.get("email_to") or "").strip(),
    }
    tokens: dict[str, Any] = raw.get("tokens") or {}
    out["tokens"] = {str(n): dict(cast(dict[str, Any], t)) for n, t in tokens.items()
                     if isinstance(t, dict) and cast(dict[str, Any], t).get("enc")}
    projects: dict[str, Any] = raw.get("projects") or {}
    out["projects"] = {str(r): _merge_project(p) for r, p in projects.items()}
    return out


def public_view(settings: dict[str, Any]) -> dict[str, Any]:
    """What the dashboard may see: everything but the ciphertext."""
    view = copy.deepcopy(settings)
    view["tokens"] = {
        name: {"hint": t.get("hint", "…"), "created_at": t.get("created_at")}
        for name, t in settings.get("tokens", {}).items()
    }
    return view


def project_settings(settings: dict[str, Any], repo: str) -> dict[str, Any]:
    return settings["projects"].get(repo) or copy.deepcopy(DEFAULT_PROJECT)


def token_for(settings: dict[str, Any], config: Config, repo: str) -> str | None:
    """The token a project should use: its named token, else the env fallback."""
    proj = project_settings(settings, repo)
    name = proj.get("token")
    if name:
        entry = settings["tokens"].get(name)
        if entry:
            try:
                return decrypt_token(config, entry["enc"])
            except Exception:  # noqa: BLE001 -- a rotated AUTH_SECRET_KEY makes old ciphertext unreadable
                logger.exception("github: token %r cannot be decrypted; falling back to GITHUB_TOKEN", name)
    return getattr(config, "github_token", None)


def any_token(settings: dict[str, Any], config: Config) -> bool:
    return bool(settings.get("tokens")) or bool(getattr(config, "github_token", None))


def enabled_projects(settings: dict[str, Any]) -> list[str]:
    """Projects with at least one source not switched off."""
    return [repo for repo, p in settings["projects"].items() if any(m != "off" for m in p["policies"].values())]


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def current() -> dict[str, Any]:
    """The loaded settings (defaults before load())."""
    return _cache if _cache is not None else normalize(None)


async def load(store: BaseStore) -> dict[str, Any]:
    global _cache
    try:
        item = await store.aget(NAMESPACE, KEY)
        _cache = normalize(item.value if item else None)
    except Exception:  # noqa: BLE001 -- a store hiccup must not take the settings page down
        logger.exception("github settings: load failed; using defaults until the next save")
        _cache = normalize(None)
    return _cache


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def apply_patch(config: Config, settings: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a dashboard patch against the current settings and return the
    new document. Pure, so it is unit-testable without a store.

    Patch shape (every key optional):
      poll_interval_min, public_url, notify{telegram,email,email_to}
      add_tokens: {name: raw_token}     stored encrypted
      remove_tokens: [name]             projects using one fall back to env
      rename_tokens: {old: new}         projects pointing at it follow
      projects: {repo: {token, policies{source: mode}, budget_usd, max_open_auto, authors, route}}
    """
    out = copy.deepcopy(settings)
    if "poll_interval_min" in patch:
        out["poll_interval_min"] = int(_clamp(float(patch["poll_interval_min"]), _POLL_MIN, _POLL_MAX))
    if "public_url" in patch:
        url = str(patch["public_url"] or "").strip().rstrip("/")
        if url and not (url.startswith("https://") or url.startswith("http://")):
            raise ValueError("public_url must start with https:// or http://")
        out["public_url"] = url
    if "notify" in patch and isinstance(patch["notify"], dict):
        n = cast(dict[str, Any], patch["notify"])
        email_to = str(n.get("email_to", out["notify"]["email_to"]) or "").strip()
        out["notify"] = {
            "telegram": bool(n.get("telegram", out["notify"]["telegram"])),
            "email": bool(n.get("email", out["notify"]["email"])),
            "email_to": email_to,
        }
        if out["notify"]["email"] and email_to and "@" not in email_to:
            raise ValueError("notify.email_to is not an email address")
    remove_tokens: list[Any] = patch.get("remove_tokens") or []
    for name in remove_tokens:
        out["tokens"].pop(str(name), None)
        for proj in out["projects"].values():
            if proj.get("token") == name:
                proj["token"] = None
    # A token's name is a label the operator chose, and they rename them in
    # GitHub as their understanding of what each one is for improves. Without
    # this the only way to correct one here was remove and re-add, which means
    # pasting the secret again and losing every project mapped to it.
    rename_tokens: dict[str, Any] = patch.get("rename_tokens") or {}
    for old_raw, new_raw in rename_tokens.items():
        old, new = str(old_raw).strip(), str(new_raw).strip()
        if old not in out["tokens"]:
            raise ValueError(f"no token named {old!r}")
        if not new or len(new) > 40:
            raise ValueError("token name must be 1-40 characters")
        if new == old:
            continue
        if new in out["tokens"]:
            raise ValueError(f"a token named {new!r} already exists")
        out["tokens"][new] = out["tokens"].pop(old)
        # Projects point at a token BY NAME, so the rename has to follow or
        # every one of them silently falls back to the environment token.
        for proj in out["projects"].values():
            if proj.get("token") == old:
                proj["token"] = new

    add_tokens: dict[str, Any] = patch.get("add_tokens") or {}
    for name_raw, raw_token in add_tokens.items():
        name = str(name_raw).strip()
        raw = str(raw_token or "").strip()
        if not name or len(name) > 40:
            raise ValueError("token name must be 1-40 characters")
        if not raw or len(raw) < 20 or any(ch.isspace() for ch in raw):
            raise ValueError(f"token {name!r} does not look like a GitHub token")
        out["tokens"][name] = {"enc": encrypt_token(config, raw), "hint": token_hint(raw), "created_at": time.time()}
    patch_projects: dict[str, Any] = patch.get("projects") or {}
    for repo, p in patch_projects.items():
        if not isinstance(p, dict):
            continue
        p = cast(dict[str, Any], p)
        cur = _merge_project(out["projects"].get(repo))
        if "token" in p:
            tok = p["token"] or None
            if tok is not None and tok not in out["tokens"]:
                raise ValueError(f"project {repo!r} names an unknown token {tok!r}")
            cur["token"] = tok
        policies: dict[str, Any] = p.get("policies") or {}
        for source, mode in policies.items():
            if source not in SOURCES:
                raise ValueError(f"unknown source {source!r}")
            if mode not in MODES:
                raise ValueError(f"mode for {source} must be one of {MODES}")
            cur["policies"][source] = mode
        if "budget_usd" in p:
            cur["budget_usd"] = _clamp(float(p["budget_usd"]), _BUDGET_MIN, _BUDGET_MAX)
        if "max_open_auto" in p:
            cur["max_open_auto"] = int(_clamp(int(p["max_open_auto"]), _MAX_OPEN_MIN, _MAX_OPEN_MAX))
        if "authors" in p:
            if p["authors"] not in AUTHOR_FILTERS:
                raise ValueError(f"authors must be one of {AUTHOR_FILTERS}")
            cur["authors"] = p["authors"]
        if "route" in p:
            if p["route"] not in ("auto", "frontend", "general"):
                raise ValueError("route must be auto, frontend or general")
            cur["route"] = p["route"]
        out["projects"][repo] = cur
    return out


async def save(store: BaseStore, config: Config, patch: dict[str, Any]) -> dict[str, Any]:
    global _cache
    new = apply_patch(config, current(), patch)
    await store.aput(NAMESPACE, KEY, new)
    _cache = new
    return new
