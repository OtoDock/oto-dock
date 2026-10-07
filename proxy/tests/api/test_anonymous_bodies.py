"""F70: a router whose routes all need a principal declares
``auth.providers.require_user``, so an anonymous request is refused 401
before FastAPI validates its body (a 422 would map the body's fields for a
caller with no account). The routers below hold that; the open routes
(sign-in, webhooks, links, WOPI) keep answering an anonymous caller."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Every router these modules own needs a principal on every route.
GUARDED = (
    "api.admin.admin_storage", "api.admin.execution_layers", "api.admin.title_generation",
    "api.agents.chats", "api.apps.app_secrets", "api.audio.audio",
    "api.auth.agent_api_keys", "api.auth.claude_oauth", "api.auth.openai_oauth",
    "api.auth.user_api_keys", "api.billing.billing", "api.billing.usage",
    "api.checks.checks", "api.departments.departments", "api.duplex.duplex",
    "api.events.subscriptions", "api.mcp.community", "api.mcp.credentials",
    "api.mcp.icons", "api.mcp.local_templates", "api.mcp.mcps",
    "api.media.uploads", "api.meetings.meetings",
    "api.notifications.notifications", "api.phone.phone", "api.sharing.shares",
    "api.tasks.continuations", "api.tasks.delegation", "api.tasks.tasks",
)


def _routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif type(r).__name__ == "_IncludedRouter":
            yield from _routes(r.original_router.routes)


@pytest.fixture(scope="module")
def client():
    from app import app
    return app, TestClient(app, raise_server_exceptions=False)


def test_the_guarded_routers_declare_the_dependency():
    import importlib

    from auth.providers import require_user
    for name in GUARDED:
        router = importlib.import_module(name).router
        assert any(d.dependency is require_user for d in router.dependencies), name


def test_an_anonymous_body_is_refused_before_it_is_validated(temp_db, client):
    app, c = client
    seen = 0
    for r in _routes(app.routes):
        if r.endpoint.__module__ not in GUARDED or not r.body_field:
            continue
        path = re.sub(r"\{[^}]+\}", "x", r.path)
        for method in r.methods - {"HEAD", "OPTIONS"}:
            resp = c.request(method, path, json={"__unknown__": 1})
            assert resp.status_code == 401, (method, r.path, resp.status_code, resp.text[:200])
            seen += 1
    assert seen > 50


def test_the_open_routes_still_take_an_anonymous_body(temp_db, client):
    _, c = client
    # A malformed sign-in is a 422 (no principal exists to require there).
    assert c.post("/auth/login/local", json={"__unknown__": 1}).status_code == 422
    assert c.post("/v1/csp-report", content=b"{}",
                  headers={"content-type": "application/csp-report"}).status_code == 204
