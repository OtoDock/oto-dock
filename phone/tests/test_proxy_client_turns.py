"""ProxyClient turn correlation: stale-frame dropping + the abort protocol.

Drives _recv_loop/_ws_send_message with a duck-typed WebSocket, pinning the
client half of the /ws/phone turn protocol: frames are routed by turn id (a
barged-in turn's tail must never be spoken as the next answer), warmup
replies flow through the control queue, and abort_turn() is turn-scoped —
Direct always; proxy mode only over a live WS (interrupt semantics: the
partial turn stays in history, so abort_erases_turn is Direct-only).
"""

import asyncio
import json

from proxy.client import ProxyClient


class FakeWS:
    """Duck-typed websockets client connection (async-iterable + send)."""

    def __init__(self):
        self.sent = []
        self._queue = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def push(self, msg):
        self._queue.put_nowait(json.dumps(msg))

    def end(self):
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def _client(llm_mode="direct"):
    c = ProxyClient(llm_mode=llm_mode)
    c._ws = FakeWS()
    c._ws_connected = True
    return c


def test_stale_turn_frames_are_dropped():
    """Leftovers from an abandoned turn (turn 0) must not surface in the
    next turn's stream — only the current turn's frames are yielded."""
    async def run():
        c = _client()
        ws = c._ws
        # Pre-load stale + fresh frames; the receiver only starts consuming
        # once _ws_send_message has already claimed turn 1 (no suspension
        # point before then), so routing is deterministic.
        ws.push({"type": "text", "turn": 0, "data": {"content": "stale tail"}})
        ws.push({"type": "done", "turn": 0, "data": {}})
        ws.push({"type": "text", "turn": 1, "data": {"content": "fresh"}})
        ws.push({"type": "done", "turn": 1, "data": {}})
        c._recv_task = asyncio.create_task(c._recv_loop())

        chunks = [chunk async for chunk in c._ws_send_message("hello")]
        assert chunks == ["fresh"]
        assert ws.sent[-1]["type"] == "chat"
        assert ws.sent[-1]["turn"] == 1

        ws.end()
        await c._recv_task
    asyncio.run(run())


def test_warmup_reply_routes_via_control_queue():
    async def run():
        c = _client()
        ws = c._ws
        ws.push({"type": "warmup_ready",
                 "data": {"session_id": "s1", "llm_mode": "direct"}})
        c._recv_task = asyncio.create_task(c._recv_loop())

        sid = await c._ws_warmup()
        assert sid == "s1"
        assert c.session_id == "s1"

        ws.end()
        await c._recv_task
    asyncio.run(run())


def test_abort_turn_sends_turn_scoped_abort():
    async def run():
        c = _client(llm_mode="direct")
        c._current_turn = 7
        await c.abort_turn()
        assert c._ws.sent == [{"type": "abort", "turn": 7}]
    asyncio.run(run())


def test_abort_turn_proxy_mode_sends_abort_over_ws():
    """Parity with the platform's graceful CLI/Codex interrupt: a WS-proxy
    client aborts (the proxy runs layer.abort), with INTERRUPT semantics —
    the partial turn persists, so abort_erases_turn stays False."""
    async def run():
        c = _client(llm_mode="proxy")
        c._current_turn = 3
        assert c.supports_abort
        assert not c.abort_erases_turn
        await c.abort_turn()
        assert c._ws.sent == [{"type": "abort", "turn": 3}]
    asyncio.run(run())


def test_abort_turn_is_noop_in_proxy_mode_without_ws():
    """HTTP-SSE fallback has no signalling channel — proxy mode degrades to
    run-to-completion, and the local history is never popped."""
    async def run():
        c = ProxyClient(llm_mode="proxy")  # no WS
        c.messages.append({"role": "user", "content": "unanswered"})
        assert not c.supports_abort
        await c.abort_turn()
        assert c.messages == [{"role": "user", "content": "unanswered"}]
    asyncio.run(run())


def test_abort_turn_http_fallback_pops_unanswered_user_message():
    async def run():
        c = ProxyClient(llm_mode="direct")  # no WS → HTTP fallback shape
        c.messages.append({"role": "user", "content": "unanswered"})
        await c.abort_turn()
        assert c.messages == []
    asyncio.run(run())
