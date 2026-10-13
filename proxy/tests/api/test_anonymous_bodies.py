"""Deny by default.

Every HTTP route of the app answers a caller with no credential 401 or 403,
or is named in ``OPEN_ROUTES`` with the reason it is open: a route added
without a guard fails here. Each route is called with its parameters and
body built from its own schema, so its handler runs and its guard is what
answers (a 422 would only prove the body was wrong). A route that judges its
own bearer (the session, hook, gateway, phone and WOPI routes, the trigger
receivers) is not open: called with none, it refuses. A member's cookie on
every ``/v1/admin`` route is refused too. WebSocket routes and the static
mounts are not ``APIRoute``s and stay outside this file (the sockets
authenticate in their own tests).

F70 rides along: a router whose routes all need a principal declares
``auth.providers.require_user``, so an anonymous request is refused 401
before FastAPI validates its body (a 422 would map the body's fields for a
caller with no account)."""

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


# --- A request each route accepts: its parameters and body built from its
# own schema, so its handler runs and its guard is what answers. ---

def _value(schema: dict, defs: dict, depth: int = 0):
    """The simplest value ``schema`` accepts: required fields only, the first
    enum member, a string as long as its minimum."""
    if depth > 12:
        return None
    if "$ref" in schema:
        return _value(defs[schema["$ref"].rsplit("/", 1)[1]], defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    for key in ("anyOf", "oneOf", "allOf"):
        options = [o for o in schema.get(key) or [] if o.get("type") != "null"]
        if options:
            return _value(options[0], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: _value(props[k], defs, depth + 1) for k in schema.get("required") or []}
    if kind == "array":
        item = _value(schema.get("items") or {}, defs, depth + 1)
        return [item] * int(schema.get("minItems") or 0)
    if kind == "integer":
        return max(1, int(schema.get("minimum") or 0))
    if kind == "number":
        return max(1.0, float(schema.get("minimum") or 0))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    choices = re.fullmatch(r"\^?\(([\w|-]+)\)\$?", schema.get("pattern") or "")
    if choices:
        return choices.group(1).split("|")[0]
    fmt = schema.get("format")
    if fmt == "email":
        return "a@example.com"
    if fmt in ("date-time", "date"):
        return "2026-01-01T00:00:00" if fmt == "date-time" else "2026-01-01"
    return "x" * max(1, int(schema.get("minLength") or 1))


def _field_value(field):
    from typing import Annotated

    from pydantic import TypeAdapter
    try:
        schema = TypeAdapter(Annotated[field.field_info.annotation, field.field_info]).json_schema(
            ref_template="#/$defs/{model}")
    except Exception:
        return "x"
    return _value(schema, schema.get("$defs") or {})


def _request_for(r) -> tuple[str, dict]:
    """The URL and the request arguments that satisfy ``r``'s parameters."""
    from fastapi import params as fa_params
    from fastapi.dependencies.utils import get_flat_dependant
    flat = get_flat_dependant(r.dependant)
    path = r.path
    for f in flat.path_params:
        value = _field_value(f)
        path = re.sub(r"\{" + re.escape(f.alias) + r"(:[^}]*)?\}", str(value), path)
    query = {f.alias: _field_value(f) for f in flat.query_params if f.field_info.is_required()}
    headers = {f.alias: str(_field_value(f)) for f in flat.header_params
               if f.field_info.is_required()}
    kw: dict = {"params": query, "headers": headers}
    if r.body_field is not None:
        body = _field_value(r.body_field)
        if any(isinstance(f.field_info, (fa_params.Form, fa_params.File)) for f in flat.body_params):
            files = {f.alias: ("x.txt", b"x") for f in flat.body_params
                     if isinstance(f.field_info, fa_params.File)}
            form = {f.alias: str(_field_value(f)) for f in flat.body_params
                    if not isinstance(f.field_info, fa_params.File)}
            kw.update(data=form, files=files or None)
        else:
            kw["json"] = body
    return path, kw


# What a caller without an account may use by design, and why. Every other
# route refuses it. A key is a path when one handler owns every method on it,
# else "METHOD path" per open method (the sweep checks that a path key names a
# one-handler path, so a guarded handler added beside an open one is swept).
_SIGN_IN = "the sign-in surface: no account exists yet"
_LINK = "an external link: its own unguessable token and, when set, its password"
_APP = "an app route: the app's launch, viewer or inbound credential, judged once the app is found"
OPEN_ROUTES = {
    "/auth/config": _SIGN_IN, "/auth/login": _SIGN_IN, "/auth/oidc-url": _SIGN_IN,
    "GET /auth/callback": _SIGN_IN, "POST /auth/callback": _SIGN_IN,
    "/auth/logout": "logs out whatever cookie it is given",
    "/auth/login/local": _SIGN_IN, "/auth/login/2fa": _SIGN_IN + " (the step token)",
    "/auth/forgot-password": _SIGN_IN, "/auth/reset-password": _SIGN_IN + " (the reset token)",
    "/auth/accept-invite": _SIGN_IN + " (the invite token)",
    "/auth/setup": "the setup wizard, open until the first account exists",
    "/auth/passkey/options": _SIGN_IN, "/auth/passkey/verify": _SIGN_IN,
    "/auth/passkey/native/start": _SIGN_IN, "/auth/passkey/native/exchange": _SIGN_IN,
    "/health": "liveness for the edge and the installer, no data",
    "/v1/csp-report": "the browser's script-policy reports",
    "/v1/webhooks/relay/{provider_id}": "vendor deliveries through the relay, signed per vendor",
    "/v1/webhooks/{provider_id}/{subscription_id}": "vendor deliveries, signed per subscription",
    "/v1/account/connect/callback": "the commercial account connect's return page",
    "/v1/oauth/{provider}/callback": "an OAuth provider's redirect back (the state binds it)",
    "/v1/oauth/microsoft/admin-consent/callback": "Microsoft's admin-consent redirect back",
    "/v1/images/temp/{token}": "a temporary image by its unguessable token",
    "/v1/twilio/inbound/{server_id}": "Twilio's signed call webhook",
    "/v1/twilio/status/{server_id}": "Twilio's signed status webhook",
    "/v1/twilio/transfer/{server_id}": "Twilio's signed action callback of a transfer's Dial",
    "/v1/satellite/{machine_id}/self-uninstall": "a machine removing itself proves its own token in the body",
    "/dashboard/{path:path}": "the dashboard's pages", "/{path:path}": "the dashboard's pages",
}
_WRITES = ("GET", "POST", "PUT", "PATCH", "DELETE")
OPEN_ROUTES.update({p: _APP for p in (
    *(f"{m} /v1/apps/{{app_id}}/api/{{path:path}}" for m in _WRITES),
    "/v1/apps/{app_id}/client/{sha}/{path:path}",
    "/v1/apps/{app_id}/platform/{method}", "/v1/apps/{app_id}/push", "PATCH /v1/apps/{app_id}/state",
    "/v1/apps/{app_id}/bindings/{name}/snapshot/{snap}", "/v1/apps/{app_id}/bindings/{name}/{path:path}",
    "/v1/apps/{app_id}/events", "/v1/apps/{app_id}/egress/{host}/{path:path}",
    "/v1/apps/{app_id}/inbound/{name}",
)})
OPEN_ROUTES.update({p: _LINK for p in (
    "/s/{token}", "/s/{token}/html", "/s/{token}/unlock", "/s/{token}/viewer-token",
    "POST /s/{token}/session", "DELETE /s/{token}/session",
    *(f"{m} /s/{{token}}/api/{{path:path}}" for m in _WRITES), "/s/{token}/client/{sha}/",
    "/s/{token}/client/{sha}/{path:path}", "/s/{token}/snapshot", "/s/{token}/ui/{media_token}",
    "/s/{token}/media/{media_token}", "/s/{token}/state", "/s/{token}/actions/batch",
    "/s/{token}/actions/{action_id}", "/s/{token}/{rest:path}",
)})


@pytest.fixture(scope="module")
def client():
    from app import app
    return app, TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def no_outbound(monkeypatch):
    """A route that ran past a missing guard must not reach out: a connect
    anywhere but this machine fails the request instead."""
    import ipaddress
    import socket
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if host:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise ConnectionRefusedError(f"no outbound connection in this test ({host})")
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield
    from auth import rate_limiter
    rate_limiter._attempts.clear()


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


def _methods(r) -> list[str]:
    return sorted(r.methods - {"HEAD", "OPTIONS"})


def _calls(app, call_filter=lambda method, path: True):
    for r in _routes(app.routes):
        for method in _methods(r):
            if not call_filter(method, r.path):
                continue
            url, kw = _request_for(r)
            yield r, method, url, kw


def _open(method: str, path: str) -> bool:
    return path in OPEN_ROUTES or f"{method} {path}" in OPEN_ROUTES


def test_every_route_refuses_a_caller_with_no_credential(temp_db, client, no_outbound,
                                                         monkeypatch):
    from api.media import collabora_proxy
    app, c = client
    # The editor relay answers 404 to everyone unless the editor runs under
    # the dashboard's own address: put it there, so its gate is what answers.
    monkeypatch.setattr(collabora_proxy, "_is_subpath_mode", lambda: True)
    methods_of: dict[str, set[str]] = {}
    handlers_of: dict[tuple[str, str], set] = {}
    for r in _routes(app.routes):
        methods_of.setdefault(r.path, set()).update(_methods(r))
        for m in _methods(r):
            handlers_of.setdefault((r.path, m), set()).add(r.endpoint)
    stale, imprecise = [], []
    for key in OPEN_ROUTES:
        method, _, path = key.rpartition(" ")
        if method and method not in methods_of.get(path, ()):
            stale.append(key)
        elif not method and key not in methods_of:
            stale.append(key)
        elif not method and len({frozenset(handlers_of[(key, m)]) for m in methods_of[key]}) > 1:
            imprecise.append((key, sorted(methods_of[key])))
    assert not stale, stale
    assert not imprecise, "an open path whose methods have different handlers names each " \
                          "open method as 'METHOD path':\n" + "\n".join(map(str, imprecise))
    wrong, seen = [], 0
    for r, method, url, kw in _calls(app, lambda m, p: not _open(m, p)):
        resp = c.request(method, url, **kw)
        seen += 1
        if resp.status_code not in (401, 403):
            wrong.append((resp.status_code, method, r.path, resp.text[:120]))
    assert not wrong, "routes open to a caller with no credential (guard them, or name "\
                      "them in OPEN_ROUTES with the reason):\n" + "\n".join(map(str, wrong))
    assert seen > 500


def test_a_member_is_refused_on_every_admin_route(temp_db, client, no_outbound):
    from auth.providers import create_session_jwt
    app, _ = client
    member = TestClient(app, raise_server_exceptions=False, cookies={
        "session": create_session_jwt("user-viewer", "viewer@test.com", "Viewer User", "member")})
    wrong, seen = [], 0
    for r, method, url, kw in _calls(app, lambda m, p: p.startswith("/v1/admin")):
        resp = member.request(method, url, **kw)
        seen += 1
        # The admin guard's own refusal, not a later guard's 403 (a route
        # whose admin check became a creator check would still answer 403).
        detail = resp.json().get("detail") if resp.status_code == 403 else resp.text[:200]
        if detail != "Admin access required":
            wrong.append((resp.status_code, method, r.path, str(detail)[:120]))
    assert not wrong, "\n".join(map(str, wrong))
    assert seen > 100
