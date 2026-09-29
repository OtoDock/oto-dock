"""Periodic loops never touch the DB on the event loop (storage/pg.py rule):
the weekly MCP auto-update gate, the webhook subscription renewer tick and the
pre-warm reaper's layer resolution all run their store calls on the DB
executor. Each is exercised with the loop guard ARMED around the call."""

import threading
import time

import pytest


@pytest.mark.asyncio
async def test_mcp_autoupdate_gate_reads_off_loop(loop_db_guard, monkeypatch):
    from services.mcp import mcp_autoupdate
    gate_threads: list[int] = []

    def _is_due(now):
        gate_threads.append(threading.get_ident())
        return (False, False)   # never due → nothing launched

    monkeypatch.setattr(mcp_autoupdate, "_is_due", _is_due)
    with loop_db_guard.active():
        await mcp_autoupdate.maybe_run_weekly()   # real enabled-read + gate, off-loop
    assert gate_threads and gate_threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_subscription_renewer_tick_reads_off_loop(loop_db_guard):
    from services.webhooks import subscription_renewer
    with loop_db_guard.active():
        await subscription_renewer._renew_tick()   # empty table → no-op, off-loop


@pytest.mark.asyncio
async def test_prewarm_reaper_touches_no_db(loop_db_guard, monkeypatch):
    # The reaper used to resolve the layer through get_execution_layer (agent
    # row + machine row + platform settings) and had to push that off the
    # loop. It now asks the registry which layer HOLDS the session, so there
    # is no DB read left to offload — and nothing to raise for a deleted
    # agent, which previously stranded the concurrency slot and the
    # subscription seat permanently.
    from core.session import prewarm_session_registry as reg
    from core.session import session_manager

    closed = []

    class _Layer:
        async def close_session(self, sid):
            closed.append(sid)

    monkeypatch.setattr(session_manager, "find_layer_for_session",
                        lambda sid: _Layer())
    reg._entries.clear()
    reg._entries["sid-1"] = reg._Entry(
        user_sub="user-admin", agent="nope", model="m", role="manager",
        exec_path="claude-code-cli", ts=time.monotonic() - 10_000,
    ) if hasattr(reg, "_Entry") else _stub_entry()
    with loop_db_guard.active():
        reaped = await reg.reap_stale(ttl=1.0)
    assert reaped == 1 and closed == ["sid-1"]


def _stub_entry():
    from types import SimpleNamespace
    return SimpleNamespace(user_sub="user-admin", agent="nope", model="m",
                           role="manager", exec_path="claude-code-cli",
                           ts=time.monotonic() - 10_000)
