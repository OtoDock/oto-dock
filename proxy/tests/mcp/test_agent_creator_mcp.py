"""Permission-matrix + manifest sanity tests for agent-creator-mcp.

The tools are thin HTTP shims; the endpoints they call are covered in
``test_local_agent_install.py``. What's locked down here is the gate that
decides whether an agent sees the tools at all, and the manifest fields the
framework reads to place the MCP.
"""

from __future__ import annotations

import json
import os

import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "agent-creator-mcp"


def _load_server(env: dict[str, str]):
    saved = {k: os.environ.get(k) for k in (
        "OTO_AGENT_NAME", "OTO_PLATFORM_ROLE", "OTO_CAN_CREATE_AGENTS", "PROXY_URL", "PROXY_API_KEY",
    )}
    try:
        os.environ.pop("OTO_PLATFORM_ROLE", None)
        os.environ.pop("OTO_CAN_CREATE_AGENTS", None)
        os.environ.update(env)
        return load_mcp_server(_MCP_DIR)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestPermissionMatrix:
    def test_admin_gets_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant",
            "OTO_PLATFORM_ROLE": "admin", "OTO_CAN_CREATE_AGENTS": "true",
        })
        assert mod.ENABLED_TOOLS == {
            "list_building_blocks", "validate_agent_template", "create_agent",
        }

    def test_creator_gets_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant",
            "OTO_PLATFORM_ROLE": "creator", "OTO_CAN_CREATE_AGENTS": "true",
        })
        assert "create_agent" in mod.ENABLED_TOOLS

    def test_member_gets_zero_tools(self):
        """Members can't create agents — they see no tools at all, not a
        surface that 403s on every call."""
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant",
            "OTO_PLATFORM_ROLE": "member", "OTO_CAN_CREATE_AGENTS": "false",
        })
        assert mod.ENABLED_TOOLS == set()

    def test_service_session_gets_zero_tools(self):
        """Agent-scope service sessions (task / phone / trigger) carry no
        platform role."""
        mod = _load_server({"OTO_AGENT_NAME": "personal-assistant"})
        assert mod.ENABLED_TOOLS == set()


class TestManifestSanity:
    def test_manifest_required_fields(self):
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        assert manifest["name"] == "agent-creator-mcp"
        assert manifest["category"] == "core"
        assert manifest["server"]["runtime"] == "python"
        assert manifest["server"]["transport"] == "stdio"
        # User-driven sessions only — chat AND interactive terminal. The
        # terminal joined once its input became identity-gated
        # (InteractiveSession.may_drive): a shared PTY runs under the warmer's
        # platform role, which is exactly what this MCP's endpoint gates on.
        # "external" (2026-09): never on a phone caller's session either.
        assert set(manifest["exclude_from"]) == {"phone", "task", "meeting", "external"}

    def test_skill_is_on_demand(self):
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        skill = manifest["skills"][0]
        assert skill["id"] == "agent-creation"
        assert skill["loading"] == "on_demand"
        assert (_MCP_DIR / skill["file"]).is_file()

    def test_schema_handler_coherence(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "x", "OTO_PLATFORM_ROLE": "admin", "OTO_CAN_CREATE_AGENTS": "true",
        })
        assert set(mod._TOOL_SCHEMAS.keys()) == set(mod._TOOL_HANDLERS.keys())
        assert set(mod._TOOL_SCHEMAS.keys()) == mod._ALL_TOOLS


class TestRetries:
    """Validate and create retry only a connection that never opened: a
    timed-out install may still be running on the platform, and a 503 or a
    timeout is the folder read failing or running long, which a second
    attempt would only repeat. Other calls keep their one retry."""

    def _serve(self, monkeypatch, mod, *outcomes):
        import httpx
        calls: list[str] = []
        pending = list(outcomes)

        async def _request(self, method, url, **kw):
            calls.append(url)
            outcome = pending.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

        async def _no_wait(_s):
            return None

        monkeypatch.setattr(httpx.AsyncClient, "request", _request)
        monkeypatch.setattr(mod.asyncio, "sleep", _no_wait)
        return calls

    def _mod(self):
        return _load_server({
            "OTO_AGENT_NAME": "x", "OTO_PLATFORM_ROLE": "admin", "OTO_CAN_CREATE_AGENTS": "true",
            "PROXY_URL": "http://test", "PROXY_API_KEY": "k",
        })

    @pytest.mark.parametrize("exc", ["ReadTimeout", "WriteTimeout", "PoolTimeout",
                                     "RemoteProtocolError", "ReadError"])
    def test_an_install_without_an_answer_is_sent_once(self, monkeypatch, exc):
        import asyncio

        import httpx
        mod = self._mod()
        calls = self._serve(monkeypatch, mod, getattr(httpx, exc)("slow"), (200, {}))
        with pytest.raises(mod._ApiError) as e:
            asyncio.run(mod._tool_create_agent("/workspace/t", ""))
        assert len(calls) == 1 and calls[0].endswith("/v1/agents/install-from-local-template")
        text = str(e.value)
        assert "may still be running" in text and "`list_building_blocks`" in text
        assert "never install it again under a suggested slug" in text

    def test_an_install_that_never_left_is_sent_again(self, monkeypatch):
        import asyncio

        import httpx
        mod = self._mod()
        calls = self._serve(monkeypatch, mod, httpx.ConnectError("refused"),
                            (200, {"agent_slug": "demo", "dashboard_path": "/agents/demo"}))
        text = asyncio.run(mod._tool_create_agent("/workspace/t", ""))
        assert text.startswith("✅ Created agent `demo`") and len(calls) == 2
        calls = self._serve(monkeypatch, mod, httpx.ConnectTimeout("slow"),
                            httpx.ConnectTimeout("slow"))
        with pytest.raises(mod._ApiError):
            asyncio.run(mod._tool_create_agent("/workspace/t", ""))
        assert len(calls) == 2

    def test_an_install_refused_with_a_503_created_nothing(self, monkeypatch):
        import asyncio
        mod = self._mod()
        detail = "'laptop' did not list the template folder, retry in a moment"
        calls = self._serve(monkeypatch, mod, (503, {"detail": detail}), (200, {}))
        with pytest.raises(mod._ApiError) as e:
            asyncio.run(mod._tool_create_agent("/workspace/t", ""))
        assert len(calls) == 1
        assert detail in str(e.value) and "Nothing was created" in str(e.value)

    def test_a_validate_is_retried_only_when_it_never_left(self, monkeypatch):
        import asyncio

        import httpx
        mod = self._mod()
        for first in (httpx.ReadTimeout("slow"), (503, {"detail": "retry in a moment"})):
            calls = self._serve(monkeypatch, mod, first, (200, {"ok": True, "slug": "t"}))
            with pytest.raises(mod._ApiError):
                asyncio.run(mod._tool_validate_agent_template("/workspace/t"))
            assert len(calls) == 1
        calls = self._serve(monkeypatch, mod, httpx.ConnectError("refused"),
                            (200, {"ok": True, "slug": "t"}))
        assert asyncio.run(mod._tool_validate_agent_template("/workspace/t")).startswith(
            "✅ Template is valid")
        assert len(calls) == 2

    def test_other_calls_keep_their_retry(self, monkeypatch):
        import asyncio

        import httpx
        mod = self._mod()
        calls = self._serve(monkeypatch, mod, httpx.ReadTimeout("slow"), (200, {"mcps": []}))
        asyncio.run(mod._tool_list_building_blocks())
        assert len(calls) == 2
