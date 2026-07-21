"""Tests for the mcps-mcp meta-MCP (mcps/custom/mcps-mcp/server.py).

Covers:
- The permission matrix at module load (viewer / manager / admin × user-scope;
  agent-scope; internal-agent guard).
- Tool result formatting and the underlying HTTP shape (mocked AsyncClient).
- Cache invalidation on mutating calls.

The MCP lives outside ``proxy/``'s import path. We load it via
``importlib.util`` from the canonical filesystem location and reset env vars
around each test (the permission matrix is resolved at module load).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys

import httpx


from tests._paths import CUSTOM_MCPS

MCP_DIR = CUSTOM_MCPS / "mcps-mcp"
MCP_FILE = MCP_DIR / "server.py"


def _load_server(env: dict[str, str]):
    """Load ``mcps-mcp/server.py`` fresh with the supplied env vars.

    Always returns a fresh module — the permission matrix is captured at
    import time, so each test needs its own load.
    """
    # Wipe stale OTO_* vars so a missing key in ``env`` doesn't bleed in
    # from a prior test.
    for key in list(os.environ):
        if key.startswith("OTO_") or key in {"PROXY_URL", "PROXY_API_KEY", "MCPS_MCP_PROXY_URL"}:
            os.environ.pop(key, None)
    os.environ.update(env)

    # Force a clean module slot every time.
    sys.modules.pop("mcps_mcp_server", None)
    spec = importlib.util.spec_from_file_location("mcps_mcp_server", MCP_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ───────────────────────────────────────────────────────────────────────────
# Permission matrix
# ───────────────────────────────────────────────────────────────────────────


class TestPermissionMatrix:
    def test_viewer_has_no_tools(self):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "viewer",
        })
        assert m.ENABLED_TOOLS == set()

    def test_manager_sees_full_tool_set(self):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        assert m.ENABLED_TOOLS == {
            "list_enabled_mcps",
            "list_available_mcps",
            "list_community_mcps",
            "request_mcp_install",
            "request_mcp_access",
            "disable_mcp_for_agent",
            "get_request_status",
            "cancel_my_request",
        }

    def test_admin_sees_same_set_as_manager(self):
        """Admin power operations (platform-wide install without an agent
        target; cross-agent enable) live in the dashboard, not mcps-mcp.
        Admin chat-level tool set is intentionally identical to manager
        — one coherent flow per the smart-routed request handler."""
        admin = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "admin",
        })
        manager = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        assert admin.ENABLED_TOOLS == manager.ENABLED_TOOLS
        # Explicit assertion the removed tools are gone.
        assert "install_mcp_admin" not in admin.ENABLED_TOOLS
        assert "enable_mcp_admin" not in admin.ENABLED_TOOLS

    def test_agent_scope_cannot_disable(self):
        """Foot-gun guard: scheduled tasks / phone / triggers must not be
        able to unilaterally turn off MCPs the agent might need on the
        next run. The change would be platform-wide, not session-local."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "agent",
            "OTO_ROLE": "",
        })
        assert "disable_mcp_for_agent" not in m.ENABLED_TOOLS

    def test_agent_scope_is_read_only(self):
        """Tasks / phone / triggers / Shared-only agents — no user in the
        session, so requests can't be created on anyone's behalf."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "agent",
            "OTO_ROLE": "",
        })
        assert m.ENABLED_TOOLS == {
            "list_enabled_mcps",
            "list_available_mcps",
            "list_community_mcps",
        }

    def test_list_tools_filters_to_enabled(self):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        tool_names = {t.name for t in asyncio.run(m.list_tools())}
        assert tool_names == m.ENABLED_TOOLS

    def test_request_tool_schemas_mark_reason_required(self):
        """Both request tools must declare reason as a required arg so MCP
        clients (Claude Code / Codex) surface a validation error to the LLM
        instead of silently dropping a contextless request on the admin."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        tools = {t.name: t for t in asyncio.run(m.list_tools())}
        for name in ("request_mcp_install", "request_mcp_access"):
            schema = tools[name].inputSchema
            assert "mcp_name" in schema["required"]
            assert "reason" in schema["required"]

    def test_call_tool_rejects_unknown_name(self):
        """Defense-in-depth: an MCP client invoking a tool that isn't in
        ENABLED_TOOLS gets a soft error, not a crash."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        out = asyncio.run(m.call_tool("ghost_tool", {"mcp_name": "x"}))
        assert "not available" in out[0].text


# ───────────────────────────────────────────────────────────────────────────
# HTTP-mocked tool behavior
# ───────────────────────────────────────────────────────────────────────────


def _mock_response(json_payload, status: int = 200) -> httpx.Response:
    req = httpx.Request("GET", "http://x")
    return httpx.Response(
        status_code=status,
        json=json_payload,
        request=req,
    )


class TestToolBehavior:
    def test_list_enabled_mcps_renders_table(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        payload = {"mcps": [
            {"name": "schedules-mcp", "category": "core", "description": "Tasks",
             "enabled": True},
            {"name": "nextcloud", "category": "community", "description": "Cloud",
             "enabled": False},
        ]}
        async def fake_request(self, method, url, **kw):
            return _mock_response(payload)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool("list_enabled_mcps", {}))
        text = out[0].text
        assert "schedules-mcp" in text
        assert "nextcloud" not in text  # disabled row filtered out
        assert "**pa**" in text

    def test_list_community_mcps_filter_and_status(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        payload = {"mcps": [
            {"name": "email-server", "category": "productivity",
             "description": "Email", "tags": ["email"],
             "installed": False, "enabled_for_agents": []},
            {"name": "prometheus", "category": "infrastructure",
             "description": "Metrics", "tags": ["monitoring"],
             "installed": True, "enabled_for_agents": []},
            {"name": "nextcloud", "category": "infrastructure",
             "description": "Cloud", "tags": ["files"],
             "installed": True, "enabled_for_agents": ["pa"]},
        ]}
        async def fake_request(self, method, url, **kw):
            return _mock_response(payload)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool("list_community_mcps", {"category": "infrastructure"}))
        text = out[0].text
        assert "prometheus" in text
        assert "nextcloud" in text
        assert "email-server" not in text  # filtered out by category
        assert "installed_not_enabled" in text  # prometheus row
        assert "enabled_for_agent" in text  # nextcloud row

    def test_list_community_mcps_search(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        payload = {"mcps": [
            {"name": "email-server", "description": "Email via IMAP/SMTP",
             "tags": [], "installed": False, "enabled_for_agents": []},
            {"name": "prometheus", "description": "Metrics", "tags": [],
             "installed": False, "enabled_for_agents": []},
        ]}
        async def fake_request(self, method, url, **kw):
            return _mock_response(payload)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        out = asyncio.run(m.call_tool("list_community_mcps", {"search": "imap"}))
        text = out[0].text
        assert "email-server" in text
        assert "prometheus" not in text

    def test_request_routes_to_admin_when_mcp_not_visible(self, monkeypatch):
        """MCP isn't in this agent's visible set → POST falls through to the
        admin request queue (not installed OR explicit-mode without instance)."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        # Prime the cache so we can confirm invalidation.
        m._cache_put("catalog:pa", {"mcps": []})
        seen: list[tuple[str, str, dict]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url, kw))
            if method == "GET" and "/v1/agents/pa/mcps" in url and "mcp-requests" not in url:
                # Visibility check: MCP isn't visible to this agent.
                return _mock_response({"mcps": [
                    {"name": "schedules-mcp", "enabled": True},
                ]})
            return _mock_response({"id": 42, "status": "pending"}, status=200)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "request_mcp_install",
            {"mcp_name": "email-server", "reason": "user wants to send invoices"},
        ))
        text = out[0].text
        assert "#42" in text
        assert "email-server" in text
        # 2 calls: GET visibility check, then POST request.
        post_calls = [c for c in seen if c[0] == "POST"]
        assert len(post_calls) == 1
        assert "/v1/agents/pa/mcp-requests" in post_calls[0][1]
        assert post_calls[0][2]["json"] == {
            "mcp_name": "email-server",
            "reason": "user wants to send invoices",
        }
        # Cache must have been wiped so the next list reflects the change.
        assert m._cache_get("catalog:pa") is None

    def test_request_self_serves_when_visible_not_enabled(self, monkeypatch):
        """The exact bug the user hit: auto-mode MCP installed + visible to
        the agent but not toggled on → mcps-mcp must PUT to enable directly,
        NOT bother the admin with a request."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str, dict]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url, kw))
            if method == "GET" and "/v1/agents/pa/mcps" in url and "mcp-requests" not in url:
                return _mock_response({"mcps": [
                    {"name": "schedules-mcp", "enabled": True},
                    {"name": "email-server", "enabled": False},
                ]})
            # PUT to enable
            return _mock_response({"mcps": ["schedules-mcp", "email-server"]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "request_mcp_access",
            {"mcp_name": "email-server", "reason": "user wants email"},
        ))
        text = out[0].text
        assert "enabled" in text.lower()
        assert "no admin approval needed" in text.lower()
        # PUT was called, no POST to mcp-requests.
        methods = [c[0] for c in seen]
        urls = [c[1] for c in seen]
        assert "PUT" in methods
        assert not any("mcp-requests" in u for u in urls)
        # PUT body included email-server.
        put_call = next(c for c in seen if c[0] == "PUT")
        assert "email-server" in put_call[2]["json"]["mcps"]

    def test_request_no_op_when_already_enabled(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url))
            return _mock_response({"mcps": [
                {"name": "email-server", "enabled": True},
            ]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "request_mcp_access",
            {"mcp_name": "email-server", "reason": "x"},
        ))
        text = out[0].text
        assert "already enabled" in text.lower()
        # No POST, no PUT.
        assert all(m == "GET" for m, _u in seen)

    def test_request_blocks_missing_mcp_name(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        out = asyncio.run(m.call_tool("request_mcp_install", {"reason": "x"}))
        assert "mcp_name is required" in out[0].text

    def test_request_blocks_missing_reason(self, monkeypatch):
        """Tool-layer required gate even though API accepts empty."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
        })
        out = asyncio.run(m.call_tool(
            "request_mcp_install", {"mcp_name": "email-server"},
        ))
        assert "reason is required" in out[0].text

    def test_disable_removes_mcp_from_enabled_list(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str, dict]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url, kw))
            if method == "GET" and "/v1/agents/pa/mcps" in url and "mcp-requests" not in url:
                return _mock_response({"mcps": [
                    {"name": "schedules-mcp", "enabled": True},
                    {"name": "email-server", "enabled": True},
                    {"name": "nextcloud", "enabled": False},
                ]})
            return _mock_response({"mcps": ["schedules-mcp"]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "disable_mcp_for_agent", {"mcp_name": "email-server"},
        ))
        text = out[0].text
        assert "disabled" in text.lower()
        # PUT body excludes email-server but keeps the other enabled MCPs.
        put_call = next(c for c in seen if c[0] == "PUT")
        assert "schedules-mcp" in put_call[2]["json"]["mcps"]
        assert "email-server" not in put_call[2]["json"]["mcps"]
        # Disabled-already row not pulled into the new set.
        assert "nextcloud" not in put_call[2]["json"]["mcps"]

    def test_disable_no_op_when_already_disabled(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url))
            return _mock_response({"mcps": [
                {"name": "email-server", "enabled": False},
            ]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "disable_mcp_for_agent", {"mcp_name": "email-server"},
        ))
        text = out[0].text
        assert "already disabled" in text.lower()
        # GET only, no PUT.
        assert all(meth == "GET" for meth, _u in seen)

    def test_disable_no_op_when_not_visible(self, monkeypatch):
        """MCP isn't in the agent's visible set — friendly message, no write."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url))
            return _mock_response({"mcps": [
                {"name": "schedules-mcp", "enabled": True},
            ]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "disable_mcp_for_agent", {"mcp_name": "email-server"},
        ))
        text = out[0].text
        assert "nothing to disable" in text.lower()
        assert all(meth == "GET" for meth, _u in seen)

    def test_cancel_request_calls_endpoint(self, monkeypatch):
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "manager",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str]] = []
        async def fake_request(self, method, url, **kw):
            seen.append((method, url))
            return _mock_response({"id": 7, "status": "cancelled"})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        out = asyncio.run(m.call_tool("cancel_my_request", {"request_id": 7}))
        assert "#7 cancelled" in out[0].text
        assert seen[-1][0] == "POST"
        assert "/v1/agents/pa/mcp-requests/7/cancel" in seen[-1][1]

    def test_admin_session_uses_smart_routed_request(self, monkeypatch):
        """Admin sessions go through the same smart-routed
        ``request_mcp_install`` path as managers — direct enable when
        visible, queue when not. Confirms the removal of the dedicated
        ``install_mcp_admin`` / ``enable_mcp_admin`` tools didn't leave
        admins without a working chat-level enable path."""
        m = _load_server({
            "OTO_AGENT_NAME": "pa",
            "OTO_SCOPE": "user",
            "OTO_ROLE": "admin",
            "PROXY_URL": "http://test",
            "PROXY_API_KEY": "k",
        })
        seen: list[tuple[str, str]] = []

        async def fake_request(self, method, url, **kw):
            seen.append((method, url))
            if method == "GET" and "/v1/agents/pa/mcps" in url and "mcp-requests" not in url:
                # MCP visible-not-enabled → direct enable.
                return _mock_response({"mcps": [
                    {"name": "email-server", "enabled": False},
                ]})
            return _mock_response({"mcps": ["email-server"]})
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

        out = asyncio.run(m.call_tool(
            "request_mcp_install",
            {"mcp_name": "email-server", "reason": "admin testing"},
        ))
        text = out[0].text
        assert "enabled" in text.lower()
        assert "no admin approval needed" in text.lower()
        # No POST to mcp-requests — admin went through direct-enable path.
        assert not any("mcp-requests" in u for _m, u in seen)


# ───────────────────────────────────────────────────────────────────────────
# Core-MCP auto-assignment filter (agent creation time)
# ───────────────────────────────────────────────────────────────────────────


class TestCoreMcpAutoAssignment:
    """Validates the agents.py core-MCP auto-assignment filter. We don't spin up
    the FastAPI router — we exercise the filter expression with stub manifests.
    ``exclude_from`` does NOT gate assignment (``phone`` / ``task`` are
    session-time hints); every core auto-assign MCP is assigned to every agent."""

    def _stub_manifest(self, *, name, category="core", mode="auto", exclude=()):
        class M:
            pass
        m = M()
        m.name = name
        m.category = category
        m.assignment_mode = mode
        m.exclude_from = list(exclude)
        return m

    def test_all_core_auto_mcps_assigned_regardless_of_exclude_from(self):
        manifests = {
            "schedules-mcp": self._stub_manifest(name="schedules-mcp"),
            "mcps-mcp": self._stub_manifest(name="mcps-mcp", exclude=["phone"]),
            "explicit-mcp": self._stub_manifest(name="explicit-mcp", mode="explicit"),
            "community-x": self._stub_manifest(name="community-x", category="community"),
        }
        core_mcps = [
            name for name, m in manifests.items()
            if m.category == "core"
            and m.assignment_mode != "explicit"
        ]
        assert set(core_mcps) == {"schedules-mcp", "mcps-mcp"}


# ───────────────────────────────────────────────────────────────────────────
# Manifest sanity
# ───────────────────────────────────────────────────────────────────────────


def test_manifest_declares_expected_shape():
    """Catch silly drift in the manifest (wrong category) early instead of at
    runtime."""
    import json
    manifest = json.loads((MCP_DIR / "manifest.json").read_text())
    assert manifest["name"] == "mcps-mcp"
    assert manifest["category"] == "core"
    assert manifest["server"]["transport"] == "stdio"
    assert manifest["exclude_from"] == []
