"""Permission-matrix + manifest sanity tests for agent-creator-mcp.

The tools are thin HTTP shims; the endpoints they call are covered in
``test_local_agent_install.py``. What's locked down here is the gate that
decides whether an agent sees the tools at all, and the manifest fields the
framework reads to place the MCP.
"""

from __future__ import annotations

import json
import os

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "agent-creator-mcp"


def _load_server(env: dict[str, str]):
    saved = {k: os.environ.get(k) for k in (
        "OTO_AGENT_NAME", "OTO_PLATFORM_ROLE", "PROXY_URL", "PROXY_API_KEY",
    )}
    try:
        os.environ.pop("OTO_PLATFORM_ROLE", None)
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
            "OTO_PLATFORM_ROLE": "admin",
        })
        assert mod.ENABLED_TOOLS == {
            "list_building_blocks", "validate_agent_template", "create_agent",
        }

    def test_creator_gets_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant",
            "OTO_PLATFORM_ROLE": "creator",
        })
        assert "create_agent" in mod.ENABLED_TOOLS

    def test_member_gets_zero_tools(self):
        """Members can't create agents — they see no tools at all, not a
        surface that 403s on every call."""
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant",
            "OTO_PLATFORM_ROLE": "member",
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
        assert set(manifest["exclude_from"]) == {"phone", "task", "meeting"}

    def test_skill_is_on_demand(self):
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        skill = manifest["skills"][0]
        assert skill["id"] == "agent-creation"
        assert skill["loading"] == "on_demand"
        assert (_MCP_DIR / skill["file"]).is_file()

    def test_schema_handler_coherence(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "x", "OTO_PLATFORM_ROLE": "admin",
        })
        assert set(mod._TOOL_SCHEMAS.keys()) == set(mod._TOOL_HANDLERS.keys())
        assert set(mod._TOOL_SCHEMAS.keys()) == mod._ALL_TOOLS
