"""Sibling-session awareness: the parallel-activity prelude.

Visibility is keyed by (agent, chat-row owner); live sibling chats and
running background task runs (name + dyn-id) render into one line that is
hash-deduped per chat and re-injected when the set changes after emptying.
"""

import asyncio
import uuid

import pytest

from core.session import sibling_awareness as sa
from storage import database as task_store

AGENT = "pa"
OWNER = "user-alice"


@pytest.fixture(autouse=True)
def _reset_caches():
    sa._snapshot = {"ts": 0.0, "lanes": {}, "tasks": []}
    sa._chat_rows.clear()
    sa._session_chats.clear()
    sa._last_hash.clear()
    yield
    sa._snapshot = {"ts": 0.0, "lanes": {}, "tasks": []}
    sa._chat_rows.clear()
    sa._session_chats.clear()
    sa._last_hash.clear()


def _mk_chat(owner=OWNER, agent=AGENT, title="", **kw) -> str:
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, owner, agent, title=title, **kw)
    return chat_id


def _stream(chat_id, *, pending=None):
    from core.session.session_state import _chat_streaming_state
    _chat_streaming_state[chat_id] = {"streaming": True, "pending_permission": pending}
    return chat_id


@pytest.fixture
def streaming_state():
    from core.session.session_state import _chat_streaming_state
    yield _chat_streaming_state
    _chat_streaming_state.clear()


def _running_task(task_id="dyn-sib1", name="Daily PR review", scope="user",
                  created_by=OWNER, agent=AGENT, chat_id=""):
    task_store.create_dynamic_task(task_id, agent, name, "p", "cli", "one_time",
                                   None, None, None, 600, created_by)
    run_id = f"run-{task_id}"
    task_store.create_run(run_id, task_id, agent, "manual", None, "p",
                          task_type="one-time", scope=scope, created_by=created_by)
    task_store.update_run(run_id, status="running",
                          chat_id=chat_id or f"task-{run_id}")
    return task_id


class TestPreludeLine:
    def test_sibling_chat_listed_with_status(self, temp_db, streaming_state):
        own = _mk_chat(title="Main chat")
        sib = _mk_chat(title="Header rework")
        _stream(sib)
        line = sa._line_for_chat_sync(own)
        assert "'Header rework' (generating)" in line
        assert "awareness only" in line
        assert "Main chat" not in line          # own lane excluded

    def test_foreign_owner_excluded(self, temp_db, streaming_state):
        own = _mk_chat()
        foreign = _mk_chat(owner="user-bob", title="Bob secret")
        _stream(foreign)
        assert sa._line_for_chat_sync(own) == ""

    def test_awaiting_user_label(self, temp_db, streaming_state):
        own = _mk_chat()
        sib = _mk_chat(title="API design")
        _stream(sib, pending={"id": "p1"})
        assert "'API design' (awaiting user)" in sa._line_for_chat_sync(own)

    def test_running_task_listed_with_id(self, temp_db):
        own = _mk_chat()
        _running_task(task_id="dyn-abc123")
        line = sa._line_for_chat_sync(own)
        assert "'Daily PR review' (dyn-abc123)" in line
        assert "background tasks running" in line

    def test_other_users_task_hidden(self, temp_db):
        own = _mk_chat()
        _running_task(task_id="dyn-bob1", created_by="user-bob")
        assert sa._line_for_chat_sync(own) == ""

    def test_agent_scope_task_visible_to_all(self, temp_db):
        own = _mk_chat()
        _running_task(task_id="dyn-ag1", scope="agent", created_by="pa")
        assert "dyn-ag1" in sa._line_for_chat_sync(own)

    def test_worker_lane_not_double_listed_as_task(self, temp_db, streaming_state):
        own = _mk_chat()
        worker = _mk_chat(title="Lane 1", origin="delegated")
        _stream(worker)
        _running_task(task_id="dyn-lane9", name="Lane 1", chat_id=worker)
        line = sa._line_for_chat_sync(own)
        assert "'Lane 1' (generating)" in line
        assert "dyn-lane9" not in line

    def test_hash_dedup_and_reinjection(self, temp_db, streaming_state):
        own = _mk_chat()
        sib = _mk_chat(title="Lane A")
        _stream(sib)
        assert sa._line_for_chat_sync(own) != ""
        sa._snapshot["ts"] = 0.0                       # force re-snapshot
        assert sa._line_for_chat_sync(own) == ""       # unchanged → deduped
        streaming_state.clear()                        # set empties → hash clears
        sa._snapshot["ts"] = 0.0
        assert sa._line_for_chat_sync(own) == ""
        _stream(sib)                                   # reappears → re-injects
        sa._snapshot["ts"] = 0.0
        assert sa._line_for_chat_sync(own) != ""

    def test_a_title_with_a_line_break_stays_one_line(self, temp_db, streaming_state):
        # The transcripts strip the line from titles and bubbles as ONE line.
        own = _mk_chat()
        sib = _mk_chat(title="Fix [bug]\n  in the  inbox")
        _stream(sib)
        line = sa._line_for_chat_sync(own)
        assert "\n" not in line and "'Fix [bug] in the inbox' (generating)" in line
        from core.session.transcript_tailer import _title_from_prompt
        assert _title_from_prompt(f"{line}\n\nthe prompt") == "the prompt"

    def test_the_caches_stay_bounded(self, temp_db, monkeypatch):
        # Every task run has its own chat and session id: past the cap the
        # expired entries go, then everything.
        monkeypatch.setattr(sa, "_CACHE_CAP", 4)
        for i in range(10):
            sa._chat_row_sync(f"no-such-chat-{i}")
        assert len(sa._chat_rows) <= 4
        sa._last_hash.update({f"c{i}": i for i in range(4)})
        sa._bound(sa._last_hash, None)
        assert sa._last_hash == {}

    def test_prelude_line_resolves_session(self, temp_db, streaming_state):
        own = _mk_chat()
        sid = str(uuid.uuid4())
        task_store.update_chat(own, session_id=sid)
        sib = _mk_chat(title="Lane B")
        _stream(sib)
        line = asyncio.run(sa.prelude_line(sid))
        assert "'Lane B'" in line

    def test_prepend_if_changed(self, temp_db, streaming_state):
        own = _mk_chat()
        sib = _mk_chat(title="Lane C")
        _stream(sib)
        out = asyncio.run(sa.prepend_if_changed(own, "the prompt"))
        assert out.endswith("the prompt")
        assert "'Lane C'" in out
        sa._snapshot["ts"] = 0.0
        again = asyncio.run(sa.prepend_if_changed(own, "the prompt"))
        assert again == "the prompt"                   # unchanged → no prefix


class TestContextBlock:
    def test_block_lists_sessions_and_tasks(self, temp_db, streaming_state):
        sib = _mk_chat(title="Header rework")
        _stream(sib)
        _running_task(task_id="dyn-ctx1")
        block = sa.context_block(AGENT, OWNER)
        assert block.startswith("## Active parallel sessions")
        assert "'Header rework' — generating" in block
        assert "(dyn-ctx1) — running" in block

    def test_none_when_quiet(self, temp_db):
        assert sa.context_block(AGENT, OWNER) is None
