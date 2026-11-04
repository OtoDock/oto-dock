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
            "search_community_agents", "list_departments", "propose_install",
            "propose_department_install", "propose_move", "check_card",
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
        for schema in mod._TOOL_SCHEMAS.values():
            assert schema["inputSchema"]["additionalProperties"] is False

    def test_the_tiers_read_and_propose_below_create(self):
        """The reads and the card draws never ask twice: the card is the
        person's confirmation (services/cards)."""
        manifest = json.loads((_MCP_DIR / "manifest.json").read_text())
        tiers = {r["tool"]: r["tier"] for r in manifest["permissions"]["rules"]}
        assert tiers["search_community_agents"] == tiers["list_departments"] == tiers["check_card"] == "open"
        assert tiers["propose_install"] == tiers["propose_department_install"] == tiers["propose_move"] == "standard"
        assert "create_agent" not in tiers and manifest["permissions"]["default_tier"] == "sensitive"
        assert manifest["version"] == "1.3.2"


class TestCards:
    """The proposal tools draw one card each through the cards route with
    the session's id, the action and its parameters, and repeat the
    route's own sentence on a refusal; the search ranks templates and sets."""

    def _mod(self, monkeypatch):
        monkeypatch.setenv("OTO_SESSION_ID", "sid-creator")
        return _load_server({
            "OTO_AGENT_NAME": "x", "OTO_PLATFORM_ROLE": "admin", "OTO_CAN_CREATE_AGENTS": "true",
            "PROXY_URL": "http://test", "PROXY_API_KEY": "k",
        })

    def _serve(self, monkeypatch, mod, answer):
        import httpx
        calls: list[tuple[str, str, dict]] = []

        async def _request(self, method, url, **kw):
            calls.append((method, url, kw.get("json") or {}))
            status, body = answer(method, url)
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", _request)
        return calls

    def test_a_move_card_carries_the_session_the_action_and_the_parameters(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        calls = self._serve(monkeypatch, mod, lambda m, u: (200, {"status": "ok", "card_id": "card-1"}))
        out = asyncio.run(mod._TOOL_HANDLERS["propose_move"](
            {"agent": "alpha", "department_id": "dept-1", "level_id": "lvl-2", "note": "The Team level fits."}))
        assert "A card is in the chat." in out and "card-1" in out
        (call,) = calls
        assert call[0] == "POST" and call[1].endswith("/v1/cards")
        assert call[2] == {"session_id": "sid-creator", "note": "The Team level fits.",
                           "buttons": [{"id": "move", "kind": "do", "action": "move_agent",
                                        "params": {"agent": "alpha", "department_id": "dept-1", "level_id": "lvl-2"}}]}

    def test_an_install_card_names_the_template_and_the_placement(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        calls = self._serve(monkeypatch, mod, lambda m, u: (200, {"status": "ok", "card_id": "card-2"}))
        asyncio.run(mod._TOOL_HANDLERS["propose_install"](
            {"template_slug": "yacht-crew", "department_id": "dept-1", "level_id": "lvl-2"}))
        asyncio.run(mod._TOOL_HANDLERS["propose_department_install"](
            {"set_slug": "yacht-office", "department_name": "Lady G"}))
        install, dept = (c[2]["buttons"][0] for c in calls)
        assert install == {"id": "install", "kind": "do", "action": "install_agent",
                           "params": {"template_slug": "yacht-crew", "department_id": "dept-1", "level_id": "lvl-2"}}
        assert dept == {"id": "install", "kind": "do", "action": "install_department_set",
                        "params": {"set_slug": "yacht-office", "department_name": "Lady G"}}

    def test_a_refusal_repeats_the_routes_sentence_and_nothing_else(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        self._serve(monkeypatch, mod, lambda m, u: (409, {"detail": "Manager or admin access required for this agent."}))
        out = asyncio.run(mod._TOOL_HANDLERS["propose_move"]({"agent": "alpha", "department_id": "d", "level_id": "l"}))
        assert out == "No card was drawn: Manager or admin access required for this agent."
        self._serve(monkeypatch, mod, lambda m, u: (422, {"detail": [{"loc": ["body"], "msg": "x"}]}))
        out = asyncio.run(mod._TOOL_HANDLERS["propose_move"]({"agent": "alpha", "department_id": "d", "level_id": "l"}))
        assert out == f"No card was drawn: {mod.NO_ANSWER}"
        # A paired machine's older satellite refuses the draw at its tunnel.
        self._serve(monkeypatch, mod, lambda m, u: (403, {"error": "path-not-allowlisted"}))
        out = asyncio.run(mod._TOOL_HANDLERS["propose_move"]({"agent": "alpha", "department_id": "d", "level_id": "l"}))
        assert out == f"No card was drawn: {mod.OLDER_SATELLITE}"

    def test_the_card_state_in_words(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        states = iter([
            {"clicks": [], "outcome": None, "expired": None},
            {"clicks": [{"button": "no", "status": "said", "detail": "Not now, thanks.", "by": "Maria"}], "outcome": None, "expired": None},
            {"clicks": [{"button": "go", "status": "done", "by": "Nikos"}], "outcome": {"status": "done", "text": "Alpha is now in Eng as Team."}, "expired": None},
            {"clicks": [], "outcome": {"status": "running", "text": "Being installed.", "lines": ["Installed Head as head."]}, "expired": None},
            {"clicks": [], "outcome": None, "expired": "This card has expired. Ask again."},
        ])
        self._serve(monkeypatch, mod, lambda m, u: (200, next(states)))
        read = lambda: asyncio.run(mod._TOOL_HANDLERS["check_card"]({"card_id": "card-1"}))
        assert read() == "Nobody has pressed a button yet."
        assert read() == "Maria answered: Not now, thanks."
        assert read() == "Nikos pressed the button. Alpha is now in Eng as Team."
        # An older platform's state names nobody.
        assert read() == "Someone pressed the button. Being installed.\nIt is still running. Read the card again in a moment.\n- Installed Head as head."
        assert read() == "The card expired: This card has expired. Ask again."

    def test_check_card_quotes_the_id_so_it_reaches_no_other_route(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        calls = self._serve(monkeypatch, mod, lambda m, u: (200, {"clicks": [], "outcome": None, "expired": None}))
        asyncio.run(mod._TOOL_HANDLERS["check_card"]({"card_id": "../settings/catalog"}))
        (method, url, _body), = calls
        assert method == "GET" and url.endswith("/v1/cards/..%2Fsettings%2Fcatalog")

    def test_the_search_ranks_templates_and_sets(self, monkeypatch):
        import asyncio
        mod = self._mod(monkeypatch)
        agents = {"agents": [
            {"slug": "yacht-crew", "display_name": "Yacht Crew", "version": "0.2.2", "tags": ["yacht"],
             "description": "The crew files.", "installed_as": ["yacht-crew"]},
            {"slug": "personal-assistant", "display_name": "Personal Assistant", "version": "3.1.2", "tags": [],
             "description": "Your everyday assistant.", "installed_as": []},
        ]}
        sets = {"departments": [
            {"slug": "yacht-office", "name": "Yacht office", "version": "0.1.0", "tags": ["yacht"],
             "description": "The five agents of a yacht management office.", "problems": [], "missing_members": [],
             "members": [{"template": "yacht-crew", "display_name": "Yacht Crew", "level": "Team"}], "installed": []}]}
        self._serve(monkeypatch, mod, lambda m, u: (200, agents if u.endswith("/agents") else sets))
        out = asyncio.run(mod._TOOL_HANDLERS["search_community_agents"]({"query": "yacht"}))
        assert "department set `yacht-office`" in out and "template `yacht-crew`" in out
        assert "personal-assistant" not in out and "Part of the set: yacht-office." in out
        assert out.index("yacht-office") < out.index("yacht-crew")
        out = asyncio.run(mod._TOOL_HANDLERS["search_community_agents"]({"query": "zebra"}))
        assert out.startswith("Nothing in the community catalog matches.")


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

    def test_the_answer_names_the_task_pins_the_install_dropped(self, monkeypatch):
        import asyncio
        mod = self._mod()
        note = ("The task 'weekly-brief' runs on the agent's usual model and engine. "
                "The model m is not turned on for Claude Code CLI.")
        self._serve(monkeypatch, mod, (200, {"agent_slug": "demo", "dashboard_path": "/agents/demo",
                                             "task_pins_dropped": [note]}))
        text = asyncio.run(mod._tool_create_agent("/workspace/t", ""))
        assert "Task pins the new agent cannot run" in text and note in text
        self._serve(monkeypatch, mod, (200, {"agent_slug": "demo", "dashboard_path": "/agents/demo"}))
        assert "Task pins" not in asyncio.run(mod._tool_create_agent("/workspace/t", ""))

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
        calls = self._serve(monkeypatch, mod, httpx.ReadTimeout("slow"), (200, {"mcps": []}),
                            (200, {}))
        asyncio.run(mod._tool_list_building_blocks())
        assert len(calls) == 3 and calls[-1].endswith("/v1/execution-layers")


class TestEngines:
    """The engines and models a template task's pin may name, as this
    install has them: the coding engines first in their page order (an
    install picks one of those), a supporting engine marked as enabled by
    hand only, the model ids with their tiers and never the empty System
    Default; an engines read that fails leaves the rest of the listing."""

    _LAYERS = {
        "direct-llm": {"configured": True, "identity": {"role": "supporting", "sort_order": 30},
                       "models": [{"value": "", "label": "System Default"},
                                  {"value": "claude-haiku-5-5", "tier_label": "fast"}]},
        "codex-cli": {"configured": False, "identity": {"role": "coding", "sort_order": 20},
                      "models": [{"value": ""}, {"value": "gpt-6.1-sol", "tier_label": "strong"}]},
        "claude-code-cli": {"configured": True, "identity": {"role": "coding", "sort_order": 10},
                            "models": [{"value": ""}, {"value": "claude-fable-5-1", "tier_label": "frontier"},
                                       {"value": "claude-opus-5-5"}]},
    }

    def test_the_engines_section_lists_what_a_pin_may_name(self, monkeypatch):
        import asyncio
        retries = TestRetries()
        mod = retries._mod()
        calls = retries._serve(monkeypatch, mod, (200, {"mcps": [], "agent_slugs": ["a"]}),
                               (200, self._LAYERS))
        text = asyncio.run(mod._tool_list_building_blocks())
        assert calls[1].endswith("/v1/execution-layers")
        section = text.split("# Engines a task pin may name", 1)[1].split("# Slugs already taken")[0]
        rows = [line for line in section.splitlines() if line.startswith("| `")]
        assert rows == [
            "| `claude-code-cli` | yes | yes | `claude-fable-5-1` (frontier), `claude-opus-5-5` |",
            "| `codex-cli` | yes | no | `gpt-6.1-sol` (strong) |",
            "| `direct-llm` | no — enabled by hand only | yes | `claude-haiku-5-5` (fast) |",
        ]
        assert "this install's engines" in section and "dropped at install" in section
        assert "System Default" not in section

    def test_a_malformed_engine_entry_is_skipped_not_fatal(self, monkeypatch):
        import asyncio
        retries = TestRetries()
        mod = retries._mod()
        layers = {"odd": "not a dict", "weird": {"identity": "x", "models": []},
                  "late": {"identity": {"role": "coding", "sort_order": "ten"}, "models": [{"value": "m"}]}}
        retries._serve(monkeypatch, mod, (200, {"mcps": []}), (200, layers))
        text = asyncio.run(mod._tool_list_building_blocks())
        assert "| `late` | yes | no | `m` |" in text and "| `weird` |" in text and "`odd`" not in text

    def test_a_failed_engines_read_keeps_the_rest(self, monkeypatch):
        import asyncio
        retries = TestRetries()
        mod = retries._mod()
        retries._serve(monkeypatch, mod, (200, {"mcps": [{"name": "m", "installed": True}],
                                                "agent_slugs": ["a"]}),
                       (403, {"detail": "no"}))
        text = asyncio.run(mod._tool_list_building_blocks())
        assert "| `m` | yes |" in text and "# Slugs already taken" in text
        assert "The engines could not be read" in text and "without a pin" in text
