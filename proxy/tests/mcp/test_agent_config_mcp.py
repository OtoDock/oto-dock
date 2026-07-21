"""Permission-matrix + manifest sanity tests for agent-config-mcp.

The MCP's tool implementations are thin HTTP shims around platform
endpoints; integration coverage lives in the broader installer + endpoint
tests. What we lock down here is:

- ``_resolve_tool_set()`` returns the right set for each session shape.
- The manifest fields match the framework's expectations (category=core,
  assignment_mode auto-resolved, empty exclude_from).
- Tool schemas + handler map are coherent (every schema has a handler and
  vice versa).
"""

from __future__ import annotations

import asyncio
import json
import os



from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "agent-config-mcp"


def _load_server(env: dict[str, str]):
    """Reload the server.py module with a fresh env so the permission gate
    is re-evaluated. Stash + restore real env to keep test isolation."""
    saved = {k: os.environ.get(k) for k in (
        "OTO_AGENT_NAME", "OTO_ROLE", "OTO_SCOPE",
        "AGENT_CONFIG_MCP_PROXY_URL", "PROXY_URL", "PROXY_API_KEY",
    )}
    try:
        os.environ.update(env)
        return load_mcp_server(_MCP_DIR)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestPermissionMatrix:
    def test_viewer_user_scope_has_no_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "viewer",
            "OTO_SCOPE": "user",
        })
        assert mod.ENABLED_TOOLS == set()

    def test_manager_user_scope_has_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "manager",
            "OTO_SCOPE": "user",
        })
        assert "update_display_name" in mod.ENABLED_TOOLS
        assert "complete_setup" in mod.ENABLED_TOOLS
        assert "get_agent_config" in mod.ENABLED_TOOLS

    def test_admin_user_scope_has_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "admin",
            "OTO_SCOPE": "user",
        })
        assert "update_default_model" in mod.ENABLED_TOOLS

    def test_agent_scope_session_has_all_tools(self):
        """Agent-scope (task / phone / trigger / Shared-only agent) can
        self-modify. Role is empty in agent-scope sessions."""
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "",
            "OTO_SCOPE": "agent",
        })
        # 16 tools = 4 read + 12 write
        # Read: get_agent_config, list_available_models, list_context_files, get_memory_settings
        # Write: update_display_name, update_description, update_color, update_default_model,
        #        update_execution_layers, update_default_layer, update_default_scope,
        #        update_default_execution_mode, set_visibility_mode,
        #        update_user_memory_enabled, update_agent_memory_enabled, complete_setup
        assert len(mod.ENABLED_TOOLS) == len(mod._READ_TOOLS | mod._WRITE_TOOLS)
        assert len(mod.ENABLED_TOOLS) == 16


class TestManifestSanity:
    def test_manifest_required_fields(self):
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        assert manifest["name"] == "agent-config-mcp"
        assert manifest["category"] == "core"
        assert manifest["server"]["runtime"] == "python"
        assert manifest["server"]["transport"] == "stdio"
        assert manifest["exclude_from"] == []

    def test_schema_handler_coherence(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "x", "OTO_ROLE": "manager",
            "OTO_SCOPE": "user",
        })
        schemas = set(mod._TOOL_SCHEMAS.keys())
        handlers = set(mod._TOOL_HANDLERS.keys())
        assert schemas == handlers


class TestSetVisibilityMode:
    """The visibility-mode tool maps each of the four mode keys to the two
    underlying columns (collaborative × default_scope) in one PATCH. The map
    must stay in lock-step with ``proxy/core/session/visibility.py``."""

    def _load(self):
        return _load_server({
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": "manager",
            "OTO_SCOPE": "user",
        })

    def test_mode_map_matches_backend_columns(self):
        mod = self._load()
        assert mod._VISIBILITY_MODES == {
            "personal_shared": (True, "user"),
            "shared_personal": (True, "agent"),
            "personal_only": (False, "user"),
            "shared_only": (False, "agent"),
        }
        # The schema enum advertises exactly those four keys.
        enum = mod._TOOL_SCHEMAS["set_visibility_mode"]["inputSchema"]["properties"]["mode"]["enum"]
        assert set(enum) == set(mod._VISIBILITY_MODES)

    def test_each_mode_patches_both_columns(self):
        mod = self._load()
        calls: list[tuple[str, str, dict]] = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        mod._request = fake_request
        for mode, (collab, scope) in mod._VISIBILITY_MODES.items():
            calls.clear()
            out = asyncio.run(mod._tool_set_visibility_mode(mode))
            assert len(calls) == 1, f"{mode} should PATCH exactly once"
            method, path, body = calls[0]
            assert method == "PATCH"
            assert path == "/v1/agents/demo"
            assert body == {"collaborative": collab, "default_scope": scope}
            assert "✅" in out

    def test_mode_is_trimmed_and_case_insensitive(self):
        mod = self._load()
        calls: list[dict] = []

        async def fake_request(method, path, **kwargs):
            calls.append(kwargs.get("json"))
            return {}

        mod._request = fake_request
        out = asyncio.run(mod._tool_set_visibility_mode("  SHARED_ONLY  "))
        assert calls == [{"collaborative": False, "default_scope": "agent"}]
        assert "✅" in out

    def test_invalid_mode_rejected_without_patch(self):
        mod = self._load()
        called = False

        async def fake_request(*a, **k):
            nonlocal called
            called = True
            return {}

        mod._request = fake_request
        out = asyncio.run(mod._tool_set_visibility_mode("bogus"))
        assert "❌" in out
        assert called is False, "an invalid mode must not hit the API"


class TestListContextFiles:
    """Regression: /v1/agents/{slug}/files returns ``tree`` as a LIST of
    top-level nodes — the descent used to call ``.get()`` on the list and
    crash with AttributeError on every call. Node timestamps live in
    ``modified`` (ISO 8601), not ``mtime``."""

    def _load(self):
        return _load_server({
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": "manager",
            "OTO_SCOPE": "user",
        })

    @staticmethod
    def _node(name, type_, path, size=0, children=None):
        return {
            "name": name, "type": type_, "path": path, "size": size,
            "modified": "2026-07-04T10:00:00+00:00",
            "children": children if children is not None else [],
        }

    def _tree(self, context_children):
        # The real endpoint shape (files.py::_build_tree): top-level entries
        # as a list of {name, type, path, size, modified, children} nodes.
        return {"tree": [
            self._node("config", "dir", "config", children=[
                self._node("context", "dir", "config/context",
                           children=context_children),
                self._node("prompt.md", "file", "config/prompt.md", size=12),
            ]),
            self._node("workspace", "dir", "workspace"),
        ]}

    def test_lists_files_from_endpoint_shaped_tree(self):
        mod = self._load()

        async def fake_request(method, path, **kwargs):
            assert (method, path) == ("GET", "/v1/agents/demo/files")
            return self._tree([
                self._node("rules.md", "file", "config/context/rules.md", size=42),
                self._node("sub", "dir", "config/context/sub", children=[
                    self._node("notes.md", "file",
                               "config/context/sub/notes.md", size=7),
                ]),
            ])

        mod._request = fake_request
        out = asyncio.run(mod._tool_list_context_files())
        assert "| `rules.md` | 42 | 2026-07-04T10:00:00+00:00 |" in out
        assert "| `sub/notes.md` | 7 | 2026-07-04T10:00:00+00:00 |" in out
        assert "2 file(s), 49 bytes" in out

    def test_empty_context_dir_reports_empty(self):
        mod = self._load()

        async def fake_request(method, path, **kwargs):
            return self._tree([])

        mod._request = fake_request
        out = asyncio.run(mod._tool_list_context_files())
        assert "empty" in out
        assert "❌" not in out

    def test_missing_context_dir_reports_not_found(self):
        mod = self._load()

        async def fake_request(method, path, **kwargs):
            return {"tree": [self._node("workspace", "dir", "workspace")]}

        mod._request = fake_request
        out = asyncio.run(mod._tool_list_context_files())
        assert "not found" in out
