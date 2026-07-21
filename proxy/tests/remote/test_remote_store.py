"""Tests for remote machine storage layer."""
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock
@pytest.fixture(autouse=True)
def mock_db():
    """Mock get_conn for all tests in this module."""
    mock_conn = MagicMock()
    @contextmanager
    def fake_get_conn():
        yield mock_conn
    with patch("storage.remote_store.get_conn", fake_get_conn):
        yield mock_conn
class TestCreateRemoteMachine:
    def test_creates_machine_with_token(self, mock_db):
        from storage.remote_store import create_remote_machine
        # Each execute() call returns a fresh cursor mock
        row_data = {
            "id": "test-id",
            "name": "test-machine",
            "status": "offline",
            "last_seen": None,
            "registered_by": "user-1",
            "pairing_token_hash": "hash",
            "pairing_token_created_at": "2026-01-01",
            "machine_secret_hash": None,
            "capabilities": "{}",
            "created_at": "2026-01-01",
        }
        # Sequence of execute results:
        # 1. SELECT name uniqueness -> fetchone returns None
        # 2. INSERT -> no fetchone needed
        # 3. SELECT new row -> fetchone returns row_data
        results = [
            MagicMock(fetchone=MagicMock(return_value=None)),   # name check
            MagicMock(),                                         # INSERT
            MagicMock(fetchone=MagicMock(return_value=row_data)),  # SELECT
        ]
        mock_db.execute.side_effect = results
        result = create_remote_machine(
            machine_id="test-id",
            name="test-machine",
            
            registered_by="user-1",
        )
        assert result["id"] == "test-id"
        assert "pairing_token" in result
        assert len(result["pairing_token"]) > 0
    def test_duplicate_name_raises(self, mock_db):
        from storage.remote_store import create_remote_machine
        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = {"id": "existing"}
        mock_db.execute.return_value = cursor_mock
        with pytest.raises(ValueError, match="already in use"):
            create_remote_machine(
                machine_id="new-id",
                name="existing-machine",
                
                registered_by="user-1",
            )
class TestExchangePairingToken:
    def test_valid_exchange(self, mock_db):
        from storage.remote_store import exchange_pairing_token, _sha256
        from datetime import datetime, timezone
        token = "test-token-abc123"
        token_hash = _sha256(token)
        now = datetime.now(timezone.utc).isoformat()
        mock_db.execute.return_value.fetchone.return_value = {
            "pairing_token_hash": token_hash,
            "pairing_token_created_at": now,
        }
        secret = exchange_pairing_token("machine-1", token)
        assert len(secret) > 0
    def test_invalid_token_raises(self, mock_db):
        from storage.remote_store import exchange_pairing_token, _sha256
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        mock_db.execute.return_value.fetchone.return_value = {
            "pairing_token_hash": _sha256("correct-token"),
            "pairing_token_created_at": now,
        }
        with pytest.raises(ValueError, match="Invalid pairing token"):
            exchange_pairing_token("machine-1", "wrong-token")
    def test_already_exchanged_raises(self, mock_db):
        from storage.remote_store import exchange_pairing_token
        mock_db.execute.return_value.fetchone.return_value = {
            "pairing_token_hash": None,
            "pairing_token_created_at": None,
        }
        with pytest.raises(ValueError, match="already exchanged"):
            exchange_pairing_token("machine-1", "any-token")
class TestVerifyMachineSecret:
    def test_valid_secret(self, mock_db):
        from storage.remote_store import verify_machine_secret, _sha256
        secret = "my-secret"
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_secret_hash": _sha256(secret),
        }
        assert verify_machine_secret("machine-1", secret) is True
    def test_invalid_secret(self, mock_db):
        from storage.remote_store import verify_machine_secret, _sha256
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_secret_hash": _sha256("correct-secret"),
        }
        assert verify_machine_secret("machine-1", "wrong-secret") is False
    def test_no_secret_stored(self, mock_db):
        from storage.remote_store import verify_machine_secret
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_secret_hash": None,
        }
        assert verify_machine_secret("machine-1", "any") is False
class TestDeleteMachine:
    def test_cascade_resets_agents(self, mock_db):
        from storage.remote_store import delete_remote_machine
        cursor_mock = MagicMock()
        cursor_mock.rowcount = 1
        mock_db.execute.return_value = cursor_mock
        with patch("storage.agent_store._invalidate_cache"):
            result = delete_remote_machine("machine-1")
        assert result is True
        # Verify agent reset and user target cleanup were called
        calls = [str(c) for c in mock_db.execute.call_args_list]
        assert any("execution_target" in c for c in calls)
        assert any("user_remote_targets" in c for c in calls)
class TestResolveExecutionTarget:
    """Test the unified target resolution: user > agent > local.
    The function now returns a (target, fallback_reason) tuple. Reachability
    is queried via services.remote.remote_status.is_reachable, mocked per-test.
    """
    def test_no_user_returns_agent_default(self, mock_db):
        from storage.remote_store import resolve_execution_target
        with patch("storage.agent_store.get_agent") as mock_agent, \
             patch("services.remote.remote_status.is_reachable", return_value=True):
            mock_agent.return_value = {"execution_target": "machine-agent"}
            result = resolve_execution_target("test-agent", user_sub=None)
            assert result == ("machine-agent", None)
    def test_no_user_no_agent_returns_local(self, mock_db):
        from storage.remote_store import resolve_execution_target
        with patch("storage.agent_store.get_agent") as mock_agent:
            mock_agent.return_value = {"execution_target": "local"}
            result = resolve_execution_target("test-agent", user_sub=None)
            assert result == ("local", None)
    def test_user_override_online_takes_priority(self, mock_db):
        from storage.remote_store import resolve_execution_target
        # Only one query — the per-agent row.
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_id": "machine-user",
            "status": "online",
            "user_sub": "user-1",
            "agent_slug": "test-agent",
            "name": "my-laptop",
            "capabilities": "{}",
        }
        with patch("storage.agent_store.get_agent") as mock_agent, \
             patch("services.remote.remote_status.is_reachable", return_value=True):
            mock_agent.return_value = {"execution_target": "machine-agent"}
            result = resolve_execution_target("test-agent", user_sub="user-1")
            assert result == ("machine-user", None)
    def test_user_offline_falls_back_to_agent(self, mock_db):
        from storage.remote_store import resolve_execution_target
        # Only the per-agent query runs. Legacy global fallback gone.
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_id": "machine-user",
            "status": "offline",
            "user_sub": "user-1",
            "agent_slug": "test-agent",
            "name": "my-laptop",
            "capabilities": "{}",
        }
        # User machine unreachable → fall through to agent default (reachable).
        with patch("storage.agent_store.get_agent") as mock_agent, \
             patch("services.remote.remote_status.is_reachable",
                   side_effect=lambda mid: mid == "machine-agent"):
            mock_agent.return_value = {"execution_target": "machine-agent"}
            result = resolve_execution_target("test-agent", user_sub="user-1")
            assert result == ("machine-agent", "user-override-offline")
    def test_user_no_target_uses_agent_default(self, mock_db):
        from storage.remote_store import resolve_execution_target
        # Only one query — per-agent — returns None.
        mock_db.execute.return_value.fetchone.return_value = None
        with patch("storage.agent_store.get_agent") as mock_agent, \
             patch("services.remote.remote_status.is_reachable", return_value=True):
            mock_agent.return_value = {"execution_target": "machine-agent"}
            result = resolve_execution_target("test-agent", user_sub="user-1")
            assert result == ("machine-agent", None)
    def test_agent_specific_target_overrides_global(self, mock_db):
        from storage.remote_store import resolve_execution_target
        # Agent-specific target found (first query)
        mock_db.execute.return_value.fetchone.return_value = {
            "machine_id": "machine-specific",
            "status": "online",
            "user_sub": "user-1",
            "agent_slug": "test-agent",
            "name": "dev-server",
            "capabilities": "{}",
        }
        with patch("storage.agent_store.get_agent") as mock_agent, \
             patch("services.remote.remote_status.is_reachable", return_value=True):
            mock_agent.return_value = {"execution_target": "local"}
            result = resolve_execution_target("test-agent", user_sub="user-1")
            assert result == ("machine-specific", None)
