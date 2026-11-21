"""A session with tracked background work is not idle: the reaper of the
sessions on machines and the RAM evictor spare it up to the background-work
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
from core.remote.remote_reaper import (
    KEEP,
    REAP,
    _idle_session_has_pending_work,
    _verdict,
)
from core.remote.remote_session_info import RemoteSessionInfo
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


MACHINE = "c-1"
GIB = 1 << 30


class _Manager:
    """The connection manager as admission and the liveness lookup ask it:
    one compute machine beside the platform, connected, with memory to
    spare."""

    def is_connected(self, machine_id: str) -> bool:
        return machine_id == MACHINE

    def is_compute(self, machine_id: str) -> bool:
        return machine_id == MACHINE

    def owns_copy(self, machine_id: str) -> bool:
        return False

    def machine_memory_report(self, machine_id: str, compute: bool = False):
        if machine_id != MACHINE:
            return None
        return {"total_bytes": 16 * GIB, "available_bytes": 12 * GIB,
                "swap_free_bytes": 0, "capped": False}, 1.0


class _Remote:
    """The remote layer as admission's scan and its evictor ask it: the
    records it holds on the compute machine, none in a reconnect grace, and
    a close that drops the record and releases the slot, like the real one."""

    def __init__(self) -> None:
        self._sessions: dict[str, RemoteSessionInfo] = {}
        self.closed: list[str] = []

    def owns_session(self, sid: str) -> bool:
        return sid in self._sessions

    def machine_of_session(self, sid: str) -> str:
        info = self._sessions.get(sid)
        return info.machine_id if info is not None else ""

    def is_session_grace_held(self, sid: str) -> bool:
        return False

    async def close_session(self, sid: str) -> None:
        from core import concurrency as C
        self.closed.append(sid)
        self._sessions.pop(sid, None)
        C.release(sid)


def _set_ledger(monkeypatch, name, value):
    """The session ledger lives in ``concurrency_scan``, re-exported by the same
    object: a test that swaps it swaps both bindings."""
    from core import concurrency as C, concurrency_scan
    monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(concurrency_scan, name, value)


def _mk_session(idle_s: float, *, execution_path: str = "claude-code-cli") -> RemoteSessionInfo:
    """The record of a headless session the compute machine runs, between
    turns and silent for ``idle_s``."""
    info = RemoteSessionInfo(
        session_id=f"sess-{uuid.uuid4().hex[:12]}", machine_id=MACHINE, agent_name="agent",
        execution_path=execution_path, event_queue=asyncio.Queue(), compute=True,
    )
    info.last_activity = time.monotonic() - idle_s
    return info


@pytest.fixture(autouse=True)
def _no_hook_is_never(monkeypatch):
    # A session with no hook activity reads 0 (a real stamp is a positive
    # time.monotonic()). These tests backdate sessions from time.monotonic(),
    # which counts from boot: on a host up for minutes (a fresh CI runner) the
    # stamps go below 0, and the idle age max(last_activity, hook) would take
    # the 0 for newer activity. Here "no hook" is minus infinity.
    from core.session import session_state
    monkeypatch.setattr(session_state, "get_hook_activity",
                        lambda sid: session_state._session_hook_activity.get(sid, float("-inf")))


@pytest.fixture
def remote(monkeypatch):
    """The remote layer's stand-in, holding nothing, on a connected compute
    machine."""
    from core.remote import satellite_connection
    from core.session import session_manager
    layer = _Remote()
    manager = _Manager()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(session_manager, "_remote_layer", layer)
    return layer


@pytest.fixture
def pool(remote):
    """The sessions the compute machine runs, by id."""
    return remote._sessions


@pytest.fixture
def ledger(monkeypatch, remote, pool):
    """The admission ledger with deterministic gates (room for N HEAVY of
    1000 MB via ``ledger["budget"](n)``), every session reserved on the
    compute machine's box."""
    from core import concurrency as C, concurrency_scan
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
    monkeypatch.setattr(config, "get_idle_timeout", lambda: 900)
    for name, value in (("_sessions", {}), ("_session_est", {}), ("_session_added_at", {}),
                        ("_session_owner", {}), ("_session_box", {}), ("_session_host", {}),
                        ("_evicted_credit", {}), ("_reserved_mb", 0), ("_parked_tasks", 0),
                        ("_budget_mb", 1000), ("_floor_mb", 100)):
        monkeypatch.setattr(C, name, value)
        if hasattr(concurrency_scan, name):
            monkeypatch.setattr(concurrency_scan, name, value)
    monkeypatch.setattr(C, "_cond", asyncio.Condition())
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)

    def budget(n_heavy: int) -> None:
        monkeypatch.setattr(C, "_budget_mb", 1000 * n_heavy)

    async def hold(session, owner: str = "") -> None:
        pool[session.session_id] = session
        assert await C.acquire(session.session_id, "chat", target=MACHINE, user_sub=owner or None)
        C._session_added_at[session.session_id] -= 200  # past the grow-in window

    return {"closed": remote.closed, "budget": budget, "hold": hold}


class TestRemoteReaper:
    @staticmethod
    def _layer(*, connected: bool = True):
        return SimpleNamespace(_cm=SimpleNamespace(
            is_connected=lambda mid: connected, is_session_in_grace=lambda mid, sid: False))

    def test_a_background_command_or_subagent_keeps_the_session(self, registries, monkeypatch):
        monkeypatch.setattr("core.remote.remote_reaper.get_hook_activity", lambda sid: 0, raising=False)
        now = time.monotonic()
        _running_command("r1")
        assert _idle_session_has_pending_work("r1", now, 10) is True
        _running_subagent("r2")
        assert _idle_session_has_pending_work("r2", now, 10) is True

    def test_running_work_keeps_an_idle_session_until_the_ceiling(self, registries, monkeypatch):
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        layer = self._layer()
        command, subagent, nothing = (_mk_session(idle_s=60) for _ in range(3))
        _running_command(command.session_id)
        _running_subagent(subagent.session_id)
        now = time.monotonic()
        assert _verdict(layer, command.session_id, command, now, 10) == KEEP
        assert _verdict(layer, subagent.session_id, subagent, now, 10) == KEEP
        assert _verdict(layer, nothing.session_id, nothing, now, 10) == REAP
        # Past the ceiling the session goes anyway.
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 30)
        assert _verdict(layer, command.session_id, command, now, 10) == REAP
        assert _verdict(layer, subagent.session_id, subagent, now, 10) == REAP

    def test_a_session_whose_machine_is_gone_is_reaped_whatever_the_registry_says(
            self, registries, monkeypatch):
        monkeypatch.setattr(config, "BACKGROUND_WORK_CEILING_S", 1000)
        s = _mk_session(idle_s=1)
        _running_command(s.session_id)
        now = time.monotonic()
        assert _verdict(self._layer(), s.session_id, s, now, 10) == KEEP
        assert _verdict(self._layer(connected=False), s.session_id, s, now, 10) == REAP


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
        busy, free = _mk_session(idle_s=500), _mk_session(idle_s=400)
        pool[busy.session_id] = busy
        pool[free.session_id] = free
        _set_ledger(monkeypatch, "_sessions", {busy.session_id: "chat", free.session_id: "chat"})
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
    async def test_a_terminal_its_report_holds_is_still_a_candidate(self, pool, registries, monkeypatch):
        # The reaper reads a terminal's Stop report and the evictor does not:
        # a stale report never blocks capacity on the platform host.
        from core.session import interactive_session
        pty = SimpleNamespace(last_activity=time.monotonic() - 500, turn_open=False,
                              question_parked=False, _background_reported=True)
        monkeypatch.setattr(interactive_session, "live_session_ids",
                            lambda local_only=False: {"px"})
        monkeypatch.setattr(interactive_session, "get", lambda sid: pty if sid == "px" else None)
        _set_ledger(monkeypatch, "_sessions", {"px": "chat"})
        scan = await _oldest_evictable_local(60, pending_leash_s=1000, turn_leash_s=1000)
        assert scan.victim == ("px", "interactive", False)
        assert scan.spared_background == 0 and scan.spared_live == 0

    @pytest.mark.asyncio
    async def test_live_turns_are_skipped_inside_the_leash(self, pool, registries, monkeypatch):
        from core.layers.direct import session as direct_session
        from core.layers.direct.layer import DirectLLMExecutionLayer
        from core.session import interactive_session
        now = time.monotonic()
        claude = _mk_session(idle_s=500)
        claude.turn_active = True
        pool[claude.session_id] = claude
        codex = _mk_session(idle_s=500, execution_path="codex-cli")
        codex.turn_active = True
        pool[codex.session_id] = codex
        direct = SimpleNamespace(last_activity=now - 500)
        pty = SimpleNamespace(last_activity=now - 500, turn_open=False, question_parked=True)
        monkeypatch.setattr(direct_session, "_direct_sessions", {"dx": direct})
        monkeypatch.setattr(interactive_session, "live_session_ids",
                            lambda local_only=False: {"px"})
        monkeypatch.setattr(interactive_session, "get", lambda sid: pty if sid == "px" else None)
        streaming = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(DirectLLMExecutionLayer._active_streams, "dx", streaming)
        _set_ledger(monkeypatch, "_sessions", {claude.session_id: "chat", codex.session_id: "chat",
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
        # A session between turns with a question parked on a person is live.
        codex.turn_active = False
        monkeypatch.setattr("core.session.session_state.has_pending_prompt",
                            lambda sid: sid == codex.session_id)
        streaming2 = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(DirectLLMExecutionLayer._active_streams, "dx", streaming2)
        scan = await _oldest_evictable_local(60, turn_leash_s=1000)
        assert scan.victim is None and scan.spared_live == 4

    @pytest.mark.asyncio
    async def test_only_chat_kind_is_a_candidate(self, pool, registries, monkeypatch):
        task, meeting, phone = (_mk_session(idle_s=900) for _ in range(3))
        chat = _mk_session(idle_s=400)
        for s in (task, meeting, phone, chat):
            pool[s.session_id] = s
        kinds = {task.session_id: "task", meeting.session_id: "meeting",
                 phone.session_id: "phone", chat.session_id: "chat"}
        _set_ledger(monkeypatch, "_sessions", kinds)
        scan = await _oldest_evictable_local(60)
        assert scan.victim is not None and scan.victim[0] == chat.session_id
        del kinds[chat.session_id]
        scan = await _oldest_evictable_local(60)
        assert scan.victim is None

    @pytest.mark.asyncio
    async def test_hook_activity_counts_as_activity(self, pool, registries, monkeypatch):
        from core.session import session_state
        s = _mk_session(idle_s=500)
        pool[s.session_id] = s
        _set_ledger(monkeypatch, "_sessions", {s.session_id: "chat"})
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
        from core.session import prewarm_session_registry as pw
        fresh = _mk_session(idle_s=5)
        pool[fresh.session_id] = fresh
        _set_ledger(monkeypatch, "_sessions", {fresh.session_id: "chat"})
        monkeypatch.setitem(pw._entries, fresh.session_id, pw._Entry(
            agent="agent", user_sub="u1", role="manager", exec_path="", ts=time.monotonic()))
        # A parked task or wake keeps the floor for a person's pre-warm.
        scan = await _oldest_evictable_local(60)
        assert scan.victim is None
        scan = await _oldest_evictable_local(60, prewarm_any_age=True)
        assert scan.victim == (fresh.session_id, "remote", True)

    @pytest.mark.asyncio
    async def test_maintenance_pass_evicts_an_idle_chat_never_a_task(
            self, pool, registries, ledger, monkeypatch):
        from core import concurrency as C
        ledger["budget"](2)
        task_run, idle_chat = _mk_session(idle_s=5000), _mk_session(idle_s=400)
        pool[task_run.session_id] = task_run
        assert await C.acquire(task_run.session_id, "task", target=MACHINE, blocking=True)
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
        mid_turn.turn_active = True
        await ledger["hold"](mid_turn)
        adm = await C.acquire("newcomer", "chat", target=MACHINE)
        assert not adm and adm.reason == "busy" and ledger["closed"] == []
        # Silent past the leash, the turn is reclaimable.
        mid_turn.last_activity = time.monotonic() - 1200
        assert await C.acquire("newcomer", "chat", target=MACHINE)
        assert ledger["closed"] == [mid_turn.session_id]

    @pytest.mark.asyncio
    async def test_prefer_user_reads_the_ledger_owner(self, pool, registries, ledger):
        # A machine session's record names no person: the ledger's owner orders
        # the requester's own idle session before another person's more idle one.
        from core import concurrency as C
        ledger["budget"](2)
        alice_s, bob_s = _mk_session(idle_s=400), _mk_session(idle_s=900)
        await ledger["hold"](alice_s, "alice")
        await ledger["hold"](bob_s, "bob")
        assert await C.acquire("alice-new", "chat", target=MACHINE, user_sub="alice")
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
        assert await C.acquire("alice-3", "chat", target=MACHINE, user_sub="alice")
        # One of her own went (the most idle), nobody else's; she holds two.
        assert ledger["closed"] == [own_old.session_id]
        assert C._owned("alice") == 2 and other.session_id in C._sessions
        # Her sessions all fresh: the cap sentence, nothing closed.
        fresh = _mk_session(idle_s=1)
        pool.pop(own_new.session_id)
        C.release(own_new.session_id)
        await ledger["hold"](fresh, "alice")
        adm = await C.acquire("alice-4", "chat", target=MACHINE, user_sub="alice")
        assert not adm and adm.reason == "user_cap"
        assert "You already have 2 sessions running" in adm.user_message
        assert ledger["closed"] == [own_old.session_id]

    @pytest.mark.asyncio
    async def test_per_user_cap_gives_up_only_sessions_it_counts(
            self, pool, registries, ledger, monkeypatch):
        # The cap counts the ledger's owner; a session with no recorded owner
        # (a view-only re-attach, a wake) counts against nobody, so it is not
        # the person's to give up even when it carries her user_sub (a Direct
        # LLM session's own field): closing it frees no counted seat.
        from core import concurrency as C
        from core.layers.direct import session as direct_session
        monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 2)
        ledger["budget"](10)
        unowned = SimpleNamespace(session_id="unowned", user_sub="alice",
                                  last_activity=time.monotonic() - 900)
        monkeypatch.setattr(direct_session, "_direct_sessions", {unowned.session_id: unowned})
        assert await C.acquire(unowned.session_id, "chat", target=MACHINE)
        C._session_added_at[unowned.session_id] -= 200  # past the grow-in window
        own_old, own_new = _mk_session(idle_s=500), _mk_session(idle_s=400)
        await ledger["hold"](own_old, "alice")
        await ledger["hold"](own_new, "alice")
        assert C._owned("alice") == 2
        scan = await _oldest_evictable_local(60, only_user="alice")
        assert scan.victim is not None and scan.victim[0] == own_old.session_id
        assert await C.acquire("alice-3", "chat", target=MACHINE, user_sub="alice")
        assert ledger["closed"] == [own_old.session_id]
        assert unowned.session_id in C._sessions
        # Her counted sessions all fresh: the cap sentence, and the uncounted
        # one is left alone.
        pool.pop(own_new.session_id)
        C.release(own_new.session_id)
        fresh = _mk_session(idle_s=1)
        await ledger["hold"](fresh, "alice")
        adm = await C.acquire("alice-4", "chat", target=MACHINE, user_sub="alice")
        assert not adm and adm.reason == "user_cap"
        assert ledger["closed"] == [own_old.session_id]
        assert unowned.session_id in C._sessions


def test_a_terminal_report_names_work_by_the_agents_own_kinds():
    # The Stop hook's labels (Claude Code): the agent's work counts, the
    # CLI's housekeeping and kinds with no end to wait on never do.
    assert background_leash.names_terminal_work(["shell"])
    assert background_leash.names_terminal_work(["dream", "MCP task"])
    assert not background_leash.names_terminal_work([])
    assert not background_leash.names_terminal_work(
        ["dream", "auto-mode scan", "memory import", "teammate", "cloud session"])
