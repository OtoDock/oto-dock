"""Mode C — proxy-restart run recovery (services.scheduler.run_recovery).

Covers the startup park/fail split, the sessions_alive adopt/fail routing,
the deadline sweeper, and recovery eligibility — without a live satellite
(the connection manager + remote layer are faked).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.scheduler import run_recovery
from storage import database as task_store


@pytest.fixture(autouse=True)
def _clean_parked():
    run_recovery._parked.clear()
    yield
    run_recovery._parked.clear()
    # A recovery pump's end arms the chat's queue on the test's own loop; the
    # task set is module state, so a later test's gather over it must not
    # meet this loop's leftovers.
    from core.events import input_queue
    input_queue._tasks.clear()


def _mk_remote_run(temp_db, *, target="machine-1",
                   exec_path="claude-code-cli", status="running"):
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    chat_id = f"task-{run_id}"
    session_id = uuid.uuid4().hex
    task_store.create_run(run_id, "task-x", "pa", "manual", None, "do it")
    task_store.create_chat(chat_id, "user-1", "pa", "default",
                           model="m", execution_path=exec_path)
    task_store.update_chat(chat_id, session_id=session_id,
                           execution_target=target)
    task_store.update_run(run_id, status=status, chat_id=chat_id,
                          session_id=session_id)
    return run_id, chat_id, session_id


class TestDeferOrphanedRuns:
    def test_remote_cli_parked_local_failed(self, temp_db):
        remote_id, _, remote_sid = _mk_remote_run(temp_db)
        local_id, _, _ = _mk_remote_run(temp_db, target="local")

        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and failed == 1
        assert remote_sid in run_recovery._parked
        assert task_store.get_run(remote_id)["status"] == "running"
        assert task_store.get_run(local_id)["status"] == "failed"

    def test_codex_remote_is_failed_not_parked(self, temp_db):
        run_id, _, _ = _mk_remote_run(temp_db, exec_path="codex-cli")
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 0 and failed == 1
        assert task_store.get_run(run_id)["status"] == "failed"

    def test_empty_exec_path_resolves_to_agent_default_and_parks(self, temp_db):
        # Delegate worker chats never stamp execution_path (empty = agent
        # default). Eligibility must resolve the EFFECTIVE path — the literal
        # comparison silently excluded every delegate lane from Mode C, so a
        # deploy-restart mid-turn dropped the round's transcript (failed
        # "Proxy shutting down", turn blocks never flushed).
        run_id, _, sid = _mk_remote_run(temp_db, exec_path="")
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 1 and failed == 0
        assert sid in run_recovery._parked
        assert task_store.get_run(run_id)["status"] == "running"

    def test_empty_exec_path_codex_default_agent_not_parked(self, temp_db):
        # The resolution consults the AGENT default — a codex-default agent's
        # empty-path chat stays out of Mode C (codex is out of recovery scope).
        from storage.agents import agent_store
        agent_store.create_agent("codex-ag", "Codex Agent",
                                 created_by="user-1",
                                 execution_path="codex-cli")
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        chat_id = f"task-{run_id}"
        sid = uuid.uuid4().hex
        task_store.create_run(run_id, "task-x", "codex-ag", "manual", None, "x")
        task_store.create_chat(chat_id, "user-1", "codex-ag", "default",
                               model="m", execution_path="")
        task_store.update_chat(chat_id, session_id=sid,
                               execution_target="machine-1")
        task_store.update_run(run_id, status="running", chat_id=chat_id,
                              session_id=sid)
        parked, failed = run_recovery.defer_orphaned_runs()
        assert parked == 0 and failed == 1


class TestIsRecoveryEligible:
    def test_empty_exec_path_remote_chat_is_eligible(self, temp_db):
        # Mirrors the shutdown guard: a delegate lane (execution_target set,
        # execution_path empty → resolves to claude-code-cli) must be LEFT
        # RUNNING at graceful shutdown for satellite re-adopt.
        _, chat_id, _ = _mk_remote_run(temp_db, exec_path="")
        assert run_recovery.is_recovery_eligible(chat_id) is True

    def test_local_chat_not_eligible(self, temp_db):
        _, chat_id, _ = _mk_remote_run(temp_db, target="local")
        assert run_recovery.is_recovery_eligible(chat_id) is False


class _FakeLayer:
    def __init__(self):
        self._sessions = {}
        self.adopted = []
        self.idle_adopted = []
        self.idle_kwargs: dict[str, dict] = {}
        self.closed = []
        self.unadopted_closed = []
        self.incarnations: dict[str, str] = {}
        self.dropped: list[str] = []
        self.modes: dict[str, tuple[str, int]] = {}
        self.machines: dict[str, str] = {}
        self.severed: set[str] = set()
        self.spawning: set[str] = set()

    def owns_session(self, session_id):
        return session_id in self._sessions

    def is_spawning(self, session_id):
        return session_id in self.spawning

    async def prepare_resume(self, session_id):
        self._sessions.pop(session_id, None)
        self.machines.pop(session_id, None)
        self.severed.discard(session_id)
        self.dropped.append(session_id)

    async def adopt_session(self, *, machine_id, session_id, agent_name,
                            execution_path, command_id,
                            use_native_permissions=False, mode="", token_floor=0):
        from core.events.common_events import CommonEvent, TEXT, DONE
        self.adopted.append(session_id)
        self.modes[session_id] = (mode, token_floor)
        yield CommonEvent(type=TEXT, data={"content": "recovered answer"})
        yield CommonEvent(type=DONE, data={})

    async def adopt_idle_session(self, *, machine_id, session_id, agent_name,
                                 execution_path, use_native_permissions=False,
                                 mode="", token_floor=0, **kw):
        self.idle_adopted.append(session_id)
        self.idle_kwargs[session_id] = {"execution_path": execution_path, **kw}
        self.modes[session_id] = (mode, token_floor)
        self._sessions[session_id] = object()
        self.machines[session_id] = machine_id
        return True

    def session_ids_on(self, machine_id):
        return [sid for sid, m in self.machines.items() if m == machine_id]

    def remote_stream_severed(self, session_id):
        return session_id in self.severed

    async def close_session(self, session_id):
        self._sessions.pop(session_id, None)
        self.closed.append(session_id)

    async def close_unadopted(self, machine_id, session_id, incarnation=""):
        self.unadopted_closed.append(session_id)
        self.incarnations[session_id] = incarnation


@pytest.fixture
def placed(temp_db):
    """Give a session the security context the index would reload for it,
    placed on a machine, with the mode and floor the index kept; its person
    is a platform admin unless ``platform_role`` says otherwise."""
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import session_state
    sids: list[str] = []

    def _place(sid, machine="machine-1", *, mode="", floor=0, platform_role="admin"):
        sids.append(sid)
        sub = f"sub-{sid[:8]}"
        task_store.upsert_user(sub, f"{sub}@x.test", f"Person {sid[:8]}", platform_role)
        session_state._session_security[sid] = SecurityContext(
            role="manager", username=task_store.get_username_by_sub(sub), agent="pa",
            is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind="remote", machine_id=machine))
        if mode or floor:
            session_state._reloaded_state[sid] = (mode, floor)

    yield _place
    for sid in sids:
        session_state._session_security.pop(sid, None)
        session_state._reloaded_state.pop(sid, None)
        session_state._starting.pop(sid, None)


class TestOnSessionsAlive:
    @pytest.mark.asyncio
    async def test_reported_run_adopted_and_completed(self, temp_db,
                                                      monkeypatch, placed):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        placed(sid)
        run_recovery.defer_orphaned_runs()

        layer = _FakeLayer()
        monkeypatch.setattr(
            "core.session.session_manager._get_remote_layer", lambda: layer)
        # _recover_session imports these lazily; patch at source module.
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1",
        }])
        # The adoption task runs on the loop — let it finish.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if task_store.get_run(run_id)["status"] != "running":
                break
        assert layer.adopted == [sid]
        run = task_store.get_run(run_id)
        assert run["status"] == "completed"
        assert sid not in run_recovery._parked

    @pytest.mark.asyncio
    async def test_unreported_parked_run_failed(self, temp_db, monkeypatch):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
        run_recovery.defer_orphaned_runs()
        # The satellite for machine-1 reconnects but does NOT report sid.
        await run_recovery.on_sessions_alive("machine-1", [])
        assert task_store.get_run(run_id)["status"] == "failed"
        assert "lost" in task_store.get_run(run_id)["error_message"]
        assert sid not in run_recovery._parked

    @pytest.mark.asyncio
    async def test_idle_session_with_chat_re_leashed(self, temp_db,
                                                     monkeypatch, placed):
        """An idle (no turn) session with a chat row must be re-REGISTERED
        so the idle reaper can leash it — restart-orphaned idle sessions
        previously lived forever satellite-side and ate the machine's
        capacity ("at capacity — too many active sessions" on every new
        spawn) — in the mode and with the floor the index kept."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", floor=1790000000)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "agent_slug": "pa",
        }])
        assert layer.idle_adopted == [sid]
        assert layer.closed == []          # has a chat → stays resumable
        assert layer.adopted == []         # no turn → no recovery pump
        assert layer.modes[sid] == ("default", 1790000000)

    @pytest.mark.asyncio
    async def test_idle_session_without_chat_closed(self, temp_db,
                                                    monkeypatch, placed):
        """A reported idle session with NO chat row is junk: its process is
        closed on the satellite and the context the index kept goes."""
        from core.session import session_state
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        sid = uuid.uuid4().hex
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == []
        assert layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is None

    @pytest.mark.asyncio
    async def test_a_session_closed_on_purpose_is_not_revived(self, temp_db, monkeypatch):
        """A close drops the session's context before it returns (an idle reap,
        offboarding, a sign-out, a close the offline satellite never got): a
        reported session without one is closed on the satellite, never
        re-leashed, and its token is never live again."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert not session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_a_context_of_another_machine_is_not_taken_by_this_one(
            self, temp_db, monkeypatch, placed):
        """Two satellites reporting one id: the machine the context names keeps
        it; the other's process is closed and the context is left alone."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, machine="machine-2")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is not None
        assert sid not in session_state._starting

    @pytest.mark.asyncio
    async def test_an_older_index_falls_back_to_the_chats_mode(self, temp_db, monkeypatch, placed):
        """An index written before modes were persisted: a chat session comes
        back in its chat row's mode, never in the default ``auto``; an
        unattended (task) session in ``auto``."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        task_store.update_chat(chat_id, permission_mode="plan")
        placed(sid)
        session_state._sessions[sid] = {"client_type": "dashboard"}
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False,
            }])
            assert layer.modes[sid] == ("plan", 0)
            assert run_recovery._readopt_state("machine-1", sid, {"permission_mode": ""}) == ("default", 0)
            session_state._sessions[sid] = {"client_type": "task"}
            assert run_recovery._readopt_state("machine-1", sid, {"permission_mode": "default"}) == ("auto", 0)
        finally:
            session_state._sessions.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_turn_that_finished_during_the_restart_is_replayed(
            self, temp_db, monkeypatch, placed):
        """The shutdown marks a turn it left running; after the restart the
        satellite's retained turn is replayed into the chat even when it
        finished meanwhile (it reports no turn in flight)."""
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        session_state.mark_recover_pending(sid)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "command_id": "c9",
            "buffered_events": 12,
        }])
        for _ in range(50):
            await asyncio.sleep(0.02)
            if layer.adopted:
                break
        assert layer.adopted == [sid] and layer.idle_adopted == []
        assert layer.modes[sid] == ("default", 0)
        assert session_state.take_recover_pending(sid) is False

    @pytest.mark.asyncio
    async def test_idle_session_already_registered_skipped(self, temp_db,
                                                           monkeypatch, placed):
        """A reconnect blip reports sessions the proxy still tracks (grace)
        — no double registration."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        layer = _FakeLayer()
        layer._sessions[sid] = object()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == []
        assert layer.closed == []


class TestReadoptionGuards:
    @pytest.mark.asyncio
    async def test_every_reported_mark_is_read_and_a_chatless_one_closed(
            self, temp_db, monkeypatch, placed):
        """The replay marks of all reported sessions are read at once, a
        parked run's too; a marked session without a chat row is closed on
        its machine with its context, not left running unreaped."""
        from core.session import session_state
        run_id, chat_id, run_sid = _mk_remote_run(temp_db)
        placed(run_sid)
        run_recovery.defer_orphaned_runs()
        session_state.mark_recover_pending(run_sid)
        orphan = uuid.uuid4().hex
        placed(orphan)
        session_state.mark_recover_pending(orphan)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [
            {"session_id": run_sid, "turn_active": True, "command_id": "c1"},
            {"session_id": orphan, "turn_active": False},
        ])
        assert session_state.take_recover_pending(run_sid) is False
        assert session_state.take_recover_pending(orphan) is False
        assert orphan in layer.unadopted_closed
        assert session_state.get_session_security(orphan) is None
        for _ in range(50):
            await asyncio.sleep(0.02)
            if task_store.get_run(run_id)["status"] != "running":
                break

    @pytest.mark.asyncio
    async def test_a_person_who_lost_the_agent_does_not_get_the_session_back(
            self, temp_db, monkeypatch, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default", platform_role="member")  # no role on the agent now
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        assert session_state.get_session_security(sid) is None
        assert not session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_an_expired_context_is_not_re_adopted(self, temp_db, monkeypatch, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        session_state._session_security_ts[sid] = 0.0
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False,
            }])
            assert layer.idle_adopted == [] and layer.unadopted_closed == [sid]
        finally:
            session_state._session_security_ts.pop(sid, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marked", [True, False])
    async def test_a_decline_of_the_claude_list_names_the_reported_incarnation(
            self, temp_db, monkeypatch, placed, marked):
        """A session the Claude list reports without a chat row is closed on
        its machine by the incarnation the report named (the active loop for
        a marked one, the idle loop otherwise), so a decision made on this
        report never closes an object a later start put under the id."""
        from core.session import session_state
        orphan = uuid.uuid4().hex
        placed(orphan)
        if marked:
            session_state.mark_recover_pending(orphan)
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        try:
            await run_recovery.on_sessions_alive("machine-1", [
                {"session_id": orphan, "turn_active": False, "incarnation": "a4-2"},
            ])
            assert layer.unadopted_closed == [orphan]
            assert layer.incarnations[orphan] == "a4-2"
        finally:
            session_state.take_recover_pending(orphan)

    @pytest.mark.asyncio
    async def test_close_unadopted_names_the_incarnation_only_when_it_has_one(self):
        """The close frame carries the incarnation it was given and no key
        otherwise, so a satellite below 0.5.138 reads the frame it always
        did."""
        from core.remote.remote_execution import RemoteExecutionLayer
        cm = MagicMock()
        cm.send_command = AsyncMock()
        layer = RemoteExecutionLayer(cm)
        await layer.close_unadopted("machine-1", "sid-1", incarnation="a4-2")
        await layer.close_unadopted("machine-1", "sid-2")
        frames = [c.args[1] for c in cm.send_command.call_args_list]
        assert frames == [
            {"type": "close_session", "session_id": "sid-1", "incarnation": "a4-2"},
            {"type": "close_session", "session_id": "sid-2"},
        ]

    def test_another_machines_decline_keeps_the_owners_mark(self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid, machine="machine-2")
        session_state.mark_starting(sid, 60)
        run_recovery._clear_readopt_mark(sid, "machine-1")
        assert sid in session_state._starting
        run_recovery._clear_readopt_mark(sid, "machine-2")
        assert sid not in session_state._starting

    def test_a_reported_session_answers_in_its_kept_mode_before_adoption(
            self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid, mode="plan", floor=1790000000)
        assert session_state.get_session_mode(sid) == "plan"


def _codex_chat(temp_db, *, target="machine-1", model="gpt-6.1-sol", exec_path="codex-cli"):
    _, chat_id, sid = _mk_remote_run(temp_db, target=target, exec_path=exec_path,
                                     status="completed")
    task_store.update_chat(chat_id, model=model)
    return chat_id, sid


def _other(sid, **over):
    entry = {"session_id": sid, "execution_path": "codex-cli", "agent_slug": "pa",
             "alive": True, "turn_active": False, "incarnation": "b1-7",
             "resume_handle": "thread-9", "model": "gpt-6.1-sol",
             "mcp_servers": [], "use_native_permissions": False}
    entry.update(over)
    return entry


class TestOtherSessions:
    """The connect report's ``other_sessions``: a satellite's idle Codex
    session the platform still has a context and a chat for is taken back;
    one that is dead, mid-turn, closed on purpose or not this machine's is
    closed there (by the incarnation the report named)."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        return lyr

    @pytest.mark.asyncio
    async def test_an_idle_codex_session_is_taken_back(self, temp_db, layer, placed, monkeypatch):
        from storage import remote_store
        monkeypatch.setattr(remote_store, "get_remote_machine",
                            lambda mid: {"id": mid, "allow_full_fs": True})
        _, sid = _codex_chat(temp_db)
        placed(sid, mode="default", floor=1790000000)

        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid)])

        assert layer.idle_adopted == [sid] and layer.unadopted_closed == []
        kw = layer.idle_kwargs[sid]
        assert kw["execution_path"] == "codex-cli" and kw["resume_handle"] == "thread-9"
        assert kw["model"] == "gpt-6.1-sol" and kw["allow_full_fs"] is True
        assert layer.modes[sid] == ("default", 1790000000)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_model, expected", [
        ("gpt-6-astra", "gpt-6-astra"),          # the engine serves it
        ("claude-fable-5-1", "gpt-5.6-terra"),   # foreign to it: the reported one
    ])
    async def test_the_chats_model_wins_when_it_is_the_engines(
            self, temp_db, layer, placed, monkeypatch, chat_model, expected):
        from storage.billing import subscription_store
        monkeypatch.setattr(subscription_store, "list_models", lambda path: [
            {"model_id": "gpt-6-astra"}, {"model_id": "gpt-5.6-terra"}] if path == "codex-cli" else [])
        _, sid = _codex_chat(temp_db, model=chat_model)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid, model="gpt-5.6-terra")])
        assert layer.idle_kwargs[sid]["model"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("over, why", [
        ({"alive": False}, "dead"),
        ({"turn_active": True}, "mid-turn"),
        ({"resume_handle": ""}, "no thread"),
        ({"execution_path": "acme-cli"}, "unknown engine"),
        ({"execution_path": "direct-llm"}, "an engine that is not taken back"),
    ])
    async def test_one_that_cannot_come_back_is_closed_by_its_incarnation(
            self, temp_db, layer, placed, over, why):
        _, sid = _codex_chat(temp_db)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [_other(sid, **over)])
        assert layer.idle_adopted == [], why
        assert layer.unadopted_closed == [sid], why
        assert layer.incarnations[sid] == "b1-7"

    @pytest.mark.asyncio
    async def test_a_chat_on_another_engine_or_machine_is_not_taken(self, temp_db, layer, placed):
        _, sid_engine = _codex_chat(temp_db, exec_path="claude-code-cli")
        _, sid_machine = _codex_chat(temp_db, target="machine-2")
        _, sid_unpinned = _codex_chat(temp_db, target="")
        for sid in (sid_engine, sid_machine, sid_unpinned):
            placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_engine), _other(sid_machine), _other(sid_unpinned)])
        assert layer.idle_adopted == [sid_unpinned]
        assert sorted(layer.unadopted_closed) == sorted([sid_engine, sid_machine])

    @pytest.mark.asyncio
    async def test_no_context_or_no_chat_closes_it(self, temp_db, layer, placed):
        _, sid_closed = _codex_chat(temp_db)          # no context: closed on purpose
        sid_chatless = uuid.uuid4().hex
        placed(sid_chatless)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_closed), _other(sid_chatless)])
        assert layer.idle_adopted == []
        assert sorted(layer.unadopted_closed) == sorted([sid_closed, sid_chatless])

    @pytest.mark.asyncio
    async def test_held_or_spawning_is_left_alone(self, temp_db, layer, placed):
        _, sid_held = _codex_chat(temp_db)
        _, sid_spawn = _codex_chat(temp_db)
        for sid in (sid_held, sid_spawn):
            placed(sid)
        layer._sessions[sid_held] = object()
        layer.spawning.add(sid_spawn)
        await run_recovery.on_sessions_alive("machine-1", [], [
            _other(sid_held), _other(sid_spawn)])
        assert layer.idle_adopted == [] and layer.unadopted_closed == []

    @pytest.mark.asyncio
    async def test_an_id_in_both_lists_is_the_turn_replay_lists(self, temp_db, layer, placed):
        _, sid = _codex_chat(temp_db, exec_path="claude-code-cli")
        placed(sid, mode="default")
        await run_recovery.on_sessions_alive(
            "machine-1", [{"session_id": sid, "turn_active": False, "agent_slug": "pa",
                           "execution_path": "claude-code-cli"}], [_other(sid)])
        assert layer.idle_adopted == [sid]
        assert layer.idle_kwargs[sid]["execution_path"] == "claude-code-cli"

    @pytest.mark.asyncio
    async def test_a_report_without_the_list_takes_no_codex_step(self, temp_db, layer, placed):
        _, sid = _codex_chat(temp_db)
        placed(sid)
        await run_recovery.on_sessions_alive("machine-1", [])
        assert layer.idle_adopted == [] and layer.unadopted_closed == []

    def test_the_reported_codex_session_is_live_until_its_adoption(self, temp_db, placed):
        from core.session import session_state
        sid = uuid.uuid4().hex
        placed(sid)
        run_recovery._mark_reported_starting({}, "machine-1", {sid: _other(sid)})
        assert sid in session_state._starting

    def test_a_local_context_is_never_a_satellites(self, temp_db):
        from auth.path_policy import SecurityContext
        from core import placement
        local = SecurityContext(role="manager", username="u", agent="pa", is_admin_agent=False,
                                placement=placement.LOCAL_PLACEMENT)
        unnamed_remote = SecurityContext(
            role="manager", username="u", agent="pa", is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind="admin_remote", machine_id=""))
        assert run_recovery._placed_here(local, "machine-1") is False
        assert run_recovery._placed_here(unnamed_remote, "machine-1") is True


class TestSeveredRecords:
    """A machine back after its reconnect grace expired: its records whose
    stream was severed are dropped (the record only), an idle one is taken
    back with what this process holds for it, a mid-turn one is left to its
    next turn's resume (nothing replayed twice)."""

    @pytest.fixture
    def layer(self, monkeypatch):
        import core.session.session_manager as sm
        lyr = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: lyr)
        return lyr

    @pytest.mark.asyncio
    async def test_an_idle_severed_record_is_taken_back_in_its_live_state(
            self, temp_db, layer, placed):
        from core.session import session_state
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid)
        layer._sessions[sid] = object()
        layer.machines[sid] = "machine-1"
        layer.severed.add(sid)
        session_state._session_modes[sid] = "plan"
        session_state._session_token_floor[sid] = 1795000000
        try:
            await run_recovery.on_sessions_alive("machine-1", [{
                "session_id": sid, "turn_active": False, "agent_slug": "pa",
                "execution_path": "claude-code-cli"}])
        finally:
            session_state._session_modes.pop(sid, None)
            session_state._session_token_floor.pop(sid, None)
        assert layer.dropped == [sid]
        assert layer.idle_adopted == [sid]
        assert layer.modes[sid] == ("plan", 1795000000)

    @pytest.mark.asyncio
    async def test_a_mid_turn_severed_record_is_dropped_and_not_replayed(
            self, temp_db, layer, placed):
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        placed(sid, mode="default")
        layer._sessions[sid] = object()
        layer.machines[sid] = "machine-1"
        layer.severed.add(sid)
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": True, "command_id": "c1",
            "agent_slug": "pa", "execution_path": "claude-code-cli"}])
        await asyncio.sleep(0.05)
        assert layer.dropped == [sid]
        assert layer.adopted == [] and layer.idle_adopted == []
        assert layer.unadopted_closed == []

    @pytest.mark.asyncio
    async def test_a_record_being_spawned_is_not_dropped(self, temp_db, layer):
        layer._sessions["s-sp"] = object()
        layer.machines["s-sp"] = "machine-1"
        layer.severed.add("s-sp")
        layer.spawning.add("s-sp")
        await run_recovery.on_sessions_alive("machine-1", [])
        assert layer.dropped == []


class TestSweepExpired:
    @pytest.mark.asyncio
    async def test_expired_deadline_fails_run(self, temp_db):
        run_id, _, sid = _mk_remote_run(temp_db)
        run_recovery.defer_orphaned_runs()
        run_recovery._parked[sid]["deadline"] = 0.0  # already expired
        await run_recovery.sweep_expired()
        assert task_store.get_run(run_id)["status"] == "failed"
        assert "did not reconnect" in task_store.get_run(run_id)["error_message"]
        assert sid not in run_recovery._parked


class TestEligibility:
    def test_remote_cli_eligible(self, temp_db):
        _, chat_id, _ = _mk_remote_run(temp_db)
        assert run_recovery.is_recovery_eligible(chat_id) is True

    def test_local_and_codex_not_eligible(self, temp_db):
        _, local_chat, _ = _mk_remote_run(temp_db, target="local")
        _, codex_chat, _ = _mk_remote_run(temp_db, exec_path="codex-cli")
        assert run_recovery.is_recovery_eligible(local_chat) is False
        assert run_recovery.is_recovery_eligible(codex_chat) is False
        assert run_recovery.is_recovery_eligible("") is False


class TestMachineResumePass:
    """A machine's reconnect resumes what its blip left waiting: the
    background monitors of its ordinary chats' sessions, and the queued
    messages of the chats on it."""

    @staticmethod
    def _chat(chat_id: str, *, target: str = "", sid: str = "", queued: bool = False,
              **kw) -> None:
        task_store.create_chat(chat_id, "user-1", "pa", "default", **kw)
        task_store.update_chat(chat_id, session_id=sid or None, execution_target=target)
        if queued:
            task_store.enqueue_chat_input(chat_id, "q-" + chat_id, "user-1", text="waiting",
                                          cli_text="waiting", event_data="", images="",
                                          origin_conn="")

    @pytest.fixture
    def recorded(self, monkeypatch):
        from core.events import input_queue, pump_bg_monitors
        import core.session.session_manager as sm
        layer = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        armed_queues: list[str] = []
        armed_monitors: list[tuple[str, str]] = []
        monkeypatch.setattr(input_queue, "arm", armed_queues.append)
        monkeypatch.setattr(pump_bg_monitors, "arm_bg_monitors",
                            lambda lyr, sid, cid: armed_monitors.append((sid, cid)))
        return SimpleNamespace(layer=layer, queues=armed_queues, monitors=armed_monitors)

    @pytest.mark.asyncio
    async def test_queued_messages_on_the_machine_are_armed(self, temp_db, recorded):
        self._chat("c-pinned", target="machine-1", queued=True)
        self._chat("c-elsewhere", target="machine-2", queued=True)
        self._chat("c-unpinned", sid="s-unpinned", queued=True)
        self._chat("c-nothing-queued", target="machine-1")
        recorded.layer.machines["s-unpinned"] = "machine-1"

        await run_recovery._machine_resume_pass("machine-1", set())

        assert sorted(recorded.queues) == ["c-pinned", "c-unpinned"]

    @pytest.mark.asyncio
    async def test_the_chips_stop_saying_the_machine_is_reconnecting(
            self, temp_db, recorded, monkeypatch):
        from core.events import input_queue
        fanned: list[tuple[str, dict]] = []
        monkeypatch.setattr(input_queue, "fan_out",
                            lambda cid, frame, **kw: fanned.append((cid, frame)))
        self._chat("c-away", target="machine-1", queued=True)
        q = await input_queue.loaded("c-away")
        q.items[0].waiting = input_queue.WAITING_RECONNECT
        recorded.queues.clear()

        await run_recovery._machine_resume_pass("machine-1", set())

        assert q.items[0].waiting == ""
        assert [(cid, f["type"]) for cid, f in fanned] == [("c-away", "queue_snapshot")]
        assert "waiting" not in fanned[0][1]["messages"][0]
        assert recorded.queues == ["c-away"]

    @pytest.mark.asyncio
    async def test_monitors_of_chats_reported_sessions_are_armed(
            self, temp_db, recorded):
        from core.events.bg_command_state import _bg_command_registries, get_bg_command_registry
        from services.scheduler import lanes
        sessions = {
            "s-chat": {},
            "s-task": {"source_type": "task"},
            "s-worker": {"delegate_role": "worker"},
            "s-window": {"delegate_role": "worker"},
            "s-phone": {"source_type": "phone"},
            "s-unreported": {},
            "s-severed": {},
            "s-idle": {},
        }
        lanes.hold_report("s-window")
        try:
            for sid, kw in sessions.items():
                self._chat("c-" + sid, target="machine-1", sid=sid, **kw)
                recorded.layer.machines[sid] = "machine-1"
                if sid != "s-idle":
                    get_bg_command_registry(sid).register_spawn("b-" + sid, "tu-" + sid)
            recorded.layer.severed.add("s-severed")

            await run_recovery._machine_resume_pass(
                "machine-1", set(sessions) - {"s-unreported"})
        finally:
            lanes.release_report("s-window")
            for sid in sessions:
                _bg_command_registries.pop(sid, None)

        # A worker's or a task run's chat outside a report window too; the
        # run's producer owns a session whose report is pending.
        assert sorted(recorded.monitors) == [
            ("s-chat", "c-s-chat"), ("s-task", "c-s-task"), ("s-worker", "c-s-worker")]

    @pytest.mark.asyncio
    async def test_a_deferred_queued_message_goes_out_after_the_reconnect(
            self, temp_db, monkeypatch):
        from core.events import input_queue
        from core.events.common_events import TurnInput
        import core.session.session_manager as sm
        layer = _FakeLayer()
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
        monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.01)
        self._chat("c-blip", target="machine-1")
        q = await input_queue.loaded("c-blip")
        await q.add("q-blip", "user-1", TurnInput("across the blip"))
        outcomes = [input_queue.DELIVERY_DEFERRED, input_queue.DELIVERY_SENT]
        calls: list[list[str]] = []

        class _Conn:
            async def _deliver_queued(self, chat_id, taken):
                calls.append([qi.queue_id for qi in taken])
                return outcomes.pop(0), "the machine was away"
        monkeypatch.setattr(input_queue, "_driver_for", lambda author, items: _Conn())

        assert await input_queue.deliver("c-blip") is False      # the gap
        await run_recovery.on_sessions_alive("machine-1", [])    # the machine is back
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.02)

        assert calls == [["q-blip"], ["q-blip"]]

    @pytest.mark.asyncio
    async def test_the_pass_does_not_wait_for_the_wake_replay(self, temp_db, recorded, monkeypatch):
        from services.scheduler import scheduler as _sched
        held = asyncio.Event()

        async def _slow_replay(machine_id=None, session_ids=()):
            await held.wait()
            return 0
        monkeypatch.setattr(_sched, "redeliver_pending_wakes", _slow_replay)
        self._chat("c-first", target="machine-1", queued=True)

        await run_recovery.on_sessions_alive("machine-1", [])
        for _ in range(100):
            if recorded.queues:
                break
            await asyncio.sleep(0.02)
        held.set()

        assert recorded.queues == ["c-first"]
