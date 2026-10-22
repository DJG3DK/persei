"""The .env editor: what the dashboard may change without a shell.

A seam out of agent/server.py (agent/routers/). The routes are unchanged --
tests/test_route_inventory.py pins every path, method and guard, so a move
that altered any of them fails the snapshot rather than landing quietly.

Reads its state off `request.app.state` rather than importing `app` back from
server.py, which includes this router and would make that import a cycle.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from agent.auth import User, require_full_auth

from agent import audit, auth
from agent import env_config
from agent.routers import audit_store
from fastapi import HTTPException
from pydantic import BaseModel
import logging

logger = logging.getLogger("tektonix")

router = APIRouter(prefix="/api/env-config", tags=["env_config"])


class SaveEnvKeysRequest(BaseModel):
    updates: dict[str, str]


@router.get("")
async def get_env_config(user: User = Depends(require_full_auth)):
    """The credentials this deployment runs on, MASKED.

    There is deliberately no endpoint that returns a secret's value. Reads give
    the last four characters and whether it is set, which is enough to confirm
    *which* key is installed without being enough to use it.
    """
    auth.require_admin(user)
    compose = env_config.managed_by_compose()
    return {"keys": env_config.list_keys(), "managed_by": "compose" if compose else "files",
            "note": env_config.COMPOSE_NOTE if compose else None}


@router.post("")
async def save_env_config(req: SaveEnvKeysRequest, request: Request, user: User = Depends(require_full_auth)):
    """Write new values for allow-listed keys.

    Restarts are reported, not performed: restarting the router interrupts every
    in-flight model call, and that is the operator's call to make, not a side
    effect of saving a form.
    """
    auth.require_admin(user)
    try:
        result = env_config.set_keys(req.updates)
    except env_config.UnknownKeyError as e:
        # The key NAME is safe to echo; the value never is.
        raise HTTPException(status_code=400, detail=str(e))
    except env_config.ReadOnlyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except env_config.InvalidValueError as e:
        # The caller's value is the problem, not the file: the message names
        # the key only. This fell through to the 500 below (2026-09-29).
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("env-config write failed for %s: %s", sorted(req.updates), type(e).__name__)
        raise HTTPException(status_code=500, detail="could not write the env file")
    logger.info("env-config updated by %s: %s", user.email, ", ".join(result["updated"]))
    # Key names only: the log is readable by every admin, and a value here
    # would be the one place a secret is shown back.
    await audit.record(audit_store(request), actor=user.email, action="env.update",
                       detail=", ".join(result["updated"]))
    return result


@router.post("/restart")
async def restart_services(req: SaveEnvKeysRequest, user: User = Depends(require_full_auth)):
    """Restart the named services so a key change takes effect."""
    auth.require_admin(user)
    import asyncio as _a
    allowed = {"model-router", "tektonix", "commit-reviewer", "agent-review"}
    names = [n for n in (req.updates.get("services", "") or "").split(",") if n.strip() in allowed]
    if not names:
        raise HTTPException(status_code=400, detail="no known services named")
    out = {}
    for n in names:
        # The agent restarting kills this request mid-flight, which is expected —
        # the client treats a dropped connection on its own restart as success.
        proc = await _a.create_subprocess_exec(
            "pm2", "restart", n, stdout=_a.subprocess.PIPE, stderr=_a.subprocess.STDOUT)
        try:
            o, _ = await _a.wait_for(proc.communicate(), timeout=60)
            out[n] = "ok" if proc.returncode == 0 else (o or b"").decode()[-200:]
        except TimeoutError:
            proc.kill()
            out[n] = "timed out"
    return {"restarted": out}
