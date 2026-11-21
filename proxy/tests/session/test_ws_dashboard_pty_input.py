"""A composer message on a socket whose terminal has ended: the chat's live
terminal takes it, else the person is told (``ClientMessageDispatcher._on_pty_input``)."""

import asyncio

from tests.session.test_ws_dashboard_resume_controls import (
    _bare_connection,
    _FakeTerminal,
)


class TestAComposerSendToAnEndedTerminal:
    """The terminal this socket viewed has ended (the idle reaper, an
    eviction for room): a composer message goes to the chat's live terminal
    when there is one, else it is answered, never dropped without a word,
    and the exit frame makes the page start the terminal again at the next
    send. Raw keys stay quiet."""

    @staticmethod
    def _send(monkeypatch, live, *, composer):
        import base64

        from core.session import interactive_session
        monkeypatch.setattr(interactive_session, "get", lambda _sid: None)
        monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _cid: live)
        conn = _bare_connection("chat-ended", "sid-ended", user_sub="user-admin", agent="agent")
        asyncio.run(conn._on_pty_input({"data": base64.b64encode(b"hi").decode(), "composer": composer}))
        return conn.sent

    def test_the_person_is_told(self, temp_db, monkeypatch):
        sent = self._send(monkeypatch, None, composer=True)
        assert sent == [
            {"type": "pty_exit", "chat_id": "chat-ended", "session_id": "sid-ended", "reason": "ended"},
            {"type": "error",
             "message": "The terminal of this chat had stopped. Your message was not sent. Send it again."}]

    def test_a_newer_terminal_of_the_chat_takes_it(self, temp_db, monkeypatch):
        live = _FakeTerminal("chat-ended", "sid-newer")
        assert self._send(monkeypatch, live, composer=True) == []
        assert live.inputs == [b"hi"]

    def test_raw_keys_stay_quiet(self, temp_db, monkeypatch):
        assert self._send(monkeypatch, None, composer=False) == []
