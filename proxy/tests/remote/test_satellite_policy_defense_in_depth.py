"""Tests for satellite-side defense-in-depth.

Covers:
  * The ``satellite.satellite_policy`` cache (get/set/is_full_fs_allowed)
  * The ``_check_satellite_host_policy`` validator on the satellite-side
    file_push / file_pull handlers
  * The proxy-side ``policy`` block in ``auth_result`` is wire-compatible
    (parsed into the cache verbatim).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT as _REPO_ROOT
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from satellite.host import satellite_policy as _pol  # noqa: E402
from satellite.sessions.session_manager import (  # noqa: E402
    _check_satellite_host_policy,
    _validate_satellite_host_path,
)


# ---------------------------------------------------------------------------
# Policy cache
# ---------------------------------------------------------------------------


class TestPolicyCache:
    def setup_method(self):
        # Reset to safe baseline before each test — module-level state.
        # device_grants is reset too for isolation.
        _pol.set_policy({"allow_full_fs": False, "device_grants": []})

    def test_default_is_home_only(self):
        # Fresh import baseline.
        _pol.set_policy({"allow_full_fs": False, "device_grants": []})
        assert _pol.is_full_fs_allowed() is False
        assert _pol.get_policy() == {"allow_full_fs": False, "device_grants": []}

    def test_set_policy_flips_flag(self):
        _pol.set_policy({"allow_full_fs": True})
        assert _pol.is_full_fs_allowed() is True

    def test_set_policy_preserves_unspecified_keys(self):
        _pol.set_policy({"allow_full_fs": True})
        _pol.set_policy({})  # missing key — must NOT clobber
        assert _pol.is_full_fs_allowed() is True

    def test_set_policy_ignores_non_dict(self):
        _pol.set_policy({"allow_full_fs": True})
        _pol.set_policy("not-a-dict")  # type: ignore[arg-type]
        assert _pol.is_full_fs_allowed() is True

    def test_get_policy_returns_copy(self):
        snapshot = _pol.get_policy()
        snapshot["allow_full_fs"] = True  # mutate the copy
        assert _pol.is_full_fs_allowed() is False


# ---------------------------------------------------------------------------
# _check_satellite_host_policy
# ---------------------------------------------------------------------------


class TestSatelliteHostPolicyCheck:
    def setup_method(self):
        _pol.set_policy({"allow_full_fs": False})

    def test_full_fs_allows_anything(self):
        _pol.set_policy({"allow_full_fs": True})
        # Doesn't raise for system paths.
        _check_satellite_host_policy("/etc/hosts")
        _check_satellite_host_policy("/usr/bin/python3")
        _check_satellite_host_policy("C:/Windows/System32/cmd.exe")

    def test_home_only_admits_home_paths(self):
        # The test runs on Linux/macOS CI; pick whichever Path.home() returns.
        home = str(Path.home().resolve()).replace("\\", "/")
        _check_satellite_host_policy(f"{home}/Desktop/foo.png")
        _check_satellite_host_policy(f"{home}/code/x.py")

    def test_home_only_rejects_etc(self):
        with pytest.raises(ValueError, match="outside the OS user's home"):
            _check_satellite_host_policy("/etc/sudoers")

    def test_home_only_rejects_other_user_home(self):
        # /root or /home/other are not under the OS user's home on Linux.
        # On macOS /Users/other isn't under /Users/<me>. Use a path that
        # is guaranteed outside any plausible CI runner's home.
        with pytest.raises(ValueError, match="outside the OS user's home"):
            _check_satellite_host_policy("/var/log/syslog")

    def test_policy_update_invalidates_admit(self):
        # Admit while allow_full_fs=True.
        _pol.set_policy({"allow_full_fs": True})
        _check_satellite_host_policy("/etc/hosts")
        # Flip to False — same path now rejects.
        _pol.set_policy({"allow_full_fs": False})
        with pytest.raises(ValueError):
            _check_satellite_host_policy("/etc/hosts")


# ---------------------------------------------------------------------------
# Structural validation (existing, unchanged behavior)
# ---------------------------------------------------------------------------


class TestStructuralValidation:
    def test_relative_path_rejected_even_with_full_fs(self):
        _pol.set_policy({"allow_full_fs": True})
        # _validate_satellite_host_path is the structural check; it
        # rejects relative paths regardless of policy.
        with pytest.raises(ValueError, match="must be absolute"):
            _validate_satellite_host_path("foo.png")

    def test_traversal_rejected_even_with_full_fs(self):
        _pol.set_policy({"allow_full_fs": True})
        with pytest.raises(ValueError, match=r"contains '\.\.'"):
            _validate_satellite_host_path("/home/erin/../etc/passwd")


# ---------------------------------------------------------------------------
# Wire compatibility: proxy auth_result `policy` block parses correctly
# ---------------------------------------------------------------------------


class TestAuthResultPolicyWire:
    def setup_method(self):
        _pol.set_policy({"allow_full_fs": False})

    def test_auth_result_policy_block_applied(self):
        # Simulate what ws_client._authenticate does on receipt.
        resp_from_proxy = {
            "type": "auth_result",
            "status": "ok",
            "policy": {"allow_full_fs": True},
        }
        _pol.set_policy(resp_from_proxy.get("policy") or {})
        assert _pol.is_full_fs_allowed() is True

    def test_missing_policy_block_defaults_to_safe(self):
        # Older proxy may not send `policy` — satellite should default
        # to home-only (the safer baseline).
        resp_from_proxy = {"type": "auth_result", "status": "ok"}
        _pol.set_policy(resp_from_proxy.get("policy") or {})
        assert _pol.is_full_fs_allowed() is False

    def test_policy_update_message_applied(self):
        # Mid-session live update (admin toggled from dashboard).
        msg = {"type": "policy_update", "policy": {"allow_full_fs": True}}
        _pol.set_policy(msg.get("policy") or {})
        assert _pol.is_full_fs_allowed() is True
