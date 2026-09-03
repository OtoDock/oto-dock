"""Device-local MCP support on the satellite.

Covers the display probe (``_detect_display``) and the device-grant policy
cache (``satellite_policy``).
"""
import platform

import pytest

from satellite.sessions.session_manager import _detect_display
from satellite.host import satellite_policy


# ---------------------------------------------------------------------------
# _detect_display
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("system, env, server, has", [
    ("Linux", {"WAYLAND_DISPLAY": "wayland-0"}, "wayland", True),
    ("Linux", {"DISPLAY": ":0"}, "x11", True),
    ("Linux", {}, "none", False),          # headless (systemd --user + linger)
    ("Darwin", {}, "quartz", True),        # per-user LaunchAgent ⇒ GUI session
    ("Windows", {}, "windows", True),      # interactive logon task
    ("Plan9", {}, "none", False),          # unknown OS → fail to no-display
])
def test_detect_display(monkeypatch, system, env, server, has):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    d = _detect_display()
    assert d["server"] == server
    assert d["has_display"] is has
    # session_active_unlocked is the coarse connect-time hint (= has_display)
    assert d["session_active_unlocked"] is has
    assert set(d) == {"has_display", "server", "session_active_unlocked"}


def test_detect_display_prefers_wayland_over_x11(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert _detect_display()["server"] == "wayland"


# ---------------------------------------------------------------------------
# satellite_policy device-grant cache
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_policy():
    # satellite_policy holds process-global state; reset around each test.
    satellite_policy.set_policy({"allow_full_fs": False, "device_grants": []})
    yield
    satellite_policy.set_policy({"allow_full_fs": False, "device_grants": []})


def test_device_grants_default_empty():
    assert satellite_policy.get_policy().get("device_grants") == []


def test_device_grants_set_and_query():
    satellite_policy.set_policy({"device_grants": ["computer", "browser"]})
    assert set(satellite_policy.get_policy()["device_grants"]) == {"computer", "browser"}


def test_partial_update_preserves_other_fields():
    satellite_policy.set_policy({"device_grants": ["computer"]})
    # A partial update of allow_full_fs must NOT clear device_grants.
    satellite_policy.set_policy({"allow_full_fs": True})
    assert satellite_policy.get_policy()["device_grants"] == ["computer"]
    assert satellite_policy.is_full_fs_allowed() is True


def test_device_grants_revoke():
    satellite_policy.set_policy({"device_grants": ["computer"]})
    satellite_policy.set_policy({"device_grants": []})
    assert satellite_policy.get_policy()["device_grants"] == []


def test_device_grants_fail_closed_on_garbage():
    satellite_policy.set_policy({"device_grants": "not-a-list"})
    assert satellite_policy.get_policy()["device_grants"] == []
