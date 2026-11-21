"""A recovery handed to a task of its own is on its way to its binding
(``satellite_link.adoption_begins`` and ``adoption_ends``). The report's
callback counts it before the task starts, and the task takes it off the
count when it ends, with a binding or with none. A machine's kept sessions
are back only when the count is empty (``reported_since_boot``)."""

import asyncio

import pytest

from core.remote import satellite_link
from services.scheduler import run_recovery
from storage import database as task_store
from tests.remote.test_run_recovery import (  # noqa: F401  (``_clean_parked`` and ``placed`` are fixtures)
    _clean_parked,
    _FakeLayer,
    _mk_remote_run,
    placed,
)


@pytest.fixture(autouse=True)
def _no_adoption_left():
    satellite_link._adopting.clear()
    yield
    satellite_link._adopting.clear()


async def _settled(done) -> None:
    """Let the recovery's task end, its last step (off the count) included."""
    for _ in range(100):
        await asyncio.sleep(0.02)
        if done() and not satellite_link._adopting.get("machine-1"):
            return


@pytest.mark.asyncio
async def test_a_runs_recovery_is_on_its_way_until_its_task_ends(temp_db, monkeypatch, placed):  # noqa: F811
    import core.session.session_manager as sm
    run_id, _chat_id, sid = _mk_remote_run(temp_db)
    placed(sid)
    run_recovery.defer_orphaned_runs()
    layer = _FakeLayer()
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    await run_recovery.on_sessions_alive("machine-1", [{"session_id": sid, "turn_active": True, "command_id": "c1"}])
    # From the moment the report's callback returns.
    assert satellite_link._adopting.get("machine-1") == {sid}
    await _settled(lambda: task_store.get_run(run_id)["status"] != "running")
    assert layer.adopted == [sid] and task_store.get_run(run_id)["status"] == "completed"
    assert not satellite_link._adopting.get("machine-1")


@pytest.mark.asyncio
async def test_a_plain_chats_recovery_is_on_its_way_too(temp_db, monkeypatch, placed):  # noqa: F811
    import core.session.session_manager as sm
    from core.session import session_state
    _run_id, _chat_id, sid = _mk_remote_run(temp_db, status="completed")
    placed(sid, mode="default")
    session_state.mark_recover_pending(sid)
    layer = _FakeLayer()
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    await run_recovery.on_sessions_alive("machine-1", [{
        "session_id": sid, "turn_active": False, "command_id": "c9", "buffered_events": 12}])
    assert satellite_link._adopting.get("machine-1") == {sid}
    await _settled(lambda: bool(layer.adopted))
    assert layer.adopted == [sid]
    assert not satellite_link._adopting.get("machine-1")


@pytest.mark.asyncio
async def test_a_recovery_that_reaches_no_binding_is_off_the_count_when_its_task_ends(temp_db, monkeypatch):
    import core.session.session_manager as sm
    run_id, chat_id, sid = _mk_remote_run(temp_db)
    ended: list[tuple] = []

    async def _fail(run, chat, reason, **kw):
        ended.append((run, chat, reason))
    monkeypatch.setattr(run_recovery, "_fail_and_report", _fail)
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: None)
    satellite_link.adoption_begins("machine-1", sid)
    await run_recovery._recover_session(
        "machine-1", sid, {"session_id": sid}, run_id=run_id, chat_id=chat_id, agent="pa")
    assert ended == [(run_id, chat_id, run_recovery.RUN_SESSION_CLOSED)]
    assert not satellite_link._adopting.get("machine-1")
