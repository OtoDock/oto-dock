"""Remote sessions at a graceful shutdown.

uvicorn closes the satellite sockets before the lifespan's shutdown runs, so
a close sent then never reaches the satellite and only dropped the session's
security context: the satellite kept the process, the next proxy re-adopted
it with no context, and every hook was refused. A session whose engine is
re-adopted after a restart is now left open, idle or mid-turn; a turn the
shutdown leaves running is marked for its replay.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import startup
from core.session import session_state


class _Remote:
    def __init__(self, sessions: dict):
        self._sessions = sessions
        self.closed: list[str] = []

    def local_session_ids(self):
        return list(self._sessions)

    async def close_session(self, sid):
        self.closed.append(sid)
        self._sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_re_adoptable_sessions_stay_open_others_close():
    """Claude's sessions and Codex's (an idle one is taken back by the
    satellite's report; one mid-turn is closed by it) stay open with their
    contexts; an engine that takes nothing back closes as before."""
    remote = _Remote({
        "claude-idle": SimpleNamespace(execution_path="claude-code-cli", turn_active=False),
        "claude-turn": SimpleNamespace(execution_path="claude-code-cli", turn_active=True),
        "codex-idle": SimpleNamespace(execution_path="codex-cli", turn_active=False),
        "codex-turn": SimpleNamespace(execution_path="codex-cli", turn_active=True),
        "acme": SimpleNamespace(execution_path="acme-cli", turn_active=False),
    })
    await startup._shutdown_remote_sessions(remote, logging.getLogger("test"))
    assert remote.closed == ["acme"]


@pytest.mark.asyncio
async def test_a_recoverable_pump_is_marked_for_its_replay(monkeypatch):
    from core.events import stream_pump
    from services.scheduler import run_recovery
    aborted: list[str] = []

    class Pump:
        def __init__(self, sid, source_type="dashboard"):
            self.session_id = sid
            self.source_type = source_type
            self._task = None

        def abort(self):
            aborted.append(self.session_id)

    monkeypatch.setattr(stream_pump, "_active_pumps", {
        "chat-remote": Pump("sess-remote"), "chat-local": Pump("sess-local"),
        "chat-run": Pump("sess-run", "task")})
    monkeypatch.setattr(startup, "_active_pumps", stream_pump._active_pumps, raising=False)
    monkeypatch.setattr(run_recovery, "is_recovery_eligible",
                        lambda cid: cid in ("chat-remote", "chat-run"))
    try:
        await startup._flush_active_pumps(logging.getLogger("test"))
        assert aborted == ["sess-local"]
        assert "chat-remote" in stream_pump._recovery_suppress_flush
        assert session_state.take_recover_pending("sess-remote") is True
        assert session_state.take_recover_pending("sess-local") is False
        # A run is recovered by its run id: suppressed, never marked.
        assert "chat-run" in stream_pump._recovery_suppress_flush
        assert session_state.take_recover_pending("sess-run") is False
    finally:
        stream_pump._recovery_suppress_flush.discard("chat-remote")
        stream_pump._recovery_suppress_flush.discard("chat-run")
        session_state._sessions.pop("sess-remote", None)
