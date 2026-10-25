"""Tests for the execution layer migration — CommonEvent, translators, ABC.

Verifies that:
- CommonEvent schema works correctly
- CLI chunk → CommonEvent translation preserves all fields
- ExecutionLayer ABC enforces the contract
- SessionManager routes agents to correct layers
- set_session_mode / get_session_mode roundtrip
- config_builder produces valid AgentConfig
"""

from unittest.mock import MagicMock, patch

import pytest

from core.execution_layer import ResumeVerdict

# ---------------------------------------------------------------------------
# CommonEvent schema
# ---------------------------------------------------------------------------


class TestCommonEvent:
    def test_create_text_event(self):
        from core.events.common_events import TEXT, CommonEvent

        e = CommonEvent(type=TEXT, data={"content": "hello"})
        assert e.type == "text"
        assert e.data["content"] == "hello"
        assert isinstance(e.timestamp, float)

    def test_create_done_event(self):
        from core.events.common_events import DONE, CommonEvent

        e = CommonEvent(type=DONE)
        assert e.type == "done"
        assert e.data == {}

    def test_default_data_is_empty_dict(self):
        from core.events.common_events import CommonEvent

        e = CommonEvent(type="test")
        assert e.data == {}

    def test_default_data_not_shared(self):
        """Each CommonEvent should get its own dict, not a shared default."""
        from core.events.common_events import CommonEvent

        e1 = CommonEvent(type="a")
        e2 = CommonEvent(type="b")
        e1.data["x"] = 1
        assert "x" not in e2.data

    def test_all_type_constants_are_strings(self):
        from core.events import common_events as ce

        constants = [
            ce.TEXT, ce.THINKING, ce.TOOL_USE, ce.TOOL_INPUT, ce.TOOL_RESULT,
            ce.PERMISSION_REQUEST, ce.QUESTION, ce.SUBAGENT_START,
            ce.SUBAGENT_END, ce.DELEGATE_SPAWN, ce.DELEGATE_RESULT, ce.CHECK_VERDICT,
            ce.PLAN_MODE, ce.SYSTEM, ce.METADATA, ce.DONE, ce.ERROR,
            ce.QUEUE_TURN, ce.PRODUCER_DONE,
        ]
        for c in constants:
            assert isinstance(c, str), f"{c} is not a string"

    def test_type_constants_unique(self):
        from core.events import common_events as ce

        constants = [
            ce.TEXT, ce.THINKING, ce.TOOL_USE, ce.TOOL_INPUT, ce.TOOL_RESULT,
            ce.PERMISSION_REQUEST, ce.QUESTION, ce.SUBAGENT_START,
            ce.SUBAGENT_END, ce.DELEGATE_SPAWN, ce.DELEGATE_RESULT, ce.CHECK_VERDICT,
            ce.PLAN_MODE, ce.SYSTEM, ce.METADATA, ce.DONE, ce.ERROR,
            ce.QUEUE_TURN, ce.PRODUCER_DONE,
        ]
        assert len(constants) == len(set(constants)), "Duplicate type constants"


# ---------------------------------------------------------------------------
# CLI chunk → CommonEvent translator
# ---------------------------------------------------------------------------


class TestCLIChunkTranslator:
    def _make_chunk(self, **kwargs):
        from core.layers.cli.helpers import ClaudeStreamChunk
        return ClaudeStreamChunk(**kwargs)

    def test_text_chunk(self):
        from core.events.common_events import TEXT
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(text="hello", event_type="text")
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == TEXT
        assert events[0].data["content"] == "hello"

    def test_empty_text_produces_no_event(self):
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(text="", event_type="text")
        events = cli_chunk_to_events(chunk)
        assert len(events) == 0

    def test_thinking_chunk(self):
        from core.events.common_events import THINKING
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="thinking",
            event_data={"phase": "start", "text": ""},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == THINKING
        assert events[0].data["phase"] == "start"

    def test_tool_start_maps_to_tool_use(self):
        from core.events.common_events import TOOL_USE
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="tool_start",
            event_data={"name": "Read", "tool_id": "t1"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == TOOL_USE
        assert events[0].data["name"] == "Read"

    def test_tool_info_maps_to_tool_input(self):
        from core.events.common_events import TOOL_INPUT
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="tool_info",
            event_data={"name": "Read", "summary": "foo.py", "tool_input": {"file_path": "/x"}},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == TOOL_INPUT
        assert events[0].data["summary"] == "foo.py"

    def test_tool_end_maps_to_tool_result(self):
        from core.events.common_events import TOOL_RESULT
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="tool_end",
            event_data={"name": "Read", "tool_id": "t1"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == TOOL_RESULT

    def test_task_spawn_maps_to_subagent_start(self):
        from core.events.common_events import SUBAGENT_START
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="task_spawn",
            event_data={"description": "research", "run_in_background": True},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == SUBAGENT_START
        assert events[0].data["run_in_background"] is True

    def test_metadata_chunk(self):
        from core.events.common_events import METADATA
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="metadata",
            event_data={"cost_usd": 0.05, "context_used": 1000},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == METADATA
        assert events[0].data["cost_usd"] == 0.05

    def test_is_done_produces_done_event(self):
        from core.events.common_events import DONE
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(is_done=True, event_type="text", text="")
        events = cli_chunk_to_events(chunk)
        assert any(e.type == DONE for e in events)

    def test_is_error_produces_error_event(self):
        from core.events.common_events import ERROR
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(is_error=True, text="something went wrong")
        events = cli_chunk_to_events(chunk)
        assert any(e.type == ERROR for e in events)
        error_evt = [e for e in events if e.type == ERROR][0]
        assert error_evt.data["message"] == "something went wrong"

    def test_text_plus_done_produces_two_events(self):
        """A chunk can have both text content AND is_done flag."""
        from core.events.common_events import DONE, TEXT
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(text="final", event_type="text", is_done=True)
        events = cli_chunk_to_events(chunk)
        assert len(events) == 2
        assert events[0].type == TEXT
        assert events[1].type == DONE

    def test_metadata_plus_done(self):
        from core.events.common_events import DONE, METADATA
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="metadata",
            event_data={"cost_usd": 0.1},
            is_done=True,
        )
        events = cli_chunk_to_events(chunk)
        types = [e.type for e in events]
        assert METADATA in types
        assert DONE in types

    def test_plan_mode_event(self):
        from core.events.common_events import PLAN_MODE
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="plan_mode",
            event_data={"action": "enter"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == PLAN_MODE
        assert events[0].data["action"] == "enter"

    def test_permission_prompt(self):
        from core.events.common_events import PERMISSION_REQUEST
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="permission_prompt",
            event_data={"request_id": "r1", "tool_name": "Bash"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == PERMISSION_REQUEST

    def test_system_event(self):
        from core.events.common_events import SYSTEM
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="system",
            event_data={"subtype": "task_notification"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == SYSTEM
        assert events[0].data["subtype"] == "task_notification"

    def test_delegate_spawn(self):
        from core.events.common_events import DELEGATE_SPAWN
        from core.layers.cli.layer import cli_chunk_to_events

        chunk = self._make_chunk(
            event_type="delegate_spawn",
            event_data={"task_name": "research", "agent": "helper"},
        )
        events = cli_chunk_to_events(chunk)
        assert len(events) == 1
        assert events[0].type == DELEGATE_SPAWN


# ---------------------------------------------------------------------------
# Direct LLM → CommonEvent translator
# ---------------------------------------------------------------------------


class TestDirectEventTranslator:
    def test_text_event(self):
        from core.events.common_events import TEXT
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({"type": "text", "data": {"content": "hi"}})
        assert event.type == TEXT
        assert event.data["content"] == "hi"

    def test_empty_text_returns_none(self):
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({"type": "text", "data": {"content": ""}})
        assert event is None

    def test_tool_start(self):
        from core.events.common_events import TOOL_USE
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({
            "type": "tool_start",
            "data": {"name": "search", "tool_use_id": "t1"},
        })
        assert event.type == TOOL_USE
        assert event.data["name"] == "search"
        assert event.data["tool_id"] == "t1"

    def test_tool_end(self):
        from core.events.common_events import TOOL_RESULT
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({
            "type": "tool_end",
            "data": {"tool_use_id": "t1", "result_preview": "found 3"},
        })
        assert event.type == TOOL_RESULT

    def test_done(self):
        from core.events.common_events import DONE
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({"type": "done", "data": {}})
        assert event.type == DONE

    def test_error(self):
        from core.events.common_events import ERROR
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({
            "type": "error",
            "data": {"message": "API error"},
        })
        assert event.type == ERROR
        assert event.data["message"] == "API error"

    def test_session_event_returns_none(self):
        from core.layers.direct.layer import direct_event_to_common

        event = direct_event_to_common({
            "type": "session",
            "data": {"session_id": "s1"},
        })
        assert event is None  # session events are internal plumbing, not user-visible


# ---------------------------------------------------------------------------
# ExecutionLayer ABC
# ---------------------------------------------------------------------------


class TestExecutionLayerABC:
    def test_cannot_instantiate_abc(self):
        from core.execution_layer import ExecutionLayer

        with pytest.raises(TypeError):
            ExecutionLayer()

    def test_agent_config_defaults(self):
        from core.execution_layer import AgentConfig

        cfg = AgentConfig(agent_name="test")
        assert cfg.agent_name == "test"
        assert cfg.system_prompt == ""
        assert cfg.permission_mode == "ask"
        assert cfg.resume is False
        assert cfg.security_context is None
        assert cfg.credential_env == {}
        assert cfg.extra_env == {}

    def test_agent_config_all_fields(self):
        from core.execution_layer import AgentConfig

        cfg = AgentConfig(
            agent_name="test",
            system_prompt="You are a helper",
            mcp_config_path="/tmp/mcp.json",
            credential_env={"API_KEY": "xxx"},
            permission_mode="plan",
            client_type="dashboard",
            model="claude-opus-4-6",
            effort="high",
            resume=True,
            use_native_permissions=True,
            extra_env={"FOO": "bar"},
            security_context={"role": "admin"},
        )
        assert cfg.model == "claude-opus-4-6"
        assert cfg.security_context == {"role": "admin"}

    def test_agent_config_data_not_shared(self):
        """Each AgentConfig should get its own dicts."""
        from core.execution_layer import AgentConfig

        c1 = AgentConfig(agent_name="a")
        c2 = AgentConfig(agent_name="b")
        c1.credential_env["x"] = 1
        assert "x" not in c2.credential_env


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    def test_get_layer_for_cli_agent(self):
        from core.layers.cli.layer import CLIExecutionLayer
        from core.session.session_manager import get_execution_layer

        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            layer = get_execution_layer("test")
            assert isinstance(layer, CLIExecutionLayer)

    def test_get_layer_for_direct_agent(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer
        from core.session.session_manager import get_execution_layer

        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "slug": "caller", "execution_path": "direct-llm",
            }
            layer = get_execution_layer("caller")
            assert isinstance(layer, DirectLLMExecutionLayer)

    def test_unknown_agent_falls_back_to_the_platform_default(self):
        # An agent row that is GONE while a session of it may still be live
        # (deleted mid-session, a shutdown sweep) must NOT raise: the caller
        # at that point is almost always trying to CLOSE something. The
        # platform default is the right guess — it is what the row said.
        # Contrast test_unknown_path_fails_closed below: a KNOWN row naming
        # an unknown engine is a configuration error and does raise.
        from core.layers.cli.layer import CLIExecutionLayer
        from core.session.session_manager import get_execution_layer

        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = None
            layer = get_execution_layer("nonexistent")
            assert isinstance(layer, CLIExecutionLayer)

    def test_execution_target_local_skips_resolution(self):
        """A provided execution_target='local' returns the local layer by
        path WITHOUT re-resolving — the resolver must not be consulted."""
        from core.layers.cli.layer import CLIExecutionLayer
        from core.session.session_manager import get_execution_layer

        with patch("core.session.session_manager.agent_store") as mock_store, \
                patch("services.remote.remote_status.resolve_execution_target") as mock_resolve:
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            layer = get_execution_layer(
                "test", execution_path="claude-code-cli", execution_target="local",
            )
            assert isinstance(layer, CLIExecutionLayer)
            mock_resolve.assert_not_called()

    def test_execution_target_remote_skips_resolution(self):
        """A provided machine_id returns the remote layer without re-resolving;
        an admin-paired machine passes the isolation guards."""
        from core.session.session_manager import get_execution_layer

        sentinel_layer = MagicMock()
        with patch("core.session.session_manager.agent_store") as mock_store, \
                patch("services.remote.remote_status.resolve_execution_target") as mock_resolve, \
                patch("storage.remote_store.get_remote_machine") as mock_machine, \
                patch("core.session.session_manager._get_remote_layer", return_value=sentinel_layer):
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            mock_machine.return_value = {"pairing_scope": "admin"}
            layer = get_execution_layer(
                "test", execution_path="claude-code-cli",
                user_sub="u1", role="manager", execution_target="machine-123",
            )
            assert layer is sentinel_layer
            mock_resolve.assert_not_called()

    def test_execution_target_keeps_user_paired_guard(self):
        """The execution_target shortcut must STILL enforce the agent-scope
        refusal on user-paired machines (user_sub=None) — skipping resolution
        must not skip the isolation guards."""
        import pytest

        from core.session.session_manager import get_execution_layer

        with patch("core.session.session_manager.agent_store") as mock_store, \
                patch("services.remote.remote_status.resolve_execution_target") as mock_resolve, \
                patch("storage.remote_store.get_remote_machine") as mock_machine:
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            mock_machine.return_value = {"pairing_scope": "user", "registered_by": "someuser"}
            with pytest.raises(RuntimeError):
                get_execution_layer(
                    "test", execution_path="claude-code-cli",
                    user_sub=None, role="manager", execution_target="machine-xyz",
                )
            mock_resolve.assert_not_called()

    def test_user_scope_on_user_paired_machine_allowed(self):
        """Masked-bug fix: a USER-scope task carrying its creator's
        user_sub may run on that user's OWN (user-paired) machine — the guard
        refuses only sessions with NO user identity. This is exactly what the
        scheduler previously broke by omitting user_sub from get_execution_layer,
        which made a legit user-scope task on the user's own machine 'fail' as
        if it were an agent-scope session."""
        from core.session.session_manager import get_execution_layer

        sentinel_layer = MagicMock()
        with patch("core.session.session_manager.agent_store") as mock_store, \
                patch("services.remote.remote_status.resolve_execution_target") as mock_resolve, \
                patch("storage.remote_store.get_remote_machine") as mock_machine, \
                patch("storage.database.get_platform_setting", return_value="1"), \
                patch("core.session.session_manager._get_remote_layer", return_value=sentinel_layer):
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            mock_machine.return_value = {"pairing_scope": "user", "registered_by": "alice"}
            layer = get_execution_layer(
                "test", execution_path="claude-code-cli",
                user_sub="alice-sub", role="manager", execution_target="machine-alice",
            )
            assert layer is sentinel_layer       # ran on the user's OWN machine
            mock_resolve.assert_not_called()

    def test_no_user_identity_guard_message_is_scope_accurate(self):
        """The guard message names the real condition (no user identity), not a
        scope — a user-scope task without a threaded user_sub used to get a
        misleading 'Agent-scope sessions' error."""
        from core.session.session_manager import get_execution_layer
        with patch("core.session.session_manager.agent_store") as mock_store, \
                patch("services.remote.remote_status.resolve_execution_target"), \
                patch("storage.remote_store.get_remote_machine") as mock_machine:
            mock_store.get_agent.return_value = {
                "slug": "test", "execution_path": "claude-code-cli",
            }
            mock_machine.return_value = {"pairing_scope": "user", "registered_by": "someuser"}
            with pytest.raises(RuntimeError, match="no user identity"):
                get_execution_layer(
                    "test", execution_path="claude-code-cli",
                    user_sub=None, role="manager", execution_target="machine-xyz",
                )

    def test_unknown_path_fails_closed(self):
        # This used to return the CLI layer, which meant a typo'd or retired
        # engine id silently ran the agent on Claude Code: wrong binary, wrong
        # credentials, wrong sandbox, no error anywhere. An id no layer claims
        # is a configuration error and must surface as one.
        import pytest

        from core.session.session_manager import (
            UnknownExecutionPath,
            get_execution_layer,
            get_layer_by_path,
        )

        with pytest.raises(UnknownExecutionPath) as exc:
            get_layer_by_path("unknown-path")
        # The message names what IS registered, so an operator can see the typo.
        assert "claude-code-cli" in str(exc.value)

        with pytest.raises(UnknownExecutionPath):
            get_execution_layer("test", execution_path="unknown-path",
                                execution_target="local")

    def test_unknown_path_is_not_a_runtime_error(self):
        # scheduler/delivery.py and ws/duplex_attach.py already catch
        # RuntimeError from this module and read it as "the remote target is
        # offline". A misconfigured engine must not masquerade as that.
        import pytest

        from core.session.session_manager import (
            UnknownExecutionPath,
            get_layer_by_path,
        )

        assert not issubclass(UnknownExecutionPath, RuntimeError)
        with pytest.raises(ValueError):   # it IS a ValueError
            get_layer_by_path("unknown-path")

    def test_valid_execution_paths_is_the_registry(self):
        from core.session.session_manager import (
            _LAYERS,
            register_layer,
            valid_execution_paths,
        )

        assert valid_execution_paths() == set(_LAYERS)
        assert "claude-code-cli" in valid_execution_paths()
        # Registering a layer is the ONE step that makes its id valid
        # everywhere — the API validators, the checks parser and the admin
        # endpoints all read this set.
        try:
            register_layer("acme-cli", MagicMock())
            assert "acme-cli" in valid_execution_paths()
        finally:
            _LAYERS.pop("acme-cli", None)
        assert "acme-cli" not in valid_execution_paths()

    def test_find_layer_for_session_asks_each_layer(self):
        from core.session.session_manager import (
            find_layer_for_session,
            get_all_layers,
        )

        assert find_layer_for_session("no-such-session") is None
        assert find_layer_for_session("") is None
        # Every registered layer answers ownership synchronously, without the
        # caller importing its private pool dict.
        for layer in get_all_layers().values():
            assert layer.owns_session("no-such-session") is False

    def test_register_layer(self):
        from core.session.session_manager import (
            _LAYERS,
            get_layer_by_path,
            register_layer,
        )

        mock_layer = MagicMock()
        register_layer("test-layer", mock_layer)
        assert get_layer_by_path("test-layer") is mock_layer
        # Cleanup
        _LAYERS.pop("test-layer", None)


# ---------------------------------------------------------------------------
# set_session_mode / get_session_mode roundtrip
# ---------------------------------------------------------------------------


class TestSessionMode:
    def test_set_and_get(self):
        from core.session.session_state import (
            _session_modes,
            get_session_mode,
            set_session_mode,
        )

        sid = "test-session-mode-1"
        set_session_mode(sid, "plan")
        assert get_session_mode(sid) == "plan"
        # Cleanup
        _session_modes.pop(sid, None)

    def test_get_default(self):
        from core.session.session_state import get_session_mode

        assert get_session_mode("nonexistent-session") == "auto"

    def test_overwrite(self):
        from core.session.session_state import (
            _session_modes,
            get_session_mode,
            set_session_mode,
        )

        sid = "test-session-mode-2"
        set_session_mode(sid, "ask")
        set_session_mode(sid, "autoReview")
        assert get_session_mode(sid) == "autoReview"
        _session_modes.pop(sid, None)

    def test_a_word_from_before_1_9_is_recorded_in_the_current_vocabulary(self):
        from core.session.session_state import (
            _session_modes,
            get_session_mode,
            register_session_state,
            set_session_mode,
        )

        sid = "test-session-mode-legacy"
        set_session_mode(sid, "acceptEdits")
        assert get_session_mode(sid) == "autoReview"
        register_session_state(sid, "default", None)
        assert get_session_mode(sid) == "ask"
        _session_modes.pop(sid, None)


# ---------------------------------------------------------------------------
# CLIExecutionLayer capability flags
# ---------------------------------------------------------------------------


class TestCLICapabilities:
    def test_capabilities_object(self):
        from core.layers.cli.layer import CLIExecutionLayer

        layer = CLIExecutionLayer()
        caps = layer.capabilities
        assert caps.name == "claude-code-cli"
        assert caps.supports_resume is True
        assert caps.supports_permissions is True
        assert caps.supports_plan_mode is True
        assert caps.supports_todos is True
        assert caps.supports_subagents is True
        assert caps.supports_context_compression is True
        assert caps.supports_control_commands is True
        assert "set_model" in caps.control_commands
        assert caps.permission_modes == ["ask", "autoReview", "dontAsk", "plan"]
        assert caps.mcp_config_format == "json"

    def test_backwards_compat_properties(self):
        from core.layers.cli.layer import CLIExecutionLayer

        layer = CLIExecutionLayer()
        assert layer.supports_resume is True
        assert layer.supports_permissions is True
        assert layer.supports_plan_mode is True

    def test_to_dict(self):
        from core.layers.cli.layer import CLIExecutionLayer

        layer = CLIExecutionLayer()
        d = layer.capabilities.to_dict()
        assert d["name"] == "claude-code-cli"
        assert isinstance(d["models"], list)
        assert isinstance(d["permission_modes"], list)


class TestDirectCapabilities:
    def test_capabilities_object(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer

        layer = DirectLLMExecutionLayer()
        caps = layer.capabilities
        assert caps.name == "direct-llm"
        assert caps.supports_resume is False
        assert caps.supports_permissions is True  # inline permission checking
        assert caps.supports_plan_mode is False
        assert caps.supports_todos is False
        assert caps.supports_subagents is False
        assert caps.mcp_delivery == "proxy_managed"

    def test_backwards_compat_properties(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer

        layer = DirectLLMExecutionLayer()
        assert layer.supports_resume is False
        assert layer.supports_permissions is True  # inline permission checking
        assert layer.supports_plan_mode is False


# ---------------------------------------------------------------------------
# DirectLLMExecutionLayer lifecycle stubs
# ---------------------------------------------------------------------------


class TestDirectLifecycle:
    @pytest.mark.asyncio
    async def test_is_session_process_dead_always_false(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer

        layer = DirectLLMExecutionLayer()
        assert await layer.is_session_process_dead("any-id") is False

    @pytest.mark.asyncio
    async def test_can_resume_always_false(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer

        layer = DirectLLMExecutionLayer()
        assert await layer.can_resume_session("any-id") is ResumeVerdict.FRESH

    @pytest.mark.asyncio
    async def test_prepare_resume_is_noop(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer

        layer = DirectLLMExecutionLayer()
        # Should not raise
        await layer.prepare_resume("any-id")


# ---------------------------------------------------------------------------
# Codex resume gate — thread-backed, survives proxy restarts
# ---------------------------------------------------------------------------


class TestCliResumeProbe:
    """The local transcript probe reads line by line in a thread: the first
    user line decides, a torn last line is no fault, a file with no user
    line or none at all is fresh, a disk fault is undecided."""

    def test_the_first_user_line_decides_and_a_torn_tail_is_no_fault(self, tmp_path):
        from core.layers.cli.layer import CLIExecutionLayer
        f = tmp_path / "s.jsonl"
        f.write_bytes(b'{"type":"summary"}\n{"type":"user","message":"hi"}\n\xff\xfe torn')
        assert CLIExecutionLayer._session_file_has_user_message(f) is ResumeVerdict.RESUMABLE
        f.write_bytes(b'{"type": "user"}\n' + b"x" * 4096)
        assert CLIExecutionLayer._session_file_has_user_message(f) is ResumeVerdict.RESUMABLE
        f.write_bytes(b'{"type":"summary"}\n\xff')
        assert CLIExecutionLayer._session_file_has_user_message(f) is ResumeVerdict.FRESH
        f.write_bytes(b"")
        assert CLIExecutionLayer._session_file_has_user_message(f) is ResumeVerdict.FRESH
        assert CLIExecutionLayer._session_file_has_user_message(tmp_path / "gone.jsonl") is ResumeVerdict.FRESH

    def test_a_disk_fault_is_undecided(self, tmp_path, monkeypatch):
        import errno
        from pathlib import Path

        from core.layers.cli.layer import CLIExecutionLayer
        f = tmp_path / "s.jsonl"
        f.write_text('{"type":"user"}\n')

        def _fault(self, *a, **kw):
            raise OSError(errno.EIO, "Input/output error")
        monkeypatch.setattr(Path, "open", _fault)
        assert CLIExecutionLayer._session_file_has_user_message(f) is ResumeVerdict.UNKNOWN

    @pytest.mark.asyncio
    async def test_the_probe_runs_off_the_loop(self, temp_db, monkeypatch):
        import threading

        import config as app_config
        from core.layers.cli.layer import CLIExecutionLayer
        claude_dir = app_config.get_agent_dir("a1") / "users" / "alice" / ".claude"
        (claude_dir / "projects" / "p1").mkdir(parents=True)
        (claude_dir / "projects" / "p1" / "sess-thr.jsonl").write_text('{"type":"user"}\n')
        threads: list = []
        original = CLIExecutionLayer._session_file_has_user_message

        def _record(session_file):
            threads.append(threading.current_thread())
            return original(session_file)
        monkeypatch.setattr(CLIExecutionLayer, "_session_file_has_user_message", staticmethod(_record))
        layer = CLIExecutionLayer()
        assert await layer.can_resume_session(
            "sess-thr", agent_name="a1", username="alice") is ResumeVerdict.RESUMABLE
        assert threads and threads[0] is not threading.main_thread()


class TestCodexResumeGate:
    """can_resume_session is THREAD-backed: a proxy restart / idle reap empties
    the in-memory session pool but the thread's rollout persists on disk — the
    gate must not read that as context loss (it made the warmup stamp a false
    ``resume_failed`` digest-reseed on chats whose thread/resume then
    succeeded)."""

    @pytest.mark.asyncio
    async def test_restart_fallback_keyed_on_thread_rollout(self, temp_db):
        import config as app_config
        from core.layers.codex.layer import CodexCLIExecutionLayer

        temp_db.create_chat("crg1", "user-admin", "a1")
        temp_db.update_chat("crg1", session_id="sess-crg1", codex_thread_id="th-abc")
        layer = CodexCLIExecutionLayer()

        # Pool empty + no rollout on disk → genuine context loss, gate refuses.
        assert await layer.can_resume_session(
            "sess-crg1", agent_name="a1", username="alice") is ResumeVerdict.FRESH

        rollout_dir = (app_config.get_agent_dir("a1") / "users" / "alice"
                       / ".codex" / "sessions" / "2026" / "07")
        rollout_dir.mkdir(parents=True)
        (rollout_dir / "rollout-2026-07-07T14-00-00-th-abc.jsonl").write_text("{}\n")
        assert await layer.can_resume_session(
            "sess-crg1", agent_name="a1", username="alice") is ResumeVerdict.RESUMABLE
        # A different thread's rollout doesn't count.
        assert await layer.can_resume_session(
            "sess-crg1", agent_name="a1", username="bob") is ResumeVerdict.FRESH

    @pytest.mark.asyncio
    async def test_agent_scope_falls_back_to_workspace_codex(self, temp_db):
        import config as app_config
        from core.layers.codex.layer import CodexCLIExecutionLayer

        temp_db.create_chat("crg2", "user-admin", "a1")
        temp_db.update_chat("crg2", session_id="sess-crg2", codex_thread_id="th-ws")
        rollout_dir = (app_config.get_agent_dir("a1") / "workspace" / ".codex"
                       / "sessions" / "2026")
        rollout_dir.mkdir(parents=True)
        (rollout_dir / "rollout-x-th-ws.jsonl").write_text("{}\n")

        layer = CodexCLIExecutionLayer()
        assert await layer.can_resume_session("sess-crg2", agent_name="a1") is ResumeVerdict.RESUMABLE

    @pytest.mark.asyncio
    async def test_no_thread_id_refuses(self, temp_db):
        from core.layers.codex.layer import CodexCLIExecutionLayer

        temp_db.create_chat("crg3", "user-admin", "a1")
        temp_db.update_chat("crg3", session_id="sess-crg3")
        layer = CodexCLIExecutionLayer()
        assert await layer.can_resume_session(
            "sess-crg3", agent_name="a1", username="alice") is ResumeVerdict.FRESH
