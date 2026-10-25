"""Per-machine off-loop status persister (core/remote/satellite_connection.py).

The 2026-09-03 stall: every satellite heartbeat committed ``last_seen`` to
Postgres ON the event loop; a slow disk turned each commit into a proxy-wide
freeze. Now register / heartbeat / deregister / the heartbeat monitor hand
their writes to a per-machine flusher that runs them on the dedicated DB
executor, newest-wins per lane (ordering) and coalesced (a heartbeat burst is
one write). These tests lock:

  1. register → deregister burst: the final row is ``disconnected`` WITH the
     exact last contact;
  2. a heartbeat burst is ≤ 2 status writes;
  3. monitor ``disconnected`` → next heartbeat persists ``online`` at once;
  4. a failing store never raises into the loop and is retried later;
  5. duplicate reconnect: the stale deregister never enqueues ``disconnected``;
  6. caps lane newest-wins (register snapshot vs cli_status re-report);
  7. POOL EXHAUSTION: with every DB-executor worker parked on a stalled commit
     the loop stays responsive AND a loop-side ``get_conn()`` still gets a
     connection (the executor is sized below the pool);
  8. no store call ever runs on the loop thread (guard).
"""

import asyncio
import threading
import time

import pytest

from core.remote import satellite_connection as sc
from core.remote.satellite_connection import SatelliteConnectionManager


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self, code=1000, reason=""):
        self.closed = True

    async def send_text(self, text):
        pass


class _Recorder:
    """Records status writes with the thread they ran on."""

    def __init__(self):
        self.status: list[tuple] = []
        self.caps: list[tuple] = []
        self.threads: set[int] = set()

    def update_machine_status(self, machine_id, status, *, last_seen=None):
        self.threads.add(threading.get_ident())
        self.status.append((machine_id, status, last_seen))

    def update_machine_capabilities(self, machine_id, caps):
        self.threads.add(threading.get_ident())
        self.caps.append((machine_id, caps))


@pytest.fixture
def rec(monkeypatch):
    from storage import remote_store
    r = _Recorder()
    monkeypatch.setattr(remote_store, "update_machine_status", r.update_machine_status)
    monkeypatch.setattr(remote_store, "update_machine_capabilities", r.update_machine_capabilities)
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: None)
    return r


@pytest.mark.asyncio
async def test_register_then_deregister_lands_final_disconnected_with_last_seen(rec, loop_db_guard):
    cm = SatelliteConnectionManager()
    with loop_db_guard.active():
        conn = await cm.register("m1", _FakeWS(), {})
        await cm.deregister("m1", expected=conn)
    assert await cm.drain_persists()
    assert rec.status, "nothing persisted"
    mid, status, last_seen = rec.status[-1]
    assert (mid, status) == ("m1", "disconnected")
    assert last_seen == conn.last_seen_iso and last_seen
    assert threading.get_ident() not in rec.threads


@pytest.mark.asyncio
async def test_heartbeat_burst_coalesces(rec, loop_db_guard):
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {})
    assert await cm.drain_persists()
    rec.status.clear()
    with loop_db_guard.active():
        for _ in range(50):
            await cm.handle_message("m1", {"type": "heartbeat"})
    assert await cm.drain_persists()
    # Already online + persisted at register → the burst adds nothing
    # (≤ 2 tolerates one in-flight write racing the register's own).
    assert len(rec.status) <= 2
    assert conn.persisted_status == "online"


@pytest.mark.asyncio
async def test_heartbeat_persists_after_interval(rec, monkeypatch):
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {})
    assert await cm.drain_persists()
    rec.status.clear()
    # Pretend the last last_seen write is older than the cadence.
    conn.last_seen_persisted_at = time.monotonic() - sc.HEARTBEAT_PERSIST_INTERVAL_S - 1
    await cm.handle_message("m1", {"type": "heartbeat"})
    assert await cm.drain_persists()
    assert [s[1] for s in rec.status] == ["online"]
    assert rec.status[0][2] == conn.last_seen_iso


@pytest.mark.asyncio
async def test_monitor_disconnected_then_heartbeat_reonlines_immediately(rec, monkeypatch):
    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {})
    assert await cm.drain_persists()
    rec.status.clear()
    # Simulate the monitor's 90 s branch.
    conn.last_heartbeat = time.monotonic() - 120
    cm._persist_status(conn, "disconnected", conn.last_seen_iso)
    assert await cm.drain_persists()
    assert rec.status[-1][1:] == ("disconnected", conn.last_seen_iso)
    # The next heartbeat is a transition → persisted at once, not after 60 s.
    await cm.handle_message("m1", {"type": "heartbeat"})
    assert await cm.drain_persists()
    assert rec.status[-1][1] == "online"


@pytest.mark.asyncio
async def test_store_failure_never_raises_and_retries_on_next_request(monkeypatch, caplog):
    from storage import remote_store
    calls: list[tuple] = []
    fail = {"on": True}

    def _status(machine_id, status, *, last_seen=None):
        calls.append((machine_id, status))
        if fail["on"]:
            raise RuntimeError("db down")

    monkeypatch.setattr(remote_store, "update_machine_status", _status)
    monkeypatch.setattr(remote_store, "update_machine_capabilities", lambda *a: None)
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: None)

    cm = SatelliteConnectionManager()
    conn = await cm.register("m1", _FakeWS(), {})
    assert await cm.drain_persists()  # the failing write completed (with a warning)
    assert calls == [("m1", "online")]
    assert any("persisting status failed" in r.getMessage() for r in caplog.records)
    # The value stays parked; the next request flushes it again.
    fail["on"] = False
    cm._persist_status(conn, "online", conn.last_seen_iso)
    assert await cm.drain_persists()
    assert calls[-1] == ("m1", "online") and len(calls) == 2


@pytest.mark.asyncio
async def test_duplicate_reconnect_stale_deregister_writes_nothing(rec):
    cm = SatelliteConnectionManager()
    a = await cm.register("m1", _FakeWS(), {})
    b = await cm.register("m1", _FakeWS(), {})  # replaces a
    assert await cm.drain_persists()
    rec.status.clear()
    await cm.deregister("m1", expected=a)  # stale → no-op
    assert await cm.drain_persists()
    assert rec.status == []
    assert cm.get_connection("m1") is b
    await cm.deregister("m1", expected=b)
    assert await cm.drain_persists()
    assert rec.status[-1][1] == "disconnected"


@pytest.mark.asyncio
async def test_caps_lane_newest_wins(rec):
    cm = SatelliteConnectionManager()
    await cm.register("m1", _FakeWS(), {"gen": "register"})
    cm.persist_lane("m1", "caps", {"gen": "re-report"})
    assert await cm.drain_persists()
    assert rec.caps[-1][1]["gen"] == "re-report"


@pytest.mark.asyncio
async def test_pool_exhaustion_keeps_loop_and_reserve_free(monkeypatch):
    """Every DB-executor worker parks on a 'stalled commit' that HOLDS a real
    pool connection. The loop must keep ticking and a loop-side get_conn()
    must still succeed — the executor is sized below the pool on purpose."""
    from storage import pg, remote_store

    release = threading.Event()
    started = threading.Semaphore(0)

    def _stalled(machine_id, status, *, last_seen=None):
        with pg.get_conn() as conn:
            conn.execute("SELECT 1")
            started.release()
            release.wait(timeout=20)

    monkeypatch.setattr(remote_store, "update_machine_status", _stalled)
    monkeypatch.setattr(remote_store, "update_machine_capabilities", lambda *a: None)
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda mid: None)

    cm = SatelliteConnectionManager()
    workers = pg.db_executor_workers()
    try:
        for i in range(workers + 6):
            cm.persist_lane(f"m{i}", "status", ("online", None))
        # Wait until every worker is parked (each holds one connection) —
        # yielding to the loop so the flusher tasks actually get scheduled.
        deadline = time.monotonic() + 10
        parked = 0
        while parked < workers and time.monotonic() < deadline:
            if started.acquire(blocking=False):
                parked += 1
            else:
                await asyncio.sleep(0.05)
        assert parked == workers

        # 1. The loop is responsive.
        t0 = time.monotonic()
        await asyncio.sleep(0)
        assert time.monotonic() - t0 < 0.05

        # 2. A residual loop-side caller still gets a connection quickly.
        t0 = time.monotonic()
        with pg.get_conn() as conn:
            conn.execute("SELECT 1")
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
        assert await cm.drain_persists(timeout=15)
