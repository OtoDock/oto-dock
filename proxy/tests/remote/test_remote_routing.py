"""Tests for execution layer routing with remote targets."""

from unittest.mock import patch

import pytest

from core import placement
from tests._compute import placed


class TestGetExecutionLayer:
    """Test that get_execution_layer correctly routes to remote layer."""

    def test_local_agent_runs_on_the_compute_machine_or_is_refused(self):
        """The platform starts no session of a process engine itself: an
        agent of the platform's place gets the remote layer for the compute
        placement its config was built with, and the refusal with none."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target", return_value=("local", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent", placement_built=placed())
            assert type(layer).__name__ == "RemoteExecutionLayer"
            with pytest.raises(placement.NoComputeMachine) as refused:
                get_execution_layer("test-agent")
            assert str(refused.value) == placement.NO_COMPUTE_SENTENCE

    def test_local_direct_llm_returns_direct_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "execution_path": "direct-llm",
                "execution_target": "local",
            }
            from core.session.session_manager import _direct_layer, get_execution_layer
            layer = get_execution_layer("test-agent")
            assert layer is _direct_layer

    def test_remote_direct_llm_still_returns_direct_layer(self):
        """Direct LLM always runs locally, even if execution_target is remote."""
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "execution_path": "direct-llm",
                "execution_target": "machine-123",
            }
            from core.session.session_manager import _direct_layer, get_execution_layer
            layer = get_execution_layer("test-agent")
            assert layer is _direct_layer

    def test_remote_cli_returns_remote_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target", return_value=("machine-123", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "machine-123",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_remote_codex_returns_remote_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target", return_value=("machine-456", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "codex-cli",
                "execution_target": "machine-456",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_execution_path_override_respected(self):
        """The override decides the engine, never the agent's row: a Direct
        LLM override on a Claude Code agent answers the Direct layer (its
        loop stays in the proxy), an engine nothing registers is its own
        error, and a Codex override of the platform's place with no compute
        placement is refused as any process engine is."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target", return_value=("local", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import (
                UnknownExecutionPath,
                _direct_layer,
                get_execution_layer,
            )
            assert get_execution_layer("test-agent", execution_path="direct-llm") is _direct_layer
            with pytest.raises(UnknownExecutionPath):
                get_execution_layer("test-agent", execution_path="no-such-engine")
            with pytest.raises(placement.NoComputeMachine):
                get_execution_layer("test-agent", execution_path="codex-cli")
            layer = get_execution_layer("test-agent", execution_path="codex-cli", placement_built=placed())
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_unknown_agent_defaults_to_the_platforms_engine(self):
        """A row that is gone reads as the platform's default engine, a
        process engine: the remote layer for a compute placement, the
        refusal with none (never an unknown-engine error)."""
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = None
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("nonexistent", placement_built=placed())
            assert type(layer).__name__ == "RemoteExecutionLayer"
            with pytest.raises(placement.NoComputeMachine):
                get_execution_layer("nonexistent")

    def test_user_override_returns_remote_layer(self):
        """User has a personal machine, agent default is local."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target", return_value=("machine-user-123", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent", user_sub="user-1")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_no_user_sub_uses_agent_default(self):
        """Without user_sub, the resolver is asked for the agent's default
        place (nobody's override). Its answer here is the platform's place,
        which has no layer without a compute placement."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("services.remote.remote_status.resolve_execution_target",
                   return_value=("local", None)) as resolve:
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer
            with pytest.raises(placement.NoComputeMachine):
                get_execution_layer("test-agent")
            resolve.assert_called_once_with("test-agent", None, "manager")


class TestServiceAccountNeverOnUserMachine:
    """Service-account sessions (no real user_sub) must be refused on user-paired
    machines, so their service-account credentials (GH_TOKEN, MCP bearer, …) can
    never land on a user-owned disk — the routing half of the bearer-swap guarantee."""

    def _user_paired(self):
        return {"pairing_scope": "user", "registered_by": "owner-x"}

    def test_agent_scope_none_refused_on_user_paired(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.get_remote_machine",
                   return_value=self._user_paired()):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli", "execution_target": "m-user-1",
            }
            from core.session.session_manager import get_execution_layer
            with pytest.raises(RuntimeError, match="user-paired"):
                get_execution_layer(
                    "test-agent", user_sub=None, execution_target="m-user-1",
                )

    def test_agent_scope_empty_string_refused_on_user_paired(self):
        """The hardening: an empty-string user_sub is ALSO service-scope
        (``pick_account`` treats ``""`` as service), so the guard keys on
        ``not user_sub`` — ``""`` must be refused too, not just ``None``."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.get_remote_machine",
                   return_value=self._user_paired()):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli", "execution_target": "m-user-1",
            }
            from core.session.session_manager import get_execution_layer
            with pytest.raises(RuntimeError, match="user-paired"):
                get_execution_layer(
                    "test-agent", user_sub="", execution_target="m-user-1",
                )

    def test_user_scope_allowed_on_user_paired(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.get_remote_machine",
                   return_value={"pairing_scope": "user", "registered_by": "real-sub"}), \
             patch("storage.database.get_platform_setting", return_value="1"):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli", "execution_target": "m-user-1",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer(
                "test-agent", user_sub="real-sub", execution_target="m-user-1",
            )
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_the_spawn_guard_refuses_another_persons_machine_and_a_role_below_owner(self):
        # The placement guard at the remote spawn: a personal machine runs
        # only its owner's sessions; an admin machine none below the owner
        # tier; a run with no person runs on an admin machine.
        from types import SimpleNamespace

        from core.remote.remote_session_start import placement_refuses

        def cfg(sub, role, agent="test-agent"):
            return SimpleNamespace(user_sub=sub, placement_sub=None, agent_name=agent,
                                   security_context=SimpleNamespace(role=role))
        mine = {"pairing_scope": "user", "registered_by": "real-sub"}
        admin = {"id": "m-admin-1", "pairing_scope": "admin", "registered_by": "admin-x"}
        defaults = {"test-agent": {"execution_target": "m-admin-1"},
                    "other-agent": {"execution_target": "local"}}
        with patch("storage.agents.agent_store.get_agent", side_effect=defaults.get):
            with patch("storage.remote_store.get_remote_machine", return_value=mine):
                assert not placement_refuses("m-user-1", cfg("real-sub", "editor"))
                assert placement_refuses("m-user-1", cfg("other-sub", "admin"))
                assert placement_refuses("m-user-1", cfg("", "manager"))
            with patch("storage.remote_store.get_remote_machine", return_value=admin):
                assert placement_refuses("m-admin-1", cfg("editor-sub", "editor"))
                assert not placement_refuses("m-admin-1", cfg("mgr-sub", "manager"))
                assert not placement_refuses("m-admin-1", cfg("", "manager"))
            # Its admin's switch: every role of the agents whose default it
            # is, never another agent's session.
            switched = dict(admin, all_roles_run_here=True)
            with patch("storage.remote_store.get_remote_machine", return_value=switched):
                assert not placement_refuses("m-admin-1", cfg("viewer-sub", "viewer"))
                assert placement_refuses("m-admin-1", cfg("viewer-sub", "viewer", "other-agent"))
            with patch("storage.remote_store.get_remote_machine", return_value=None):
                assert not placement_refuses("m-gone", cfg("x", "viewer"))

    def test_agent_scope_allowed_on_admin_shared(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.get_remote_machine",
                   return_value={"pairing_scope": "admin", "registered_by": "admin-x"}):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli", "execution_target": "m-admin-1",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer(
                "test-agent", user_sub=None, execution_target="m-admin-1",
            )
            assert type(layer).__name__ == "RemoteExecutionLayer"
