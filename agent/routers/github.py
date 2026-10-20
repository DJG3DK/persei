"""The GitHub inbox and its approve links.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-23 once task
creation had moved to agent/tasks.py -- approving an item starts a task, and
that is what kept these routes in server.py. The routes are unchanged;
tests/test_route_inventory.py and tests/test_repo_scope.py pin every path,
method, guard and repo check.

What stays in server.py is the machinery the routes only TRIGGER: the poller,
the notifier, and `_github_create_task`, the creator that forces merge review
on for every inbox task. They are reached on `app.state` (`github_poll_once`,
`github_create_task`, `github_last_poll`, `config`), looked up at call time,
because importing `app` back from server.py would be a cycle -- and because a
test standing in for one of them patches `app.state`, which a name captured
at import would ignore. PROJECTS is read the same way, off `agent.config`.
"""
from __future__ import annotations

import html
import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from agent import audit, auth, github_inbox, github_settings, rate_limit
from agent import config as agent_config
from agent.auth import User, check_repo_access, require_full_auth
from agent.routers import audit_store

router = APIRouter(tags=["github"])


@router.get("/api/github/inbox")
async def github_inbox_list(request: Request, repo: str | None = None, user: User = Depends(require_full_auth)):
    repos = [repo] if repo else [r for r in agent_config.PROJECTS if user.can_access(r)]
    if repo:
        check_repo_access(user, repo)
    items = []
    for r in repos:
        items.extend((await github_inbox.list_items(request.app.state.store, r)).values())
    items.sort(key=lambda i: i.get("updated_at", 0), reverse=True)
    # Written by the poller in server.py, which mirrors it onto app.state.
    return {"items": items, "last_poll": request.app.state.github_last_poll}


class InboxActionRequest(BaseModel):
    days: float | None = None    # snooze length


async def _github_act(app, repo: str, key: str, action: str, *, nonce: str | None = None,
                      days: float | None = None, actor: str = "signed link") -> dict:
    """Approve / dismiss / snooze one inbox item. `nonce` is set when the
    request came through a signed link and must match the item's current
    nonce, which is what makes a link single-use.

    `actor` is who the audit log will name. It defaults to the link because
    that is the honest answer for the path with no session behind it:
    clicking an approve link in Telegram starts a real task, and the log
    would otherwise show a task appearing with nobody having started it.

    Under the item's lock from the read to the write: the nonce check and the
    state check are only single-use if nobody else can pass them while
    create_task_for_item is awaited."""
    async with github_inbox.item_lock(repo, key):
        return await _github_act_locked(app, repo, key, action, nonce=nonce, days=days, actor=actor)


async def _github_act_locked(app, repo: str, key: str, action: str, *, nonce: str | None,
                             days: float | None, actor: str) -> dict:
    items = await github_inbox.list_items(app.state.store, repo)
    item = items.get(key)
    if not item:
        raise HTTPException(404, "that item is no longer in the inbox")
    if nonce is not None and item.get("approval_nonce") != nonce:
        raise HTTPException(409, "this link was already used")
    if action == "approve":
        if item.get("state") == "task_created" and item.get("task_id"):
            return {"ok": True, "already": True, "task_id": item["task_id"], "item": item}
        if item.get("state") not in ("proposed", "snoozed", "seen"):
            raise HTTPException(409, f"item is {item.get('state')}; nothing to approve")
        task_id = await github_inbox.create_task_for_item(
            item, github_settings.current(), app.state.config,
            # server.py's creator, which forces merge review on for every
            # inbox task -- see _github_create_task there.
            app.state.github_create_task)
        item.update({"state": "task_created", "task_id": task_id, "reason": "approved by operator", "approval_nonce": None})
    elif action == "dismiss":
        item.update({"state": "dismissed", "reason": "dismissed by operator", "approval_nonce": None})
    elif action == "snooze":
        until = time.time() + max(0.05, float(days or 1.0)) * 86400
        item.update({"state": "snoozed", "snoozed_until": until, "reason": f"snoozed until {time.strftime('%Y-%m-%d', time.gmtime(until))}"})
    else:
        raise HTTPException(400, "unknown action")
    await github_inbox.put_item(app.state.store, item)
    await audit.record(
        audit_store(SimpleNamespace(app=app)), actor=actor, action=f"inbox.{action}", target=f"{repo}/{key}",
        detail=(item.get("title") or "")[:160],
        extra={"task_id": item.get("task_id")} if item.get("task_id") else None,
    )
    return {"ok": True, "item": item, "task_id": item.get("task_id")}


@router.post("/api/github/inbox/{repo}/{key}/{action}")
async def github_inbox_action(request: Request, repo: str, key: str, action: str,
                              req: InboxActionRequest | None = None,
                              user: User = Depends(require_full_auth)):
    if repo not in agent_config.PROJECTS:
        raise HTTPException(404, "unknown repo")
    check_repo_access(user, repo)
    return await _github_act(request.app, repo, key, action, days=(req.days if req else None), actor=user.email)


@router.post("/api/github/poll")
async def github_poll_now(request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    # The poller lives in server.py (it reaches the notifier and task
    # creation); the route only triggers it.
    return {"results": await request.app.state.github_poll_once()}


_APPROVE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Persei · GitHub inbox</title>
<style>body{font:16px/1.5 system-ui,sans-serif;background:#0f1220;color:#e6e8f0;margin:0;padding:24px}
.card{max-width:560px;margin:8vh auto;background:#181c30;border:1px solid #2a3050;border-radius:12px;padding:24px}
h1{font-size:18px;margin:0 0 12px}p{margin:8px 0}.muted{color:#9aa3c0}.err{color:#ff8a8a}
button{font:inherit;font-weight:700;border:0;border-radius:8px;padding:12px 18px;cursor:pointer;margin-top:12px}
.go{background:#3fb950;color:#06210c}.no{background:#2a3050;color:#e6e8f0;margin-left:8px}</style></head>
<body><div class="card">{body}</div></body></html>"""


def _approve_html(body: str, status_code: int = 200) -> Response:
    from fastapi.responses import HTMLResponse
    # no-store: the confirmation page carries a live token in its form, and
    # the GET's own URL carries it too. Referrer-Policy: no-referrer is set on
    # every response already, so it does not leak onward; this keeps it out of
    # proxy and browser caches as well.
    return HTMLResponse(_APPROVE_PAGE.replace("{body}", body), status_code=status_code,
                        headers={"Cache-Control": "no-store"})


def _approve_is_cross_site(request: Request) -> bool:
    """The same rule the review dashboard's mutating routes use
    (services/agent-review/server.js): a browser marks where a request came
    from, and only its own page (same-origin) or a direct navigation (none)
    may act. Absent means a non-browser client, which the token alone
    governs -- as it always has."""
    sfs = request.headers.get("sec-fetch-site")
    return bool(sfs) and sfs not in ("same-origin", "none")


# Fixed wording, looked up by reason: nothing an exception carries reaches
# the page (CodeQL py/stack-trace-exposure). verify_approval raises
# ValueError with one of these reason codes as its message.
_LINK_PROBLEMS = {
    "malformed": "this link is malformed",
    "invalid": "this link is not valid for this deployment",
    "expired": "this link has expired; open the GitHub inbox in the dashboard instead",
    "unknown": "this link asks for an unknown action",
}
_ACT_PROBLEMS = {
    404: "that item is no longer in the inbox",
    409: "this link was already used, or the item was already handled",
    0: "the request could not be completed; open the GitHub inbox in the dashboard",
}


def _link_problem(e: ValueError) -> str:
    return _LINK_PROBLEMS.get(str(e), _LINK_PROBLEMS["malformed"])


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


@router.get("/api/github/approve")
async def github_approve_page(request: Request, t: str = ""):
    """The link from Telegram/email. Shows what would happen and a button;
    the button POSTs. A GET never acts -- messengers fetch links for previews."""
    try:
        data = github_inbox.verify_approval(request.app.state.config, t)
    except ValueError as e:
        return _approve_html(f"<h1>Link problem</h1><p class=err>{_link_problem(e)}</p>")
    items = await github_inbox.list_items(request.app.state.store, data["r"])
    item = items.get(data["k"])
    if not item:
        return _approve_html("<h1>Gone</h1><p class=muted>That item is no longer in the inbox.</p>")
    if item.get("approval_nonce") != data["n"]:
        state = item.get("state")
        return _approve_html(f"<h1>Already handled</h1><p class=muted>This item is <b>{_esc(state)}</b>"
                             + (f" (task {_esc(item.get('task_id', '')[:8])})" if item.get("task_id") else "") + ".</p>")
    verb = "Start a task for" if data["a"] == "approve" else "Dismiss"
    budget = github_settings.project_settings(github_settings.current(), data["r"])["budget_usd"]
    body = (f"<h1>{verb} this?</h1><p><b>{_esc(item.get('title'))}</b></p><p class=muted>{_esc(item.get('summary'))}</p>"
            f"<p class=muted>{_esc(data['r'])} · {_esc(item.get('kind'))}"
            + (f" · budget ${budget:.2f}, through the normal review gate" if data["a"] == "approve" else "") + "</p>"
            # action="" posts back to this same URL, whatever prefix nginx serves it under.
            f"<form method=post action=\"\"><input type=hidden name=t value=\"{_esc(t)}\">"
            f"<button class=go type=submit>{'Approve and start' if data['a'] == 'approve' else 'Dismiss'}</button></form>")
    return _approve_html(body)


@router.post("/api/github/approve")
async def github_approve_submit(request: Request):
    """The approve link's one acting request. Its token is the credential and
    it is single-use; on top of that, a page elsewhere cannot submit it (the
    cross-site check) and nobody can try tokens quickly (the rate limit).
    What stays is that the token sits in the GET's URL -- SECURITY.md, "Approve
    links"."""
    if _approve_is_cross_site(request):
        return _approve_html("<h1>Not done</h1><p class=err>This link must be confirmed from its own "
                             "page. Open it again and press the button there.</p>", 403)
    try:
        rate_limit.check_rate_limit(request, "github-approve")
    except HTTPException:
        return _approve_html("<h1>Slow down</h1><p class=err>Too many attempts from here. "
                             "Try again in a few minutes.</p>", 429)
    form = await request.form()
    t = str(form.get("t") or "")
    try:
        data = github_inbox.verify_approval(request.app.state.config, t)
        result = await _github_act(request.app, data["r"], data["k"], data["a"], nonce=data["n"])
    except ValueError as e:
        return _approve_html(f"<h1>Link problem</h1><p class=err>{_link_problem(e)}</p>")
    except HTTPException as e:
        return _approve_html(f"<h1>Not done</h1><p class=err>{_ACT_PROBLEMS.get(e.status_code, _ACT_PROBLEMS[0])}</p>")
    if data["a"] == "approve":
        tid = result.get("task_id") or ""
        return _approve_html(f"<h1>Task started</h1><p>{_esc(result['item'].get('title'))}</p>"
                             f"<p class=muted>Task {_esc(tid[:8])} is running on {_esc(data['r'])}. It will ask for your merge approval when the review is ready.</p>")
    return _approve_html(f"<h1>Dismissed</h1><p class=muted>{_esc(result['item'].get('title'))}</p>")
