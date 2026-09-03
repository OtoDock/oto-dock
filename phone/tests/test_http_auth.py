"""Auth-middleware tests for the phone HTTP API.

Exercises the fail-closed path split in
``calls/http_api._auth_middleware_factory`` without binding a socket:
``/health`` is open, ``/v1/calls/register`` checks the per-server
``register_secrets`` set, everything else checks the global
``PHONE_API_SECRET``. Every guarded surface 401s on a missing/empty secret.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import config
from calls.http_api import _auth_middleware_factory
from config_manager import ConfigManager


def _req(path: str, token: str | None = None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return SimpleNamespace(path=path, headers=headers)


async def _ok_handler(request):
    return "HANDLED"


def _run(mw, req):
    return asyncio.run(mw(req, _ok_handler))


def _cfg(register_secrets):
    cfg = ConfigManager()
    cfg.load({"credentials": {"register_secrets": register_secrets}})
    return cfg


def test_health_is_open(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    mw = _auth_middleware_factory(_cfg(["r1"]))
    assert _run(mw, _req("/health")) == "HANDLED"
    assert _run(mw, _req("/health", token=None)) == "HANDLED"  # even with no token


def test_register_accepts_any_server_secret(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    mw = _auth_middleware_factory(_cfg(["r1", "r2"]))
    assert _run(mw, _req("/v1/calls/register", token="r1")) == "HANDLED"
    assert _run(mw, _req("/v1/calls/register", token="r2")) == "HANDLED"


def test_register_rejects_wrong_global_or_missing_token(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    mw = _auth_middleware_factory(_cfg(["r1"]))
    assert _run(mw, _req("/v1/calls/register", token="nope")).status == 401
    # the GLOBAL secret is NOT accepted on /register (per-server only)
    assert _run(mw, _req("/v1/calls/register", token="global")).status == 401
    assert _run(mw, _req("/v1/calls/register")).status == 401  # missing


def test_register_fail_closed_when_no_secrets(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    mw = _auth_middleware_factory(_cfg([]))  # zero servers provisioned
    assert _run(mw, _req("/v1/calls/register", token="r1")).status == 401
    assert _run(mw, _req("/v1/calls/register", token="")).status == 401


def test_api_calls_uses_global_secret_and_guards_subpaths(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    mw = _auth_middleware_factory(_cfg(["r1"]))
    assert _run(mw, _req("/api/calls", token="global")) == "HANDLED"
    # a register secret does NOT open /api/calls
    assert _run(mw, _req("/api/calls", token="r1")).status == 401
    # sub-paths are guarded too (the split is by prefix, not exact match)
    assert _run(mw, _req("/api/calls/abc/wait", token="global")) == "HANDLED"
    assert _run(mw, _req("/api/calls/abc/wait", token="nope")).status == 401


def test_api_calls_fail_closed_when_global_unset(monkeypatch):
    monkeypatch.setattr(config, "PHONE_API_SECRET", "")
    mw = _auth_middleware_factory(_cfg(["r1"]))
    assert _run(mw, _req("/api/calls", token="anything")).status == 401
    assert _run(mw, _req("/api/calls")).status == 401


def test_live_config_reload_adds_and_revokes(monkeypatch):
    """A pushed config replaces cfg._data in place, so the live middleware
    closure sees added secrets immediately and stops honoring revoked ones."""
    monkeypatch.setattr(config, "PHONE_API_SECRET", "global")
    cfg = _cfg(["r1"])
    mw = _auth_middleware_factory(cfg)  # closes over the live cfg object
    assert _run(mw, _req("/v1/calls/register", token="r2")).status == 401
    # proxy pushes a new server's secret → r2 now valid
    cfg.load({"credentials": {"register_secrets": ["r1", "r2"]}})
    assert _run(mw, _req("/v1/calls/register", token="r2")) == "HANDLED"
    # ...and deleting the first server revokes r1
    cfg.load({"credentials": {"register_secrets": ["r2"]}})
    assert _run(mw, _req("/v1/calls/register", token="r1")).status == 401
