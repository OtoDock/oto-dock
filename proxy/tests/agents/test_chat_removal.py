"""The one removal of a chat (services/agents/chat_removal.py) and the guard
the retention sweep uses (``db_chats.delete_chat``): a chat touched since the
sweep's query loses nothing of it."""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import config
from storage import database as task_store, db_reply_reports
from storage.pg import get_conn

AGENT = "alpha"
OWNER = "user-admin"


def _chat(age_days: int = 40) -> str:
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, OWNER, AGENT)
    task_store.add_chat_message(cid, "user", "the words of the chat")
    task_store.mark_chat_read(cid, OWNER)
    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE chats SET updated_at=%s WHERE id=%s", (stamp, cid))
        conn.commit()
    return cid


def _count(sql: str, cid: str) -> int:
    with get_conn() as conn:
        return int(conn.execute(sql, (cid,)).fetchone()["n"])


def _rows(cid: str) -> dict[str, int]:
    return {
        "chat": _count("SELECT count(*) AS n FROM chats WHERE id=%s", cid),
        "messages": _count("SELECT count(*) AS n FROM chat_messages WHERE chat_id=%s", cid),
        "search": _count("SELECT count(*) AS n FROM chat_search WHERE chat_id=%s", cid),
        "reads": _count("SELECT count(*) AS n FROM chat_reads WHERE chat_id=%s", cid),
    }


def _cutoff(days: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _report(cid: str) -> int:
    with get_conn() as conn:
        message_id = conn.execute(
            "SELECT id FROM chat_messages WHERE chat_id=%s LIMIT 1", (cid,),
        ).fetchone()["id"]
    return db_reply_reports.add(OWNER, cid, message_id, agent=AGENT, reason="wrong",
                               note="", reply_text="the reply")


def test_a_chat_touched_since_the_query_loses_nothing(temp_db):
    cid = _chat(age_days=1)
    before = _rows(cid)
    assert before["chat"] == 1 and before["search"] == 1 and before["reads"] == 1

    assert task_store.delete_chat(cid, untouched_before=_cutoff()) is False

    assert _rows(cid) == before


def test_a_chat_past_the_guard_goes_whole(temp_db):
    cid = _chat(age_days=40)

    assert task_store.delete_chat(cid, untouched_before=_cutoff()) is True

    assert _rows(cid) == {"chat": 0, "messages": 0, "search": 0, "reads": 0}


def test_a_missing_chat_under_the_guard_is_not_an_error(temp_db):
    assert task_store.delete_chat("no-such-chat", untouched_before=_cutoff()) is False


def test_reply_reports_go_only_when_asked_and_in_the_same_delete(temp_db):
    kept = _chat()
    report_kept = _report(kept)
    gone = _chat()
    report_gone = _report(gone)

    assert task_store.delete_chat(kept, untouched_before=_cutoff()) is True
    assert task_store.delete_chat(
        gone, untouched_before=_cutoff(), with_reply_reports=True) is True

    with get_conn() as conn:
        rows = {r["id"]: r["chat_id"] for r in conn.execute(
            "SELECT id, chat_id FROM reply_reports").fetchall()}
    assert rows == {report_kept: None}
    assert report_gone not in rows


def test_reports_stay_when_the_guard_keeps_the_chat(temp_db):
    cid = _chat(age_days=1)
    report = _report(cid)

    assert task_store.delete_chat(
        cid, untouched_before=_cutoff(), with_reply_reports=True) is False

    with get_conn() as conn:
        row = conn.execute("SELECT chat_id FROM reply_reports WHERE id=%s", (report,)).fetchone()
    assert row["chat_id"] == cid


def test_remove_chat_kept_by_the_guard_touches_nothing_else(temp_db, monkeypatch):
    from core.events import input_queue
    from services.agents import chat_removal
    from services.media import preview_snapshots
    previews: list[str] = []
    monkeypatch.setattr(preview_snapshots, "delete_chat_dir", previews.append)
    cid = _chat(age_days=1)
    chat = task_store.get_chat(cid)

    async def run():
        return await chat_removal.remove_chat(chat, untouched_before=_cutoff())

    assert asyncio.run(run()) is False
    assert task_store.get_chat(cid) is not None
    assert previews == []
    assert not input_queue.deleting(cid)


def test_remove_chat_runs_the_steps_when_the_rows_go(temp_db, monkeypatch):
    from core.events import input_queue
    from services.agents import chat_removal
    from services.media import preview_snapshots
    previews: list[str] = []
    monkeypatch.setattr(preview_snapshots, "delete_chat_dir", previews.append)
    cid = _chat(age_days=40)
    chat = task_store.get_chat(cid)

    async def run():
        return await chat_removal.remove_chat(chat, untouched_before=_cutoff())

    assert asyncio.run(run()) is True
    assert task_store.get_chat(cid) is None
    assert previews == [cid]
    assert input_queue.deleting(cid)


# ── a chat's cached media copies ────────────────────────────────────────────


def _cache_token(cid: str, path, *, sid: str = "sess-cache", expired: bool = False) -> str:
    token = uuid.uuid4().hex
    task_store.create_media_token(token, str(path), media_kind="image", chat_id=cid, session_id=sid,
                                  agent=AGENT, cache_owned=True,
                                  expires_at="2000-01-01T00:00:00+00:00" if expired else "")
    return token


def _copies(tmp_path, monkeypatch, cid: str, *, expired: bool = False):
    """A chat's cache-owned media: a host media copy, a pulled file in its
    session's host cache, one whose session folder is a link to a stand-in
    folder beside the agents folder, and a row (never made by the platform)
    that names a file of the agent's own folder."""
    import config
    from services.media import media_pipeline
    monkeypatch.setattr(media_pipeline, "_HOST_CACHE_DIR", tmp_path / "host-media-cache")
    host = tmp_path / "host-media-cache" / "clip.mp4"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"v")
    cache = config.AGENTS_DIR / ".remote-host-cache"
    pulled = cache / "sess-a" / "Desktop" / "photo.png"
    pulled.parent.mkdir(parents=True)
    pulled.write_bytes(b"p")
    out = tmp_path / "outside"
    (out / "Desktop").mkdir(parents=True)
    (out / "Desktop" / "photo.png").write_text("kept")
    (cache / "sess-b").symlink_to(out, target_is_directory=True)
    own = config.AGENTS_DIR / AGENT / "workspace" / "own.png"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(b"o")
    _cache_token(cid, host, expired=expired)
    _cache_token(cid, pulled, sid="sess-a", expired=expired)
    _cache_token(cid, cache / "sess-b" / "Desktop" / "photo.png", sid="sess-b", expired=expired)
    _cache_token(cid, own, expired=expired)
    return host, pulled, out / "Desktop" / "photo.png", own


def test_a_deleted_chats_cached_copies_go_beneath_their_root(temp_db, tmp_path, monkeypatch):
    from services.agents import chat_removal
    from services.media import preview_snapshots
    monkeypatch.setattr(preview_snapshots, "delete_chat_dir", lambda _cid: None)
    cid = _chat(age_days=1)
    host, pulled, outside, own = _copies(tmp_path, monkeypatch, cid)
    assert asyncio.run(chat_removal.remove_chat(task_store.get_chat(cid))) is True
    assert not host.exists() and not pulled.exists()
    assert outside.read_text() == "kept" and own.read_bytes() == b"o"


def test_expired_cached_copies_go_beneath_their_root(temp_db, tmp_path, monkeypatch):
    import startup_sweeps
    cid = _chat(age_days=1)
    host, pulled, outside, own = _copies(tmp_path, monkeypatch, cid, expired=True)
    asyncio.run(startup_sweeps._sweep_media())
    assert not host.exists() and not pulled.exists()
    assert outside.read_text() == "kept" and own.read_bytes() == b"o"
    assert _count("SELECT count(*) AS n FROM media_tokens WHERE chat_id=%s", cid) == 0


def test_a_phone_chats_cached_copies_go_with_it(temp_db, tmp_path, monkeypatch):
    """The phone chat's copies, and the copies of its wrap-up run's own
    chat, go with the conversation."""
    from core.session import session_kind
    from services.infra import external_retention
    from storage.chat import conversation_store
    cid = _chat(age_days=1)
    host, pulled, outside, own = _copies(tmp_path, monkeypatch, cid)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(run_id, conversation_store.wrapup_task_id(cid), AGENT, "manual", None,
                          "transcript", "wrapup", "agent", None)
    run_chat = session_kind.task_chat_id(run_id)
    task_store.create_chat(run_chat, f"task::{AGENT}", AGENT, "auto", source_type="task")
    wrapped = config.AGENTS_DIR / ".remote-host-cache" / "sess-w" / "Desktop" / "notes.png"
    wrapped.parent.mkdir(parents=True)
    wrapped.write_bytes(b"w")
    _cache_token(run_chat, wrapped, sid="sess-w")
    stats = {"phone_chats_deleted": 0, "caller_bytes_freed": 0, "errors": 0}
    external_retention._delete_phone_chat(task_store.get_chat(cid), stats, dry_run=False)
    assert task_store.get_chat(cid) is None and task_store.get_chat(run_chat) is None
    assert not host.exists() and not pulled.exists() and not wrapped.exists()
    assert outside.read_text() == "kept" and own.read_bytes() == b"o"


def test_a_copy_whose_session_folder_already_went_is_removed_quietly(temp_db, monkeypatch, caplog):
    """A chat of a paired computer deleted after its session closed: the
    session's cache folder went at the close, so its copies are gone
    already and nothing is said."""
    from services.agents import chat_removal
    from services.media import preview_snapshots
    monkeypatch.setattr(preview_snapshots, "delete_chat_dir", lambda _cid: None)
    cid = _chat(age_days=1)
    config.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    gone = config.AGENTS_DIR / ".remote-host-cache" / "sess-closed" / "abc" / "photo.png"
    _cache_token(cid, gone, sid="sess-closed")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(chat_removal.remove_chat(task_store.get_chat(cid))) is True
    assert "Media cache copy not removed" not in caplog.text


def test_an_expired_download_whose_folder_already_went_is_swept_quietly(temp_db, tmp_path, monkeypatch, caplog):
    """A prepared download whose folder the person's next download removed:
    its row expires later and the sweep says nothing."""
    import startup_sweeps
    from services.media import media_pipeline
    monkeypatch.setattr(media_pipeline, "_HOST_CACHE_DIR", tmp_path / "host-media-cache")
    (tmp_path / "host-media-cache" / "converted").mkdir(parents=True)
    gone = tmp_path / "host-media-cache" / "converted" / "tok" / "report.pdf"
    _cache_token(_chat(age_days=1), gone, expired=True)
    with caplog.at_level(logging.WARNING):
        asyncio.run(startup_sweeps._sweep_media())
    assert "Media cache copy not removed" not in caplog.text
