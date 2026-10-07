"""Every reaper tick reads the idle timeout once, through the cached
off-loop helper (``session_state.cached_idle_timeout``), never the setting
on the loop. The interactive reaper's twin lives in test_interactive_session.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest


def _forbid_on_loop_read(monkeypatch, module):
    def _boom():
        raise AssertionError("the idle timeout was read on the loop")
    monkeypatch.setattr(module, "get_idle_timeout", _boom)


def _one_tick(monkeypatch, module):
    """``asyncio.sleep`` as the loops see it: the first wait returns at
    once, the second ends the loop."""
    calls = {"n": 0}

    async def _sleep(s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)


@pytest.mark.asyncio
async def test_codex_reaper_reads_the_cached_timeout(monkeypatch):
    from core.layers.codex import session as cs
    from core.session import session_state
    seen: list = []
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=7))
    _forbid_on_loop_read(monkeypatch, cs.app_config)
    monkeypatch.setattr(cs, "_codex_reap_candidates", lambda now, t: seen.append(t) or [])
    _one_tick(monkeypatch, cs)
    with pytest.raises(asyncio.CancelledError):
        await cs.reap_idle_codex_sessions()
    assert seen == [7]


@pytest.mark.asyncio
async def test_remote_reaper_reads_the_cached_timeout(monkeypatch):
    import config as app_config
    from core.remote import remote_reaper as rr
    from core.session import session_manager, session_state
    from services.scheduler import run_recovery
    from types import SimpleNamespace
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=7))
    _forbid_on_loop_read(monkeypatch, app_config)
    monkeypatch.setattr(run_recovery, "sweep_expired", AsyncMock())
    layer = SimpleNamespace(_sessions={}, _cm=None)
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    _one_tick(monkeypatch, rr)
    with pytest.raises(asyncio.CancelledError):
        await rr.reap_idle_remote_sessions()
    session_state.cached_idle_timeout.assert_awaited_once()


@pytest.mark.asyncio
async def test_headless_app_sweep_reads_the_cached_timeout(monkeypatch):
    from services.apps import headless_exec as hx
    from core.session import session_state
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=7))
    _forbid_on_loop_read(monkeypatch, hx.config)
    monkeypatch.setattr(hx, "_pool", {})
    await hx._sweep_once()
    session_state.cached_idle_timeout.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_eviction_reads_the_cached_timeout(monkeypatch):
    import config as app_config
    from core.remote import remote_session_start as rss
    from core.session import session_state
    from types import SimpleNamespace
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=7))
    _forbid_on_loop_read(monkeypatch, app_config)
    layer = SimpleNamespace(
        _sessions={}, _cm=SimpleNamespace(machine_at_capacity=lambda m: True))
    evicted = await rss.RemoteSessionStartMixin._evict_idle_on_machine(layer, "m1")
    assert evicted == 0
    session_state.cached_idle_timeout.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_remote_reaper_spares_a_session_waiting_on_a_person(monkeypatch):
    """A remote turn parked on a prompt past the CLI turn ceiling is not a
    wedge: the prompt's own wait (three days) bounds it."""
    import time
    import config as app_config
    from core.remote import remote_reaper as rr
    from core.session import session_manager, session_state
    from services.scheduler import run_recovery
    from types import SimpleNamespace
    monkeypatch.setattr(session_state, "cached_idle_timeout", AsyncMock(return_value=7))
    monkeypatch.setattr(run_recovery, "sweep_expired", AsyncMock())
    closed: list[str] = []
    old = time.monotonic() - app_config.CLAUDE_TIMEOUT - 60

    class _Cm:
        def is_connected(self, mid):
            return True

        def is_session_in_grace(self, mid, sid):
            return False

    async def close_session(sid):
        closed.append(sid)

    layer = SimpleNamespace(
        _sessions={"parked": SimpleNamespace(last_activity=old, machine_id="m", turn_active=True),
                   "idle": SimpleNamespace(last_activity=old, machine_id="m", turn_active=False)},
        _cm=_Cm(), close_session=close_session,
        probe_session_process_dead=AsyncMock(return_value=False))
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: layer)
    monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: sid == "parked")
    _one_tick(monkeypatch, rr)
    with pytest.raises(asyncio.CancelledError):
        await rr.reap_idle_remote_sessions()
    assert closed == ["idle"]
