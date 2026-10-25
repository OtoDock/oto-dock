"""The proxy core holds its bookkeeping when one piece fails: a reaped
session's seat and slot, a satellite frame that cannot be encoded, the
database liveness probe, a CLI result with no cost, and the sandbox
preflight's account of a missing tap device."""

import asyncio
import logging
import os

import pytest

from core import concurrency
from core.sandbox import sandbox as sandbox_mod

# --- the reapers -------------------------------------------------------------

@pytest.fixture
def released(monkeypatch):
    from services.engines import subscription_pool
    out = {"slot": [], "seat": []}
    monkeypatch.setattr(concurrency, "release", out["slot"].append)
    monkeypatch.setattr(subscription_pool, "release_subscription", out["seat"].append)
    return out


@pytest.mark.asyncio
async def test_a_close_that_raises_still_releases_the_slot_and_the_seat(released, caplog):
    async def boom(sid):
        raise RuntimeError("close failed")
    with caplog.at_level(logging.ERROR):
        await concurrency.reap_session("sid-a", boom)
    assert released == {"slot": ["sid-a"], "seat": ["sid-a"]}
    assert "the close failed" in caplog.text


@pytest.mark.asyncio
async def test_a_slot_release_that_raises_still_releases_the_seat(released, monkeypatch):
    def slot_boom(sid):
        raise RuntimeError("slot")
    monkeypatch.setattr(concurrency, "release", slot_boom)

    async def ok(sid):
        return None
    with pytest.raises(RuntimeError):
        await concurrency.reap_session("sid-b", ok)
    assert released["seat"] == ["sid-b"]


@pytest.mark.asyncio
async def test_a_cancelled_reaper_still_releases(released):
    async def hang(sid):
        await asyncio.sleep(3600)
    task = asyncio.create_task(concurrency.reap_session("sid-c", hang))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released == {"slot": ["sid-c"], "seat": ["sid-c"]}


@pytest.mark.asyncio
async def test_the_direct_reaper_goes_on_past_a_failed_close(released, monkeypatch):
    import time

    from core.layers.direct import session as direct

    class _S:
        last_activity = time.monotonic() - 10_000

    closed = []

    async def close(sid):
        closed.append(sid)
        if sid == "d1":
            raise RuntimeError("mcp close failed")

    async def no_idle(**kw):
        return None

    async def idle_timeout():
        return 60
    monkeypatch.setattr(direct, "_direct_sessions", {"d1": _S(), "d2": _S()})
    monkeypatch.setattr(direct, "has_pending_prompt", lambda sid: False)
    monkeypatch.setattr(direct, "mark_closing", lambda sid: None)
    monkeypatch.setattr(direct.mcp_pool, "close_session", close)
    monkeypatch.setattr(direct.mcp_pool, "reap_idle", no_idle)
    from core.session import session_state
    monkeypatch.setattr(session_state, "cached_idle_timeout", idle_timeout)
    await direct._reap_idle_direct_pass()
    assert closed == ["d1", "d2"]
    assert released == {"slot": ["d1", "d2"], "seat": ["d1", "d2"]}


@pytest.mark.asyncio
async def test_the_codex_reaper_goes_on_past_a_failed_close(released, monkeypatch):
    from core.layers.codex import session as codex
    from core.session import session_state
    closed = []

    async def close(sid):
        closed.append(sid)
        if sid == "c1":
            raise RuntimeError("daemon close failed")

    ticks = []

    async def one_tick(seconds):
        ticks.append(seconds)
        if len(ticks) > 1:
            raise asyncio.CancelledError  # the reaper's second wait ends the test

    async def idle_timeout():
        return 60
    monkeypatch.setattr(codex, "_codex_reap_candidates", lambda now, idle: ["c1", "c2"])
    monkeypatch.setattr(codex, "close_codex_session", close)
    monkeypatch.setattr(session_state, "cached_idle_timeout", idle_timeout)
    monkeypatch.setattr(codex.asyncio, "sleep", one_tick)
    with pytest.raises(asyncio.CancelledError):
        await codex.reap_idle_codex_sessions()
    assert closed == ["c1", "c2"]
    assert released == {"slot": ["c1", "c2"], "seat": ["c1", "c2"]}


# --- a satellite frame that cannot be encoded ---------------------------------

class _Ws:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.asyncio
async def test_an_unencodable_frame_is_dropped_and_its_waiter_fails_at_once():
    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
    )
    mgr = SatelliteConnectionManager()
    gone = []

    async def deregister(*a, **k):
        gone.append(a)
    mgr.deregister = deregister
    ws = _Ws()
    conn = SatelliteConnection(machine_id="m1deadbeef", ws=ws)
    mgr._connections["m1deadbeef"] = conn
    conn.writer_task = asyncio.create_task(mgr._writer_loop(conn))
    try:
        with pytest.raises(RuntimeError, match="could not be encoded"):
            await mgr.send_command("m1deadbeef", {"type": "probe", "bad": object()}, timeout=5)
        await conn.enqueue_send({"type": "ping"})
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        assert [frame for frame in ws.sent if '"ping"' in frame] and gone == []
        assert mgr._connections.get("m1deadbeef") is conn
    finally:
        conn.writer_task.cancel()


# --- the database liveness probe ------------------------------------------------

def test_a_probe_that_raises_lets_the_next_verdict_probe_again(monkeypatch, caplog):
    from storage import pg
    probe = pg._Liveness("postgresql://x", {}, 1.0)
    probe._result = (0.0, 1.0, True)

    def boom():
        raise RuntimeError("unexpected")
    monkeypatch.setattr(probe, "probe", boom)
    probe._running = True
    with pytest.raises(RuntimeError):
        probe._probe_async()
    assert probe._running is False and probe._result == (0.0, 1.0, True)
    assert "liveness probe raised" in caplog.text


# --- a CLI result with no cost -------------------------------------------------

def test_a_result_with_a_null_cost_reads_zero():
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    chunks = ClaudeCLIEventTranslator("sid").feed(
        {"type": "result", "total_cost_usd": None, "duration_ms": 5, "is_error": False,
         "usage": {"input_tokens": 1, "output_tokens": 1}})
    meta = [c.event_data for c in chunks if c.event_type == "metadata"]
    assert meta and meta[0]["cost_usd"] == 0.0


# --- the sandbox preflight and the tap device -----------------------------------

def test_a_missing_tun_device_is_named(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_mod, "_TUN_DEVICE", str(tmp_path / "tun"))
    assert "is missing" in sandbox_mod._tun_unavailable("anything")
    (tmp_path / "tun").write_text("")
    assert "not a character device" in sandbox_mod._tun_unavailable("anything")


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs a character device")
def test_the_probes_error_naming_the_tap_is_the_cause(monkeypatch):
    monkeypatch.setattr(sandbox_mod, "_TUN_DEVICE", "/dev/null")
    detail = "pasta: Failed to open tun socket in namespace"
    assert sandbox_mod._tun_unavailable(detail) == detail
    assert sandbox_mod._tun_unavailable("bwrap: setting up uid map: Permission denied") is None


def test_the_preflight_never_claims_the_sandbox_works_without_the_tap(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(sandbox_mod, "_TUN_DEVICE", str(tmp_path / "missing"))
    with caplog.at_level(logging.WARNING):
        sandbox_mod._warn_nested_probe("pasta: Failed to open tun socket in namespace")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "no session can start in this server's sandbox" in errors[0].getMessage()
    assert "itself works" not in caplog.text
    caplog.clear()
    monkeypatch.setattr(sandbox_mod, "_TUN_DEVICE", "/dev/null")
    with caplog.at_level(logging.WARNING):
        sandbox_mod._warn_nested_probe("bwrap: setting up uid map: Permission denied")
    assert "itself works" in caplog.text and not [r for r in caplog.records if r.levelno == logging.ERROR]
