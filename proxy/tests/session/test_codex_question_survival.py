"""A prompt that waits on a person must not cost them their answer.

Live on T1 (2026-08-06): a headless codex session blocked on a held server
request was reaped by the idle sweep (which only looked at
``last_activity``); answering afterwards resolved an event whose waiter was
gone, and the app-server answer write blew up on a closed transport
(``RuntimeError: … <WriteUnixTransport closed=True …>``) as an unhandled task
exception. The answer vanished and the turn never resumed.

Three seams pinned here:
  * the codex idle sweep spares a session with a pending prompt (an approval
    the bridge holds; a question holds nothing since 1.9.1)
    (``_codex_reap_candidates``);
  * a ``question_response`` (a held question card stored by 1.9.0) is the
    person's next message: the chat path (``_handle_chat`` → registry-aware
    single-flight revival), claimed once across sockets;
  * ``respond()`` on a closed transport logs and returns instead of raising.

No daemon / no DB.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_codex_question_survival.py -q
"""
import pytest

from core.layers.codex import session as codex_session
from core.layers.codex.app_server_client import AppServerClient, AppServerError
from core.session import session_state
from ws.dashboard_dispatch import (
    ClientMessageDispatcher,
    _question_answers_to_text,
)
from ws.dashboard_prompts import PromptController

# ─────────────────────────────────────────────────────────────────────────
# Codex idle sweep: the question spare
# ─────────────────────────────────────────────────────────────────────────

class _FakeCodexSession:
    def __init__(self, idle: float, *, alive: bool = True):
        self.last_activity = 1000.0 - idle
        self.is_alive = alive
        self._current_turn_id = None


@pytest.fixture
def codex_registry():
    """Isolate the module-level session pool + the prompt registries."""
    codex_session._codex_sessions.clear()
    yield codex_session._codex_sessions
    codex_session._codex_sessions.clear()
    for rid in ("q-1", "q-x"):
        session_state._permission_events.pop(rid, None)
    session_state._session_permission_requests.clear()


def _park_question(session_id: str, request_id: str = "q-1") -> None:
    """Register an unanswered approval the way wait_for_permission does."""
    session_state.open_permission_wait(request_id, session_id)


class TestCodexReapCandidates:
    def test_over_idle_without_question_is_reaped(self, codex_registry):
        codex_registry["s-idle"] = _FakeCodexSession(idle=200)
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-idle"]

    def test_pending_question_spared_under_cap(self, codex_registry):
        # Blocked on a HUMAN: byte-idle but not abandoned.
        codex_registry["s-parked"] = _FakeCodexSession(idle=150)
        _park_question("s-parked")
        assert codex_session._codex_reap_candidates(1000.0, 100) == []

    def test_pending_question_spared_while_it_waits(self, codex_registry):
        # The prompt's own wait (PROMPT_WAIT_S) bounds the spare, not the
        # idle knob: a question parked overnight is still answerable.
        codex_registry["s-parked"] = _FakeCodexSession(idle=10 * 100 + 10)
        _park_question("s-parked")
        assert codex_session._codex_reap_candidates(1000.0, 100) == []

    def test_answered_question_does_not_spare(self, codex_registry):
        # The waiter is gone (answered/released) → nothing left to wait on.
        codex_registry["s-answered"] = _FakeCodexSession(idle=150)
        session_state._session_permission_requests["s-answered"] = {"q-gone"}
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-answered"]

    def test_dead_session_is_reaped_regardless_of_question(self, codex_registry):
        codex_registry["s-dead"] = _FakeCodexSession(idle=1, alive=False)
        _park_question("s-dead")
        assert codex_session._codex_reap_candidates(1000.0, 100) == ["s-dead"]


# ─────────────────────────────────────────────────────────────────────────
# question_response: fallback when no live waiter took the answer
# ─────────────────────────────────────────────────────────────────────────

class _FakeConnection(ClientMessageDispatcher, PromptController):
    """The dispatcher and prompt mixins with only the collaborators
    question_response uses."""

    def __init__(self, *, streaming: bool = False, chat_id: str = "chat-q"):
        self.session_id = "sid-reaped"
        self.chat_id = chat_id
        self.streaming = streaming
        self.sent: list[dict] = []
        self.chat_calls: list[dict] = []
        # What waits in a chat's queue for its next turn (the chat queue's
        # own behaviour is tests/session/test_input_queue.py's).
        self.queued: list[tuple[str, str]] = []

    async def _queue_for_next_turn(self, cid, item, queue_id=None) -> None:
        self.queued.append((cid, item.text))

    async def _may_resolve_permission(self, request_id: str) -> bool:
        return True

    async def _handle_chat(self, msg: dict) -> None:
        self.chat_calls.append(msg)

    async def _send(self, frame: dict) -> None:
        self.sent.append(frame)


def _answers(*values: str) -> dict:
    return {"q-id-verbatim": {"answers": list(values)}}


def _rid() -> str:
    # Each answer is claimed once across sockets (a late second answer is
    # dropped), so every test answers its own card.
    import uuid
    return f"q-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
class TestQuestionResponseFallback:
    async def test_the_answer_is_sent_as_a_chat_message(self):
        # The answer rides the composer path (dead-session branch → revival).
        conn = _FakeConnection()
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": _rid(),
            "answers": _answers("Ship it", "with tests"),
        })
        assert conn.chat_calls == [
            {"text": "Ship it, with tests", "chat_id": "chat-q"}
        ]

    async def test_while_streaming_it_queues_instead(self):
        conn = _FakeConnection(streaming=True)
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": _rid(),
            "answers": _answers("Option B"),
        })
        assert conn.chat_calls == []
        assert conn.queued == [("chat-q", "Option B")]

    async def test_no_answer_text_is_a_noop(self):
        conn = _FakeConnection()
        await conn._dispatch_client_message({
            "type": "question_response", "request_id": _rid(), "answers": {},
        })
        assert conn.chat_calls == []
        assert conn.queued == []

    async def test_two_sockets_answering_one_card_send_one_message(self):
        rid = _rid()
        first, second = _FakeConnection(), _FakeConnection()
        for conn in (first, second):
            await conn._dispatch_client_message({
                "type": "question_response", "request_id": rid,
                "answers": _answers("Option A"),
            })
        assert len(first.chat_calls) + len(second.chat_calls) == 1


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
