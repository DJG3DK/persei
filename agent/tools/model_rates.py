"""Per-model $/token rate table, loaded once: OpenRouter's live pricing for
every model the LLM router's own config.yaml pins, with the config's own
model_info rates as the fallback.

Only used as a fallback for BudgetGuardMiddleware's cost read. The router's own
computed response_metadata["token_usage"]["cost"] is preferred when present
(it's the proxy's own exact billed cost), but that field is absent entirely
on every call that goes through agent.astream_events(..., version="v3") --
OpenAI-compatible streaming responses carry standard token counts
(usage_metadata) but not the router's extra cost annotation, regardless of
stream_usage/stream_options. This computes cost from
usage_metadata.input_tokens/output_tokens against this table instead.

Cache-aware: many OpenRouter-routed models bill a cached-prompt-token read
at a steep discount off the base input rate (confirmed against OpenRouter's
own public pricing: one pinned model's cache-read rate is roughly 1/5th its
base input rate). A long tool-calling conversation resends nearly the same
prefix on every turn, so the overwhelming majority of a later call's "input
tokens" are cache reads, not fresh ones -- treating all of them as full-price
input (as a naive per-token calculation does) can overestimate real cost by
several times on exactly the kind of long, looping conversation this exists
to catch. All three rates are taken from OpenRouter's own public,
unauthenticated pricing endpoint, once per process lifetime, because that is
what the router is actually billed; config.yaml's two base rates are the
fallback when OpenRouter is unreachable. An undated pin the catalog does not
list by name (`qwen/qwen3.8-max` vs the catalog's `qwen3.8-max-0902`) is
resolved through the per-model endpoints resource -- the miss used to fall
back to full input price on cache reads and overstated one turn 4.7x.

Even so, this is the fallback: the budget is enforced against the router's
own billed cost wherever it can be had (see router_ledger.py).
"""

import logging
from pathlib import Path
from typing import Any

import os

import httpx
import yaml

logger = logging.getLogger("tektonix")

# Repo-relative, not an absolute /home path (audit C-1): the hard-coded path
# meant the entire budget ceiling silently did not exist on any box where the
# repo lived elsewhere -- warm_rates() raised FileNotFoundError at startup
# (swallowed) and every task then died on its first model call. This file is
# agent/tools/model_rates.py, so the config is three parents up.
def _router_config_path(router: Path | None = None) -> Path:
    """The live router config, or the example when there is no live one.

    config.yaml is gitignored: the Models page rewrites it on every repin, so
    tracking it made each model change a diff, and an upgrade could overwrite
    pins the operator chose. A fresh clone therefore has only
    config.example.yaml -- and the rate table, the Models page and the
    managed-role tests all still need SOMETHING to read, or a checkout with no
    install becomes a pile of import errors. The example is that something.
    """
    override = os.environ.get("MODEL_ROUTER_CONFIG_PATH")
    if override:
        return Path(override)
    # `router` is a parameter only so a test can ask the question about a
    # directory it built, rather than monkeypatching this module's __file__ --
    # which leaks into every later test through the module-level constant.
    router = router or Path(__file__).resolve().parents[2] / "services" / "model-router"
    live = router / "config.yaml"
    return live if live.is_file() else router / "config.example.yaml"


ROUTER_CONFIG_PATH = _router_config_path()
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"

# Log when config.yaml's rate and OpenRouter's current rate disagree by more
# than this much: the config figure is decorative for OpenRouter-routed
# models (the proxy bills from OpenRouter's own usage.cost), but a stale one
# still misleads anyone reading the file.
_DRIFT_WARN_RATIO = 1.25

_rates: dict[str, dict[str, float]] | None = None
# mtime of config.yaml the cached table was built from. Pins change from the
# Models page without an agent restart (2026-09-09: agent-planner repinned to
# qwen3.8-flash, agent-cartographer to glm-flash-latest while the agent kept
# running); a table cached at startup would price the alias at the OLD model's
# rate, or not at all, until the next restart.
_rates_config_mtime: float | None = None


def _config_changed() -> bool:
    """True once config.yaml has been rewritten since the table was built.
    A table installed without a recorded mtime (tests inject one directly) is
    taken as authoritative until it is explicitly reset."""
    if _rates_config_mtime is None:
        return False
    try:
        return ROUTER_CONFIG_PATH.stat().st_mtime != _rates_config_mtime
    except OSError:
        return False


def _table() -> dict[str, dict[str, float]]:
    """The rate table, rebuilt when config.yaml has been rewritten since."""
    global _rates, _rates_config_mtime
    if _rates is None or _config_changed():
        try:
            mtime = ROUTER_CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = None
        _rates = _load_rates()
        _rates_config_mtime = mtime
    return _rates


def _pricing_of(entry: dict[str, Any]) -> dict[str, float] | None:
    """{"input", "output", "cache_read"} from one OpenRouter pricing block, or
    None when the base rates are missing or malformed. A missing cache-read
    price means "no discount", so cache reads bill at the input rate."""
    pricing: dict[str, Any] = entry.get("pricing") or {}
    try:
        rates = {"input": float(pricing["prompt"]), "output": float(pricing["completion"])}
        cache_read = pricing.get("input_cache_read")
        rates["cache_read"] = float(cache_read) if cache_read is not None else rates["input"]
        return rates
    except (KeyError, TypeError, ValueError):
        return None


def _fetch_catalog_rates() -> dict[str, dict[str, float]]:
    """{model_id: {"input", "output", "cache_read"}} per token, from
    OpenRouter's public models listing. Best-effort: an unreachable endpoint
    means an empty catalog, and every rate falls back to config.yaml."""
    try:
        resp = httpx.get(OPENROUTER_MODELS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 -- pricing metadata, never worth failing a task over
        logger.warning("OpenRouter pricing fetch failed: %s", e)
        return {}
    rates: dict[str, dict[str, float]] = {}
    for entry in data.get("data", []):
        model_id = entry.get("id")
        pricing = _pricing_of(entry)
        if model_id and pricing:
            rates[model_id] = pricing
    return rates


def _fetch_endpoint_rates(model_id: str) -> dict[str, float] | None:
    """Rates for a model id the catalog does not list under that exact name.

    OpenRouter lists dated snapshots under their dated id and serves the
    undated alias by redirect -- `qwen/qwen3.8-max` is pinned in config.yaml,
    the catalog only has `qwen/qwen3.8-max-0902`. The per-model endpoints
    resource resolves the alias. Its pricing is per serving provider; the
    dearest one is taken so the estimate errs high, never low -- the router's
    billed figure replaces the estimate anyway (see router_ledger.py).
    """
    try:
        resp = httpx.get(OPENROUTER_ENDPOINTS_URL.format(model_id=model_id), timeout=10)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json().get("data") or {}
        endpoints: list[dict[str, Any]] = data.get("endpoints") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenRouter endpoint pricing fetch failed for %s: %s", model_id, e)
        return None
    priced = [p for p in (_pricing_of(e) for e in endpoints) if p]
    if not priced:
        return None
    return {key: max(p[key] for p in priced) for key in ("input", "output", "cache_read")}


def _load_rates() -> dict[str, dict[str, float]]:
    """{model_key: {"input": ..., "output": ..., "cache_read": ...}}, keyed
    both by the raw model id the router returns in response_metadata["model_name"]
    (return_raw_model_name: true strips config.yaml's own "openrouter/"
    prefix off params.model -- mirror that here so lookups match)
    and by the config alias (pinned-role calls echo the alias, not the raw
    id -- see the alias branch below).

    Rates come from OpenRouter's live pricing when it is reachable, because
    that is what the router is actually billed: config.yaml's model_info is
    the fallback, and a drift between the two is logged so the stale figure
    gets fixed rather than trusted.
    """
    rates: dict[str, dict[str, float]] = {}
    cfg = yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
    catalog = _fetch_catalog_rates()
    drift_reported: set[str] = set()  # one warning per raw id, however many aliases pin it
    for entry in cfg.get("model_list", []):
        params: dict[str, Any] = entry.get("params") or entry.get("litellm_params") or {}
        model_info: dict[str, Any] = entry.get("model_info") or {}
        raw: str = params.get("model", "")
        stripped = raw.split("/", 1)[1] if raw.startswith("openrouter/") else raw
        input_cost = model_info.get("input_cost_per_token")
        output_cost = model_info.get("output_cost_per_token")
        if not stripped or input_cost is None or output_cost is None:
            continue
        config_rates = {"input": float(input_cost), "output": float(output_cost)}
        live = rates.get(stripped)  # the same raw id pinned under two aliases: one fetch
        if live is None:
            live = catalog.get(stripped)
            if live is None and catalog and raw.startswith("openrouter/"):
                live = _fetch_endpoint_rates(stripped)
        if live is None:
            entry_rates = {**config_rates, "cache_read": config_rates["input"]}
        else:
            entry_rates = dict(live)
            for key in ("input", "output"):
                a, b = config_rates[key], live[key]
                if a and b and max(a, b) / min(a, b) > _DRIFT_WARN_RATIO and stripped not in drift_reported:
                    drift_reported.add(stripped)
                    logger.warning(
                        "rate drift for %s: config.yaml says $%.2f/M %s, OpenRouter bills $%.2f/M -- using OpenRouter's",
                        stripped, a * 1e6, key, b * 1e6,
                    )
        rates[stripped] = entry_rates
        # Also key by the config alias (model_name): pinned-alias calls echo
        # the alias as the response's model_name -- return_raw_model_name
        # only applies to auto_router deployments. Without this, every
        # pinned-role call would fall through the raw-id lookup and be
        # costed at $0.0.
        alias = entry.get("model_name")
        if alias and alias not in rates:
            rates[alias] = entry_rates
    return rates


async def warm_rates() -> None:
    """Pre-loads the rate table (including the OpenRouter network fetch) in
    a background thread at server startup, so the first real cost estimate
    doesn't block the event loop on a synchronous network call.
    """
    import asyncio

    await asyncio.to_thread(_table)


def estimate_cost(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Returns 0.0 (not a guess in either direction) if model_name is
    missing or unrecognized -- an unknown model is a signal to add it to
    model-router/config.yaml's model_info, not a reason to estimate blind.

    `cache_read_tokens` (from usage_metadata.input_token_details.cache_read)
    is billed at its own discounted rate, not the full input rate -- see
    this module's own docstring for why that distinction matters.
    """
    if not model_name:
        return 0.0
    rate = _table().get(model_name)
    if rate is None:
        return 0.0  # best-effort caller (analytics); the budget guard uses estimate_cost_strict
    cache_read_tokens = min(cache_read_tokens, input_tokens)
    fresh_input_tokens = input_tokens - cache_read_tokens
    return (
        fresh_input_tokens * rate["input"]
        + cache_read_tokens * rate["cache_read"]
        + output_tokens * rate["output"]
    )


class UnpricedModelError(Exception):
    """A model with no rate in model-router/config.yaml was billed against a hard
    budget ceiling. For a SPEND ceiling, "unknown price" and "free" must not be
    the same value -- 200 calls at ~1.4M tokens once tracked as $0.00 against a
    $5 ceiling that never tripped (audit C-1). Raised by estimate_cost_strict so
    the budget guard fails safe instead of undercounting to zero."""


def estimate_cost_strict(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Like estimate_cost, but raises UnpricedModelError when the model has no
    known rate, so a hard budget ceiling can never be defeated by an unpriced
    model reading as $0. Callers that only want a best-effort dollar figure
    (analytics) keep using estimate_cost."""
    if model_name and _table().get(model_name) is not None:
        return estimate_cost(model_name, input_tokens, output_tokens, cache_read_tokens)
    raise UnpricedModelError(
        f"model {model_name!r} has no rate in model-router/config.yaml model_info; "
        f"add it so its spend counts against the budget ceiling"
    )
