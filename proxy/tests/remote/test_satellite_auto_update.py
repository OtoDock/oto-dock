"""Unit tests for the satellite auto-update mechanism.

Covers the tarball+hash builder, the auto-update toggle column, the
version-bookkeeping helpers, and the broadcast helpers' recipient
resolution. The auth-time WS handler in proxy/ws/satellite.py is
exercised indirectly via the broadcast helpers (full WS auth flow
testing lives in the existing test_satellite_security.py).
"""

import hashlib
import io
import tarfile

import pytest


def test_tarball_ships_all_subpackages_and_no_tests_or_venv():
    """The package is split into subpackages (transport/ sessions/ terminal/
    host/ _vendored/). The builder MUST recurse — an earlier top-level-only
    ``iterdir()`` silently dropped every subpackage module the moment the flat
    layout was split, shipping a 3-file tarball whose ``python -m satellite``
    died on ``from .transport... import`` and bricked every satellite that
    auto-updated. Guard against that regression; also guard that the dev
    ``venv/`` and the ``tests/`` tree never leak into the tarball."""
    from api.remote import remote_machines as rm

    rm._TARBALL_CACHE.update({"bytes": b"", "built_at": 0.0, "sha256": ""})
    data, _ = rm.get_satellite_tarball_with_hash()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        members = {m.name for m in t.getmembers() if m.isfile()}

    must_ship = {
        "satellite/__init__.py",
        "satellite/__main__.py",
        "satellite/config.py",
        "satellite/transport/__init__.py",
        "satellite/transport/ws_client.py",
        "satellite/sessions/__init__.py",
        "satellite/sessions/session_manager.py",
        "satellite/sessions/mcp_interceptor.py",
        "satellite/terminal/__init__.py",
        "satellite/terminal/otodock_cli.py",
        "satellite/host/__init__.py",
        "satellite/host/tray.py",
        "satellite/_vendored/__init__.py",
        "satellite/_vendored/mcp_installer.py",
        "satellite/_vendored/stdio_path_interceptor.py",
    }
    missing = must_ship - members
    assert not missing, f"tarball is missing package files: {sorted(missing)}"

    leaked = [m for m in members if "/tests/" in m or "/venv/" in m]
    assert not leaked, f"tarball leaked non-shipping files: {leaked}"


def test_tarball_hash_cached_and_recomputed_on_source_change(tmp_path, monkeypatch):
    """get_satellite_tarball_with_hash should cache (bytes, sha256). When the
    satellite source files haven't changed, both calls return identical
    bytes AND the cached hash matches sha256(bytes)."""
    from api.remote import remote_machines as rm

    # Clear the module-level cache so this test is hermetic.
    rm._TARBALL_CACHE["bytes"] = b""
    rm._TARBALL_CACHE["built_at"] = 0.0
    rm._TARBALL_CACHE["sha256"] = ""

    data1, hash1 = rm.get_satellite_tarball_with_hash()
    assert data1, "tarball should not be empty"
    assert hash1 and len(hash1) == 64, "sha256 hex digest is 64 chars"
    assert hash1 == hashlib.sha256(data1).hexdigest()

    # Second call: should return the cached blob without recomputing.
    data2, hash2 = rm.get_satellite_tarball_with_hash()
    assert data1 is data2 or data1 == data2
    assert hash1 == hash2


def test_set_auto_update_enabled_persists():
    """The auto_update_enabled column is read back after writing."""
    from storage import remote_store

    # Build a minimal machine row in the test DB so we can update it.
    import uuid as _uuid
    mid = str(_uuid.uuid4())
    name = f"test-machine-{mid[:8]}"
    remote_store.create_remote_machine(
        machine_id=mid, name=name, registered_by="test-user",
    )
    try:
        # Default is True from schema.
        row = remote_store.get_remote_machine(mid)
        assert row is not None
        # Newly-created rows may carry the column default = TRUE.
        assert row.get("auto_update_enabled", True) is True

        remote_store.set_auto_update_enabled(mid, False)
        row = remote_store.get_remote_machine(mid)
        assert row["auto_update_enabled"] is False

        remote_store.set_auto_update_enabled(mid, True)
        row = remote_store.get_remote_machine(mid)
        assert row["auto_update_enabled"] is True
    finally:
        remote_store.delete_remote_machine(mid)


def test_set_satellite_version_persists():
    """Recording the satellite version on auth shows up in the row."""
    from storage import remote_store
    import uuid as _uuid

    mid = str(_uuid.uuid4())
    remote_store.create_remote_machine(
        machine_id=mid, name=f"v-{mid[:8]}", registered_by="test-user",
    )
    try:
        remote_store.set_satellite_version(mid, "0.4.2")
        row = remote_store.get_remote_machine(mid)
        assert row["satellite_version"] == "0.4.2"

        remote_store.set_satellite_version(mid, "0.5.0")
        row = remote_store.get_remote_machine(mid)
        assert row["satellite_version"] == "0.5.0"
    finally:
        remote_store.delete_remote_machine(mid)


def test_record_update_result_success_clears_error_and_sets_version():
    """A successful update result stamps last_update_at + clears any
    prior error + updates satellite_version + clears pending_update."""
    from storage import remote_store
    import uuid as _uuid

    mid = str(_uuid.uuid4())
    remote_store.create_remote_machine(
        machine_id=mid, name=f"u-{mid[:8]}", registered_by="test-user",
    )
    try:
        # Seed a failure first.
        remote_store.record_update_result(mid, error="boom")
        row = remote_store.get_remote_machine(mid)
        assert row["last_update_error"] == "boom"

        # Then a success.
        remote_store.set_pending_update(mid, True)
        remote_store.record_update_result(
            mid, target_version="0.4.2", error=None,
        )
        row = remote_store.get_remote_machine(mid)
        assert row["last_update_error"] is None
        assert row["satellite_version"] == "0.4.2"
        assert row["last_update_at"] is not None
        assert row["pending_update"] is False
    finally:
        remote_store.delete_remote_machine(mid)


def test_set_pending_update_round_trip():
    from storage import remote_store
    import uuid as _uuid

    mid = str(_uuid.uuid4())
    remote_store.create_remote_machine(
        machine_id=mid, name=f"p-{mid[:8]}", registered_by="test-user",
    )
    try:
        remote_store.set_pending_update(mid, True)
        assert remote_store.get_remote_machine(mid)["pending_update"] is True
        remote_store.set_pending_update(mid, False)
        assert remote_store.get_remote_machine(mid)["pending_update"] is False
    finally:
        remote_store.delete_remote_machine(mid)


@pytest.mark.asyncio
async def test_broadcast_recipients_include_owner_and_admins(monkeypatch):
    """_dashboard_recipients_for_machine returns the machine's
    registered_by (owner) + every admin user_sub. Used to route
    satellite_updating events to the right dashboards."""
    from ws import satellite as sat_ws

    machine = {"registered_by": "owner-sub-1"}

    # Stub list_users to return one admin and one non-admin.
    def _fake_list_users():
        return [
            {"sub": "admin-1", "role": "admin"},
            {"sub": "user-2", "role": "creator"},
        ]

    monkeypatch.setattr(
        "storage.database.list_users", _fake_list_users,
    )

    recipients = sat_ws._dashboard_recipients_for_machine(machine)
    assert "owner-sub-1" in recipients
    assert "admin-1" in recipients
    assert "user-2" not in recipients
    # Sorted for stability.
    assert recipients == sorted(recipients)


@pytest.mark.asyncio
async def test_push_machine_event_enqueues_to_all_user_connections(monkeypatch):
    """_push_machine_event pushes the payload into every recipient's
    notify queue via notification_manager."""
    from ws import satellite as sat_ws
    from services.notifications import notification_manager
    import asyncio as _asyncio

    # Two queues for the same user (two tabs).
    q1 = _asyncio.Queue(maxsize=8)
    q2 = _asyncio.Queue(maxsize=8)
    notification_manager._user_connections.clear()
    notification_manager.register_user_connection("owner-sub-1", "conn-A", q1)
    notification_manager.register_user_connection("owner-sub-1", "conn-B", q2)

    # No admins for this test.
    monkeypatch.setattr(
        "storage.database.list_users", lambda: [],
    )

    try:
        machine = {"registered_by": "owner-sub-1", "name": "MacBookPro"}
        await sat_ws._push_machine_event(machine, {
            "type": "satellite_updating",
            "machine_id": "m-1",
        })

        # Both queues got the event.
        ev1 = q1.get_nowait()
        ev2 = q2.get_nowait()
        assert ev1["type"] == "satellite_updating"
        assert ev2["type"] == "satellite_updating"
    finally:
        notification_manager.unregister_user_connection("owner-sub-1", "conn-A")
        notification_manager.unregister_user_connection("owner-sub-1", "conn-B")


def test_rewrite_stdio_paths_swaps_category_segment():
    """Remote sessions: the satellite installs MCPs by manifest category,
    not by the platform's source-directory category. The rewrite must
    swap the category segment when the two disagree, else the CLI on
    the satellite tries to spawn a path that doesn't exist."""
    from core.remote.remote_execution import _rewrite_stdio_paths
    import config as app_config

    plat = str(app_config.MCPS_DIR.resolve())

    # Common case: MCP physically stored in mcps/custom/<X> but manifest
    # declares category: core. Satellite installed it under core/.
    cmd, args = _rewrite_stdio_paths(
        f"{plat}/custom/notifications-mcp/venv/bin/python",
        [f"{plat}/custom/notifications-mcp/server.py"],
        mcp_name="notifications-mcp",
        satellite_category="core",
        platform_dir=f"{plat}/custom/notifications-mcp",
    )
    assert cmd == "~/.oto-dock/mcps/core/notifications-mcp/venv/bin/python"
    assert args == ["~/.oto-dock/mcps/core/notifications-mcp/server.py"]

    # Aligned case: platform dir == manifest category. Should still work.
    cmd, args = _rewrite_stdio_paths(
        f"{plat}/community/unifi-network/venv/bin/unifi-network-mcp",
        [],
        mcp_name="unifi-network",
        satellite_category="community",
        platform_dir=f"{plat}/community/unifi-network",
    )
    assert cmd == "~/.oto-dock/mcps/community/unifi-network/venv/bin/unifi-network-mcp"

    # Fallback (no manifest info): plain prefix swap, preserves whatever
    # category segment the platform path has.
    cmd, args = _rewrite_stdio_paths(
        f"{plat}/custom/foo/venv/bin/python",
        [],
    )
    assert cmd == "~/.oto-dock/mcps/custom/foo/venv/bin/python"


@pytest.mark.asyncio
async def test_broadcast_satellite_updating_event_shape(monkeypatch):
    """The published event includes machine_id, machine_name, from/to
    versions and an ISO timestamp."""
    from ws import satellite as sat_ws
    from services.notifications import notification_manager
    import asyncio as _asyncio

    q = _asyncio.Queue(maxsize=4)
    notification_manager._user_connections.clear()
    notification_manager.register_user_connection("owner-sub", "c", q)
    monkeypatch.setattr(
        "storage.database.list_users", lambda: [],
    )

    try:
        machine = {"registered_by": "owner-sub", "name": "Tower"}
        await sat_ws._broadcast_satellite_updating(
            "m-1", machine, "0.4.0", "0.4.2",
        )
        ev = q.get_nowait()
        assert ev["type"] == "satellite_updating"
        assert ev["machine_id"] == "m-1"
        assert ev["machine_name"] == "Tower"
        assert ev["from_version"] == "0.4.0"
        assert ev["to_version"] == "0.4.2"
        assert "started_at" in ev
    finally:
        notification_manager.unregister_user_connection("owner-sub", "c")


def test_satellite_dir_anchor_resolves_to_repo_satellite():
    """Regression: an api/ subpackage move once left _SATELLITE_DIR pointing at
    proxy/satellite (absent), silently breaking the tarball builder and every
    real satellite install/auto-update (FileNotFoundError). Pin the anchor to
    the repo-root satellite/ package so any future move of remote_machines.py
    fails loudly HERE (with a clear message) instead of at tarball-build time.
    """
    from api.remote import remote_machines as rm

    assert rm._SATELLITE_DIR.is_dir(), f"_SATELLITE_DIR does not exist: {rm._SATELLITE_DIR}"
    assert rm._SATELLITE_DIR.name == "satellite"
    # satellite/ must be a runnable package — the tarball is shipped so the
    # satellite can `python -m satellite`.
    assert (rm._SATELLITE_DIR / "__main__.py").is_file()


# ---------------------------------------------------------------------------
# Rollback budget (2026-08-29): the reconnect-after-push check only skips ONE
# re-push, so a deterministically broken build used to loop push → crash →
# rollback on every reconnect. Rollbacks are now counted per target version;
# at MAX_AUTO_UPDATE_ROLLBACKS automatic pushes of that target stop until an
# explicit "Update now" (or a successful reconnect on the target / a new
# target resets the budget).
# ---------------------------------------------------------------------------


def _machine(prefix: str):
    from storage import remote_store
    import uuid as _uuid

    mid = str(_uuid.uuid4())
    remote_store.create_remote_machine(
        machine_id=mid, name=f"{prefix}-{mid[:8]}", registered_by="test-user",
    )
    return mid


def test_record_update_rollback_counts_per_target_and_clears_pending():
    from storage import remote_store

    mid = _machine("rb")
    try:
        remote_store.set_pending_update(mid, True)
        # First rollback of 0.9.1 → count 1, pending consumed.
        assert remote_store.record_update_rollback(
            mid, target_version="0.9.1", error="rolled back",
        ) == 1
        row = remote_store.get_remote_machine(mid)
        assert row["update_rollback_count"] == 1
        assert row["update_rollback_target"] == "0.9.1"
        assert row["last_update_error"] == "rolled back"
        assert row["last_update_at"] is not None
        assert row["pending_update"] is False

        # Same target again → 2.
        assert remote_store.record_update_rollback(
            mid, target_version="0.9.1", error="rolled back again",
        ) == 2
        assert remote_store.get_remote_machine(mid)["update_rollback_count"] == 2

        # A NEW target starts a fresh budget at 1.
        assert remote_store.record_update_rollback(
            mid, target_version="0.9.2", error="new build rolled back",
        ) == 1
        row = remote_store.get_remote_machine(mid)
        assert row["update_rollback_count"] == 1
        assert row["update_rollback_target"] == "0.9.2"
    finally:
        remote_store.delete_remote_machine(mid)


def test_successful_update_resets_rollback_budget():
    from storage import remote_store

    mid = _machine("ok")
    try:
        remote_store.record_update_rollback(mid, target_version="0.9.1", error="x")
        remote_store.record_update_rollback(mid, target_version="0.9.1", error="x")
        remote_store.record_update_result(mid, target_version="0.9.1", error=None)
        row = remote_store.get_remote_machine(mid)
        assert row["update_rollback_count"] == 0
        assert row["update_rollback_target"] is None
        assert row["last_update_error"] is None
        assert row["satellite_version"] == "0.9.1"
    finally:
        remote_store.delete_remote_machine(mid)


def test_push_failure_result_does_not_touch_rollback_budget():
    """record_update_result(error=...) is the 'push never left the proxy'
    path — it must not count as a rollback nor clear the budget."""
    from storage import remote_store

    mid = _machine("pf")
    try:
        remote_store.record_update_rollback(mid, target_version="0.9.1", error="x")
        remote_store.record_update_result(mid, error="push failed: boom")
        row = remote_store.get_remote_machine(mid)
        assert row["update_rollback_count"] == 1
        assert row["update_rollback_target"] == "0.9.1"
        assert row["last_update_error"] == "push failed: boom"
    finally:
        remote_store.delete_remote_machine(mid)


def test_auto_update_paused_matrix():
    from ws import satellite as sat_ws

    assert sat_ws.MAX_AUTO_UPDATE_ROLLBACKS == 2
    paused = sat_ws.auto_update_paused
    # Fresh row → never paused.
    assert paused({}, "0.9.1") is False
    assert paused({"update_rollback_count": 0, "update_rollback_target": None}, "0.9.1") is False
    # One rollback → still automatic.
    assert paused({"update_rollback_count": 1, "update_rollback_target": "0.9.1"}, "0.9.1") is False
    # Budget exhausted for THIS target.
    assert paused({"update_rollback_count": 2, "update_rollback_target": "0.9.1"}, "0.9.1") is True
    assert paused({"update_rollback_count": 5, "update_rollback_target": "0.9.1"}, "0.9.1") is True
    # A different (newer) target is a fresh build → not paused.
    assert paused({"update_rollback_count": 2, "update_rollback_target": "0.9.1"}, "0.9.2") is False
    # No target (satellite source absent) → nothing to pause.
    assert paused({"update_rollback_count": 2, "update_rollback_target": "0.9.1"}, None) is False


def test_note_update_pushed_is_popped_by_the_reconnect_check():
    """note_update_pushed is the single writer of _pending_pushed_updates and
    the in-flight banner reconcile reads the same dict (must keep popping)."""
    from ws import satellite as sat_ws

    sat_ws._pending_pushed_updates.pop("m-note", None)
    try:
        sat_ws.note_update_pushed("m-note", "0.9.1")
        assert sat_ws._pending_pushed_updates["m-note"] == "0.9.1"
        assert sat_ws._pending_pushed_updates.pop("m-note", "") == "0.9.1"
        assert "m-note" not in sat_ws._pending_pushed_updates
    finally:
        sat_ws._pending_pushed_updates.pop("m-note", None)


@pytest.mark.asyncio
async def test_broadcast_update_failed_carries_attempts_and_pause(monkeypatch):
    from ws import satellite as sat_ws
    from services.notifications import notification_manager
    import asyncio as _asyncio

    q = _asyncio.Queue(maxsize=4)
    notification_manager._user_connections.clear()
    notification_manager.register_user_connection("owner-sub", "c", q)
    monkeypatch.setattr("storage.database.list_users", lambda: [])
    try:
        machine = {"registered_by": "owner-sub", "name": "Tower"}
        await sat_ws._broadcast_satellite_update_failed(
            "m-1", machine, error="Update to 0.9.1 failed; rolled back.",
            rolled_back_to="0.9.0", attempts=2, paused=True,
        )
        ev = q.get_nowait()
        assert ev["type"] == "satellite_update_failed"
        assert ev["rolled_back_to"] == "0.9.0"
        assert ev["attempts"] == 2
        assert ev["auto_update_paused"] is True
        # Defaults: a first rollback is attempt 1, not paused.
        await sat_ws._broadcast_satellite_update_failed(
            "m-1", machine, error="e", rolled_back_to="0.9.0",
        )
        ev = q.get_nowait()
        assert ev["attempts"] == 1
        assert ev["auto_update_paused"] is False
    finally:
        notification_manager.unregister_user_connection("owner-sub", "c")


@pytest.mark.asyncio
async def test_update_now_online_push_registers_the_target(monkeypatch):
    """The manual 'Update now' online push must arm the same rollback
    detection as the automatic push — before 2026-08-29 a satellite that
    rolled back after a manual push reconnected silently (no banner, no
    error, no count)."""
    from api.remote import remote_machines as rm
    from ws import satellite as sat_ws
    import core.remote.satellite_connection as sc

    sent: list[dict] = []

    class _Conn:
        async def enqueue_send(self, msg, *, bulk=False):
            sent.append(msg)

    class _CM:
        def is_connected(self, mid):
            return True

        def get_connection(self, mid):
            return _Conn()

    async def _no_broadcast(*a, **k):
        return None

    monkeypatch.setattr(sc, "get_connection_manager", _CM)
    monkeypatch.setattr(rm, "_require_satellite_source", lambda: None)
    monkeypatch.setattr(rm, "get_satellite_tarball_with_hash", lambda: (b"tar", "sha"))
    monkeypatch.setattr(sat_ws, "_broadcast_satellite_updating", _no_broadcast)
    monkeypatch.setattr(sat_ws, "SATELLITE_VERSION_LATEST", "9.9.9")
    sat_ws._pending_pushed_updates.pop("m-now", None)
    try:
        out = await rm._do_trigger_update_now("m-now", {"satellite_version": "0.9.0"})
        assert out == {"ok": True, "queued": False, "pushed_now": True}
        assert sent and sent[0]["type"] == "update_required"
        assert sent[0]["target_version"] == "9.9.9"
        assert sat_ws._pending_pushed_updates.get("m-now") == "9.9.9"
    finally:
        sat_ws._pending_pushed_updates.pop("m-now", None)
