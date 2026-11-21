"""What a connection holds for its machine is bounded in bytes: a reader
that stays behind is dropped, a burst a healthy machine drains is not, and a
tunneled response waits for room instead of filling the queue."""

import asyncio
import time
from types import SimpleNamespace

import pytest

import config
from core.remote import satellite_connection as sc, satellite_http_tunnel as tunnel
from tests._timing import bound
from tests.remote import link_fakes as lf


@pytest.fixture
def cm(monkeypatch):
    manager = sc.SatelliteConnectionManager()
    monkeypatch.setattr(sc, "_connection_manager", manager)
    monkeypatch.setattr(sc, "_persist_write", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_kick_reconnect_steps", lambda machine_id: None)
    return manager


async def _settle(n: int = 40) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def test_a_frames_cost_counts_its_strings_at_any_depth():
    frame = {"type": "sync_mcps", "mcps": [{"name": "a", "files": {"tar_b64": "x" * 1000}}]}
    assert sc._frame_cost(frame) == len("sync_mcps") + 1 + 1000
    assert sc._frame_cost({"n": 5, "ok": True, "items": [1, 2, None]}) == 0


@pytest.mark.asyncio
async def test_a_burst_a_healthy_machine_drains_drops_nothing_and_nobody(cm):
    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    for n in range(12_000):          # a scrub's deletes, queued with no yield
        conn.enqueue_send_nowait({"type": "file_push", "action": "delete", "agent_slug": "a",
                                  "path": f"f{n}"})
    assert conn.queued_frames == 12_000 and conn.over_since is not None
    for _ in range(400):
        await asyncio.sleep(0.01)
        if len(m.frames) == 12_000:
            break
    assert [f["path"] for f in m.frames] == [f"f{n}" for n in range(12_000)]
    assert cm.is_connected("m1") and conn.over_since is None and conn.queued_bytes == 0


@pytest.mark.asyncio
async def test_a_reader_behind_for_the_grace_is_dropped_and_its_waiters_fail(cm, monkeypatch):
    monkeypatch.setattr(config, "SAT_SEND_QUEUE_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(sc, "_SLOW_READER_GRACE_S", 0.2)
    m = lf.FakeMachine(cm, "m1", mode=lf.FROZEN)
    conn = await m.connect()
    waiting = asyncio.ensure_future(cm.send_command("m1", {"type": "check_session_process"}, timeout=30))
    chunk = "x" * (300 * 1024)
    for _ in range(6):
        conn.enqueue_send_nowait({"type": "http_response_chunk", "body_b64": chunk})
    await _settle()
    assert cm.is_connected("m1") and conn.over_since is not None      # over, not for long yet
    await asyncio.sleep(0.25)
    conn.enqueue_send_nowait({"type": "pong"})
    await _settle()
    assert not cm.is_connected("m1")
    assert m.ws.closed == (4008, "slow reader")
    with pytest.raises(RuntimeError, match="disconnected"):
        await waiting
    held = conn.queued_frames
    conn.enqueue_send_nowait({"type": "pong"})                          # nothing more is queued
    assert conn.queued_frames == held


@pytest.mark.asyncio
async def test_four_times_the_bound_drops_the_reader_at_once(cm, monkeypatch):
    monkeypatch.setattr(config, "SAT_SEND_QUEUE_MAX_BYTES", 1024 * 1024)
    m = lf.FakeMachine(cm, "m1", mode=lf.FROZEN)
    conn = await m.connect()
    chunk = "x" * (600 * 1024)
    for _ in range(9):
        conn.enqueue_send_nowait({"type": "http_response_chunk", "body_b64": chunk})
    await _settle()
    assert not cm.is_connected("m1")


@pytest.mark.asyncio
async def test_a_slow_machine_that_keeps_draining_is_kept(cm, monkeypatch):
    """A reader that takes its bytes at a rate is over the bound for a
    moment and under it again: it is never dropped for being slow alone."""
    monkeypatch.setattr(config, "SAT_SEND_QUEUE_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(sc, "_SLOW_READER_GRACE_S", 0.3)
    m = lf.FakeMachine(cm, "m1", mode=lf.SLOW, rate=40 * 1024 * 1024)
    conn = await m.connect()
    chunk = "x" * (300 * 1024)
    for _ in range(8):
        conn.enqueue_send_nowait({"type": "http_response_chunk", "body_b64": chunk})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if conn.queued_frames == 0:
            break
    conn.enqueue_send_nowait({"type": "pong"})
    await _settle()
    assert cm.is_connected("m1") and conn.over_since is None


# -- the tunnel's wait for room ----------------------------------------------


def _stream() -> tunnel._HttpStream:
    return tunnel._HttpStream(stream_id="s1", machine_id="m1")


@pytest.mark.asyncio
async def test_a_tunneled_body_waits_for_room_and_the_last_frame_never_does(cm):
    m = lf.FakeMachine(cm, "m1", mode=lf.FROZEN)
    conn = await m.connect()
    # The writer holds the first frame in its send, the second waits.
    for _ in range(2):
        conn.enqueue_send_nowait({"type": "http_response_chunk", "body_b64": "x" * sc.ROOM_BYTES})
    await _settle()
    assert not conn.room.is_set()
    dispatcher, stream = tunnel.SatelliteHttpTunnelDispatcher(), _stream()
    before = conn.queued_frames
    body = asyncio.ensure_future(dispatcher._send_chunk(cm, "m1", "s1", b"data", body_eof=False,
                                                        stream=stream))
    await _settle()
    assert not body.done() and conn.queued_frames == before
    stamped = stream.last_activity
    await dispatcher._send_chunk(cm, "m1", "s1", b"", body_eof=True)        # the last frame
    assert conn.queued_frames == before + 1
    # The stream's cancel ends the wait and the chunk is not queued.
    stream.cancel_event.set()
    conn.room.set()
    await asyncio.wait_for(body, 1)
    assert conn.queued_frames == before + 1
    assert stream.last_activity >= stamped


@pytest.mark.asyncio
async def test_waiting_producers_queue_one_at_a_time_when_room_opens(cm):
    conn = SimpleNamespace(room=asyncio.Event(), queued=0)

    async def enqueue_send(msg, bulk=False):
        conn.queued += 1
        conn.room.clear()           # the first frame fills the room again

    conn.enqueue_send = enqueue_send
    manager = SimpleNamespace(get_connection=lambda machine_id: conn)
    dispatcher = tunnel.SatelliteHttpTunnelDispatcher()
    waiters = [asyncio.ensure_future(dispatcher._send_chunk(
        manager, "m1", f"s{i}", b"data", body_eof=False, stream=_stream())) for i in range(5)]
    await _settle()
    assert conn.queued == 0
    conn.room.set()
    await _settle()
    assert conn.queued == 1
    for task in waiters:
        task.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_producer_waiting_for_room_is_released_when_its_machine_drops(cm):
    m = lf.FakeMachine(cm, "m1", mode=lf.FROZEN)
    conn = await m.connect()
    for _ in range(2):
        conn.enqueue_send_nowait({"type": "http_response_chunk", "body_b64": "x" * sc.ROOM_BYTES})
    await _settle()
    dispatcher = tunnel.SatelliteHttpTunnelDispatcher()
    body = asyncio.ensure_future(dispatcher._send_chunk(cm, "m1", "s1", b"data", body_eof=False,
                                                        stream=_stream()))
    await _settle()
    started = time.monotonic()
    await m.drop()
    await asyncio.wait_for(body, 1)
    assert time.monotonic() - started < bound(0.5)


@pytest.mark.asyncio
async def test_a_scrub_past_the_hard_bound_does_not_cut_a_healthy_machine(cm):
    """A scrub's deletes are queued in a loop: it lets the writer run and waits
    while the queue is long, so what a healthy machine reads never reaches a
    bound. Each frame still goes out, in order."""
    from core.remote.remote_workspace_apply import RemoteWorkspaceApplyMixin

    class _Layer(RemoteWorkspaceApplyMixin):
        _cm = cm

    m = lf.FakeMachine(cm, "m1")
    conn = await m.connect()
    await _settle()
    count = 45_000
    run = SimpleNamespace(machine_id="m1", agent_slug="a", n={"scrub": 0},
                          plan=SimpleNamespace(to_scrub=[f"users/other/f{n}" for n in range(count)]))
    await _Layer()._apply_scrubs(run)
    assert run.n["scrub"] == count and conn.slow_dropped is False and cm.get_connection("m1") is conn
    for _ in range(1000):
        if len(m.frames) >= count:
            break
        await asyncio.sleep(0.01)
    assert [f["path"] for f in m.frames if f.get("type") == "file_push"] == run.plan.to_scrub


@pytest.mark.asyncio
async def test_a_scrub_toward_a_machine_that_reads_nothing_ends_when_it_is_cut(cm):
    from core.remote.remote_workspace_apply import RemoteWorkspaceApplyMixin

    class _Layer(RemoteWorkspaceApplyMixin):
        _cm = cm

    m = lf.FakeMachine(cm, "m1", mode=lf.FROZEN)
    await m.connect()
    await _settle()
    run = SimpleNamespace(machine_id="m1", agent_slug="a", n={"scrub": 0},
                          plan=SimpleNamespace(to_scrub=[f"users/other/f{n}" for n in range(16_000)]))
    scrub = asyncio.ensure_future(_Layer()._apply_scrubs(run))
    await asyncio.sleep(0.2)
    assert not scrub.done()
    await m.drop()
    await asyncio.wait_for(scrub, bound(5.0))
