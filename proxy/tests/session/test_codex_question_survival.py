"""An unanswered ``request_user_input`` must not cost the user their answer.

Live on T1 (2026-08-06): a headless codex session blocked on
``request_user_input`` was reaped by the idle sweep (which only looked at
``last_activity``); answering afterwards resolved an event whose waiter was
gone, and the app-server answer write blew up on a closed transport
(``RuntimeError: … <WriteUnixTransport closed=True …>``) as an unhandled task
exception. The answer vanished and the turn never resumed.

Three seams pinned here:
  * the codex idle sweep spares a session with a pending question for one extra
    idle window (``_codex_reap_candidates``);
  * a ``question_response`` that reaches no live waiter falls back to the chat
    path (``_handle_chat`` → registry-aware single-flight revival);
  * ``respond()`` on a closed transport logs and returns instead of raising.

No daemon / no DB.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_codex_question_survival.py -q
"""
import asyncio

import pytest

from core.layers.codex import session as codex_session
from core.layers.codex.app_server_client import AppServerClient, AppServerError
from core.session import session_state
from ws.dashboard_dispatch import (
    ClientMessageDispatcher, _question_answers_to_text,
)


# ─────────────────────────────────────────────────────────────────────────
# Codex idle sweep: the question spare
# ─────────────────────────────────────────────────────────────────────────

class _FakeCodexSession:
    def __init__(self, idle: float, *, alive: bool = True):
        self.last_activity = 1000.0 - idle
        self.is_alive = alive


@pytest.fixture
def codex_registry():
    """Isolate the module-level session pool + the question registries."""
    codex_session._codex_sessions.clear()
    yield codex_session._codex_sessions
    codex_session._codex_sessions.clear()
    session_state._question_events.clear()
    session_state._session_permission_requests.clear()


def _park_question(session_id: str, request_id: str = "q-1") -> None:
    """Register an unanswered request_user_input the way wait_for_question does."""
    session_state._question_events[request_id] = asyncio.Event()
    session_state._session_permission_requests.setdefault(session_id, set()).add(request_id)


class TestCodexReapCandidates:
    def test_over_idle_without_question_is_reaped(self, codex_registry):
        codex_registry["s-idle"] = _FakeCodexSession(idle=200)
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-idle"]

    def test_pending_question_spared_under_cap(self, codex_registry):
        # Blocked on a HUMAN: byte-idle but not abandoned.
        codex_registry["s-parked"] = _FakeCodexSession(idle=150)
        _park_question("s-parked")
        assert codex_session._codex_reap_candidates(1000.0, 100) == []

    def test_pending_question_reaped_over_cap(self, codex_registry):
        # One extra idle window only — an abandoned question can't pin the slot.
        codex_registry["s-parked"] = _FakeCodexSession(
            idle=codex_session._QUESTION_PARK_TIMEOUT_MULT * 100 + 10)
        _park_question("s-parked")
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-parked"]

    def test_answered_question_does_not_spare(self, codex_registry):
        # The waiter is gone (answered/released) → nothing left to wait on.
        codex_registry["s-answered"] = _FakeCodexSession(idle=150)
        session_state._session_permission_requests["s-answered"] = {"q-gone"}
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-answered"]

    def test_dead_session_is_reaped_regardless_of_question(self, codex_registry):
        codex_registry["s-dead"] = _FakeCodexSession(idle=1, alive=False)
        _park_question("s-dead")
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-dead"]


def test_has_pending_question_tracks_the_session_index():
    session_state._question_events.clear()
    session_state._session_permission_requests.clear()
    assert session_state.has_pending_question("s-x") is False
    _park_question("s-x", "q-x")
    assert session_state.has_pending_question("s-x") is True
    session_state._question_events.pop("q-x")
    assert session_state.has_pending_question("s-x") is False
    session_state._session_permission_requests.clear()


# ─────────────────────────────────────────────────────────────────────────
# question_response: fallback when no live waiter took the answer
# ─────────────────────────────────────────────────────────────────────────

class _FakeConnection(ClientMessageDispatcher):
    """The dispatcher mixin with only the collaborators question_response uses."""

    def __init__(self, *, streaming: bool = False, chat_id: str = "chat-q"):
        self.session_id = "sid-reaped"
        self.chat_id = chat_id
        self.streaming = streaming
        self.message_queue: list[str] = []
        self.sent: list[dict] = []
        self.chat_calls: list[dict] = []

    async def _may_resolve_permission(self, request_id: str) -> bool:
        return True

    async def _handle_chat(self, msg: dict) -> None:
        self.chat_calls.append(msg)

    async def _send(self, frame: dict) -> None:
        self.sent.append(frame)


def _answers(*values: str) -> dict:
    return {"q-id-verbatim": {"answers": list(values)}}


@pytest.mark.asyncio
class TestQuestionResponseFallback:
    async def test_live_waiter_takes_the_answer_no_chat_turn(self):
        event = asyncio.Event()
        session_state._question_events["q-live"] = event
        try:
            conn = _FakeConnection()
            await conn._dispatch_client_message({
                "type": "question_response", "request_id": "q-live",
                "answers": _answers("Option A"),
            })
            assert event.is_set()
            assert session_state._question_answers["q-live"] == _answers("Option A")
            assert conn.chat_calls == []   # the held turn resumes in place
        finally:
            session_state._question_events.pop("q-live", None)
            session_state._question_answers.pop("q-live", None)

    async def test_no_waiter_replays_the_answer_as_a_chat_message(self):
        # The parked session was reaped — resolve_question finds nothing, so the
        # answer rides the composer path (dead-session branch → revival).
        conn = _FakeConnection()
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": "q-dead",
            "answers": _answers("Ship it", "with tests"),
        })
        assert conn.chat_calls == [
            {"text": "Ship it, with tests", "chat_id": "chat-q"}
        ]

    async def test_no_waiter_while_streaming_queues_instead(self):
        conn = _FakeConnection(streaming=True)
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": "q-dead",
            "answers": _answers("Option B"),
        })
        assert conn.chat_calls == []
        assert conn.message_queue == ["Option B"]
        assert conn.sent == [{"type": "queued", "index": 0, "text": "Option B"}]

    async def test_no_waiter_and_no_answer_text_is_a_noop(self):
        conn = _FakeConnection()
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": "q-dead", "answers": {},
        })
        assert conn.chat_calls == []
        assert conn.message_queue == []


class TestAnswersToText:
    """Mirrors the dashboard's free-text branch (QuestionDialog): selected
    labels joined by ", ", one question per line."""

    def test_multi_question_one_line_each(self):
        text = _question_answers_to_text({
            "q1": {"answers": ["Postgres"]},
            "q2": {"answers": ["Redis", "S3"]},
        })
        assert text == "Postgres\nRedis, S3"

    def test_ignores_malformed_entries(self):
        assert _question_answers_to_text({"q1": "nope", "q2": {"answers": []}}) == ""


# ─────────────────────────────────────────────────────────────────────────
# respond() on a closed transport
# ─────────────────────────────────────────────────────────────────────────

class _ClosedStdin:
    def write(self, data: bytes) -> None:
        raise RuntimeError(
            "unable to perform operation on <WriteUnixTransport closed=True "
            "reading=False 0x1>; the handler is closed")

    async def drain(self) -> None:  # pragma: no cover - write raises first
        pass


class _ClosedProc:
    stdin = _ClosedStdin()


@pytest.mark.asyncio
class TestClosedTransportWrite:
    def _client(self) -> AppServerClient:
        client = AppServerClient(env={}, label="codex-test")
        client.proc = _ClosedProc()
        client._open_server_requests.add(7)
        return client

    async def test_respond_swallows_the_closed_transport(self):
        # The session died while a held request waited on a human — there is
        # nobody to answer. Must NOT surface as an unhandled task exception.
        client = self._client()
        await client.respond(7, {"answers": {}})
        assert client.is_server_request_open(7) is False

    async def test_respond_error_swallows_the_closed_transport(self):
        client = self._client()
        await client.respond_error(7, -32603, "handler error")
        assert client.is_server_request_open(7) is False

    async def test_request_path_raises_the_typed_transport_error(self):
        # Callers that DO need the failure still get one clean error type, not
        # a raw event-loop RuntimeError.
        client = self._client()
        with pytest.raises(AppServerError):
            await client.notify("thread/ping", {})
