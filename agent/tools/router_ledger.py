"""The router's own per-call bill, read back into the budget.

BudgetGuardMiddleware has to price every model call itself, because the router's
exact cost annotation does not survive OpenAI-compatible streaming (see
budget_guard.py). An estimate computed from token counts and a rate table is
only as good as the table: on 2026-09-08 a planning turn was ended at
"$8.09 spent" when OpenRouter had billed $1.72 -- the pinned model id was
missing from OpenRouter's catalog, so its cache-read discount fell back to
the full input rate and 4.9M mostly-cached prompt tokens were charged 8x.

The router knows the true figure the moment each call completes: it
asks OpenRouter for `usage.cost` on every request and hands it to
the router's own ledger writer, which appends one line per call to
logs/routing.jsonl, keyed by the same `x-router-call-id` the proxy returned
in the response headers. This module reads that record back. The tracker
carries each call at its estimate until the router's line lands, then swaps
in the billed cost -- so the running total the ceiling is enforced against
is the router's own number for every call but the one that just finished.

Best-effort by design: the file lives beside the router, so an agent talking
to a router on another host (MODEL_ROUTER_LEDGER unset and no local file)
simply never resolves anything and the estimate stands, exactly as before.
"""

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("tektonix")

# Repo-relative, with an env override, like ROUTER_CONFIG_PATH in
# model_rates.py. This file is agent/tools/router_ledger.py, so the router
# lives three parents up.
ROUTING_LOG_PATH = Path(
    os.environ.get("MODEL_ROUTER_LEDGER")
    or (Path(__file__).resolve().parents[2] / "services" / "model-router" / "logs" / "routing.jsonl")
)

# routing.jsonl is appended forever and trimmed only past 50MB; a call we are
# waiting on is always within the last few hundred lines, so read the tail.
_TAIL_BYTES = 512_000


class RouterLedger:
    """Looks up the router's billed cost by call id. Re-reads the log only
    when its size or mtime changed, so polling it between model calls costs
    one stat() when nothing new has landed."""

    def __init__(self, path: Path | str = ROUTING_LOG_PATH):
        self.path = Path(path)
        self._signature: tuple[int, int] | None = None
        self._costs: dict[str, float] = {}

    def actual_costs(self, call_ids: Iterable[str | None]) -> dict[str, float]:
        """{call_id: billed_cost} for every requested id the router has
        logged so far. Ids the router has not billed yet are simply absent."""
        wanted = [c for c in call_ids if c]
        if not wanted:
            return {}
        self._refresh()
        return {c: self._costs[c] for c in wanted if c in self._costs}

    def _refresh(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            return  # no router log on this box: nothing ever resolves
        signature = (st.st_size, st.st_mtime_ns)
        if signature == self._signature:
            return
        try:
            with open(self.path, "rb") as f:
                if st.st_size > _TAIL_BYTES:
                    f.seek(st.st_size - _TAIL_BYTES)
                    f.readline()  # drop the partial line we landed inside
                data = f.read()
        except OSError as e:
            logger.debug("router ledger unreadable: %s", e)
            return
        costs: dict[str, float] = {}
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a line still being written, or a torn one
            call_id = row.get("call_id")
            cost = row.get("cost")
            if call_id and isinstance(cost, (int, float)):
                costs[call_id] = float(cost)
        self._costs = costs
        self._signature = signature

    def total_for_task(self, task_id: str) -> float:
        """Everything the router has billed for this task, across every pass.

        Reads the WHOLE log rather than the tail: a task can run for hours and
        its early calls are long past the last 512KB. Bounded anyway, because
        the router trims the file past 50MB (router/ledger.py MAX_BYTES) -- and
        that trim is exactly why this
        is a floor rather than a truth. A caller compares it with its own
        checkpointed figure and keeps the larger (see _reconciled_cost in
        agent/nodes/work.py).

        Costs a full parse, so it is for a pass boundary, not a hot path.
        """
        if not task_id:
            return 0.0
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except OSError as e:
            logger.debug("router ledger unreadable for a task total: %s", e)
            return 0.0
        total = 0.0
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("task_id") != task_id:
                continue
            cost = row.get("cost")
            if isinstance(cost, (int, float)):
                total += float(cost)
        return total
