"""File-tools master-key → per-session-JWT.

Covers the sentinel-bearer mechanism that replaces the master ``PROXY_API_KEY``
for Docker MCPs that call back to the proxy (manifest ``server.proxy_callbacks``):

  * the swap helper (``auth.session_token.swap_session_jwt_bearer``);
  * build-time sentinel injection (``mcp_registry._inject_session_jwt_sentinel``);
  * the file-tools manifest opt-in parses;
  * the four per-layer swap sites — CLI, Codex, remote-JSON, remote-TOML
    (direct is covered by ``test_direct_mcp_*``; header-forward asserted here);
  * the master key is no longer a resolvable template token.

The security contract: the swapped JWT's ``sid`` equals the session_id the MCP
also sends in the request body, so ``verify_session_match`` passes — and a real
vendor bearer is NEVER clobbered.
"""

from types import SimpleNamespace
from unittest.mock import patch

from auth.session_token import (
    SESSION_JWT_PLACEHOLDER,
    SESSION_JWT_SENTINEL_BEARER,
    swap_session_jwt_bearer,
    validate_session_token,
)

SAT_PORT = 49152


# --------------------------------------------------------------------------
# The swap helper
# --------------------------------------------------------------------------

class TestSwapHelper:
    def test_sentinel_swapped_to_valid_session_jwt(self):
        out = swap_session_jwt_bearer(
            SESSION_JWT_SENTINEL_BEARER, "sess-1", "personal-assistant", "sub-9",
        )
        assert out.startswith("Bearer ")
        payload = validate_session_token(out.split(" ", 1)[1])
        # sid MUST match the session_id the MCP sends in the body → the proxy's
        # verify_session_match cross-check passes.
        assert payload["sid"] == "sess-1"
        assert payload["agent"] == "personal-assistant"
        assert payload["user_sub"] == "sub-9"

    def test_user_sub_optional(self):
        """CLI/Codex sites have no user_sub — the JWT is still valid and the sid
        check is all file-tools' callbacks rely on."""
        out = swap_session_jwt_bearer(SESSION_JWT_SENTINEL_BEARER, "s2", "agentA")
        payload = validate_session_token(out.split(" ", 1)[1])
        assert payload["sid"] == "s2" and payload["user_sub"] == ""

    def test_real_vendor_bearer_untouched(self):
        assert swap_session_jwt_bearer("Bearer ghp_real", "s", "a") is None

    def test_empty_header_untouched(self):
        assert swap_session_jwt_bearer("", "s", "a") is None


# --------------------------------------------------------------------------
# Build-time sentinel injection
# --------------------------------------------------------------------------

class TestBuildTimeInjection:
    def _manifest(self, proxy_callbacks):
        return SimpleNamespace(server=SimpleNamespace(proxy_callbacks=proxy_callbacks))

    def test_opted_in_http_entry_gets_sentinel(self):
        from services.mcp.mcp_registry import _inject_session_jwt_sentinel

        entry = {"type": "http", "url": "http://localhost:8932/mcp/"}
        out = _inject_session_jwt_sentinel(entry, self._manifest(True))
        assert out["headers"]["Authorization"] == SESSION_JWT_SENTINEL_BEARER

    def test_not_opted_in_no_sentinel(self):
        from services.mcp.mcp_registry import _inject_session_jwt_sentinel

        entry = {"type": "http", "url": "http://localhost:8932/mcp/"}
        out = _inject_session_jwt_sentinel(entry, self._manifest(False))
        assert "headers" not in out

    def test_existing_vendor_bearer_not_clobbered(self):
        from services.mcp.mcp_registry import _inject_session_jwt_sentinel

        entry = {
            "type": "http", "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer vendor_tok"},
        }
        out = _inject_session_jwt_sentinel(entry, self._manifest(True))
        assert out["headers"]["Authorization"] == "Bearer vendor_tok"

    def test_stdio_entry_no_url_skipped(self):
        from services.mcp.mcp_registry import _inject_session_jwt_sentinel

        entry = {"type": "stdio", "command": "python", "args": ["s.py"]}
        out = _inject_session_jwt_sentinel(entry, self._manifest(True))
        assert "headers" not in out


# --------------------------------------------------------------------------
# Manifest opt-in parses
# --------------------------------------------------------------------------

class TestManifestOptIn:
    def test_file_tools_manifest_declares_proxy_callbacks(self):
        import config
        from services.mcp.mcp_registry import _parse_manifest

        path = config.MCPS_DIR / "custom" / "file-tools-mcp" / "manifest.json"
        manifest = _parse_manifest(path)
        assert manifest is not None
        assert manifest.server.proxy_callbacks is True
        # The master key must be GONE from the manifest env.
        assert "PROXY_API_KEY" not in (manifest.env or {})

    def test_default_proxy_callbacks_false(self):
        from services.mcp.mcp_registry import ServerConfig

        assert ServerConfig(runtime="python", transport="stdio").proxy_callbacks is False


# --------------------------------------------------------------------------
# Codex TOML swap
# --------------------------------------------------------------------------

class TestCodexSwap:
    def test_sentinel_swapped_in_toml(self):
        from core.layers.codex.layer import _inject_session_jwt_toml

        toml = (
            '[mcp_servers.file-tools]\n'
            'url = "http://localhost:8932/mcp/"\n'
            '[mcp_servers.file-tools.http_headers]\n'
            f'"Authorization" = "{SESSION_JWT_SENTINEL_BEARER}"\n'
        )
        out = _inject_session_jwt_toml(toml, "sess-c", "agentC")
        assert SESSION_JWT_PLACEHOLDER not in out
        # Extract the swapped bearer and validate it.
        import re
        m = re.search(r'"Authorization" = "Bearer ([^"]+)"', out)
        assert m, out
        payload = validate_session_token(m.group(1))
        assert payload["sid"] == "sess-c" and payload["agent"] == "agentC"

    def test_real_bearer_untouched(self):
        from core.layers.codex.layer import _inject_session_jwt_toml

        toml = (
            '[mcp_servers.github-mcp.http_headers]\n'
            '"Authorization" = "Bearer ghp_real"\n'
        )
        out = _inject_session_jwt_toml(toml, "s", "a")
        assert out == toml


# --------------------------------------------------------------------------
# Codex TOML session_id injection
# --------------------------------------------------------------------------

class TestCodexSessionIdInjection:
    def test_plain_url_gets_query(self):
        from core.layers.codex.layer import _inject_session_id_toml

        toml = '[mcp_servers.file-tools]\nurl = "http://localhost:8932/mcp/"\n'
        out = _inject_session_id_toml(toml, "sess-1")
        assert 'url = "http://localhost:8932/mcp/?session_id=sess-1"' in out

    def test_existing_query_appends_with_ampersand(self):
        # The bug this closes: the old regex always appended "?", corrupting a
        # URL that already carried a query string into a double-"?" URL.
        from core.layers.codex.layer import _inject_session_id_toml

        toml = '[mcp_servers.vendor]\nurl = "https://mcp.example.com/mcp?api_key=k1"\n'
        out = _inject_session_id_toml(toml, "sess-2")
        assert 'url = "https://mcp.example.com/mcp?api_key=k1&session_id=sess-2"' in out
        assert out.count("?") == 1

    def test_stale_session_id_replaced_not_duplicated(self):
        from core.layers.codex.layer import _inject_session_id_toml

        toml = '[mcp_servers.file-tools]\nurl = "http://localhost:8932/mcp/?session_id=old"\n'
        out = _inject_session_id_toml(toml, "sess-3")
        assert "session_id=old" not in out
        assert out.count("session_id=") == 1
        assert "session_id=sess-3" in out

    def test_non_url_lines_untouched(self):
        from core.layers.codex.layer import _inject_session_id_toml

        toml = (
            '[mcp_servers.display]\n'
            'command = "python"\n'
            'args = ["display_mcp.py"]\n'
        )
        assert _inject_session_id_toml(toml, "s") == toml


# --------------------------------------------------------------------------
# CLI swap
# --------------------------------------------------------------------------

class TestCliSwap:
    def _session(self):
        from core.layers.cli.session import PersistentSession

        return PersistentSession("sess-cli", None, None, agent_name="agentCLI")

    def test_sentinel_swapped(self):
        sess = self._session()
        conf = {
            "type": "http", "url": "http://localhost:8932/mcp/",
            "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
        }
        assert sess._swap_session_jwt(conf) is True
        payload = validate_session_token(
            conf["headers"]["Authorization"].split(" ", 1)[1]
        )
        assert payload["sid"] == "sess-cli" and payload["agent"] == "agentCLI"

    def test_real_bearer_untouched(self):
        sess = self._session()
        conf = {"headers": {"Authorization": "Bearer ghp_real"}}
        assert sess._swap_session_jwt(conf) is False
        assert conf["headers"]["Authorization"] == "Bearer ghp_real"

    def test_no_headers_noop(self):
        sess = self._session()
        assert sess._swap_session_jwt({"type": "http", "url": "x"}) is False


# --------------------------------------------------------------------------
# Remote (satellite) swap — JSON + TOML
# --------------------------------------------------------------------------

class TestRemoteSwap:
    def test_json_sentinel_swapped_to_session_jwt(self):
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        cfg = {
            "mcpServers": {
                "file-tools": {
                    "type": "http", "url": "http://localhost:8932/mcp/",
                    "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
                }
            }
        }
        out = _rewrite_mcp_json_for_remote(
            cfg, SAT_PORT, session_id="s1", proxy_api_key="JWT.real_123",
        )
        assert out["mcpServers"]["file-tools"]["headers"]["Authorization"] == (
            "Bearer JWT.real_123"
        )

    def test_json_no_proxy_key_leaves_sentinel(self):
        """Defensive: without a session JWT the sentinel is left as-is (the MCP
        then reports 'not session-bound' rather than authenticating)."""
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        cfg = {
            "mcpServers": {
                "file-tools": {
                    "type": "http", "url": "http://localhost:8932/mcp/",
                    "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
                }
            }
        }
        out = _rewrite_mcp_json_for_remote(cfg, SAT_PORT, session_id="s1")
        assert out["mcpServers"]["file-tools"]["headers"]["Authorization"] == (
            SESSION_JWT_SENTINEL_BEARER
        )

    def test_json_vendor_bearer_untouched(self):
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote

        cfg = {
            "mcpServers": {
                "linear": {
                    "url": "https://mcp.linear.app/mcp",
                    "headers": {"Authorization": "Bearer vendor"},
                }
            }
        }
        out = _rewrite_mcp_json_for_remote(
            cfg, SAT_PORT, session_id="s1", proxy_api_key="JWT.real",
        )
        assert out["mcpServers"]["linear"]["headers"]["Authorization"] == "Bearer vendor"

    def test_toml_sentinel_swapped(self):
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote

        toml = (
            '[mcp_servers.file-tools]\n'
            'url = "http://localhost:8932/mcp"\n'
            '[mcp_servers.file-tools.http_headers]\n'
            f'"Authorization" = "{SESSION_JWT_SENTINEL_BEARER}"\n'
        )
        out = _rewrite_mcp_toml_for_remote(
            toml, SAT_PORT, session_id="s1", proxy_api_key="JWT.real_456",
        )
        assert '"Authorization" = "Bearer JWT.real_456"' in out
        assert SESSION_JWT_PLACEHOLDER not in out


# --------------------------------------------------------------------------
# Direct layer — the entry's Authorization header is forwarded
# --------------------------------------------------------------------------

class TestDirectA2:
    def test_streamable_http_forwards_swapped_jwt_header(self):
        """The direct layer must pass headers= to the SDK client (it used to
        drop them, 401ing vendor MCPs and breaking file-tools). The session-JWT
        sentinel is swapped for a real JWT before forwarding."""
        import asyncio
        from core.layers.direct.mcp import MCPServerConnection

        conn = MCPServerConnection(
            "file-tools",
            {
                "type": "streamable-http", "url": "http://localhost:8932/mcp/",
                "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
            },
            session_id="sess-d", agent_name="agentD",
        )

        captured = {}

        class _FakeCM:
            async def __aenter__(self):
                return (object(), object(), None)

            async def __aexit__(self, *a):
                return False

        def _fake_streamable(url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeCM()

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch("core.layers.direct.mcp.streamablehttp_client", _fake_streamable), \
             patch("core.layers.direct.mcp.ClientSession", lambda *a, **k: _FakeSession()):
            asyncio.run(conn._start_remote())

        assert "session_id=sess-d" in captured["url"]
        assert captured["headers"] is not None
        payload = validate_session_token(
            captured["headers"]["Authorization"].split(" ", 1)[1]
        )
        assert payload["sid"] == "sess-d" and payload["agent"] == "agentD"


# --------------------------------------------------------------------------
# The master key is no longer a resolvable template token
# --------------------------------------------------------------------------

class TestMasterKeyRetired:
    def test_platform_api_key_token_not_resolved(self):
        from services.mcp.mcp_registry import _resolve_template

        manifest = SimpleNamespace(
            name="x",
            mcp_dir="/tmp/x",
            server=SimpleNamespace(port=8932, proxy_callbacks=True),
        )
        # The retired token is left literal (loud), never the master key.
        out = _resolve_template("${platform.api_key}", manifest)
        assert out == "${platform.api_key}"
