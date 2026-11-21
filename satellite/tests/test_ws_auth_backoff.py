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
import json
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
    tool_relay = SimpleNamespace(close_all=lambda reason: 0)

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


async def _run_until(client, waits_wanted: int, caplog) -> list[float]:
    caplog.set_level(logging.INFO, logger="satellite")
    loop_task = asyncio.create_task(client.connect_forever())
    waits: list[float] = []
    try:
        for _ in range(1000):
            waits = [r.args[0] for r in caplog.records if r.msg == "Reconnecting in %.0fs..."]
            if len(waits) >= waits_wanted:
                break
            await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
    return waits


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize("steps_fail", [False, True])
async def test_a_dropped_link_ends_every_relayed_tool(monkeypatch, caplog, steps_fail):
    """A tool process relayed for the platform has nobody to answer once the
    link is gone, and it holds a session's secrets: the disconnect ends it,
    whatever ending the running steps does."""
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", 0.02)
    closed: list[str] = []

    class _Relayed(_SM):
        tool_relay = SimpleNamespace(close_all=closed.append)

        def kill_steps(self, reason: str) -> None:
            if steps_fail:
                raise RuntimeError("a step would not end")

    async def _accept_then_drop(ws):
        await ws.recv()
        await ws.send('{"type": "auth_result", "status": "ok", "report_id": "r-0"}')
        await ws.close(1011, "dropped")

    async with websockets.serve(_accept_then_drop, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", cli_bins={},
                              platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)
        await _run_until(SatelliteWSClient(cfg, _Relayed()), 1, caplog)

    assert closed and closed[0] == "the link to the platform dropped"


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_a_link_that_signs_in_and_drops_waits_longer_each_time_up_to_its_own_cap(monkeypatch, caplog):
    """A sign-in alone does not reset the wait: a link that drops right
    after it doubles the wait, capped lower than a refused connect is, so a
    machine whose link keeps dropping reconnects every few seconds, not
    every second."""
    base = 0.02
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", base)
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_SIGNED_IN_CAP", base * 4)
    monkeypatch.setattr(ws_client.random, "random", lambda: 1.0)
    reports: list[str] = []

    async def _accept_then_drop(ws):
        await ws.recv()
        await ws.send(f'{{"type": "auth_result", "status": "ok", "report_id": "r-{len(reports)}"}}')
        reports.append(await ws.recv())            # pty_alive
        await ws.close(1011, "dropped")

    async with websockets.serve(_accept_then_drop, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", cli_bins={},
                              platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)
        waits = await _run_until(SatelliteWSClient(cfg, _SM()), 5, caplog)

    assert waits[:5] == pytest.approx([base, base * 2, base * 4, base * 4, base * 4])
    # Each sign-in's alive frames repeat the id its answer named.
    assert '"report_id": "r-0"' in reports[0] and '"report_id": "r-1"' in reports[1]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_a_refused_connect_after_a_short_link_climbs_from_its_cap(monkeypatch, caplog):
    """The signed-in cap holds the climb itself, not only the wait of that
    round: several refused connects after a flapping link stay inside the
    platform's reconnect window."""
    base = 0.02
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", base)
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_SIGNED_IN_CAP", base * 4)
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_CAP", base * 64)
    monkeypatch.setattr(ws_client.random, "random", lambda: 1.0)
    links = {"n": 0}
    done = asyncio.Event()

    async def _accept_then_drop(ws):
        await ws.recv()
        await ws.send('{"type": "auth_result", "status": "ok"}')
        links["n"] += 1
        await ws.close(1011, "dropped")
        if links["n"] == 6:
            done.set()

    server = await websockets.serve(_accept_then_drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", cli_bins={},
                          platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)

    async def _stop_after_the_links():
        await done.wait()
        server.close()
        await server.wait_closed()

    stopper = asyncio.create_task(_stop_after_the_links())
    waits = await _run_until(SatelliteWSClient(cfg, _SM()), 8, caplog)
    await stopper
    assert waits[:6] == pytest.approx([base, base * 2, base * 4, base * 4, base * 4, base * 4])
    assert waits[6:8] == pytest.approx([base * 8, base * 16])


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_a_link_that_held_starts_from_the_base_again(monkeypatch, caplog):
    base = 0.02
    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", base)
    monkeypatch.setattr(ws_client, "_STABLE_LINK_S", 0.05)
    monkeypatch.setattr(ws_client.random, "random", lambda: 1.0)
    count = {"n": 0}

    async def _accept_hold_drop(ws):
        await ws.recv()
        await ws.send('{"type": "auth_result", "status": "ok"}')
        count["n"] += 1
        await asyncio.sleep(0.12 if count["n"] >= 3 else 0.0)     # the third link holds
        await ws.close(1011, "dropped")

    async with websockets.serve(_accept_hold_drop, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", cli_bins={},
                              platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)
        waits = await _run_until(SatelliteWSClient(cfg, _SM()), 3, caplog)

    assert waits[:3] == pytest.approx([base, base * 2, base])



@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_the_terminals_report_names_each_terminals_open(monkeypatch, caplog):
    """A platform that takes a terminal back after a restart hears its exit
    and closes it by the open it had: the report names it."""
    reports: list[str] = []

    class _Terminals(_SM):
        pty_sessions = {"t-1": SimpleNamespace(open_token="aa11"), "t-2": SimpleNamespace(open_token="")}

    async def _accept_once(ws):
        await ws.recv()
        await ws.send('{"type": "auth_result", "status": "ok", "report_id": "r-0"}')
        reports.append(await ws.recv())
        await ws.close(1011, "dropped")

    monkeypatch.setattr(ws_client, "_RECONNECT_BACKOFF_BASE", 0.02)
    async with websockets.serve(_accept_once, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = SimpleNamespace(machine_id="m-1", machine_secret="s", cli_bins={},
                              platform_url=f"ws://127.0.0.1:{port}", allow_insecure_transport=False)
        await _run_until(SatelliteWSClient(cfg, _Terminals()), 1, caplog)
    frame = json.loads(reports[0])
    assert frame["session_ids"] == ["t-1", "t-2"] and frame["open_tokens"] == {"t-1": "aa11"}
