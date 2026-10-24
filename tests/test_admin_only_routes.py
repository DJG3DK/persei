"""Every admin-only route, pinned twice.

tests/test_route_inventory.py pins each route's authentication and
tests/test_repo_scope.py pins the per-repo check on repo-scoped routes. The
routes that are admin-only because of WHAT they touch rather than which
project -- the .env editor, model config, runtime settings, the audit log,
the daily jobs, user accounts, the router balance, the review proxy -- had
no structural pin: a seam extraction that dropped `auth.require_admin(user)`
from one of them left every test green (audit 2026-09-29, B22).

Two pins per route. First, `require_admin` is the handler's FIRST statement
(after the docstring), so nothing runs for a non-admin before the refusal --
not a store read, not a subprocess. Second, for the routes that need no
request body, a restricted user actually gets a 403.
"""
import ast
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User
from tests.test_route_inventory import _STATIC_PATHS, _walk

_RESTRICTED = User(id=2, email="dev@example.com", role="user", allowed_repos=["demo"],
                   totp_enabled=True, must_change_password=False,
                   auto_approve_commands=False, require_merge_review=True)


def _admin_routes():
    rows = []
    for r in _walk(srv.app.routes):
        if r.path in _STATIC_PATHS or not isinstance(r, (APIRoute, APIWebSocketRoute)):
            continue
        if "require_admin(" not in inspect.getsource(r.endpoint):
            continue
        methods = ["WS"] if isinstance(r, APIWebSocketRoute) else sorted(r.methods - {"HEAD", "OPTIONS"})
        for m in methods:
            rows.append((m, r.path))
    return sorted(rows)


# The admin-only surface. A route added here is admin-only from now on; one
# removed here stopped being admin-only, and the commit that does either
# says so by editing this list.
ADMIN_ONLY = [
    ('DELETE', '/_review/{path:path}'),
    ('DELETE', '/api/auth/users/{user_id}'),
    ('DELETE', '/api/projects/archives/{filename}'),
    ('DELETE', '/api/projects/{name}'),
    ('DELETE', '/api/projects/{name}/deploy-key'),
    ('GET', '/_review/{path:path}'),
    ('GET', '/api/analytics'),
    ('GET', '/api/auth/admin-check'),
    ('GET', '/api/analytics/benchmarks'),
    ('GET', '/api/analytics/models'),
    ('GET', '/api/analytics/tool-reliability'),
    ('GET', '/api/analytics/trace-summary'),
    ('GET', '/api/audit'),
    ('GET', '/api/auth/users'),
    ('GET', '/api/consolidation/status'),
    ('GET', '/api/env-config'),
    ('GET', '/api/evals'),
    ('GET', '/api/evals/runs/{name}'),
    ('GET', '/api/jobs'),
    ('GET', '/api/model-config'),
    ('GET', '/api/model-config/catalog'),
    ('GET', '/api/model-config/endpoints'),
    ('GET', '/api/model-config/privacy'),
    ('GET', '/api/projects'),
    ('GET', '/api/projects/archives'),
    ('GET', '/api/projects/{name}/checkout'),
    ('GET', '/api/projects/{name}/deploy-key'),
    ('GET', '/api/router-balance'),
    ('GET', '/api/settings/github'),
    ('GET', '/api/settings/runtime'),
    ('GET', '/api/swebench'),
    ('GET', '/api/swebench/runs/{name}'),
    ('GET', '/api/swebench/runs/{name}/log'),
    ('GET', '/api/swebench/runs/{name}/tasks/{instance_id}'),
    ('PATCH', '/_review/{path:path}'),
    ('PATCH', '/api/auth/users/{user_id}'),
    ('POST', '/_review/{path:path}'),
    ('POST', '/api/auth/users'),
    ('POST', '/api/env-config'),
    ('POST', '/api/env-config/restart'),
    ('POST', '/api/evals/run'),
    ('POST', '/api/evals/stop'),
    ('POST', '/api/github/poll'),
    ('POST', '/api/github/repos'),
    ('POST', '/api/jobs/{name}/run'),
    ('POST', '/api/model-config'),
    ('POST', '/api/model-config/privacy'),
    ('POST', '/api/model-config/probe-forced-tool-call'),
    ('POST', '/api/model-config/providers'),
    ('POST', '/api/model-config/restart-router'),
    ('POST', '/api/planning/sessions/{session_id}/new-project'),
    ('POST', '/api/projects/clone'),
    ('POST', '/api/projects/create'),
    ('POST', '/api/projects/detect'),
    ('POST', '/api/projects/onboard-github'),
    ('POST', '/api/projects/provision'),
    ('POST', '/api/projects/{name}/deploy-key'),
    ('POST', '/api/projects/{name}/deploy-key/generate'),
    ('POST', '/api/projects/{name}/deploy-key/test'),
    ('POST', '/api/projects/{name}/move'),
    ('POST', '/api/settings/github'),
    ('POST', '/api/settings/github/test'),
    ('POST', '/api/settings/runtime'),
    ('POST', '/api/swebench/run'),
    ('POST', '/api/swebench/runs/{name}/stop'),
    ('PUT', '/_review/{path:path}'),
]

# Routes any user may call whose handler holds an admin-only BRANCH: the
# planning "new project" decision is the session owner's, and only the
# create half of it asks for admin. Pinned as present, not as first.
CONDITIONAL = {
    ('POST', '/api/planning/sessions/{session_id}/new-project'),
}


def test_the_admin_only_surface_has_not_silently_changed():
    actual = _admin_routes()
    assert actual == sorted(ADMIN_ONLY), (
        "the admin-only routes changed. If intended, update ADMIN_ONLY in the same commit.\n"
        f"added: {sorted(set(actual) - set(ADMIN_ONLY))}\nremoved: {sorted(set(ADMIN_ONLY) - set(actual))}"
    )


def _first_statement(fn) -> ast.stmt | None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]          # the docstring
    return body[0] if body else None


def _calls_require_admin(stmt) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "require_admin"
               for n in ast.walk(stmt))


def _route(method, path):
    return next(r for r in _walk(srv.app.routes) if r.path == path
                and (method == "WS" or method in getattr(r, "methods", set())))


@pytest.mark.parametrize("method,path", [(m, p) for m, p in ADMIN_ONLY if (m, p) not in CONDITIONAL])
def test_require_admin_is_the_handlers_first_statement(method, path):
    first = _first_statement(_route(method, path).endpoint)
    assert first is not None and _calls_require_admin(first), (
        f"{method} {path}: require_admin is not the first thing the handler does")


# Required query parameters, so the request reaches the handler rather than
# stopping at a 422.
_QUERY = {"/api/model-config/endpoints": "?model=openai/gpt-4o"}


def _fill(path: str) -> str:
    filled = (path.replace("{name}", "demo").replace("{user_id}", "9").replace("{instance_id}", "i")
              .replace("{filename}", "a.json").replace("{path:path}", "x"))
    return filled + _QUERY.get(path, "")


def _takes_a_body(endpoint) -> bool:
    import typing
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001
        hints = {n: p.annotation for n, p in inspect.signature(endpoint).parameters.items()}
    return any(getattr(ann, "model_fields", None) is not None for ann in hints.values())


_BODYLESS = [(m, p) for m, p in ADMIN_ONLY
             if (m, p) not in CONDITIONAL and not _takes_a_body(_route(m, p).endpoint)]


@pytest.mark.parametrize("method,path", _BODYLESS)
def test_a_restricted_user_gets_403_from_each_admin_route(method, path, monkeypatch):
    """The routes that need no body (a body that fails validation is a 422
    before the handler runs, which would pin nothing): the request reaches
    the handler, and the handler's first statement refuses it."""
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _RESTRICTED)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.get_current_user, lambda: _RESTRICTED)
    monkeypatch.setitem(srv.PROJECTS, "demo", {"live": "/nowhere", "sandbox": "/nowhere"})
    res = TestClient(srv.app).request(method, _fill(path))
    assert res.status_code == 403, f"{method} {path} answered {res.status_code} to a restricted user: {res.text[:200]}"


def test_probe_script_path_exists():
    # The route moved into agent/routers/ and kept a parent.parent that now
    # stopped at agent/: every refresh was a FileNotFoundError 500.
    from agent.routers import model_config

    assert model_config.PROBE_SCRIPT.is_file()
