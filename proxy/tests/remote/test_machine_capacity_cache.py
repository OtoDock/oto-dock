"""``machine_at_capacity`` / ``concurrency_stats`` read the admin
``max_sessions`` override and the machine name from a per-connection cache
filled OFF the loop at register (and refreshed by ``note_max_sessions``) —
never from the DB on the event loop (storage/pg.py's rule)."""

import asyncio
import time

import pytest

from core.remote.satellite_connection import SatelliteConnectionManager


class _FakeWS:
    async def close(self, code=1000, reason=""):
        pass

    async def send_text(self, text):
        pass


@pytest.fixture
def stores(monkeypatch):
    from storage import remote_store
    rows = {"m1": {"id": "m1", "name": "box", "max_sessions": 2}}
    monkeypatch.setattr(remote_store, "update_machine_status", lambda *a, **k: None)
    monkeypatch.setattr(remote_store, "update_machine_capabilities", lambda *a: None)
    monkeypatch.setattr(remote_store, "get_remote_machine", rows.get)
    return rows


async def _wait_cache(conn, timeout=2.0):
    deadline = time.monotonic() + timeout
    while conn.max_sessions is None and time.monotonic() < deadline:  # noqa: ASYNC110  # the connection's capacity field: the test polls the real code's write
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_cache_filled_at_register_and_used_without_db(stores, loop_db_guard):
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {"recommended_max_sessions": 9})
    await _wait_cache(conn)
    assert conn.max_sessions == 2 and conn.name == "box"

    with loop_db_guard.active():
        conn.reported_sessions = 1
        assert cm.machine_at_capacity("m1") is False
        conn.reported_sessions = 2
        assert cm.machine_at_capacity("m1") is True     # admin override wins
        (row,) = cm.concurrency_stats()
        assert row["name"] == "box" and row["max_sessions"] == 2
    await cm.deregister("m1", expected=conn)
    assert await cm.drain_persists()


@pytest.mark.asyncio
async def test_note_max_sessions_refreshes_and_clears(stores, loop_db_guard):
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {"recommended_max_sessions": 3})
    await _wait_cache(conn)
    with loop_db_guard.active():
        cm.note_max_sessions("m1", 5)
        conn.reported_sessions = 4
        assert cm.machine_at_capacity("m1") is False
        cm.note_max_sessions("m1", None)          # override cleared → recommendation
        assert cm.machine_at_capacity("m1") is True
        cm.note_max_sessions("nope", 1)           # offline machine → no-op
    await cm.deregister("m1", expected=conn)
    assert await cm.drain_persists()


@pytest.mark.asyncio
async def test_fail_open_when_row_missing(stores):
    stores.clear()
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {})
    await asyncio.sleep(0.1)
    conn.reported_sessions = 99
    assert cm.machine_at_capacity("m1") is False   # no override, no recommendation
    await cm.deregister("m1", expected=conn)
    assert await cm.drain_persists()
