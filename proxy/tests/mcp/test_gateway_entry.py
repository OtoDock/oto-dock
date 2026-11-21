"""The builder's one writer of a credentialed remote-HTTP entry
(``mcp_registry.gateway_entry``) and what the local copies carry after it:
the gateway route and the session-token sentinel, never a vendor secret,
in the shared build file, the per-session Claude copy and the Codex TOML.
"""

import json
import sys
import uuid
from types import SimpleNamespace

from tests._paths import PROXY_DIR

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import config
from auth.session_token import SESSION_JWT_SENTINEL_BEARER
from core.credentials.mcp_gateway import GatewayCredential, TokenRef
from services.mcp import mcp_registry, mcp_server_resolve
from services.mcp.mcp_manifest_types import (
    CredentialConfig,
    McpManifest,
    ServerConfig,
)
from storage.identity import bearer_allowlist


def _manifest(name, *, transport="streamable_http", url_template="https://mcp.example.com/mcp",
              oauth=None, api_key_header=None, cred_type="per_user", fields=None,
              runtime="none"):
    return McpManifest(
        name=name, label=name.title(), description="", version="1.0.0", category="community",
        server=ServerConfig(runtime=runtime, transport=transport, url_template=url_template,
                            port=8935 if runtime == "docker" else 0),
        credentials=CredentialConfig(type=cred_type, fields=fields or [], oauth=oauth,
                                     api_key_header=api_key_header),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[],
    )


def _provider():
    return f"gw-{uuid.uuid4().hex[:8]}"


def _call(entry, manifest, **kw):
    base = dict(srv_key=manifest.name, user_sub="u-1", agent_name="agent", task_scope="user",
                bundle_env={})
    base.update(kw)
    return mcp_registry.gateway_entry(entry, manifest, **base)


def test_a_stdio_entry_and_an_uncredentialed_http_entry_are_untouched():
    m = _manifest("x", transport="stdio")
    entry = {"type": "stdio", "command": "x", "args": []}
    assert _call(entry, m) == (None, None)
    assert entry == {"type": "stdio", "command": "x", "args": []}
    m = _manifest("y", oauth={"provider_id": "p"})
    entry = {"type": "http", "url": "https://mcp.example.com/mcp"}
    assert _call(entry, m) == (None, None)
    assert entry["url"] == "https://mcp.example.com/mcp" and "headers" not in entry


def test_a_bearer_entry_becomes_the_gateway_route_with_the_token_sentinel(monkeypatch, tmp_path):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    m = _manifest("vendor", oauth={"provider_id": key, "bearer_required": True,
                                   "proposed_hosts": ["mcp.example.com"]})
    monkeypatch.setattr(mcp_server_resolve, "_gateway_bearer_source",
                        lambda *a, **k: (TokenRef(str(tmp_path), "acct", ""), None))
    entry = {"type": "http", "url": "https://mcp.example.com/mcp"}
    cred, reason = _call(entry, m)
    assert reason is None
    assert entry["url"] == f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/vendor/mcp"
    assert entry["headers"]["Authorization"] == SESSION_JWT_SENTINEL_BEARER
    assert isinstance(cred, GatewayCredential)
    assert cred.upstream == "https://mcp.example.com" and cred.path == "/mcp"
    assert cred.allowlist_key == key and cred.token_ref == TokenRef(str(tmp_path), "acct", "")
    assert cred.proxy_local is False and cred.value == "" and cred.manifest == "vendor"


def test_a_header_key_entry_takes_the_value_from_the_bundle_env():
    key = "maps"
    bearer_allowlist.add_allowed(key, "mapstools.googleapis.com", "test")
    m = _manifest("maps", url_template="https://mapstools.googleapis.com/mcp",
                  cred_type="infra",
                  fields=[{"key": "GOOGLE_MAPS_API_KEY", "label": "k", "input_type": "password"}],
                  api_key_header={"name": "X-Goog-Api-Key", "value_from": "GOOGLE_MAPS_API_KEY",
                                  "proposed_hosts": ["mapstools.googleapis.com"]})
    entry = {"type": "http", "url": "https://mapstools.googleapis.com/mcp"}
    cred, reason = _call(entry, m, bundle_env={"GOOGLE_MAPS_API_KEY": "AIza-1"})
    assert reason is None
    assert entry["url"].endswith("/v1/mcp-gateway/maps/mcp")
    assert entry["headers"]["Authorization"] == SESSION_JWT_SENTINEL_BEARER
    assert (cred.header, cred.prefix, cred.value) == ("X-Goog-Api-Key", "", "AIza-1")
    assert cred.allowlist_key == "maps" and cred.token_ref is None
    rows = [e["id"] for e in bearer_allowlist.list_allowed() if e["provider_id"] == key]
    for r in rows:
        bearer_allowlist.delete_allowed(r)


def test_an_empty_key_value_leaves_the_mcp_out_with_a_reason():
    m = _manifest("maps", url_template="https://mapstools.googleapis.com/mcp",
                  fields=[{"key": "GOOGLE_MAPS_API_KEY", "label": "k", "input_type": "password"}],
                  api_key_header={"name": "X-Goog-Api-Key", "value_from": "GOOGLE_MAPS_API_KEY",
                                  "proposed_hosts": ["mapstools.googleapis.com"]})
    entry = {"type": "http", "url": "https://mapstools.googleapis.com/mcp"}
    cred, reason = _call(entry, m, bundle_env={})
    assert cred is None and "GOOGLE_MAPS_API_KEY" in reason
    assert entry["url"] == "https://mapstools.googleapis.com/mcp"


def test_an_unapproved_host_leaves_the_mcp_out_naming_the_security_tab(monkeypatch, tmp_path, caplog):
    key = _provider()
    m = _manifest("vendor", url_template="https://attacker.example.com/mcp",
                  oauth={"provider_id": key, "bearer_required": True,
                         "proposed_hosts": ["attacker.example.com"]})
    monkeypatch.setattr(mcp_server_resolve, "_gateway_bearer_source",
                        lambda *a, **k: (TokenRef(str(tmp_path), "acct", ""), None))
    entry = {"type": "http", "url": "https://attacker.example.com/mcp"}
    with caplog.at_level("WARNING"):
        cred, reason = _call(entry, m)
    assert cred is None and "Admin > Security" in reason
    assert entry["url"] == "https://attacker.example.com/mcp" and "headers" not in entry


def test_a_dead_account_leaves_the_mcp_out_with_the_reconnect_wording(monkeypatch):
    key = _provider()
    bearer_allowlist.add_allowed(key, "mcp.example.com", "test")
    m = _manifest("vendor", oauth={"provider_id": key, "bearer_required": True,
                                   "proposed_hosts": ["mcp.example.com"]})
    monkeypatch.setattr(mcp_server_resolve, "_gateway_bearer_source",
                        lambda *a, **k: (None, "Vendor: the vendor ended the account's grant; reconnect it"))
    entry = {"type": "http", "url": "https://mcp.example.com/mcp"}
    cred, reason = _call(entry, m)
    assert cred is None and "reconnect" in reason


def test_a_sidecar_is_judged_under_localhost_and_marked_proxy_local(monkeypatch, tmp_path):
    key = _provider()
    bearer_allowlist.add_allowed(key, "localhost", "test")
    m = _manifest("github-mcp", runtime="docker", url_template="http://localhost:8935/mcp",
                  oauth={"provider_id": key, "bearer_required": True, "proposed_hosts": ["localhost"]})
    monkeypatch.setattr(mcp_server_resolve, "_gateway_bearer_source",
                        lambda *a, **k: (TokenRef(str(tmp_path), "acct", ""), None))
    entry = {"type": "http", "url": "http://localhost:8935/mcp"}
    cred, reason = _call(entry, m)
    assert reason is None and cred.proxy_local is True
    assert cred.upstream == "http://localhost:8935" and cred.path == "/mcp"
    assert entry["url"].endswith("/v1/mcp-gateway/github-mcp/mcp")


def test_a_hosted_mcp_naming_a_loopback_host_is_refused(monkeypatch, tmp_path):
    key = _provider()
    bearer_allowlist.add_allowed(key, "localhost", "test")
    m = _manifest("sneaky", url_template="http://localhost:5432/mcp",
                  oauth={"provider_id": key, "bearer_required": True, "proposed_hosts": ["localhost"]})
    monkeypatch.setattr(mcp_server_resolve, "_gateway_bearer_source",
                        lambda *a, **k: (TokenRef(str(tmp_path), "acct", ""), None))
    entry = {"type": "http", "url": "http://localhost:5432/mcp"}
    cred, reason = _call(entry, m)
    assert cred is None and "loopback" in reason


def test_the_bearer_source_resolves_user_and_service_scope(monkeypatch, tmp_path):
    """User scope reads the person's bound account; agent scope the service
    binding's owner; a dead file is refused with the reconnect wording."""
    from services.oauth import credential_resolver, oauth_account_store
    from storage import database as _db
    key = _provider()
    m = _manifest("vendor", oauth={"provider_id": key, "bearer_required": True,
                                   "proposed_hosts": ["mcp.example.com"]})
    picks = {}

    def _pick(mcp, agent, *, user_sub="", only_owner=None):
        picks["user_sub"] = user_sub
        return credential_resolver.AccountRef("acct", user_sub or "owner-sub")
    monkeypatch.setattr(credential_resolver, "pick_account", _pick)
    monkeypatch.setattr(_db, "get_username_by_sub", lambda sub: "alice" if sub else "")
    monkeypatch.setattr(oauth_account_store, "get_token_dir",
                        lambda username, *, provider_id: tmp_path)
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at", "expires_at": "",
                                                    "extra": {"preferred_bearer": "bot"}}))
    ref, reason = mcp_server_resolve._gateway_bearer_source(m, "u-1", "agent", "user")
    assert reason is None and ref == TokenRef(str(tmp_path), "acct", "bot")
    assert picks["user_sub"] == "u-1"
    ref, reason = mcp_server_resolve._gateway_bearer_source(m, "u-1", "agent", "agent")
    assert reason is None and picks["user_sub"] == ""
    (tmp_path / "acct.json").write_text(json.dumps({"access_token": "at", "expires_at": "",
                                                    "extra": {"refresh_failed": "invalid_grant"}}))
    ref, reason = mcp_server_resolve._gateway_bearer_source(m, "u-1", "agent", "user")
    assert ref is None and "reconnect" in reason
    monkeypatch.setattr(credential_resolver, "pick_account", lambda *a, **k: None)
    ref, reason = mcp_server_resolve._gateway_bearer_source(m, "u-1", "agent", "user")
    assert ref is None and "no account" in reason


# ---------------------------------------------------------------------------
# The local copies after the builder: no secret, the route and the sentinel
# ---------------------------------------------------------------------------


def test_the_claude_sandbox_copy_is_per_session_and_keeps_the_sentinel(tmp_path, monkeypatch):
    from core.credentials import mcp_broker
    from core.sandbox.session_config_dir import prepare_mcp_config_for_sandbox
    cfg = {"mcpServers": {
        "vendor": {"type": "http", "url": f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/vendor/mcp",
                   "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
        "tool": {"command": "python3", "args": ["s.py"], "env": {}},
    }}
    src = tmp_path / "agent-abc123.json"
    src.write_text(json.dumps(cfg))
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    host_dir = tmp_path / "agents" / "agent-x" / "users" / "alice" / ".claude"
    host_dir.mkdir(parents=True)
    # a copy an earlier release left under the shared name, with a secret inline
    (host_dir / "agent-abc123.json").write_text('{"mcpServers": {"v": {"headers": {"Authorization": "Bearer xoxb-old"}}}}')
    bundles = {
        "vendor": mcp_broker.SecretBundle(gateway=GatewayCredential(
            upstream="https://mcp.example.com", path="/mcp", allowlist_key="k", value="xoxb-real")),
        "tool": mcp_broker.SecretBundle(env={"K": "v"}),
    }
    out = prepare_mcp_config_for_sandbox(
        src, host_dir, sandbox_config_dir="/users/alice/.claude",
        session_id="sess-1234567890ab", secret_bundles=bundles, agent_name="agent-x",
    )
    assert out == "/users/alice/.claude/agent-abc123-sess-1234567.json"
    assert not (host_dir / "agent-abc123.json").exists()
    written = json.loads((host_dir / "agent-abc123-sess-1234567.json").read_text())
    vendor = written["mcpServers"]["vendor"]
    assert vendor["headers"]["Authorization"] == SESSION_JWT_SENTINEL_BEARER
    assert "xoxb-real" not in (host_dir / "agent-abc123-sess-1234567.json").read_text()
    tool = written["mcpServers"]["tool"]
    from core.sandbox.session_config_dir import sandbox_python
    assert tool["command"] == sandbox_python()
    assert tool["args"][:3] == ["-I", "/users/alice/.claude/stdio_path_interceptor.py", "--"]
    assert mcp_broker.verify_token(tool["env"]["OTO_MCP_FETCH_TOKEN"]) == ("sess-1234567890ab", "tool")


def test_two_sessions_of_one_person_get_two_copies(tmp_path, monkeypatch):
    from core.sandbox.session_config_dir import prepare_mcp_config_for_sandbox
    src = tmp_path / "agent-abc123.json"
    src.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    host_dir = tmp_path / "agents" / "agent-x" / "users" / "alice" / ".claude"
    host_dir.mkdir(parents=True)
    a = prepare_mcp_config_for_sandbox(src, host_dir, sandbox_config_dir="/u/.claude", session_id="aaaa",
                                       agent_name="agent-x")
    b = prepare_mcp_config_for_sandbox(src, host_dir, sandbox_config_dir="/u/.claude", session_id="bbbb",
                                       agent_name="agent-x")
    assert a != b and (host_dir / "agent-abc123-aaaa.json").exists() and (host_dir / "agent-abc123-bbbb.json").exists()


def test_the_codex_toml_carries_the_route_and_the_sentinel_never_a_secret():
    servers = {
        "vendor": {"type": "streamable-http",
                   "url": f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/vendor/mcp",
                   "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
    }
    toml = mcp_registry._servers_to_toml(servers)
    assert "[mcp_servers.vendor.http_headers]" in toml
    assert f'"Authorization" = "{SESSION_JWT_SENTINEL_BEARER}"' in toml
    assert "/v1/mcp-gateway/vendor/mcp" in toml
    from core.sandbox.interceptor_wrap import wrap_toml_text
    wrapped = wrap_toml_text(
        '[mcp_servers.tool]\ncommand = "python3"\nargs = ["s.py"]\nenv = { "OTO_MCP_FETCH_TOKEN" = "t" }\n',
        interpreter="python3", interpreter_args=("-I",), interceptor_path="/w/.codex/stdio_path_interceptor.py",
    )
    assert 'args = ["-I", "/w/.codex/stdio_path_interceptor.py", "--", "python3", "s.py"]' in wrapped


def test_the_direct_layer_forwards_the_swapped_session_token(monkeypatch):
    """The Direct layer connects through the route like every client: the
    sentinel becomes the session JWT and no vendor header is ever set."""
    import asyncio

    from core.layers.direct.mcp import MCPServerConnection
    from core.session import session_state
    captured = {}

    class _Cm:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})

        async def __aenter__(self):
            return (object(), object(), lambda: None)

        async def __aexit__(self, *a):
            return False

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    session_state.mark_starting("dir-s1", 60)
    conn = MCPServerConnection(
        "vendor", {"type": "http", "url": f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/vendor/mcp",
                   "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER}},
        credential_env={}, session_id="dir-s1", sandbox_builder=SimpleNamespace(), agent_name="agent",
    )
    monkeypatch.setattr("core.layers.direct.mcp.streamablehttp_client", _Cm)
    monkeypatch.setattr("core.layers.direct.mcp.ClientSession", lambda *a, **k: _Sess())
    asyncio.run(conn._start_remote())
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("Bearer ") and SESSION_JWT_SENTINEL_BEARER not in auth
    # The session's own kind, which the gateway route takes; never the callbacks'.
    from auth.session_token import validate_callback_token, validate_session_token
    token = auth.removeprefix("Bearer ")
    assert validate_session_token(token)["sid"] == "dir-s1" and validate_callback_token(token) is None
    assert "/v1/mcp-gateway/vendor/mcp" in captured["url"]


def test_a_docker_tool_with_no_credential_becomes_the_gateway_route_of_its_door(monkeypatch):
    from auth.session_token import CALLBACK_JWT_SENTINEL_BEARER
    from core.config import deployment
    from core.credentials.mcp_gateway import CONTAINER
    monkeypatch.setattr(deployment, "is_proxy_local_mcp_host", lambda host, manifest: host == "localhost")
    m = _manifest("video-tools", transport="http", url_template="http://localhost:8933", runtime="docker",
                  cred_type="none")
    m.server.proxy_callbacks = True
    entry = {"type": "http", "url": "http://localhost:8933/mcp/",
             "headers": {"Authorization": CALLBACK_JWT_SENTINEL_BEARER}}
    cred, reason = _call(entry, m)
    assert reason is None
    assert entry["url"] == f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/video-tools/mcp/"
    # The callback token is the door's to add per request, never the config's.
    assert entry["headers"]["Authorization"] == SESSION_JWT_SENTINEL_BEARER
    assert cred.kind == CONTAINER and cred.callbacks is True and cred.proxy_local is True
    assert cred.upstream == "http://localhost:8933" and cred.path == "/mcp/" and cred.value == ""
    again = GatewayCredential.from_descriptor(cred.descriptor())
    assert (again.kind, again.callbacks) == (CONTAINER, True)


def test_a_vendor_http_tool_with_no_credential_is_still_untouched(monkeypatch):
    from core.config import deployment
    monkeypatch.setattr(deployment, "is_proxy_local_mcp_host", lambda host, manifest: False)
    m = _manifest("vendor-open", transport="http", url_template="https://mcp.example.com", cred_type="none")
    entry = {"type": "http", "url": "https://mcp.example.com/mcp/"}
    assert _call(entry, m) == (None, None)
    assert entry["url"] == "https://mcp.example.com/mcp/"


def test_codex_gives_a_docker_tools_entry_the_doors_two_budgets(monkeypatch):
    from services.mcp import mcp_session_config
    m = _manifest("video-tools", transport="http", url_template="http://localhost:8933", runtime="docker",
                  cred_type="none")
    from services.mcp import mcp_manifest_store
    monkeypatch.setattr(mcp_manifest_store, "get_manifest_by_config_key",
                        lambda key: m if key == "video-tools" else None)
    gateway_url = f"http://127.0.0.1:{config.PORT}/v1/mcp-gateway/video-tools/mcp/"
    toml = mcp_session_config._servers_to_toml({
        "video-tools": {"type": "streamable-http", "url": gateway_url},
        "vendor": {"type": "streamable-http", "url": "https://mcp.example.com/mcp/"},
    })
    door_part, vendor_part = toml.split("[mcp_servers.vendor]")
    assert "startup_timeout_sec = 30" in door_part and "tool_timeout_sec = 120" in door_part
    assert "startup_timeout_sec = 10" in vendor_part and "tool_timeout_sec" not in vendor_part
    # The wake's budget is the door's own, read where it is set.
    from services.containers import door
    monkeypatch.setattr(door, "WAKE_BUDGET_S", 45.0)
    toml = mcp_session_config._servers_to_toml({"video-tools": {"type": "streamable-http", "url": gateway_url}})
    assert "tool_timeout_sec = 105" in toml
