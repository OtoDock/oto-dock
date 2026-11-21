"""A prompt that waits on a person must not cost them their answer.

Live on T1 (2026-08-06): a headless codex session blocked on a held server
request was reaped by the idle sweep (which only looked at
``last_activity``); answering afterwards resolved an event whose waiter was
gone, and the app-server answer write blew up on a closed transport
(``RuntimeError: … <WriteUnixTransport closed=True …>``) as an unhandled task
exception. The answer vanished and the turn never resumed.

Three seams pinned here:
  * the idle sweep spares a session with a pending prompt (an approval the
    bridge holds; a question holds nothing since 1.9.1) only while the prompt
    still waits and its machine is there (``remote_reaper._verdict``; the
    spare past the turn ceiling is ``tests/session/test_reaper_idle_timeout.py``'s);
  * a ``question_response`` (a held question card stored by 1.9.0) is the
    person's next message: the chat path (``_handle_chat`` → registry-aware
    single-flight revival), claimed once across sockets;
  * ``respond()`` on a closed transport logs and returns instead of raising.

No daemon / no DB.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/session/test_codex_question_survival.py -q
"""
from types import SimpleNamespace

import pytest

from core.layers.codex.app_server_client import AppServerClient, AppServerError
from core.remote import remote_reaper
from core.session import session_state
from ws.dashboard_dispatch import (
    ClientMessageDispatcher,
    _question_answers_to_text,
)
from ws.dashboard_prompts import PromptController

# ─────────────────────────────────────────────────────────────────────────
# The idle sweep: what a parked prompt spares
# ─────────────────────────────────────────────────────────────────────────

class _Machines:
    def __init__(self, connected: bool):
        self._connected = connected

    def is_connected(self, machine_id):
        return self._connected

    def is_session_in_grace(self, machine_id, session_id):
        return False


def _sweep(session_id: str, *, idle: float, connected: bool = True) -> str:
    """The reaper's verdict for a session between turns, ``idle`` seconds
    after its last event, under an idle timeout of 100 s."""
    info = SimpleNamespace(last_activity=1000.0 - idle, machine_id="m", turn_active=False,
                           cli_dead=False)
    return remote_reaper._verdict(
        SimpleNamespace(_cm=_Machines(connected)), session_id, info, 1000.0, 100)


@pytest.fixture
def prompts():
    """Isolate the prompt registries."""
    yield
    for rid in ("q-1", "q-x"):
        session_state._permission_events.pop(rid, None)
    session_state._session_permission_requests.clear()


def _park_question(session_id: str, request_id: str = "q-1") -> None:
    """Register an unanswered approval the way wait_for_permission does."""
    session_state.open_permission_wait(request_id, session_id)


class TestTheIdleSweepAndAParkedPrompt:
    def test_an_answered_prompt_spares_nothing(self, prompts):
        # The waiter is gone (answered/released) → nothing left to wait on.
        session_state._session_permission_requests["s-answered"] = {"q-gone"}
        assert _sweep("s-answered", idle=150) == remote_reaper.REAP
        _park_question("s-parked")
        assert _sweep("s-parked", idle=150) == remote_reaper.KEEP

    def test_a_session_whose_machine_is_gone_is_reaped_whatever_waits(self, prompts):
        # Nobody can answer a card of a session that runs nowhere.
        _park_question("s-gone")
        assert _sweep("s-gone", idle=150) == remote_reaper.KEEP
        assert _sweep("s-gone", idle=150, connected=False) == remote_reaper.REAP
        assert _sweep("s-gone", idle=1, connected=False) == remote_reaper.REAP


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
