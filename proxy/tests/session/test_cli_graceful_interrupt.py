"""Graceful interrupt for the persistent Claude CLI session.

Pins the N-series abort redesign: ``interrupt_turn`` is a fire-and-forget
stdin write of ``control_request {subtype:"interrupt"}`` into a LIVE turn
(never a stdout read — the turn's own loop is the sole reader);
``CLIExecutionLayer.abort`` prefers it over killpg and arms a watchdog that
falls back to killpg (and re-arms the cancelled-context injection) when the
turn doesn't close; the #63943 wedge detector kills the process on the
thinking-signature 400 that can follow a mid-thinking interrupt.
"""

import asyncio
import json
import uuid

import pytest

from core.layers.cli import layer as cli_layer_mod
from core.layers.cli import session as cli_session_mod
from core.layers.cli.layer import CLIExecutionLayer
from core.layers.cli.session import PersistentSession, _persistent_sessions


class _FakeStdin:
    def __init__(self, *, broken: bool = False):
        self.lines: list[bytes] = []
        self.broken = broken

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError("pipe closed")
        self.lines.append(data)

    async def drain(self) -> None:
        pass


class _FakeStdout:
    """readline() feeds queued lines, then blocks until more are fed."""

    def __init__(self):
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, obj: dict) -> None:
        self._q.put_nowait((json.dumps(obj) + "\n").encode())

    async def readline(self) -> bytes:
        return await self._q.get()


class _FakeProc:
    def __init__(self, *, broken_stdin: bool = False):
        self.stdin = _FakeStdin(broken=broken_stdin)
        self.stdout = _FakeStdout()
        self.stderr = None
        self.returncode: int | None = None
        self.pid = 4242


def _mk_session(sid: str | None = None, *, broken_stdin: bool = False) -> PersistentSession:
    s = PersistentSession(
        session_id=sid or f"sess-{uuid.uuid4().hex[:12]}",
        agent_prompt=None,
        mcp_config_path=None,
        model="claude-opus-5",
        agent_name="agent",
    )
    s.proc = _FakeProc(broken_stdin=broken_stdin)
    s._started = True
    return s


def _sent_frames(session: PersistentSession) -> list[dict]:
    return [json.loads(line) for line in session.proc.stdin.lines]


async def _drive_result(session: PersistentSession, result: dict) -> list:
    """Run one send_message drive whose stdout yields ``result``.

    The result is fed AFTER the stale-output drain window (0.05s probe) so
    the drain can't consume it as a previous turn's tail.
    """
    async def feeder():
        await asyncio.sleep(0.15)
        session.proc.stdout.feed(result)

    feed_task = asyncio.create_task(feeder())
    chunks = []
    async for chunk in session.send_message("follow-up"):
        chunks.append(chunk)
    await feed_task
    await asyncio.sleep(0.1)  # let the detector's kill task run
    return chunks


@pytest.fixture(autouse=True)
def _clean_pool():
    _persistent_sessions.clear()
    yield
    _persistent_sessions.clear()


@pytest.fixture
def _no_foreign(monkeypatch):
    """Zero-content scripted results must close the turn, not re-arm as a
    resume-handshake foreign result (60s silence valve)."""
    monkeypatch.setattr(cli_session_mod, "is_foreign_result", lambda *a: False)


# ---------------------------------------------------------------------------
# interrupt_turn — the stdin frame.
# ---------------------------------------------------------------------------

class TestInterruptTurn:
    @pytest.mark.asyncio
    async def test_writes_interrupt_frame_into_live_turn(self):
        s = _mk_session()
        s._turn_active = True
        assert await s.interrupt_turn() is True
        frames = _sent_frames(s)
        assert len(frames) == 1
        assert frames[0]["type"] == "control_request"
        assert frames[0]["request"] == {"subtype": "interrupt"}
        assert frames[0]["request_id"]
        assert s._post_interrupt_watch is True

    @pytest.mark.asyncio
    async def test_refuses_between_turns(self):
        s = _mk_session()
        assert s._turn_active is False
        assert await s.interrupt_turn() is False
        assert s.proc.stdin.lines == []
        assert s._post_interrupt_watch is False

    @pytest.mark.asyncio
    async def test_refuses_on_dead_pipe(self):
        s = _mk_session(broken_stdin=True)
        s._turn_active = True
        assert await s.interrupt_turn() is False
        assert s._post_interrupt_watch is False

    @pytest.mark.asyncio
    async def test_refuses_on_dead_process(self):
        s = _mk_session()
        s._turn_active = True
        s.proc.returncode = 1
        assert await s.interrupt_turn() is False


# ---------------------------------------------------------------------------
# CLIExecutionLayer.abort — graceful-first with killpg fallback.
# ---------------------------------------------------------------------------

class TestLayerAbort:
    @pytest.mark.asyncio
    async def test_graceful_path_skips_killpg_and_releases_permissions(self, monkeypatch):
        s = _mk_session()
        s._turn_active = True
        _persistent_sessions[s.session_id] = s

        killed: list[str] = []
        released: list[tuple[str, bool]] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        monkeypatch.setattr(
            cli_layer_mod, "resolve_session_permissions",
            lambda sid, approved: released.append((sid, approved)),
        )
        monkeypatch.setattr(cli_layer_mod, "_INTERRUPT_WATCHDOG_S", 0.4)

        graceful = await CLIExecutionLayer().abort(s.session_id)
        assert graceful is True
        assert killed == []
        assert released == [(s.session_id, False)]
        assert _sent_frames(s)[0]["request"] == {"subtype": "interrupt"}

        # Turn closes before the deadline → watchdog exits without killing.
        s._turn_active = False
        await asyncio.sleep(0.7)
        assert killed == []

    @pytest.mark.asyncio
    async def test_hard_path_when_no_turn_active(self, monkeypatch):
        s = _mk_session()
        _persistent_sessions[s.session_id] = s
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        graceful = await CLIExecutionLayer().abort(s.session_id)
        assert graceful is False
        assert killed == [s.session_id]

    @pytest.mark.asyncio
    async def test_watchdog_falls_back_to_killpg_and_rearms_injection(
        self, temp_db, monkeypatch,
    ):
        s = _mk_session()
        s._turn_active = True
        _persistent_sessions[s.session_id] = s

        cid = str(uuid.uuid4())
        temp_db.create_chat(cid, "user-admin", "agent", "default")
        temp_db.update_chat(cid, session_id=s.session_id,
                            last_turn_aborted=True, last_abort_graceful=True)

        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        monkeypatch.setattr(
            cli_layer_mod, "resolve_session_permissions", lambda *a, **k: None,
        )
        monkeypatch.setattr(cli_layer_mod, "_INTERRUPT_WATCHDOG_S", 0.3)

        assert await CLIExecutionLayer().abort(s.session_id) is True
        # The turn never closes (wedged pipe / skipped foreign result).
        await asyncio.sleep(0.8)
        assert killed == [s.session_id]
        chat = temp_db.get_chat(cid)
        assert chat["last_turn_aborted"] is True
        assert chat["last_abort_graceful"] is False

    @pytest.mark.asyncio
    async def test_watchdog_never_kills_a_successor_turn(self, monkeypatch):
        s = _mk_session()
        s._turn_active = True
        _persistent_sessions[s.session_id] = s
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        monkeypatch.setattr(
            cli_layer_mod, "resolve_session_permissions", lambda *a, **k: None,
        )
        monkeypatch.setattr(cli_layer_mod, "_INTERRUPT_WATCHDOG_S", 0.3)

        assert await CLIExecutionLayer().abort(s.session_id) is True
        # Interrupted turn closed and a NEW turn opened before the deadline.
        s._turn_seq += 1
        await asyncio.sleep(0.8)
        assert killed == []


# ---------------------------------------------------------------------------
# The #63943 wedge detector — thinking-signature 400 after an interrupt.
# ---------------------------------------------------------------------------

class TestWedgeDetector:
    @pytest.mark.asyncio
    async def test_signature_error_after_interrupt_kills_and_rearms(
        self, temp_db, monkeypatch,
    ):
        s = _mk_session()
        s._post_interrupt_watch = True
        cid = str(uuid.uuid4())
        temp_db.create_chat(cid, "user-admin", "agent", "default")
        temp_db.update_chat(cid, session_id=s.session_id)

        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
        await _drive_result(s, {
            "type": "result", "subtype": "error_during_execution",
            "is_error": True,
            "result": "API Error: 400 invalid `thinking` block: missing signature",
        })
        assert killed == [s.session_id]
        chat = temp_db.get_chat(cid)
        assert chat["last_turn_aborted"] is True
        assert chat["last_abort_graceful"] is False
        assert s._post_interrupt_watch is False

    @pytest.mark.asyncio
    async def test_clean_result_disarms_watch(self, _no_foreign, monkeypatch):
        s = _mk_session()
        s._post_interrupt_watch = True
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
        await _drive_result(s, {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "all good",
        })
        assert killed == []
        assert s._post_interrupt_watch is False

    @pytest.mark.asyncio
    async def test_unrelated_error_keeps_watch_armed(self, monkeypatch):
        s = _mk_session()
        s._post_interrupt_watch = True
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
        await _drive_result(s, {
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "Request was aborted",
        })
        assert killed == []
        assert s._post_interrupt_watch is True


# ---------------------------------------------------------------------------
# Turn-active span + stray control_response drain.
# ---------------------------------------------------------------------------

class TestTurnSpan:
    @pytest.mark.asyncio
    async def test_turn_active_spans_send_message(self, _no_foreign):
        s = _mk_session()
        assert s._turn_active is False

        async def feeder():
            await asyncio.sleep(0.15)
            assert s._turn_active is True  # live mid-drive
            s.proc.stdout.feed({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "ok",
            })

        feed = asyncio.create_task(feeder())
        done = [c.is_done async for c in s.send_message("hi")]
        await feed
        assert any(done)
        assert s._turn_active is False
        assert s._turn_seq == 1

    @pytest.mark.asyncio
    async def test_stray_control_response_between_turns_is_drained(self, _no_foreign):
        """An interrupt that raced turn end leaves a control_response in the
        pipe; the next turn's stale-output drain must swallow it silently
        (the drain stops at the previous turn's trailing result)."""
        s = _mk_session()
        s.proc.stdout.feed({
            "type": "control_response",
            "response": {"request_id": "stray", "subtype": "success"},
        })
        s.proc.stdout.feed({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "previous turn tail",
        })
        s.proc.stdout.feed({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "ok",
        })
        done = []
        async for chunk in s.send_message("hi"):
            if chunk.is_done:
                done.append(True)
        assert done == [True]


# ---------------------------------------------------------------------------
# CLIExecutionLayer.interrupt_for_queued — stop-and-send: graceful-ONLY.
# ---------------------------------------------------------------------------

class TestInterruptForQueued:
    @pytest.mark.asyncio
    async def test_live_turn_interrupts_and_releases_permissions(self, monkeypatch):
        s = _mk_session()
        s._turn_active = True
        _persistent_sessions[s.session_id] = s

        killed: list[str] = []
        released: list[tuple[str, bool]] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        monkeypatch.setattr(
            cli_layer_mod, "resolve_session_permissions",
            lambda sid, approved: released.append((sid, approved)),
        )

        assert await CLIExecutionLayer().interrupt_for_queued(s.session_id) is True
        assert _sent_frames(s)[0]["request"] == {"subtype": "interrupt"}
        assert released == [(s.session_id, False)]
        # Graceful-only: no killpg, no watchdog — even if the turn never
        # closes, nothing escalates.
        await asyncio.sleep(0.3)
        assert killed == []
        assert s.proc.returncode is None

    @pytest.mark.asyncio
    async def test_no_live_turn_is_a_pure_noop(self, monkeypatch):
        """The critical difference from abort(): no turn → False and NOTHING
        else — no killpg fallback, no permission release, process alive."""
        s = _mk_session()
        _persistent_sessions[s.session_id] = s
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)
        released: list = []
        monkeypatch.setattr(
            cli_layer_mod, "resolve_session_permissions",
            lambda sid, approved: released.append(sid),
        )

        assert await CLIExecutionLayer().interrupt_for_queued(s.session_id) is False
        assert killed == []
        assert released == []
        assert s.proc.stdin.lines == []
        assert s.proc.returncode is None

    @pytest.mark.asyncio
    async def test_unknown_session_and_dead_pipe_return_false(self, monkeypatch):
        killed: list[str] = []

        async def fake_kill(sid):
            killed.append(sid)
            return True

        monkeypatch.setattr(cli_layer_mod, "interrupt_persistent_session", fake_kill)

        assert await CLIExecutionLayer().interrupt_for_queued("no-such") is False

        s = _mk_session(broken_stdin=True)
        s._turn_active = True
        _persistent_sessions[s.session_id] = s
        assert await CLIExecutionLayer().interrupt_for_queued(s.session_id) is False
        assert killed == []
