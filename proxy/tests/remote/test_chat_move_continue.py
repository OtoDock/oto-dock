"""Moving a chat that continues work: a chat a task continued once, a
delegated lane, where a continue runs (``api/tasks/delegation``,
``core/config/task_config_builder.task_target``), and the agent's move
armed again after a restart (``services/remote/chat_move``)."""

import asyncio
import uuid

import pytest

from services.remote import chat_move
from services.scheduler import task_kinds
from tests.remote.test_chat_move import (  # noqa: F401  (``online`` is a fixture)
    _agent,
    _asker,
    _chat,
    _machine,
    _user,
    online,
)


def _task(agent, sid, *, kind=task_kinds.SCHEDULED, schedule=None, run_at=None,
          enabled=True, fired=False, chat_id=None, owner="u-ann"):
    """A ``dynamic_tasks`` row naming ``sid`` as its ``continue_session``."""
    from storage import database as db
    from storage.pg import get_conn
    tid = f"dyn-{uuid.uuid4().hex[:8]}"
    db.create_dynamic_task(tid, agent, "t", "go", "cli", kind, schedule, run_at, None, 600,
                           owner, continue_session=sid, target_chat_id=chat_id)
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET enabled=%s, fired=%s WHERE id=%s",
                     (enabled, fired, tid))
        conn.commit()
    return tid


def _run(task_id, agent, status, *, chat_id=None):
    from storage import database as db
    rid = f"run-{uuid.uuid4().hex[:8]}"
    db.create_run(rid, task_id, agent, "manual", None, "go", task_type=task_kinds.RUN_DELEGATE)
    db.update_run(rid, status=status, **({"chat_id": chat_id} if chat_id else {}))
    return rid


# -- a chat a task continued once ------------------------------------------------------

def test_a_finished_delegate_row_no_longer_keeps_the_chat(temp_db, online):  # noqa: F811
    """A delegate's row is meant to go with its run; one that stayed kept
    its chat unmovable and unreloadable for good."""
    from services.remote import chat_reload
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    tid = _task(slug, sid, kind=task_kinds.DELEGATE, chat_id=cid)
    _run(tid, slug, "completed", chat_id=cid)
    chat = db.get_chat(cid)
    me = _asker("u-ann", "manager")
    chat_move._validate(chat, "m-ann", me, expect_from=None, check_turn=False)
    assert chat_move.targets_view(chat, me)["can_move"]
    assert chat_reload._refusal(chat, me) != chat_reload.DELEGATED_WORK
    assert not remote_store.session_refs_shared(cid, [sid])


@pytest.mark.parametrize("row", [
    {"kind": task_kinds.SCHEDULED, "schedule": "0 9 * * *"},
    {"kind": task_kinds.SCHEDULED, "schedule": "0 9 * * *", "enabled": False},
    {"kind": task_kinds.TRIGGER},
    {"kind": task_kinds.ONE_TIME, "run_at": "2099-01-01T09:00:00"},
])
def test_a_task_that_can_still_fire_keeps_the_chat(temp_db, online, row):  # noqa: F811
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _task(slug, sid, **row)
    me = _asker("u-ann", "manager")
    with pytest.raises(chat_move.MoveRefused, match=chat_move.CONTINUES_WORK):
        chat_move._validate(db.get_chat(cid), "m-ann", me, expect_from=None, check_turn=False)
    assert remote_store.session_refs_shared(cid, [sid])


@pytest.mark.parametrize("row", [
    {"kind": task_kinds.ONE_TIME, "run_at": "2026-01-01T09:00:00", "fired": True},
    {"kind": task_kinds.ONE_TIME},                       # a Run-now row, no clock
])
def test_a_task_that_cannot_fire_again_lets_the_chat_go(temp_db, online, row):  # noqa: F811
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _task(slug, sid, **row)
    chat_move._validate(db.get_chat(cid), "m-ann", _asker("u-ann", "manager"),
                        expect_from=None, check_turn=False)


def test_a_live_delegate_round_still_counts_for_the_reload_and_the_accounts(
        temp_db, online):  # noqa: F811
    from storage import database as db
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    tid = _task(slug, sid, kind=task_kinds.DELEGATE, chat_id=cid)
    rid = _run(tid, slug, "pending")
    assert chat_move._continues_delegated_work(db.get_chat(cid))
    db.update_run(rid, status="completed")
    assert not chat_move._continues_delegated_work(db.get_chat(cid))


# -- a lane moves once no turn is open -----------------------------------------------------

def _lane(slug, *, pin="m-ann", sid=None):
    """A delegated worker chat of Ann's, its round over."""
    from storage import database as db
    cid = _chat(slug, "u-ann", pin=pin, sid=sid or str(uuid.uuid4()))
    db.update_chat(cid, parent_chat_id=str(uuid.uuid4()))
    return cid


def _move_now(cid, target, asker=None):
    from tests.remote.test_chat_move import _settled

    async def go():
        await chat_move.request_move(cid, target, asker=asker or _asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())


def test_a_lane_whose_round_ended_moves_with_its_files(temp_db, online, monkeypatch):  # noqa: F811
    from storage import database as db, move_schema, remote_store
    from tests.remote.test_chat_move import _FakeFiles
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _lane(slug, sid=sid)
    me = _asker("u-ann", "manager")
    view = chat_move.targets_view(db.get_chat(cid), me)
    assert view["can_move"] and not view["refusal"]
    files = _FakeFiles(monkeypatch)
    _move_now(cid, "m-two")
    row = db.get_chat(cid)
    assert row["execution_target"] == "m-two" and row["session_id"] == sid
    assert remote_store.latest_chat_move(cid)["memory"] == move_schema.MEMORY_FILES
    assert files.calls == [("m-ann", "m-two")]


def test_a_lane_with_a_round_queued_or_running_waits(temp_db, online):  # noqa: F811
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _lane(slug, sid=sid)
    tid = _task(slug, sid, kind=task_kinds.DELEGATE, chat_id=cid)
    rid = _run(tid, slug, "pending")
    chat = db.get_chat(cid)
    assert chat_move.open_turn(chat)
    with pytest.raises(chat_move.MoveRefused, match=chat_move.LANE_BUSY):
        chat_move._validate(chat, "m-two", _asker("u-ann", "manager"), expect_from=None,
                            check_turn=True)
    db.update_run(rid, status="completed")
    assert not chat_move.open_turn(db.get_chat(cid))


def test_the_agent_never_moves_its_own_lane(temp_db, online):  # noqa: F811
    """Its resume on the new place would run a turn its delegator never
    hears; the person moves it from the menu."""
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    cid = _lane(_agent())
    me = _asker("u-ann", "manager")
    with pytest.raises(chat_move.MoveRefused, match=chat_move.CONTINUES_WORK):
        asyncio.run(chat_move.schedule_after_turn(cid, "m-two", asker=me))
    view = chat_move.targets_view(db.get_chat(cid), me, own_move=True)
    assert view["refusal"] == chat_move.CONTINUES_WORK and not view["can_move"]


def test_a_lane_moves_only_with_its_memory(temp_db, online, monkeypatch):  # noqa: F811
    from core.remote import session_files as sf
    from storage import database as db, move_schema
    from tests.remote.test_chat_move import _FakeFiles, _frames
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.add("m-two")                                     # its place is offline
    slug = _agent()
    cid = _lane(slug)
    with pytest.raises(chat_move.MoveRefused) as e:
        chat_move._validate(db.get_chat(cid), "m-two", _asker("u-ann", "manager"),
                            expect_from=None, check_turn=False)
    assert str(e.value) == ("This chat continues delegated work, so it moves only with its "
                            "memory. Move it when Ann laptop is back.")
    # A copy that fails twice leaves it where it was, never on a summary.
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _lane(slug, sid=sid)
    _FakeFiles(monkeypatch, fail=move_schema.WHY_TRANSFER_FAILED)
    frames = _frames(monkeypatch)
    _move_now(cid, "m-two")
    row = db.get_chat(cid)
    assert row["execution_target"] == "m-ann" and row["session_id"] == sid
    assert not row["pending_history_seed"]
    assert frames[-1]["message"] == "The move to Ann desktop did not finish. The chat stays on Ann laptop."
    assert sf  # the module the fake replaced


def test_a_message_never_hands_a_lane_over(temp_db, online, monkeypatch):  # noqa: F811
    from core.session.visibility import shared_chat_owner
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent(default_scope="agent", collaborative=False)
    cid = _chat(slug, shared_chat_owner(slug), pin="m-ann")
    db.update_chat(cid, parent_chat_id=str(uuid.uuid4()))
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    with pytest.raises(chat_move.MoveRefused) as e:
        asyncio.run(chat_move.hand_over(cid, asker=_asker("u-bob", "editor")))
    assert str(e.value) == ("This chat continues delegated work. Move it from the Runs on menu, "
                            "then send your message.")


# -- the agent's move armed again after a restart --------------------------------------------

def _rearm_world(monkeypatch, up, *, role="manager"):
    """Ann's chat on her laptop, a stored standing on its agent, and the
    agent's move to her desktop waiting when the platform stopped (its row
    taken, as the boot takes it). The move's start is recorded."""
    from storage.chat import pending_rebuilds
    from storage.identity import db_users
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    up.update({"m-ann", "m-two"})
    slug = _agent()
    if role:
        db_users.add_user_agent("u-ann", slug, role, "u-admin")
    cid = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))
    pending_rebuilds.save(cid, pending_rebuilds.MOVE, person="u-ann", wait_s=600, target="m-two")
    row = pending_rebuilds.take(cid)
    started = []

    async def _start(chat, hit, person, **kw):
        started.append((hit.target, kw.get("resume"), kw["asker"].role))
    monkeypatch.setattr(chat_move, "_start", _start)
    monkeypatch.setattr(chat_move, "TURN_POLL_S", 0.01)
    return cid, row, started


async def _waiter_done(cid, timeout=5.0):
    task = chat_move._waiters.get(cid)
    if task is not None:
        await asyncio.wait_for(task, timeout)


def _said(cid):
    import json

    from storage import database as db
    return [json.loads(m["event_data"]).get("message") for m in db.get_chat_messages(cid)
            if m.get("event_type") == "system"]


def test_a_move_waiting_at_a_restart_goes_on_when_its_session_lives_on(
        temp_db, online, monkeypatch):  # noqa: F811
    from services.remote import chat_reload
    from storage.chat import pending_rebuilds
    cid, row, started = _rearm_world(monkeypatch, online)

    async def go():
        await chat_reload.settle_after_restart(row, continued=False, lives_on=True)
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == [("m-two", True, "manager")]
    assert _said(cid) == [] and pending_rebuilds.listed() == []


def test_a_restart_that_ended_the_session_says_the_chat_did_not_move(
        temp_db, online, monkeypatch):  # noqa: F811
    from services.remote import chat_reload
    from storage.chat import pending_rebuilds
    cid, row, started = _rearm_world(monkeypatch, online)

    async def go():
        await chat_reload.settle_after_restart(row, continued=False, lives_on=False)
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == [] and cid not in chat_move._waiters
    assert _said(cid) == ["The chat did not move: the platform restarted. Ask again."]
    assert pending_rebuilds.listed() == []


def test_the_continue_pass_goes_first(temp_db, online, monkeypatch):  # noqa: F811
    """Settled while the pass holds the chat's cut turn, the waiter takes
    no claim (the pass's start would be refused) until the record settles,
    and the move then waits for the continued turn's end."""
    from core.events import input_queue
    from services.remote import chat_reload
    from storage.chat import cut_records
    cid, row, started = _rearm_world(monkeypatch, online)
    record = cut_records.add(cid, cause=cut_records.CAUSE_CRASH, state=cut_records.RUNNING,
                             session_id="s", engine_open=True)

    async def go():
        await chat_reload.settle_after_restart(row, continued=True)
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert started == [] and not input_queue.is_claimed(cid)
        cut_records.settle(record["id"], cut_records.CONTINUED)
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == [("m-two", True, "manager")]


def test_a_person_who_lost_the_agent_by_the_fire_is_refused(temp_db, online, monkeypatch):  # noqa: F811
    from services.remote import chat_reload
    cid, row, started = _rearm_world(monkeypatch, online, role="")

    async def go():
        await chat_reload.settle_after_restart(row, continued=False, lives_on=True)
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == []
    assert _said(cid) == [f"The chat did not move. {chat_move.NOT_MOVER}"]


def test_a_rearmed_move_waits_for_its_machine_to_come_back(temp_db, online, monkeypatch):  # noqa: F811
    from services.remote import chat_reload
    cid, row, started = _rearm_world(monkeypatch, online)
    online.discard("m-ann")                       # not back yet after the restart

    async def go():
        await chat_reload.settle_after_restart(row, continued=False, lives_on=True)
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert started == []
        online.add("m-ann")
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == [("m-two", True, "manager")]


def test_the_agents_move_reads_its_persons_standing_when_it_fires(temp_db, online, monkeypatch):  # noqa: F811
    """The live path, no restart: the move is asked by a manager inside a
    turn, the person loses the agent before the turn ends, and the move that
    fires then is refused on their standing now, not the role the call
    captured."""
    from storage.identity import db_users
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    slug = _agent()
    db_users.add_user_agent("u-ann", slug, "manager", "u-admin")
    cid = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))
    started = []

    async def _start(chat, hit, person, **kw):
        started.append(hit.target)
    monkeypatch.setattr(chat_move, "_start", _start)
    monkeypatch.setattr(chat_move, "TURN_POLL_S", 0.01)
    turn = {"open": True}
    monkeypatch.setattr(chat_move, "open_turn", lambda chat: turn["open"])

    async def go():
        await chat_move.schedule_after_turn(cid, "m-two", asker=_asker("u-ann", "manager"))
        await asyncio.sleep(0.05)
        db_users.set_user_agents("u-ann", [], "u-admin")
        turn["open"] = False
        await _waiter_done(cid)
    asyncio.run(go())
    assert started == []
    assert _said(cid) == [f"The chat did not move. {chat_move.NOT_MOVER}"]


def test_a_lanes_hand_over_says_the_more_exact_sentence_first(temp_db, online, monkeypatch):  # noqa: F811
    """A lane on a compute machine, an admin machine the sender cannot use
    or another person's computer is told what is true there; only a lane
    the hand-over would move hears the lane's own sentence."""
    from core import placement
    from storage import database as db, remote_store
    _user("u-ann", name="Ann")
    made = remote_store.create_remote_machine(machine_id="c-2", name="Compute two", registered_by=None,
                                              pairing_scope=placement.PAIRING_COMPUTE)
    cid = _chat(_agent(), "u-ann", pin=made["id"])
    db.update_chat(cid, parent_chat_id=str(uuid.uuid4()))
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    with pytest.raises(chat_move.MoveRefused) as e:
        asyncio.run(chat_move.hand_over(cid, asker=_asker("u-ann", "manager")))
    assert str(e.value) == ("This chat is on Compute two, a compute machine. Move it from the Runs "
                            "on menu.")
