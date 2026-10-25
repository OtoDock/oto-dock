"""Where a chat runs, chosen per chat: the offered places
(``services/remote/chat_targets``), the move with its files or its summary
(``services/remote/chat_move``), the proof sweep, the lock and the
hand-over, and the machine cascade's settlements."""

import asyncio
import uuid

import pytest

from core import placement
from services.remote import chat_move, chat_targets
from storage import move_schema


def _user(sub, role="member", name=None):
    from storage import database as db
    db.upsert_user(sub, f"{sub}@t.io", name or sub, role, display_name=name or sub)


def _machine(mid, name, *, scope="user", owner="u-ann", engines=None):
    import json

    from storage.pg import get_conn
    caps = json.dumps({"engines": engines} if engines else {})
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, pairing_scope, capabilities,"
            " created_at) VALUES (%s, %s, %s, %s, %s, '2026-10-19T00:00:00+00:00')",
            (mid, name, owner, scope, caps))
        conn.commit()


def _agent(**kw):
    from tests.fixtures.ws_dashboard_harness import make_test_agent
    return make_test_agent(**kw)


def _chat(agent, owner, *, pin="local", sid=None, thread=None, engine="claude-code-cli"):
    from storage import database as db
    cid = str(uuid.uuid4())
    db.create_chat(cid, owner, agent, "default", execution_path=engine)
    db.update_chat(cid, execution_target=pin, session_id=sid, codex_thread_id=thread)
    return cid


@pytest.fixture
def online(monkeypatch):
    """Machines reachable by name: the set the test fills."""
    from services.remote import remote_status
    up: set[str] = set()

    def _live(mid, *, machine=None):
        ok = mid in up
        return {"state": "online" if ok else "disconnected", "last_heartbeat_age_s": 1,
                "last_seen_iso": "", "reachable": ok}
    monkeypatch.setattr(remote_status, "get_live_machine_status", _live)
    monkeypatch.setattr(remote_status, "is_reachable", lambda mid: mid in up)
    from core.remote import satellite_connection as sc
    cm = sc.get_connection_manager()
    monkeypatch.setattr(cm, "satellite_engines", lambda mid: frozenset())
    monkeypatch.setattr(cm, "satellite_supports_session_move", lambda mid: mid in up)
    monkeypatch.setattr(cm, "is_connected", lambda mid: mid in up)
    return up


def _asker(sub, role, admin=False):
    return chat_move.Asker(sub=sub, role=role, platform_admin=admin)


# -- the offered places -----------------------------------------------------------

def test_a_person_is_offered_the_server_their_default_and_their_own_machines(temp_db, online):
    _user("u-ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-bob", "Bob laptop", owner="u-bob")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    online.update({"m-ann", "m-srv"})
    slug = _agent()
    default, targets = chat_targets.offered(slug, chat_targets.RunPerson("u-ann", "manager"),
                                            engine_path="claude-code-cli", admin_asker=False)
    by = {t.target: t for t in targets}
    assert default == placement.LOCAL and by[placement.LOCAL].is_default
    assert set(by) == {placement.LOCAL, "m-ann"}          # never Bob's, no admin machine
    assert by["m-ann"].pickable and by["m-ann"].pairing == placement.PAIRING_USER


def test_an_admin_asker_sees_the_admin_machines_only_for_an_owner_tier_run(temp_db, online):
    _user("u-ann"); _user("u-admin", "admin")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    online.add("m-srv")
    slug = _agent()
    _d, as_manager = chat_targets.offered(slug, chat_targets.RunPerson("u-admin", "admin"),
                                          engine_path="claude-code-cli", admin_asker=True)
    assert "m-srv" in {t.target for t in as_manager}
    _d, for_editor = chat_targets.offered(slug, chat_targets.RunPerson("u-ann", "editor"),
                                          engine_path="claude-code-cli", admin_asker=True)
    assert "m-srv" not in {t.target for t in for_editor}


def test_an_offline_machine_is_listed_but_not_pickable(temp_db, online):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    _d, targets = chat_targets.offered(slug, chat_targets.RunPerson("u-ann", "manager"),
                                       engine_path="claude-code-cli", admin_asker=False)
    ann = next(t for t in targets if t.target == "m-ann")
    assert not ann.pickable and ann.why_not == "Ann laptop is offline."
    # The menu reads the offline case from the guard's answer: its dot says it.
    assert ann.allowed and ann.view()["allowed"] is True


def test_a_machine_that_does_not_run_the_engine_is_not_pickable(temp_db, online, monkeypatch):
    from core.remote import satellite_connection as sc
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    monkeypatch.setattr(sc.get_connection_manager(), "satellite_engines",
                        lambda mid: frozenset({"some-other-engine"}))
    slug = _agent()
    _d, targets = chat_targets.offered(slug, chat_targets.RunPerson("u-ann", "manager"),
                                       engine_path="claude-code-cli", admin_asker=False)
    ann = next(t for t in targets if t.target == "m-ann")
    assert ann.why_not == "Ann laptop can't run this chat's engine."


def test_personal_machines_go_when_the_policy_is_off(temp_db, online):
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    db.set_platform_setting("allow_user_paired_machines", "0")
    slug = _agent()
    _d, targets = chat_targets.offered(slug, chat_targets.RunPerson("u-ann", "manager"),
                                       engine_path="claude-code-cli", admin_asker=False)
    assert "m-ann" not in {t.target for t in targets}


def test_a_machine_the_guard_refuses_says_so_plainly(temp_db, online, monkeypatch):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    monkeypatch.setattr(placement, "may_run_on", lambda m, **kw: False)
    _d, targets = chat_targets.offered(_agent(), chat_targets.RunPerson("u-ann", "manager"),
                                       engine_path="claude-code-cli", admin_asker=False)
    ann = next(t for t in targets if t.target == "m-ann")
    assert ann.why_not == "You can't run this chat on Ann laptop." and not ann.pickable
    assert ann.view()["allowed"] is False


def test_a_task_machine_is_resolved_with_its_scope_rules(temp_db, online):
    _user("u-ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    slug = _agent()
    mine = chat_targets.RunPerson("u-ann", "manager")
    assert chat_targets.resolve_work_machine(slug, "Ann laptop", person=mine,
                                             admin_asker=False) == ("m-ann", "")
    assert chat_targets.resolve_work_machine(slug, "default", person=mine,
                                             admin_asker=False) == ("", "")
    machine, why = chat_targets.resolve_work_machine(
        slug, "Ann laptop", person=chat_targets.RunPerson(None, "manager"), admin_asker=True)
    assert machine == "" and "is not a machine this work can run on" in why
    assert chat_targets.resolve_work_machine(
        slug, "Server", person=chat_targets.RunPerson(None, "manager"),
        admin_asker=True) == ("m-srv", "")


# -- the chip's answer ------------------------------------------------------------

def test_the_chip_marks_the_current_place_and_says_who_may_move(temp_db, online):
    from storage import database as db
    _user("u-ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", pin="m-ann")
    view = chat_move.targets_view(db.get_chat(cid), _asker("u-ann", "manager"))
    assert view["current"]["target"] == "m-ann" and view["current"]["pairing"] == placement.PAIRING_USER
    assert next(t for t in view["targets"] if t["target"] == "m-ann")["is_current"]
    assert view["can_move"] and view["lock"] is None
    other = chat_move.targets_view(db.get_chat(cid), _asker("u-bob", "manager"))
    assert not other["can_move"] and other["refusal"] == chat_move.NOT_MOVER


def test_a_direct_llm_chat_stays_on_the_server(temp_db, online):
    from storage import database as db
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", engine="direct-llm")
    view = chat_move.targets_view(db.get_chat(cid), _asker("u-ann", "manager"))
    assert view["refusal"] == chat_move.DIRECT_LOCAL and not view["can_move"]


# -- the lock ---------------------------------------------------------------------

def test_a_shared_chat_on_a_persons_computer_is_locked_for_the_others(temp_db, online, monkeypatch):
    from core.session.visibility import shared_chat_owner
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent(default_scope="agent", collaborative=False)
    cid = _chat(slug, shared_chat_owner(slug), pin="m-ann", sid=str(uuid.uuid4()))
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: True)
    chat = db.get_chat(cid)
    assert chat_move.lock_sentence(chat, "u-bob") == "This chat is running on Ann's computer."
    assert chat_move.lock_sentence(chat, "u-ann") == ""
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    assert chat_move.lock_sentence(chat, "u-bob") == ""


def test_the_lock_holds_from_the_owners_move_until_the_first_turn(temp_db, online, monkeypatch):
    from core.session.visibility import shared_chat_owner
    from storage import database as db, remote_store
    _user("u-ann", name="Ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent(default_scope="agent", collaborative=False)
    cid = _chat(slug, shared_chat_owner(slug), pin="m-ann")
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": cid, "target": "m-ann",
                                   "actor": "u-ann", "state": move_schema.AWAITING_RESUME,
                                   "created_at": chat_move._now()})
    assert chat_move.lock_sentence(db.get_chat(cid), "u-bob").startswith("This chat is running on Ann")


# -- the move job -----------------------------------------------------------------

class _FakeFiles:
    """``core.remote.session_files`` with its transfer faked."""

    def __init__(self, monkeypatch, *, fail=None):
        from core.remote import session_files as sf
        self.calls = []
        self.deleted = []

        async def copy_session(scope, *, source, target, move_id, progress=None):
            self.calls.append((source, target))
            if fail:
                raise sf.TransferError(fail, "faked")
            return sf.CopyResult(files=[sf.SessionFile(f".claude/projects/k/{scope.session_id}.jsonl",
                                                       10, "sha256:x")],
                                 bytes=10, main_rel=f".claude/projects/k/{scope.session_id}.jsonl",
                                 main_size=10)

        async def discard(place, scope, move_id):
            pass

        async def delete_files(place, scope):
            self.deleted.append(place)
            return 1

        monkeypatch.setattr(sf, "copy_session", copy_session)
        monkeypatch.setattr(sf, "discard", discard)
        monkeypatch.setattr(sf, "delete_files", delete_files)


async def _settled(cid, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if not chat_move.moving(cid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the move did not end")


def _frames(monkeypatch):
    from services.notifications import notification_manager as nm
    seen = []
    monkeypatch.setattr(nm, "broadcast_chat_moved",
                        lambda owner, agent, actor, frame, **kw: seen.append(frame))
    return seen


def test_a_move_with_files_keeps_the_ids_and_waits_for_proof(temp_db, online, monkeypatch):
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    files = _FakeFiles(monkeypatch)
    frames = _frames(monkeypatch)

    async def go():
        res = await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"),
                                           expect_from="local")
        assert res["to_label"] == "Ann laptop" and res["memory"] == move_schema.MEMORY_FILES
        await _settled(cid)
    asyncio.run(go())
    row = db.get_chat(cid)
    assert row["execution_target"] == "m-ann" and row["session_id"] == sid
    move = remote_store.latest_chat_move(cid)
    assert move["state"] == move_schema.AWAITING_RESUME and move["memory"] == move_schema.MEMORY_FILES
    assert files.calls == [("local", "m-ann")]
    assert frames[-1]["ok"] and frames[-1]["memory"] == move_schema.MEMORY_FILES
    rows = [m for m in db.get_chat_messages(cid) if m.get("event_type") == "system"]
    moved = "This chat moved from the OtoDock platform to Ann laptop.\""
    assert any(moved in (m.get("event_data") or "") for m in rows)


def test_a_failed_transfer_falls_back_to_the_summary_at_the_chosen_place(temp_db, online, monkeypatch):
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _FakeFiles(monkeypatch, fail=move_schema.WHY_TRANSFER_FAILED)
    frames = _frames(monkeypatch)

    async def go():
        await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())
    row = db.get_chat(cid)
    move = remote_store.latest_chat_move(cid)
    assert row["execution_target"] == "m-ann" and row["session_id"] is None
    assert row["pending_history_seed"] == f"moved:{move['id']}"
    assert move["memory"] == move_schema.MEMORY_SUMMARY and move["why"] == move_schema.WHY_TRANSFER_FAILED
    assert frames[-1]["ok"] and frames[-1]["memory"] == move_schema.MEMORY_SUMMARY


def _old(monkeypatch, online, *old):
    """Machines online whose satellite cannot move the files (0.5.156)."""
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc.get_connection_manager(), "satellite_supports_session_move",
                        lambda mid: mid in online and mid not in old)


def test_the_chips_answer_says_which_places_move_the_files(temp_db, online, monkeypatch):
    """Each place says whether a move with it carries the session files: the
    platform always, a machine whose satellite moves them, never an older
    one. The current place says so too when the person is not offered it."""
    from storage import database as db
    _user("u-ann"); _user("u-bob")
    _machine("m-new", "Ann laptop", owner="u-ann")
    _machine("m-old", "Ann desktop", owner="u-ann")
    _machine("m-bob", "Bob laptop", owner="u-bob")
    online.update({"m-new", "m-old", "m-bob"})
    _old(monkeypatch, online, "m-old", "m-bob")
    slug = _agent()
    view = chat_move.targets_view(db.get_chat(_chat(slug, "u-ann")), _asker("u-ann", "manager"))
    by = {t["target"]: t for t in view["targets"]}
    assert by[placement.LOCAL]["moves_files"] is True
    assert by["m-new"]["moves_files"] is True and by["m-old"]["moves_files"] is False
    # A chat of Ann's on Bob's computer: the current place is not offered.
    cur = chat_move.targets_view(db.get_chat(_chat(slug, "u-ann", pin="m-bob")),
                                 _asker("u-ann", "manager"))
    here = next(t for t in cur["targets"] if t["is_current"])
    assert here["target"] == "m-bob" and here["moves_files"] is False


def test_a_move_to_an_older_satellite_promises_and_gets_a_summary(temp_db, online, monkeypatch):
    """The answer says what the move will carry, and the card names only the
    place whose satellite could not take the files, never the platform."""
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _old(monkeypatch, online, "m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    files = _FakeFiles(monkeypatch)
    frames = _frames(monkeypatch)

    async def go():
        res = await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"))
        assert res["memory"] == move_schema.MEMORY_SUMMARY
        await _settled(cid)
    asyncio.run(go())
    move = remote_store.latest_chat_move(cid)
    assert move["memory"] == move_schema.MEMORY_SUMMARY and move["why"] == move_schema.WHY_OLD_TARGET
    assert files.calls == [] and db.get_chat(cid)["execution_target"] == "m-ann"
    assert frames[-1]["ok"] and frames[-1]["message"] == (
        "This chat moved from the OtoDock platform to Ann laptop. Ann laptop runs an older "
        "satellite, so it continues from a summary of the conversation.")


def test_the_agents_move_promises_a_summary_from_an_older_satellite(temp_db, online,
                                                                     monkeypatch):
    """The tool's answer ("continues there from a summary") follows the
    satellite of the place the chat leaves too."""
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _old(monkeypatch, online, "m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))

    async def _wait(chat_id, target, asker, blocked=None, *, resume=False):
        pass
    monkeypatch.setattr(chat_move, "_wait_and_move", _wait)
    res = asyncio.run(chat_move.schedule_after_turn(cid, "default",
                                                    asker=_asker("u-ann", "manager")))
    assert res["memory"] == move_schema.MEMORY_SUMMARY
    _old(monkeypatch, online)
    res = asyncio.run(chat_move.schedule_after_turn(cid, "default",
                                                    asker=_asker("u-ann", "manager")))
    assert res["memory"] == move_schema.MEMORY_FILES


def test_the_older_side_is_named(temp_db, online, monkeypatch):
    online.update({"m-1", "m-2"})
    _old(monkeypatch, online, "m-1")
    assert chat_move._old_satellite("m-1", "local") == move_schema.WHY_OLD_SOURCE
    assert chat_move._old_satellite("local", "m-1") == move_schema.WHY_OLD_TARGET
    assert chat_move._old_satellite("m-2", "m-1") == move_schema.WHY_OLD_TARGET
    assert chat_move._old_satellite("local", "m-2") == ""
    _old(monkeypatch, online, "m-1", "m-2")
    assert chat_move._old_satellite("m-1", "m-2") == move_schema.WHY_OLD_SATELLITE


def test_a_target_that_drops_mid_copy_leaves_the_chat_where_it_was(temp_db, online, monkeypatch):
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    from core.remote import session_files as sf

    async def copy_session(scope, *, source, target, move_id, progress=None):
        online.discard("m-ann")
        raise sf.TransferError(move_schema.WHY_TRANSFER_FAILED, "dropped")

    async def discard(place, scope, move_id):
        pass
    monkeypatch.setattr(sf, "copy_session", copy_session)
    monkeypatch.setattr(sf, "discard", discard)
    frames = _frames(monkeypatch)

    async def go():
        await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())
    row = db.get_chat(cid)
    assert row["execution_target"] == "local" and row["session_id"] == sid
    assert remote_store.latest_chat_move(cid)["state"] == move_schema.FAILED
    assert not frames[-1]["ok"] and "went offline" in frames[-1]["message"]


def test_a_second_pick_while_a_move_runs_is_refused(temp_db, online, monkeypatch):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    gate = asyncio.Event()
    from core.remote import session_files as sf

    async def copy_session(scope, *, source, target, move_id, progress=None):
        await gate.wait()
        raise sf.TransferError(move_schema.WHY_TRANSFER_FAILED)

    async def discard(place, scope, move_id):
        pass
    monkeypatch.setattr(sf, "copy_session", copy_session)
    monkeypatch.setattr(sf, "discard", discard)
    _frames(monkeypatch)

    async def go():
        await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"))
        with pytest.raises(chat_move.MoveRefused, match="This chat is moving to Ann laptop."):
            await chat_move.request_move(cid, "local", asker=_asker("u-ann", "manager"))
        gate.set()
        await _settled(cid)
    asyncio.run(go())


def test_the_validation_sentences(temp_db, online):
    from storage import database as db
    _user("u-ann"); _user("u-bob")
    _machine("m-bob", "Bob laptop", owner="u-bob")
    online.add("m-bob")
    slug = _agent()
    cid = _chat(slug, "u-ann")
    chat = db.get_chat(cid)
    with pytest.raises(chat_move.MoveRefused, match="This chat already runs there."):
        chat_move._validate(chat, "local", _asker("u-ann", "manager"), expect_from=None,
                            check_turn=False)
    with pytest.raises(chat_move.MoveRefused, match="You can't move this chat there."):
        chat_move._validate(chat, "m-bob", _asker("u-ann", "manager"), expect_from=None,
                            check_turn=False)
    with pytest.raises(chat_move.MoveRefused, match=chat_move.NOT_MOVER):
        chat_move._validate(chat, "local", _asker("u-bob", "manager"), expect_from=None,
                            check_turn=False)


# -- the proof sweep -----------------------------------------------------------------

def _awaiting(cid, *, sid, source="local", target="m-ann", main_size=10):
    from storage import remote_store
    mid = str(uuid.uuid4())
    remote_store.create_chat_move({
        "id": mid, "chat_id": cid, "agent": "a", "engine": "claude-code-cli",
        "session_id": sid, "resume_handle": "", "source": source, "target": target,
        "from_label": "OtoDock platform", "to_label": "Ann laptop",
        "memory": move_schema.MEMORY_FILES, "state": move_schema.AWAITING_RESUME,
        "main_rel": f".claude/projects/k/{sid}.jsonl", "main_size": main_size,
        "created_at": chat_move._now()})
    return mid


def test_the_source_copy_goes_only_once_the_target_file_grew(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    from storage import database as db, remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", pin="m-ann", sid=sid)
    mid = _awaiting(cid, sid=sid)
    sizes = {"now": 10}
    deleted = []

    async def main_size(place, scope, rel):
        return sizes["now"]

    async def delete_files(place, scope):
        deleted.append(place)
        return 1
    monkeypatch.setattr(sf, "main_size", main_size)
    monkeypatch.setattr(sf, "delete_files", delete_files)
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(mid)["state"] == move_schema.AWAITING_RESUME and not deleted
    sizes["now"] = 99
    chat_move._proof_next.clear()          # past the row's back-off
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(mid)["state"] == move_schema.SETTLED
    assert deleted == ["local"]
    assert db.get_chat(cid)["execution_target"] == "m-ann"


def test_a_move_back_while_the_sweep_waits_keeps_the_source_copy(temp_db, online, monkeypatch):
    """The target's size can take a while: a move of the chat that started
    meanwhile (back to the source, say) leaves the source's files alone."""
    from core.remote import session_files as sf
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", pin="m-ann", sid=sid)
    mid = _awaiting(cid, sid=sid)
    deleted = []

    async def main_size(place, scope, rel):
        chat_move._running[cid] = chat_move._Job("mv-back", cid, "OtoDock platform", "local")
        return 99

    async def delete_files(place, scope):
        deleted.append(place)
        return 1
    monkeypatch.setattr(sf, "main_size", main_size)
    monkeypatch.setattr(sf, "delete_files", delete_files)
    try:
        asyncio.run(chat_move.sweep())
    finally:
        chat_move._running.pop(cid, None)
    assert deleted == [] and remote_store.get_chat_move(mid)["state"] == move_schema.AWAITING_RESUME


def test_changed_ids_and_a_newer_move_delete_nothing(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    from storage import database as db, remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", pin="m-ann", sid=sid)
    first = _awaiting(cid, sid=sid)
    db.update_chat(cid, session_id=str(uuid.uuid4()))
    called = []
    monkeypatch.setattr(sf, "delete_files", lambda *a: called.append(a))
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(first)["state"] == move_schema.REF_CHANGED
    db.update_chat(cid, session_id=sid)
    older = _awaiting(cid, sid=sid)
    import time
    time.sleep(0.01)
    _awaiting(cid, sid=sid)
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(older)["state"] == move_schema.SUPERSEDED
    assert not called


def test_a_restart_fails_the_running_moves(temp_db):
    from storage import remote_store
    cid = _chat(_agent(), "u-ann")
    mid = str(uuid.uuid4())
    remote_store.create_chat_move({"id": mid, "chat_id": cid, "state": move_schema.MOVING,
                                   "created_at": chat_move._now()})
    assert chat_move.settle_at_start() == 1
    assert remote_store.get_chat_move(mid)["state"] == move_schema.FAILED


# -- the machine cascade -----------------------------------------------------------

def test_unpairing_a_target_returns_a_chat_waiting_for_proof_to_its_source(temp_db):
    from storage import database as db, remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    sid = str(uuid.uuid4())
    slug = _agent()
    back = _chat(slug, "u-ann", pin="m-ann", sid=sid)
    mid = _awaiting(back, sid=sid)
    plain = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))
    assert remote_store.delete_remote_machine("m-ann")
    row = db.get_chat(back)
    assert row["execution_target"] == "local" and row["session_id"] == sid
    assert remote_store.get_chat_move(mid)["state"] == move_schema.RETURNED
    other = db.get_chat(plain)
    assert other["session_id"] is None and other["pending_history_seed"].startswith("machine_removed:")
    assert not row["pending_history_seed"]            # no turn ran there: the source is current


def test_a_returned_chat_gets_back_the_turns_it_ran_on_the_removed_machine(temp_db):
    import time

    from core.session import history_seed
    from storage import database as db, remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    sid = str(uuid.uuid4())
    back = _chat(_agent(), "u-ann", pin="m-ann", sid=sid)
    db.add_chat_message(back, "user", "an old question the session already holds")
    time.sleep(0.01)
    mid = _awaiting(back, sid=sid)
    time.sleep(0.01)
    db.add_chat_message(back, "user", "a question asked on the laptop")
    db.add_chat_message(back, "assistant", "the laptop's answer")
    assert remote_store.delete_remote_machine("m-ann")
    row = db.get_chat(back)
    assert row["execution_target"] == "local" and row["session_id"] == sid
    assert row["pending_history_seed"] == f"returned:{mid}"
    prompt, notice = history_seed.consume_pending_seed(back, "next")
    assert "the laptop's answer" in prompt and "a question asked on the laptop" in prompt
    assert "an old question" not in prompt and prompt.endswith("next")
    assert notice == ("This chat came back to the OtoDock platform because Ann laptop was removed. "
                      "Its turns on Ann laptop were restored from chat history. Files from "
                      "Ann laptop are not available.")


# -- the hand-over -------------------------------------------------------------------

def test_a_chat_on_a_place_the_person_may_not_use_needs_a_hand_over(temp_db, online):
    from core.session.visibility import shared_chat_owner
    from storage import database as db
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    slug = _agent(default_scope="agent", collaborative=False)
    on_ann = db.get_chat(_chat(slug, shared_chat_owner(slug), pin="m-ann"))
    on_srv = db.get_chat(_chat(slug, shared_chat_owner(slug), pin="m-srv"))
    assert chat_move.placement_blocked(on_ann, "u-bob", "editor")
    assert not chat_move.placement_blocked(on_ann, "u-ann", "editor")
    assert chat_move.placement_blocked(on_srv, "u-bob", "editor")
    assert not chat_move.placement_blocked(on_srv, "u-bob", "manager")


# -- the config builder's two reads ------------------------------------------------

def test_a_summarised_move_keeps_its_chosen_place_on_the_fresh_spawn(temp_db, online):
    from core.config import config_builder as cb
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    cid = _chat(_agent(), "u-ann", pin="m-ann")
    assert cb._moved_pin(cid) == ""                      # no move row: the resolver decides
    remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": cid, "target": "m-ann",
                                   "memory": move_schema.MEMORY_SUMMARY,
                                   "state": move_schema.SETTLED, "created_at": chat_move._now()})
    assert cb._moved_pin(cid) == ""                      # offline: the default takes it
    online.add("m-ann")
    assert cb._moved_pin(cid) == "m-ann"


def test_an_engine_switch_keeps_a_chat_moved_with_its_files_where_it_went(temp_db, online):
    """The switch drops the session ids and keeps the pin, so the next spawn
    is fresh: the place the person moved the chat to holds, whatever the move
    carried; a move that failed says nothing."""
    from core.config import config_builder as cb
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    cid = _chat(_agent(), "u-ann", pin="m-ann")
    states = (move_schema.AWAITING_RESUME, move_schema.REF_CHANGED, move_schema.SETTLED,
              move_schema.FAILED)
    for i, state in enumerate(states):
        remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": cid, "target": "m-ann",
                                       "memory": move_schema.MEMORY_FILES, "state": state,
                                       "created_at": f"2026-10-19T12:00:0{i}+00:00"})
        assert cb._moved_pin(cid) == ("" if state == move_schema.FAILED else "m-ann"), state


def test_a_chat_summarised_to_the_platform_stays_there(temp_db, online):
    """A move to the platform that could not take the files leaves the chat
    with no session; its fresh spawn stays on the platform even when the
    person's default is a machine (the resolver would name that machine)."""
    from core.config import config_builder as cb
    from storage import remote_store
    cid = _chat(_agent(), "u-ann", pin="local")
    assert cb._moved_pin(cid) == ""
    remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": cid, "target": "local",
                                   "source": "m-ann", "memory": move_schema.MEMORY_SUMMARY,
                                   "state": move_schema.SETTLED, "created_at": chat_move._now()})
    assert cb._moved_pin(cid) == placement.LOCAL
    # A move row that names a machine says nothing about a chat pinned here.
    other = _chat(_agent(), "u-ann", pin="local")
    remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": other, "target": "m-ann",
                                   "memory": move_schema.MEMORY_SUMMARY,
                                   "state": move_schema.SETTLED, "created_at": chat_move._now()})
    assert cb._moved_pin(other) == ""


# -- the progress ------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_the_progress_frames_go_at_two_points_and_half_a_second(monkeypatch):
    from services.notifications import notification_manager as nm
    clock = _Clock()
    monkeypatch.setattr(chat_move, "time", clock)
    frames = []
    monkeypatch.setattr(nm, "broadcast_chat_move_progress",
                        lambda owner, agent, actor, frame, **kw: frames.append(frame))
    job = chat_move._Job("mv-1", "c-1", "drill-sat", "m-1")
    p = chat_move._Progress(job, {"user_sub": "u-ann", "agent": "a"}, "u-ann", set())
    total = 1000
    p(0, total, "stage")                    # the first report goes
    p(0, total, "copy")                     # a new phase goes
    for done in range(1, 1000):             # a fast copy: 999 reports in no time
        p(done, total, "copy")
    assert len(frames) == 2 and job.progress == {"done": 999, "total": total, "phase": "copy"}
    p(1000, total, "copy")                  # the copy's end always goes
    p(1000, total, "commit")
    assert [(f["done"], f["phase"]) for f in frames[2:]] == [(1000, "copy"), (1000, "commit")]

    # A slow line: a report every 0.125 s, 0.5 points each: a frame once two
    # points AND half a second passed, never faster.
    frames.clear()
    q = chat_move._Progress(chat_move._Job("mv-2", "c-1", "drill-sat", "m-1"),
                            {"user_sub": "u-ann", "agent": "a"}, "u-ann", set())
    q(0, total, "copy")
    for i in range(1, 41):
        clock.now += 0.125
        q(i * 5, total, "copy")
    assert [f["done"] for f in frames] == list(range(0, 201, 20))
    assert set(frames[0]) == {"chat_id", "move_id", "to_label", "done", "total", "phase"}


def test_a_move_frame_reaches_a_focused_tab_only_when_its_person_may_read_the_chat(
        temp_db, monkeypatch):
    from services.notifications import notification_manager as nm
    _user("u-ann"); _user("u-eve"); _user("u-root", "admin")
    cid = _chat(_agent(), "u-ann")

    class _Q:
        def __init__(self):
            self.items = []

        def put_nowait(self, item):
            self.items.append(item)

    class _C:
        def __init__(self):
            self.focus = {"surface": "chat", "chat_id": cid}
            self.queue = _Q()
    tabs = {"u-ann": _C(), "u-eve": _C(), "u-root": _C()}
    monkeypatch.setattr(nm, "_user_connections", {s: [c] for s, c in tabs.items()})
    from storage import database as db
    readers = asyncio.run(chat_move._readers(db.get_chat(cid)))
    assert readers == {"u-ann", "u-root"}       # Eve's tab says it shows the chat: it may not
    nm.broadcast_chat_move_progress("u-ann", "a", "u-ann", {"chat_id": cid, "done": 1},
                                    readers=readers)
    assert [len(tabs[s].queue.items) for s in ("u-ann", "u-eve", "u-root")] == [1, 0, 1]
    assert tabs["u-ann"].queue.items[0]["type"] == "chat_move_progress"


def test_the_chips_answer_mid_move_carries_the_last_progress(temp_db, online):
    _user("u-ann")
    cid = _chat(_agent(), "u-ann")
    from storage import database as db
    job = chat_move._Job("mv-1", cid, "Ann laptop", "m-ann",
                         progress={"done": 5, "total": 10, "phase": "copy"})
    chat_move._running[cid] = job
    try:
        view = chat_move.targets_view(db.get_chat(cid), _asker("u-ann", "manager"))
    finally:
        chat_move._running.pop(cid, None)
    assert view["moving"] == {"to_label": "Ann laptop",
                              "progress": {"done": 5, "total": 10, "phase": "copy"}}


# -- the words ---------------------------------------------------------------------

def test_a_place_reads_with_its_article_and_a_machine_by_its_name(temp_db):
    _machine("m-ann", "drill-sat", owner="u-ann")
    _machine("m-look", "OtoDock Platform", owner="u-ann")
    assert chat_targets.LOCAL_LABEL == "OtoDock platform"
    assert chat_targets.place_phrase("local") == "the OtoDock platform"
    assert chat_targets.place_phrase("", start=True) == "The OtoDock platform"
    assert chat_targets.place_phrase("m-ann") == "drill-sat"
    assert chat_targets.place_phrase("m-ann", "drill-sat", start=True) == "drill-sat"
    # A machine named like the platform says it is a computer, and takes no article.
    assert chat_targets.label_of("m-look") == "OtoDock Platform (computer)"
    assert chat_targets.place_phrase("m-look") == "OtoDock Platform (computer)"


def test_the_platform_is_found_by_its_names_before_a_machine(temp_db, online):
    _user("u-ann")
    _machine("m-look", "OtoDock platform", owner="u-ann")
    online.add("m-look")
    _d, targets = chat_targets.offered(_agent(), chat_targets.RunPerson("u-ann", "manager"),
                                       engine_path="claude-code-cli", admin_asker=False)
    for name in ("OtoDock platform", "otodock server", "local"):
        assert chat_targets.find(targets, name).target == placement.LOCAL
    look = chat_targets.find(targets, "m-look")
    assert look.label == "OtoDock platform (computer)"
    assert chat_targets.find(targets, "OtoDock platform (computer)").target == "m-look"


def test_the_summary_card_opens_its_sentence_plainly():
    assert chat_move.summary_notice(
        "OtoDock platform", "drill-sat", move_schema.WHY_OFFLINE, source="local",
        target="m-1") == ("This chat moved from the OtoDock platform to drill-sat. The OtoDock "
                          "platform was offline, so it continues from a summary of the "
                          "conversation.")
    assert chat_move.summary_notice(
        "drill-sat", "OtoDock platform", move_schema.WHY_OFFLINE, source="m-1",
        target="local") == ("This chat moved from drill-sat to the OtoDock platform. drill-sat "
                            "was offline, so it continues from a summary of the conversation.")
    assert chat_move.summary_notice(
        "drill-sat", "Ann laptop", move_schema.WHY_NO_FILES, source="m-1", target="m-2") == (
        "This chat moved from drill-sat to Ann laptop. The previous session left no file to "
        "take, so it continues from a summary of the conversation.")
    assert chat_move.summary_notice(
        "drill-sat", "OtoDock platform", move_schema.WHY_OLD_SOURCE, source="m-1",
        target="local") == ("This chat moved from drill-sat to the OtoDock platform. drill-sat "
                            "runs an older satellite, so it continues from a summary of the "
                            "conversation.")
    assert chat_move.summary_notice(
        "OtoDock platform", "drill-sat", move_schema.WHY_OLD_TARGET, source="local",
        target="m-1") == ("This chat moved from the OtoDock platform to drill-sat. drill-sat "
                          "runs an older satellite, so it continues from a summary of the "
                          "conversation.")
    assert chat_move.summary_notice(
        "drill-sat", "Ann laptop", move_schema.WHY_OLD_SATELLITE, source="m-1",
        target="m-2") == ("This chat moved from drill-sat to Ann laptop. drill-sat and Ann "
                          "laptop run an older satellite, so it continues from a summary of "
                          "the conversation.")
    assert chat_move.DIRECT_LOCAL == "Direct LLM chats run on the OtoDock platform."


def test_the_builder_names_where_a_refused_chat_is(temp_db):
    from core.config import config_builder as cb
    _user("u-ann", name="Ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    assert cb._placement_refusal("m-ann", "u-ann", "editor") == ""
    assert cb._placement_refusal("m-ann", "u-bob", "admin") == (
        "This chat is on Ann's computer (Ann laptop). Move it to one of your places from "
        "the Runs on menu.")
    assert cb._placement_refusal("m-srv", "u-bob", "editor").startswith("This chat is on Server")
    assert cb._placement_refusal("m-srv", "u-bob", "manager") == ""


# -- the routes ----------------------------------------------------------------------

def _user_ctx(sub, role="member", agents=(), **kw):
    from auth.providers import UserContext
    return UserContext(sub=sub, email=f"{sub}@t.io", name=sub, role=role, agents=list(agents), **kw)


def test_the_targets_route_answers_a_reader_and_hides_the_chat_from_others(temp_db, online):
    from fastapi import HTTPException

    from api.remote import chat_placement as routes
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann")
    view = asyncio.run(routes.chat_targets_route(cid, user=_user_ctx("u-ann", agents=[slug])))
    assert {t["target"] for t in view["targets"]} == {"local", "m-ann"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes.chat_targets_route(cid, user=_user_ctx("u-bob", agents=[slug])))
    assert e.value.status_code == 404


def test_a_new_chats_places_follow_the_engine_the_page_picked(temp_db, online, monkeypatch):
    """The new chat's menu asks for the engine the page shows: a machine
    that does not run it is listed but not pickable."""
    from api.remote import chat_placement as routes
    from core.remote import satellite_connection as sc
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    monkeypatch.setattr(sc.get_connection_manager(), "satellite_engines",
                        lambda mid: frozenset({"claude-code-cli"}))
    slug = _agent()
    me = _user_ctx("u-ann", agents=[slug])

    def ann(view):
        return next(t for t in view["targets"] if t["target"] == "m-ann")
    assert ann(asyncio.run(routes.agent_run_targets(slug, engine="claude-code-cli", user=me)))[
        "pickable"]
    codex = ann(asyncio.run(routes.agent_run_targets(slug, engine="codex-cli", user=me)))
    assert not codex["pickable"] and codex["why_not"] == "Ann laptop can't run this chat's engine."


def test_the_move_route_refuses_with_the_sentence_and_an_agent_token(temp_db, online):
    from fastapi import HTTPException

    from api.remote import chat_placement as routes
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann")
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes.move_chat_route(cid, {"target": "local"},
                                           user=_user_ctx("u-ann", agents=[slug])))
    assert e.value.status_code == 409 and e.value.detail == "This chat already runs there."
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes.move_chat_route(cid, {"target": "local"},
                                           user=_user_ctx("u-ann", agents=[slug], is_api_key=True,
                                                          session_id="s-1", agent=slug)))
    assert e.value.status_code == 403


def test_the_agent_tool_lists_and_schedules_for_its_own_chat(temp_db, online, monkeypatch):
    from api.remote import chat_placement as routes
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    monkeypatch.setattr(db, "get_chat_by_session", lambda s: db.get_chat(cid) if s == sid else None)
    started = []

    async def _wait(chat_id, target, asker, blocked=None, *, resume=False):
        started.append((chat_id, target, resume))
    monkeypatch.setattr(chat_move, "_wait_and_move", _wait)
    me = _user_ctx("u-ann", agents=[slug], session_id=sid, agent=slug, is_api_key=True)
    listed = asyncio.run(routes.agent_move(routes.AgentMoveRequest(), user=me))
    assert listed["current"]["target"] == "local"

    async def go():
        resp = await routes.agent_move(routes.AgentMoveRequest(machine="Ann laptop"), user=me)
        await asyncio.sleep(0.05)
        return resp
    resp = asyncio.run(go())
    assert resp.status_code == 202 and started == [(cid, "m-ann", True)]


def test_an_agent_scope_tasks_chat_moves_itself_as_the_task(temp_db, online, monkeypatch):
    """No person stands behind the run, so its session token reads as a
    viewer: the move of its own chat asks as the task's identity."""
    from api.remote import chat_placement as routes
    from core.session import session_kind
    from storage import database as db
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    online.add("m-srv")
    slug = _agent()
    run_id = str(uuid.uuid4())
    db.create_run(run_id, _task_row(slug, scope="agent", owner=slug), slug, "manual", None, "go",
                  scope="agent", created_by=slug)
    cid = session_kind.task_chat_id(run_id)
    sid = str(uuid.uuid4())
    db.create_chat(cid, f"task::{slug}", slug, "auto", execution_path="claude-code-cli")
    db.update_chat(cid, execution_target="m-srv", session_id=sid)
    db.update_run(run_id, status="completed")
    monkeypatch.setattr(db, "get_chat_by_session", lambda s: db.get_chat(cid) if s == sid else None)
    started = []

    async def _wait(chat_id, target, asker, blocked=None, *, resume=False):
        started.append((chat_id, target, asker.sub, resume))
    monkeypatch.setattr(chat_move, "_wait_and_move", _wait)
    me = _user_ctx(f"session:{sid}", agents=[slug], session_id=sid, agent=slug, is_api_key=True)
    listed = asyncio.run(routes.agent_move(routes.AgentMoveRequest(), user=me))
    assert listed["can_move"] and listed["refusal"] == ""

    async def go():
        resp = await routes.agent_move(routes.AgentMoveRequest(machine="local"), user=me)
        await asyncio.sleep(0.05)
        return resp
    assert asyncio.run(go()).status_code == 202 and started == [(cid, "local", "", True)]


@pytest.mark.parametrize("token_names_the_creator", [False, True])
def test_a_user_scope_tasks_chat_moves_itself_as_its_creator(temp_db, online, monkeypatch,
                                                              token_names_the_creator):
    """A user-scope run moves its own chat as its creator, at the creator's
    role on the agent, whatever the session's token resolves to: a Codex
    task's MCPs carry a token that names no person (the walk on T1, where an
    admin's task was refused "Only someone who can continue this chat can
    move it."), a Claude task's names the creator."""
    from api.remote import chat_placement as routes
    from core.session import session_kind
    from storage import database as db
    _user("u-adm", "admin")
    _machine("m-srv", "Server", scope="admin", owner="u-adm")
    online.add("m-srv")
    slug = _agent()
    run_id = str(uuid.uuid4())
    db.create_run(run_id, _task_row(slug, owner="u-adm"), slug, "manual", None, "go",
                  scope="user", created_by="u-adm")
    cid = session_kind.task_chat_id(run_id)
    sid = str(uuid.uuid4())
    db.create_chat(cid, "u-adm", slug, "auto", execution_path="codex-cli")
    db.update_chat(cid, execution_target="local", session_id=sid)
    db.update_run(run_id, status="completed")
    monkeypatch.setattr(db, "get_chat_by_session", lambda s: db.get_chat(cid) if s == sid else None)
    started = []

    async def _wait(chat_id, target, asker, blocked=None, *, resume=False):
        started.append((chat_id, target, asker.sub, asker.platform_admin, resume))
    monkeypatch.setattr(chat_move, "_wait_and_move", _wait)
    sub = "u-adm" if token_names_the_creator else f"session:{sid}"
    me = _user_ctx(sub, "admin" if token_names_the_creator else "service", agents=[slug],
                   session_id=sid, agent=slug, is_api_key=True)
    listed = asyncio.run(routes.agent_move(routes.AgentMoveRequest(), user=me))
    assert listed["can_move"] and listed["refusal"] == ""
    assert "m-srv" in {t["target"] for t in listed["targets"] if t["pickable"]}

    async def go():
        resp = await routes.agent_move(routes.AgentMoveRequest(machine="Server"), user=me)
        await asyncio.sleep(0.05)
        return resp
    assert asyncio.run(go()).status_code == 202
    assert started == [(cid, "m-srv", "u-adm", True, True)]


def test_an_agent_scope_tasks_own_move_is_offered_only_the_agents_places(temp_db, online,
                                                                         monkeypatch):
    """A run with no person moves as the agent: the platform and the agent's
    default, never another admin machine (moving between admin machines is a
    platform admin's pick from the menu)."""
    from api.remote import chat_placement as routes
    from core.session import session_kind
    from storage import database as db
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    online.add("m-srv")
    slug = _agent()
    run_id = str(uuid.uuid4())
    db.create_run(run_id, _task_row(slug, scope="agent", owner=slug), slug, "manual", None, "go",
                  scope="agent", created_by="u-admin")
    cid = session_kind.task_chat_id(run_id)
    sid = str(uuid.uuid4())
    db.create_chat(cid, f"task::{slug}", slug, "auto", execution_path="claude-code-cli")
    db.update_chat(cid, execution_target="local", session_id=sid)
    db.update_run(run_id, status="completed")
    monkeypatch.setattr(db, "get_chat_by_session", lambda s: db.get_chat(cid) if s == sid else None)
    me = _user_ctx(f"session:{sid}", agents=[slug], session_id=sid, agent=slug, is_api_key=True)
    listed = asyncio.run(routes.agent_move(routes.AgentMoveRequest(), user=me))
    assert "m-srv" not in {t["target"] for t in listed["targets"]}
    with pytest.raises(Exception) as e:
        asyncio.run(routes.agent_move(routes.AgentMoveRequest(machine="Server"), user=me))
    assert getattr(e.value, "status_code", None) == 409
    assert getattr(e.value, "detail", "") == chat_move.CANT_THERE


# -- the resume after the agent's own move -----------------------------------------

def _resumes(monkeypatch):
    """The delivery the resume asks for, recorded (nothing spawns)."""
    from core.session import session_delivery
    asked = []

    async def deliver_prompt(chat_id, text, **kw):
        asked.append({"chat_id": chat_id, "text": text, **kw})
        return session_delivery.DeliveryOutcome("oneshot", chat_id=chat_id)
    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)
    chat_move._recent_resumes.clear()
    return asked


def _tool_move(cid, target, asker, *, resume=True):
    async def go():
        await chat_move._wait_and_move(cid, target, asker, resume=resume)
        await _settled(cid)
        for _ in range(100):           # the resume runs as its own job
            await asyncio.sleep(0.01)
    asyncio.run(go())


def test_the_agents_own_move_continues_on_the_new_place_by_itself(temp_db, online,
                                                                  monkeypatch):
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    db.update_chat(cid, permission_mode="autoReview")
    _FakeFiles(monkeypatch)
    _frames(monkeypatch)
    asked = _resumes(monkeypatch)
    _tool_move(cid, "m-ann", _asker("u-ann", "manager"))
    [call] = asked
    assert call["text"] == "You now run on Ann laptop. Continue the work."
    assert call["user_sub"] == "u-ann" and call["allow_ws"] is False and call["allow_pty"] is False
    one = call["oneshot_fn"].keywords
    assert one == {"permission_mode": "autoReview", "client_type": "dashboard",
                   "fresh_without_session": True, "note_prompt": True}
    # The wake row the transcript shows, at the head of the resumed turn (the
    # ladder writes it on the chat's lane, on the loop).
    async def persist():
        call["persist_event"](cid)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rows = [m for m in db.get_chat_messages(cid) if m.get("event_type") == "schedule_wake"]
            if rows:
                return rows
        return []
    rows = asyncio.run(persist())
    assert rows and '"source": "move"' in rows[0]["event_data"]


def test_a_move_from_the_menu_or_of_another_chat_never_resumes(temp_db, online, monkeypatch):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _FakeFiles(monkeypatch)
    _frames(monkeypatch)
    asked = _resumes(monkeypatch)
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))

    async def chip():
        await chat_move.request_move(cid, "m-ann", asker=_asker("u-ann", "manager"),
                                     expect_from="local")
        await _settled(cid)
        await asyncio.sleep(0.2)
    asyncio.run(chip())
    other = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    _tool_move(other, "m-ann", _asker("u-ann", "manager"), resume=False)
    assert asked == []


def test_a_waiting_message_goes_first_and_the_lock_holds(temp_db, online, monkeypatch):
    from types import SimpleNamespace

    from core.events import input_queue
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _FakeFiles(monkeypatch)
    _frames(monkeypatch)
    asked = _resumes(monkeypatch)
    ann = _asker("u-ann", "manager")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    monkeypatch.setattr(input_queue, "peek", lambda chat_id: (
        [SimpleNamespace(author_sub="u-ann")] if chat_id == cid else []))
    _tool_move(cid, "m-ann", ann)
    assert asked == []
    # A message the lock would send back to its author holds nothing.
    other = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    monkeypatch.setattr(input_queue, "peek", lambda chat_id: (
        [SimpleNamespace(author_sub="u-bob")] if chat_id == other else []))
    monkeypatch.setattr(chat_move, "lock_sentence",
                        lambda chat, sub: "This chat is running on x." if sub == "u-bob" else "")
    _tool_move(other, "m-ann", ann)
    assert [a["chat_id"] for a in asked] == [other]
    # A chat read-only for its person there does not continue, and says so.
    asked.clear()
    monkeypatch.setattr(input_queue, "peek", lambda chat_id: [])
    locked = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    monkeypatch.setattr(chat_move, "lock_sentence", lambda chat, sub: "This chat is running on x.")
    job = chat_move._Job("mv-x", locked, "Ann laptop", "m-ann")
    db.update_chat(locked, execution_target="m-ann")
    monkeypatch.setattr(remote_store, "latest_chat_move", lambda c: {"id": "mv-x"})
    asyncio.run(chat_move._resume_after_tool_move(job, ann))
    assert asked == []
    [card] = [m["event_data"] for m in db.get_chat_messages(locked) if m.get("event_type") == "system"]
    assert "did not continue on Ann laptop on its own" in card


def test_the_agents_later_move_replaces_its_earlier_one(temp_db, online, monkeypatch):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-box", "Box", owner="u-ann")
    online.update({"m-ann", "m-box"})
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    waiting = []

    async def _wait(chat_id, target, asker, blocked=None, *, resume=False):
        waiting.append(target)
        await asyncio.Event().wait()
    monkeypatch.setattr(chat_move, "_wait_and_move", _wait)

    async def go():
        await chat_move.schedule_after_turn(cid, "m-ann", asker=_asker("u-ann", "manager"))
        await asyncio.sleep(0.01)
        first = chat_move._waiters[cid]
        await chat_move.schedule_after_turn(cid, "m-box", asker=_asker("u-ann", "manager"))
        await asyncio.sleep(0.01)
        second = chat_move._waiters[cid]
        assert first.cancelled() and not second.done() and first is not second
        second.cancel()
        await asyncio.sleep(0.01)
        assert cid not in chat_move._waiters
    asyncio.run(go())
    assert waiting == ["m-ann", "m-box"]


def test_the_chain_stops_after_ten_moves_with_no_message_from_a_person(temp_db, online,
                                                                       monkeypatch):
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _FakeFiles(monkeypatch)
    _frames(monkeypatch)
    asked = _resumes(monkeypatch)
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    db.add_chat_message(cid, "user", "move around", author_sub="u-ann")
    for _ in range(9):
        remote_store.create_chat_move({
            "id": str(uuid.uuid4()), "chat_id": cid, "state": move_schema.SETTLED,
            "origin": move_schema.ORIGIN_TOOL, "created_at": chat_move._now()})
    # A prompt the platform wrote (no author) is no person's message.
    db.add_chat_message(cid, "user", "You now run on x. Continue the work.")
    _tool_move(cid, "m-ann", _asker("u-ann", "manager"))       # the tenth
    assert asked == []
    cards = [m["event_data"] for m in db.get_chat_messages(cid) if m.get("event_type") == "system"]
    assert any("10 times in a row" in c and "resume_stopped" in c for c in cards)
    # A person's message starts a new chain.
    db.add_chat_message(cid, "user", "go on", author_sub="u-ann")
    _tool_move(cid, "local", _asker("u-ann", "manager"))
    assert len(asked) == 1


def test_the_hourly_cap_holds_whatever_the_rows_say(temp_db, online, monkeypatch):
    import time as _t
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    _FakeFiles(monkeypatch)
    _frames(monkeypatch)
    asked = _resumes(monkeypatch)
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    chat_move._recent_resumes[cid] = [_t.monotonic()] * chat_move.RESUME_CHAIN_MAX
    _tool_move(cid, "m-ann", _asker("u-ann", "manager"))
    assert asked == []
    # The card names the hour, not a chain the person broke with a message.
    from storage import database as db
    [card] = [m["event_data"] for m in db.get_chat_messages(cid)
              if m.get("event_type") == "system" and "resume_stopped" in m["event_data"]]
    assert "10 times in the last hour" in card and "in a row" not in card


def test_an_undelivered_resume_says_so_unless_a_person_took_the_chat(temp_db, online,
                                                                     monkeypatch):
    from core.session import session_delivery
    from storage import database as db
    _user("u-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()), pin="m-ann")
    job = chat_move._Job("mv-1", cid, "Ann laptop", "m-ann")
    calls = []

    async def deliver_prompt(chat_id, text, **kw):
        # The ladder writes the row before its rungs, then none delivers.
        kw["persist_event"](chat_id)
        outcome = session_delivery.DeliveryOutcome("none", chat_id=chat_id)
        calls.append(kw)
        await kw["on_outcome"](outcome)
        return outcome
    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)
    # The chat is free when the nudge starts; by its outcome a person's turn
    # (or message) may have taken it.
    took = {"now": True}
    monkeypatch.setattr(chat_move, "_person_took_chat", lambda c: took["now"])

    def kinds():
        return [m.get("event_type") for m in db.get_chat_messages(cid)]
    asyncio.run(chat_move._deliver_resume(db.get_chat(cid), job, _asker("u-ann", "manager")))
    assert kinds() == []                    # the row taken back, a person's turn has the chat
    took["now"] = False
    asyncio.run(chat_move._deliver_resume(db.get_chat(cid), job, _asker("u-ann", "manager")))
    assert kinds() == ["system"]            # no "Continued after the move" row, the card
    [card] = [m["event_data"] for m in db.get_chat_messages(cid) if m.get("event_type") == "system"]
    assert "did not continue on Ann laptop on its own" in card and "resume_skipped" in card


def test_a_resume_whose_spawn_is_refused_is_undone_with_its_card(temp_db, online,
                                                                 monkeypatch):
    """The spawn's placement guard (or any start that breaks) raises out of
    the ladder: the "Continued after the move" row is taken back and the
    card says the agent did not continue."""
    from core import placement
    from core.session import session_delivery
    from storage import database as db
    _user("u-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()), pin="m-ann")
    job = chat_move._Job("mv-1", cid, "Ann laptop", "m-ann")

    async def deliver_prompt(chat_id, text, **kw):
        kw["persist_event"](chat_id)
        await asyncio.sleep(0)
        raise placement.PlacementRefused(placement.REFUSED_SENTENCE)
    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)
    asyncio.run(chat_move._deliver_resume(db.get_chat(cid), job, _asker("u-ann", "manager")))
    rows = db.get_chat_messages(cid)
    assert [m.get("event_type") for m in rows] == ["system"]
    assert "did not continue on Ann laptop on its own" in rows[0]["event_data"]


def test_the_resume_stands_down_when_a_persons_turn_took_the_chat_first(temp_db, online,
                                                                         monkeypatch):
    """The move's release re-armed the queue: its drain sent the person's
    queued message and holds the chat for that turn before the nudge runs.
    The queue reads empty, the nudge neither steers into the turn nor runs
    after it, and no card is written (the message went first)."""
    from core.events import input_queue
    from core.session import session_delivery
    from storage import database as db
    _user("u-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()), pin="m-ann")
    job = chat_move._Job("mv-1", cid, "Ann laptop", "m-ann")
    asked = []

    async def deliver_prompt(chat_id, text, **kw):
        asked.append(text)
        return session_delivery.DeliveryOutcome("steer", chat_id=chat_id)
    monkeypatch.setattr(session_delivery, "deliver_prompt", deliver_prompt)

    async def scenario():
        q = await input_queue.loaded(cid)
        claim = q.try_claim()               # the drain's claim for the person's turn
        assert claim is not None and not input_queue.peek(cid)
        try:
            await chat_move._deliver_resume(db.get_chat(cid), job, _asker("u-ann", "manager"))
        finally:
            q.release(claim)
    asyncio.run(scenario())
    assert asked == []
    assert db.get_chat_messages(cid) == []


def _waiting_move(monkeypatch, online):
    started = []

    async def _start(chat, hit, person, **kw):
        started.append(kw.get("resume"))
    monkeypatch.setattr(chat_move, "_start", _start)
    monkeypatch.setattr(chat_move, "TURN_POLL_S", 0.01)
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    return started, _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))


def test_the_agents_move_waits_for_a_turn_its_claim_does_not_show(temp_db, online, monkeypatch):
    """A task run, a terminal turn and a person's warmup hold no claim: the
    waiter gives the claim back and asks again until the turn is over."""
    started, cid = _waiting_move(monkeypatch, online)
    open_turns = {"left": 3}

    def _open(chat):
        open_turns["left"] -= 1
        return open_turns["left"] > 0
    monkeypatch.setattr(chat_move, "open_turn", _open)
    asyncio.run(chat_move._wait_and_move(cid, "m-ann", _asker("u-ann", "manager"), resume=True))
    assert started == [True] and open_turns["left"] == 0


def test_the_agents_move_of_a_terminal_chat_waits_for_its_turn_to_end(temp_db, online,
                                                                      monkeypatch):
    """The terminal's turn is open right after the tool call: the move waits
    for it instead of failing with "Finish or stop the current turn"."""
    from types import SimpleNamespace

    from core.session import interactive_session
    from storage import database as db
    started, cid = _waiting_move(monkeypatch, online)
    term = SimpleNamespace(turn_open=True)
    polls = {"n": 0}

    def _find(chat_id):
        polls["n"] += 1
        if polls["n"] > 2:
            term.turn_open = False
        return term
    monkeypatch.setattr(interactive_session, "find_live_for_chat", _find)
    asyncio.run(chat_move._wait_and_move(cid, "m-ann", _asker("u-ann", "manager"), resume=True))
    assert started == [True] and polls["n"] >= 3
    assert not [m for m in db.get_chat_messages(cid) if m.get("event_type") == "system"]


def test_a_move_never_borrows_a_persons_warmup(temp_db, online):
    """A warmup holds no claim: a move that found one in flight refuses and
    leaves the person's registration in place (the tool's waiter asks again)."""
    from core.events import input_queue
    from core.session import warmup_registry
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    cid = _chat(_agent(), "u-ann", sid=str(uuid.uuid4()))
    chat = db.get_chat(cid)
    asker = _asker("u-ann", "manager")
    hit, person = chat_move._validate(chat, "m-ann", asker, expect_from=None, check_turn=False)

    async def scenario():
        await warmup_registry.register(cid, "u-ann", chat["agent"])
        q = await input_queue.loaded(cid)
        claim = q.try_claim()
        with pytest.raises(chat_move.MoveRefused) as e:
            await chat_move._start(chat, hit, person, asker=asker,
                                   origin=move_schema.ORIGIN_TOOL, claim=claim)
        assert str(e.value) == chat_move.FINISH_TURN
        assert warmup_registry.get(cid) is not None and cid not in chat_move._running
        await warmup_registry.unregister(cid)
    asyncio.run(scenario())


# -- delegated work stays where it runs ---------------------------------------------

def _task_row(agent, *, continue_session=None, owner="u-ann", scope="user"):
    from storage import database as db
    tid = str(uuid.uuid4())
    db.create_dynamic_task(tid, agent, "lane", "go", "default", "one-time", None, None, None,
                           600, owner, continue_session=continue_session, scope=scope)
    return tid


def test_a_chat_delegated_work_continues_is_refused(temp_db, online):
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    worker = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    db.update_chat(worker, parent_chat_id=str(uuid.uuid4()))
    sid = str(uuid.uuid4())
    continued = _chat(slug, "u-ann", sid=sid)
    _task_row(slug, continue_session=sid)
    me = _asker("u-ann", "manager")
    for cid in (worker, continued):
        with pytest.raises(chat_move.MoveRefused, match=chat_move.CONTINUES_WORK):
            chat_move._validate(db.get_chat(cid), "m-ann", me, expect_from=None,
                                check_turn=False)
        view = chat_move.targets_view(db.get_chat(cid), me)
        assert view["refusal"] == chat_move.CONTINUES_WORK and not view["can_move"]


def _proved(monkeypatch, size=99):
    from core.remote import session_files as sf
    deleted = []

    async def main_size(place, scope, rel):
        return size

    async def delete_files(place, scope):
        deleted.append(place)
        return 1
    monkeypatch.setattr(sf, "main_size", main_size)
    monkeypatch.setattr(sf, "delete_files", delete_files)
    return deleted


def test_the_sweep_keeps_a_session_another_chat_still_names(temp_db, online, monkeypatch):
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", pin="m-ann", sid=sid)
    _chat(slug, "u-ann", sid=sid)                       # a second chat on the same session
    mid = _awaiting(cid, sid=sid)
    deleted = _proved(monkeypatch)
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(mid)["state"] == move_schema.SETTLED and deleted == []


def test_the_sweep_keeps_a_session_a_task_continues(temp_db, online, monkeypatch):
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", pin="m-ann", sid=sid)
    _task_row(slug, continue_session=sid)
    mid = _awaiting(cid, sid=sid)
    deleted = _proved(monkeypatch)
    asyncio.run(chat_move.sweep())
    assert remote_store.get_chat_move(mid)["state"] == move_schema.SETTLED and deleted == []
    assert remote_store.session_refs_shared(cid, [sid])
    assert not remote_store.session_refs_shared(cid, [str(uuid.uuid4())])


def test_a_busy_target_is_not_proved_yet_and_is_asked_later(temp_db, online, monkeypatch, caplog):
    import logging

    from core.remote import session_files as sf
    from storage import remote_store
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    sid = str(uuid.uuid4())
    cid = _chat(_agent(), "u-ann", pin="m-ann", sid=sid)
    mid = _awaiting(cid, sid=sid)
    asked = []

    async def main_size(place, scope, rel):
        asked.append(place)
        raise sf.TransferError(sf.WHY_BUSY, "busy: a live session holds this session",
                               place=place)
    monkeypatch.setattr(sf, "main_size", main_size)
    with caplog.at_level(logging.INFO, logger="otodock.remote"):
        asyncio.run(chat_move.sweep())
        asyncio.run(chat_move.sweep())
    assert asked == ["m-ann"]                         # the second pass waits its back-off
    assert remote_store.get_chat_move(mid)["state"] == move_schema.AWAITING_RESUME
    assert not any(r.exc_info for r in caplog.records if "not proved yet" in r.getMessage())
    assert chat_move._proof_next[mid][1] == chat_move.PROOF_BACKOFF_S[0]
    chat_move._not_yet(mid)
    assert chat_move._proof_next[mid][1] == 2 * chat_move.PROOF_BACKOFF_S[0]
    for _ in range(10):
        chat_move._not_yet(mid)
    assert chat_move._proof_next[mid][1] == chat_move.PROOF_BACKOFF_S[1]


# -- the job's failure paths ---------------------------------------------------------

def _staging_spy(monkeypatch):
    from core.remote import session_files as sf
    seen = {"local": 0, "machines": []}

    def local_discard(scope, move_id):
        seen["local"] += 1

    async def discard(place, scope, move_id):
        seen["machines"].append(place)
    monkeypatch.setattr(sf, "local_discard", local_discard)
    monkeypatch.setattr(sf, "discard", discard)
    return seen


def test_an_unexpected_failure_cleans_every_staging_and_falls_back(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-two", "Ann desktop", owner="u-ann")
    online.update({"m-ann", "m-two"})
    slug = _agent()
    cid = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))

    async def copy_session(scope, *, source, target, move_id, progress=None):
        raise ValueError("a malformed size")
    monkeypatch.setattr(sf, "copy_session", copy_session)
    seen = _staging_spy(monkeypatch)
    deleted = []

    async def delete_files(place, scope):
        deleted.append(place)
        return 1
    monkeypatch.setattr(sf, "delete_files", delete_files)
    _frames(monkeypatch)

    async def go():
        await chat_move.request_move(cid, "m-two", asker=_asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())
    assert seen["local"] == 1 and sorted(seen["machines"]) == ["m-ann", "m-two"]
    assert deleted == []                              # nothing was committed on the target
    move = remote_store.latest_chat_move(cid)
    assert move["memory"] == move_schema.MEMORY_SUMMARY
    assert move["why"] == move_schema.WHY_TRANSFER_FAILED
    assert db.get_chat(cid)["execution_target"] == "m-two"


def test_a_busy_source_keeps_the_chat_and_its_memory(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    from storage import database as db, remote_store
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", pin="m-ann", sid=sid)

    async def copy_session(scope, *, source, target, move_id, progress=None):
        raise sf.TransferError(sf.WHY_BUSY, "busy: a live session holds this session",
                               place=source)
    monkeypatch.setattr(sf, "copy_session", copy_session)
    _staging_spy(monkeypatch)
    frames = _frames(monkeypatch)

    async def go():
        await chat_move.request_move(cid, "local", asker=_asker("u-ann", "manager"))
        await _settled(cid)
    asyncio.run(go())
    row = db.get_chat(cid)
    assert row["execution_target"] == "m-ann" and row["session_id"] == sid
    assert not row["pending_history_seed"]
    move = remote_store.latest_chat_move(cid)
    assert move["state"] == move_schema.FAILED and move["why"] == move_schema.WHY_BUSY
    assert not frames[-1]["ok"]
    assert frames[-1]["message"] == ("This chat is busy on Ann laptop, so it stays there. "
                                     "Finish or stop its turn there, then move it again.")


def test_the_loser_of_two_picks_hears_where_the_chat_is_moving(temp_db, online, monkeypatch):
    from core.events import input_queue
    from storage import database as db
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    online.add("m-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))

    class _Held:
        def try_claim(self):
            return None

    async def loaded(chat_id):
        return _Held()
    monkeypatch.setattr(input_queue, "loaded", loaded)
    hit = chat_targets.Target("m-ann", "Ann laptop", placement.PAIRING_USER, "online", True,
                              False, True, "")
    chat_move._running[cid] = chat_move._Job("m-1", cid, "Ann laptop", "m-ann")
    try:
        with pytest.raises(chat_move.MoveRefused, match="This chat is moving to Ann laptop."):
            asyncio.run(chat_move._start(db.get_chat(cid), hit,
                                         chat_targets.RunPerson("u-ann", "manager"),
                                         asker=_asker("u-ann", "manager"), origin="chip"))
    finally:
        chat_move._running.pop(cid, None)
    with pytest.raises(chat_move.MoveRefused, match=chat_move.FINISH_TURN):
        asyncio.run(chat_move._start(db.get_chat(cid), hit,
                                     chat_targets.RunPerson("u-ann", "manager"),
                                     asker=_asker("u-ann", "manager"), origin="chip"))


# -- the lock after a summarised move ------------------------------------------------

def test_a_summarised_move_locks_until_its_first_turn_window_ends(temp_db, online, monkeypatch):
    from core.session.visibility import shared_chat_owner
    from storage import database as db, remote_store
    _user("u-ann", name="Ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent(default_scope="agent", collaborative=False)
    cid = _chat(slug, shared_chat_owner(slug), pin="m-ann")
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    remote_store.create_chat_move({"id": str(uuid.uuid4()), "chat_id": cid, "target": "m-ann",
                                   "actor": "u-ann", "state": move_schema.SETTLED,
                                   "memory": move_schema.MEMORY_SUMMARY,
                                   "created_at": chat_move._now()})
    assert chat_move.lock_sentence(db.get_chat(cid), "u-bob").startswith("This chat is running on Ann")
    monkeypatch.setattr(chat_move, "_idle_timeout_s", lambda: 0.0)
    assert chat_move.lock_sentence(db.get_chat(cid), "u-bob") == ""


# -- a voice conversation -------------------------------------------------------------

def test_a_voice_conversation_keeps_the_chat_and_says_so(temp_db, online, monkeypatch):
    from storage import database as db
    from ws import dashboard_chat_move, duplex
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann")
    monkeypatch.setattr(duplex, "live_for_chat", lambda chat_id: object())
    assert dashboard_chat_move.voice_attached(cid) == chat_move.VOICE_IN_USE
    view = chat_move.targets_view(db.get_chat(cid), _asker("u-ann", "manager"),
                                  blocked=dashboard_chat_move.voice_attached(cid))
    assert view["refusal"] == chat_move.VOICE_IN_USE and not view["can_move"]
    assert chat_move.VOICE_IN_USE == ("A voice conversation is using this chat. End it before "
                                      "moving the chat.")


# -- the hand-over follows the session's identity ------------------------------------

def test_a_shared_chat_hands_over_to_the_senders_default(temp_db, online, monkeypatch):
    from core.session.visibility import shared_chat_owner
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent(default_scope="agent", collaborative=False)
    cid = _chat(slug, shared_chat_owner(slug), pin="m-ann")
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)
    asked = []

    async def request_move(chat_id, wanted, *, asker, origin, **kw):
        asked.append((chat_id, wanted, origin))
        return {"move_id": "m", "to_label": "OtoDock platform", "memory": "files"}
    monkeypatch.setattr(chat_move, "request_move", request_move)
    out = asyncio.run(chat_move.hand_over(cid, asker=_asker("u-bob", "editor")))
    assert out["to_label"] == "OtoDock platform" and asked == [(cid, "local", "handover")]
    assert chat_move._handover_refusal(db.get_chat(cid), _asker("u-bob", "editor")) == ""


def test_an_admin_never_hands_a_colleagues_chat_to_their_own_default(temp_db, online, monkeypatch):
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-admin", "admin")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", pin="m-ann")
    monkeypatch.setattr(chat_move, "_session_live", lambda chat: False)

    async def request_move(*a, **kw):
        raise AssertionError("a colleague's chat never moves on an admin's send")
    monkeypatch.setattr(chat_move, "request_move", request_move)
    with pytest.raises(chat_move.MoveRefused) as e:
        asyncio.run(chat_move.hand_over(cid, asker=_asker("u-admin", "admin", admin=True)))
    assert str(e.value) == ("This chat runs on Ann's computer, so only they can continue it "
                            "there. Move it from the Runs on menu.")
    assert db.get_chat(cid)["execution_target"] == "m-ann"


def test_an_editors_own_chat_leaves_an_admin_machine(temp_db, online, monkeypatch):
    from storage import database as db
    _user("u-ed")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    slug = _agent()
    cid = _chat(slug, "u-ed", pin="m-srv")
    chat = db.get_chat(cid)
    assert chat_move.placement_blocked(chat, "u-ed", "editor")
    assert chat_move._handover_refusal(chat, _asker("u-ed", "editor")) == ""


def test_a_task_chat_is_judged_by_the_identity_it_builds_with(temp_db, online, monkeypatch):
    from core.config import task_config_builder as tcb
    from core.session import session_kind
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-admin", "admin")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    cid = session_kind.task_chat_id(str(uuid.uuid4()))
    db.create_chat(cid, "u-ann", slug, "default", execution_path="claude-code-cli")
    db.update_chat(cid, execution_target="m-ann")
    ident = type("Ident", (), {"creds_user_sub": "u-ann", "role": "manager", "username": "u-ann"})
    monkeypatch.setattr(tcb, "rewarm_task_identity", lambda chat_id, agent: ident)
    # The admin continuing Ann's run builds as Ann, who may use her laptop.
    assert not chat_move.placement_blocked(db.get_chat(cid), "u-admin", "admin")
    monkeypatch.setattr(tcb, "rewarm_task_identity", lambda chat_id, agent: None)
    assert chat_move.placement_blocked(db.get_chat(cid), "u-admin", "admin")
    assert "Ann's computer" in chat_move._handover_refusal(
        db.get_chat(cid), _asker("u-admin", "admin", admin=True))


def test_the_refusal_names_a_lookalike_machine_as_a_computer(temp_db, online, monkeypatch):
    from core.config import task_config_builder as tcb
    from core.session import session_kind
    from storage import database as db
    _user("u-ed")
    _machine("m-srv", "OtoDock platform", scope="admin", owner="u-admin")
    slug = _agent()
    cid = session_kind.task_chat_id(str(uuid.uuid4()))
    db.create_chat(cid, "u-ed", slug, "default", execution_path="claude-code-cli")
    db.update_chat(cid, execution_target="m-srv")
    ident = type("Ident", (), {"creds_user_sub": "u-ed", "role": "editor", "username": "u-ed"})
    monkeypatch.setattr(tcb, "rewarm_task_identity", lambda chat_id, agent: ident)
    assert chat_move._handover_refusal(db.get_chat(cid), _asker("u-ed", "editor")) == (
        "You can't run this chat on OtoDock platform (computer). Move it from the Runs on menu.")


def test_a_chat_with_no_session_expects_a_summary():
    """An engine switch or an earlier summary left no session to carry."""
    assert chat_move._expected_memory({"execution_target": "local"}) == move_schema.MEMORY_SUMMARY
    assert chat_move._expected_memory(
        {"execution_target": "local", "session_id": "s"}) == move_schema.MEMORY_FILES


# -- who sees which places, role by role ---------------------------------------------

def _set_default(slug, mid):
    from storage.agents import agent_store
    agent_store.update_agent(slug, execution_target=mid)


def _role_case(case, online):
    """``(chat row, asker)`` for one row of the roles table."""
    from core.session import session_kind
    from core.session.visibility import shared_chat_owner
    from storage import database as db
    _user("u-ann", name="Ann"); _user("u-ed"); _user("u-mgr"); _user("u-admin", "admin")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-srv", "Server", scope="admin", owner="u-admin")
    _machine("m-gpu", "GPU box", scope="admin", owner="u-admin")
    online.update({"m-ann", "m-srv", "m-gpu"})
    if case in ("viewer", "contributor"):
        slug = _agent(default_scope="agent", collaborative=False)
        cid = _chat(slug, shared_chat_owner(slug))
        return db.get_chat(cid), _asker("u-ed", case)
    slug = _agent()
    _set_default(slug, "m-srv")
    if case == "manager":
        return db.get_chat(_chat(slug, "u-mgr", pin="m-srv")), _asker("u-mgr", "manager")
    if case == "editor":
        return db.get_chat(_chat(slug, "u-ed")), _asker("u-ed", "editor")
    if case == "admin-on-owner":
        return db.get_chat(_chat(slug, "u-ann")), _asker("u-admin", "admin", admin=True)
    if case == "phone":
        cid = _chat(slug, "u-ann")
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute("UPDATE chats SET source_type = %s WHERE id = %s",
                         (session_kind.PHONE.source_type, cid))
            conn.commit()
        return db.get_chat(cid), _asker("u-ann", "manager")
    if case == "task":
        run_id = str(uuid.uuid4())
        tid = _task_row(slug)
        db.create_run(run_id, tid, slug, "manual", None, "go", scope="user", created_by="u-ann")
        cid = session_kind.task_chat_id(run_id)
        db.create_chat(cid, "u-ann", slug, "default", execution_path="claude-code-cli")
        return db.get_chat(cid), _asker("u-ann", "editor")
    raise AssertionError(case)


@pytest.mark.parametrize("case, offered, default, refusal", [
    # A manager: the server and the agent's admin default, no other admin machine.
    ("manager", {"local", "m-srv"}, "m-srv", ""),
    # An editor on an admin-default agent: the server is their default, and
    # their colleague's laptop and the admin machines are not offered.
    ("editor", {"local"}, "local", ""),
    # A Shared-only viewer and contributor cannot drive, so cannot move.
    ("viewer", {"local"}, "local", chat_move.NOT_MOVER),
    ("contributor", {"local"}, "local", chat_move.NOT_MOVER),
    # A task-run chat: the run's person (its creator, an editor here).
    ("task", {"local", "m-ann"}, "local", ""),
    # A phone chat stays where it runs.
    ("phone", {"local", "m-ann", "m-srv"}, "m-srv", chat_move.KIND_STAYS),
    # A platform admin on a colleague's chat: the owner's default and the
    # owner's machines (the admin machines only for an owner-tier owner).
    ("admin-on-owner", {"local", "m-ann"}, "local", ""),
])
def test_the_chip_offers_each_role_its_places(temp_db, online, monkeypatch, case, offered,
                                              default, refusal):
    from storage import database as db
    chat, asker = _role_case(case, online)
    if case == "admin-on-owner":
        db.add_user_agent("u-ann", chat["agent"], "editor", "test")
    from core.config import task_config_builder as tcb
    if case == "task":
        ident = type("Ident", (), {"creds_user_sub": "u-ann", "role": "editor",
                                   "username": "u-ann"})
        monkeypatch.setattr(tcb, "rewarm_task_identity", lambda chat_id, agent: ident)
    view = chat_move.targets_view(chat, asker)
    assert {t["target"] for t in view["targets"]} == offered
    assert view["default_target"] == default
    assert view["refusal"] == refusal and view["can_move"] == (not refusal)


def test_the_task_dialog_reads_the_places_of_the_tasks_run_person(temp_db, online):
    from fastapi import HTTPException

    from api.remote import chat_placement as routes
    from storage import database as db
    _user("u-ann"); _user("u-bob")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    _machine("m-bob", "Bob laptop", owner="u-bob")
    online.update({"m-ann", "m-bob"})
    slug = _agent()
    db.add_user_agent("u-ann", slug, "editor", "test")
    db.add_user_agent("u-bob", slug, "editor", "test")
    mine = _task_row(slug, owner="u-ann", scope="user")
    shared = _task_row(slug, owner="u-bob", scope="agent")
    bob = _user_ctx("u-bob", agents=[slug])
    # Bob's own places for a new chat, but an agent-scope task runs with no
    # person: no personal computer is offered for it.
    own = asyncio.run(routes.agent_run_targets(slug, user=bob))
    assert "m-bob" in {t["target"] for t in own["targets"]}
    task = asyncio.run(routes.agent_run_targets(slug, task_id=shared, user=bob))
    assert {t["target"] for t in task["targets"]} == {"local"}
    ann_task = asyncio.run(routes.agent_run_targets(slug, task_id=mine,
                                                    user=_user_ctx("u-ann", agents=[slug])))
    assert {t["target"] for t in ann_task["targets"]} == {"local", "m-ann"}
    with pytest.raises(HTTPException) as e:          # another person's user-scope task
        asyncio.run(routes.agent_run_targets(slug, task_id=mine, user=bob))
    assert e.value.status_code == 404
