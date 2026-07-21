"""Tests for services.remote.remote_status — live WS status merge."""

import time
from unittest.mock import MagicMock, patch



def _fake_conn(last_heartbeat_age_s: float):
    """Build a fake SatelliteConnection with the given heartbeat age."""
    conn = MagicMock()
    conn.last_heartbeat = time.monotonic() - last_heartbeat_age_s
    return conn


def test_online_when_heartbeat_recent(temp_db):
    """Heartbeat < 60s → state = online."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": "2026-01-01T00:00:00Z"}):
        mock_cm.return_value.get_connection.return_value = _fake_conn(10.0)
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "online"
        assert status["reachable"] is True
        assert status["last_heartbeat_age_s"] == 10


def test_stale_when_heartbeat_70s(temp_db):
    """Heartbeat 60-90s → state = stale."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": ""}):
        mock_cm.return_value.get_connection.return_value = _fake_conn(70.0)
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "stale"
        assert status["reachable"] is True


def test_disconnected_in_memory_when_heartbeat_over_90s(temp_db):
    """Heartbeat > 90s but still in _connections → state = disconnected."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": "2026-01-01T00:00:00Z"}):
        mock_cm.return_value.get_connection.return_value = _fake_conn(120.0)
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "disconnected"
        assert status["reachable"] is False


def test_disconnected_not_in_memory_with_last_seen(temp_db):
    """Not in _connections but has DB last_seen → disconnected."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine",
               return_value={"last_seen": "2026-01-01T00:00:00Z"}):
        mock_cm.return_value.get_connection.return_value = None
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "disconnected"
        assert status["reachable"] is False
        assert status["last_heartbeat_age_s"] is None


def test_never_connected_no_machine(temp_db):
    """Machine not in DB → never_connected."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value=None):
        mock_cm.return_value.get_connection.return_value = None
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "never_connected"
        assert status["reachable"] is False


def test_never_connected_no_last_seen(temp_db):
    """Machine paired but never connected (no last_seen)."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": ""}):
        mock_cm.return_value.get_connection.return_value = None
        status = remote_status.get_live_machine_status("m-1")
        assert status["state"] == "never_connected"


def test_is_reachable_shortcut(temp_db):
    """is_reachable is a shortcut for state ∈ {online, stale}."""
    from services.remote import remote_status

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": ""}):
        mock_cm.return_value.get_connection.return_value = _fake_conn(5.0)
        assert remote_status.is_reachable("m-1") is True

    with patch("core.remote.satellite_connection.get_connection_manager") as mock_cm, \
         patch("storage.remote_store.get_remote_machine", return_value={"last_seen": ""}):
        mock_cm.return_value.get_connection.return_value = None
        assert remote_status.is_reachable("m-1") is False
