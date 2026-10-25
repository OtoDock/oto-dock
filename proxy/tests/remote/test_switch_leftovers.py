"""The session files an engine switch leaves behind
(``services/remote/switch_leftovers``): the switch records them, and the
sweep removes them where the chat ran once the new engine has answered and
that turn ended well; a turn still open or one that ended badly, a switch
that never answered, a session something else still names, and a place
that cannot take the delete now keep them; every record ends after its
window, and a delete that hangs is asked again later."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from core import placement
from services.remote import switch_leftovers
from tests.remote.test_chat_move import _agent, _chat, _machine, _task_row, _user

CLAUDE = "claude-code-cli"
CODEX = "codex-cli"


@pytest.fixture
def online(monkeypatch):
    """Machines reachable and on a satellite that takes the file frames:
    the set the test fills."""
    from core.remote import satellite_connection as sc
    from services.remote import remote_status
    up: set[str] = set()
    monkeypatch.setattr(remote_status, "is_reachable", lambda mid: mid in up)
    monkeypatch.setattr(sc.get_connection_manager(), "satellite_supports_session_move",
                        lambda mid: mid in up)
    return up


def _rows():
    from storage import remote_store
    return remote_store.switch_leftovers()


def _record(cid: str, old: str = CLAUDE, asker: str = "u-ann", role: str = "manager") -> bool:
    """The switch: the row as it read it is recorded, then the rebind drops
    the ids."""
    from storage import database as db
    done = switch_leftovers.record(db.get_chat(cid), old, asker, role)
    db.update_chat(cid, session_id=None, codex_thread_id=None)
    return done


def _answer(cid: str, text: str = "Hello from the new engine.") -> None:
    from storage import database as db
    db.add_chat_message(cid, "assistant", text)


def _turn_ended(cid: str, reason: str) -> None:
    """A turn's ending row, as the pump writes it (and a restart's, which
    closes a turn it cut with reason ``restarted``)."""
    import json

    from storage import database as db
    from ws import wire_events as wire
    db.add_chat_message(cid, "event", "", event_type=wire.SYSTEM, event_data=json.dumps({
        "type": wire.SYSTEM, "subtype": wire.SUBTYPE_TURN_ENDED, "reason": reason,
        "message": "The turn ended."}))


def _age(days_past_window: float = 0.0) -> None:
    from storage.pg import get_conn
    old = (datetime.now(timezone.utc) - switch_leftovers.WINDOW
           - timedelta(minutes=1, days=days_past_window)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE engine_switch_leftovers SET created_at = %s", (old,))
        conn.commit()


@pytest.fixture
def deletes(monkeypatch):
    """The deletes the sweep asks for, as ``(place, engine, refs)``."""
    from core.remote import session_files as sf
    seen: list[tuple[str, str, list[str]]] = []

    async def _delete(place, scope):
        seen.append((place, scope.engine, scope.refs))
        return 2
    monkeypatch.setattr(sf, "delete_files", _delete)
    return seen


def _sweep():
    switch_leftovers._next.clear()
    asyncio.run(switch_leftovers.sweep())


def test_a_switch_records_the_old_engines_session_where_the_chat_ran(temp_db, online):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    sid, thread = str(uuid.uuid4()), str(uuid.uuid4())
    cid = _chat(slug, "u-ann", pin="m-ann", sid=sid, thread=thread, engine=CODEX)
    assert _record(cid, old=CODEX)
    [row] = _rows()
    assert (row["chat_id"], row["engine"], row["session_id"], row["resume_handle"], row["place"]) == (
        cid, CODEX, sid, thread, "m-ann")


def test_nothing_is_recorded_without_an_id_or_without_session_files(temp_db, online):
    _user("u-ann")
    slug = _agent()
    assert not _record(_chat(slug, "u-ann"))                                   # no id
    assert not _record(_chat(slug, "u-ann", sid="not-a-uuid"))                 # no real id
    assert not _record(_chat(slug, "u-ann", sid=str(uuid.uuid4())), old="direct-llm")  # no files
    assert _rows() == []


def test_the_files_go_on_the_platform_once_the_new_engine_answered(temp_db, online, deletes):
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _record(cid)
    _sweep()
    assert deletes == [] and len(_rows()) == 1          # no answer yet: kept
    _answer(cid)
    _sweep()
    assert deletes == [(placement.LOCAL, CLAUDE, [sid])]
    assert _rows() == []


def test_the_real_files_and_the_folder_beside_them_go(temp_db, online):
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _record(cid)
    [row] = _rows()
    home = Path(config.AGENTS_DIR) / slug / (f"users/{row['home_user']}" if row["home_user"] else "workspace")
    proj = home / ".claude" / "projects" / "-key"
    (proj / sid / "subagents").mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n')
    (proj / sid / "subagents" / "agent-a.jsonl").write_text("sub")
    _answer(cid)
    _sweep()
    assert not (proj / f"{sid}.jsonl").exists() and not (proj / sid).exists()
    assert _rows() == []


def test_on_a_machine_the_delete_goes_to_the_machine_and_waits_while_it_is_away(
        temp_db, online, deletes):
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    thread = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", pin="m-ann", thread=thread, engine=CODEX)
    _record(cid, old=CODEX)
    _answer(cid)
    _sweep()
    assert deletes == [] and len(_rows()) == 1          # offline: asked again later
    online.add("m-ann")
    _sweep()
    assert deletes == [("m-ann", CODEX, [thread])] and _rows() == []


def test_a_switch_whose_new_engine_never_answered_keeps_the_files(temp_db, online, deletes):
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _record(cid)
    # An error of the new engine is no answer.
    from storage import database as db
    db.add_chat_message(cid, "event", "", event_type="error", event_data="{}")
    _sweep()
    assert deletes == [] and len(_rows()) == 1
    _age()
    _sweep()
    assert deletes == [] and _rows() == []              # given up: the files stay


@pytest.mark.parametrize("reason", ["error", "limit", "stopped", "restarted"])
def test_text_followed_by_a_bad_ending_is_no_answer(temp_db, online, deletes, reason):
    """The new engine's first turn saved some text (a steer's early save, a
    long turn's checkpoint) and then ended badly, or a restart cut it: the
    old files stay."""
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _record(cid)
    _answer(cid, "Here is what I found so far")
    _turn_ended(cid, reason)
    _sweep()
    assert deletes == [] and len(_rows()) == 1
    # The next turn answers and ends well: now they go.
    _answer(cid)
    _sweep()
    assert len(deletes) == 1 and _rows() == []


def test_text_of_a_turn_still_open_is_no_answer(temp_db, online, deletes, monkeypatch):
    from types import SimpleNamespace

    from core.events import stream_pump
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _record(cid)
    _answer(cid, "Still working")
    monkeypatch.setitem(stream_pump._active_pumps, cid, SimpleNamespace(is_done=False))
    _sweep()
    assert deletes == [] and len(_rows()) == 1
    monkeypatch.delitem(stream_pump._active_pumps, cid)
    _sweep()
    assert len(deletes) == 1 and _rows() == []


def test_a_row_whose_machine_never_returns_ends_after_its_window(temp_db, online, deletes, caplog):
    import logging
    _user("u-ann")
    _machine("m-ann", "Ann laptop", owner="u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", pin="m-ann", sid=str(uuid.uuid4()))
    _record(cid)
    _answer(cid)
    _sweep()
    assert deletes == [] and len(_rows()) == 1          # offline: asked again later
    _age()
    with caplog.at_level(logging.INFO, logger="otodock.remote"):
        _sweep()
    assert deletes == [] and _rows() == []
    assert any("given up after 30 days" in r.getMessage() for r in caplog.records)


def test_a_delete_that_hangs_is_cut_short_and_asked_again(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _record(cid)
    _answer(cid)

    async def _silent(place, scope):
        await asyncio.sleep(60)
        return 0
    monkeypatch.setattr(sf, "delete_files", _silent)
    monkeypatch.setattr(switch_leftovers, "DELETE_WAIT_S", 0.2)
    switch_leftovers._next.clear()
    started = asyncio.run(_timed(switch_leftovers.sweep()))
    assert started < 5.0                                # never the delete's own wait
    [row] = _rows()
    assert row["id"] in switch_leftovers._next


async def _timed(coro) -> float:
    import time
    t0 = time.monotonic()
    await coro
    return time.monotonic() - t0


def test_deleting_the_agent_deletes_its_records(temp_db, online, deletes):
    from storage.agents import agent_store
    _user("u-ann")
    slug, other = _agent(), _agent()
    _record(_chat(slug, "u-ann", sid=str(uuid.uuid4())))
    _record(_chat(other, "u-ann", sid=str(uuid.uuid4())))
    assert len(_rows()) == 2
    assert agent_store.delete_agent(slug)
    assert [r["agent"] for r in _rows()] == [other]


def test_a_session_something_else_names_stays(temp_db, online, deletes):
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _chat(slug, "u-ann", sid=sid)                        # another chat on the same session
    _record(cid)
    _answer(cid)
    _sweep()
    assert deletes == [] and _rows() == []


def test_a_session_a_task_reports_to_stays(temp_db, online, deletes):
    from storage.pg import get_conn
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _record(cid)
    _answer(cid)
    tid = _task_row(slug)
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET on_complete_session_id = %s WHERE id = %s", (sid, tid))
        conn.commit()
    _sweep()
    assert deletes == [] and _rows() == []


def test_a_chat_that_names_the_old_session_again_keeps_it(temp_db, online, deletes):
    from storage import database as db
    _user("u-ann")
    slug = _agent()
    sid = str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    _record(cid)
    _answer(cid)
    db.update_chat(cid, session_id=sid)
    _sweep()
    assert deletes == [] and _rows() == []


def test_a_deleted_chats_leftover_goes_and_a_deleted_machine_drops_the_row(temp_db, online, deletes):
    from storage import database as db
    from storage.pg import get_conn
    _user("u-ann")
    _machine("m-gone", "Gone PC", owner="u-ann")
    slug = _agent()
    sid, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
    cid = _chat(slug, "u-ann", sid=sid)
    cid2 = _chat(slug, "u-ann", pin="m-gone", sid=sid2)
    _record(cid)
    _record(cid2)
    db.delete_chat(cid)
    with get_conn() as conn:
        conn.execute("DELETE FROM remote_machines WHERE id = 'm-gone'")
        conn.commit()
    _answer(cid2)
    _sweep()
    assert deletes == [(placement.LOCAL, CLAUDE, [sid])]
    assert _rows() == []


def test_a_busy_place_is_asked_again_later(temp_db, online, monkeypatch):
    from core.remote import session_files as sf
    _user("u-ann")
    slug = _agent()
    cid = _chat(slug, "u-ann", sid=str(uuid.uuid4()))
    _record(cid)
    _answer(cid)

    async def _busy(place, scope):
        raise sf.TransferError(sf.WHY_BUSY, "busy: a live session holds the ids")
    monkeypatch.setattr(sf, "delete_files", _busy)
    _sweep()
    [row] = _rows()
    assert row["id"] in switch_leftovers._next
