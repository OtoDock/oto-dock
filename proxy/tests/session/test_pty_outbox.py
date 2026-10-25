"""A PTY viewer's bounded outbox (ws/dashboard_pty._PtyOutbox): output,
statuses and replays go out in one order through one drain task; a reader
that falls a full outbox behind gets the screen replayed instead of the
dropped backlog; a replay drops the output queued around it (its snapshot
holds it); a held outbox drains nothing until released; a detach cancels
what is left, and a release after it is a no-op.
"""

import asyncio

import pytest

from ws.dashboard_pty import _mirror_replay, _PtyOutbox


class _Sink:
    def __init__(self):
        self.sent: list = []
        self.replays = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def send(self, data: bytes) -> None:
        await self.gate.wait()
        self.sent.append(data)

    async def replay(self) -> None:
        await self.gate.wait()
        self.replays += 1
        self.sent.append("REPLAY")

    async def status(self, state: str) -> None:
        await self.gate.wait()
        self.sent.append(f"status:{state}")


def _box(sink, **kw):
    return _PtyOutbox(sink.send, sink.replay, sink.status, **kw)


async def _settle(box):
    for _ in range(50):
        if box.task is None or box.task.done():
            return
        await asyncio.sleep(0)
    await box.task


@pytest.mark.asyncio
async def test_chunks_go_out_in_order_through_one_task():
    sink = _Sink()
    box = _box(sink, limit=1024)
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
    box = _box(sink, limit=100)
    box.push(b"a" * 60)
    await asyncio.sleep(0)          # the drain task holds it, parked in send()
    box.push(b"b" * 70)             # 70 queued behind the one in flight
    box.push(b"c" * 40)             # 110 > 100: the backlog is dropped
    box.push(b"d" * 10)             # a replay is waiting: it holds this too
    assert box.replay_pending
    sink.gate.set()
    await _settle(box)
    assert sink.sent == [b"a" * 60, "REPLAY", "status:reconnected"]
    assert sink.replays == 1
    assert not box.replay_pending
    box.push(b"e")
    await _settle(box)
    assert sink.sent[-1] == b"e" and sink.replays == 1


@pytest.mark.asyncio
async def test_a_reconnect_replay_drops_the_output_queued_around_it():
    """The gap output a satellite blip delivers is in the ring before it is
    pushed: queued before or after the replay request, the snapshot holds
    it, so it is sent once, inside the replay; output after the snapshot
    follows it."""
    sink = _Sink()
    sink.gate.clear()
    box = _box(sink, limit=1024)
    box.push(b"before")
    await asyncio.sleep(0)          # in flight
    box.push(b"gap-1")
    box.request_replay()
    box.push(b"gap-2")              # recorded before the snapshot is taken
    sink.gate.set()
    await _settle(box)
    box.push(b"after")
    await _settle(box)
    assert sink.sent == [b"before", "REPLAY", "status:reconnected", b"after"]


@pytest.mark.asyncio
async def test_statuses_keep_their_place_behind_a_replay():
    """A "reconnecting" that comes after a queued replay goes out after it
    and its "reconnected": the latest state wins on the viewer."""
    sink = _Sink()
    sink.gate.clear()
    box = _box(sink, limit=1024)
    box.push(b"x")
    await asyncio.sleep(0)
    box.request_replay()
    box.push_status("reconnecting")
    sink.gate.set()
    await _settle(box)
    assert sink.sent == [b"x", "REPLAY", "status:reconnected", "status:reconnecting"]


@pytest.mark.asyncio
async def test_a_held_outbox_drains_nothing_until_released():
    sink = _Sink()
    box = _box(sink, limit=1024, held=True)
    box.push(b"live-1")
    box.push_status("reconnecting")
    await asyncio.sleep(0)
    assert box.task is None and sink.sent == []
    box.release()
    await _settle(box)
    assert sink.sent == [b"live-1", "status:reconnecting"]


@pytest.mark.asyncio
async def test_cancel_drops_the_rest_and_a_later_release_is_a_no_op():
    sink = _Sink()
    sink.gate.clear()
    box = _box(sink, limit=1024)
    box.push(b"x")
    box.push(b"y")
    await asyncio.sleep(0)
    box.cancel()
    sink.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await box.task
    assert sink.sent == []

    held = _box(sink, limit=1024, held=True)
    held.push(b"z")
    held.request_replay()
    held.cancel()
    held.release()
    held.push(b"w")
    await asyncio.sleep(0)
    assert held.task is None and sink.sent == []


class _Ring:
    def __init__(self, data: bytes):
        self._data = data

    def scrollback(self) -> bytes:
        return self._data


class _Sess:
    def __init__(self, data: bytes, *, remote: bool):
        self.pty = _Ring(data)
        self.is_remote = remote


def test_a_replay_strips_a_local_rings_queries_and_every_rings_clipboard_writes():
    """The overflow replay used to strip clipboard writes only: a local ring
    is raw, so its replay must lose the program's terminal queries too, the
    rule the attach replay follows."""
    raw = b"text\x1b[6nmore\x1b]52;c;aGk=\x07end"
    assert _mirror_replay(_Sess(raw, remote=False)) == b"textmoreend"
    # A remote ring is stripped of queries where it is fed.
    assert _mirror_replay(_Sess(raw, remote=True)) == b"text\x1b[6nmoreend"
