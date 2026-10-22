"""The two WebSockets refuse before the handshake, through the real handler.

tests/test_route_inventory.py only checks that each handler's source mentions
get_user_from_ws_cookie. These connect: no cookie closes 4401; a signed-in user
held at a forced screen, or without access to the repo, closes 4403. The
cookie is read by the real helper -- only the session lookup it ends in (a
database query) is stood in for.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import agent.server as srv
from agent import auth
from agent.auth import User

ROUTES = ["/api/tasks/t-1/stream", "/api/planning/sessions/s-1/stream"]


def _user(**over):
    base = dict(id=7, email="u@example.com", role="user", allowed_repos=["other-repo"],
                totp_enabled=True, must_change_password=False,
                auto_approve_commands=False, require_merge_review=True)
    base.update(over)
    return User(**base)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)

    async def task_repo(task_id):
        return "proj"

    async def planning_meta(session_id):
        return "proj", {"session_id": session_id, "repo": "proj"}

    from agent.routers import tasks as tasks_routes
    monkeypatch.setattr(tasks_routes, "_resolve_task_repo", lambda app, task_id: task_repo(task_id))
    monkeypatch.setattr(srv, "_find_planning_meta", planning_meta)
    return TestClient(srv.app)


def _close_code(client, route, cookie=None):
    if cookie:
        client.cookies.set(auth.SESSION_COOKIE_NAME, cookie)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(route) as ws:
            ws.receive_text()
    return e.value.code


@pytest.mark.parametrize("route", ROUTES)
def test_no_cookie_is_closed_4401(client, route):
    assert _close_code(client, route) == 4401


@pytest.mark.parametrize("route", ROUTES)
def test_a_dead_session_is_closed_4401(client, monkeypatch, route):
    async def resolve(pool, token):
        return None
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="expired-token") == 4401


@pytest.mark.parametrize("route", ROUTES)
def test_a_user_without_the_repo_is_closed_4403(client, monkeypatch, route):
    async def resolve(pool, token):
        return _user(allowed_repos=["other-repo"])
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="live-token") == 4403


@pytest.mark.parametrize("route", ROUTES)
def test_an_admin_without_2fa_is_held_at_the_forced_screen(client, monkeypatch, route):
    async def resolve(pool, token):
        return _user(role="admin", allowed_repos=None, totp_enabled=False)
    monkeypatch.setattr(auth, "resolve_session", resolve)
    assert _close_code(client, route, cookie="live-token") == 4403


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("origin", ["http://127.0.0.1:5000", "http://localhost:5173", "https://evil.example"])
def test_a_page_on_another_origin_is_refused_before_the_cookie_is_read(client, monkeypatch, route, origin):
    """B31 (2026-10-22): cookies ignore ports, so a page on another
    localhost port sent the session cookie and opened the stream."""
    looked_up = []

    async def resolve(pool, token):
        looked_up.append(token)
        return _user(allowed_repos=["proj"])
    monkeypatch.setattr(auth, "resolve_session", resolve)
    client.cookies.set(auth.SESSION_COOKIE_NAME, "live-token")
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(route, headers={"Origin": origin, "Host": "127.0.0.1:8100"}) as ws:
            ws.receive_text()
    assert e.value.code == 4403 and looked_up == []


@pytest.mark.parametrize("origin, host, ok", [
    ("http://127.0.0.1:8100", "127.0.0.1:8100", True),
    ("http://localhost:8100", "localhost:8100", True),
    ("https://agent.example.com", "agent.example.com", True),        # nginx's $host has no port
    ("https://agent.example.com:8443", "agent.example.com", True),
    ("http://127.0.0.1:5000", "127.0.0.1:8100", False),
    ("http://localhost:8100", "127.0.0.1:8100", False),
    ("https://chat.example.com", "agent.example.com", False),
    ("null", "agent.example.com", False),
    (None, "agent.example.com", True),                               # not a browser
])
def test_the_origin_must_be_the_host_the_handshake_came_to(origin, host, ok):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    assert auth.ws_origin_allowed(headers) is ok
