"""Tests for services.mcp.mcp_sync._diff — install/update/remove computation."""

from unittest.mock import MagicMock, patch


def _fake_manifest(name: str, runtime: str = "python"):
    m = MagicMock()
    m.name = name
    m.server_name = name
    m.server = MagicMock()
    m.server.runtime = runtime
    m.server.source = f"pypi:{name}@1.0.0"
    m.mcp_dir = f"/tmp/{name}"
    m.category = "custom"
    m.version = "1.0.0"
    return m


def test_install_when_missing():
    """Desired but not installed → to_install."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="abc123"):
        install, update, remove = mcp_sync._diff(
            desired={"foo", "bar"},
            installed={},
        )
        assert install == {"foo", "bar"}
        assert update == set()
        assert remove == set()


def test_update_when_hash_drifts():
    """Installed but version_hash differs → to_update."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="NEW_HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "OLD_HASH", "healthy": True}},
        )
        assert install == set()
        assert update == {"foo"}
        assert remove == set()


def test_remove_when_unassigned():
    """Installed but not desired → to_remove (scope-aware GC)."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={
                "foo": {"version_hash": "h", "healthy": True},
                "bar": {"version_hash": "h", "healthy": True},
            },
        )
        assert remove == {"bar"}


def test_docker_mcp_never_installed_on_satellite():
    """Docker runtime MCPs stay on the platform — excluded from satellite install."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=lambda n: _fake_manifest(n, runtime="docker")):
        install, update, remove = mcp_sync._diff(
            desired={"file-tools"},
            installed={},
        )
        assert install == set()


def test_unhealthy_triggers_reinstall():
    """Unhealthy (mid-install marker) → reinstall."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "h", "healthy": False}},
        )
        assert install == {"foo"}


def test_same_hash_no_update():
    """Matching version_hash → no update needed."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "HASH", "healthy": True}},
        )
        assert install == set()
        assert update == set()
        assert remove == set()


def test_force_always_updates():
    """force=True re-installs even when hashes match."""
    from services.mcp import mcp_sync

    with patch("services.mcp.mcp_registry.get_manifest",
               side_effect=_fake_manifest), \
         patch("services.mcp.mcp_installer.compute_version_hash",
               return_value="HASH"):
        install, update, remove = mcp_sync._diff(
            desired={"foo"},
            installed={"foo": {"version_hash": "HASH", "healthy": True}},
            force=True,
        )
        assert update == {"foo"}


# --- Deferred-update backoff (Fix 2b: a swap blocked by an in-use old version
# is kept + deferred, not re-shipped/rebuilt every session) ------------------

def test_deferred_backoff_mark_check_clear():
    """mark → deferred; per-(machine,mcp) isolation; clear drops the backoff."""
    from services.mcp import mcp_sync

    mcp_sync._deferred_updates.clear()
    key = ("machine-1", "google-maps")
    assert mcp_sync._is_update_deferred(*key) is False
    mcp_sync._mark_update_deferred(*key)
    assert mcp_sync._is_update_deferred(*key) is True
    # Independent of other mcp / other machine.
    assert mcp_sync._is_update_deferred("machine-1", "other") is False
    assert mcp_sync._is_update_deferred("machine-2", "google-maps") is False
    # Clearing (as the ack loop does on a successful install) drops it.
    mcp_sync._deferred_updates.pop(key, None)
    assert mcp_sync._is_update_deferred(*key) is False


def test_deferred_backoff_expires_and_self_prunes():
    """A past deadline reads as not-deferred and is pruned from the store."""
    import time

    from services.mcp import mcp_sync

    mcp_sync._deferred_updates.clear()
    key = ("machine-1", "google-maps")
    mcp_sync._deferred_updates[key] = time.monotonic() - 1.0  # already expired
    assert mcp_sync._is_update_deferred(*key) is False
    assert key not in mcp_sync._deferred_updates  # self-pruned on read
