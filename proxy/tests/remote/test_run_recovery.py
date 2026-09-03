"""Mode C — proxy-restart run recovery (services.scheduler.run_recovery).

Covers the startup park/fail split, the sessions_alive adopt/fail routing,
the deadline sweeper, and recovery eligibility — without a live satellite
(the connection manager + remote layer are faked).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from services.scheduler import run_recovery
from storage import database as task_store


@pytest.fixture(autouse=True)
def _clean_parked():
    run_recovery._parked.clear()
    yield
    run_recovery._parked.clear()


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
        from storage import agent_store
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
        self.closed = []

    async def adopt_session(self, *, machine_id, session_id, agent_name,
                            command_id, use_native_permissions=False):
        from core.events.common_events import CommonEvent, TEXT, DONE
        self.adopted.append(session_id)
        yield CommonEvent(type=TEXT, data={"content": "recovered answer"})
        yield CommonEvent(type=DONE, data={})

    async def adopt_idle_session(self, *, machine_id, session_id, agent_name,
                                 use_native_permissions=False):
        self.idle_adopted.append(session_id)
        self._sessions[session_id] = object()

    async def close_session(self, session_id):
        self._sessions.pop(session_id, None)
        self.closed.append(session_id)


class TestOnSessionsAlive:
    @pytest.mark.asyncio
    async def test_reported_run_adopted_and_completed(self, temp_db,
                                                      monkeypatch):
        run_id, chat_id, sid = _mk_remote_run(temp_db)
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
                                                     monkeypatch):
        """An idle (no turn) session with a chat row must be re-REGISTERED
        so the idle reaper can leash it — restart-orphaned idle sessions
        previously lived forever satellite-side and ate the machine's
        capacity ("at capacity — too many active sessions" on every new
        spawn)."""
        _, chat_id, sid = _mk_remote_run(temp_db, status="completed")
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False, "agent_slug": "pa",
        }])
        assert layer.idle_adopted == [sid]
        assert layer.closed == []          # has a chat → stays resumable
        assert layer.adopted == []         # no turn → no recovery pump

    @pytest.mark.asyncio
    async def test_idle_session_without_chat_closed(self, temp_db,
                                                    monkeypatch):
        """A reported idle session with NO chat row is junk — register then
        close it immediately (the close path needs the registry entry to
        reach the satellite)."""
        layer = _FakeLayer()
        import core.session.session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

        sid = uuid.uuid4().hex
        await run_recovery.on_sessions_alive("machine-1", [{
            "session_id": sid, "turn_active": False,
        }])
        assert layer.idle_adopted == [sid]
        assert layer.closed == [sid]

    @pytest.mark.asyncio
    async def test_idle_session_already_registered_skipped(self, temp_db,
                                                           monkeypatch):
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
