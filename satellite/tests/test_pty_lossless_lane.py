"""The lossless, backpressured interactive-PTY output lane.

`pty_output` rides a dedicated bounded lane (`_pty_send_queue`) drained CONTROL-FIRST
by the writer; while CONNECTED the producer BLOCKS when the lane is full (instead of
drop-oldest), which pauses the PTY reader (Unix reader task awaits the put; the
Windows reader thread blocks on it via run_coroutine_threadsafe) so the TUI's own
write() stalls rather than us corrupting the xterm byte stream.

DISCONNECTED, frames are dropped and the lane is purged — parking the reader on a
dead lane bursts the raw local tee and re-ignites the 59fe52b query/answer storm
(the proxy-outage terminal flood).

Standalone runner (no pytest) — safe while the proxy is live. Run from anywhere:
    ./proxy/venv/bin/python satellite/tests/test_pty_lossless_lane.py
"""

import asyncio
import base64
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.asyncio


def _mk_client(lane_size=4, connected=True):
    from satellite.transport.ws_client import SatelliteWSClient
    c = SatelliteWSClient(types.SimpleNamespace(), types.SimpleNamespace())
    c._pty_send_queue = asyncio.Queue(maxsize=lane_size)  # tiny → forces backpressure
    if connected:
        # enqueue_pty only accepts frames while connected+authenticated
        # (disconnected frames are dropped — no viewer, and parking the
        # reader on a dead lane floods the local terminal).
        c._ws = object()
        c._authenticated = True
    return c


class _SlowWS:
    """A fake ws whose send() is deliberately slow, so the bounded lane fills and
    the PTY reader must pause (exercising backpressure)."""
    def __init__(self):
        self.sent = []

    async def send(self, s):
        await asyncio.sleep(0.001)
        self.sent.append(json.loads(s))


def _nums_from_frames(frames):
    raw = b"".join(
        base64.b64decode(m["data_b64"]) for m in frames if m.get("type") == "pty_output"
    )
    return [int(x) for x in raw.split()]


async def test_writer_control_first():
    """With both lanes full, the writer drains ALL control frames before any pty
    frame (a terminal burst can't delay an ack/heartbeat → no false disconnect)."""
    c = _mk_client(lane_size=16)
    ws = _SlowWS()
    c._ws = ws
    # Fill both lanes BEFORE the writer runs, so each selection sees both ready.
    await c.enqueue_pty({"type": "pty_output", "session_id": "s", "data_b64": ""})
    await c.enqueue_pty({"type": "pty_output", "session_id": "s", "data_b64": ""})
    await c.enqueue_send({"type": "ack", "command_id": "1"})
    await c.enqueue_send({"type": "heartbeat"})
    w = asyncio.create_task(c._writer_loop())
    await asyncio.sleep(0.1)
    w.cancel()
    order = [m["type"] for m in ws.sent]
    assert order == ["ack", "heartbeat", "pty_output", "pty_output"], order


async def test_control_lane_still_drops_oldest():
    """The control lane keeps its drop-oldest semantics (unchanged)."""
    c = _mk_client()
    c._send_queue = asyncio.Queue(maxsize=2)
    await c.enqueue_send({"type": "a", "i": 0})
    await c.enqueue_send({"type": "b", "i": 1})
    await c.enqueue_send({"type": "c", "i": 2})  # overflow → drops oldest (i=0)
    items = []
    while not c._send_queue.empty():
        items.append(c._send_queue.get_nowait())
    assert [m["i"] for m in items] == [1, 2], items


async def test_pty_lane_blocks_when_full_not_dropped():
    """The pty lane BLOCKS the producer when full (backpressure) — never drops."""
    c = _mk_client(lane_size=4)
    for i in range(4):
        await c.enqueue_pty({"type": "pty_output", "i": i, "data_b64": ""})
    blocked = asyncio.create_task(
        c.enqueue_pty({"type": "pty_output", "i": 4, "data_b64": ""}))
    await asyncio.sleep(0.03)
    assert not blocked.done(), "enqueue_pty must block on a full lane (no drop)"
    # Draining one frees a slot → the blocked put completes (lossless).
    c._pty_send_queue.get_nowait()
    await asyncio.wait_for(blocked, timeout=1.0)


async def test_unix_integration_lossless_backpressure():
    """Real Unix PTY → enqueue_pty → control-first writer → slow ws. A large output
    through a tiny lane forces the reader to pause; every byte must still arrive in
    order (no drop)."""
    from satellite.terminal import pty_relay
    c = _mk_client(lane_size=4)
    ws = _SlowWS()
    c._ws = ws
    w = asyncio.create_task(c._writer_loop())
    done = asyncio.Event()
    N = 50000
    code = ("import sys\n"
            f"for i in range({N}): sys.stdout.write('%06d\\n' % i)\n"
            "sys.stdout.flush()\n")
    pty_relay.spawn_pty(
        ["python3", "-c", code],
        env={"TERM": "xterm-256color", "PATH": os.environ.get("PATH", "")},
        on_output=lambda d: c.enqueue_pty({
            "type": "pty_output", "session_id": "s",
            "data_b64": base64.b64encode(d).decode()}),
        on_exit=lambda code: done.set(),
    )
    await asyncio.wait_for(done.wait(), timeout=60)
    while not c._pty_send_queue.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    w.cancel()
    nums = _nums_from_frames(ws.sent)
    assert nums == list(range(N)), f"lossless/order FAIL: {len(nums)}/{N} nums"


class _FakeWinPty:
    """Minimal pywinpty.PtyProcess stand-in: yields known str chunks then EOFs."""
    def __init__(self, chunks):
        self.pid = 4242
        self._chunks = list(chunks)
        self._i = 0
        self._alive = True

    def read(self, n):
        if self._i >= len(self._chunks):
            self._alive = False
            raise EOFError
        c = self._chunks[self._i]
        self._i += 1
        return c

    def write(self, s):
        pass

    def setwinsize(self, r, c):
        pass

    def isalive(self):
        return self._alive

    def close(self, force=False):
        pass

    def terminate(self, force=False):
        pass

    def wait(self):
        return 0


async def test_windows_integration_lossless_backpressure():
    """Fake-winpty reader THREAD → run_coroutine_threadsafe → enqueue_pty → writer.
    The thread must block on a full lane (backpressure) and lose nothing."""
    from satellite.terminal import winpty_relay
    c = _mk_client(lane_size=4)
    ws = _SlowWS()
    c._ws = ws
    w = asyncio.create_task(c._writer_loop())
    N = 2000
    fake = _FakeWinPty([f"{i:06d}\n" for i in range(N)])
    done = asyncio.Event()
    handle = winpty_relay.WinPtyProcess(
        proc=fake, rows=24, cols=80,
        on_output=lambda d: c.enqueue_pty({
            "type": "pty_output", "session_id": "s",
            "data_b64": base64.b64encode(d).decode()}),
        on_exit=lambda code: done.set(),
        scrollback_limit=10 * 1024 * 1024,
    )
    handle._attach()  # starts the reader thread
    await asyncio.wait_for(done.wait(), timeout=60)
    while not c._pty_send_queue.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    w.cancel()
    nums = _nums_from_frames(ws.sent)
    assert nums == list(range(N)), f"lossless/order FAIL: {len(nums)}/{N} nums"


async def test_pty_lane_drops_when_disconnected():
    """DISCONNECTED, enqueue_pty drops immediately — the PTY reader must never
    park on a lane nobody drains (proxy-outage terminal flood)."""
    c = _mk_client(lane_size=1, connected=False)
    for i in range(5):
        await asyncio.wait_for(
            c.enqueue_pty({"type": "pty_output", "i": i, "data_b64": ""}),
            timeout=1.0,
        )
    assert c._pty_send_queue.empty(), "disconnected frames must be dropped"

    # Mid-life disconnect: an authenticated client that loses the WS drops too.
    c2 = _mk_client(lane_size=1)
    c2._ws = None
    await asyncio.wait_for(
        c2.enqueue_pty({"type": "pty_output", "data_b64": ""}), timeout=1.0,
    )
    assert c2._pty_send_queue.empty()


async def test_disconnect_purge_unparks_blocked_reader():
    """The connect-loop's disconnect purge frees a producer parked on a full
    lane (the reader resumes within ms of disconnect detection instead of
    delivering a query burst to the raw local tee on reconnect)."""
    c = _mk_client(lane_size=2)
    await c.enqueue_pty({"type": "pty_output", "i": 0, "data_b64": ""})
    await c.enqueue_pty({"type": "pty_output", "i": 1, "data_b64": ""})
    parked = asyncio.create_task(
        c.enqueue_pty({"type": "pty_output", "i": 2, "data_b64": ""}))
    await asyncio.sleep(0.03)
    assert not parked.done(), "connected + full lane must still backpressure"
    # Simulate the connect_forever finally: state cleared, lane purged.
    c._ws = None
    c._authenticated = False
    while True:
        try:
            c._pty_send_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    await asyncio.wait_for(parked, timeout=1.0)


if __name__ == "__main__":
    import traceback
    _tests = [
        test_writer_control_first,
        test_control_lane_still_drops_oldest,
        test_pty_lane_blocks_when_full_not_dropped,
        test_pty_lane_drops_when_disconnected,
        test_disconnect_purge_unparks_blocked_reader,
        test_unix_integration_lossless_backpressure,
        test_windows_integration_lossless_backpressure,
    ]
    _fails = 0
    for _t in _tests:
        try:
            asyncio.run(_t())
            print(f"PASS {_t.__name__}")
        except Exception:
            _fails += 1
            traceback.print_exc()
            print(f"FAIL {_t.__name__}")
    print(f"\n{len(_tests) - _fails}/{len(_tests)} passed")
    sys.exit(1 if _fails else 0)
