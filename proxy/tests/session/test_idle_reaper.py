"""The idle timeout a reaper tick reads (``session_state.cached_idle_timeout``):
read once, off the loop, cached; a failed read is never cached; Direct LLM's
reaper reads it once per tick. The reaper of the sessions on machines is in
``tests/remote/test_remote_reaper.py`` and ``test_reaper_idle_timeout.py``.
"""

import time

import psycopg
import pytest

import config


@pytest.fixture
def fresh_idle_cache(monkeypatch):
    from core.session import session_state
    monkeypatch.setattr(session_state, "_idle_timeout_cache", None)


class TestIdleTimeoutRead:
    @pytest.mark.asyncio
    async def test_one_read_off_the_loop_serves_the_next_tick_too(self, monkeypatch, fresh_idle_cache):
        import threading

        from core.session import session_state
        loop_thread = threading.get_ident()
        reads = []

        def counting():
            reads.append(threading.get_ident())
            return 900

        monkeypatch.setattr(config, "get_idle_timeout", counting)
        assert await session_state.cached_idle_timeout() == 900
        assert await session_state.cached_idle_timeout() == 900   # the second tick is served by the cache
        assert len(reads) == 1
        assert reads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_a_patched_getter_is_never_served_a_stale_value(self, monkeypatch, fresh_idle_cache):
        from core.session import session_state
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 111)
        assert await session_state.cached_idle_timeout() == 111
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 222)
        assert await session_state.cached_idle_timeout() == 222

    @pytest.mark.asyncio
    async def test_a_failed_read_is_never_cached(self, monkeypatch, fresh_idle_cache):
        from core.session import session_state
        from storage import database as db
        calls = {"n": 0}

        def flaky(key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.OperationalError("database down")
            return "1234"

        monkeypatch.setattr(db, "get_platform_setting", flaky)
        assert await session_state.cached_idle_timeout() == config.PERSISTENT_SESSION_TIMEOUT
        assert await session_state.cached_idle_timeout() == 1234   # read again, not cached
        monkeypatch.setattr(db, "get_platform_setting", lambda key: (_ for _ in ()).throw(psycopg.OperationalError("down")))
        assert await session_state.cached_idle_timeout() == 1234   # the cache serves it

    @pytest.mark.asyncio
    async def test_the_direct_reaper_reads_once_per_tick(self, monkeypatch, fresh_idle_cache):
        from core.layers.direct import session as direct_session
        reads = []
        monkeypatch.setattr(config, "get_idle_timeout",
                            lambda: reads.append(1) or 5)
        reaped_mcps = []

        async def _reap_idle(timeout=0, *, keep=frozenset()):
            reaped_mcps.append(timeout)

        monkeypatch.setattr(direct_session.mcp_pool, "reap_idle", _reap_idle)
        saved = dict(direct_session._direct_sessions)
        direct_session._direct_sessions.clear()
        try:
            now = time.monotonic()
            for i in range(20):
                direct_session._direct_sessions[f"d{i}"] = type(
                    "S", (), {"last_activity": now, "mcp_manager": None})()
            await direct_session._reap_idle_direct_pass()
        finally:
            direct_session._direct_sessions.clear()
            direct_session._direct_sessions.update(saved)
        assert reads == [1]
        assert reaped_mcps == [5]
