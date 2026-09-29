"""`sessions/index.json` growth bound.

`core.session.session_state._sessions` is append-only tracking metadata persisted to
`index.json`. `prune_dead_sessions()` drops entries whose `last_active` is older
than the TTL so the file can't grow unbounded — but it must keep recent entries
and never guess about entries it can't date.
"""

from datetime import datetime, timedelta, timezone

from core.session import session_state


def test_prune_drops_old_keeps_recent_undated_and_unparseable():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    old = (now - timedelta(days=session_state._SESSION_INDEX_TTL_DAYS + 20)).isoformat()
    recent = (now - timedelta(days=10)).isoformat()

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "old": {"created": True, "message_count": 3, "last_active": old},
            "recent": {"created": True, "message_count": 1, "last_active": recent},
            # no last_active → indistinguishable from a brand-new registration → keep
            "undated": {"created": True, "message_count": 0},
            # unparseable timestamp → don't guess, keep
            "bad": {"created": True, "last_active": "not-a-date"},
        })
        removed = session_state.prune_dead_sessions(now=now)
        assert removed == 1
        assert set(session_state._sessions) == {"recent", "undated", "bad"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


def test_prune_is_noop_when_all_recent():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "a": {"last_active": now.isoformat()},
            "b": {"last_active": (now - timedelta(days=1)).isoformat()},
        })
        assert session_state.prune_dead_sessions(now=now) == 0
        assert set(session_state._sessions) == {"a", "b"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


# ---------------------------------------------------------------------------
# reap_task_sessions — defense-in-depth backstop for the is_task leak
# (entries are normally popped on run completion in scheduler._run_task)
# ---------------------------------------------------------------------------


def test_reap_task_sessions_drops_leaked_keeps_recent_and_non_task(monkeypatch):
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    ttl = session_state._TASK_SESSION_REAP_TTL_SECONDS
    old = (now - timedelta(seconds=ttl + 60)).isoformat()
    recent = (now - timedelta(seconds=60)).isoformat()
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            # leaked task session, older than TTL → reaped
            "old-task": {"is_task": True, "last_active": old},
            # task session still within TTL (maybe running) → kept
            "live-task": {"is_task": True, "last_active": recent},
            # legacy task stub with no timestamp → reaped (treated as stale)
            "stub-task": {"is_task": True, "created": True},
            # NON-task old session → untouched (prune_dead_sessions owns it)
            "old-chat": {"last_active": old},
        })
        removed = session_state.reap_task_sessions(now=now)
        assert removed == 2
        assert set(session_state._sessions) == {"live-task", "old-chat"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


def test_reap_task_sessions_noop_when_recent_or_non_task(monkeypatch):
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "live-task": {"is_task": True, "last_active": now.isoformat()},
            # old but NOT is_task → the task reaper must not touch it
            "chat": {"last_active": (now - timedelta(days=200)).isoformat()},
        })
        assert session_state.reap_task_sessions(now=now) == 0
        assert set(session_state._sessions) == {"live-task", "chat"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


# ---------------------------------------------------------------------------
# The index is written behind: at most one write per
# SESSION_INDEX_FLUSH_S, compact, atomic, in a thread; synchronous with no
# loop, and synchronous at shutdown.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture
def index_iso(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    monkeypatch.setattr(session_state, "_SESSION_INDEX", path)
    monkeypatch.setattr(session_state, "_index_writer", session_state._IndexWriter())
    monkeypatch.setattr(config, "SESSION_INDEX_FLUSH_S", 0.05)
    saved = dict(session_state._sessions)
    session_state._sessions.clear()
    writes = []
    real = session_state._atomic_write

    def counting(p, blob, *, fsync):
        writes.append(blob)
        return real(p, blob, fsync=fsync)

    monkeypatch.setattr(session_state, "_atomic_write", counting)
    yield path, writes
    session_state._sessions.clear()
    session_state._sessions.update(saved)


def test_records_on_a_loop_coalesce_into_one_compact_write(index_iso):
    path, writes = index_iso

    async def scenario():
        for i in range(20):
            session_state._record_session_use(f"s{i}", client_type="dashboard")
        assert not path.exists()          # nothing on the loop, nothing yet
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert len(writes) == 1
    text = path.read_text()
    assert "\n" not in text               # compact
    assert len(json.loads(text)) == 20


def test_without_a_loop_the_write_is_immediate(index_iso):
    path, writes = index_iso
    session_state._record_session_use("s-sync", client_type="phone")
    assert json.loads(path.read_text())["s-sync"]["client_type"] == "phone"
    assert len(writes) == 1


def test_an_older_generation_never_replaces_a_newer_one(index_iso):
    path, _ = index_iso
    w = session_state._index_writer
    w._install('{"new": {}}', 2)
    w._install('{"old": {}}', 1)
    assert json.loads(path.read_text()) == {"new": {}}


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp(index_iso, monkeypatch):
    path, _ = index_iso
    path.write_text('{"kept": {}}')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(session_state.os, "replace", boom)
    session_state._record_session_use("s-fail")
    assert json.loads(path.read_text()) == {"kept": {}}
    assert [p.name for p in path.parent.iterdir()] == ["index.json"]


def test_a_call_from_another_thread_lands_on_the_owner_loop(index_iso):
    path, writes = index_iso

    async def scenario():
        session_state._record_session_use("s-loop")
        t = threading.Thread(target=session_state._record_session_use, args=("s-thread",))
        t.start()
        t.join()
        assert writes == []               # the thread did not write by itself
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert len(writes) == 1
    assert set(json.loads(path.read_text())) == {"s-loop", "s-thread"}


def test_flush_session_index_writes_the_last_state_at_once(index_iso):
    path, writes = index_iso

    async def scenario():
        session_state._record_session_use("s-a")
        session_state.flush_session_index()
        assert set(json.loads(path.read_text())) == {"s-a"}
        await asyncio.sleep(0.2)          # the cancelled timer never writes again

    asyncio.run(scenario())
    assert len(writes) == 1


def test_an_unchanged_timezone_writes_nothing(index_iso):
    path, writes = index_iso
    session_state.set_session_user_tz("s-tz", "Europe/Athens")
    session_state.set_session_user_tz("s-tz", "Europe/Athens")
    assert len(writes) == 1


def test_the_suite_writes_its_own_session_index():
    """The suite must never rewrite the live install's ``sessions/index.json``
    (the T1 dev install runs from this checkout): the conftest points the
    sessions dir, and so the two index files, under its scratch root."""
    import tempfile
    from pathlib import Path
    import config
    root = Path(tempfile.gettempdir())
    assert Path(config.SESSIONS_DIR).is_relative_to(root)
    assert Path(session_state._SESSION_INDEX).is_relative_to(root)
    assert Path(session_state._SECURITY_INDEX).is_relative_to(root)
