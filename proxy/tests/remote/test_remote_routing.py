"""Tests for execution layer routing with remote targets."""

import pytest
from unittest.mock import patch


class TestGetExecutionLayer:
    """Test that get_execution_layer correctly routes to remote layer."""

    def test_local_agent_returns_cli_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("local", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer, _cli_layer
            layer = get_execution_layer("test-agent")
            assert layer is _cli_layer

    def test_local_direct_llm_returns_direct_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "execution_path": "direct-llm",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer, _direct_layer
            layer = get_execution_layer("test-agent")
            assert layer is _direct_layer

    def test_remote_direct_llm_still_returns_direct_layer(self):
        """Direct LLM always runs locally, even if execution_target is remote."""
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = {
                "execution_path": "direct-llm",
                "execution_target": "machine-123",
            }
            from core.session.session_manager import get_execution_layer, _direct_layer
            layer = get_execution_layer("test-agent")
            assert layer is _direct_layer

    def test_remote_cli_returns_remote_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("machine-123", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "machine-123",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_remote_codex_returns_remote_layer(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("machine-456", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "codex-cli",
                "execution_target": "machine-456",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_execution_path_override_respected(self):
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("local", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer, _codex_layer
            layer = get_execution_layer("test-agent", execution_path="codex-cli")
            assert layer is _codex_layer

    def test_unknown_agent_defaults_to_cli(self):
        with patch("core.session.session_manager.agent_store") as mock_store:
            mock_store.get_agent.return_value = None
            from core.session.session_manager import get_execution_layer, _cli_layer
            layer = get_execution_layer("nonexistent")
            assert layer is _cli_layer

    def test_user_override_returns_remote_layer(self):
        """User has a personal machine, agent default is local."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("machine-user-123", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer("test-agent", user_sub="user-1")
            assert type(layer).__name__ == "RemoteExecutionLayer"

    def test_no_user_sub_uses_agent_default(self):
        """Without user_sub, resolve_execution_target uses agent default."""
        with patch("core.session.session_manager.agent_store") as mock_store, \
             patch("storage.remote_store.resolve_execution_target", return_value=("local", None)):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli",
                "execution_target": "local",
            }
            from core.session.session_manager import get_execution_layer, _cli_layer
            layer = get_execution_layer("test-agent")
            assert layer is _cli_layer


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
                   return_value=self._user_paired()), \
             patch("storage.database.get_platform_setting", return_value="1"):
            mock_store.get_agent.return_value = {
                "execution_path": "claude-code-cli", "execution_target": "m-user-1",
            }
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer(
                "test-agent", user_sub="real-sub", execution_target="m-user-1",
            )
            assert type(layer).__name__ == "RemoteExecutionLayer"

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
