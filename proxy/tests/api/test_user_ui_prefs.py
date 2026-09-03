"""HTTP tests for the per-user roaming UI preferences bag
(GET/PUT /v1/users/me/ui-prefs — the audio-prefs pattern).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _make_client(sub: str, email: str) -> TestClient:
    from api.audio import audio as audio_router

    app = FastAPI()
    app.include_router(audio_router.router)

    async def _user():
        # Subs must exist in the DB (temp_db seeds them; the prefs row FKs users).
        return UserContext(sub=sub, email=email, name="U",
                           role="member", agents=[], agent_roles={})

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


@pytest.fixture
def client(temp_db):
    return _make_client("user-viewer", "viewer@test.com")


def test_get_default_empty(client):
    assert client.get("/v1/users/me/ui-prefs").json() == {}


def test_put_get_roundtrip(client):
    r = client.put("/v1/users/me/ui-prefs",
                   json={"last_execution_mode": {"helper": "interactive"}})
    assert r.status_code == 200
    assert r.json() == {"last_execution_mode": {"helper": "interactive"}}
    assert client.get("/v1/users/me/ui-prefs").json() == {
        "last_execution_mode": {"helper": "interactive"}
    }


def test_shallow_merge_keeps_other_keys(client):
    assert client.put("/v1/users/me/ui-prefs", json={"a": 1}).status_code == 200
    assert client.put("/v1/users/me/ui-prefs", json={"b": 2}).status_code == 200
    assert client.get("/v1/users/me/ui-prefs").json() == {"a": 1, "b": 2}
    # Provided top-level keys REPLACE (shallow, not deep): b overwritten, a kept.
    assert client.put("/v1/users/me/ui-prefs", json={"b": {"x": True}}).status_code == 200
    assert client.get("/v1/users/me/ui-prefs").json() == {"a": 1, "b": {"x": True}}


def test_oversize_payload_rejected(client):
    r = client.put("/v1/users/me/ui-prefs", json={"blob": "x" * 9000})
    assert r.status_code == 413
    # Nothing was stored.
    assert client.get("/v1/users/me/ui-prefs").json() == {}


def test_other_user_isolation(client, temp_db):
    assert client.put("/v1/users/me/ui-prefs", json={"a": 1}).status_code == 200
    other = _make_client("user-viewer2", "viewer2@test.com")
    assert other.get("/v1/users/me/ui-prefs").json() == {}
    assert other.put("/v1/users/me/ui-prefs", json={"a": 99}).status_code == 200
    # Each user reads back their own bag.
    assert client.get("/v1/users/me/ui-prefs").json() == {"a": 1}
    assert other.get("/v1/users/me/ui-prefs").json() == {"a": 99}
