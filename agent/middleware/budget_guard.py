"""BudgetGuardMiddleware — the primary, code-enforced hard $ ceiling.

Enforces the budget with a check after every individual model call, inside
the model-call itself — the tightest enforcement point deepagents exposes
(`awrap_model_call`), a Python exception propagating out of the call rather
than a text instruction the model can ignore. This is the primary defense;
the outer `work` node's own watchdog over the whole astream() is a secondary
backstop, not a substitute.

Must be attached to the coordinator's own `middleware=[...]` and to every
subagent's own `middleware=[...]` list individually -- a SubAgent spec's
`middleware` list is merged in by name (replace-if-name-matches, else
append), never inherited by default. This does not mean subagents otherwise
run with an empty middleware stack -- deepagents auto-prepends its own fresh
FilesystemMiddleware/SubAgentMiddleware/summarization/PatchToolCallsMiddleware/
prompt-caching to every subagent independently of anything in `spec[
"middleware"]`. What's true, and the actual reason this class exists, is
narrower: our custom middleware specifically (this one) is never
auto-attached to a subagent just because it's on the coordinator -- that one
has to be listed explicitly per subagent, or that subagent spends
completely unmetered against the shared budget.

Cost is the router's own billed figure wherever it can be had, and an
estimate only until then. The router's exact `response_metadata["token_usage"]
["cost"]` is used when present, but it never is once a model call goes
through `agent.astream_events(..., version="v3")`: that API always registers
a `.messages` native projection, which forces every model call into a real
token stream, and the router's extra cost annotation does not survive
OpenAI-compatible SSE streaming no matter what `stream_usage`/`stream_options`
are set (the standard `usage_metadata` token counts come through fine with
`stream_usage=True` on `llm_for_role()`). So each call is first charged at
`agent/tools/model_rates.estimate_cost()` -- real token counts against live
OpenRouter rates, cache-aware -- and tagged with the `x-router-call-id` the
proxy returned in the response headers (`include_response_headers=True` on
`llm_for_role()`). The router logs that same id with OpenRouter's actual
`usage.cost` in logs/routing.jsonl as soon as the call completes, and
`BudgetTracker` swaps the estimate for the billed cost on its next read
(`agent/tools/router_ledger.py`). The ceiling is therefore enforced against
the router's own number for every call except the one that just finished --
and when THAT one's estimate would trip the ceiling, the guard waits up to
two seconds for its billed figure before ending a turn on an estimate. An
estimate 4.7x too high did exactly that on 2026-09-08 ($8.09 "spent" against
$1.72 billed), which is why the estimate is no longer the last word.

Shared tracker, not per-instance state: a BudgetGuardMiddleware that held
its own running total would let the ceiling be blown well past its intended
value, since coordinator and every subagent each get their own middleware
instance (see above) -- if each tracked independently, the coordinator could
spend up to budget_usd and each subagent could also independently spend up
to budget_usd, for a true aggregate of budget_usd * (1 + subagent_count),
not budget_usd. `BudgetTracker` is the one shared mutable object every
middleware instance for a given task invocation must wrap, so the ceiling is
enforced against the real aggregate spend across the coordinator and every
subagent call combined. Safe without a lock because every update here is a
synchronous step on the one event loop: nothing awaits between reading and
writing the totals. It is NOT because calls are serial -- the coordinator
can put several `task` calls in one message and LangGraph runs them
concurrently, so parallel branches are in flight together (2026-09-29
audit, A5). Each branch checks the ceiling before its own call, so an
overshoot is bounded by one call per parallel branch, never unbounded.
"""

import asyncio
import logging
import time

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.callbacks import AsyncCallbackHandler

from agent.tools.model_rates import UnpricedModelError, estimate_cost_strict
from agent.tools.router_ledger import RouterLedger

logger = logging.getLogger("tektonix")

# How long the guard will wait for the router to bill the call that just
# finished before it ends a turn on that call's ESTIMATE. The router logs a
# completed call within milliseconds of the stream closing, so this is
# rarely reached; it exists so a slow disk cannot hang a turn.
SETTLE_TIMEOUT_S = 2.0
SETTLE_POLL_S = 0.1

# The router sets this on every response, streaming included, and logs
# the same value with the call's billed cost (services/model-router/router/app.py).
CALL_ID_HEADER = "x-router-call-id"


class BudgetExceededError(Exception):
    def __init__(self, spent: float, budget: float, detail: str | None = None):
        msg = f"budget exceeded: ${spent:.4f} spent against a ${budget:.2f} ceiling"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.spent = spent
        self.budget = budget


def call_id_of(msg) -> str | None:
    """The proxy's call id from a message's response headers, if the model was
    built with include_response_headers=True (llm_for_role does this)."""
    meta = getattr(msg, "response_metadata", None) or {}
    headers = meta.get("headers")
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == CALL_ID_HEADER and value:
            return str(value)
    return None


def _estimate_for(msg) -> float:
    """Price one response message: the router's own cost if it came through,
    else the cache-aware estimate, else a loud non-zero placeholder."""
    meta = getattr(msg, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or {}
    if "cost" in token_usage:
        return float(token_usage["cost"])
    usage = getattr(msg, "usage_metadata", None) or {}
    cache_read = (usage.get("input_token_details") or {}).get("cache_read", 0)
    try:
        return estimate_cost_strict(
            meta.get("model_name"),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            cache_read_tokens=cache_read,
        )
    except UnpricedModelError as e:
        # An unpriced model must not read as $0 against a hard ceiling (audit
        # C-1). Charge a conservative non-zero placeholder so the ceiling
        # still advances and log loudly -- undercounting to zero is the exact
        # failure that let 1.4M tokens through a $5 cap. The router's billed
        # figure replaces this too, once it lands.
        logger.warning("budget: %s -- charging $0.02/1k output tokens as a placeholder", e)
        return usage.get("output_tokens", 0) / 1000 * 0.02


class BudgetTracker:
    """One instance per task invocation, shared by reference across the
    coordinator's and every subagent's own BudgetGuardMiddleware. Never
    share an instance across different tasks/threads -- `build_deep_agent`
    constructs a fresh one per `work`-node invocation, seeded with whatever
    `cost_so_far` the outer AgentState already carries in from a resume, so
    a resumed task keeps counting from its real total rather than
    restarting at zero.

    Each charge is carried at its estimate until the router's billed cost for
    the same call id shows up in routing.jsonl (RouterLedger); `total_cost`
    reconciles on every read. Charges without a call id -- a model built
    without response headers, or a direct `total_cost +=` from older code --
    stay at their estimate for good.
    """

    def __init__(self, budget_usd: float, starting_cost: float = 0.0, ledger: RouterLedger | None = None):
        self.budget_usd = budget_usd
        self.starting_cost = starting_cost
        self._unattributed = 0.0
        # call_id -> [estimate, billed | None]; insertion-ordered so the
        # breakdown reads in call order.
        self._calls: dict[str, list] = {}
        self._ledger = ledger if ledger is not None else RouterLedger()

    def charge(self, estimate: float, call_id: str | None = None) -> None:
        if not call_id:
            self._unattributed += estimate
            return
        entry = self._calls.get(call_id)
        if entry is None:
            self._calls[call_id] = [estimate, None]
        else:
            entry[0] += estimate  # several messages from one call

    def reconcile(self) -> None:
        """Swap estimates for the router's billed cost wherever it has landed."""
        pending = self.pending_call_ids
        if pending:
            self._apply_billed(self._ledger.actual_costs(pending))

    async def areconcile(self) -> None:
        """reconcile() with the ledger read in a worker thread: it parses up
        to half a megabyte of log, and the event loop is serving every other
        task. A sync `total_cost` read right after finds the file unchanged
        and costs one stat()."""
        pending = self.pending_call_ids
        if pending:
            self._apply_billed(await asyncio.to_thread(self._ledger.actual_costs, pending))

    def _apply_billed(self, billed: dict[str, float]) -> None:
        for call_id, cost in billed.items():
            entry = self._calls.get(call_id)
            if entry is not None:
                entry[1] = cost

    async def settle(self, timeout_s: float = SETTLE_TIMEOUT_S, poll_s: float = SETTLE_POLL_S) -> bool:
        """Wait briefly for the router to bill whatever is still carried at
        its estimate. True if nothing is pending afterwards."""
        deadline = time.monotonic() + timeout_s
        while True:
            await self.areconcile()
            if not self.pending_call_ids:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_s)

    @property
    def pending_call_ids(self) -> list[str]:
        return [cid for cid, (_, billed) in self._calls.items() if billed is None]

    @property
    def total_cost(self) -> float:
        self.reconcile()
        return self.starting_cost + self._unattributed + sum(
            billed if billed is not None else estimate for estimate, billed in self._calls.values()
        )

    @total_cost.setter
    def total_cost(self, value: float) -> None:
        # `tracker.total_cost += x` from a caller that has no call id: keep
        # the delta as an unattributed charge rather than rejecting it.
        self._unattributed += value - self.total_cost

    def breakdown(self) -> str:
        billed = [(e, b) for e, b in self._calls.values() if b is not None]
        pending = [e for e, b in self._calls.values() if b is None]
        parts = [f"router-billed ${sum(b for _, b in billed):.4f} over {len(billed)} calls"]
        if pending:
            parts.append(f"${sum(pending):.4f} estimated for {len(pending)} not yet billed")
        if self._unattributed:
            parts.append(f"${self._unattributed:.4f} estimated with no call id")
        if self.starting_cost:
            parts.append(f"${self.starting_cost:.4f} carried in")
        return "; ".join(parts)


class BudgetGuardMiddleware(AgentMiddleware):
    def __init__(self, tracker: BudgetTracker):
        super().__init__()
        self.tracker = tracker

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        await self.tracker.areconcile()
        if self.tracker.total_cost >= self.tracker.budget_usd:
            # Refuse before spending on a call that's already over budget --
            # don't wait for this call's own cost to land before tripping.
            raise BudgetExceededError(
                self.tracker.total_cost, self.tracker.budget_usd, self.tracker.breakdown()
            )

        response = await handler(request)

        for msg in response.result:
            self.tracker.charge(_estimate_for(msg), call_id_of(msg))

        await self.tracker.areconcile()
        if self.tracker.total_cost >= self.tracker.budget_usd:
            # The estimate for the call that just finished says we are over.
            # The router bills it within milliseconds of the stream closing;
            # let that figure land before ending a turn on an estimate.
            await self.tracker.settle()
            if self.tracker.total_cost >= self.tracker.budget_usd:
                raise BudgetExceededError(
                    self.tracker.total_cost, self.tracker.budget_usd, self.tracker.breakdown()
                )

        return response


class BudgetMeterCallback(AsyncCallbackHandler):
    """Meters model calls that never pass through `awrap_model_call`.

    SummarizationMiddleware invokes its own summary model directly
    (`self._summary_model.ainvoke(...)` inside `_acreate_summary`) -- that call
    is not a graph model node, so no agent middleware wraps it and its spend
    was invisible to the tracker: measured 2026-08-27 at $0.48 of $16.43 over
    48h (2.9% of all router spend) never counted against any ceiling.

    A LangChain callback fires on every call made BY the model object it is
    attached to, regardless of who invokes it or how -- which makes attaching
    this to the summarizer model itself (see `llm_for_role(callbacks=...)`)
    the one seam that catches the direct-ainvoke path.

    Meter only, never a gate: `on_llm_end` cannot usefully refuse a call that
    already completed, and raising from inside a callback would surface as a
    summarization failure -- which SummarizationMiddleware handles by
    substituting fallback text for the ENTIRE prior conversation (the exact
    silent-history-wipe failure documented at SUMMARIZATION_TRIM_TOKENS).
    The BudgetGuardMiddleware on the next real model call trips the ceiling
    with this spend already counted, one summarizer call later at most.

    Token counts are read from generation_info/usage_metadata the same way
    BudgetGuardMiddleware reads them, and priced through the same
    estimate_cost_strict against the router's own config -- one pricing
    source, not two that can drift. The unpriced-model fallback mirrors the
    guard's (audit C-1): a conservative non-zero placeholder, never $0.
    """

    def __init__(self, tracker: BudgetTracker):
        super().__init__()
        self.tracker = tracker

    async def on_llm_end(self, response, **kwargs) -> None:
        for gens in response.generations:
            for gen in gens:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                usage = getattr(msg, "usage_metadata", None) or {}
                meta = getattr(msg, "response_metadata", None) or {}
                if not usage and "cost" not in (meta.get("token_usage") or {}):
                    continue
                self.tracker.charge(_estimate_for(msg), call_id_of(msg))
