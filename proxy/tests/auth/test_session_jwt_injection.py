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

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tomllib

from auth import tokens
from auth.session_token import (
    SESSION_JWT_PLACEHOLDER,
    SESSION_JWT_SENTINEL_BEARER,
    swap_session_jwt_bearer,
    validate_session_token,
)
from core.config import deployment
from services.mcp import mcp_registry
from services.scheduler import wake_session
from storage import remote_store
from tests.mcp import test_registry_characterization as _char

SAT_PORT = 49152


# --------------------------------------------------------------------------
# The swap helper
# --------------------------------------------------------------------------

class TestSwapHelper:
    def test_the_session_sentinel_swaps_to_the_sessions_own_token(self):
        """A gateway entry's sentinel: the session's own token, which the
        gateway route takes; never the callback kind."""
        from auth.session_token import validate_callback_token
        out = swap_session_jwt_bearer(SESSION_JWT_SENTINEL_BEARER, "sess-0", "personal-assistant", "sub-9")
        token = out.split(" ", 1)[1]
        assert validate_callback_token(token) is None
        payload = validate_session_token(token)
        assert payload["sid"] == "sess-0" and payload["agent"] == "personal-assistant"
        assert payload["user_sub"] == "sub-9"

    def test_the_callback_sentinel_swaps_to_a_callback_token(self):
        """The Docker tools' own kind: good for the five file callbacks, never
        a session token (a session token reaches nothing on the public
        listener, where the container arrives)."""
        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            validate_callback_token,
        )
        out = swap_session_jwt_bearer(
            CALLBACK_JWT_SENTINEL_BEARER, "sess-1", "personal-assistant", "sub-9",
        )
        assert out.startswith("Bearer ")
        token = out.split(" ", 1)[1]
        assert validate_session_token(token) is None
        payload = validate_callback_token(token)
        # sid MUST match the session_id the MCP sends in the body → the proxy's
        # verify_session_match cross-check passes.
        assert payload["sid"] == "sess-1"
        assert payload["agent"] == "personal-assistant"
        assert payload["user_sub"] == "sub-9"

    def test_user_sub_optional(self):
        """CLI/Codex sites have no user_sub — the token is still valid and the sid
        check is all file-tools' callbacks rely on."""
        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            validate_callback_token,
        )
        out = swap_session_jwt_bearer(CALLBACK_JWT_SENTINEL_BEARER, "s2", "agentA")
        payload = validate_callback_token(out.split(" ", 1)[1])
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
        from auth.session_token import CALLBACK_JWT_SENTINEL_BEARER
        from services.mcp.mcp_server_resolve import _inject_session_jwt_sentinel
        entry = {"type": "http", "url": "http://localhost:8932/mcp/"}
        out = _inject_session_jwt_sentinel(entry, self._manifest(True))
        assert out["headers"]["Authorization"] == CALLBACK_JWT_SENTINEL_BEARER

    def test_not_opted_in_no_sentinel(self):
        from services.mcp.mcp_server_resolve import _inject_session_jwt_sentinel

        entry = {"type": "http", "url": "http://localhost:8932/mcp/"}
        out = _inject_session_jwt_sentinel(entry, self._manifest(False))
        assert "headers" not in out

    def test_existing_vendor_bearer_not_clobbered(self):
        from services.mcp.mcp_server_resolve import _inject_session_jwt_sentinel

        entry = {
            "type": "http", "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer vendor_tok"},
        }
        out = _inject_session_jwt_sentinel(entry, self._manifest(True))
        assert out["headers"]["Authorization"] == "Bearer vendor_tok"

    def test_stdio_entry_no_url_skipped(self):
        from services.mcp.mcp_server_resolve import _inject_session_jwt_sentinel

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
        from auth.session_token import (
            CALLBACK_JWT_PLACEHOLDER,
            CALLBACK_JWT_SENTINEL_BEARER,
        )
        from core.layers.codex.layer import _inject_session_jwt_toml
        toml = (
            '[mcp_servers.file-tools]\n'
            'url = "http://localhost:8932/mcp/"\n'
            '[mcp_servers.file-tools.http_headers]\n'
            f'"Authorization" = "{CALLBACK_JWT_SENTINEL_BEARER}"\n'
            '[mcp_servers.github]\n'
            'url = "http://localhost:8400/v1/mcp-gateway/github/mcp/"\n'
            '[mcp_servers.github.http_headers]\n'
            f'"Authorization" = "{SESSION_JWT_SENTINEL_BEARER}"\n'
        )
        out = _inject_session_jwt_toml(toml, "sess-c", "agentC")
        assert SESSION_JWT_PLACEHOLDER not in out and CALLBACK_JWT_PLACEHOLDER not in out
        # Each sentinel became its own kind: the callback kind for the tool,
        # the session's own token for the gateway entry.
        import re
        bearers = re.findall(r'"Authorization" = "Bearer ([^"]+)"', out)
        assert len(bearers) == 2, out
        from auth.session_token import validate_callback_token
        payload = validate_callback_token(bearers[0])
        assert payload["sid"] == "sess-c" and payload["agent"] == "agentC"
        assert validate_session_token(bearers[0]) is None
        session = validate_session_token(bearers[1])
        assert session["sid"] == "sess-c" and validate_callback_token(bearers[1]) is None

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
        from core.layers.cli.spawn import ClaudeSpawn

        return ClaudeSpawn("sess-cli", None, None, agent_name="agentCLI")

    def test_sentinel_swapped(self):
        sess = self._session()
        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            validate_callback_token,
        )
        conf = {
            "type": "http", "url": "http://localhost:8932/mcp/",
            "headers": {"Authorization": CALLBACK_JWT_SENTINEL_BEARER},
        }
        assert sess._swap_session_jwt(conf) is True
        payload = validate_callback_token(
            conf["headers"]["Authorization"].split(" ", 1)[1]
        )
        assert payload["sid"] == "sess-cli" and payload["agent"] == "agentCLI"
        gateway = {
            "type": "http", "url": "http://localhost:8400/v1/mcp-gateway/github/mcp/",
            "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
        }
        assert sess._swap_session_jwt(gateway) is True
        token = gateway["headers"]["Authorization"].split(" ", 1)[1]
        assert validate_session_token(token)["sid"] == "sess-cli" and validate_callback_token(token) is None

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

    def test_json_a_live_session_token_becomes_a_callback_token(self):
        """A paired machine's file-tools callbacks arrive on the public
        listener: the rewrite mints the callback kind from the session's own
        token (the same session, person and floor), never the session token."""
        import time

        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            create_session_token,
            validate_callback_token,
        )
        from core.remote.remote_execution import _rewrite_mcp_json_for_remote
        issued = int(time.time()) - 5
        real = create_session_token("s1", "personal-assistant", "sub-9", issued_at=issued)
        cfg = {"mcpServers": {"file-tools": {
            "type": "http", "url": "http://localhost:8932/mcp/",
            "headers": {"Authorization": CALLBACK_JWT_SENTINEL_BEARER},
        }, "github": {
            "type": "http", "url": "http://localhost:8400/v1/mcp-gateway/github/mcp/",
            "headers": {"Authorization": SESSION_JWT_SENTINEL_BEARER},
        }}}
        out = _rewrite_mcp_json_for_remote(cfg, SAT_PORT, session_id="s1", proxy_api_key=real)
        bearer = out["mcpServers"]["file-tools"]["headers"]["Authorization"]
        token = bearer.removeprefix("Bearer ")
        assert token != real and validate_session_token(token) is None
        claims = validate_callback_token(token)
        assert (claims["sid"], claims["agent"], claims["user_sub"], claims["iat"]) == (
            "s1", "personal-assistant", "sub-9", issued)
        # A gateway entry keeps the session's own token: the machine's
        # gateway table is keyed by it.
        assert out["mcpServers"]["github"]["headers"]["Authorization"] == f"Bearer {real}"

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

    def test_toml_both_sentinels_with_a_live_token(self):
        """A live session token: the gateway entry keeps it, the Docker
        tool's callback entry gets the callback kind minted from it. A fake
        key would pass unchanged through both branches."""
        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            create_session_token,
            validate_callback_token,
        )
        from core.remote.remote_execution import _rewrite_mcp_toml_for_remote
        real = create_session_token("s1", "personal-assistant", "sub-9")
        toml = (
            '[mcp_servers.github]\n'
            'url = "http://localhost:8400/v1/mcp-gateway/github/mcp/"\n'
            '[mcp_servers.github.http_headers]\n'
            f'"Authorization" = "{SESSION_JWT_SENTINEL_BEARER}"\n'
            '[mcp_servers.file-tools]\n'
            'url = "http://localhost:8932/mcp"\n'
            '[mcp_servers.file-tools.http_headers]\n'
            f'"Authorization" = "{CALLBACK_JWT_SENTINEL_BEARER}"\n'
        )
        out = _rewrite_mcp_toml_for_remote(toml, SAT_PORT, session_id="s1", proxy_api_key=real)
        import re
        bearers = re.findall(r'"Authorization" = "Bearer ([^"]+)"', out)
        assert len(bearers) == 2 and bearers[0] == real
        assert validate_session_token(bearers[1]) is None
        claims = validate_callback_token(bearers[1])
        assert claims["sid"] == "s1" and claims["user_sub"] == "sub-9"

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

        from auth.session_token import (
            CALLBACK_JWT_SENTINEL_BEARER,
            validate_callback_token,
        )
        from core.layers.direct.mcp import MCPServerConnection

        conn = MCPServerConnection(
            "file-tools",
            {
                "type": "streamable-http", "url": "http://localhost:8932/mcp/",
                "headers": {"Authorization": CALLBACK_JWT_SENTINEL_BEARER},
            },
            session_id="sess-d", agent_name="agentD",
        )

        captured = {}

        class _FakeCM:
            async def __aenter__(self):
                return (object(), object(), None)

            async def __aexit__(self, *a):
                return False

        def _fake_streamable(url, headers=None, **_factory):
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
        payload = validate_callback_token(
            captured["headers"]["Authorization"].split(" ", 1)[1]
        )
        assert payload["sid"] == "sess-d" and payload["agent"] == "agentD"

    def _start_with(self, auth: str, session_id: str) -> dict:
        import asyncio

        from core.layers.direct.mcp import MCPServerConnection

        conn = MCPServerConnection(
            "github-mcp",
            {"type": "http", "url": "http://github-mcp:8935/mcp",
             "headers": {"Authorization": auth}},
            session_id=session_id, agent_name="agentE",
        )
        captured = {}

        class _FakeCM:
            async def __aenter__(self):
                return (object(), object(), None)

            async def __aexit__(self, *a):
                return False

        def _fake_streamable(url, headers=None, **_factory):
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
        return captured["headers"] or {}

    def test_a_header_that_is_not_the_sentinel_is_forwarded_untouched(self):
        """The direct layer swaps the session-token sentinel alone; a
        credentialed HTTP MCP's entry names the gateway route with that
        sentinel (tests/mcp/test_gateway_entry.py), and no other header
        value is ever rewritten in-process."""
        assert self._start_with("Bearer gho_vendor", "appx-e1")["Authorization"] == "Bearer gho_vendor"


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


# --------------------------------------------------------------------------
# The sweep: a session's real MCP config through every swap site
# --------------------------------------------------------------------------

sweep_world = _char.world  # the fixture: the catalog and the rows behind it
_CHAR_AGENT = _char.AGENT

_SWEEP_SID = "sweep-session-1"


def _built_config(world, compose: bool, fmt: str, monkeypatch):
    """The real build's five values for the admin of the characterization
    world, as a chat or a wake gets them."""
    monkeypatch.setattr(deployment, "in_docker_compose", lambda: compose)
    return mcp_registry.build_session_mcp_config(
        _CHAR_AGENT, "user-admin", username="ada", user_role="admin",
        mcp_config_format=fmt, session_id=_SWEEP_SID,
    )


def _kinds_of_json(servers: dict) -> dict[str, str]:
    """The sentinel the build gave each server: ``session`` (a gateway entry),
    ``callback`` (a Docker tool that calls back)."""
    from auth.session_token import (
        CALLBACK_JWT_SENTINEL_BEARER,
        SESSION_JWT_SENTINEL_BEARER,
    )
    kinds = {}
    for name, entry in servers.items():
        auth = (entry.get("headers") or {}).get("Authorization")
        if auth == SESSION_JWT_SENTINEL_BEARER:
            kinds[name] = "session"
        elif auth == CALLBACK_JWT_SENTINEL_BEARER:
            kinds[name] = "callback"
        else:
            assert "OTO_" not in (auth or ""), f"an unknown sentinel on {name}: {auth}"
    return kinds


def _assert_bearer_of_kind(bearer: str, want: str, sid: str = _SWEEP_SID, agent: str = _CHAR_AGENT) -> None:
    """The token decoded under the kind its sentinel stands for, its session
    and agent checked, and it does NOT decode under the other kind (the two
    kinds' keys and audiences differ)."""
    assert bearer.startswith("Bearer "), bearer[:40]
    token = bearer.split(" ", 1)[1]
    assert "OTO_" not in token, "a sentinel left in a built config"
    expected, other = (tokens.SESSION, tokens.TOOL_CALLBACK) if want == "session" else (tokens.TOOL_CALLBACK, tokens.SESSION)
    claims = tokens.decode(token, expected, legacy=False)
    with pytest.raises(tokens.TokenInvalid):
        tokens.decode(token, other, legacy=False)
    assert claims["sid"] == sid and claims["agent"] == agent


def _assert_config_kinds(bearers: dict[str, str], kinds: dict[str, str]) -> None:
    # A Docker tool's entry is a gateway entry too: its callback token is the
    # door's to add per request, so no config carries the callback sentinel.
    assert set(kinds.values()) == {"session"}, f"every entry carries the session's own sentinel: {kinds}"
    assert set(bearers) >= set(kinds)
    for name, want in kinds.items():
        _assert_bearer_of_kind(bearers[name], want)


def _toml_bearers(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    return {name: (entry.get("http_headers") or {}).get("Authorization", "")
            for name, entry in (data.get("mcp_servers") or {}).items()
            if (entry.get("http_headers") or {}).get("Authorization")}


def _direct_headers(name: str, entry: dict) -> dict:
    """The headers the direct layer hands its transport for one server."""
    from core.layers.direct.mcp import MCPServerConnection

    captured = {}

    class _FakeCM:
        async def __aenter__(self):
            return (object(), object(), None)

        async def __aexit__(self, *a):
            return False

    def _transport(url, headers=None, **_factory):
        captured["headers"] = headers
        return _FakeCM()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    conn = MCPServerConnection(name, entry, session_id=_SWEEP_SID, agent_name=_CHAR_AGENT)
    with patch("core.layers.direct.mcp.streamablehttp_client", _transport), \
         patch("core.layers.direct.mcp.sse_client", _transport), \
         patch("core.layers.direct.mcp.ClientSession", lambda *a, **k: _FakeSession()):
        asyncio.run(conn._start_remote())
    return captured["headers"] or {}


def _wake_build(world, fmt: str):
    """The wake's own build of the chat's session, with its credential step
    on the TOML leg (``_wake_credential_env``)."""
    vis = wake_session._wake_visibility(_CHAR_AGENT, "ada", "admin", "user-admin")
    where = remote_store.placement_of("local", "user-admin", _CHAR_AGENT)
    browser = remote_store.get_target_browser_settings(where)
    built = wake_session._wake_mcp_build(
        _CHAR_AGENT, "user-admin", username="ada", role="admin", vis=vis,
        chat_id="sweep-chat", where=where, browser=browser, mcp_format=fmt,
        session_id=_SWEEP_SID, targets=[],
    )
    path, flat_env, excluded, bundles, bash_keys = built
    if fmt == "toml":
        path, _env, _mv, _floor = wake_session._wake_credential_env(
            _CHAR_AGENT, _SWEEP_SID, user_sub="user-admin", role="admin", vis=vis, where=where,
            mcp_config=path, flat_env=flat_env, bash_env_keys=bash_keys, mcp_format=fmt,
        )
    return Path(path), flat_env, excluded, bundles, bash_keys


def test_the_sweep_names_every_site_that_swaps_a_sentinel():
    """A new swap site joins the sweep below: the sweep names its sites one by one."""
    proxy = Path(__file__).resolve().parents[2]
    names = re.compile(r"\b(?:SESSION|CALLBACK)_JWT_SENTINEL_BEARER\b|\bswap_session_jwt_bearer\b")
    found = {str(f.relative_to(proxy)) for f in proxy.rglob("*.py")
             if not {"tests", "venv"} & set(f.relative_to(proxy).parts) and names.search(f.read_text(encoding="utf-8"))}
    assert found == {"auth/session_token.py", "core/layers/cli/spawn.py", "core/layers/codex/mcp_toml.py",
                     "core/layers/direct/mcp.py", "core/remote/remote_mcp_rewrite.py",
                     "services/mcp/mcp_server_resolve.py"}


def test_the_door_adds_the_callback_kind_for_a_tool_that_calls_back():
    """A Docker tool that calls the proxy back gets a callback token its door
    mints per request from the request's own session token, decoded as the
    callback kind; one that does not call back gets none, nor a request
    whose token is another session's."""
    from auth.session_token import create_session_token
    from core.credentials.mcp_gateway import CONTAINER, GatewayCredential
    from services.containers import door
    cred = GatewayCredential(upstream="http://localhost:8932", path="/mcp/", allowlist_key="", proxy_local=True,
                             manifest="file-tools", kind=CONTAINER, callbacks=True)
    inbound = [("Authorization", f"Bearer {create_session_token(_SWEEP_SID, _CHAR_AGENT, 'user-admin')}")]
    headers = door._callback_headers(cred, _SWEEP_SID, inbound)
    _assert_bearer_of_kind(headers["Authorization"], "callback")
    assert door._callback_headers(cred, "another-session", inbound) == {}
    cred.callbacks = False
    assert door._callback_headers(cred, _SWEEP_SID, inbound) == {}


class TestSessionConfigSweep:
    """Every gateway entry of a session's real MCP config, a Docker tool's
    included, carries the session's own token, on every engine, on a paired
    machine and after a wake's rebuild, decoded by kind. The callback kind is
    the door's alone (``test_the_door_adds_the_callback_kind_...``)."""

    @pytest.fixture(params=[False, True], ids=["bare-metal", "compose"])
    def compose(self, request):
        return request.param

    def test_cli_engine(self, sweep_world, compose, monkeypatch):
        from core.layers.cli.spawn import ClaudeSpawn
        path = Path(_built_config(sweep_world, compose, "json", monkeypatch)[0])
        built = json.loads(path.read_text())["mcpServers"]
        kinds = _kinds_of_json(built)
        session = ClaudeSpawn(_SWEEP_SID, None, None, agent_name=_CHAR_AGENT)
        # The form a sandboxed session rewrites in place, and the per-session copy.
        session._inject_session_id_into_sse_inplace(path)
        servers = json.loads(path.read_text())["mcpServers"]
        _assert_config_kinds({n: e["headers"]["Authorization"] for n, e in servers.items()
                              if n in kinds}, kinds)

    def test_codex_engine(self, sweep_world, compose, monkeypatch):
        from core.layers.codex.mcp_toml import _inject_session_jwt_toml
        json_kinds = _kinds_of_json(json.loads(
            Path(_built_config(sweep_world, compose, "json", monkeypatch)[0]).read_text())["mcpServers"])
        path = Path(_built_config(sweep_world, compose, "toml", monkeypatch)[0])
        out = _inject_session_jwt_toml(path.read_text(), _SWEEP_SID, _CHAR_AGENT)
        _assert_config_kinds(_toml_bearers(out), json_kinds)

    def test_direct_engine(self, sweep_world, compose, monkeypatch):
        built = json.loads(Path(_built_config(sweep_world, compose, "json", monkeypatch)[0]).read_text())["mcpServers"]
        kinds = _kinds_of_json(built)
        bearers = {name: _direct_headers(name, built[name])["Authorization"] for name in kinds}
        _assert_config_kinds(bearers, kinds)

    @pytest.fixture
    def live_token(self):
        from auth.session_token import create_session_token
        return create_session_token(_SWEEP_SID, _CHAR_AGENT, "user-admin")

    def test_paired_machine_json(self, sweep_world, compose, monkeypatch, live_token):
        from core.remote.remote_mcp_rewrite import _rewrite_mcp_json_for_remote
        from core.remote.remote_start_payload import _gateway_facts
        path, _env, _excl, bundles, _keys = _built_config(sweep_world, compose, "json", monkeypatch)
        built = json.loads(Path(path).read_text())
        kinds = _kinds_of_json(built["mcpServers"])
        out = _rewrite_mcp_json_for_remote(built, SAT_PORT, session_id=_SWEEP_SID, proxy_api_key=live_token,
                                           gateway=_gateway_facts(bundles))
        _assert_config_kinds({n: e["headers"]["Authorization"] for n, e in out["mcpServers"].items()
                              if n in kinds}, kinds)

    def test_paired_machine_toml(self, sweep_world, compose, monkeypatch, live_token):
        from core.remote.remote_mcp_rewrite import _rewrite_mcp_toml_for_remote
        from core.remote.remote_start_payload import _gateway_facts
        json_kinds = _kinds_of_json(json.loads(
            Path(_built_config(sweep_world, compose, "json", monkeypatch)[0]).read_text())["mcpServers"])
        path, _env, _excl, bundles, _keys = _built_config(sweep_world, compose, "toml", monkeypatch)
        out = _rewrite_mcp_toml_for_remote(Path(path).read_text(), SAT_PORT, session_id=_SWEEP_SID,
                                           proxy_api_key=live_token, gateway=_gateway_facts(bundles))
        _assert_config_kinds(_toml_bearers(out), json_kinds)

    def test_a_wakes_rebuild_keeps_both_kinds(self, sweep_world, compose, monkeypatch):
        """A wake rebuilds the session's config from scratch: the two engines'
        own swaps give the same two kinds, the token's session and agent the
        same (minted again, its ``iat`` no earlier than the first)."""
        from core.layers.cli.spawn import ClaudeSpawn
        from core.layers.codex.mcp_toml import _inject_session_jwt_toml
        monkeypatch.setattr(deployment, "in_docker_compose", lambda: compose)
        first = Path(_built_config(sweep_world, compose, "json", monkeypatch)[0])
        built = json.loads(first.read_text())["mcpServers"]
        kinds = _kinds_of_json(built)
        session = ClaudeSpawn(_SWEEP_SID, None, None, agent_name=_CHAR_AGENT)
        session._inject_session_id_into_sse_inplace(first)
        first_bearers = {n: e["headers"]["Authorization"] for n, e in json.loads(first.read_text())["mcpServers"].items()
                         if n in kinds}
        # The rebuild, as ``_wake_built_config`` runs it.
        wake_json = _wake_build(sweep_world, "json")[0]
        session._inject_session_id_into_sse_inplace(wake_json)
        rebuilt = json.loads(wake_json.read_text())["mcpServers"]
        wake_bearers = {n: e["headers"]["Authorization"] for n, e in rebuilt.items() if n in kinds}
        _assert_config_kinds(wake_bearers, kinds)
        for name in kinds:
            before = tokens.decode(first_bearers[name].split(" ", 1)[1], tokens.SESSION if kinds[name] == "session"
                                   else tokens.TOOL_CALLBACK, legacy=False)
            after = tokens.decode(wake_bearers[name].split(" ", 1)[1], tokens.SESSION if kinds[name] == "session"
                                  else tokens.TOOL_CALLBACK, legacy=False)
            assert after["iat"] >= before["iat"]
        toml_out = _inject_session_jwt_toml(_wake_build(sweep_world, "toml")[0].read_text(), _SWEEP_SID, _CHAR_AGENT)
        _assert_config_kinds(_toml_bearers(toml_out), kinds)


def test_the_in_place_rewrite_follows_no_link(tmp_path, monkeypatch):
    """The session's tool config copy is read and rewritten beneath the
    agent's real folder with no link followed: a copy replaced by a link
    is left alone and its target never gets the session's values."""
    import config as app_config
    from core.layers.cli.spawn import ClaudeSpawn
    agents = tmp_path / "agents"
    claude = agents / "pa" / "users" / "alice" / ".claude"
    claude.mkdir(parents=True)
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    entry = {"mcpServers": {"m": {"type": "http", "url": "http://x/mcp"}}}
    (tmp_path / "elsewhere.json").write_text(json.dumps(entry))
    copy = claude / "pa-sess.json"
    copy.symlink_to(tmp_path / "elsewhere.json")
    ClaudeSpawn("sess-link-1", None, None, agent_name="pa")._inject_session_id_into_sse_inplace(copy)
    assert json.loads((tmp_path / "elsewhere.json").read_text()) == entry
    assert copy.is_symlink()   # nothing of the target was read into a copy
    copy.unlink()
    copy.write_text(json.dumps(entry))
    ClaudeSpawn("sess-link-1", None, None, agent_name="pa")._inject_session_id_into_sse_inplace(copy)
    assert "session_id=sess-link-1" in json.loads(copy.read_text())["mcpServers"]["m"]["url"]
