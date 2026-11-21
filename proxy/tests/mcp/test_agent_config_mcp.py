"""Permission-matrix + manifest sanity tests for agent-config-mcp.

The MCP's tool implementations are thin HTTP shims around platform
endpoints; integration coverage lives in the broader installer + endpoint
tests. What we lock down here is:

- ``_resolve_tool_set()`` returns the right set for each session shape.
- The manifest fields match the framework's expectations (category=core,
  assignment_mode auto-resolved, meeting-excluded).
- Tool schemas + handler map are coherent (every schema has a handler and
  vice versa).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "agent-config-mcp"


def _load_server(env: dict[str, str]):
    """Reload the server.py module with a fresh env so the permission gate
    is re-evaluated. Stash + restore real env to keep test isolation; the
    session keys are cleared first, so a suite run from inside an agent
    session (whose own OTO_CAN_* flags are in the environment) sees only
    what the test sets."""
    saved = {k: os.environ.get(k) for k in (
        "OTO_AGENT_NAME", "OTO_ROLE", "OTO_SCOPE", "OTO_TASK_TYPE",
        "OTO_CAN_MANAGE_AGENT", "OTO_CAN_EDIT_AGENT",
        "OTO_CAN_WRITE_WORKSPACE", "OTO_CAN_CREATE_AGENTS",
        "AGENT_CONFIG_MCP_PROXY_URL", "PROXY_URL", "PROXY_API_KEY",
    )}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        return load_mcp_server(_MCP_DIR)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestSessionReload:
    """The two tools of the chat's own session post the reload route of the
    session's own agent and tell the agent to end its turn."""

    answer: dict = {}

    def _mod(self, role="viewer"):
        mod = _load_server({"OTO_AGENT_NAME": "pa", "OTO_ROLE": role, "OTO_SCOPE": "user"})
        calls: list = []

        async def _request(method, path, **kw):
            calls.append((method, path, kw.get("json"), kw.get("attempts")))
            return {"scheduled": True, **self.answer}

        mod._request = _request
        return mod, calls

    def test_reload_posts_the_reason_once(self):
        import asyncio
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["reload_session"]({"reason": "Notion was enabled"}))
        assert calls == [("POST", "/v1/agents/pa/reload-session",
                          {"reason": "Notion was enabled"}, 1)]
        assert out.endswith("End your turn now.")

    def test_switch_account_posts_the_choice_for_one_tool(self):
        import asyncio
        mod, calls = self._mod("manager")
        out = asyncio.run(mod._TOOL_HANDLERS["switch_account"](
            {"tool": "mail-x", "account": "home", "reason": "the person asked"}))
        assert calls == [("POST", "/v1/agents/pa/reload-session",
                          {"reason": "the person asked", "accounts": {"mail-x": "home"}}, 1)]
        assert "home" in out and out.endswith("End your turn now.")

    def test_the_switchs_answer_names_what_the_platform_recorded(self, monkeypatch):
        """The route answers the label it recorded, or none for the agent's
        default account: the word ``default`` may be an account's own label."""
        import asyncio
        mod, _calls = self._mod("manager")
        switch = mod._TOOL_HANDLERS["switch_account"]
        args = {"tool": "mail-x", "account": "default", "reason": "the person asked"}
        monkeypatch.setattr(self, "answer", {"accounts": {"mail-x": ""}}, raising=False)
        assert asyncio.run(switch(args)).startswith(
            "This chat uses the agent's default account for mail-x from the reload on.")
        monkeypatch.setattr(self, "answer", {"accounts": {"mail-x": "default"}}, raising=False)
        assert asyncio.run(switch(args)).startswith(
            "This chat uses default for mail-x from the reload on.")

    def test_a_refusal_is_the_platforms_sentence_alone(self):
        """The reload route refuses with a sentence written for the person:
        the tool answers it without the request's method, path and status."""
        import asyncio
        mod, _calls = self._mod("manager")
        sentence = ("This chat runs on Office-PC. A chat there uses the agent's "
                    "default account for Mail.")

        async def _refuse(method, path, **kw):
            err = mod._ApiError(f"{method} {path} → 409: {sentence}")
            err.status, err.detail = 409, sentence
            raise err

        mod._request = _refuse
        [out] = asyncio.run(mod.call_tool(
            "switch_account", {"tool": "mail-x", "account": "home", "reason": "asked"}))
        assert out.text == f"Not done. {sentence} Tell the person this in these words."
        [out] = asyncio.run(mod.call_tool("reload_session", {"reason": "a tool was enabled"}))
        assert out.text.startswith("Not done. This chat runs on Office-PC.")

        async def _down(method, path, **kw):
            raise mod._ApiError(f"{method} {path}: connection refused")

        mod._request = _down
        [out] = asyncio.run(mod.call_tool("reload_session", {"reason": "a tool was enabled"}))
        assert out.text.startswith("❌ Error: POST /v1/agents/pa/reload-session")

    def test_the_skill_stays_off_a_call(self):
        import json
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        [skill] = manifest["skills"]
        assert skill["default_exclude_from"] == ["phone", "task"]
        text = (_MCP_DIR / skill["file"]).read_text()
        assert "Background commands" in text and "Ask no question" in text
        assert "viewers see zero tools" not in manifest["description"]

    def test_the_switch_asks_where_the_reload_does_not(self):
        import json
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        tiers = {r["tool"]: r["tier"] for r in manifest["permissions"]["rules"]}
        assert tiers["reload_session"] == "standard" and tiers["switch_account"] == "sensitive"
        [skill] = manifest["skills"]
        assert skill["loading"] == "always" and (_MCP_DIR / skill["file"]).is_file()


class TestPermissionMatrix:
    def test_viewer_user_scope_has_only_complete_setup(self):
        # Viewers get exactly complete_setup: per-user onboarding
        # (user-setup.md) targets the default-attach audience, who join as
        # viewers. Everything else stays manager-tier.
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "viewer",
            "OTO_SCOPE": "user",
        })
        assert mod.ENABLED_TOOLS == {"complete_setup"} | mod._SESSION_TOOLS

    def test_manager_user_scope_has_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true",
            "OTO_SCOPE": "user",
        })
        assert "update_display_name" in mod.ENABLED_TOOLS
        assert "complete_setup" in mod.ENABLED_TOOLS
        assert "get_agent_config" in mod.ENABLED_TOOLS

    def test_admin_user_scope_has_all_tools(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "admin", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true",
            "OTO_SCOPE": "user",
        })
        assert "update_default_model" in mod.ENABLED_TOOLS

    def test_contributor_has_only_complete_setup(self):
        """A contributor writes the shared workspace and nothing under the
        agent's identity: the same surface as a viewer here, read off the
        proxy's answer (OTO_CAN_EDIT_AGENT), never a role word."""
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "contributor",
            "OTO_SCOPE": "user",
            "OTO_CAN_MANAGE_AGENT": "false",
            "OTO_CAN_EDIT_AGENT": "false",
            "OTO_CAN_WRITE_WORKSPACE": "true",
        })
        assert mod.ENABLED_TOOLS == {"complete_setup"} | mod._SESSION_TOOLS

    def test_viewer_agent_scope_has_only_complete_setup(self):
        """A Shared-only agent mounts agent-scope for HUMAN chats too, so the
        viewer clamp can't key on scope — a real viewer drives that session."""
        mod = _load_server({
            "OTO_AGENT_NAME": "shared-service",
            "OTO_ROLE": "viewer",
            "OTO_SCOPE": "agent",
        })
        assert mod.ENABLED_TOOLS == {"complete_setup"} | mod._SESSION_TOOLS

    def test_agent_scope_session_has_all_tools(self):
        """Agent-scope (task / phone / trigger / Shared-only agent) can
        self-modify. Role is empty in agent-scope service sessions."""
        mod = _load_server({
            "OTO_AGENT_NAME": "personal-assistant-lite",
            "OTO_ROLE": "",
            "OTO_SCOPE": "agent",
        })
        # 24 tools = 6 read + 18 write
        # Read: get_agent_config, list_available_models, list_context_files,
        #       get_memory_settings, list_knowledge_libraries, get_outside_people
        # Write: update_persona, update_display_name, update_description,
        #        update_color, update_default_model,
        #        update_execution_layers, update_default_layer, update_default_scope,
        #        update_default_execution_mode, set_visibility_mode,
        #        update_user_memory_enabled, update_agent_memory_enabled, complete_setup,
        #        share_knowledge_folder, attach_knowledge_library,
        #        detach_knowledge_library, set_department, set_outside_people
        #        (platform-role, CRITICAL tier — proxy enforces the role server-side)
        # update_persona is ADVERTISED here but refuses at call time on any
        # session without a user — see TestUpdatePersona.
        # and the two tools of the chat's own session (reload_session,
        # switch_account), open to every role.
        assert mod.ENABLED_TOOLS == mod._READ_TOOLS | mod._WRITE_TOOLS | mod._SESSION_TOOLS
        assert len(mod.ENABLED_TOOLS) == 28


class TestManifestSanity:
    def test_manifest_required_fields(self):
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        assert manifest["name"] == "agent-config-mcp"
        assert manifest["category"] == "core"
        assert manifest["server"]["runtime"] == "python"
        assert manifest["server"]["transport"] == "stdio"
        # Excluded from meetings (self-reconfiguration mid-discussion,
        # possibly on another participant's suggestion, and meetings
        # multiply tool-schema cost per turn) and from a phone caller's
        # session. A run loads it for the two session tools alone (the
        # server's tool set and the hook's limit agree).
        assert manifest["exclude_from"] == ["meeting", "external"]

    @pytest.mark.parametrize("role, scope", [("", "agent"), ("manager", "user"), ("viewer", "agent")])
    def test_a_run_is_handed_the_two_session_tools_alone(self, role, scope):
        """A task's or a worker's session, whatever the role behind it:
        reload_session and switch_account, nothing that configures the agent."""
        mod = _load_server({
            "OTO_AGENT_NAME": "pa", "OTO_ROLE": role, "OTO_SCOPE": scope, "OTO_TASK_TYPE": "scheduled",
            "OTO_CAN_MANAGE_AGENT": "true" if role == "manager" else "false",
            "OTO_CAN_EDIT_AGENT": "true" if role == "manager" else "false",
        })
        assert mod.ENABLED_TOOLS == {"reload_session", "switch_account"}

    def test_schema_handler_coherence(self):
        mod = _load_server({
            "OTO_AGENT_NAME": "x", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true",
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
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true",
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


class TestUpdatePersona:
    """The persona write goes through the proxy file API (role check + git
    commit + satellite fan-out) and is pinned to owner-tier sessions with a
    human present — the sandbox mount table's rule, restated where the tool
    can enforce it (the file API's service-principal path would otherwise
    skip the role check for a no-user phone session). "Human present" is a
    non-empty role and no task type: NOT scope, since a Shared-only agent
    mounts agent-scope for human chats too."""

    def _load(self, role="manager", scope="user", task_type=""):
        owner = role in ("manager", "admin")
        return _load_server({
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": role, "OTO_SCOPE": scope,
            "OTO_TASK_TYPE": task_type,
            "OTO_CAN_MANAGE_AGENT": "true" if owner else "false",
            "OTO_CAN_EDIT_AGENT": "true" if owner or role == "editor" else "false",
        })

    def _spy(self, mod):
        calls: list[tuple[str, str, dict]] = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        mod._request = fake_request
        return calls

    def test_manager_writes_persona_through_file_api(self):
        mod = self._load()
        calls = self._spy(mod)
        out = asyncio.run(mod._tool_update_persona("# Demo\n\nYou are calm."))
        assert calls == [(
            "PUT", "/v1/agents/demo/files/config/agent.md",
            {"content": "# Demo\n\nYou are calm.\n"},
        )]
        assert "✅" in out
        # The agent must be told the edit is not live in THIS session.
        assert "next new session" in out

    def test_no_user_session_refused(self):
        # Phone / service sessions carry OTO_SCOPE=agent with no role —
        # rewriting an agent's soul unattended is exactly what this blocks.
        mod = self._load(role="", scope="agent")
        calls = self._spy(mod)
        out = asyncio.run(mod._tool_update_persona("# Demo\n\nhijacked"))
        assert "❌" in out and not calls

    def test_owner_tier_agent_scope_chat_allowed(self):
        # A Shared-only agent mounts agent-scope for HUMAN chats too: an
        # admin/manager driving one is a user present. Keying on scope
        # refused every such chat (found live 2026-09-03).
        for role in ("admin", "manager"):
            mod = self._load(role=role, scope="agent")
            calls = self._spy(mod)
            out = asyncio.run(mod._tool_update_persona("# Demo\n\nYou are calm."))
            assert "✅" in out, role
            assert calls == [(
                "PUT", "/v1/agents/demo/files/config/agent.md",
                {"content": "# Demo\n\nYou are calm.\n"},
            )]

    def test_task_fire_refused_even_with_creator_role(self):
        # Task fires carry their creator's role (manager provenance) but
        # nobody is watching — OTO_TASK_TYPE marks them unattended.
        for task_type in ("scheduled", "one_time", "trigger"):
            mod = self._load(role="manager", scope="agent", task_type=task_type)
            calls = self._spy(mod)
            out = asyncio.run(mod._tool_update_persona("# Demo\n\nhijacked"))
            assert "❌" in out and not calls, task_type

    def test_editor_refused(self):
        mod = self._load(role="editor")
        calls = self._spy(mod)
        out = asyncio.run(mod._tool_update_persona("# Demo\n\nnope"))
        assert "❌" in out and not calls

    def test_empty_and_oversized_rejected_without_call(self):
        mod = self._load()
        calls = self._spy(mod)
        assert "❌" in asyncio.run(mod._tool_update_persona("   \n  "))
        assert "❌" in asyncio.run(
            mod._tool_update_persona("x" * (mod._PERSONA_MAX_BYTES + 1)))
        assert not calls


class TestListContextFiles:
    """Regression: /v1/agents/{slug}/files returns ``tree`` as a LIST of
    top-level nodes — the descent used to call ``.get()`` on the list and
    crash with AttributeError on every call. Node timestamps live in
    ``modified`` (ISO 8601), not ``mtime``."""

    def _load(self):
        return _load_server({
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true",
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
            return {"tree": [self._node("workspace", "dir", "workspace"),
                             self._node("config", "dir", "config", children=[])]}

        mod._request = fake_request
        out = asyncio.run(mod._tool_list_context_files())
        assert "not found" in out

    def test_a_session_that_cannot_see_config_is_told_so(self):
        """The files route hides ``config/`` from an editor or a viewer (and
        an agent-scope task whose context lacks it): the folder exists, the
        session cannot see it."""
        mod = self._load()

        async def fake_request(method, path, **kwargs):
            return {"tree": [self._node("workspace", "dir", "workspace")]}

        mod._request = fake_request
        out = asyncio.run(mod._tool_list_context_files())
        assert "cannot see the agent's `config/` folder" in out
        assert "not found" not in out


class TestEngineCatalog:
    """The engines come from the platform — ``GET /v1/execution-layers`` —
    never a literal (engine-contract phase 5): the two layer enums and the
    engine-naming descriptions are filled at tools/list, the validators refuse
    an id the catalog lacks, the interactive gate reads
    ``runtime.supports_interactive_pty``. The engines here are invented so a
    server that remembered the real three would fail. An unreachable catalog
    advertises no enum and lets the proxy's own validation answer."""

    CATALOG = {
        "acme-cli": {
            "display_name": "Acme Coder",
            "runtime": {"supports_interactive_pty": True},
            "models": [{"value": "acme-1", "label": "Acme 1", "tier": 2}],
            "auto_model": "acme-1", "auto_model_label": "Acme 1",
        },
        "zephyr-api": {
            "display_name": "Zephyr API",
            "runtime": {"supports_interactive_pty": False},
            "models": [{"value": "z-1", "label": "Z 1", "tier": 3}],
            "auto_model": "z-1", "auto_model_label": "Z 1",
        },
    }

    def _load(self, *, catalog_down=False, default_model="z-1"):
        mod = _load_server({
            "OTO_AGENT_NAME": "demo", "OTO_ROLE": "manager", "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true", "OTO_SCOPE": "user",
        })
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if path == "/v1/execution-layers":
                if catalog_down:
                    raise mod._ApiError("GET /v1/execution-layers: connection refused")
                return json.loads(json.dumps(self.CATALOG))
            if path.endswith("/info"):
                return {"execution_path": "acme-cli", "execution_paths": ["acme-cli"],
                        "default_model": default_model}
            return {}

        mod._request = fake_request
        return mod, calls

    @staticmethod
    def _tools(mod):
        return {t.name: t for t in asyncio.run(mod.list_tools())}

    def test_static_table_names_no_engine(self):
        mod, _ = self._load()
        blob = json.dumps(mod._TOOL_SCHEMAS)
        for literal in ("claude-code-cli", "codex-cli", "direct-llm"):
            assert literal not in blob, literal
        assert not hasattr(mod, "VALID_LAYERS")

    def test_tools_list_fills_the_enums_and_descriptions_from_the_catalog(self):
        mod, calls = self._load()
        tools = self._tools(mod)
        layers = tools["update_execution_layers"].inputSchema["properties"]["layers"]
        assert layers["items"]["enum"] == ["acme-cli", "zephyr-api"]
        assert tools["update_default_layer"].inputSchema["properties"]["layer"]["enum"] == ["acme-cli", "zephyr-api"]
        assert "acme-cli, zephyr-api" in tools["update_execution_layers"].description
        assert "acme-cli, zephyr-api" in tools["update_default_layer"].description
        mode_desc = tools["update_default_execution_mode"].description
        assert "acme-cli" in mode_desc and "zephyr-api" not in mode_desc  # only the TUI engine
        assert "{engines}" not in mode_desc and "{interactive}" not in mode_desc
        # Read once per process: a second tools/list does not refetch.
        self._tools(mod)
        assert sum(1 for c in calls if c[1] == "/v1/execution-layers") == 1

    def test_unreachable_catalog_advertises_no_enum_and_defers_to_the_proxy(self):
        mod, calls = self._load(catalog_down=True)
        tools = self._tools(mod)
        assert "enum" not in tools["update_execution_layers"].inputSchema["properties"]["layers"]["items"]
        assert "enum" not in tools["update_default_layer"].inputSchema["properties"]["layer"]
        assert "list_available_models" in tools["update_execution_layers"].description
        assert "{engines}" not in tools["update_default_layer"].description
        # The validators let the PATCH answer (the proxy fails closed on an unknown id).
        out = asyncio.run(mod._tool_update_execution_layers(["whatever"]))
        assert "✅" in out
        assert ("PATCH", "/v1/agents/demo", {"execution_paths": ["whatever"]}) in calls
        # A failed fetch is not cached: the next call tries again.
        assert sum(1 for c in calls if c[1] == "/v1/execution-layers") >= 2

    def test_validators_refuse_an_engine_the_catalog_lacks(self):
        mod, calls = self._load()
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli", "nope"]))
        assert "❌" in out and "['nope']" in out and "acme-cli" in out
        out = asyncio.run(mod._tool_update_default_layer("nope"))
        assert "❌" in out and "acme-cli" in out
        assert not any(c[0] == "PATCH" for c in calls)
        # A catalog engine passes the validator and reaches the PATCH.
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli", "zephyr-api"]))
        assert "✅" in out
        assert ("PATCH", "/v1/agents/demo", {"execution_paths": ["acme-cli", "zephyr-api"]}) in calls

    def test_interactive_gate_reads_supports_interactive_pty(self):
        # The default model runs on the engine WITHOUT a TUI → refused, naming the TUI engine.
        mod, calls = self._load(default_model="z-1")
        out = asyncio.run(mod._tool_update_default_execution_mode("interactive"))
        assert "❌" in out and "acme-cli" in out and "zephyr-api" in out
        assert not any(c[0] == "PATCH" for c in calls)
        # On the TUI engine → allowed.
        mod, calls = self._load(default_model="acme-1")
        out = asyncio.run(mod._tool_update_default_execution_mode("interactive"))
        assert "✅" in out
        assert ("PATCH", "/v1/agents/demo", {"default_execution_mode": "interactive"}) in calls

    def test_the_toggle_rides_the_patch_only_when_given(self):
        mod, calls = self._load()
        # Without the toggle the body is the allowlist alone (a tool that
        # names nothing about local models leaves the setting as it is).
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli", "zephyr-api"]))
        assert "✅" in out and "local" not in out.lower()
        assert ("PATCH", "/v1/agents/demo", {"execution_paths": ["acme-cli", "zephyr-api"]}) in calls
        # The catalog declares no local provider: the toggle is refused
        # before any PATCH.
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli"], ["acme-cli"]))
        assert "❌" in out and "cannot be limited to local models" in out
        # An empty toggle clears it and rides the body.
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli"], []))
        assert "✅" in out and "No engine is limited to local models" in out
        assert ("PATCH", "/v1/agents/demo",
                {"execution_paths": ["acme-cli"], "local_only_engines": []}) in calls

    def test_a_local_provider_fills_the_toggles_enum_and_text(self):
        mod, calls = self._load()
        catalog = json.loads(json.dumps(self.CATALOG))
        catalog["acme-cli"]["providers"] = [{"id": "acme", "kind": "vendor"}, {"id": "ollama", "kind": "local"}]
        real = mod._request

        async def fake_request(method, path, **kwargs):
            if path == "/v1/execution-layers":
                return catalog
            return await real(method, path, **kwargs)
        mod._request = fake_request
        tools = self._tools(mod)
        schema = tools["update_execution_layers"].inputSchema["properties"]["local_only_layers"]
        assert schema["items"]["enum"] == ["acme-cli"]
        assert "possible on acme-cli" in tools["update_execution_layers"].description
        assert "{local_engines}" not in tools["update_execution_layers"].description
        out = asyncio.run(mod._tool_update_execution_layers(["acme-cli"], ["acme-cli"]))
        assert "✅" in out and "Local models only on ['acme-cli']" in out
        assert ("PATCH", "/v1/agents/demo",
                {"execution_paths": ["acme-cli"], "local_only_engines": ["acme-cli"]}) in calls

    def test_without_a_local_provider_the_description_says_so(self):
        mod, _ = self._load()
        tools = self._tools(mod)
        desc = tools["update_execution_layers"].description
        assert "possible on no engine of this platform" in desc
        assert "resolve per person" in desc
        assert "resolve per person" in tools["list_available_models"].description
        assert "stays a preference" in tools["update_default_layer"].description

    def test_list_available_models_flags_what_the_person_can_run(self):
        mod, _ = self._load()
        real = mod._request

        async def fake_request(method, path, **kwargs):
            if path.endswith("/info"):
                return {"execution_path": "acme-cli", "execution_paths": ["acme-cli", "zephyr-api"],
                        "default_model": "z-1", "engine_for_me": "zephyr-api",
                        "engines_for_me": {"acme-cli": False, "zephyr-api": True},
                        "local_only_engines": ["zephyr-api"]}
            return await real(method, path, **kwargs)
        mod._request = fake_request
        out = asyncio.run(mod._tool_list_available_models())
        assert "## Acme Coder (`acme-cli`) — **the agent's default engine** — you cannot run it here" in out
        assert "## Zephyr API (`zephyr-api`) — **allowed on this agent** — your new chats take it — local models only" in out
        assert "Engines resolve per person" in out

    def test_list_available_models_names_what_auto_runs(self):
        mod, _ = self._load()
        out = asyncio.run(mod._tool_list_available_models())
        assert "## Acme Coder (`acme-cli`)" in out
        assert "Auto (no `default_model`) currently runs `acme-1` (Acme 1) on this engine." in out
        assert "Auto (no `default_model`) currently runs `z-1` (Z 1) on this engine." in out


class TestExternalIdentities:
    """The agent's own tools for the External identities card: a change sends
    only what it names, and the read shows every choice of the card."""

    PAGE = {
        "tools": [
            {"mcp": "crm", "label": "CRM", "access": "read_only", "functions": [], "changed": False,
             "known_functions": [{"name": "find", "reads_only": True}, {"name": "update", "reads_only": False}]},
            {"mcp": "mail", "label": "Mail", "access": "custom", "functions": ["list", "read"], "changed": True,
             "known_functions": None},
            {"mcp": "files", "label": "Files", "access": "", "functions": [], "changed": False,
             "known_functions": None},
        ],
        "files": "read_write", "web": True, "knowledge": ["faq", "projects/alpha"],
        "knowledge_choices": ["faq", "projects", "shared/brand"],
        "wrapup_enabled": True, "shared_notes": False, "wrapup_model": "",
        "apps": [{"id": "app-1", "slug": "booking", "title": "Booking", "methods": ["book"], "changed": False,
                  "problem": ""}],
        "app_choices": [{"id": "app-1", "slug": "booking", "title": "Booking", "methods": ["book"]},
                        {"id": "app-2", "slug": "board", "title": "Board", "methods": []}],
    }

    def _mod(self):
        mod = _load_server({"OTO_AGENT_NAME": "pa", "OTO_ROLE": "manager", "OTO_SCOPE": "user",
                            "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true"})
        calls: list = []

        async def _request(method, path, **kw):
            calls.append((method, path, kw.get("json")))
            return dict(self.PAGE)

        mod._request = _request
        return mod, calls

    def test_a_change_sends_only_what_it_names(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["set_outside_people"](
            {"wrapup": False, "tools": [{"mcp": "mail", "access": "custom", "functions": ["read", 3]}]}))
        assert calls == [("PUT", "/v1/agents/pa/outside",
                          {"wrapup_enabled": False,
                           "tools": [{"mcp": "mail", "access": "custom", "functions": ["read", "3"]}]})]
        assert out.startswith("✅")
        calls.clear()
        asyncio.run(mod._TOOL_HANDLERS["set_outside_people"](
            {"remove_tools": ["crm"], "files": "read_only", "web": False, "add_folders": ["projects/beta"],
             "remove_folders": ["faq"], "notes_for_team": True, "wrapup_model": "m-small"}))
        assert calls[0][2] == {"remove_tools": ["crm"], "files": "read_only", "web": False,
                               "add_knowledge": ["projects/beta"], "remove_knowledge": ["faq"],
                               "shared_notes": True, "wrapup_model": "m-small"}

    def test_a_change_that_names_nothing_sends_nothing(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["set_outside_people"]({}))
        assert calls == [] and out.startswith("❌")
        out = asyncio.run(mod._TOOL_HANDLERS["set_outside_people"]({"tools": [{"access": "read_only"}]}))
        assert calls == [] and out.startswith("❌")

    def test_the_read_shows_every_choice_of_the_card(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["get_outside_people"]({}))
        assert calls == [("GET", "/v1/agents/pa/outside", None)]
        assert "**crm** (CRM): read only: find" in out
        assert "**mail** (Mail): custom: list, read" in out and "maker changed" in out
        assert "**files** (Files): not added" in out
        assert "**This person's files**: read and write" in out
        assert "**Web search**: on" in out
        assert "**Remember each conversation**: on" in out and "notes for the team off" in out
        assert "**Knowledge folders**: faq, projects/alpha" in out
        assert "faq, projects, shared/brand, and any folder inside them" in out
        assert "`booking` (Booking): opens book" in out
        assert "Apps on offer: `board` (opens nothing to them yet)" in out

    def test_apps_are_named_by_their_short_name_and_sent_by_id(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["set_outside_people"]({"add_apps": ["board"], "remove_apps": ["booking"]}))
        assert out.startswith("✅")
        assert calls[-1] == ("PUT", "/v1/agents/pa/outside", {"add_apps": ["app-2"], "remove_apps": ["app-1"]})
        calls.clear()
        out = asyncio.run(mod._TOOL_HANDLERS["set_outside_people"]({"add_apps": ["nope"]}))
        assert out.startswith("❌") and "nope" in out and not any(c[0] == "PUT" for c in calls)

    def test_the_schema_names_the_cards_words(self):
        mod, _ = self._mod()
        spec = mod._TOOL_SCHEMAS["set_outside_people"]["inputSchema"]
        assert set(spec["properties"]) == {"tools", "remove_tools", "files", "web", "add_folders",
                                           "remove_folders", "wrapup", "notes_for_team", "wrapup_model",
                                           "add_apps", "remove_apps"}
        assert "required" not in spec


class TestToolLimitsTools:
    """The two limits tools: the read as the card shows it, the write as the
    route takes it, each column alone, a refusal read as written."""

    def _mod(self, answer=None):
        mod = _load_server({"OTO_AGENT_NAME": "pa", "OTO_ROLE": "manager", "OTO_SCOPE": "user",
                            "OTO_CAN_MANAGE_AGENT": "true", "OTO_CAN_EDIT_AGENT": "true"})
        calls: list = []
        page = answer or {"tools": [
            {"mcp": "mail", "label": "Mail", "known_functions": [{"name": "read", "reads_only": True},
                                                                   {"name": "send", "reads_only": False}],
             "outside": None, "runs": {"access": "read_only", "functions": [], "changed": True},
             "chats": {"access": "custom", "functions": ["read"], "changed": False}},
            {"mcp": "crm", "label": "CRM", "known_functions": None, "outside": {"access": "read_write", "functions": [], "changed": False},
             "runs": None, "chats": None},
        ]}

        async def _request(method, path, **kw):
            calls.append((method, path, kw.get("json")))
            return page
        mod._request = _request
        return mod, calls

    def test_the_tools_are_listed_for_a_manager_and_the_setter_is_critical(self):
        mod, _ = self._mod()
        assert {"get_tool_limits", "set_tool_limits"} <= mod.ENABLED_TOOLS
        import json
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        tiers = {r["tool"]: r["tier"] for r in manifest["permissions"]["rules"]}
        assert tiers["set_tool_limits"] == "critical"
        viewer = _load_server({"OTO_AGENT_NAME": "pa", "OTO_ROLE": "viewer", "OTO_SCOPE": "user"})
        assert "set_tool_limits" not in viewer.ENABLED_TOOLS

    def test_the_read_shows_each_column(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["get_tool_limits"]({}))
        assert calls == [("GET", "/v1/agents/pa/tool-limits", None)]
        assert "- **mail** (Mail): functions read (reads), send" in out
        assert "External identities: not added" in out
        assert "The agent's own runs: only the tools that read (the maker changed" in out
        assert "People's chats: only read" in out
        assert "- **crm** (CRM): functions not listed yet" in out
        assert "External identities: read and write" in out and "The agent's own runs: all tools" in out

    def test_the_write_takes_each_column_alone_and_a_removal(self):
        mod, calls = self._mod()
        out = asyncio.run(mod._TOOL_HANDLERS["set_tool_limits"](
            {"tools": [{"mcp": "mail", "runs": "read_only", "chats": ["read", "list"]}],
             "remove": [{"mcp": "crm", "column": "chats"}]}))
        assert calls == [("PUT", "/v1/agents/pa/tool-limits", {
            "tools": [{"mcp": "mail", "runs": {"access": "read_only", "functions": []},
                       "chats": {"access": "custom", "functions": ["read", "list"]}}],
            "remove": [{"mcp": "crm", "governs": "chats"}]})]
        assert out.startswith("✅ Saved.")
        for bad in ({}, {"tools": [{"mcp": "mail"}]}, {"tools": [{"mcp": "mail", "runs": "all"}]},
                    {"remove": [{"mcp": "mail", "column": "outside"}]}):
            calls.clear()
            assert asyncio.run(mod._TOOL_HANDLERS["set_tool_limits"](bad)).startswith("❌")
            assert calls == []

    def test_a_refusal_is_read_as_written(self):
        mod, _ = self._mod()

        async def refused(method, path, **kw):
            err = mod._ApiError("refused")
            err.status, err.detail = 403, "Only a manager, from Agent Settings, can widen what the agent may use."
            raise err
        mod._request = refused
        out = asyncio.run(mod.call_tool("set_tool_limits", {"remove": [{"mcp": "mail", "column": "runs"}]}))
        assert out[0].text == ("Not done. Only a manager, from Agent Settings, can widen what the agent may use. "
                               "Tell the person this in these words.")
