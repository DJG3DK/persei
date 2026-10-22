"""Telegram alerts for the events an operator would act on.

Telegram, specifically, because the operator already lives there -- the
Another application on the box already alerts over Telegram, so this reuses a
channel that is already open on their phone rather than introducing a second
messaging system to configure and monitor.

Design constraints, in order:
  1. BEST-EFFORT, ALWAYS. An alert failing must never break, slow, or fail
     the thing it is alerting about. Every public function here swallows its
     own errors after logging them.
  2. One choke point per event class. Task alerts hook _stream_graph's
     terminal-status write (every rest state funnels through it), not a
     scatter of call sites.
  3. Details AND costs in every message (operator requirement, 2026-08-27):
     an alert should carry enough to decide "phone or laptop?" without
     opening the dashboard.

Recipients are every user with Telegram configured in Settings (bot token +
chat id, stored per-user in agent_users). This is a small-team deployment;
per-user routing rules can come later if it ever matters.
"""

import asyncio
import shutil
import logging

import httpx

from agent import live_state

logger = logging.getLogger("tektonix")

_API = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram hard limit is 4096 chars/message; leave margin for the ellipsis.
_MAX_LEN = 4000


async def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """One message to one chat. Returns success; never raises."""
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN] + "…"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_API.format(token=token), json={
                "chat_id": chat_id,
                "text": text,
                # Plain text on purpose: goals/errors contain arbitrary
                # markdown-hostile characters, and a 400 from a bad entity
                # would silently eat the alert. Reliability beats bold text.
                "disable_web_page_preview": True,
            })
        if r.status_code != 200:
            logger.warning("telegram send failed (%s): %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001 -- constraint 1: alerts never break the caller
        logger.warning("telegram send failed: %s", e)
        return False


def _may_hear_about(role: str, allowed_repos, repo: str | None) -> bool:
    """Mirror of User.can_access, applied to an alert recipient.

    `repo=None` means an alert with no project scope (a service restart, the
    backend coming back up). Those name infrastructure rather than anyone's
    code, so they go to admins only rather than to every token holder.
    """
    if role == "admin":
        return True
    if repo is None:
        return False
    return allowed_repos is None or repo in allowed_repos


def task_url(task_id: str) -> str:
    """Where a click on a task alert lands: the task's own page (route.ts
    reads /task/<id>). Every alert opened "/" until 2026-09-29 (audit, U6),
    which for an open window meant leaving whatever the operator was on."""
    return f"/task/{task_id}"


def planning_url(session_id: str) -> str:
    return f"/planning/{session_id}"


def push_tag(repo: str | None, url: str = "/") -> str:
    """The notification tag: one per task or planning session, read off the
    link the notification opens, else one per project. A notification
    replaces the one with its tag, and tagging by project alone made one
    task's DONE replace another's escalation on the lock screen (audit B39,
    2026-10-22)."""
    for prefix in ("/task/", "/planning/"):
        if url.startswith(prefix) and len(url) > len(prefix):
            return f"{prefix.strip('/')}:{url[len(prefix):]}"
    return repo or "persei"


async def _push_operators(auth_pool, text: str, repo: str | None, url: str = "/") -> int:
    """The push half of the fan-out. Same recipients, same scoping rule, same
    never-raises contract as the Telegram half.

    A dead subscription is DELETED rather than retried: 404/410 from a push
    service means the browser uninstalled the app, cleared its data, or the
    endpoint expired, and those never come back. Leaving them accumulates rows
    that fail on every alert forever.
    """
    from agent import auth, push
    if not push.configured():
        return 0
    try:
        targets = await auth.get_push_targets(auth_pool)
    except Exception:  # noqa: BLE001
        logger.exception("push: could not load recipients")
        return 0

    title, _, rest = text.partition("\n")
    body = rest.strip() or title
    sent = 0
    for t in targets:
        if not _may_hear_about(t["role"], t["allowed_repos"], repo):
            continue
        ok, status = await push.send_one(t, title.strip() or "Persei", body,
                                         url=url, tag=push_tag(repo, url))
        if ok:
            sent += 1
            await auth.mark_push_ok(auth_pool, t["endpoint"])
        elif status in (404, 410):
            logger.info("push: dropping a subscription the service says is gone (%s)", status)
            await auth.delete_push_subscription(auth_pool, t["endpoint"])
    return sent


async def notify_operators(auth_pool, text: str, repo: str | None = None, url: str = "/") -> int:
    """Send `text` to the users allowed to know about `repo`.

    `url` is where a push notification opens (task_url / planning_url); the
    Telegram half carries no link.

    audit H1: the fan-out used to reach everyone with a token configured,
    regardless of allowed_repos, while the message body carries the repo name,
    a goal excerpt and up to 1500 characters of failure detail. Passing the
    repo through and filtering here is what stops a single-repo user from
    receiving a live feed of every project.

    Returns how many sends succeeded; never raises.
    """
    try:
        from agent import auth
        targets = await auth.get_telegram_targets(auth_pool)
    except Exception:  # noqa: BLE001
        logger.exception("telegram: could not load recipients")
        return 0
    sent = 0
    for token, chat_id, role, allowed in targets:
        if not _may_hear_about(role, allowed, repo):
            continue
        if await send_telegram(token, chat_id, text):
            sent += 1
    # Both transports, one fan-out. Push is additive and must never be able to
    # cost a Telegram alert, so its failures are swallowed the same way the
    # loop above swallows a failed send.
    try:
        sent += await _push_operators(auth_pool, text, repo, url)
    except Exception:  # noqa: BLE001
        logger.exception("push: fan-out failed")
    return sent


def notify_operators_bg(auth_pool, text: str, repo: str | None = None, url: str = "/") -> None:
    """Fire-and-forget wrapper for call sites inside request/stream handlers."""
    try:
        live_state.fire_and_forget(notify_operators(auth_pool, text, repo, url))
    except Exception:  # noqa: BLE001
        logger.exception("telegram: could not schedule notification")


# ---------------------------------------------------------------------------
# message builders -- pure functions, unit-tested
# ---------------------------------------------------------------------------

_STATUS_LINES = {
    "escalated":         "🔴 Task ESCALATED — needs you",
    "awaiting_approval": "🟡 Task waiting on your APPROVAL",
    "awaiting_merge":    "🟡 Review READY — waiting on your merge approval",
    "done":              "✅ Task DONE",
    "error":             "🔴 Task ERROR",
    "auto_resumed":      "🔄 Task auto-resumed after a restart",
    "auto_healed":       "🩹 Task auto-healed — back through the gate",
    "auto_concluded":    "✅ Task's work is already on main — marked done",
    "planning_error":    "🔴 Planning turn FAILED",
}


def task_alert(kind: str, repo: str, goal: str, cost_usd: float | None, detail: str | None = None) -> str:
    """One alert message: what happened, which task, the cost so far, and the
    actionable detail (escalation reason / approval prompt / error)."""
    goal_line = (goal or "").strip().splitlines()[0][:120] if goal else "(no goal recorded)"
    lines = [
        _STATUS_LINES.get(kind, kind),
        f"repo: {repo}",
        f"task: {goal_line}",
    ]
    if cost_usd is not None:
        lines.append(f"cost so far: ${cost_usd:.2f}")
    if detail:
        lines.append("")
        lines.append(str(detail).strip()[:1500])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pm2 service-restart watch
# ---------------------------------------------------------------------------

def snapshot_services(pm2_jlist: list) -> dict:
    """{name: (pid, restart_count)} from `pm2 jlist` output."""
    snap = {}
    for proc in pm2_jlist:
        try:
            name = proc["name"]
            env = proc.get("pm2_env") or {}
            snap[name] = (proc.get("pid"), env.get("restart_time", 0), env.get("status"))
        except (KeyError, TypeError):
            continue
    return snap


def diff_services(prev: dict, cur: dict) -> list[str]:
    """Human alert lines for anything that restarted, died, or appeared since
    `prev`. Pure function -- the poller wraps it; tests pin it."""
    alerts = []
    for name, (pid, restarts, status) in cur.items():
        if name not in prev:
            continue  # new service appearing is a deploy action, not an incident
        p_pid, p_restarts, p_status = prev[name]
        if status != "online" and p_status == "online":
            alerts.append(f"🔴 service DOWN: {name} (status: {status})")
        elif restarts > p_restarts or (pid != p_pid and status == "online"):
            alerts.append(f"🔄 service restarted: {name} (restart #{restarts})")
    for name in prev:
        if name not in cur:
            alerts.append(f"🔴 service GONE from pm2: {name}")
    return alerts


async def watch_services(auth_pool, interval: float = 60.0, exclude: tuple = ("tektonix",)) -> None:
    """Poll `pm2 jlist` and alert on restarts/deaths of the OTHER services --
    the model router, other applications, the reviewers. The agent backend itself is
    excluded: its own restart resets this watcher's baseline (it runs inside
    that process), so it announces itself via the startup alert instead.
    Runs forever; every failure is swallowed after logging -- constraint 1.
    """
    if shutil.which("pm2") is None:
        # The compose bundle: no pm2, and compose restarts a dead service
        # itself. Polling would only print a traceback a minute (the first
        # Windows install's log, 2026-09-28).
        logger.info("service watch: no pm2 on this deployment; not polling")
        return

    import json as _json
    prev: dict = {}
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pm2", "jlist", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            cur = snapshot_services(_json.loads(out.decode() or "[]"))
            for name in exclude:
                cur.pop(name, None)
            if prev:
                for line in diff_services(prev, cur):
                    await notify_operators(auth_pool, line)
            prev = cur
        except Exception:  # noqa: BLE001
            logger.exception("service watch poll failed (will retry)")
        await asyncio.sleep(interval)
