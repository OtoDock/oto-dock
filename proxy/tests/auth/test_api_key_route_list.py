"""The routes a person's API key reaches, pinned against the app's own route
table (``auth/api_key_endpoints.py``): every HTTP route is judged with
sample values for its parameters, the listed set is exactly the public
routes and the two webhook fires, each listed pattern matches one route, and
every other route answers 403 to a key bearer before it runs. A new route a
key should reach joins the list, the description and this test together."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app import app
from auth import api_key_endpoints as endpoints, api_keys
from tests.api.test_anonymous_bodies import _routes

KEY = "otok_" + "A" * 43

LISTED = {
    ("GET", "/v1/api/me"),
    ("GET", "/v1/api/agents"),
    ("GET", "/v1/api/openapi.json"),
    ("GET", "/v1/api/otodock.py"),
    ("GET", "/v1/api/chats"),
    ("POST", "/v1/api/chats"),
    ("GET", "/v1/api/chats/{chat_id}"),
    ("POST", "/v1/api/chats/{chat_id}/messages"),
    ("GET", "/v1/api/chats/{chat_id}/messages/{message_id}"),
    ("GET", "/v1/api/tasks"),
    ("POST", "/v1/api/tasks"),
    ("POST", "/v1/api/tasks/{task_id}/run"),
    ("GET", "/v1/api/runs/{run_id}"),
    ("GET", "/v1/api/runs/{run_id}/stream"),
    ("GET", "/v1/api/agents/{agent}/files"),
    ("GET", "/v1/api/agents/{agent}/files/{path:path}"),
    ("PUT", "/v1/api/agents/{agent}/files/{path:path}"),
    ("GET", "/v1/api/notifications"),
    ("POST", "/v1/api/notifications"),
    ("POST", "/v1/webhooks/agent/{agent}/{slug}"),
    ("POST", "/v1/webhooks/user/{username}/{slug}"),
}

# The families a key never reaches, named one by one.
NEVER = (
    ("POST", "/v1/user-api-keys"), ("GET", "/v1/user-api-keys"),
    ("DELETE", "/v1/user-api-keys/{key_id}"),
    ("POST", "/v1/agents/{agent}/api-keys"),
    ("GET", "/v1/admin/api-keys"), ("DELETE", "/v1/admin/api-keys/{kind}/{key_id}"),
    ("PUT", "/v1/users/me/password"),
    ("POST", "/v1/users/me/totp/setup"), ("DELETE", "/v1/users/me/totp"),
    ("POST", "/v1/hooks/permission"),
    ("GET", "/v1/admin/users"),
)


def _sample(path: str) -> str:
    path = re.sub(r"\{[^}]+:path\}", "a/b.txt", path)
    return re.sub(r"\{[^}]+\}", "sample", path)


def _http_routes() -> list[tuple[str, str]]:
    seen = set()
    for route in _routes(app.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            seen.add((method, route.path))
    return sorted(seen)


def test_the_list_is_exactly_the_public_routes_and_the_webhook_fires():
    routes = _http_routes()
    listed = {(m, p) for m, p in routes if endpoints.key_route(m, _sample(p)) is not None}
    assert listed == LISTED
    for route in endpoints.listed():
        for method in route.methods:
            hits = [p for m, p in routes if m == method and route.pattern.match(_sample(p))]
            assert len(hits) == 1, (route.pattern.pattern, method, hits)


def test_every_other_route_refuses_a_key_before_it_runs():
    client = TestClient(app, headers={"Authorization": f"Bearer {KEY}"})
    refused = [(m, p) for m, p in _http_routes() if (m, p) not in LISTED]
    assert len(refused) > 600
    for method, path in refused:
        r = client.request(method, _sample(path))
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json()["detail"] == api_keys.NOT_LISTED, (method, path)
    for method, path in NEVER:
        assert (method, path) in refused, (method, path)
    print(f"{len(refused)} routes refuse a key")


def test_an_encoded_or_climbing_path_is_judged_as_routed():
    assert endpoints.key_route("GET", "/v1/api/chats/../../admin/users") is None
    assert endpoints.key_route("GET", "/v1/api/me%3F/x") is None
    assert endpoints.key_route("DELETE", "/v1/api/chats") is None


def test_a_webhook_senders_address_may_end_in_a_slash():
    assert endpoints.key_route("POST", "/v1/webhooks/user/ada/deploy/") is not None
    assert endpoints.key_route("POST", "/v1/webhooks/agent/pa/deploy/") is not None
    assert endpoints.key_route("POST", "/v1/webhooks/agent/pa/deploy/extra") is None


def test_a_key_on_a_websocket_resolves_to_nobody():
    import asyncio

    from starlette.requests import HTTPConnection

    from auth.providers import _resolve_principal
    scope = {"type": "websocket", "path": "/ws/dashboard", "headers": [
        (b"authorization", f"Bearer {KEY}".encode())], "query_string": b""}
    assert asyncio.run(_resolve_principal(HTTPConnection(scope))) is None
