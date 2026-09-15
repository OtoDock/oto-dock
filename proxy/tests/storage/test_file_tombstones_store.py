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
