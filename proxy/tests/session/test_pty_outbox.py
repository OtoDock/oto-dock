"""A PTY viewer's bounded outbox (ws/dashboard_pty._PtyOutbox): chunks go
out in order through one drain task; a reader that falls a full outbox
behind gets the screen replayed instead of the dropped backlog, and a
detach cancels what is left.
"""

import asyncio

import pytest

from ws.dashboard_pty import _PtyOutbox


class _Sink:
    def __init__(self):
        self.sent: list[bytes] = []
        self.replays = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def send(self, data: bytes) -> None:
        await self.gate.wait()
        self.sent.append(data)

    async def replay(self) -> None:
        await self.gate.wait()
        self.replays += 1


@pytest.mark.asyncio
async def test_chunks_go_out_in_order_through_one_task():
    sink = _Sink()
    box = _PtyOutbox(sink.send, sink.replay, limit=1024)
    for i in range(5):
        box.push(bytes([i]) * 10)
    first = box.task
    await first
    assert sink.sent == [bytes([i]) * 10 for i in range(5)]
    box.push(b"late")
    assert box.task is not first
    await box.task
    assert sink.sent[-1] == b"late" and sink.replays == 0


@pytest.mark.asyncio
async def test_a_paused_reader_past_the_limit_gets_a_replay_not_the_backlog():
    sink = _Sink()
    sink.gate.clear()
    box = _PtyOutbox(sink.send, sink.replay, limit=100)
    box.push(b"a" * 60)
    await asyncio.sleep(0)          # the drain task holds it, parked in send()
    box.push(b"b" * 70)             # 70 queued behind the one in flight
    box.push(b"c" * 40)             # 110 > 100: the backlog is dropped
    box.push(b"d" * 10)             # still overflowed: dropped too
    assert box.overflowed
    sink.gate.set()
    await box.task
    assert sink.sent == [b"a" * 60]
    assert sink.replays == 1
    assert not box.overflowed
    box.push(b"e")
    await box.task
    assert sink.sent[-1] == b"e" and sink.replays == 1


@pytest.mark.asyncio
async def test_cancel_drops_the_rest():
    sink = _Sink()
    sink.gate.clear()
    box = _PtyOutbox(sink.send, sink.replay, limit=1024)
    box.push(b"x")
    box.push(b"y")
    await asyncio.sleep(0)
    box.cancel()
    sink.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await box.task
    assert sink.sent == []
