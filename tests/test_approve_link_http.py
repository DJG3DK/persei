"""The approve link over HTTP: GET is inert, POST acts once and only from its
own page, and nobody can try tokens quickly.

The token logic itself is covered in tests/test_github_inbox.py; this is the
request handling around it (2026-09-23 review, finding 4.1).
"""
import base64
import dataclasses
import secrets

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import agent.server as srv
from agent import github_inbox as gi


@pytest.fixture
def client(monkeypatch):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)
    acted = []

    async def act(app, repo, key, action, nonce=None):
        if any(a[3] == nonce for a in acted):
            raise HTTPException(409, "already handled")     # the nonce is spent
        acted.append((repo, key, action, nonce))
        return {"task_id": "t" * 8, "item": {"title": "Fix it"}}

    async def item(store, repo, key):
        return {"title": "Fix it", "summary": "s", "kind": "code_scanning",
                "approval_nonce": "n1", "state": "proposed"} if key == "k1" else None

    from agent.routers import github as github_routes
    monkeypatch.setattr(github_routes, "_github_act", act)
    monkeypatch.setattr(srv.app.state, "store", object(), raising=False)
    monkeypatch.setattr(gi, "get_item", item)
    monkeypatch.setattr(srv.github_settings, "project_settings", lambda *a, **k: {"budget_usd": 2.0})
    c = TestClient(srv.app)
    c.acted = acted
    c.token = gi.sign_approval(fake, "proj", "k1", "n1")
    return c


def _post(client, token, **headers):
    return client.post("/api/github/approve", data={"t": token}, headers=headers)


def test_opening_the_link_does_not_start_anything(client):
    r = client.get(f"/api/github/approve?t={client.token}")
    assert r.status_code == 200 and "Approve and start" in r.text
    assert client.acted == []
    assert r.headers["cache-control"] == "no-store"


def test_the_button_on_its_own_page_starts_the_task(client):
    r = _post(client, client.token, **{"Sec-Fetch-Site": "same-origin"})
    assert "Task started" in r.text and len(client.acted) == 1
    assert r.headers["cache-control"] == "no-store"


def test_a_post_from_another_site_is_refused(client):
    r = _post(client, client.token, **{"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and client.acted == []
    r = _post(client, client.token, **{"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403 and client.acted == []


def test_a_used_link_does_not_start_a_second_task(client):
    _post(client, client.token)
    r = _post(client, client.token)
    assert "already used" in r.text and len(client.acted) == 1


def test_tokens_cannot_be_tried_quickly(client):
    for _ in range(10):
        _post(client, "not-a-token")
    r = _post(client, client.token)
    assert r.status_code == 429 and client.acted == []
