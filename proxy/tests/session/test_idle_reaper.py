"""Idle-reaper safety around session startup (the 2026-09-01 task race).

A persistent session sits in the pool with ``_started=False`` between the
pool insert and the end of ``start()``. The reaper's dead-session check
(``not is_alive``) used to match that window and could kill a session
mid-birth — observed live on the internal VM when the 60s reaper tick
landed inside a task cron's session spawn ("Session not found" after
319ms). These tests pin the fixed semantics of ``_reap_idle_pass``:

* never reap an un-started session inside the startup grace window;
* still collect an un-started entry stranded past the grace (crashed
  ``start()`` with no cleanup);
* keep reaping dead-process and idle-timeout sessions;
* a failing ``start()`` in ``get_or_create_persistent_session`` removes the
  pool entry and releases the chat slot + subscription (the reaper can no
  longer see the entry, so the creator must clean up).
"""

import asyncio
import time
import uuid

import pytest

from core.layers.cli import session as cli_session
from core.sandbox import pty_relay
from core.layers.cli.session import (
    PersistentSession,
    _persistent_sessions,
    _reap_idle_pass,
    _STARTUP_REAP_GRACE_S,
    get_or_create_persistent_session,
)


class _FakeStdin:
    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = None
        self.stderr = None
        self.returncode: int | None = None
        self.pid = 4242

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _mk_session(*, started: bool, proc_alive: bool | None = None,
                created_ago_s: float = 0.0, idle_s: float = 0.0) -> PersistentSession:
    s = PersistentSession(
        session_id=f"sess-{uuid.uuid4().hex[:12]}",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-5",
        agent_name="agent",
    )
    s._started = started
    if proc_alive is not None:
        s.proc = _FakeProc()
        if not proc_alive:
            s.proc.returncode = 1
    now = time.monotonic()
    s._created = now - created_ago_s
    s.last_activity = now - idle_s
    return s


@pytest.fixture
def pool():
    """Isolated pool: snapshot + restore the module-global session dict."""
    saved = dict(_persistent_sessions)
    _persistent_sessions.clear()
    yield _persistent_sessions
    _persistent_sessions.clear()
    _persistent_sessions.update(saved)


class TestReapPass:
    @pytest.mark.asyncio
    async def test_starting_session_survives_the_pass(self, pool):
        s = _mk_session(started=False)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id in pool
        assert not s._closed

    @pytest.mark.asyncio
    async def test_stranded_unstarted_entry_reaped_past_grace(self, pool):
        s = _mk_session(started=False, created_ago_s=_STARTUP_REAP_GRACE_S + 1)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_dead_process_still_reaped(self, pool):
        s = _mk_session(started=True, proc_alive=False)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_idle_timeout_still_reaped(self, pool, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        s = _mk_session(started=True, proc_alive=True, idle_s=11)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_active_session_untouched(self, pool, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        s = _mk_session(started=True, proc_alive=True, idle_s=1)
        pool[s.session_id] = s
        await _reap_idle_pass()
        assert s.session_id in pool
        assert not s._closed


class TestStartFailureCleanup:
    @pytest.mark.asyncio
    async def test_failed_start_pops_entry_and_releases(self, pool, monkeypatch):
        released = {"slot": None, "sub": None}

        async def boom(self):
            raise OSError("spawn failed")

        monkeypatch.setattr(PersistentSession, "start", boom)
        monkeypatch.setattr("core.concurrency.release_chat_slot",
                            lambda sid: released.__setitem__("slot", sid))
        monkeypatch.setattr("services.engines.subscription_pool.release_subscription",
                            lambda sid: released.__setitem__("sub", sid))

        sid = f"sess-{uuid.uuid4().hex[:12]}"
        with pytest.raises(OSError):
            await get_or_create_persistent_session(
                session_id=sid, agent_prompt=None, mcp_config_path=None,
                model="claude-opus-5", agent_name="agent",
            )
        assert sid not in pool
        assert released == {"slot": sid, "sub": sid}

    @pytest.mark.asyncio
    async def test_close_during_start_kills_fresh_process(self, monkeypatch):
        """The in-start guard: close() flipping _closed while the spawn is in
        flight makes start() kill the fresh process and raise, not leak it."""
        s = _mk_session(started=False)
        killed = []

        async def fake_spawn(argv, **k):
            s._closed = True  # closed while the spawn was in flight
            return _FakeProc()

        async def fake_kill(proc, sid):
            killed.append(sid)

        monkeypatch.setattr(pty_relay, "spawn_piped", fake_spawn)
        monkeypatch.setattr(cli_session, "_kill_process", fake_kill)
        monkeypatch.setattr(PersistentSession, "build_spawn_command",
                            lambda self: (["claude"], {}, "/tmp"))
        with pytest.raises(RuntimeError, match="closed during start"):
            await s.start()
        assert killed == [s.session_id]
        assert not s.is_alive

    @pytest.mark.asyncio
    async def test_a_close_before_the_spawn_ends_the_start_without_a_process(self, monkeypatch):
        """A session closed while it waited for a spawn slot never spawns."""
        s = _mk_session(started=False)
        s._closed = True
        spawned = []

        async def fake_spawn(argv, **k):
            spawned.append(argv)
            return _FakeProc()

        monkeypatch.setattr(pty_relay, "spawn_piped", fake_spawn)
        monkeypatch.setattr(PersistentSession, "build_spawn_command",
                            lambda self: (["claude"], {}, "/tmp"))
        with pytest.raises(RuntimeError, match="closed during start"):
            await s.start()
        assert spawned == []

    @pytest.mark.asyncio
    async def test_the_spawn_is_niced_off_the_loop_in_a_slot(self, monkeypatch):
        s = _mk_session(started=False)
        seen = {}

        async def fake_spawn(argv, *, cwd, env, limit):
            seen.update(argv=argv, limit=limit, slots=pty_relay._spawn_semaphore()._value)
            return _FakeProc()

        monkeypatch.setattr(pty_relay, "spawn_piped", fake_spawn)
        monkeypatch.setattr(PersistentSession, "build_spawn_command",
                            lambda self: (["claude", "-p"], {}, "/tmp"))
        pty_relay._spawn_slots.clear()
        full = pty_relay._spawn_semaphore()._value
        await s.start()
        assert seen["argv"] == [*pty_relay.nice_prefix(), "claude", "-p"]
        assert seen["limit"] == 200 * 1024 * 1024
        assert seen["slots"] == full - 1         # the spawn ran inside a slot
        assert s.is_alive


class TestStartingSessionsInThePool:
    """The spawn slot makes the window between pool registration and spawn
    seconds long: the pool code treats a session still starting as neither
    dead nor usable."""

    @pytest.mark.asyncio
    async def test_a_second_get_or_create_during_the_start_returns_the_same_session(
            self, pool, monkeypatch):
        gate = asyncio.Event()
        spawns = []

        async def gated_spawn(argv, **k):
            spawns.append(argv)
            await gate.wait()
            return _FakeProc()

        monkeypatch.setattr(pty_relay, "spawn_piped", gated_spawn)
        monkeypatch.setattr(PersistentSession, "build_spawn_command",
                            lambda self: (["claude"], {}, "/tmp"))
        monkeypatch.setattr(cli_session, "_record_session_use", lambda *a, **k: None)
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        kw = dict(session_id=sid, agent_prompt=None, mcp_config_path=None,
                  model="claude-opus-5", agent_name="agent")
        first = asyncio.create_task(get_or_create_persistent_session(**kw))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(get_or_create_persistent_session(**kw))
        await asyncio.sleep(0.05)
        gate.set()
        a, b = await asyncio.gather(first, second)
        assert a is b and len(spawns) == 1

    @pytest.mark.asyncio
    async def test_get_persistent_session_keeps_a_starting_session(self, pool):
        s = _mk_session(started=False)
        pool[s.session_id] = s
        assert await cli_session.get_persistent_session(s.session_id) is None
        assert pool[s.session_id] is s

    @pytest.mark.asyncio
    async def test_an_abort_while_starting_ends_the_start(self, pool, monkeypatch):
        s = _mk_session(started=False)
        pool[s.session_id] = s
        await cli_session.abort_persistent_session(s.session_id)
        assert s._closed and s.session_id not in pool


# ---------------------------------------------------------------------------
# A reaper tick reads the idle timeout once, off the loop, cached
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_idle_cache(monkeypatch):
    from core.session import session_state
    monkeypatch.setattr(session_state, "_idle_timeout_cache", None)


class TestIdleTimeoutRead:
    @pytest.mark.asyncio
    async def test_one_read_per_tick_off_the_loop(self, pool, monkeypatch, fresh_idle_cache):
        import threading
        loop_thread = threading.get_ident()
        reads = []

        def counting():
            reads.append(threading.get_ident())
            return 900

        monkeypatch.setattr(cli_session.config, "get_idle_timeout", counting)
        for _ in range(50):
            s = _mk_session(started=True, proc_alive=True, idle_s=1)
            pool[s.session_id] = s
        await _reap_idle_pass()
        await _reap_idle_pass()           # the second tick is served by the cache
        assert len(reads) == 1
        assert reads[0] != loop_thread
        assert len(pool) == 50

    @pytest.mark.asyncio
    async def test_a_patched_getter_is_never_served_a_stale_value(self, monkeypatch, fresh_idle_cache):
        from core.session import session_state
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 111)
        assert await session_state.cached_idle_timeout() == 111
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 222)
        assert await session_state.cached_idle_timeout() == 222

    @pytest.mark.asyncio
    async def test_a_failed_read_is_never_cached(self, monkeypatch, fresh_idle_cache):
        from core.session import session_state
        from storage import database as db
        calls = {"n": 0}

        def flaky(key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database down")
            return "1234"

        monkeypatch.setattr(db, "get_platform_setting", flaky)
        assert await session_state.cached_idle_timeout() == cli_session.config.PERSISTENT_SESSION_TIMEOUT
        assert await session_state.cached_idle_timeout() == 1234   # read again, not cached
        monkeypatch.setattr(db, "get_platform_setting", lambda key: (_ for _ in ()).throw(RuntimeError("down")))
        assert await session_state.cached_idle_timeout() == 1234   # the cache serves it

    @pytest.mark.asyncio
    async def test_the_direct_reaper_reads_once_per_tick(self, monkeypatch, fresh_idle_cache):
        from core.layers.direct import session as direct_session
        reads = []
        monkeypatch.setattr(cli_session.config, "get_idle_timeout",
                            lambda: reads.append(1) or 5)
        reaped_mcps = []

        async def _reap_idle(timeout=0):
            reaped_mcps.append(timeout)

        monkeypatch.setattr(direct_session.mcp_pool, "reap_idle", _reap_idle)
        saved = dict(direct_session._direct_sessions)
        direct_session._direct_sessions.clear()
        try:
            now = time.monotonic()
            for i in range(20):
                direct_session._direct_sessions[f"d{i}"] = type(
                    "S", (), {"last_activity": now})()
            await direct_session._reap_idle_direct_pass()
        finally:
            direct_session._direct_sessions.clear()
            direct_session._direct_sessions.update(saved)
        assert reads == [1]
        assert reaped_mcps == [5]
