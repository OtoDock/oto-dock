"""A machine starts a bounded number of sessions at once, and every process a
session starts runs below the satellite's own CPU priority.

Before, a paired computer and the compute machine started every session the
platform asked for at the same moment, at the satellite's own priority: after
a restart of the platform the wave of interpreters and tool servers competed
with the loop that keeps the machine's link up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from satellite import config as sat_config_mod
from satellite.sessions import sandbox_host, start_slots


@pytest.fixture
def tmp_agent_dir(tmp_path):
    agent_dir = tmp_path / "agents" / "test-agent"
    agent_dir.mkdir(parents=True)
    return agent_dir


@pytest.fixture
def sat_config(tmp_path):
    from satellite.config import SatelliteConfig
    return SatelliteConfig(
        machine_id="test-machine", machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps",
    )


@pytest.fixture
def cli_config():
    return {
        "cwd_relative": "users/alice",
        "claude_dir_relative": "users/alice/.claude",
        "system_prompt": "You are a test agent.",
        "mcp_config": {"mcpServers": {}},
        "model": "claude-sonnet-5",
        "env": {"PROXY_URL": "http://100.1.2.3:8400", "PROXY_API_KEY": "test-key"},
    }


@pytest.fixture(autouse=True)
def _own_bound(monkeypatch):
    monkeypatch.setattr(start_slots, "_wanted", 0)
    monkeypatch.setattr(start_slots, "_slots", {})
    monkeypatch.setattr(start_slots, "_SETTLE_S", 0.0)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestTheBound:
    @pytest.mark.parametrize("cpus, own", [(1, 2), (4, 2), (5, 2), (6, 3), (16, 8)])
    def test_a_machines_own_bound_is_half_its_cpus_at_least_two(self, monkeypatch, cpus, own):
        monkeypatch.setattr(start_slots, "_cpus", lambda: cpus)
        assert start_slots.bound() == own

    @pytest.mark.parametrize("sent", [None, 0, -3, "4", 2.0, True, [4]])
    def test_only_a_whole_number_above_zero_is_the_platforms(self, monkeypatch, sent):
        monkeypatch.setattr(start_slots, "_cpus", lambda: 8)
        start_slots.set_bound(7)
        assert start_slots.bound() == 7
        start_slots.set_bound(sent)
        assert start_slots.bound() == 4

    @pytest.mark.asyncio
    async def test_at_most_the_bound_start_at_once(self):
        start_slots.set_bound(2)
        running: list[int] = []
        peak = 0
        go = asyncio.Event()

        async def one(n: int) -> None:
            nonlocal peak
            async with start_slots.slot():
                running.append(n)
                peak = max(peak, len(running))
                await go.wait()
                running.remove(n)

        tasks = [asyncio.create_task(one(n)) for n in range(5)]
        await _settle()
        assert sorted(running) == [0, 1]
        go.set()
        await asyncio.gather(*tasks)
        assert peak == 2

    @pytest.mark.asyncio
    async def test_a_slot_is_held_a_moment_after_its_start(self, monkeypatch):
        monkeypatch.setattr(start_slots, "_SETTLE_S", 0.15)
        start_slots.set_bound(1)
        loop = asyncio.get_running_loop()
        async with start_slots.slot():
            pass
        ended = loop.time()
        async with start_slots.slot():
            waited = loop.time() - ended
        assert waited >= 0.1

    @pytest.mark.asyncio
    async def test_a_start_that_raises_frees_its_slot_at_once(self, monkeypatch):
        monkeypatch.setattr(start_slots, "_SETTLE_S", 30.0)
        start_slots.set_bound(1)
        with pytest.raises(RuntimeError):
            async with start_slots.slot():
                raise RuntimeError("the engine is not installed")
        await asyncio.wait_for(start_slots.slot().__aenter__(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_a_new_bound_applies_to_the_next_start(self):
        start_slots.set_bound(1)
        async with start_slots.slot():
            start_slots.set_bound(2)
            await asyncio.wait_for(start_slots.slot().__aenter__(), timeout=1.0)


@pytest.mark.asyncio
async def test_the_platforms_number_arrives_at_the_sign_in(sat_config, monkeypatch):
    """``auth_result.session_starts`` is the admin's ``SESSION_SPAWN_CONCURRENCY``.
    A platform that sends none (the setting at 0, an older platform) leaves
    the machine its own bound, at this sign-in and after one that named a number."""
    import json

    from satellite.sessions.session_manager import SessionManager
    from satellite.transport import ws_client
    monkeypatch.setattr(start_slots, "_cpus", lambda: 8)
    client = ws_client.SatelliteWSClient(sat_config, SessionManager(sat_config))

    def _answering(**fields):
        class _Socket:
            async def send(self, text):
                pass

            async def recv(self):
                return json.dumps({"type": "auth_result", "status": "ok", **fields})
        return _Socket()

    client._ws = _answering(session_starts=3)
    await client._authenticate()
    assert start_slots.bound() == 3
    client._ws = _answering()
    await client._authenticate()
    assert start_slots.bound() == 4


class TestEveryStartTakesASlot:
    """The three places a session's process starts: a headless Claude Code
    session, a Codex daemon, a terminal's child."""

    @staticmethod
    async def _held():
        """The only slot, taken: a start waits for it."""
        start_slots.set_bound(1)
        taken = start_slots.slot()
        await taken.__aenter__()
        return taken

    @pytest.mark.asyncio
    async def test_a_headless_claude_session(self, tmp_agent_dir, cli_config, sat_config):
        from satellite.sessions.cli_session import CLISession
        taken = await self._held()
        proc = AsyncMock()
        proc.pid, proc.returncode = 11, None
        proc.stderr.read = AsyncMock(return_value=b"")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            session = CLISession("sess-slot", tmp_agent_dir, cli_config, sat_config)
            start = asyncio.create_task(session.start())
            await asyncio.sleep(0.2)
            assert spawn.await_count == 0 and not start.done()
            await taken.__aexit__(None, None, None)
            await asyncio.wait_for(start, timeout=5.0)
            assert spawn.await_count == 1

    @pytest.mark.asyncio
    async def test_a_codex_daemon(self, tmp_agent_dir, sat_config):
        from satellite.sessions import codex_session
        taken = await self._held()
        client = MagicMock()
        client.start = AsyncMock(return_value={})
        session = codex_session.CodexSession("sess-slot", tmp_agent_dir, {}, sat_config)
        session._cwd = Path(tmp_agent_dir)
        with patch.object(codex_session, "AppServerClient", return_value=client):
            start = asyncio.create_task(session._connect_with_retry({}, codex_bin="/usr/bin/codex"))
            await asyncio.sleep(0.2)
            assert client.start.await_count == 0 and not start.done()
            await taken.__aexit__(None, None, None)
            await asyncio.wait_for(start, timeout=5.0)
            assert client.start.await_count == 1

    @pytest.mark.asyncio
    async def test_a_terminals_child(self, tmp_agent_dir, cli_config, sat_config, monkeypatch):
        from satellite.terminal import pty_relay
        from satellite.terminal.pty_session import PtySession
        taken = await self._held()
        spawned = MagicMock(return_value=SimpleNamespace(pid=12))
        monkeypatch.setattr(pty_relay, "spawn_pty", spawned)
        session = PtySession("sess-slot", tmp_agent_dir, cli_config, sat_config, MagicMock())
        session._compute, session._own_copy = True, False       # no file snapshot

        async def _no_tail():
            return None
        session._transcript_tail_loop = _no_tail
        start = asyncio.create_task(session._wire_and_spawn(["claude"], {"TERM": "xterm"}, None))
        await asyncio.sleep(0.2)
        assert spawned.call_count == 0 and not start.done()
        await taken.__aexit__(None, None, None)
        await asyncio.wait_for(start, timeout=5.0)
        assert spawned.call_count == 1 and session.pid == 12


class TestThePriority:
    """``nice -n 10`` in front of every command a session's process starts
    with, so the engine and everything it starts run below the satellite."""

    @pytest.fixture(autouse=True)
    def _nice(self, monkeypatch):
        sandbox_host._nice_bin.cache_clear()
        monkeypatch.setattr(sandbox_host.shutil, "which", lambda name: f"/usr/bin/{name}")
        yield
        sandbox_host._nice_bin.cache_clear()

    def test_a_command_is_run_through_nice(self):
        assert sandbox_host.niced(["claude", "-p"]) == ["/usr/bin/nice", "-n", "10", "claude", "-p"]

    def test_a_machine_without_nice_runs_the_command_as_it_is(self, monkeypatch):
        monkeypatch.setattr(sandbox_host.shutil, "which", lambda name: None)
        assert sandbox_host.niced(["claude"]) == ["claude"]

    def test_windows_has_no_nice_whatever_its_path_holds(self, monkeypatch):
        monkeypatch.setattr(sat_config_mod, "HOST", SimpleNamespace(posix=False, oom_score=False))
        assert sandbox_host.niced(["claude.cmd"]) == ["claude.cmd"]
        assert sandbox_host.paired_command(["claude.cmd"]) == ["claude.cmd"]

    def test_a_paired_computers_session_and_its_codex_daemon(self, monkeypatch):
        monkeypatch.setattr(sat_config_mod, "HOST", SimpleNamespace(posix=True, oom_score=True))
        cmd = sandbox_host.paired_command(["claude", "-p"])
        assert cmd[:5] == ["/usr/bin/nice", "-n", "10", "/bin/sh", "-c"] and cmd[-2:] == ["claude", "-p"]
        # The prefix a Codex daemon's command is put behind.
        assert sandbox_host.paired_command([])[:3] == ["/usr/bin/nice", "-n", "10"]
        monkeypatch.setattr(sat_config_mod, "HOST", SimpleNamespace(posix=True, oom_score=False))
        assert sandbox_host.paired_command(["claude"]) == ["/usr/bin/nice", "-n", "10", "claude"]

    @pytest.mark.asyncio
    async def test_a_compute_session_a_terminal_and_a_codex_daemon(self, sat_config, monkeypatch):
        monkeypatch.setattr(sandbox_host, "render_command",
                            lambda desc, sat, **kw: ["/bin/sh", "-c", "raise", "oto-session", "bwrap", *kw["argv"]])
        monkeypatch.setattr(sandbox_host.render.SandboxDescription, "from_dict", staticmethod(lambda d: d))
        monkeypatch.setattr(sandbox_host, "link_port", lambda payload: 1)
        from satellite.host import cli_versions
        monkeypatch.setattr(cli_versions, "resolve_spawn_bin_async", AsyncMock(return_value="/opt/claude"))
        payload = {"sandbox": {}, "argv": ["claude", "-p"]}
        for for_pty in (False, True):
            cmd = await sandbox_host.spawn_command(payload, sat_config, "claude", for_pty=for_pty)
            assert cmd[:4] == ["/usr/bin/nice", "-n", "10", "/bin/sh"] and cmd[-2:] == ["claude", "-p"]
        monkeypatch.setattr(sandbox_host, "render_host", lambda sat, **kw: object())
        monkeypatch.setattr(sandbox_host.render, "render", lambda desc, host, argv: ["bwrap", "--", *argv])
        monkeypatch.setattr(sandbox_host, "refuse_other_tools_path", lambda desc, sat: None)
        payload["sandbox"] = SimpleNamespace(tmp_size_mb=0)
        prefix, _bin = await sandbox_host.sandbox_prefix(payload, sat_config, "codex")
        assert prefix[:4] == ["/usr/bin/nice", "-n", "10", "/bin/sh"] and prefix[-2:] == ["bwrap", "--"]

    def test_a_tool_of_a_compute_session_takes_the_same_owner(self, sat_config, monkeypatch):
        """The tool relay had a ``nice`` lookup of its own."""
        from satellite.sessions import tool_relay
        monkeypatch.setattr(tool_relay.render.SandboxDescription, "from_dict",
                            staticmethod(lambda d: SimpleNamespace(tmp_size_mb=0)))
        monkeypatch.setattr(sandbox_host, "render_host", lambda sat, **kw: object())
        monkeypatch.setattr(tool_relay.render, "render", lambda desc, host, argv: ["bwrap", "--", *argv])
        monkeypatch.setattr(sandbox_host, "link_port", lambda msg: 1)
        relay = tool_relay.ToolRelay.__new__(tool_relay.ToolRelay)
        relay.config = sat_config
        cmd = relay._command({}, {}, ["python", "server.py"])
        assert cmd[:4] == ["/usr/bin/nice", "-n", "10", "/bin/sh"] and cmd[-2:] == ["python", "server.py"]
        assert not hasattr(tool_relay, "shutil")
