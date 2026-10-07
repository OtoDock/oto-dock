"""The bulk credit: what a connection may have in flight to a satellite.

The protocol's ping and pong queue behind every byte the proxy already
handed to the transport, so on a slow uplink a large push used to hold a
pong past its deadline and the keepalive closed a healthy socket. The
credit bounds those bytes per connection: a frame takes its share before
it is enqueued and gets it back with its ack, first come, first served.

The fakes here model the link: frames the writer sent are "delivered" only
when the test (or a pacer at a fixed virtual rate) says so, and the fake
satellite acks a frame once it is delivered.
"""

import asyncio
import base64
import hashlib
import json

import pytest

from core.remote import satellite_file_transfer as sft
from core.remote.satellite_connection import (
    CreditFailed,
    SatelliteConnection,
    SatelliteConnectionManager,
    _BulkCredit,
)
from services.path_policy_v2 import PathRef

pytestmark = pytest.mark.asyncio


def _h(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Wire:
    """A websocket whose send returns at once (a transport under its high
    mark) and records what was handed over, in order."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        pass


def _manager_with_writer():
    mgr = SatelliteConnectionManager()
    wire = _Wire()
    conn = SatelliteConnection(machine_id="m1", ws=wire)
    mgr._connections["m1"] = conn
    conn.writer_task = asyncio.create_task(mgr._writer_loop(conn))
    return mgr, conn, wire


async def _ack(mgr, frame, status="ok"):
    await mgr.handle_message("m1", {"type": "ack", "command_id": frame["command_id"],
                                    "status": status, "error": ""})


def _file_bytes(frame) -> int:
    return len(base64.b64decode(frame["content_b64"]))


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- the credit itself --------------------------------------------------------


async def test_a_frame_goes_alone_when_nothing_is_in_flight():
    credit = _BulkCredit(cap=10)
    await credit.take(25)            # above the cap: alone, never stuck
    assert credit.inflight == 25
    assert not credit.try_take(1)
    credit.release(25, acked=True)
    assert credit.inflight == 0


async def test_admission_is_first_come_first_served():
    credit = _BulkCredit(cap=10)
    credit.try_take(8)
    order: list[str] = []

    async def _want(name, n):
        await credit.take(n)
        order.append(name)

    big = asyncio.create_task(_want("big", 9))
    await _settle()
    small = asyncio.create_task(_want("small", 1))
    await _settle()
    # 1 byte would fit beside the 8 in flight, but the 9 asked first.
    assert order == [] and credit.inflight == 8
    credit.release(8, acked=True)
    await _settle()
    assert order == ["big", "small"]
    await asyncio.gather(big, small)
    assert credit.inflight == 10


async def test_a_cancelled_waiter_leaves_its_place_to_the_next():
    credit = _BulkCredit(cap=10)
    credit.try_take(10)
    first = asyncio.create_task(credit.take(5))
    second = asyncio.create_task(credit.take(5))
    await _settle()
    first.cancel()
    await _settle()
    credit.release(10, acked=True)
    await second
    assert credit.inflight == 5


async def test_a_waiter_granted_then_cancelled_gives_the_bytes_back():
    credit = _BulkCredit(cap=10)
    credit.try_take(10)
    waiter = asyncio.create_task(credit.take(4))
    await _settle()
    credit.release(10, acked=True)   # grants the waiter's 4
    waiter.cancel()                  # before it ran
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert credit.inflight == 0


async def test_a_failed_credit_refuses_every_waiter_and_every_later_take():
    credit = _BulkCredit(cap=10)
    credit.try_take(10)
    waiter = asyncio.create_task(credit.take(4))
    await _settle()
    credit.fail_all("gone")
    with pytest.raises(CreditFailed):
        await waiter
    with pytest.raises(CreditFailed):
        credit.try_take(1)


async def test_a_connection_is_measured_slow_from_its_acks():
    credit = _BulkCredit(cap=1 << 20)
    credit.try_take(4096)
    credit._busy_since -= 3          # busy for 3 s
    credit.release(4096, acked=True)  # 4 KiB acked in the window: slow
    assert credit.is_slow() is True


# --- push_file on the credit ---------------------------------------------------


async def test_unacked_bulk_bytes_stay_under_the_credit(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 1024)
    mgr, conn, wire = _manager_with_writer()
    conn.bulk_credit = _BulkCredit(cap=4096)
    data = bytes(range(256)) * 256   # 64 KiB → 64 chunks
    push = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/big.bin"), data, agent_slug="a1"))
    acked = 0
    peak = 0
    while not push.done():
        await _settle()
        unacked = sum(_file_bytes(f) for f in wire.sent[acked:])
        peak = max(peak, unacked)
        if acked < len(wire.sent):
            await _ack(mgr, wire.sent[acked])
            acked += 1
    assert await push is True
    assert peak <= 4096
    await _settle()
    assert conn.bulk_credit.inflight == 0
    conn.writer_task.cancel()


async def test_a_control_frame_never_waits_behind_more_than_the_credit(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 1024)
    mgr, conn, wire = _manager_with_writer()
    cap = 4096
    conn.bulk_credit = _BulkCredit(cap=cap)
    data = bytes(64 * 1024)
    behind: list[int] = []
    delivered = 0                    # frames of wire.sent the link carried

    async def _pacer():
        nonlocal delivered
        while True:
            await asyncio.sleep(0.002)
            if delivered < len(wire.sent):
                frame = wire.sent[delivered]
                delivered += 1
                if frame.get("type") == "file_push":
                    await _ack(mgr, frame)
                else:
                    # A control frame: what bulk bytes went out ahead of it
                    # and had not reached the satellite yet.
                    behind.append(0)

    async def _controls():
        for i in range(20):
            await asyncio.sleep(0.005)
            before = len(wire.sent)
            await conn.enqueue_send({"type": "pong", "n": i})
            await _settle()
            idx = next(j for j in range(before, len(wire.sent))
                       if wire.sent[j].get("n") == i)
            ahead = sum(_file_bytes(f) for f in wire.sent[delivered:idx]
                        if f.get("type") == "file_push")
            behind.append(ahead)

    pacer = asyncio.create_task(_pacer())
    controls = asyncio.create_task(_controls())
    assert await mgr.push_file(
        "m1", PathRef("agent_tree", "workspace/big.bin"), data, agent_slug="a1") is True
    await controls
    pacer.cancel()
    conn.writer_task.cancel()
    assert max(behind) <= cap


async def test_slow_steady_acks_do_not_fail_the_push(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4)
    mgr, conn, wire = _manager_with_writer()

    async def _satellite():
        n = 0
        while True:
            await asyncio.sleep(0.1)
            if n < len(wire.sent):
                await _ack(mgr, wire.sent[n])
                n += 1

    sat = asyncio.create_task(_satellite())
    data = b"x" * 64                 # 16 chunks at one ack per 0.1 s: 1.6 s
    ok = await mgr.push_file("m1", PathRef("agent_tree", "w.bin"), data,
                             agent_slug="a1", stall_s=0.6)
    sat.cancel()
    conn.writer_task.cancel()
    assert ok is True
    await _settle()
    assert conn.bulk_credit.inflight == 0


async def test_a_stalled_push_fails_and_leaves_nothing_on_the_bulk_lane(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4)
    mgr = SatelliteConnectionManager()
    conn = SatelliteConnection(machine_id="m1", ws=_Wire())
    mgr._connections["m1"] = conn
    conn.bulk_credit = _BulkCredit(cap=8)
    # No writer: the frames stay on the lane, nothing is ever acked.
    ok = await mgr.push_file("m1", PathRef("agent_tree", "w.bin"), b"y" * 40,
                             agent_slug="a1", stall_s=0.1)
    assert ok is False
    assert conn.bulk_queue.qsize() == 2
    await _settle()
    assert conn.bulk_credit.inflight == 0
    assert mgr._pending_acks == {}
    # The writer drops what the push abandoned instead of sending it.
    wire = conn.ws
    conn.writer_task = asyncio.create_task(mgr._writer_loop(conn))
    await _settle()
    assert wire.sent == []
    conn.writer_task.cancel()


async def test_a_push_queued_behind_a_moving_transfer_is_not_stalled(monkeypatch):
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4)
    mgr, conn, wire = _manager_with_writer()

    async def _other_traffic():
        # Another transfer's frames keep arriving on the connection (a
        # satellite below 0.5.137 sends one pull whole before anything
        # else), while this push's ack waits behind them.
        for _ in range(8):
            await asyncio.sleep(0.1)
            await mgr.handle_message("m1", {"type": "file_content", "request_id": "other"})

    traffic = asyncio.create_task(_other_traffic())
    push = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), b"abc", agent_slug="a1", stall_s=0.4))
    await traffic
    assert not push.done()           # 0.8 s without its own ack, still alive
    await _ack(mgr, wire.sent[0])
    assert await push is True
    conn.writer_task.cancel()
    await _settle()
    assert conn.bulk_credit.inflight == 0


async def test_a_waiting_push_goes_before_the_running_one_takes_again(monkeypatch):
    """A foreground write behind a long fan-out goes right after the frame
    the fan-out had already queued for, never starved by the push whose
    frames keep being acked."""
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 1024)
    mgr, conn, wire = _manager_with_writer()
    conn.bulk_credit = _BulkCredit(cap=2048)
    long_push = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "big.bin"), bytes(64 * 1024), agent_slug="a1"))
    await _settle()
    assert len(wire.sent) == 2       # the credit is full
    write = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "doc.docx"), bytes(1000), agent_slug="a1"))
    await _settle()
    await _ack(mgr, wire.sent[0])    # the fan-out's frame queued first
    await _settle()
    await _ack(mgr, wire.sent[1])    # then the write, before its next one
    await _settle()
    paths = [f["path"] for f in wire.sent]
    assert paths[:4] == ["big.bin", "big.bin", "big.bin", "doc.docx"]
    # Let both finish.
    acked = 2
    while not (long_push.done() and write.done()):
        await _settle()
        if acked < len(wire.sent):
            await _ack(mgr, wire.sent[acked])
            acked += 1
    assert await write is True and await long_push is True
    await _settle()
    assert conn.bulk_credit.inflight == 0
    conn.writer_task.cancel()


async def test_small_chunks_reassemble_on_an_old_satellite(monkeypatch):
    """A connection measured slow sends smaller chunks: a 0.5.76 satellite
    appends any size, truncates on index 0, and commits an agent-tree file on
    the hash and a satellite-host one on ``chunk_index + 1 >= total``."""
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 64)
    monkeypatch.setattr(sft, "PUSH_SLOW_CHUNK_BYTES", 16)
    mgr, conn, wire = _manager_with_writer()
    conn.bulk_credit.slow = True
    files: dict[str, bytes] = {}
    partial: dict[str, bytes] = {}

    async def _old_satellite():
        n = 0
        while True:
            await asyncio.sleep(0)
            while n < len(wire.sent):
                f = wire.sent[n]
                n += 1
                block = base64.b64decode(f["content_b64"])
                key = f["path"]
                partial[key] = (b"" if f["chunk_index"] == 0 else partial.get(key, b"")) + block
                if f["path_kind"] == "agent_tree" and f["hash"]:
                    assert _h(partial[key]) == f["hash"]
                    files[key] = partial[key]
                elif f["path_kind"] == "satellite_host" and f["chunk_index"] + 1 >= f["total_chunks"]:
                    files[key] = partial[key]
                await _ack(mgr, f)

    sat = asyncio.create_task(_old_satellite())
    data = bytes(range(200))
    assert await mgr.push_file("m1", PathRef("agent_tree", "w.bin"), data, agent_slug="a1")
    assert await mgr.push_file("m1", PathRef("satellite_host", "/tmp/w.bin"), data)
    sat.cancel()
    conn.writer_task.cancel()
    assert files == {"w.bin": data, "/tmp/w.bin": data}
    await _settle()
    assert conn.bulk_credit.inflight == 0
    assert {_file_bytes(f) for f in wire.sent} == {16, 8}   # 12 × 16 + 8


async def test_a_credit_below_a_chunk_sends_small_frames_the_inline_write_too(monkeypatch):
    """With the credit set below one chunk every frame is the slow chunk,
    a file that would fit one inline write included, so the credit paces."""
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 64)
    monkeypatch.setattr(sft, "PUSH_SLOW_CHUNK_BYTES", 16)
    mgr, conn, wire = _manager_with_writer()
    conn.bulk_credit.cap = 32

    async def _satellite():
        n = 0
        while True:
            await asyncio.sleep(0)
            while n < len(wire.sent):
                await _ack(mgr, wire.sent[n])
                n += 1

    sat = asyncio.create_task(_satellite())
    assert await mgr.push_file("m1", PathRef("agent_tree", "w.bin"), b"x" * 40, agent_slug="a1")
    sat.cancel()
    conn.writer_task.cancel()
    assert [f["action"] for f in wire.sent] == ["write_chunk"] * 3
    assert [_file_bytes(f) for f in wire.sent] == [16, 16, 8]
    await _settle()
    assert conn.bulk_credit.inflight == 0


async def test_a_waiting_path_source_push_goes_before_the_running_one_takes_again(tmp_path, monkeypatch):
    """The fairness of the test above with Path sources, whose reads await
    between frames."""
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 1024)
    big, doc = tmp_path / "big.bin", tmp_path / "doc.docx"
    big.write_bytes(bytes(64 * 1024))
    doc.write_bytes(bytes(1000))
    mgr, conn, wire = _manager_with_writer()
    conn.bulk_credit = _BulkCredit(cap=2048)
    long_push = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "big.bin"), big, agent_slug="a1"))
    while len(wire.sent) < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)        # its third chunk is read and queued for credit
    write = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "doc.docx"), doc, agent_slug="a1"))
    await asyncio.sleep(0.05)
    acked = 0
    while not (long_push.done() and write.done()):
        await asyncio.sleep(0.01)
        if acked < len(wire.sent):
            await _ack(mgr, wire.sent[acked])
            acked += 1
    paths = [f["path"] for f in wire.sent]
    assert paths.index("doc.docx") <= 3
    assert await write is True and await long_push is True
    await _settle()
    assert conn.bulk_credit.inflight == 0
    conn.writer_task.cancel()


async def test_a_lost_frame_aborts_the_push_at_once(monkeypatch):
    """A satellite answers its frames in arrival order: an answer to a later
    frame while an earlier one is unanswered means that one was lost."""
    monkeypatch.setattr("core.remote.file_sync.MAX_CHUNK_SIZE", 4)
    mgr, conn, wire = _manager_with_writer()
    push = asyncio.create_task(mgr.push_file(
        "m1", PathRef("agent_tree", "w.bin"), b"x" * 16, agent_slug="a1", stall_s=30))
    while len(wire.sent) < 2:
        await asyncio.sleep(0.01)
    await _ack(mgr, wire.sent[1])    # the first frame's answer never comes
    assert await asyncio.wait_for(push, 2) is False
    await _settle()
    assert conn.bulk_credit.inflight == 0
    conn.writer_task.cancel()


async def test_a_transfer_frame_is_contact_for_the_heartbeat_monitor():
    import time
    from core.remote.satellite_connection import _last_contact
    conn = SatelliteConnection(machine_id="m1", ws=_Wire())
    conn.last_heartbeat = time.monotonic() - 200       # queued behind a pull
    conn.last_transfer_at = time.monotonic() - 5
    assert time.monotonic() - _last_contact(conn) < 10
