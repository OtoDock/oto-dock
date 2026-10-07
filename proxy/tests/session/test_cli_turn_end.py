"""Every way a headless Claude turn ends other than by its own result is a
typed ending: the engine's error result (the process stays), its exit before
a result, and silence past the ceiling with nothing keeping the turn (the
process is ended). The stream's life is every stdout line, and a steer is
refused once the silence has passed its ceiling."""

import asyncio
import json
import time
import uuid

import pytest

import config
from core.events import turn_ending
from core.events.common_events import DONE, ERROR
from core.layers.cli import session as cli_session_mod
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.session import PersistentSession
from core.session import session_state


class _Stdin:
    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass


class _Stdout:
    """readline() feeds queued lines, then blocks until more are fed or EOF."""

    def __init__(self):
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, obj: dict) -> None:
        self._q.put_nowait((json.dumps(obj) + "\n").encode())

    def eof(self) -> None:
        self._q.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._q.get()


class _Stderr:
    def __init__(self, text: str = ""):
        self._text = text.encode()

    async def read(self) -> bytes:
        return self._text


class _Proc:
    def __init__(self, stderr: str = ""):
        self.stdin = _Stdin()
        self.stdout = _Stdout()
        self.stderr = _Stderr(stderr)
        self.returncode: int | None = None
        self.pid = 4242


def _session(stderr: str = "") -> PersistentSession:
    s = PersistentSession(
        session_id=f"sess-{uuid.uuid4().hex[:12]}", agent_prompt=None,
        mcp_config_path=None, model="claude-opus-5", agent_name="agent",
    )
    s.proc = _Proc(stderr)
    s._started = True
    return s


@pytest.fixture
def _no_foreign(monkeypatch):
    monkeypatch.setattr(cli_session_mod, "is_foreign_result", lambda *a: False)


@pytest.fixture
def _no_drain(monkeypatch):
    async def _skip(self):
        return None
    monkeypatch.setattr(PersistentSession, "_drain_stale_output", _skip)


def _synthetic_error(text: str) -> dict:
    return {"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def _error_result(text: str) -> dict:
    return {"type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": text, "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0.0}


async def _drive(session: PersistentSession, *, timeout: float = 5.0):
    chunks = []
    async for chunk in session.send_message("hello"):
        chunks.append(chunk)
    return chunks


def _events(chunks):
    return [e for c in chunks for e in cli_chunk_to_events(c)]


_ECONNRESET = "API Error: Connection dropped (ECONNRESET)"


@pytest.mark.asyncio
async def test_the_error_result_is_a_typed_error_ending_and_the_process_stays(
        _no_foreign, _no_drain, monkeypatch):
    s = _session()
    killed: list[str] = []

    async def fake_kill(sid):
        killed.append(sid)
        return True
    monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
    s.proc.stdout.feed(_synthetic_error(_ECONNRESET))
    s.proc.stdout.feed(_error_result(_ECONNRESET))
    events = _events(await asyncio.wait_for(_drive(s), 5))
    assert events[0].type == ERROR and events[-1].type == DONE
    ending = turn_ending.from_dict(events[0].data["ending"])
    assert ending.reason == turn_ending.ERROR and ending.detail == _ECONNRESET
    assert events[0].data["message"] == ending.line()
    assert killed == [] and s.proc.returncode is None


@pytest.mark.asyncio
async def test_an_exit_before_the_result_is_an_exited_ending(_no_foreign, _no_drain):
    s = _session(stderr="boom\nout of memory")
    s.proc.stdout.feed({"type": "system", "subtype": "init", "session_id": "cli-1"})

    async def _die():
        await asyncio.sleep(0.05)
        s.proc.returncode = 137
        s.proc.stdout.eof()
    dying = asyncio.create_task(_die())
    events = _events(await asyncio.wait_for(_drive(s), 5))
    await dying
    assert [e.type for e in events] == [ERROR, DONE]
    ending = turn_ending.from_dict(events[0].data["ending"])
    assert ending.reason == turn_ending.EXITED and ending.exit_code == 137
    assert ending.detail == "exit 137: out of memory"


@pytest.mark.asyncio
async def test_a_silent_process_is_ended_with_the_silent_ending(
        _no_foreign, _no_drain, monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.3)
    s = _session()
    killed: list[str] = []

    async def fake_kill(sid):
        killed.append(sid)
        s.proc.returncode = -9
        return True
    monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
    # The readline timeout is the heartbeat slice; shrink it for the test.
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    s.proc.stdout.feed({"type": "system", "subtype": "init", "session_id": "cli-1"})
    events = _events(await asyncio.wait_for(_drive(s), 5))
    assert [e.type for e in events] == [ERROR, DONE]
    ending = turn_ending.from_dict(events[0].data["ending"])
    assert ending.reason == turn_ending.SILENT
    assert killed == [s.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("life", ("tool", "hook", "prompt", "background"))
async def test_a_silent_turn_with_life_is_kept(_no_foreign, _no_drain, monkeypatch, life):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 0.2)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    from core.layers.cli import settle as settle_mod
    monkeypatch.setattr(settle_mod.SettleController, "effective_timeout", lambda self: 0.1)
    s = _session()
    killed: list[str] = []

    async def fake_kill(sid):
        killed.append(sid)
        return True
    monkeypatch.setattr(cli_session_mod, "interrupt_persistent_session", fake_kill)
    if life == "hook":
        monkeypatch.setattr(session_state, "get_hook_activity",
                            lambda sid: time.monotonic())
    elif life == "prompt":
        monkeypatch.setattr(session_state, "has_pending_prompt", lambda sid: True)
    elif life == "background":
        from core.events.bg_command_state import get_bg_command_registry
        get_bg_command_registry(s.session_id).register_spawn("task-1", "tool-1", label="x")
    out = s.proc.stdout
    out.feed({"type": "system", "subtype": "init", "session_id": "cli-1"})
    if life == "tool":
        out.feed({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                  "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash"}}})

    async def _late_result():
        await asyncio.sleep(0.8)   # well past the ceiling, the turn still runs
        if life == "tool":
            out.feed({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})
        out.feed({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "duration_ms": 1, "num_turns": 1,
                  "total_cost_usd": 0.0})
    feeder = asyncio.create_task(_late_result())
    events = _events(await asyncio.wait_for(_drive(s), 5))
    await feeder
    assert killed == []
    assert ERROR not in [e.type for e in events] and events[-1].type == DONE


@pytest.mark.asyncio
async def test_every_stdout_line_moves_last_activity(_no_foreign, _no_drain):
    s = _session()
    out = s.proc.stdout
    before = time.monotonic() - 100.0
    s.last_activity = before

    async def feed():
        await asyncio.sleep(0.05)
        out.feed({"type": "stream_event", "event": {"type": "message_start",
                  "message": {"usage": {}}}})
        await asyncio.sleep(0.05)
        assert s.last_activity > before
        assert s._last_line_at > before
        out.feed({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "duration_ms": 1, "num_turns": 1,
                  "total_cost_usd": 0.0})
    feeder = asyncio.create_task(feed())
    await asyncio.wait_for(_drive(s), 5)
    await feeder


@pytest.mark.asyncio
async def test_a_steer_is_refused_once_the_silence_passed_its_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    s = _session()
    s._turn_active = True
    s._last_line_at = time.monotonic() - 10.0
    assert await s.steer("go") is True
    s._last_line_at = time.monotonic() - 61.0
    assert await s.steer("again") is False
    assert len(s.proc.stdin.lines) == 1


def test_session_idle_seconds_reads_the_pooled_sessions_last_activity():
    from core.layers.cli.layer import CLIExecutionLayer
    from core.layers.cli.session import _persistent_sessions
    layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
    s = _session()
    s.last_activity = time.monotonic() - 42.0
    _persistent_sessions[s.session_id] = s
    try:
        assert 41.0 < layer.session_idle_seconds(s.session_id) < 50.0
    finally:
        _persistent_sessions.pop(s.session_id, None)
    assert layer.session_idle_seconds("no-such") is None


@pytest.mark.asyncio
async def test_the_proxys_own_kill_ends_the_turn_with_no_ending(_no_foreign, _no_drain):
    """A Stop's kill (or the watchdog's) is the proxy's own: the end of
    output after it is no ``exited`` card and no Send again."""
    s = _session(stderr="Terminated")
    s.proc.stdout.feed({"type": "system", "subtype": "init", "session_id": "cli-1"})

    async def _stop():
        await asyncio.sleep(0.05)
        s._proxy_killed = True
        s.proc.returncode = -15
        s.proc.stdout.eof()
    stopping = asyncio.create_task(_stop())
    events = _events(await asyncio.wait_for(_drive(s), 5))
    await stopping
    assert [e.type for e in events] == [DONE]


@pytest.mark.asyncio
async def test_interrupt_marks_the_kill_as_the_proxys_own(monkeypatch):
    from core.layers.cli.session import _persistent_sessions, interrupt_persistent_session
    s = _session()

    async def fake_kill(proc, sid):
        proc.returncode = -15
    monkeypatch.setattr(cli_session_mod, "_kill_process", fake_kill)
    _persistent_sessions[s.session_id] = s
    try:
        assert await interrupt_persistent_session(s.session_id) is True
        assert s._proxy_killed is True
    finally:
        _persistent_sessions.pop(s.session_id, None)


def test_a_task_turn_keeps_its_silence_to_the_turn_ceiling(monkeypatch):
    monkeypatch.setattr(config, "TURN_SILENCE_S", 60)
    monkeypatch.setattr(config, "CLAUDE_TIMEOUT", 7200)
    s = _session()
    s._turn_active = True
    s._last_line_at = time.monotonic() - 600.0
    assert s._silent_past_ceiling() is True
    s._task_turn = True
    assert s._silent_past_ceiling() is False


@pytest.mark.asyncio
async def test_the_steer_is_marked_written_before_the_write_lands(monkeypatch):
    """A result read while the steer's drain awaits must already know a
    frame is on its way, or the turn it starts runs unread."""
    s = _session()
    s._turn_active = True
    seen: list[bool] = []

    async def drain():
        seen.append(s._steer_written)
    s.proc.stdin.drain = drain
    assert await s.steer("go") is True
    assert seen == [True] and s._steer_written is True
