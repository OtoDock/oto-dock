"""A meeting participant runs in its host chat's permission mode. When the
host chat's session is gone (reaped idle, or the meeting started with none)
the participant reads the chat's stored mode (a word from before 1.9 read as
its current one), and ``ask`` when there is none: an unknown host never reads
as ``auto``, which let a participant write without asking
(``api/hooks/permission._meeting_mode``)."""

import asyncio

import pytest

import storage.database as db
from api.hooks import permission, routing
from api.hooks.hooks import decide_tool_permission
from auth.path_policy import SecurityContext
from core.session import session_events, session_state

PART = "part-mode-1"
HOST = "host-mode-1"
CHAT = "chat-meeting-mode-1"


def _route(parent: str = HOST, chat: str = CHAT) -> routing.HookRoute:
    return routing.HookRoute(queue_session_id="meeting-mm", chat_id=chat,
                             meeting_agent="agent-a", parent_session_id=parent,
                             is_meeting=True)


@pytest.fixture
def chat_row(temp_db):
    def make(mode: str) -> None:
        db.create_chat(CHAT, "user-a", "agent-a", permission_mode=mode)
    return make


@pytest.fixture(autouse=True)
def _clean():
    yield
    for sid in (HOST, PART):
        session_state._session_modes.pop(sid, None)
        session_state._reloaded_state.pop(sid, None)
    session_state.cleanup_meeting_session_info(PART)
    session_state.cleanup_session_permission_state(PART)


def test_an_unknown_session_records_no_mode_and_reads_auto_elsewhere():
    assert session_state.recorded_session_mode(HOST) == ""
    assert session_state.get_session_mode(HOST) == "auto"
    session_state.set_session_mode(HOST, "acceptEdits")
    assert session_state.recorded_session_mode(HOST) == "autoReview"


@pytest.mark.asyncio
async def test_the_live_host_mode_wins(chat_row):
    chat_row("default")
    session_state.set_session_mode(HOST, "dontAsk")
    assert await permission._meeting_mode(_route()) == "dontAsk"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,reads", [
    ("dontAsk", "dontAsk"), ("ask", "ask"), ("autoReview", "autoReview"), ("plan", "plan"),
    ("auto", "auto"), ("default", "ask"), ("acceptEdits", "autoReview"),
])
async def test_a_gone_host_reads_the_chats_stored_mode(chat_row, stored, reads):
    chat_row(stored)
    assert await permission._meeting_mode(_route()) == reads
    assert await permission._meeting_mode(_route(parent="")) == reads


@pytest.mark.asyncio
async def test_no_usable_stored_mode_reads_ask(chat_row):
    assert await permission._meeting_mode(_route()) == "ask"   # no row
    chat_row("judge")
    assert await permission._meeting_mode(_route()) == "ask"
    assert await permission._meeting_mode(_route(chat="")) == "ask"


@pytest.mark.asyncio
async def test_the_chat_row_is_read_on_the_fast_lane(chat_row, monkeypatch):
    """A participant's tool call never queues behind turn persistence."""
    import storage.pg as pg
    reads: list = []

    async def _fast(fn, /, *args):
        reads.append(fn)
        return fn(*args)

    async def _bulk(fn, /, *args):
        raise AssertionError("the meeting's chat was read on the bulk lane")

    monkeypatch.setattr(pg, "run_db_fast", _fast)
    monkeypatch.setattr(pg, "run_db", _bulk)
    chat_row("acceptEdits")
    assert await permission._meeting_mode(_route(parent="")) == "autoReview"
    assert reads == [db.get_chat]


def _participant() -> None:
    session_state.register_session_state(PART, "auto", SecurityContext(
        role="manager", username="user-a", agent="agent-a", is_admin_agent=False))
    session_state.set_meeting_session_info(PART, HOST, "meeting-mm", "agent-a", CHAT)


async def _first_decision(sid: str, decide, monkeypatch) -> tuple[dict, list]:
    """The decision on an edit and the prompts it queued (no person answers)."""
    queued: list = []

    class _Queue:
        async def put(self, item):
            queued.append(item)

    async def _no_answer(request_id, session_id, timeout):
        return False

    monkeypatch.setattr(permission, "get_permission_queue", lambda sid: _Queue())
    monkeypatch.setattr(permission, "wait_for_permission", _no_answer)
    result = await asyncio.wait_for(
        decide(sid, "Bash", {"command": "touch /workspace/x"}), timeout=10)
    return result, queued


@pytest.mark.asyncio
@pytest.mark.parametrize("decide", [decide_tool_permission, session_events.pre_tool],
                         ids=["hook", "direct-llm"])
async def test_a_gone_host_of_a_default_chat_asks_before_an_edit(chat_row, monkeypatch, decide):
    chat_row("default")
    _participant()
    result, queued = await _first_decision(PART, decide, monkeypatch)
    assert result["decision"] == "deny" and len(queued) == 1


@pytest.mark.asyncio
async def test_a_gone_host_of_a_dont_ask_chat_runs_the_edit(chat_row, monkeypatch):
    chat_row("dontAsk")
    _participant()
    result, queued = await _first_decision(PART, decide_tool_permission, monkeypatch)
    assert result["decision"] == "allow" and queued == []
