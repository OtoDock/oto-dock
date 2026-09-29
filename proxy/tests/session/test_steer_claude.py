"""Native steering of a headless Claude turn: the user frame goes into the
live turn's stdin, is refused once the result was read or while a card is
parked, and a frame the turn did not consume becomes the next turn the
stream reads through."""

import asyncio
import json
import uuid

import pytest

from core.events.stream_pump import _pending_permissions
from core.layers.cli import session as cli_session
from core.layers.cli.layer import CLIExecutionLayer
from core.layers.cli.session import PersistentSession, _persistent_sessions


class _Stdin:
    def __init__(self):
        self.frames: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Proc:
    def __init__(self):
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = None
        self.returncode = None
        self.pid = 4242

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _session() -> PersistentSession:
    s = PersistentSession(
        session_id=f"sess-{uuid.uuid4().hex[:12]}", agent_prompt=None,
        mcp_config_path=None, model="claude-sonnet-5", agent_name="agent",
    )
    s._started = True
    s.proc = _Proc()
    return s


def _frame(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _init():
    return {"type": "system", "subtype": "init", "session_id": "cli-1", "mcp_servers": []}


def _assistant(text: str):
    return {"type": "assistant", "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": text}]}}


def _result(text: str):
    return {"type": "result", "subtype": "success", "result": text, "is_error": False,
            "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0.0, "session_id": "cli-1"}


class TestSessionSteer:
    @pytest.mark.asyncio
    async def test_writes_a_user_frame_only_into_a_live_turn(self):
        s = _session()
        assert await s.steer("go") is False          # no live turn
        s._turn_active = True
        assert await s.steer("") is False            # nothing to say
        assert await s.steer("go") is True
        assert s.proc.stdin.frames == [{"type": "user", "message": {"role": "user", "content": "go"}}]
        assert s._steer_written is True
        s._result_seen = True
        assert await s.steer("late") is False        # the turn's result was read
        s._result_seen = False
        s.proc.returncode = 1
        assert await s.steer("dead") is False


class TestLayerSteer:
    @pytest.fixture
    def pool(self):
        saved = dict(_persistent_sessions)
        _persistent_sessions.clear()
        yield _persistent_sessions
        _persistent_sessions.clear()
        _persistent_sessions.update(saved)

    @pytest.mark.asyncio
    async def test_refused_while_a_permission_card_is_parked(self, pool):
        s = _session()
        s._turn_active = True
        pool[s.session_id] = s
        layer = CLIExecutionLayer.__new__(CLIExecutionLayer)
        _pending_permissions[s.session_id] = {"request_id": "r1"}
        try:
            assert await layer.steer(s.session_id, "go") is False
        finally:
            _pending_permissions.pop(s.session_id, None)
        assert await layer.steer(s.session_id, "go") is True
        assert await layer.steer("no-such-session", "go") is False


async def _collect(session: PersistentSession, prompt: str) -> tuple[list[str], int]:
    texts: list[str] = []
    done = 0
    async for chunk in session.send_message(prompt):
        if chunk.is_done:
            done += 1
        elif getattr(chunk, "text", None):
            texts.append(chunk.text)
    return texts, done


class TestQueuedSteerTurn:
    @pytest.mark.asyncio
    async def test_an_unconsumed_steer_becomes_a_turn_the_stream_reads_through(self, monkeypatch):
        monkeypatch.setattr(cli_session, "_STEER_INIT_GRACE_S", 1.0)
        s = _session()
        out = s.proc.stdout

        async def feed():
            await asyncio.sleep(0.05)
            out.feed_data(_frame(_init()) + _frame(_assistant("first")))
            await asyncio.sleep(0.05)
            assert await s.steer("change of plan") is True   # before the result
            out.feed_data(_frame(_result("first")))
            # The CLI starts the queued turn by itself.
            await asyncio.sleep(0.05)
            out.feed_data(_frame(_init()) + _frame(_assistant("second")) + _frame(_result("second")))

        feeder = asyncio.create_task(feed())
        texts, done = await asyncio.wait_for(_collect(s, "hi"), timeout=10)
        await feeder
        assert done == 1
        assert "first" in "".join(texts) and "second" in "".join(texts)
        assert s._steer_written is False and s._turn_active is False
        assert s.proc.stdin.frames[0]["message"]["content"].endswith("hi")
        assert s.proc.stdin.frames[1]["message"]["content"] == "change of plan"

    @pytest.mark.asyncio
    async def test_a_consumed_steer_ends_the_turn_after_the_grace(self, monkeypatch):
        monkeypatch.setattr(cli_session, "_STEER_INIT_GRACE_S", 0.2)
        s = _session()
        out = s.proc.stdout

        async def feed():
            await asyncio.sleep(0.05)
            out.feed_data(_frame(_init()) + _frame(_assistant("first")))
            await asyncio.sleep(0.05)
            assert await s.steer("consumed mid-turn") is True
            out.feed_data(_frame(_result("first")))
            # Nothing follows: the CLI consumed the frame at a tool boundary.

        feeder = asyncio.create_task(feed())
        texts, done = await asyncio.wait_for(_collect(s, "hi"), timeout=10)
        await feeder
        assert done == 1 and "first" in "".join(texts)
        assert s._steer_written is False

    @pytest.mark.asyncio
    async def test_no_steer_means_no_grace_wait(self, monkeypatch):
        monkeypatch.setattr(cli_session, "_STEER_INIT_GRACE_S", 5.0)
        s = _session()
        out = s.proc.stdout

        async def feed():
            await asyncio.sleep(0.1)
            out.feed_data(_frame(_init()) + _frame(_assistant("only")) + _frame(_result("only")))

        feeder = asyncio.create_task(feed())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        texts, done = await asyncio.wait_for(_collect(s, "hi"), timeout=10)
        await feeder
        assert done == 1 and "only" in "".join(texts)
        assert loop.time() - t0 < 2.0
