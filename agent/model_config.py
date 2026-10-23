"""Reads and edits this agent's own model pins in model-router/config.yaml --
strictly scoped to the agent-* entries (see MANAGED_ROLES). Other entries
in that file are not this agent's to set and are never read or written
here. The unnamed fallback targets (deepseek-v4-pro, claude-haiku-4.5,
gpt-4o-mini) exist as router_settings.fallbacks destinations.

(The SIMPLE/MEDIUM/COMPLEX/REASONING tier system that used to live there was
removed on 2026-09-13 along with smart-router and reasoning-tier -- built for
OpenHands, which was archived three weeks earlier, and called once in the
router ledger's entire history.)

config.yaml is shared, heavily-commented living documentation for another
service, not something safe to round-trip through a generic YAML dump --
writing a pin change does a surgical text replace of just that one role's
`model:`/cost lines, leaving every comment and every other entry in the
file byte-for-byte untouched.
"""

import json
from pathlib import Path
import re
import subprocess
import time

import httpx
import yaml

from agent.tools.model_rates import ROUTER_CONFIG_PATH, OPENROUTER_MODELS_URL

MANAGED_ROLES = {
    "agent-planner": "Planner",
    "agent-coder": "Coder",
    "agent-coder-frontend": "Coder (Frontend)",
    "agent-coder-fallback": "Coder (Loop fallback)",
    "agent-investigator": "Investigator",
    "agent-test-writer": "Test Writer",
    "agent-verifier": "Verifier",
    "agent-summarizer": "Summarizer",
    "agent-vision": "Vision",
    "agent-consolidator": "Consolidator",
    "agent-cartographer": "Cartographer",
    "agent-classifier": "Classifier",
    "agent-planning-chat": "Planning Chat (Easy)",
    "agent-planning-chat-hard": "Planning Chat (Hard)",
    "agent-planning-chat-frontend": "Planning Chat (Frontend)",
    "agent-demo-chat": "Demo Chat (public)",
    # The commit reviewer. It is a separate service (commit-reviewer/reviewer.js)
    # and deliberately independent of the agent, but "independent" did not have to
    # mean "hardcoded and invisible" — it now routes through this router like every
    # other role, so it is pickable here and its spend is attributable.
    "agent-reviewer": "Commit Reviewer",
}


# What each role actually asks a model to DO — and what has been VERIFIED,
# rather than what a catalog claims.
#
# Three capability axes, because they fail independently:
#   tools       - the role hands the model callable tools
#   structured  - the role constrains the output shape
#   strict      - the model is FORCED into a particular response shape. Two
#                 variants exist here and both break the same models:
#                   * tools + structured output in one request  (Consolidator)
#                   * tool_choice pinned to a specific function (Reviewer)
#
# `strict` is the one that breaks models. Only the Consolidator needs it, and it
# is where every non-Gemini candidate tested has failed:
#
#   Consolidator shape (ProviderStrategy + tools, tool must actually be called),
#   probed 2026-08-25 through this router:
#     google/gemini-3.1-pro-preview  PASS
#     qwen/qwen3.8-max               FAIL  — 200 OK, silently skipped the tool,
#                                            invented an answer, claimed it used
#                                            the tool. The dangerous failure.
#     z-ai/glm-5.3                   FAIL  — 404, no endpoint takes the params
#     z-ai/glm-5.2                   FAIL  — native structured output returned
#                                            unparseable JSON
#
# Everything else is far more permissive. Verified the same day, production
# shape, tool actually invoked: all seven tool-using roles PASSED on their
# current pins — including qwen3.8-max on Investigator and Planning Chat (Hard).
# And with_structured_output defaults to json_schema (not function calling), so
# the Classifier is unconstrained too — qwen3.8-max passes it.
#
# Net: pick freely for every role except the Consolidator.
STRICT_VERIFIED_OK = ["google/gemini-3.1-pro-preview", "anthropic/claude-sonnet-5",
                      "anthropic/claude-haiku-4.5"]
STRICT_VERIFIED_BAD = ["qwen/qwen3.8-max", "z-ai/glm-5.3", "z-ai/glm-5.2"]

ROLE_REQUIREMENTS: dict[str, dict] = {
    "agent-planner": {
        "tools": True, "structured": False, "strict": False,
        "note": "Plans the task. Tools, no output-shape constraint. Any tool-capable model.",
    },
    "agent-coder": {
        "tools": True, "structured": False, "strict": False,
        "note": "The main work loop — file edits, shell, checks. Wants strong tool calling above all.",
    },
    "agent-coder-frontend": {
        "tools": True, "structured": False, "strict": False,
        "note": "The coder seat for frontend work (route: category ui-styling, mostly-frontend paths, "
                "or the operator's toggle -- agent/frontend_route.py). Also serves as that task's "
                "investigator; the test-writer stays on its own pin. Pinned for polish, not price.",
    },
    "agent-coder-fallback": {
        "tools": True, "structured": False, "strict": False,
        "note": "Takes over a pass when the loop guard ends it: the regular coder kept repeating "
                "one refused call. Also serves that pass's investigator, test-writer and verifier, "
                "and, at low reasoning effort, every seat's one retry of an empty length-capped "
                "reply (agent/middleware/empty_reply.py). A different model is the point -- pin "
                "something other than the coder.",
    },
    "agent-investigator": {
        "tools": True, "structured": False, "strict": False,
        "note": "Read-only subagent: no write or shell tools at all. Any tool-capable model.",
    },
    "agent-test-writer": {
        "tools": True, "structured": False, "strict": False,
        "note": "Writes tests and must run the checks itself before reporting done.",
    },
    "agent-verifier": {
        "tools": True, "structured": False, "strict": False,
        "note": "Tries to break a bug fix before it ships: runs the reported case and its "
                "neighbours, edits nothing. Its own seat since 2026-09-25 so its calls are not "
                "billed as the test-writer's. A different model from the coder is the point.",
    },
    "agent-summarizer": {
        "tools": False, "structured": False, "strict": False,
        "note": "Plain completion. No tools — the cheapest tier is genuinely fine here.",
    },
    "agent-vision": {
        "tools": False, "structured": False, "strict": False,
        "note": "Image description only. Needs vision; no tools.",
    },
    "agent-consolidator": {
        "tools": True, "structured": True, "strict": True,
        "note": "⚠ The one constrained role. Sends tools AND structured output in the same "
                "request. Verified working: gemini-3.1-pro-preview. Verified FAILING: "
                "qwen *-max (silently skips the tool and invents an answer), glm-5.3 (404), "
                "glm-5.2 (unparseable JSON). Change this pin only against a probe.",
    },
    "agent-cartographer": {
        "tools": False, "structured": False, "strict": False,
        "note": "Builds the per-project codebase map from a deterministic inventory this "
                "code gathers first — it reads no files itself, so no tools and no output "
                "shape are required. Wants a large context window (the inventory for a big "
                "repo is long) and good summarisation, not tool skill. Runs on a schedule, "
                "once per repo, so a mid-tier model is the sensible default.",
    },
    "agent-classifier": {
        "tools": False, "structured": True, "strict": False,
        "note": "Structured output only, no tools. with_structured_output defaults to "
                "json_schema rather than function calling, so this is NOT constrained — "
                "verified working even on models that reject forced tool calls.",
    },
    "agent-planning-chat": {
        "tools": True, "structured": False, "strict": False,
        "note": "Interactive planning chat. Tools including describe_image.",
    },
    "agent-planning-chat-hard": {
        "tools": True, "structured": False, "strict": False,
        "note": "Escalation tier for the same chat. Same needs as the easy tier.",
    },
    "agent-planning-chat-frontend": {
        "tools": True, "structured": False, "strict": False,
        "note": "Planning chat for frontend sessions, ahead of the EASY/HARD ladder. Same needs as "
                "the other two tiers; pinned to the model that will build the plan.",
    },
    "agent-demo-chat": {
        "tools": True, "structured": False, "strict": False,
        "note": "Public portfolio bot — web search and fetch. Keep it cheap; it is exposed.",
    },
    "agent-reviewer": {
        "tools": True, "structured": False, "strict": True,
        "note": "Reads a diff and files findings via a single submit_review tool call. "
                "Not agentic (one call, no loop) and it never writes or runs code — the "
                "mechanical checks run in Node first. Needs instruction-following and "
                "judgement above all: its failure mode is FALSE POSITIVES, and a wrong "
                "blocking finding costs a full fix-and-review round. Long context too "
                "(diff + commit log + gathered test/reference files). A coding-tuned "
                "model is the wrong instinct here. ⚠ Marked strict: it pins tool_choice "
                "to submit_review, the same forced shape that Qwen *-max and GLM reject. "
                "claude-sonnet-5 verified passing 2026-08-25.",
    },
}


# Live probe results, written by scripts/probe_forced_tool_call.py.
#
# This is a cache of REAL requests, not a reading of OpenRouter's catalog --
# because the catalog cannot answer the question. qwen3.8-max advertises tools,
# tool_choice, structured_outputs, response_format and reasoning, byte-identical
# to gemini-3.1-pro-preview which works, and its per-endpoint data claims the
# same. supported_parameters is a flat union across providers, so it can say
# "supports tool_choice" and "supports reasoning" while refusing the two
# TOGETHER. Only an actual request settles it.
FORCED_TOOL_PROBE_PATH = Path(__file__).resolve().parent.parent / "data" / "forced_tool_call_probe.json"


def load_forced_tool_probe() -> dict:
    """{"probed_at": iso, "models": {id: {"status": "ok"|"fail"|"error", ...}}}"""
    try:
        return json.loads(FORCED_TOOL_PROBE_PATH.read_text())
    except Exception:  # noqa: BLE001 -- no readable probe file means nothing probed yet
        return {"probed_at": None, "models": {}}


def forced_tool_call_stats() -> dict:
    """Full probe breakdown, not just pass/fail.

    Reporting only "122 passed - 34 failed" made the probe look like it had
    covered 156 models when it had actually attempted 218, and made 122 read as
    the size of the whole catalogue rather than the number of models that passed
    one specific test. The other two verdicts matter and are different from each
    other: `unavailable` models cannot be probed at all (batch-only endpoints,
    or no provider serving them under this account's data policy) and never will
    be, while `transient` means the probe itself was rate-limited and the model
    deserves another attempt.
    """
    data = load_forced_tool_probe()
    models = data.get("models") or {}
    by_status: dict[str, list[str]] = {}
    for model_id, v in models.items():
        by_status.setdefault(v.get("status") or "unknown", []).append(model_id)
    return {
        "probed_at": data.get("probed_at"),
        "attempted": len(models),
        "compliant": sorted(by_status.get("ok", [])),
        "non_compliant": sorted(by_status.get("fail", [])),
        "unavailable": sorted(by_status.get("unavailable", [])),
        "transient": sorted(by_status.get("transient", [])),
    }


class UnknownRoleError(Exception):
    pass


class ModelNotInCatalogError(Exception):
    pass


class PinBlockNotFoundError(Exception):
    pass


def _openrouter_key() -> str | None:
    """The OpenRouter key lives in the ROUTER's .env (single source of truth;
    the agent's own .env deliberately doesn't duplicate it). Authenticated
    calls to the endpoints API get real latency/throughput percentiles --
    anonymous calls return those fields as null."""
    import os
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    env_path = ROUTER_CONFIG_PATH.parent / ".env"
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _p50(stat) -> float | None:
    """OpenRouter reports latency/throughput as percentile dicts ({p50, p75,
    p90, p99}); p50 is the honest single number for a picker row."""
    if isinstance(stat, dict):
        return stat.get("p50")
    return stat if isinstance(stat, (int, float)) else None


_catalog_cache: dict = {"at": 0.0, "data": None}
_CATALOG_TTL_S = 600


async def fetch_model_catalog(force: bool = False) -> list[dict]:
    """OpenRouter's live public model catalog -- id, display name, and
    per-token pricing -- for the dashboard's model picker. Cached for
    _CATALOG_TTL_S since this is a large catalog and the picker doesn't need
    up-to-the-second freshness.
    """
    now = time.time()
    if not force and _catalog_cache["data"] is not None and now - _catalog_cache["at"] < _CATALOG_TTL_S:
        return _catalog_cache["data"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        data = resp.json()

    catalog = []
    for entry in data.get("data", []):
        model_id = entry.get("id")
        if not model_id:
            continue
        pricing = entry.get("pricing") or {}
        try:
            input_cost = float(pricing.get("prompt", 0) or 0)
            output_cost = float(pricing.get("completion", 0) or 0)
        except (TypeError, ValueError):
            input_cost = output_cost = 0.0
        # Quality metrics for educated pin-picking (operator ask 2026-08-28).
        # OpenRouter's `benchmarks.design_arena` carries crowd-arena ELO/rank
        # per category; the AGENTS categories are the closest proxy for what
        # this stack does (tool-calling build loops), so surface the model's
        # BEST agents-category entry plus knowledge cutoff. These are
        # design-flavored arenas, not code-correctness suites -- a compass,
        # not a verdict (deepseek ranks #41 there while running 3,261 coder
        # calls here without a failure).
        agents = [b for b in ((entry.get("benchmarks") or {}).get("design_arena") or [])
                  if b.get("arena") == "agents" and b.get("elo") is not None]
        best = max(agents, key=lambda b: b["elo"], default=None)
        catalog.append({
            "id": model_id,
            "name": entry.get("name") or model_id,
            "context_length": entry.get("context_length"),
            "knowledge_cutoff": entry.get("knowledge_cutoff"),
            "arena": {
                "category": best.get("category"), "elo": best.get("elo"),
                "rank": best.get("rank"), "win_rate": best.get("win_rate"),
            } if best else None,
            "input_cost_per_token": input_cost,
            "output_cost_per_token": output_cost,
        })
    catalog.sort(key=lambda m: m["name"].lower())
    _catalog_cache["data"] = catalog
    _catalog_cache["at"] = now
    return catalog


_ENDPOINTS_CACHE: dict[str, tuple[float, list]] = {}
_ENDPOINTS_TTL_S = 600


async def fetch_model_endpoints(model_id: str) -> list[dict]:
    """The providers actually serving one model, from OpenRouter's endpoints
    API -- the provider names here are what `provider.only` accepts. Cached
    10 minutes per model; failures raise (the caller shows the error, an
    empty list would read as "no providers exist")."""
    import time as _time
    hit = _ENDPOINTS_CACHE.get(model_id)
    if hit and _time.monotonic() - hit[0] < _ENDPOINTS_TTL_S:
        return hit[1]
    import httpx
    key = _openrouter_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://openrouter.ai/api/v1/models/{model_id}/endpoints", headers=headers
        )
        r.raise_for_status()
    endpoints = []
    for e in (r.json().get("data") or {}).get("endpoints") or []:
        pricing = e.get("pricing") or {}
        endpoints.append({
            "provider": e.get("provider_name") or e.get("name"),
            "context_length": e.get("context_length"),
            "input_cost_per_token": float(pricing.get("prompt") or 0),
            "output_cost_per_token": float(pricing.get("completion") or 0),
            "quantization": e.get("quantization"),
            "uptime": e.get("uptime_last_30m"),
            # The three numbers that pick a provider for THIS stack: how fast
            # it answers, whether it stays up, and whether repeat context
            # bills at cache rates without any plumbing.
            # p50 over the last 30m; latency arrives in ms, shown as s.
            "latency_s": (lambda v: round(v / 1000, 2) if v is not None else None)(
                _p50(e.get("latency_last_30m"))
            ),
            "throughput_tps": _p50(e.get("throughput_last_30m")),
            "implicit_caching": bool(e.get("supports_implicit_caching")),
        })
    _ENDPOINTS_CACHE[model_id] = (_time.monotonic(), endpoints)
    return endpoints


async def get_current_pins_priced() -> dict[str, dict]:
    """get_current_pins with prices taken from OpenRouter's LIVE catalog.

    The per-role `model_info` blocks in model-router/config.yaml are written by
    hand and drift silently: on 2026-08-26 agent-coder's block still claimed
    $1.60/M input for deepseek-v4-pro, which by then actually cost $0.56/M --
    the dashboard was reporting a model as ~3x more expensive than it was, which
    is exactly the number someone picks a model on.

    The catalog is the authority for price. model_info stays as the fallback for
    anything the catalog does not list (a pin that is not an OpenRouter id), so
    a missing entry degrades to the old behaviour rather than to "free".
    """
    pins = get_current_pins()
    try:
        catalog = {c["id"]: c for c in await fetch_model_catalog()}
    except Exception:  # noqa: BLE001 -- a catalog outage must not break the page
        return pins
    for pin in pins.values():
        live = catalog.get(pin.get("model"))
        if not live:
            pin["price_source"] = "config.yaml (not in catalog)"
            continue
        pin["input_cost_per_token"] = live["input_cost_per_token"]
        pin["output_cost_per_token"] = live["output_cost_per_token"]
        pin["context_length"] = live.get("context_length")
        pin["price_source"] = "openrouter"
    return pins


_PINS_CACHE: dict = {"key": None, "pins": None}


def get_current_pins() -> dict[str, dict]:
    """{role: {"label", "model", "input_cost_per_token", "output_cost_per_token"}}
    for every managed role currently present in config.yaml. Read-only, so a
    plain yaml.safe_load is fine here -- only writes need the surgical
    text-level approach (see set_pins's own docstring).

    audit H-22: cached on the config file's (mtime, size). resolve_alias calls
    this per streamed chat message, and the uncached version re-read and
    re-parsed the whole YAML every time. The staleness concern the old
    resolve_alias docstring guarded (a pin changed via set_pins must be seen
    immediately, with no restart) is preserved exactly: set_pins rewrites the
    file, which bumps its mtime, which invalidates this cache on the next call.
    """
    try:
        st = ROUTER_CONFIG_PATH.stat()
        # path in the key too: tests swap ROUTER_CONFIG_PATH between temp
        # files, and two distinct files could otherwise collide on (mtime, size).
        cache_key = (str(ROUTER_CONFIG_PATH), st.st_mtime_ns, st.st_size)
        if _PINS_CACHE["pins"] is not None and _PINS_CACHE["key"] == cache_key:
            return _PINS_CACHE["pins"]
    except OSError:
        cache_key = None
    cfg = yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
    pins: dict[str, dict] = {}
    for entry in cfg.get("model_list", []):
        name = entry.get("model_name")
        if name not in MANAGED_ROLES:
            continue
        params = entry.get("params") or entry.get("litellm_params") or {}
        info = entry.get("model_info") or {}
        raw = params.get("model", "")
        model_id = raw.split("/", 1)[1] if raw.startswith("openrouter/") else raw
        _provider_pref = ((params.get("extra_body") or {}).get("provider") or {})
        _only = _provider_pref.get("only") or []
        pins[name] = {
            "label": MANAGED_ROLES[name],
            "model": model_id,
            # Provider pin (dashboard-editable, 2026-08-28): OpenRouter routes
            # each call across a pool of providers unless told otherwise --
            # None means auto.
            "provider": _only[0] if _only else None,
            "input_cost_per_token": info.get("input_cost_per_token"),
            "output_cost_per_token": info.get("output_cost_per_token"),
            # Surfaced in the dashboard so a model is chosen against what the
            # role actually needs, not just price/quality.
            **(ROLE_REQUIREMENTS.get(name) or {}),
            "strict_ok": STRICT_VERIFIED_OK,
            "strict_bad": STRICT_VERIFIED_BAD,
        }
    _PINS_CACHE.update(key=cache_key, pins=pins)
    return pins


def resolve_alias(alias: str | None) -> str | None:
    """alias -> real underlying model id, e.g. "agent-investigator" ->
    "qwen/qwen3.8-max". A pinned role's response always echoes back the bare
    alias (return_raw_model_name only applies to the shared auto_router
    deployment, not these entries -- see fetch_model_catalog's own module
    docstring), so this is the only way the dashboard ever learns which real
    model actually answered a turn.

    Backed by get_current_pins's mtime-keyed cache (audit H-22). Correctness
    is unchanged from the old fresh-read-every-call behaviour: an operator
    changing a pin through set_pins() rewrites config.yaml, which bumps its
    mtime and invalidates the cache on the very next call -- so a swap is seen
    immediately, with no restart, but repeated reads of an unchanged file no
    longer re-parse the whole YAML per streamed message.
    """
    if not alias:
        return alias
    pins = get_current_pins()
    if alias in pins:
        return pins[alias]["model"]
    return alias


def _format_rate(value: float) -> str:
    """Decimal (never scientific) notation matching this file's existing
    style, e.g. 1.4e-06 -> "0.0000014" -- config.yaml is hand-read by
    whoever maintains it, and every existing rate in the file is written
    this way.
    """
    return f"{value:.15f}".rstrip("0").rstrip(".")


def _block_pattern(role: str) -> re.Pattern:
    # Matches the exact, established agent-* entry shape: a `- model_name:`
    # line, then its params block (allowing any comment lines in between),
    # then the model: and api_key: lines, then model_info's two cost lines.
    # Scoped to one role by exact name, so it can never match a different
    # entry (including another role whose name is a prefix of this one).
    #
    # audit H-5: the intervening-lines groups are TEMPERED -- they match any
    # line EXCEPT the start of another entry (`  - model_name:`), so the lazy
    # match can never walk past this role's block into a DIFFERENT role's
    # model_info. The old `(?:.*\n)*?` did exactly that when a block's shape
    # differed (agent-cartographer matched claude-haiku-4.5's cost lines),
    # silently repricing two other roles on a single repin.
    not_entry = r"(?:(?!  - model_name:).*\n)*?"
    return re.compile(
        r"(?P<head>  - model_name: " + re.escape(role) + r"\n"
        + not_entry +
        r"      model: )(?P<model>\S+)(?P<mid1>\n"
        r"      api_key: os\.environ/OPENROUTER_API_KEY\n"
        + not_entry +
        r"    model_info:\n"
        + not_entry +
        r"      input_cost_per_token: )(?P<input_cost>\S+)(?P<mid2>\n"
        r"      output_cost_per_token: )(?P<output_cost>\S+)"
    )


# ---------------------------------------------------------------------------
# Family extras -- deployment params that belong to the MODEL FAMILY, not the
# role, and must follow the pin (operator requirement 2026-08-28, after a
# dashboard repin from Sonnet to qwen kept Sonnet's extras: the leftover
# additional_drop_params silently stripped temperature=0 from every call).
#
# Anthropic (claude-*) models:
#   - REJECT sampling params outright (temperature/top_p/top_k are a 400 on
#     Sonnet 5+), and llm_for_role sends temperature=0 on every role -- with
#     require_parameters, OpenRouter then refuses to route AT ALL and the
#     role silently lives on its fallback (seen live 2026-08-27).
#   - cache nothing without explicit cache_control breakpoints; the injection
#     points are what make an Anthropic pin economically sane.
# Everything else: BOTH extras must be absent -- dropping params un-pins
# temperature, and the cache injection is Anthropic-specific dead weight.
# ---------------------------------------------------------------------------

_ANTHROPIC_EXTRAS = (
    '      # [managed] family extras for an Anthropic pin -- auto-added by the\n'
    '      # dashboard editor; auto-removed when this role is repinned off\n'
    '      # Anthropic. Do not hand-edit: see _normalize_family_extras.\n'
    '      additional_drop_params: ["temperature", "top_p", "top_k"]\n'
    '      cache_control_injection_points:\n'
    '        - location: message\n'
    '          role: system\n'
    '        - location: message\n'
    '          index: -1\n'
)

def _strip_family_extras(block: str) -> str:
    """Drop the managed extras from a role block, line by line: the
    `additional_drop_params` line (with the [managed] comment block right
    above it, when present) and `cache_control_injection_points` with its
    indented body. Line-based rather than a regex: the pattern this replaced
    was flagged for polynomial backtracking on repeated comment lines
    (CodeQL py/polynomial-redos, 2026-09-10)."""
    lines = block.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("      # [managed]"):
            j = i + 1
            while j < len(lines) and lines[j].startswith("      #"):
                j += 1
            if j < len(lines) and lines[j].startswith("      additional_drop_params:"):
                i = j + 1          # comment block + the line it introduces
                continue
            out.append(line)
            i += 1
            continue
        if line.startswith("      additional_drop_params:"):
            i += 1
            continue
        if line.startswith("      cache_control_injection_points:"):
            i += 1
            while i < len(lines) and lines[i].startswith("        "):
                i += 1
            continue
        out.append(line)
        i += 1
    return "".join(out)


def _end_of_first_line_starting(block: str, prefix: str) -> int | None:
    """Index just past the first line that starts with `prefix`, or None."""
    pos = 0
    for line in block.splitlines(keepends=True):
        if line.startswith(prefix):
            return pos + len(line)
        pos += len(line)
    return None


def _is_anthropic(model_id: str) -> bool:
    return model_id.startswith("anthropic/") or model_id.startswith("openrouter/anthropic/")


_CLAUDE_VERSION = re.compile(r"claude-(?:opus|sonnet|haiku)-(\d+(?:\.\d+)?)")


def _requires_family_extras(model_id: str) -> bool:
    """True for the Anthropic generations that REJECT sampling params
    (temperature/top_p/top_k are a 400): the 4.6+ line, the 5s, and
    fable/mythos. Older Anthropic models (haiku-4.5, opus-4.5, 3.x) accept
    them -- the reviewer's haiku-4.5 pin was the first thing the live-config
    guard flagged, and force-dropping ITS temperature would be the same
    un-pinning bug this guard exists to prevent, in the other direction."""
    if not _is_anthropic(model_id):
        return False
    if "fable" in model_id or "mythos" in model_id:
        return True
    m = _CLAUDE_VERSION.search(model_id)
    return bool(m) and float(m.group(1)) >= 4.6


def _normalize_family_extras(block: str, model_id: str) -> str:
    """One role's config block, extras made to match the pinned model's
    family. Pure text-in/text-out so tests can pin every direction."""
    block = _strip_family_extras(block)
    if _requires_family_extras(model_id):
        i = _end_of_first_line_starting(block, "      extra_body:")
        if i is None:
            i = _end_of_first_line_starting(block, "      api_key:")
        if i is not None:
            block = block[:i] + _ANTHROPIC_EXTRAS + block[i:]
    return block


# The first line that starts a new TOP-LEVEL key (column 0, `name:`), which is
# where model_list ends whatever follows it.
_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:", re.MULTILINE)


def _role_block_span(text: str, role: str) -> tuple[int, int]:
    """Where this role's entry begins and ends.

    The end used to be hard-coded to `\nlitellm_settings:`. That was wrong in
    two ways at once: it named a block that has now been deleted, and even
    while it existed `router_settings:` sat between it and model_list, so the
    LAST role's span swallowed an unrelated top-level block. Finding the next
    top-level key is correct regardless of which blocks a config carries.
    """
    start = text.index(f"  - model_name: {role}\n")
    nxt = text.find("  - model_name:", start + 1)
    if nxt != -1:
        return start, nxt
    m = _TOP_LEVEL_KEY.search(text, start)
    return start, (m.start() if m else len(text))


def _atomic_write_config(text: str) -> None:
    """Temp file + rename in the same dir -- a reader never sees a partial
    config and a crash mid-write can't truncate it."""
    import os
    import tempfile
    d = str(ROUTER_CONFIG_PATH.parent)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes the file 0600. In the bundle the router reads this
        # file as its own unprivileged user while the agent writes it, so
        # world-readable -- it holds pins and rates, never a key (the
        # example config says `api_key: os.environ/...` for that reason).
        os.chmod(tmp, 0o644)
        os.replace(tmp, ROUTER_CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# The canonical extra_body forms. require_parameters is non-negotiable on
# every managed pin (see the pins' own comments); a provider pin adds
# only+allow_fallbacks:false -- a pin the pool can silently ignore is not a
# pin. Written as exact single-line JSON-in-YAML, matching every existing
# block, so the surgical regex below stays trivial.
_EXTRA_BODY_LINE = re.compile(r"^      extra_body: .*$", re.M)


def _extra_body_for(provider: str | None) -> str:
    if provider:
        return ('      extra_body: {"provider": {"require_parameters": true, '
                f'"only": ["{provider}"], "allow_fallbacks": false}}}}')
    return '      extra_body: {"provider": {"require_parameters": true}}'


class ProviderPinError(Exception):
    pass


def set_provider_pins(new_pins: dict[str, str | None]) -> dict[str, str | None]:
    """Pin (or clear, with None) the OpenRouter PROVIDER a role's model is
    served by. Dashboard-driven, same contract as set_pins: surgical text
    edit, managed roles only, yaml re-parse verification before an atomic
    write, router restart required to take effect."""
    text = ROUTER_CONFIG_PATH.read_text()
    for role, provider in new_pins.items():
        if role not in MANAGED_ROLES:
            raise UnknownRoleError(f"{role!r} is not a role this dashboard manages")
        if provider is not None and not re.fullmatch(r"[A-Za-z0-9 ._/-]{1,64}", provider):
            raise ProviderPinError(f"{provider!r} is not a plausible provider name")
        b0, b1 = _role_block_span(text, role)
        block = text[b0:b1]
        if not _EXTRA_BODY_LINE.search(block):
            raise ProviderPinError(f"{role!r}'s block has no extra_body line to edit")
        block = _EXTRA_BODY_LINE.sub(_extra_body_for(provider), block, count=1)
        text = text[:b0] + block + text[b1:]
    # verify before writing, same discipline as set_pins
    parsed = yaml.safe_load(text)
    by_name = {e.get("model_name"): (e.get("params") or e.get("litellm_params") or {}) for e in parsed.get("model_list", [])}
    for role, provider in new_pins.items():
        pref = ((by_name.get(role) or {}).get("extra_body") or {}).get("provider") or {}
        got = (pref.get("only") or [None])[0]
        if got != provider or pref.get("require_parameters") is not True:
            raise ProviderPinError(
                f"post-edit verification failed for {role!r}: wanted provider {provider!r}, "
                f"parsed {got!r} -- refusing to write a corrupted config")
    _atomic_write_config(text)
    return dict(new_pins)


def set_pins(new_pins: dict[str, str], catalog: list[dict]) -> dict[str, dict]:
    """Surgically replaces the model:/cost lines for each requested role,
    leaving every comment, every other role, and every non-agent-* entry in
    config.yaml byte-for-byte untouched. Only ever touches roles in
    MANAGED_ROLES -- an unrecognized role name raises rather than silently
    doing nothing, since this is the one thing standing between "edit our
    own pins" and "edit a file another service depends on."

    Real per-token pricing for the new model is looked up from `catalog`
    (OpenRouter's own live pricing) and written into model_info alongside
    the model change, so BudgetGuard's cost fallback (agent/tools/
    model_rates.py) stays accurate for the new model automatically instead
    of silently keeping the old model's rates.
    """
    catalog_by_id = {m["id"]: m for m in catalog}
    text = ROUTER_CONFIG_PATH.read_text()
    changed: dict[str, dict] = {}

    for role, model_id in new_pins.items():
        if role not in MANAGED_ROLES:
            raise UnknownRoleError(f"{role!r} is not a role this dashboard manages")
        priced = catalog_by_id.get(model_id)
        if priced is None:
            raise ModelNotInCatalogError(f"{model_id!r} was not found in the OpenRouter catalog")

        match = _block_pattern(role).search(text)
        if not match:
            raise PinBlockNotFoundError(f"could not locate {role!r}'s entry in config.yaml")

        replacement = (
            match.group("head") + f"openrouter/{model_id}"
            + match.group("mid1") + _format_rate(priced["input_cost_per_token"])
            + match.group("mid2") + _format_rate(priced["output_cost_per_token"])
        )
        text = text[: match.start()] + replacement + text[match.end():]
        # Family extras follow the pin -- see _normalize_family_extras. A
        # model change also RESETS any provider pin: the provider was chosen
        # for the old model and OpenRouter would refuse to route a model the
        # pinned provider doesn't serve.
        b0, b1 = _role_block_span(text, role)
        block = _normalize_family_extras(text[b0:b1], model_id)
        block = _EXTRA_BODY_LINE.sub(_extra_body_for(None), block, count=1)
        text = text[:b0] + block + text[b1:]
        changed[role] = {
            "label": MANAGED_ROLES[role],
            "model": model_id,
            "input_cost_per_token": priced["input_cost_per_token"],
            "output_cost_per_token": priced["output_cost_per_token"],
        }

    # audit H-5: re-parse the edited text and confirm every role we intended to
    # change now actually carries the intended model, BEFORE writing. A regex
    # that matched the wrong block (the bug this fix closes) would otherwise
    # silently reprice a different role; this turns that into a loud failure with
    # the file left untouched.
    parsed = yaml.safe_load(text)
    parsed_models = {}
    for entry in parsed.get("model_list", []):
        name = entry.get("model_name")
        raw = (entry.get("params") or entry.get("litellm_params") or {}).get("model", "")
        parsed_models[name] = raw.split("/", 1)[1] if raw.startswith("openrouter/") else raw
    parsed_params = {e.get("model_name"): (e.get("params") or e.get("litellm_params") or {}) for e in parsed.get("model_list", [])}
    for role, model_id in new_pins.items():
        if parsed_models.get(role) != model_id:
            raise PinBlockNotFoundError(
                f"post-edit verification failed for {role!r}: expected {model_id!r}, "
                f"got {parsed_models.get(role)!r} -- refusing to write a corrupted config"
            )
        lp = parsed_params.get(role) or {}
        has_extras = "additional_drop_params" in lp and "cache_control_injection_points" in lp
        wants_extras = _requires_family_extras(model_id)
        if has_extras != wants_extras:
            raise PinBlockNotFoundError(
                f"post-edit verification failed for {role!r}: family extras "
                f"{'missing for an Anthropic pin' if wants_extras else 'left behind on a non-Anthropic pin'} "
                f"-- refusing to write a corrupted config"
            )

    _atomic_write_config(text)
    return changed


# How long to wait for the router to answer again after a restart. The router
# loads a large config and opens a Prisma connection at boot, so a few seconds
# is normal; past this it is worth telling the operator rather than spinning.
_ROUTER_BOOT_WAIT_S = 25.0


def restart_llm_router(base_url: str | None = None) -> dict:
    """Restarts the model router pm2 process so a pin change actually
    takes effect -- the router reloads config.yaml on change, but a restart is
    no hot-reload. Hardcoded process name, no caller-supplied value ever
    reaches this command: this is the one thing standing between "restart
    our own dependency" and an arbitrary-process-restart primitive exposed
    over HTTP. Affects every consumer of the model router (the review
    service, this agent), not just this agent -- callers should treat this
    as a real, shared-impact action, not a routine save side effect.

    Then it WAITS for the router to answer its own liveness route, and
    reports that.

    pm2's exit code alone is the wrong answer to "did it restart?". It says
    whether the restart command was accepted, not whether the thing came
    back -- and the two disagree in both directions: pm2 can print a warning
    and exit non-zero while the app restarts perfectly (the operator sees a
    failure, and their only evidence to the contrary is a Telegram alert), or
    exit zero while the router dies on a bad config and nothing answers. The
    caller's dialog is only as honest as this return value.
    """
    result = subprocess.run(
        ["pm2", "restart", "model-router"], capture_output=True, text=True, timeout=30
    )
    output = (result.stdout + result.stderr)[-2000:]

    url = _router_liveness_url(base_url)
    healthy, waited = _wait_for_router(url) if url else (None, 0.0)

    return {
        # Healthy wins over pm2's exit code in both directions: the router
        # answering IS the restart having worked.
        "ok": bool(healthy) if healthy is not None else result.returncode == 0,
        "restarted": result.returncode == 0,
        "healthy": healthy,
        "waited_s": round(waited, 1),
        "output": output,
    }


def _router_liveness_url(base_url: str | None) -> str | None:
    from agent.config import load_config  # noqa: PLC0415 -- avoids an import cycle
    from agent.health import router_liveness_url  # noqa: PLC0415

    try:
        return router_liveness_url(base_url or load_config().router_base_url)
    except Exception:  # noqa: BLE001 -- a missing base URL must not fail the restart
        return None


def _wait_for_router(url: str, limit_s: float = _ROUTER_BOOT_WAIT_S) -> tuple[bool, float]:
    """(answering, seconds waited). Polls rather than sleeping a fixed time:
    a router that comes back in two seconds should not make the operator
    stare at a spinner for twenty."""
    started = time.monotonic()
    while True:
        waited = time.monotonic() - started
        try:
            if httpx.get(url, timeout=3.0).status_code == 200:
                return True, waited
        except Exception:  # noqa: BLE001 -- still booting
            pass
        if waited >= limit_s:
            return False, waited
        time.sleep(1.0)
