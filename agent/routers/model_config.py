"""Model pins: which model each agent role actually calls.

A seam out of agent/server.py (agent/routers/). The routes are unchanged --
tests/test_route_inventory.py pins every path, method and guard, so a move
that altered any of them fails the snapshot rather than landing quietly.

State comes off `request.app.state`; importing `app` back from server.py,
which includes this router, would be a cycle.
"""
from __future__ import annotations

import os
import sys

from fastapi import APIRouter, Depends, Request

from agent.auth import User, require_full_auth

from agent import auth
from agent import model_config

from fastapi import HTTPException
from pathlib import Path
from pydantic import BaseModel
import logging

logger = logging.getLogger("tektonix")

router = APIRouter(prefix="/api/model-config", tags=["model_config"])


class SaveModelPinsRequest(BaseModel):
    pins: dict[str, str]  # {role: openrouter_model_id}


class SaveProviderPinsRequest(BaseModel):
    # role -> provider name, or null/"" to clear back to auto-routing
    pins: dict[str, str | None]


@router.get("")
async def get_model_config(user: User = Depends(require_full_auth)):
    """Current pins for this agent's own roles -- see model_config.MANAGED_ROLES
    (fifteen of them, including agent-reviewer, which the commit-reviewer
    service resolves by alias). The remaining entries in model-router/config.yaml
    are not this agent's to set and are never exposed here: unnamed fallback
    targets, and any alias another process on the box may have added.
    """
    auth.require_admin(user)
    # Live catalog prices, not the hand-written model_info blocks (which drift).
    return {"roles": await model_config.get_current_pins_priced(), "router_restart": _router_restart()}


BUNDLE_RESTART_NOTE = ("The bundle's router re-reads its config on the next call, so a saved pin is already live. "
                       "To restart it anyway: docker compose restart router")


def _router_restart() -> dict:
    """Whether this page can restart the router. In the bundle it cannot
    (no pm2 in a container) and need not, and the page shows this note in
    place of the button rather than a dialog that then fails."""
    if os.environ.get("TEKTONIX_BUNDLE") == "1":
        return {"available": False, "note": BUNDLE_RESTART_NOTE}
    return {"available": True, "note": None}


@router.get("/catalog")
async def get_model_catalog(refresh: bool = False, user: User = Depends(require_full_auth)):
    """OpenRouter's live model catalog for the picker's dropdown -- cached
    for 10 minutes; pass ?refresh=true to force a fresh fetch."""
    auth.require_admin(user)
    stats = model_config.forced_tool_call_stats()
    catalog = await model_config.fetch_model_catalog(force=refresh)
    return {
        "models": catalog,
        # Roles that force a tool call cannot use every model, and OpenRouter's
        # catalog cannot tell you which -- supported_parameters lists tool_choice
        # and reasoning separately while some providers refuse the COMBINATION.
        # These lists come from scripts/probe_forced_tool_call.py making real
        # requests, so the picker can hide models that would fail.
        "forced_tool_call": {**stats, "catalog_size": len(catalog)},
    }


@router.post("")
async def save_model_config(req: SaveModelPinsRequest, user: User = Depends(require_full_auth)):
    """Writes new pins for one or more of this agent's own roles. The router
    re-reads config.yaml on its next call when the file's mtime changed
    (services/model-router/router/config.py), so a saved pin is live without
    a restart. POST /api/model-config/restart-router stays a separate,
    explicit action: a restart affects every consumer of the shared router,
    and kills a model call in flight.
    """
    auth.require_admin(user)
    catalog = await model_config.fetch_model_catalog()
    try:
        changed = model_config.set_pins(req.pins, catalog)
    except model_config.UnknownRoleError as e:
        raise HTTPException(400, str(e))
    except model_config.ModelNotInCatalogError as e:
        raise HTTPException(400, str(e))
    except model_config.PinBlockNotFoundError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "changed": changed, "roles": await model_config.get_current_pins_priced()}


@router.get("/endpoints")
async def get_model_endpoints(model: str, user: User = Depends(require_full_auth)):
    """The providers currently serving `model` on OpenRouter -- feeds the
    dashboard's provider picker. Names returned here are exactly what
    provider pinning writes into `provider.only`."""
    auth.require_admin(user)
    try:
        return {"model": model, "endpoints": await model_config.fetch_model_endpoints(model)}
    except Exception as e:  # noqa: BLE001 -- surface upstream failures as a readable 502
        raise HTTPException(502, f"could not fetch providers for {model!r}: {e}")


class SavePrivacyRequest(BaseModel):
    mode: str


@router.get("/privacy")
def get_privacy(user: User = Depends(require_full_auth)):
    """Which OpenRouter providers may serve the router's calls: "zdr" (keep
    nothing), "deny" (no storing or training) or "off"."""
    auth.require_admin(user)
    return {"mode": model_config.get_privacy(), "modes": list(model_config.PRIVACY_MODES)}


@router.post("/privacy")
def save_privacy(req: SavePrivacyRequest, user: User = Depends(require_full_auth)):
    """Writes router_settings.privacy; the router applies it on its next call."""
    auth.require_admin(user)
    try:
        return {"ok": True, "mode": model_config.set_privacy(req.mode.strip().lower())}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/providers")
async def save_provider_pins(req: SaveProviderPinsRequest, user: User = Depends(require_full_auth)):
    """Pin (or clear) the OpenRouter provider per role. Same contract as the
    model pin save: writes config.yaml, and the router picks it up on its
    next call; no restart."""
    auth.require_admin(user)
    cleaned = {r: (p or None) for r, p in req.pins.items()}
    try:
        model_config.set_provider_pins(cleaned)
    except model_config.UnknownRoleError as e:
        raise HTTPException(400, str(e))
    except model_config.ProviderPinError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "roles": await model_config.get_current_pins_priced()}


# The repo root's scripts/, two levels above agent/routers/. The server's own
# interpreter runs it: the bundle has no .venv.
PROBE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_forced_tool_call.py"


@router.post("/probe-forced-tool-call")
async def probe_forced_tool_call(user: User = Depends(require_full_auth)):
    """Re-run the forced-tool-call probe and refresh the picker's allow-list.

    Worth a button because the answer genuinely goes stale: OpenRouter adds and
    retires models constantly, and compliance is per-provider -- the same model
    can pass or fail depending on who answers, so a cached verdict decays. This
    runs the real probe (scripts/probe_forced_tool_call.py) rather than re-reading
    the catalog, because the catalog cannot express the constraint.
    """
    auth.require_admin(user)
    import asyncio as _asyncio

    proc = await _asyncio.create_subprocess_exec(
        sys.executable, str(PROBE_SCRIPT), "--all", "--concurrency", "8",
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await _asyncio.wait_for(proc.communicate(), timeout=1500)
    except TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="probe timed out")

    if proc.returncode != 0:
        # audit H5: this used to return HTTP 200 with the traceback tucked into
        # `tail`, so a probe that never ran looked to the dashboard exactly
        # like one that found nothing. A failed probe is an error.
        tail = (out or b"").decode(errors="replace")[-1200:]
        logger.error("forced-tool-call probe exited %s: %s", proc.returncode, tail)
        raise HTTPException(
            status_code=500,
            detail=f"probe failed (exit {proc.returncode}). Last output:\n{tail}")

    stats = model_config.forced_tool_call_stats()
    return {
        "ok": True,
        **stats,
        "catalog_size": len(await model_config.fetch_model_catalog()),
        "tail": (out or b"").decode(errors="replace")[-1200:],
    }


@router.post("/restart-router")
def restart_model_router(request: Request, user: User = Depends(require_full_auth)):
    """Restarts the model router so a saved pin change actually takes effect.
    Shared-impact action: this restarts the same router the
    review service depend on, not just this agent -- the frontend must
    surface that plainly rather than bundling this into save.
    """
    auth.require_admin(user)
    if os.environ.get("TEKTONIX_BUNDLE") == "1":
        # No pm2 in a container, and no need: the bundle's router re-reads
        # the shared config on its next call. `docker compose restart router`
        # is the operator's, from the host, if they want one anyway.
        raise HTTPException(409, BUNDLE_RESTART_NOTE)
    result = model_config.restart_llm_router(request.app.state.config.router_base_url)
    if not result["ok"]:
        # The message is what the dialog shows, so it has to say which half
        # failed: a router that never came back is a different problem from a
        # restart command pm2 refused.
        detail = ("the router did not answer its liveness route within "
                  f"{result['waited_s']:.0f}s after the restart"
                  if result.get("restarted") else "pm2 could not restart model-router")
        raise HTTPException(500, f"{detail}\n\n{result['output']}".strip())
    return result
