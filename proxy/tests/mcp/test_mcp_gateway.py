"""The credential gateway's model and resolver (core/credentials/mcp_gateway.py):
the entry URL a session's config carries, the per-request resolution of a
static value and of a token file, the refusals (no credential, a removed
allowlist row, a private vendor host, a dead account), the memos, and the
secret-free descriptor a session leaves behind for a re-adoption.
"""

import json
import os
import sys
import time
import uuid

import pytest

from tests._paths import PROXY_DIR
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import config  # noqa: E402
from core.credentials import mcp_broker, mcp_gateway  # noqa: E402
from core.credentials.mcp_gateway import (  # noqa: E402
    GatewayCredential, Refusal, Resolved, TokenRef,
)
from storage.identity import bearer_allowlist  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    mcp_gateway.forget_memos()
    yield
    mcp_gateway.forget_memos()


def _provider() -> str:
    return f"gw-{uuid.uuid4().hex[:8]}"


def _vendor(key: str, **kw) -> GatewayCredential:
    base = dict(upstream="https://mcp.example.com", path="/mcp", allowlist_key=key,
                value="tok-static", manifest="")
    base.update(kw)
    return GatewayCredential(**base)


def test_entry_url_names_the_route_the_proxy_port_and_the_endpoint_path():
    assert mcp_gateway.entry_url("linear-mcp", "/mcp") == (
        f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/linear-mcp/mcp")
    assert mcp_gateway.gateway_key_of(mcp_gateway.entry_url("linear-mcp", "/mcp")) == "linear-mcp"
    assert mcp_gateway.gateway_key_of("https://mcp.linear.app/mcp") is None
    assert mcp_gateway.gateway_key_of("http://127.0.0.1:8400/mcp/file-tools/mcp/") is None


def test_the_forward_is_confined_to_the_declared_path():
    assert mcp_gateway.path_matches("/mcp", "/mcp")
    assert mcp_gateway.path_matches("/mcp", "/mcp/")
    assert mcp_gateway.path_matches("/mcp/", "/mcp")
    assert not mcp_gateway.path_matches("/mcp", "/mcp/other")
    assert not mcp_gateway.path_matches("/mcp", "/")
    assert not mcp_gateway.path_matches("/mcp", "")
    assert not mcp_gateway.path_matches("", "/mcp")


def test_no_credential_for_the_session_and_mcp_is_a_refusal():
    out = mcp_gateway.resolve("ghost", "linear-mcp")
    assert isinstance(out, Refusal) and out.reason == "no_credential"
    mcp_broker.provision("s-gw-1", {"slack": mcp_broker.SecretBundle(env={"X": "1"})})
    out = mcp_gateway.resolve("s-gw-1", "slack")
    assert isinstance(out, Refusal) and out.reason == "no_credential"
    mcp_broker.purge_session("s-gw-1")


def test_a_static_value_resolves_to_the_header_when_the_host_is_allowed(monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    mcp_broker.provision("s-gw-2", {"vendor": mcp_broker.SecretBundle(gateway=_vendor(key))})
    out = mcp_gateway.resolve("s-gw-2", "vendor")
    assert isinstance(out, Resolved)
    assert out.header == "Authorization" and out.value == "Bearer tok-static"
    assert out.upstream == "https://mcp.example.com" and out.path == "/mcp"
    assert out.proxy_local is False and out.expires_in is None
    mcp_broker.purge_session("s-gw-2")


def test_a_header_style_key_keeps_its_name_and_no_prefix(monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mapstools.googleapis.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    cred = GatewayCredential(upstream="https://mapstools.googleapis.com", path="/mcp",
                             allowlist_key=key, header="X-Goog-Api-Key", prefix="",
                             value="AIza-key")
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Resolved) and (out.header, out.value) == ("X-Goog-Api-Key", "AIza-key")


def test_a_removed_allowlist_row_refuses_at_the_next_request(monkeypatch):
    key = _provider()
    row = bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    cred = _vendor(key)
    assert isinstance(mcp_gateway.resolve_credential(cred), Resolved)
    bearer_allowlist.delete_allowed(row)
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Refusal) and out.reason == "host_not_allowed"
    assert "Admin > Security" in out.detail


def test_a_wildcard_row_admits_the_vendor_subdomain(monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "*.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    assert isinstance(mcp_gateway.resolve_credential(_vendor(key)), Resolved)


def test_a_vendor_that_resolves_onto_the_platform_is_refused():
    # An allowlisted host that resolves to the platform's own loopback is
    # the self-SSRF the guard blocks; an ordinary LAN host the admin
    # approved is reached (the allowlist is the host authority).
    key = _provider()
    bearer_allowlist.add_allowed(key, "127.0.0.1", "test")
    bearer_allowlist.add_allowed(key, "169.254.169.254", "test")
    out = mcp_gateway.resolve_credential(_vendor(key, upstream="http://127.0.0.1:8080"))
    assert isinstance(out, Refusal) and out.reason == "egress_refused"
    assert "own address space" in out.detail
    out = mcp_gateway.resolve_credential(_vendor(key, upstream="http://169.254.169.254"))
    assert isinstance(out, Refusal) and out.reason == "egress_refused"


def test_a_sidecar_is_judged_under_localhost_and_skips_the_outbound_guard():
    key = _provider()
    bearer_allowlist.add_allowed(key, "localhost", "test")
    cred = GatewayCredential(upstream="http://github-mcp:8935", path="/mcp", allowlist_key=key,
                             value="ghp_x", proxy_local=True)
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Resolved) and out.proxy_local is True
    assert out.upstream == "http://github-mcp:8935"


def _token_file(tmp_path, label="acct", **fields):
    raw = {"access_token": "at-1", "refresh_token": "rt-1", "token_type": "bearer",
           "expires_at": "2099-01-01T00:00:00Z", "extra": {}}
    raw.update(fields)
    (tmp_path / f"{label}.json").write_text(json.dumps(raw))
    return raw


def test_a_token_file_is_read_per_request_and_followed_when_rewritten(tmp_path, monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    _token_file(tmp_path)
    cred = _vendor(key, value="", token_ref=TokenRef(str(tmp_path), "acct"))
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Resolved) and out.value == "Bearer at-1"
    assert out.expires_in is not None and out.expires_in > 3600
    # the refresh worker rewrote the file: the next request carries the new token
    time.sleep(0.01)
    _token_file(tmp_path, access_token="at-2")
    os.utime(tmp_path / "acct.json", None)
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Resolved) and out.value == "Bearer at-2"


def test_a_preferred_bearer_key_in_extra_wins(tmp_path, monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    _token_file(tmp_path, extra={"preferred_bearer": "user_token", "user_token": "xoxp-1"})
    cred = _vendor(key, value="", token_ref=TokenRef(str(tmp_path), "acct"))
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Resolved) and out.value == "Bearer xoxp-1"


def test_a_dead_account_is_a_reconnect_refusal_never_a_header(tmp_path, monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    _token_file(tmp_path, extra={"refresh_failed": "invalid_grant"})
    cred = _vendor(key, value="", token_ref=TokenRef(str(tmp_path), "acct"))
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Refusal) and out.reason == "needs_reconnect"
    assert "reconnect" in out.detail and "at-1" not in out.detail


def test_a_missing_token_file_or_an_empty_value_refuses(tmp_path, monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda u: None)
    cred = _vendor(key, value="", token_ref=TokenRef(str(tmp_path), "nobody"))
    out = mcp_gateway.resolve_credential(cred)
    assert isinstance(out, Refusal) and out.reason == "no_value"
    out = mcp_gateway.resolve_credential(_vendor(key, value=""))
    assert isinstance(out, Refusal) and out.reason == "no_value"


def test_the_descriptor_carries_references_and_never_a_value(tmp_path):
    sid = f"s-gw-{uuid.uuid4().hex[:8]}"
    creds = {
        "vendor": _vendor("linear", value="secret-value"),
        "maps": GatewayCredential(upstream="https://mapstools.googleapis.com", path="/mcp",
                                  allowlist_key="maps", header="X-Goog-Api-Key", prefix="",
                                  value="AIza-secret", manifest="maps"),
        "tok": _vendor("notion", value="", token_ref=TokenRef(str(tmp_path), "acct", "")),
    }
    mcp_gateway.write_descriptor(sid, creds, machine_id="m-1", token_hash="h" * 64)
    path = mcp_gateway.descriptor_dir() / f"{sid}.json"
    text = path.read_text()
    assert "secret-value" not in text and "AIza-secret" not in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    back = mcp_gateway.read_descriptor(sid)
    assert back["machine_id"] == "m-1" and back["token_hash"] == "h" * 64
    assert set(back["credentials"]) == {"vendor", "maps", "tok"}
    assert back["credentials"]["maps"].header == "X-Goog-Api-Key"
    assert back["credentials"]["maps"].value == ""
    assert back["credentials"]["tok"].token_ref == TokenRef(str(tmp_path), "acct", "")
    mcp_gateway.remove_descriptor(sid)
    assert mcp_gateway.read_descriptor(sid) is None
    mcp_gateway.remove_descriptor(sid)  # idempotent


def test_a_descriptor_with_nothing_to_record_is_removed_and_bad_ids_are_ignored():
    sid = f"s-gw-{uuid.uuid4().hex[:8]}"
    mcp_gateway.write_descriptor(sid, {"v": _vendor("x")})
    mcp_gateway.write_descriptor(sid, {})
    assert mcp_gateway.read_descriptor(sid) is None
    mcp_gateway.write_descriptor("../escape", {"v": _vendor("x")})
    assert not (mcp_gateway.descriptor_dir().parent / "escape.json").exists()
    assert mcp_gateway.read_descriptor("") is None


def test_the_broker_purge_removes_the_descriptor_and_runs_the_hooks():
    sid = f"s-gw-{uuid.uuid4().hex[:8]}"
    seen: list[str] = []
    mcp_gateway.add_purge_hook(seen.append)
    try:
        mcp_broker.provision(sid, {"v": mcp_broker.SecretBundle(gateway=_vendor("x"))})
        mcp_gateway.write_descriptor(sid, mcp_gateway.credentials_of(sid))
        assert mcp_gateway.read_descriptor(sid) is not None
        mcp_broker.purge_session(sid)
        assert mcp_gateway.read_descriptor(sid) is None
        assert seen == [sid]
        assert mcp_gateway.credentials_of(sid) == {}
    finally:
        mcp_gateway._purge.hooks.remove(seen.append)


def test_a_purge_hook_that_raises_never_reaches_the_cleanup():
    sid = f"s-gw-{uuid.uuid4().hex[:8]}"

    def _boom(_sid):
        raise RuntimeError("hook")
    mcp_gateway.add_purge_hook(_boom)
    try:
        mcp_broker.provision(sid, {"v": mcp_broker.SecretBundle(gateway=_vendor("x"))})
        mcp_broker.purge_session(sid)  # no raise
    finally:
        mcp_gateway._purge.hooks.remove(_boom)


def test_a_re_adoption_resolves_a_header_key_from_an_env_delivered_instance(monkeypatch):
    """``value_from`` may name an env-delivered ``instances.fields`` key: a
    re-adoption resolves it from the agent's instance as the session build
    does (the instance's value over the resolver's), not only from the
    resolver's per-MCP secrets."""
    from types import SimpleNamespace
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    from storage.mcp import mcp_store

    manifest = SimpleNamespace(
        name="maps-inst", instances=SimpleNamespace(delivery="env"), hosted=None)
    monkeypatch.setattr(mcp_registry, "get_manifest",
                        lambda name: manifest if name == "maps-inst" else None)
    monkeypatch.setattr(credential_resolver, "resolve_credentials",
                        lambda agent, user_sub, *, task_scope="user":
                        credential_resolver.ResolvedCredentials(env_by_mcp={}, secret_keys=set()))
    seen: dict = {}

    def chosen(mcp_name, agent_name):
        seen["call"] = (mcp_name, agent_name)
        return {"field_values": {"MAPS_KEY": "inst-key"}, "hosted_mode": "self"}

    monkeypatch.setattr(mcp_store, "get_instance_for_agent_env_delivery", chosen)
    creds = {"maps": _vendor("k", header="X-Key", prefix="", value="",
                             manifest="maps-inst", value_from="MAPS_KEY")}
    assert mcp_gateway.static_values_for("pa", "u-1", "user", creds) == {"maps": "inst-key"}
    assert seen["call"] == ("maps-inst", "pa")
