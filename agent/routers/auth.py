"""Sessions, second factors and accounts: the /api/auth/* routes.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-27. The
routes are unchanged -- tests/test_route_inventory.py pins every path,
method and guard, and the five that are reachable without a session (login,
the second factor, logout, the two password-reset steps) are listed there as
public with the reason each one is.

The guards stay the objects agent/auth.py defines: `require_full_auth` for
most of these, `auth.get_current_user` for the ones that must work mid-login
(2FA setup, change password) before the forced-screen gates pass. The
inventory identifies a guard by its `__name__` and every test that overrides
one is keyed on identity, so a copy here would have been a second guard with
the same name.

State comes off `request.app.state` (`auth_pool`, `config`, the store for the
audit log): importing `app` back from server.py, which includes this router,
would be a cycle. PROJECTS is read off `agent.config` at call time for the
same reason, so a test that swaps it is seen.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from agent import audit, auth, features, rate_limit
from agent import config as agent_config
from agent.auth import SESSION_COOKIE_NAME, User, require_full_auth
from agent.notify import send_telegram, task_alert
from agent.routers import audit_store

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class Verify2FARequest(BaseModel):
    temp_token: str
    code: str


class Setup2FARequest(BaseModel):
    password: str | None = None


class Confirm2FARequest(BaseModel):
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool = False
    auto_approve_repos: list[str] | None = None


class UpdateAutoApproveRequest(BaseModel):
    auto_approve_commands: bool
    # Which projects it covers. Required when turning it ON: a switch whose
    # blast radius nobody chose should not be the widest one.
    repos: list[str] | None = None


class UpdateUserAccessRequest(BaseModel):
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool | None = None
    auto_approve_repos: list[str] | None = None


def _validated_auto_repos(target: User, repos: list[str] | None, *, turning_on: bool) -> list[str] | None:
    """The projects an auto-approve switch may cover, or None to leave the
    stored scope alone.

    Turning it ON must name projects. The alternative -- an empty or absent
    list meaning "everywhere" -- is exactly the inheritance this scoping
    exists to stop: a second account handed the switch would silently get it
    for production as well as for the sandbox it was meant for.
    """
    if repos is None:
        if turning_on and not (target.auto_approve_repos or []):
            raise HTTPException(400, (
                "auto mode needs the projects it covers -- send `repos` with at least one, "
                "so turning it on cannot quietly mean every project"))
        return None
    unknown = [r for r in repos if r not in agent_config.PROJECTS]
    if unknown:
        raise HTTPException(400, f"unknown project(s): {', '.join(sorted(unknown))}")
    denied = [r for r in repos if not target.can_access(r)]
    if denied:
        raise HTTPException(403, f"{target.email} has no access to: {', '.join(sorted(denied))}")
    if turning_on and not repos:
        raise HTTPException(400, "auto mode with no projects does nothing -- name at least one")
    return repos


def _user_public(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "allowed_repos": user.allowed_repos, "totp_enabled": user.totp_enabled,
        "must_change_password": user.must_change_password,
        "require_totp_setup": user.role == "admin" and not user.totp_enabled and not auth.desktop_install(),
        "auto_approve_commands": user.auto_approve_commands,
        "auto_approve_repos": user.auto_approve_repos or [],
        "require_merge_review": user.require_merge_review,
        # None until the account picks one; the frontend maps that to the
        # default rather than the server writing the default into every row.
        "theme": user.theme or auth.DEFAULT_THEME,
        # Which licensed features this deployment has; the dashboard shows
        # or hides their pages by this.
        "features": features.public(),
    }


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def cookie_secure(request: Request | None) -> bool:
    """Whether the session cookie carries Secure: always, except over plain
    http to this machine's own loopback with no proxy in front.

    The desktop app talks to the agent at http://localhost:8100. Chromium
    (the Windows app's WebView2) treats localhost as a secure origin and
    keeps a Secure cookie there; WebKitGTK (the Linux app) does not, and
    dropped it -- every Linux sign-in answered 200 and landed back on the
    sign-in page (2026-10-04, found in an Ubuntu 24.04 VM). Loopback traffic
    never leaves the machine, so Secure protects nothing there. Anything
    behind a proxy (nginx sets Host to the public name, and says https in
    X-Forwarded-Proto) keeps Secure."""
    if request is None:
        return True
    host = (request.headers.get("host") or "").strip().lower()
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    forwarded = (request.headers.get("x-forwarded-proto") or "").lower()
    loopback = hostname in _LOOPBACK_HOSTS or hostname.endswith(".localhost")
    return not (request.url.scheme == "http" and loopback
                and forwarded in ("", "http"))


def _set_session_cookie(response: Response, token: str, request: Request | None = None) -> None:
    # SameSite=strict is this app's whole CSRF defence for the session API:
    # there are no CSRF tokens, because a strict cookie is never sent on a
    # request another site starts, and the SPA only ever calls same-origin.
    # That coupling is load-bearing. Loosening this to lax or none (for an
    # embed, an OAuth return, a subdomain) makes CSRF tokens -- or a
    # Sec-Fetch-Site check on every mutating route -- required in the same
    # change. The one unauthenticated acting POST, the approve link, has its
    # own Sec-Fetch-Site check (_approve_is_cross_site) for this reason.
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=auth.session_ttl_seconds(),
        httponly=True, samesite="strict", secure=cookie_secure(request), path="/",
    )


@router.post("/api/auth/login")
async def login(req: LoginRequest, response: Response, request: Request):
    email = req.email.strip().lower()
    # Per account (B25): a good login as one account clears only its own
    # misses, never the window on the account being guessed at.
    await rate_limit.check_rate_limit(request, "login", account=email)  # audit H-7
    row = await auth.get_user_by_email(request.app.state.auth_pool, email)
    # audit M-1: run argon2 on both branches so an unknown email takes the same
    # time as a real one (no user-enumeration timing oracle).
    if not row:
        auth.verify_password_absent()
        raise HTTPException(401, "invalid email or password")
    if not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    await rate_limit.clear_rate_limit(request, "login", account=email)
    if row["totp_enabled"]:
        # Returned in the body, held in page memory between the two login
        # steps: short-lived, single-use, and useless without the second
        # factor. The page that holds it is the one place script injection
        # would matter most, which is one more reason the dashboard never
        # renders model- or repo-supplied HTML (ChatMessage renders text, and
        # the CSP is script-src 'self').
        temp_token = await auth.create_pending_2fa(request.app.state.auth_pool, row["id"])
        return {"requires_2fa": True, "temp_token": temp_token}
    token = await auth.create_session(request.app.state.auth_pool, row["id"])
    _set_session_cookie(response, token, request)
    return {"requires_2fa": False, "user": _user_public(auth._row_to_user(row))}


@router.post("/api/auth/2fa/verify")
async def verify_2fa(req: Verify2FARequest, response: Response, request: Request):
    pending = await auth.resolve_pending_2fa(request.app.state.auth_pool, req.temp_token)
    # Counted per account once the token names one (B25); an expired or
    # unknown token counts against the IP alone. The token is single-use, so
    # being refused here costs a fresh password step, not a stuck login.
    await rate_limit.check_rate_limit(request, "verify-2fa",
                                account=pending["user_id"] if pending else None)  # audit H-7
    if not pending:
        raise HTTPException(401, "2FA challenge expired -- log in again")
    ok = await auth.verify_totp_or_recovery(request.app.state.auth_pool, request.app.state.config, pending["user_id"], req.code.strip())
    if not ok:
        raise HTTPException(400, "invalid code")
    await rate_limit.clear_rate_limit(request, "verify-2fa", account=pending["user_id"])
    token = await auth.create_session(request.app.state.auth_pool, pending["user_id"])
    _set_session_cookie(response, token, request)
    row = await auth.get_user_by_id(request.app.state.auth_pool, pending["user_id"])
    return {"user": _user_public(auth._row_to_user(row))}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response, agent_session: str | None = Cookie(default=None)):
    if agent_session:
        await auth.revoke_session(request.app.state.auth_pool, agent_session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/api/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    await rate_limit.check_rate_limit(request, "reset-request")  # audit H-7
    # Always {"ok": true} regardless of whether the email matches a real
    # account -- auth.request_password_reset itself silently no-ops for an
    # unknown email; the point is not letting the response tell an attacker
    # which emails are registered users.
    try:
        await auth.request_password_reset(request.app.state.auth_pool, request.app.state.config, req.email)
    except Exception:  # noqa: BLE001 -- an SMTP hiccup must not turn into "this email doesn't exist" info leakage either
        logger.exception("password reset email failed to send for %s", req.email)
    return {"ok": True}


@router.post("/api/auth/reset-password")
async def reset_password_endpoint(req: ResetPasswordRequest, request: Request):
    await rate_limit.check_rate_limit(request, "reset-password")  # audit H-7
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    ok = await auth.reset_password(request.app.state.auth_pool, req.email, req.code, req.new_password)
    if not ok:
        raise HTTPException(400, "invalid or expired code")
    return {"ok": True}


@router.get("/api/auth/me")
async def get_me(user: User = Depends(auth.get_current_user)):
    return _user_public(user)


@router.get("/api/auth/admin-check", status_code=204)
async def admin_check(user: User = Depends(auth.require_full_auth)):
    """204 for a signed-in admin, 401 or 403 otherwise, and nothing more.
    For a reverse proxy's auth_request in front of pages the agent does not
    serve itself (an operator's own report, say), so they share the
    dashboard's sign-in instead of a password of their own.

    The session cookie is host-only, so this works only for pages served on
    the console's own host -- which makes them same-origin with it: any
    script on such a page can call every admin API as the admin viewing
    it. SECURITY.md says to serve them with `Content-Security-Policy:
    sandbox` (audit B32)."""
    auth.require_admin(user)
    return Response(status_code=204)


@router.post("/api/auth/change-password")
async def change_password_endpoint(request: Request, req: ChangePasswordRequest, user: User = Depends(auth.get_current_user)):
    await rate_limit.check_rate_limit(request, "password-recheck", account=user.id)
    row = await auth.get_user_by_id(request.app.state.auth_pool, user.id)
    if not auth.verify_password(req.current_password, row["password_hash"]):
        raise HTTPException(401, "current password is incorrect")
    await rate_limit.clear_rate_limit(request, "password-recheck", account=user.id)
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    removed = await auth.change_password(request.app.state.auth_pool, user.id, req.new_password,
                                         keep_token=request.cookies.get(auth.SESSION_COOKIE_NAME))
    return {"ok": True, "passkeys_removed": removed or 0}


@router.post("/api/auth/2fa/setup")
async def setup_2fa(request: Request, req: Setup2FARequest = Setup2FARequest(), user: User = Depends(auth.get_current_user)):
    # audit H-3: start_totp_setup clears totp_enabled as it writes the new
    # secret, so an attacker with a live session could silently DISABLE 2FA by
    # hitting this endpoint -- bypassing /2fa/disable, which explicitly refuses
    # for admins. Re-authenticate with the password before re-initiating setup
    # when 2FA is already enabled. First-time setup (2FA off) needs no password:
    # the session already proves who they are, and there is nothing to protect.
    if user.totp_enabled:
        await rate_limit.check_rate_limit(request, "password-recheck", account=user.id)
        row = await auth.get_user_by_id(request.app.state.auth_pool, user.id)
        if not req.password or not auth.verify_password(req.password, row["password_hash"]):
            raise HTTPException(403, "current password required to re-initialize 2FA")
        await rate_limit.clear_rate_limit(request, "password-recheck", account=user.id)
    # Once-only by construction: every call mints a NEW secret (start_totp_setup
    # overwrites the pending one), so this response is the only time a given
    # secret leaves the server -- no GET returns it later. The raw secret rides
    # along with the provisioning URI for someone who cannot scan a QR code.
    secret, uri = await auth.start_totp_setup(request.app.state.auth_pool, request.app.state.config, user.id)
    return {"secret": secret, "uri": uri}


@router.post("/api/auth/2fa/confirm")
async def confirm_2fa(request: Request, req: Confirm2FARequest, user: User = Depends(auth.get_current_user)):
    codes = await auth.confirm_totp_setup(request.app.state.auth_pool, request.app.state.config, user.id, req.code.strip())
    return {"recovery_codes": codes}


class Disable2FARequest(BaseModel):
    password: str = ""


@router.post("/api/auth/2fa/disable")
async def disable_2fa_endpoint(request: Request, req: Disable2FARequest,
                               user: User = Depends(require_full_auth)):
    """Removing a second factor is exactly the action a stolen session would
    want, so it is not something a session alone should authorise.

    Two changes over the original: require_full_auth rather than
    get_current_user (a half-authenticated session must not reach this at
    all), and the current password, matching what /2fa/setup already demands
    to RE-initialise. Enabling 2FA needs no password because the session
    already proves identity and there is nothing yet to protect; disabling it
    destroys a protection, which is the asymmetry.
    """
    if user.role == "admin":
        raise HTTPException(403, "2FA cannot be disabled on the admin account")
    await rate_limit.check_rate_limit(request, "password-recheck", account=user.id)
    row = await auth.get_user_by_id(request.app.state.auth_pool, user.id)
    if not req.password or not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(403, "current password required to disable 2FA")
    await rate_limit.clear_rate_limit(request, "password-recheck", account=user.id)
    await auth.disable_totp(request.app.state.auth_pool, user.id)
    await audit.record(audit_store(request), actor=user.email, action="auth.2fa_disable", target=user.email)
    return {"ok": True}


class UpdateThemeRequest(BaseModel):
    theme: str


class UpdateMergeReviewRequest(BaseModel):
    require_merge_review: bool


@router.post("/api/auth/me/merge-review")
async def set_own_merge_review(request: Request, req: UpdateMergeReviewRequest, user: User = Depends(require_full_auth)):
    """Self-service for the same reason auto-approve is: turning the final
    look OFF removes a review the operator was doing for their own benefit,
    not a safety property someone else depends on -- the independent review
    service still gates every merge regardless. Captured onto each task at
    creation, so flipping this never changes a task already in flight."""
    await auth.update_require_merge_review(request.app.state.auth_pool, user.id, req.require_merge_review)
    await audit.record(audit_store(request), actor=user.email, action="settings.merge_review",
                       target=user.email,
                       detail="on" if req.require_merge_review else "off")
    return {"ok": True, "require_merge_review": req.require_merge_review}


@router.post("/api/auth/me/theme")
async def set_own_theme(request: Request, req: UpdateThemeRequest, user: User = Depends(require_full_auth)):
    """The account's colour scheme. Self-service and unaudited: it grants
    nothing and reveals nothing, and an audit line per colour change would
    bury the entries that matter."""
    try:
        await auth.update_theme(request.app.state.auth_pool, user.id, req.theme)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "theme": req.theme}


@router.post("/api/auth/me/auto-approve")
async def set_own_auto_approve(request: Request, req: UpdateAutoApproveRequest, user: User = Depends(require_full_auth)):
    """Self-service, deliberately not admin-only: this grants no capability
    the account doesn't already have -- every action it stops prompting for
    could be approved by hand, one at a time, by this same user today. It
    only removes the clicking. The destructive-command subset stays gated no
    matter what this is set to (see deep_agent.py's interrupt_on_for), which
    is what makes self-service reasonable rather than a way to switch off
    the safety net.
    """
    repos = _validated_auto_repos(user, req.repos, turning_on=req.auto_approve_commands)
    await auth.update_auto_approve(request.app.state.auth_pool, user.id, req.auto_approve_commands, repos)
    await audit.record(audit_store(request), actor=user.email, action="settings.auto_approve",
                       target=user.email,
                       detail=("on for " + ", ".join(repos) if req.auto_approve_commands and repos
                               else "on" if req.auto_approve_commands else "off"))
    return {"ok": True, "auto_approve_commands": req.auto_approve_commands,
            "auto_approve_repos": repos if repos is not None else (user.auto_approve_repos or [])}


class TelegramSettingsRequest(BaseModel):
    bot_token: str | None = None  # None/empty clears; masked sentinel keeps existing
    chat_id: str | None = None


@router.get("/api/auth/me/telegram")
async def get_telegram_settings_endpoint(request: Request, user: User = Depends(require_full_auth)):
    """Masked: reports whether a token is configured, never the token."""
    return await auth.get_telegram_settings(request.app.state.auth_pool, user.id)


@router.post("/api/auth/me/telegram")
async def set_telegram_settings_endpoint(request: Request, req: TelegramSettingsRequest, user: User = Depends(require_full_auth)):
    token = (req.bot_token or "").strip()
    chat_id = (req.chat_id or "").strip()
    if token == "__unchanged__":
        # The Settings page never receives the stored token back (masked
        # endpoint above), so "save" with an untouched token field must not
        # blank a working credential -- the sentinel keeps it.
        existing = await auth.get_telegram_settings(request.app.state.auth_pool, user.id)
        if existing["configured"]:
            await auth.update_telegram_chat_only(request.app.state.auth_pool, user.id, chat_id or None)
            return await auth.get_telegram_settings(request.app.state.auth_pool, user.id)
        token = ""
    await auth.update_telegram(request.app.state.auth_pool, user.id, token or None, chat_id or None)
    return await auth.get_telegram_settings(request.app.state.auth_pool, user.id)


@router.post("/api/auth/me/telegram/test")
async def test_telegram_endpoint(request: Request, user: User = Depends(require_full_auth)):
    """Sends a real message to THIS user's configured chat so the operator can
    verify the token/chat pair before trusting it with real alerts."""
    row = await auth.get_telegram_raw(request.app.state.auth_pool, user.id)
    if not row:
        raise HTTPException(400, "telegram is not configured -- save a bot token and chat id first")
    token, chat_id = row
    ok = await send_telegram(token, chat_id, task_alert(
        "done", "persei", "Test alert from the dashboard settings page",
        0.00, "If you can read this, task alerts will reach you here."))
    if not ok:
        raise HTTPException(502, "telegram rejected the send -- check the bot token and chat id (and that you have messaged the bot once)")
    return {"ok": True}


@router.get("/api/auth/users")
async def list_users_endpoint(request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    rows = await auth.list_users(request.app.state.auth_pool)
    return [_user_public(auth._row_to_user(r)) for r in rows]


@router.post("/api/auth/users", status_code=201)
async def create_user_endpoint(request: Request, req: CreateUserRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if not features.enabled(features.MULTI_USER):
        # The public build is single-operator; accounts beyond the first are
        # licensed (agent/features.py). Existing accounts are untouched.
        raise HTTPException(403, "user accounts beyond the first are a licensed feature and are not enabled "
                                 "on this deployment")
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    # audit H7: can_access() treats allowed_repos=None as UNRESTRICTED for any
    # role, and this field defaults to None -- so POST with {"role": "user"}
    # and no repo list minted an account that could reach every project. The
    # sibling PATCH endpoint already rejects this; the create path did not.
    if req.role != "admin" and req.allowed_repos is None:
        raise HTTPException(
            400, "a non-admin user needs an explicit allowed_repos list "
                 "(use [] for no access); omitting it would grant every repo")
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in agent_config.PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    error = auth.validate_password_strength(req.password)
    if error:
        raise HTTPException(400, error)
    scope: list[str] = []
    if req.auto_approve_commands:
        # A brand-new account cannot be handed a blanket switch: it is scoped
        # to the projects it is being granted, and an admin account (whose
        # allowed_repos is None, meaning everything) must name them. Checked
        # before create_user: this block once ran after it, so a bad scope
        # returned 400 with the account already made and no audit line, and
        # the retry hit 409 (2026-09-29).
        scope = list(req.auto_approve_repos if req.auto_approve_repos is not None else req.allowed_repos or [])
        if not scope:
            raise HTTPException(400, (
                "auto mode for a new account needs the projects it covers -- send "
                "`auto_approve_repos`, or create the account with `allowed_repos`"))
        unknown = [r for r in scope if r not in agent_config.PROJECTS]
        if unknown:
            raise HTTPException(400, f"unknown project(s): {', '.join(sorted(unknown))}")
    if await auth.get_user_by_email(request.app.state.auth_pool, req.email.strip().lower()):
        raise HTTPException(409, "a user with this email already exists")
    row = await auth.create_user(
        request.app.state.auth_pool, req.email.strip().lower(), req.password, req.role, req.allowed_repos,
        must_change_password=True,
    )
    await audit.record(audit_store(request), actor=user.email, action="auth.user_create",
                       target=row["email"],
                       detail=req.role if req.role == "admin" else
                       "user with " + (", ".join(sorted(req.allowed_repos or [])) or "no projects"))
    if req.auto_approve_commands:
        await auth.update_auto_approve(request.app.state.auth_pool, row["id"], True, scope)
        row = {**row, "auto_approve_commands": True, "auto_approve_repos": sorted(set(scope))}
        await audit.record(audit_store(request), actor=user.email, action="settings.auto_approve",
                           target=row["email"],
                           detail="on at account creation for " + ", ".join(sorted(set(scope))))
    return _user_public(auth._row_to_user(row))


@router.patch("/api/auth/users/{user_id}")
async def update_user_access_endpoint(request: Request, user_id: int, req: UpdateUserAccessRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in agent_config.PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    target = await auth.get_user_by_id(request.app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    # allowed_repos is meaningless for admin (always full access already);
    # auto_approve_commands is orthogonal to repo scope and applies to any
    # role, admin included -- it's the one most likely to want it.
    if req.allowed_repos is not None:
        if target["role"] == "admin":
            raise HTTPException(400, "the admin account always has full access")
        await auth.update_user_access(request.app.state.auth_pool, user_id, req.allowed_repos)
        await audit.record(audit_store(request), actor=user.email, action="auth.user_access",
                           target=target["email"],
                           detail=", ".join(sorted(req.allowed_repos)) or "no projects")
    if req.auto_approve_commands is not None or req.auto_approve_repos is not None:
        target_user = auth._row_to_user(target)
        enabled = (req.auto_approve_commands if req.auto_approve_commands is not None
                   else target_user.auto_approve_commands)
        repos = _validated_auto_repos(target_user, req.auto_approve_repos, turning_on=enabled)
        await auth.update_auto_approve(request.app.state.auth_pool, user_id, enabled, repos)
        # An admin granting someone else the right to skip prompts is the
        # single most consequential thing on the Users panel, and the person
        # it is granted to has no other way to learn who did it.
        await audit.record(
            audit_store(request), actor=user.email,
            action="settings.auto_approve_repos" if req.auto_approve_repos is not None
            else "settings.auto_approve",
            target=target["email"],
            detail=("on for " + ", ".join(repos)) if enabled and repos
            else ("on" if enabled else "off"))
    return {"ok": True}


@router.delete("/api/auth/users/{user_id}")
async def delete_user_endpoint(request: Request, user_id: int, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "cannot delete your own account")
    target = await auth.get_user_by_id(request.app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if target["role"] == "admin":
        raise HTTPException(400, "cannot delete the admin account")
    await auth.delete_user(request.app.state.auth_pool, user_id)
    await audit.record(audit_store(request), actor=user.email, action="auth.user_delete", target=target["email"])
    return {"ok": True}
