"""A session with tracked background work is not idle: the local, Codex and
remote reapers and the RAM evictor spare it up to the background-work
ceiling, then close it anyway; a task run records what it left running."""

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

import config
from core.concurrency import _oldest_evictable_local
from core.events.bg_command_state import (
    _bg_command_registries,
    get_bg_command_registry,
    peek_bg_command_registry,
)
from core.layers.cli import session as cli_session
from core.layers.cli.session import PersistentSession, _persistent_sessions, _reap_idle_pass
from core.layers.codex import session as codex_session
from core.remote.remote_reaper import _idle_session_has_pending_work
from core.session import background_leash
from core.session.session_state import (
    _subagent_registries,
    get_subagent_registry,
    peek_subagent_registry,
)


@pytest.fixture
def registries():
    saved_bg, saved_sub = dict(_bg_command_registries), dict(_subagent_registries)
    _bg_command_registries.clear()
    _subagent_registries.clear()
    yield
    _bg_command_registries.clear()
    _bg_command_registries.update(saved_bg)
    _subagent_registries.clear()
    _subagent_registries.update(saved_sub)


def _running_command(sid: str, task_id: str = "bash-1") -> None:
    reg = get_bg_command_registry(sid)
    reg.spawned.add(task_id)
    reg._refresh()


def _running_subagent(sid: str, task_id: str = "agent-1") -> None:
    reg = get_subagent_registry(sid)
    reg.spawned.add(task_id)
    reg._refresh()


class TestLeash:
    def test_peek_never_creates_a_registry(self, registries):
        assert peek_bg_command_registry("nope") is None
        assert peek_subagent_registry("nope") is None
        assert background_leash.pending_background("nope") == (0, 0)
        assert "nope" not in _bg_command_registries and "nope" not in _subagent_registries

    def test_reason_names_the_running_work_until_the_ceiling(self, registries, monkeypatch):
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 100)
        _running_command("s1")
        _running_subagent("s1")
        assert background_leash.pending_summary("s1") == (
            "1 background command(s) + 1 background subagent(s)")
        assert background_leash.spare_reason("s1", 50) == (
            "1 background command(s) + 1 background subagent(s)")
        # Past the ceiling the session goes anyway.
        assert background_leash.spare_reason("s1", 101) == ""
        # Nothing running: nothing to spare.
        assert background_leash.spare_reason("s2", 50) == ""
        # A completion clears it.
        get_bg_command_registry("s1").completed.add("bash-1")
        get_subagent_registry("s1").completed.add("agent-1")
        assert background_leash.pending_summary("s1") == ""


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


def _mk_session(idle_s: float) -> PersistentSession:
    s = PersistentSession(
        session_id=f"sess-{uuid.uuid4().hex[:12]}", agent_prompt=None,
        mcp_config_path=None, model="claude-opus-5", agent_name="agent",
    )
    s._started = True
    s.proc = _FakeProc()
    now = time.monotonic()
    s._created = now
    s.last_activity = now - idle_s
    return s


@pytest.fixture
def pool():
    saved = dict(_persistent_sessions)
    _persistent_sessions.clear()
    yield _persistent_sessions
    _persistent_sessions.clear()
    _persistent_sessions.update(saved)


@pytest.fixture
def ledger(monkeypatch, pool):
    """The admission ledger with deterministic gates (room for N HEAVY of
    1000 MB via ``ledger["budget"](n)``) and a CLI layer whose close pops the
    pool and releases the slot, like the real one."""
    from core import concurrency as C
    from core.session import session_manager
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)
    for name, value in (("_sessions", {}), ("_session_est", {}), ("_session_added_at", {}),
                        ("_session_owner", {}), ("_reserved_mb", 0), ("_parked_tasks", 0),
                        ("_budget_mb", 1000), ("_floor_mb", 100)):
        monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(C, "_cond", asyncio.Condition())
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)
    closed: list[str] = []

    class _Layer:
        async def close_session(self, sid):
            closed.append(sid)
            pool.pop(sid, None)
            C.release(sid)

    monkeypatch.setattr(session_manager, "get_layer_by_path", lambda path: _Layer())

    def budget(n_heavy: int) -> None:
        monkeypatch.setattr(C, "_budget_mb", 1000 * n_heavy)

    async def hold(session, owner: str = "") -> None:
        pool[session.session_id] = session
        assert await C.acquire(session.session_id, "chat", user_sub=owner or None)
        C._session_added_at[session.session_id] -= 200  # past the grow-in window

    return {"closed": closed, "budget": budget, "hold": hold}


class TestLocalReaper:
    @pytest.mark.asyncio
    async def test_running_command_keeps_the_session(self, pool, registries, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = _mk_session(idle_s=60)
        pool[s.session_id] = s
        _running_command(s.session_id)
        await _reap_idle_pass()
        assert s.session_id in pool and not s._closed

    @pytest.mark.asyncio
    async def test_past_the_ceiling_the_session_is_reaped(self, pool, registries, monkeypatch):
        monkeypatch.setattr(cli_session.config, "get_idle_timeout", lambda: 10)
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 30)
        s = _mk_session(idle_s=60)
        pool[s.session_id] = s
        _running_subagent(s.session_id)
        await _reap_idle_pass()
        assert s.session_id not in pool

    @pytest.mark.asyncio
    async def test_dead_process_is_reaped_whatever_the_registry_says(self, pool, registries, monkeypatch):
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = _mk_session(idle_s=1)
        s.proc.returncode = 1
        pool[s.session_id] = s
        _running_command(s.session_id)
        await _reap_idle_pass()
        assert s.session_id not in pool


class TestCodexReaper:
    def test_candidates_spare_a_running_terminal(self, registries, monkeypatch):
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        monkeypatch.setattr(codex_session, "has_pending_question", lambda sid: False)

        class _S:
            is_alive = True
            last_activity = time.monotonic() - 100

        monkeypatch.setattr(codex_session, "_codex_sessions", {"c1": _S(), "c2": _S()})
        _running_command("c1")
        assert codex_session._codex_reap_candidates(time.monotonic(), 10) == ["c2"]
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 50)
        assert sorted(codex_session._codex_reap_candidates(time.monotonic(), 10)) == ["c1", "c2"]


class TestRemoteReaper:
    def test_background_command_leg_is_opt_in(self, registries, monkeypatch):
        monkeypatch.setattr("core.remote.remote_reaper.get_hook_activity", lambda sid: 0, raising=False)
        now = time.monotonic()
        _running_command("r1")
        assert _idle_session_has_pending_work("r1", now, 10) is False
        assert _idle_session_has_pending_work("r1", now, 10, bg_commands=True) is True
        _running_subagent("r2")
        assert _idle_session_has_pending_work("r2", now, 10) is True


@pytest.mark.usefixtures("temp_db")
class TestRunRecord:
    def test_a_run_records_what_it_left_running(self):
        from storage import database as task_store
        task_store.create_run("run-bg-1", "dyn-1", "briefer", "schedule", None, "do it")
        task_store.update_run("run-bg-1", status="completed", background_pending=2)
        (run,) = [r for r in task_store.list_runs(limit=5) if r["id"] == "run-bg-1"]
        assert run["background_pending"] == 2
        assert run["status"] == "completed"


class TestEvictor:
    @pytest.mark.asyncio
    async def test_pending_sessions_are_skipped_inside_the_leash(self, pool, registries, monkeypatch):
        from core import concurrency as C
        busy, free = _mk_session(idle_s=500), _mk_session(idle_s=400)
        pool[busy.session_id] = busy
        pool[free.session_id] = free
        monkeypatch.setattr(C, "_sessions", {busy.session_id: "chat", free.session_id: "chat"})
        _running_command(busy.session_id)
        scan = await _oldest_evictable_local(60, pending_leash_s=1000)
        assert scan.victim is not None and scan.victim[0] == free.session_id
        assert scan.spared_background == 1
        # Without a leash the most idle one goes, running work or not.
        scan = await _oldest_evictable_local(60)
        assert scan.victim is not None and scan.victim[0] == busy.session_id
        assert scan.spared_background == 0
        # Past the leash the busy one is a candidate again.
        scan = await _oldest_evictable_local(60, pending_leash_s=450)
        assert scan.victim is not None and scan.victim[0] == busy.session_id
        assert scan.spared_background == 0

    @pytest.mark.asyncio
    async def test_live_turns_are_skipped_inside_the_leash(self, pool, registries, monkeypatch):
        from core import concurrency as C
        from core.layers.codex import session as codex_session
        from core.layers.direct import session as direct_session
        from core.layers.direct.layer import DirectLLMExecutionLayer
        from core.session import interactive_session
        now = time.monotonic()
        cli = _mk_session(idle_s=500)
        cli._turn_active = True
        pool[cli.session_id] = cli
        codex = SimpleNamespace(last_activity=now - 500, _current_turn_id="turn-1")
        direct = SimpleNamespace(last_activity=now - 500)
        pty = SimpleNamespace(last_activity=now - 500, turn_open=False, question_parked=True)
        monkeypatch.setattr(codex_session, "_codex_sessions", {"cx": codex})
        monkeypatch.setattr(direct_session, "_direct_sessions", {"dx": direct})
        monkeypatch.setattr(interactive_session, "live_session_ids",
                            lambda local_only=False: {"px"})
        monkeypatch.setattr(interactive_session, "get", lambda sid: pty if sid == "px" else None)
        streaming = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(DirectLLMExecutionLayer._active_streams, "dx", streaming)
        monkeypatch.setattr(C, "_sessions", {cli.session_id: "chat", "cx": "chat",
                                             "dx": "chat", "px": "chat"})
        scan = await _oldest_evictable_local(60, turn_leash_s=1000)
        assert scan.victim is None and scan.spared_live == 4
        # A turn silent past the leash is reclaimable again.
        scan = await _oldest_evictable_local(60, turn_leash_s=450)
        assert scan.victim is not None and scan.spared_live == 0
        # A Direct stream that finished is no live turn.
        streaming.set_result(None)
        scan = await _oldest_evictable_local(60, turn_leash_s=1000)
        assert scan.victim is not None and scan.victim[0] == "dx" and scan.spared_live == 3
        # A Codex session with no turn but a question parked on a person is live.
        codex._current_turn_id = None
        monkeypatch.setattr("core.session.session_state.has_pending_question",
                            lambda sid: sid == "cx")
        streaming2 = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(DirectLLMExecutionLayer._active_streams, "dx", streaming2)
        scan = await _oldest_evictable_local(60, turn_leash_s=1000)
        assert scan.victim is None and scan.spared_live == 4

    @pytest.mark.asyncio
    async def test_only_chat_kind_is_a_candidate(self, pool, registries, monkeypatch):
        from core import concurrency as C
        task, meeting, phone = (_mk_session(idle_s=900) for _ in range(3))
        chat = _mk_session(idle_s=400)
        for s in (task, meeting, phone, chat):
            pool[s.session_id] = s
        kinds = {task.session_id: "task", meeting.session_id: "meeting",
                 phone.session_id: "phone", chat.session_id: "chat"}
        monkeypatch.setattr(C, "_sessions", kinds)
        scan = await _oldest_evictable_local(60)
        assert scan.victim is not None and scan.victim[0] == chat.session_id
        del kinds[chat.session_id]
        scan = await _oldest_evictable_local(60)
        assert scan.victim is None

    @pytest.mark.asyncio
    async def test_hook_activity_counts_as_activity(self, pool, registries, monkeypatch):
        from core import concurrency as C
        from core.session import session_state
        s = _mk_session(idle_s=500)
        pool[s.session_id] = s
        monkeypatch.setattr(C, "_sessions", {s.session_id: "chat"})
        monkeypatch.setitem(session_state._session_hook_activity, s.session_id,
                            time.monotonic() - 10)
        scan = await _oldest_evictable_local(60)
        assert scan.victim is None
        monkeypatch.setitem(session_state._session_hook_activity, s.session_id,
                            time.monotonic() - 100)
        scan = await _oldest_evictable_local(60)
        assert scan.victim is not None and scan.victim[0] == s.session_id

    @pytest.mark.asyncio
    async def test_unclaimed_prewarm_is_a_candidate_at_any_age_for_interactive_admits_only(
            self, pool, registries, monkeypatch):
        from core import concurrency as C
        from core.session import prewarm_session_registry as pw
        fresh = _mk_session(idle_s=5)
        pool[fresh.session_id] = fresh
        monkeypatch.setattr(C, "_sessions", {fresh.session_id: "chat"})
        monkeypatch.setitem(pw._entries, fresh.session_id, pw._Entry(
            agent="agent", user_sub="u1", role="manager", exec_path="", ts=time.monotonic()))
        # A parked task or wake keeps the floor for a person's pre-warm.
        scan = await _oldest_evictable_local(60)
        assert scan.victim is None
        scan = await _oldest_evictable_local(60, prewarm_any_age=True)
        assert scan.victim == (fresh.session_id, "cli", True)

    @pytest.mark.asyncio
    async def test_maintenance_pass_evicts_an_idle_chat_never_a_task(
            self, pool, registries, ledger, monkeypatch):
        from core import concurrency as C
        ledger["budget"](2)
        task_run, idle_chat = _mk_session(idle_s=5000), _mk_session(idle_s=400)
        pool[task_run.session_id] = task_run
        assert await C.acquire(task_run.session_id, "task", blocking=True)
        await ledger["hold"](idle_chat)
        monkeypatch.setattr(C, "_parked_tasks", 1)
        await C._maintenance_pass()
        assert ledger["closed"] == [idle_chat.session_id]
        await C._maintenance_pass()
        assert ledger["closed"] == [idle_chat.session_id]  # the task run is never a candidate

    @pytest.mark.asyncio
    async def test_interactive_admit_spares_a_live_turn_until_the_leash(
            self, pool, registries, ledger):
        from core import concurrency as C
        mid_turn = _mk_session(idle_s=400)
        mid_turn._turn_active = True
        await ledger["hold"](mid_turn)
        adm = await C.acquire("newcomer", "chat")
        assert not adm and adm.reason == "busy" and ledger["closed"] == []
        # Silent past the leash, the turn is reclaimable.
        mid_turn.last_activity = time.monotonic() - 1200
        assert await C.acquire("newcomer", "chat")
        assert ledger["closed"] == [mid_turn.session_id]

    @pytest.mark.asyncio
    async def test_prefer_user_reads_the_ledger_owner(self, pool, registries, ledger):
        # CLI sessions carry no user_sub attribute: the ledger's owner orders
        # the requester's own idle session before another person's more idle one.
        from core import concurrency as C
        ledger["budget"](2)
        alice_s, bob_s = _mk_session(idle_s=400), _mk_session(idle_s=900)
        await ledger["hold"](alice_s, "alice")
        await ledger["hold"](bob_s, "bob")
        assert await C.acquire("alice-new", "chat", user_sub="alice")
        assert ledger["closed"] == [alice_s.session_id]
        scan = await _oldest_evictable_local(60, only_user="bob")
        assert scan.victim is not None and scan.victim[0] == bob_s.session_id

    @pytest.mark.asyncio
    async def test_per_user_cap_evicts_own_idle_session_first(
            self, pool, registries, ledger, monkeypatch):
        from core import concurrency as C
        monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 2)
        ledger["budget"](10)
        own_old, own_new, other = (_mk_session(idle_s=500), _mk_session(idle_s=400),
                                   _mk_session(idle_s=900))
        await ledger["hold"](own_old, "alice")
        await ledger["hold"](own_new, "alice")
        await ledger["hold"](other, "bob")
        assert await C.acquire("alice-3", "chat", user_sub="alice")
        # One of her own went (the most idle), nobody else's; she holds two.
        assert ledger["closed"] == [own_old.session_id]
        assert C._owned("alice") == 2 and other.session_id in C._sessions
        # Her sessions all fresh: the cap sentence, nothing closed.
        fresh = _mk_session(idle_s=1)
        pool.pop(own_new.session_id)
        C.release(own_new.session_id)
        await ledger["hold"](fresh, "alice")
        adm = await C.acquire("alice-4", "chat", user_sub="alice")
        assert not adm and adm.reason == "user_cap"
        assert "You already have 2 sessions running" in adm.user_message
        assert ledger["closed"] == [own_old.session_id]
