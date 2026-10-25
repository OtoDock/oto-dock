"""A refused sign-in backs off: the reconnect loop resets its backoff only
once the platform accepted the auth frame, so a platform that closes every
auth with 1013 ("the platform could not check this machine now") or 4001
sees the machine wait longer each time, up to the cap, instead of retrying
once a second.

Driven against a real websockets server on loopback that answers each auth
frame with a 1013 close; the jitter is pinned to its top so each wait is the
backoff itself, read from the loop's own "Reconnecting in" line.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import websockets

from satellite.transport import ws_client
from satellite.transport.ws_client import SatelliteWSClient


class _SM:
    """The session manager surface the reconnect loop touches."""

    pty_sessions: dict = {}
    sessions: dict = {}
    turn_state: dict = {}

    def detect_capabilities(self) -> dict:
        return {"os": "linux"}

    def kill_steps(self, reason: str) -> None:
        pass

    def cancel_pulls(self) -> int:
        return 0

    def headless_sessions_alive(self) -> list:
        return []


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_a_refused_auth_waits_longer_each_time(monkeypatch, caplog):
    base = 0.02
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", base)
    monkeypatch.setattr(ws_client.random, "random", lambda: 1.0)
    auths: list[str] = []
    refused = asyncio.Event()

    async def _refuse(ws):
        auths.append(await ws.recv())
        await ws.close(1013, "the platform could not check this machine now")
        if len(auths) >= 3:
            refused.set()

    async with websockets.serve(_refuse, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = SimpleNamespace(machine_id="m-1", machine_secret="s",
                              platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)
        client = SatelliteWSClient(cfg, _SM())
        caplog.set_level(logging.INFO, logger="satellite")
        loop_task = asyncio.create_task(client.connect_forever())
        try:
            await asyncio.wait_for(refused.wait(), timeout=10)
            # the third refusal's wait is logged as the loop leaves the close
            for _ in range(200):
                waits = [r.args[0] for r in caplog.records if r.msg == "Reconnecting in %.0fs..."]
                if len(waits) >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    assert len(auths) >= 3
    assert waits[:3] == pytest.approx([base, base * 2, base * 4])
