"""Tests for storage/files/file_tombstones_store.py — explicit delete tombstones.

Run individually (the conftest DB pool exhausts if test files run together):
    proxy/venv/bin/python -m pytest tests/storage/test_file_tombstones_store.py
"""

from storage.files import file_tombstones_store as ts

A = "agent-x"


def test_record_get_and_load():
    ts.record(A, "workspace/a.txt", 100.0, origin="dashboard")
    row = ts.get(A, "workspace/a.txt")
    assert row is not None and row["deleted_at_mtime"] == 100.0
    assert ts.load_for_agent(A) == {"workspace/a.txt": 100.0}


def test_record_upserts_refresh():
    ts.record(A, "workspace/a.txt", 100.0)
    ts.record(A, "workspace/a.txt", 200.0, origin="live")  # refresh
    assert ts.get(A, "workspace/a.txt")["deleted_at_mtime"] == 200.0
    assert len(ts.load_for_agent(A)) == 1


def test_drop():
    ts.record(A, "workspace/a.txt", 100.0)
    ts.drop(A, "workspace/a.txt")
    assert ts.get(A, "workspace/a.txt") is None
    assert ts.load_for_agent(A) == {}


def test_expired_excluded_and_reaped():
    ts.record(A, "live.txt", 100.0)
    ts.record(A, "old.txt", 100.0, ttl_days=-1)  # already expired
    # Expired tombstone is invisible to get/load (no longer authorizes a delete).
    assert ts.get(A, "old.txt") is None
    assert set(ts.load_for_agent(A)) == {"live.txt"}
    assert ts.delete_expired() == 1
    assert ts.delete_expired() == 0


def test_isolated_per_agent():
    ts.record(A, "p", 1.0)
    ts.record("agent-y", "p", 2.0)
    assert ts.load_for_agent(A) == {"p": 1.0}


def test_holds_are_kept_apart_from_tombstones():
    ts.record(A, "deleted.txt", 1.0, origin="dashboard")
    ts.record(A, "held.txt", 2.0, origin=ts.HOLD_ORIGIN)
    assert ts.load_for_agent(A) == {"deleted.txt": 1.0}
    assert ts.load_holds_for_agent(A) == {"held.txt": 2.0}
    # A later delete of the path is a real delete again.
    ts.record(A, "held.txt", 3.0, origin="dashboard")
    assert ts.load_holds_for_agent(A) == {}


class TestWindowsHashHoldMigration:
    """1.8.0's one-shot: the bug's idle-merge deletes of an agent a Windows
    machine syncs become holds; everything else stays a delete."""

    def _setup(self):
        import json

        from storage import pg
        with pg.get_conn() as conn:
            conn.execute("DELETE FROM platform_settings WHERE key = 'windows_hash_holds'")
            for mid, caps in (("m-win", {"os": "windows"}), ("m-lin", {"os": "linux"}),
                              ("m-odd", "not json"), ("m-new", {})):
                conn.execute(
                    "INSERT INTO remote_machines (id, name, registered_by, created_at, capabilities) "
                    "VALUES (%s, %s, 'admin', '2026-09-01', %s)",
                    (mid, mid, caps if isinstance(caps, str) else json.dumps(caps)))
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login) "
                "VALUES ('u-1', 'u1@x', 'U', 'member', '2026-09-01', '2026-09-01')")
            for mid, agent in (("m-win", "pa"), ("m-lin", "dev"), ("m-gone", "orphaned"),
                               ("m-odd", "odd"), ("m-new", "fresh")):
                conn.execute(
                    "INSERT INTO sync_state (machine_id, agent_slug, rel_path, base_hash, updated_at) "
                    "VALUES (%s, %s, 'workspace/keep.txt', 'sha256:k', '2026-10-02')", (mid, agent))
            conn.execute(
                "INSERT INTO user_remote_targets (user_sub, machine_id, agent_slug, added_at) "
                "VALUES ('u-1', 'm-win', 'targeted', '2026-09-01')")
            # A user's default machine names no agent: it ties none.
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login) "
                "VALUES ('u-2', 'u2@x', 'V', 'member', '2026-09-01', '2026-09-01')")
            conn.execute(
                "INSERT INTO user_remote_targets (user_sub, machine_id, agent_slug, added_at) "
                "VALUES ('u-2', 'm-win', '', '2026-09-01')")
            conn.execute(
                "INSERT INTO agents (slug, display_name, created_at, updated_at) "
                "VALUES ('placed', 'Placed', '2026-09-01', '2026-09-01')")
            conn.execute(
                "INSERT INTO agent_remote_targets (agent_slug, machine_id, added_by, created_at) "
                "VALUES ('placed', 'm-win', 'admin', '2026-09-01')")
        for agent in ("pa", "dev", "orphaned", "odd", "targeted", "fresh", "placed"):
            ts.record(agent, "workspace/x.txt", 5.0, origin="satellite-idle-delete")
        ts.record("pa", "workspace/by-hand.txt", 5.0, origin="dashboard")
        ts.record("pa", "workspace/older.txt", 5.0, origin="satellite-idle-delete")
        with pg.get_conn() as conn:
            conn.execute(
                "UPDATE file_tombstones SET deleted_at = '2026-09-20T10:00:00+00:00' "
                "WHERE rel_path = 'workspace/older.txt'")

    def _migrate(self):
        from storage import pg, schema
        with pg.get_conn() as conn:
            schema._hold_windows_hash_deletes(conn)

    def test_the_bug_window_deletes_of_windows_tied_agents_become_holds(self, temp_db):
        self._setup()
        self._migrate()
        for agent in ("pa", "orphaned", "odd", "targeted", "placed"):
            assert ts.load_holds_for_agent(agent) == {"workspace/x.txt": 5.0}, agent
            assert "workspace/x.txt" not in ts.load_for_agent(agent)
        # Linux only, and a machine that never connected (it never ran the bug).
        for agent in ("dev", "fresh"):
            assert ts.load_holds_for_agent(agent) == {}, agent
            assert ts.load_for_agent(agent) == {"workspace/x.txt": 5.0}, agent
        # A delete by hand and one from before the bug stay deletes.
        assert ts.load_for_agent("pa") == {"workspace/by-hand.txt": 5.0,
                                           "workspace/older.txt": 5.0}

    def test_it_runs_once(self, temp_db):
        self._setup()
        self._migrate()
        ts.record("dev", "workspace/later.txt", 6.0, origin="satellite-idle-delete")
        ts.record("pa", "workspace/later.txt", 6.0, origin="satellite-idle-delete")
        self._migrate()
        assert "workspace/later.txt" in ts.load_for_agent("pa")
        from storage import database as db
        assert db.get_platform_setting("windows_hash_holds") == "done"
