"""The deployment table, read from the operator's own config.yaml.

The same file the dashboard's Models page writes to. That
is the whole migration story: none. Point MODEL_ROUTER_URL at this service and
every pin, cost figure and fallback rule carries over untouched.

Reloaded on change rather than at startup. The old proxy read the file once, so
repinning a model needed a router restart, and a restart kills every model call
in flight across every service sharing the router -- the one operation
docs/architecture.md tells you never to do while a task is running. Here the
file's mtime is checked per request (a stat, ~microseconds) and a changed file
is swapped in atomically between requests. In-flight calls keep the table they
started with.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("model-router")

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MODEL_ROUTER_CONFIG")
    or str(Path(__file__).resolve().parents[1] / "config.yaml")
)
# Applies when a deployment does not set its own. The old proxy defaulted to none at
# all, which is how one upstream call ran 1802 seconds for 280 output tokens.
DEFAULT_TIMEOUT_S = float(os.environ.get("MODEL_ROUTER_TIMEOUT_S", "600"))


PRIVACY_MODES = ("zdr", "deny", "off")


@dataclass(frozen=True)
class Deployment:
    """One `- model_name:` entry."""

    alias: str
    model: str                      # provider-qualified, e.g. "deepseek/deepseek-v4.1-flash"
    extra_body: dict = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class Table:
    """An immutable snapshot. Swapped wholesale, never mutated, so a request
    that started under one version finishes under it."""

    deployments: dict[str, Deployment]
    fallbacks: dict[str, list[str]]
    loaded_at: float
    mtime: float
    # router_settings.privacy: which OpenRouter providers may serve a call.
    # "zdr" (the default) only those that keep nothing, "deny" only those
    # that neither store nor train on it, "off" whatever the account allows.
    privacy: str = "zdr"

    def resolve(self, alias: str) -> Deployment | None:
        return self.deployments.get(alias)

    def chain(self, alias: str) -> list[str]:
        """The alias itself, then its fallbacks -- each one only if it exists.

        A fallback naming a deployment that is not in the table is dropped
        rather than attempted: the old proxy answered that case with "Available Model
        Group Fallbacks=None" at the moment of failure, which is the worst
        possible time to find out.
        """
        out = [alias]
        for name in self.fallbacks.get(alias, []):
            if name in self.deployments and name not in out:
                out.append(name)
        return out


def _strip_provider_prefix(model: str) -> str:
    """`openrouter/deepseek/deepseek-v4.1-flash` -> `deepseek/deepseek-v4.1-flash`.

    The old proxy used the leading segment to pick an SDK. We only ever talk to
    OpenRouter, so it is noise -- but it stays in the file, because the file is
    the operator's and the Models page writes that form.
    """
    return model.split("/", 1)[1] if model.startswith("openrouter/") else model


def empty(path: Path, reason: str) -> Table:
    """A table with nothing in it, rather than an exception.

    A missing config is a real state, not a programming error: config.yaml is
    the operator's file and is gitignored, so it does not exist in a fresh
    checkout, in CI, or in a container before first run. Refusing to import
    without it made the module unloadable in all three -- caught by CI on
    2026-09-15, and it would have hit the Docker bundle next.

    Readiness reports `deployments: 0` and 503s, which is the honest answer:
    the service is up and cannot route anything yet.
    """
    logger.warning("no usable config at %s (%s); serving an empty table", path, reason)
    return Table(deployments={}, fallbacks={}, loaded_at=time.time(), mtime=0.0)


def load(path: Path | None = None) -> Table:
    p = Path(path or DEFAULT_CONFIG_PATH)
    try:
        text = p.read_text()
    except OSError as e:
        return empty(p, str(e))
    raw = yaml.safe_load(text) or {}
    deployments: dict[str, Deployment] = {}

    for entry in raw.get("model_list") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("model_name")
        # `params` since 2026-09-16; `litellm_params` is still read so an
        # existing operator config.yaml keeps working across the upgrade.
        params = entry.get("params") or entry.get("litellm_params") or {}
        model = params.get("model")
        if not alias or not model:
            continue
        if alias in deployments:
            # The old proxy load-balanced duplicate model_names across deployments.
            # Nothing here has relied on that since the tier pools were removed
            # (2026-09-13) -- every alias is one model. Keeping the first and
            # saying so beats silently picking one.
            logger.warning("duplicate deployment %s; keeping the first", alias)
            continue
        info = entry.get("model_info") or {}
        deployments[alias] = Deployment(
            alias=alias,
            model=_strip_provider_prefix(str(model)),
            # `max_tokens` beside `model` is the deployment's output cap (the
            # vision bridge's 1500). It was read by the old proxy and silently
            # dropped by this loader until 2026-09-24; it travels in extra_body,
            # under whatever a caller sends.
            extra_body={**({"max_tokens": int(params["max_tokens"])} if params.get("max_tokens") else {}),
                        **dict(params.get("extra_body") or {})},
            timeout_s=float(params.get("timeout") or DEFAULT_TIMEOUT_S),
            input_cost_per_token=info.get("input_cost_per_token"),
            output_cost_per_token=info.get("output_cost_per_token"),
        )

    fallbacks: dict[str, list[str]] = {}
    for rule in (raw.get("router_settings") or {}).get("fallbacks") or []:
        if isinstance(rule, dict):
            for src, targets in rule.items():
                if isinstance(targets, list):
                    fallbacks[src] = [str(t) for t in targets]

    raw_privacy = (raw.get("router_settings") or {}).get("privacy")
    # A bare `privacy: off` is YAML for false; that means off, not the default.
    privacy = "off" if raw_privacy is False else str(raw_privacy or "zdr").strip().lower()
    if privacy not in PRIVACY_MODES:
        logger.warning("router_settings.privacy %r is not one of %s; using zdr", privacy, ", ".join(PRIVACY_MODES))
        privacy = "zdr"

    st = p.stat()
    return Table(deployments=deployments, fallbacks=fallbacks,
                 loaded_at=time.time(), mtime=st.st_mtime, privacy=privacy)


class Registry:
    """Holds the current table and swaps it when the file changes.

    Constructing one never raises. The router must be able to start before its
    config exists -- a container's first boot, a fresh clone, CI -- and say so
    through /health/readiness rather than by failing to import.
    """

    def __init__(self, path: Path | None = None):
        self._path = Path(path or DEFAULT_CONFIG_PATH)
        self._lock = threading.Lock()
        self._table = load(self._path)
        logger.info("loaded %d deployments from %s", len(self._table.deployments), self._path)

    @property
    def table(self) -> Table:
        """Current table, reloading first if the file changed.

        A failed reload keeps the old table and logs. A config the operator
        broke must not take the router down with it -- the running one is known
        to work, and that is exactly the moment to keep using it.
        """
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._table          # still missing; keep serving empty
        if mtime == self._table.mtime:
            return self._table
        with self._lock:
            if mtime == self._table.mtime:      # another thread got there first
                return self._table
            try:
                fresh = load(self._path)
            except Exception as e:  # noqa: BLE001
                logger.error("config reload failed, keeping the running table: %s", e)
                # Remember the mtime anyway, or every request retries the parse.
                object.__setattr__(self._table, "mtime", mtime)
                return self._table
            logger.info("config reloaded: %d deployments", len(fresh.deployments))
            self._table = fresh
            return self._table
