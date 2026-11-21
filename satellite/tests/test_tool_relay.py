"""The relay of Direct LLM's tool processes on a compute machine.

Load-bearing: a tool runs in the session's rendered sandbox or not at all; a
frame for a child this machine does not run, or with another key, touches
nothing; lines keep their order both ways; a line too large for the link
answers its call and leaves the link standing; a close ends the child; the
link's drop ends every child; the environment never reaches a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from satellite._vendored import sandbox_render as render
from satellite.config import SatelliteConfig
from satellite.sessions import sandbox_host, tool_relay
from satellite.sessions.tool_relay import ToolRelay, oversize_answer

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix processes")

SESSION, SERVER, KEY = "s" * 36, "memory-mcp", "k" * 32
# A tool that answers each stdin line with that line, upper-cased, and ends
# when its stdin does.
ECHO = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write(line.upper()); sys.stdout.flush()\n"
)


class _WS:
    def __init__(self, *, connected: bool = True):
        self.control: list[dict] = []
        self.bulk: list[dict] = []
        self.connected = connected

    async def enqueue_send(self, msg):
        self.control.append(msg)

    async def enqueue_bulk(self, msg):
        if not self.connected:
            return False
        self.bulk.append(msg)
        return True

    def ack(self) -> dict:
        acks = [m for m in self.control if m.get("type") == "ack"]
        assert len(acks) == 1, self.control
        return acks[0]

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.bulk if m.get("type") == kind]


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    agents = tmp_path / "platform" / "agents"
    (agents / "pa" / "workspace").mkdir(parents=True)
    mcps = tmp_path / "platform" / "mcps"
    (mcps / "custom" / "memory-mcp").mkdir(parents=True)
    return agents, mcps


@pytest.fixture
def compute(tmp_path, monkeypatch) -> SatelliteConfig:
    agents, mcps = _tree(tmp_path)
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: tmp_path / "machine" / ".oto-dock")
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    monkeypatch.setattr(render, "mem_total_mb", lambda: 16000)
    monkeypatch.setattr(render, "host_resolv_has_loopback_ns", lambda *_a: False)
    return SatelliteConfig(machine_id="c-1", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                           agents_dir=agents, mcps_dir=mcps, compute=True)


def _description(mcps: Path) -> dict:
    return render.SandboxDescription(
        agent="pa", binds=(render.Bind(render.ROOT_AGENT, "workspace", "/workspace", True),),
        mcp_binds=("custom/memory-mcp",), mcps_root=str(mcps), extra_binds=(),
        cwd="/workspace", proxy_port=8400, forwards=(render.PROXY_FORWARD,),
        allow_hosts=(), tmp_size_mb=0,
    ).to_dict()


def _open_frame(compute, **over) -> dict:
    return {"type": "tool_open", "command_id": "c1", "session_id": SESSION, "server": SERVER, "key": KEY,
            "sandbox": _description(compute.mcps_dir), "argv": ["/venv/bin/python", "server.py"],
            "env": {"PATH": "/usr/bin", "SECRET_VALUE": "hunter2-secret"}, "tunnel_port": 43210, **over}


def _frame(kind: str, **over) -> dict:
    return {"type": kind, "session_id": SESSION, "server": SERVER, "key": KEY, **over}


def _bare_child(relay: ToolRelay, script: str = ECHO) -> None:
    """The render replaced by a plain interpreter: the tool's pipes and
    frames under test, not the sandbox (the render has its own test)."""
    relay._command = lambda _msg, _sandbox, _argv: [sys.executable, "-c", script]  # type: ignore[method-assign]


async def _until(check, timeout: float = 10.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.01)


# -- what an open may start ------------------------------------------------


def test_a_paired_machine_refuses_the_open(compute):
    async def run():
        compute.compute = False
        relay, ws = ToolRelay(compute), _WS()
        await relay.open(_open_frame(compute), ws)
        assert ws.ack()["status"] == "error"
        assert len(relay) == 0
    asyncio.run(run())


@pytest.mark.parametrize("over", [
    {"sandbox": None}, {"sandbox": "render it yourself"},
    {"argv": []}, {"argv": "python server.py"}, {"env": None},
    {"session_id": ""}, {"server": ""}, {"key": ""},
])
def test_an_open_without_its_parts_spawns_nothing(compute, monkeypatch, over):
    async def run():
        async def _no_spawn(*_a, **_k):
            raise AssertionError("a process was spawned")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)
        relay, ws = ToolRelay(compute), _WS()
        await relay.open(_open_frame(compute, **over), ws)
        assert ws.ack()["status"] == "error"
        assert len(relay) == 0
    asyncio.run(run())


def test_an_open_of_a_tool_this_machine_has_not_built_is_answered_with_the_sentence(compute, monkeypatch):
    """The platform names a community tool's own folder. One that is not
    here yet is answered, never spawned. A folder outside this machine's
    tools is not a tool's."""
    async def run():
        async def _no_spawn(*_a, **_k):
            raise AssertionError("a process was spawned")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)
        relay, ws = ToolRelay(compute), _WS()
        missing = compute.mcps_dir / "community" / "not-built-yet"
        await relay.open(_open_frame(compute, tool_dir=str(missing)), ws)
        assert ws.ack() == {"type": "ack", "command_id": "c1", "status": "error",
                            "error": "This tool is being installed. Try again in a minute."}
        for outside in (str(compute.agents_dir), str(compute.mcps_dir), "community/x", "", 7,
                        str(compute.mcps_dir / "community" / ".." / ".." / "agents")):
            ws = _WS()
            await relay.open(_open_frame(compute, tool_dir=outside), ws)
            assert ws.ack()["error"] == "the open's tool folder is malformed", outside
        assert len(relay) == 0
    asyncio.run(run())


def test_an_open_that_names_a_built_tools_folder_starts_it(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay)
        await relay.open(_open_frame(compute, tool_dir=str(compute.mcps_dir / "custom" / "memory-mcp")), ws)
        assert ws.ack()["status"] == "ok" and len(relay) == 1
        relay.close_all("test end")
    asyncio.run(run())


def test_the_tool_runs_inside_the_rendered_description(compute):
    relay = ToolRelay(compute)
    msg = _open_frame(compute)
    command = relay._command(msg, msg["sandbox"], msg["argv"])
    launcher = str(sandbox_host.launcher_path())
    assert launcher in command and "bwrap" in command
    assert command.index(launcher) < command.index("bwrap")
    # The tool is the last thing in the command, after bubblewrap's own separator.
    assert command[-3:] == ["--", "/venv/bin/python", "server.py"]
    # The proxy port is spliced onto this machine's link.
    assert "8400:43210" in command
    # The tool folders come from this machine's own root, read-only.
    src = str(compute.mcps_dir / "custom" / "memory-mcp")
    assert any(command[i:i + 2] == ["--ro-bind", src] for i in range(len(command) - 1))


def test_a_refused_render_answers_the_ack_and_keeps_no_slot(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        # No link port: the render would splice the proxy port onto this host's own.
        await relay.open(_open_frame(compute, tunnel_port=0), ws)
        assert ws.ack()["status"] == "error"
        assert len(relay) == 0
    asyncio.run(run())


def test_a_second_open_of_the_same_tool_is_refused(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay)
        await relay.open(_open_frame(compute), ws)
        second = _WS()
        await relay.open(_open_frame(compute, command_id="c2"), second)
        assert second.ack()["status"] == "error"
        assert len(relay) == 1
        relay.close_all("test end")
    asyncio.run(run())


# -- the lines ----------------------------------------------------------------


def test_lines_cross_in_order_both_ways(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay)
        await relay.open(_open_frame(compute), ws)
        assert ws.ack()["status"] == "ok"
        lines = [f'{{"n":{i}}}' for i in range(200)]
        # Spawned tasks in arrival order, as the link dispatches them.
        tasks = [asyncio.ensure_future(relay.take_in(_frame("tool_in", line=line), ws)) for line in lines]
        await _until(lambda: len(ws.of("tool_out")) == 200)
        await asyncio.gather(*tasks)
        assert [m["line"] for m in ws.of("tool_out")] == [line.upper() for line in lines]
        assert {(m["session_id"], m["server"], m["key"]) for m in ws.of("tool_out")} == {(SESSION, SERVER, KEY)}
        relay.close_all("test end")
    asyncio.run(run())


@pytest.mark.parametrize("over", [{"key": "z" * 32}, {"session_id": "other"}, {"server": "other"}])
@pytest.mark.parametrize("kind", ["tool_in", "tool_close"])
def test_a_frame_for_another_child_or_key_touches_nothing(compute, kind, over):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay)
        await relay.open(_open_frame(compute), ws)
        refused = _WS()
        handler = relay.take_in if kind == "tool_in" else relay.close
        await handler(_frame(kind, line='{"bad":1}', **over), refused)
        assert refused.of("tool_exit") and refused.of("tool_exit")[0]["reason"] == "not running"
        # The right child still runs and heard nothing of it.
        assert len(relay) == 1
        await relay.take_in(_frame("tool_in", line='{"good":1}'), ws)
        await _until(lambda: ws.of("tool_out"))
        assert [m["line"] for m in ws.of("tool_out")] == ['{"GOOD":1}']
        relay.close_all("test end")
    asyncio.run(run())


# -- the end of a child ----------------------------------------------------


def test_a_close_ends_the_child_with_its_exit_frame(compute, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "_CLOSE_GRACE_S", 0.05)
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay)
        await relay.open(_open_frame(compute), ws)
        await relay.close(_frame("tool_close"), ws)
        await _until(lambda: ws.of("tool_exit"))
        exit_frame = ws.of("tool_exit")[0]
        assert exit_frame["key"] == KEY and exit_frame["code"] == 0 and exit_frame["reason"] == ""
        assert len(relay) == 0
    asyncio.run(run())


def test_a_child_that_ignores_its_stdin_is_told_to_end(compute, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "_CLOSE_GRACE_S", 0.05)
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay, "import time\ntime.sleep(60)\n")
        await relay.open(_open_frame(compute), ws)
        await relay.close(_frame("tool_close"), ws)
        await _until(lambda: ws.of("tool_exit"))
        assert ws.of("tool_exit")[0]["code"] not in (0, None)
    asyncio.run(run())


def test_a_close_during_the_open_closes_the_child(compute, monkeypatch):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay, "import time\ntime.sleep(60)\n")
        real_spawn = asyncio.create_subprocess_exec

        async def _slow(*a, **k):
            # The close lands while the spawn is in flight.
            await relay.close(_frame("tool_close"), ws)
            return await real_spawn(*a, **k)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _slow)
        await relay.open(_open_frame(compute), ws)
        assert ws.ack()["status"] == "error"
        await _until(lambda: ws.of("tool_exit"))
        assert len(relay) == 0
    asyncio.run(run())


def test_the_links_drop_ends_every_child(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay, "import time\ntime.sleep(60)\n")
        await relay.open(_open_frame(compute), ws)
        await relay.open(_open_frame(compute, server="other-mcp", command_id="c2"), _WS())
        pids = [c.proc.pid for c in relay._children.values()]
        assert relay.close_all("the link to the platform dropped") == 2
        assert len(relay) == 0

        def gone(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            # A zombie counts as ended: asyncio's child watcher reaps it, and
            # can do so between the two looks.
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii") as f:
                    return f.read().rsplit(")", 1)[1].split()[0] == "Z"
            except (FileNotFoundError, ProcessLookupError):
                return True
        await _until(lambda: all(gone(p) for p in pids))
    asyncio.run(run())


def test_a_disconnected_link_stops_the_reader(compute):
    async def run():
        relay, ws = ToolRelay(compute), _WS(connected=False)
        _bare_child(relay, "print('{}', flush=True)\nimport time\ntime.sleep(60)\n")
        await relay.open(_open_frame(compute), ws)
        await _until(lambda: len(relay) == 0)
    asyncio.run(run())


# -- a line too large for the link ----------------------------------------


def test_the_id_is_read_from_the_end_or_the_start_of_a_message():
    assert json.loads(oversize_answer(b'{"result":{', b'"jsonrpc":"2.0","id":7}'))["id"] == 7
    assert json.loads(oversize_answer(b'{"jsonrpc":"2.0","id":"a1","result":{', b'xx'))["id"] == "a1"


def test_a_nested_id_is_not_taken_for_the_messages_own():
    assert oversize_answer(b'{"result":{"id":99,"x":"', b'yyy"}}') == ""
    assert oversize_answer(b'{"method":"notifications/x","params":{"id":1,', b'}}') == ""


def _too_large_child(prefix: str, suffix: str, *, then: str = "") -> str:
    return (
        "import sys\n"
        f"sys.stdout.write({prefix!r} + 'x' * 400 + {suffix!r} + '\\n')\n"
        f"sys.stdout.write({then!r})\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    pass\n"
    )


def test_a_line_over_the_cap_answers_its_call_and_the_child_lives(compute, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_FRAME_BYTES", 100)
        relay, ws = ToolRelay(compute), _WS()
        script = _too_large_child('{"jsonrpc":"2.0","id":41,"result":"', '"}', then='{"next":true}\n')
        _bare_child(relay, script)
        await relay.open(_open_frame(compute), ws)
        await _until(lambda: len(ws.of("tool_out")) == 2)
        first = json.loads(ws.of("tool_out")[0]["line"])
        assert first["id"] == 41 and first["error"]["message"] == tool_relay.TOO_LARGE
        assert ws.of("tool_out")[1]["line"] == '{"next":true}'
        assert len(relay) == 1
        relay.close_all("test end")
    asyncio.run(run())


def test_a_line_over_the_cap_with_no_id_ends_the_child(compute, monkeypatch):
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_FRAME_BYTES", 100)
        relay, ws = ToolRelay(compute), _WS()
        _bare_child(relay, _too_large_child('{"method":"notifications/x","params":"', '"}'))
        await relay.open(_open_frame(compute), ws)
        await _until(lambda: ws.of("tool_exit"))
        assert not ws.of("tool_out")
        assert "too large" in ws.of("tool_exit")[0]["reason"]
    asyncio.run(run())


def test_a_line_within_the_raw_cap_but_over_it_encoded_is_measured(compute, monkeypatch):
    """Greek text is short in bytes and three times longer in the frame."""
    async def run():
        monkeypatch.setattr(tool_relay, "MAX_FRAME_BYTES", 2000)
        monkeypatch.setattr(tool_relay, "_SAFE_LINE_BYTES", 10)
        relay, ws = ToolRelay(compute), _WS()
        script = (
            "import sys\n"
            "sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":5,\"result\":\"' + '\\u03b1' * 600 + '\"}\\n')\n"
            "sys.stdout.flush()\n"
            "for line in sys.stdin:\n    pass\n"
        )
        _bare_child(relay, script)
        await relay.open(_open_frame(compute), ws)
        await _until(lambda: ws.of("tool_out"))
        assert json.loads(ws.of("tool_out")[0]["line"])["error"]["message"] == tool_relay.TOO_LARGE
        relay.close_all("test end")
    asyncio.run(run())


# -- what is written down --------------------------------------------------


def test_the_environment_reaches_no_log_line(compute, caplog):
    async def run():
        with caplog.at_level(logging.DEBUG, logger="satellite"):
            relay, ws = ToolRelay(compute), _WS()
            _bare_child(relay)
            await relay.open(_open_frame(compute), ws)
            await relay.open(_open_frame(compute, command_id="c2"), _WS())       # refused
            await relay.open(_open_frame(compute, server="b", tunnel_port=0), _WS())  # refused render
            relay.close_all("test end")
    asyncio.run(run())
    assert "hunter2-secret" not in caplog.text


def test_the_environment_is_handed_to_the_child_as_sent(compute, monkeypatch):
    async def run():
        relay, ws = ToolRelay(compute), _WS()
        script = "import os,sys\nprint(os.environ['SECRET_VALUE'], flush=True)\nfor l in sys.stdin:\n    pass\n"
        _bare_child(relay, script)
        await relay.open(_open_frame(compute), ws)
        await _until(lambda: ws.of("tool_out"))
        assert ws.of("tool_out")[0]["line"] == "hunter2-secret"
        relay.close_all("test end")
    asyncio.run(run())
