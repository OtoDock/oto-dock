"""ProxyClient turn correlation: stale-frame dropping + the abort protocol.

Drives _recv_loop/_ws_send_message with a duck-typed WebSocket, pinning the
client half of the /ws/phone turn protocol: frames are routed by turn id (a
barged-in turn's tail must never be spoken as the next answer), warmup
replies flow through the control queue, and abort_turn() is turn-scoped and
goes over the live WS in both modes (interrupt semantics on proxy mode: the
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

        sid = await c.warmup_session()
        assert sid == "s1"
        assert c.session_id == "s1"
        # The PIN-gate result always rides the warmup (False by default).
        assert ws.sent[-1]["type"] == "warmup"
        assert ws.sent[-1]["pin_verified"] is False

        ws.end()
        await c._recv_task
    asyncio.run(run())


def test_warmup_carries_the_pin_gate_result():
    async def run():
        c = ProxyClient(llm_mode="direct", phone_mode=True, caller_phone="+3021",
                        pin_verified=True)
        c._ws = FakeWS()
        c._ws_connected = True
        c._ws.push({"type": "warmup_ready",
                    "data": {"session_id": "s2", "llm_mode": "direct"}})
        c._recv_task = asyncio.create_task(c._recv_loop())
        assert await c.warmup_session() == "s2"
        sent = c._ws.sent[-1]
        assert sent["pin_verified"] is True and sent["caller_phone"] == "+3021"
        c._ws.end()
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


def test_abort_turn_is_noop_without_ws():
    """No socket, no signalling channel in either mode; the local history is
    never touched."""
    async def run():
        for mode in ("proxy", "direct"):
            c = ProxyClient(llm_mode=mode)  # no WS
            c.messages.append({"role": "user", "content": "unanswered"})
            assert not c.supports_abort
            await c.abort_turn()
            assert c.messages == [{"role": "user", "content": "unanswered"}]
    asyncio.run(run())


def test_a_client_with_no_socket_has_no_other_transport(monkeypatch):
    """A failed connect marks the proxy lost; the warmup answers None, a turn
    yields nothing (the pipeline ends the call, no error speech), the close
    sends nothing."""
    import proxy.client as pc

    async def refuse(*a, **kw):
        raise OSError("refused")
    monkeypatch.setattr(pc.websockets, "connect", refuse)

    async def run():
        c = ProxyClient(llm_mode="proxy")
        assert await c.connect() is False
        assert c.proxy_lost is True and not hasattr(c, "_http")
        assert await c.warmup_session() is None
        said = [chunk async for chunk in c.send_message("hello")]
        assert said == []
        assert await c.send_system_note("x") is False
        c.session_id = "s-1"
        assert await c.close_session() is False
        await c.close()
    asyncio.run(run())


def test_the_daemons_own_close_is_not_a_loss():
    """The receive loop's end marks the proxy lost and tells the pipeline,
    except when the daemon itself is closing the call."""
    seen = []

    async def run():
        c = _client(llm_mode="proxy")
        c.on_proxy_lost = lambda: seen.append("lost")
        c._recv_task = asyncio.create_task(c._recv_loop())
        await asyncio.sleep(0)
        c._ws.close = _async_noop
        await c.close()
        return c
    c = asyncio.run(run())
    assert c.proxy_lost is False and seen == []


async def _async_noop(*a, **kw):
    return None
