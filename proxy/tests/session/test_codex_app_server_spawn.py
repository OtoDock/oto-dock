"""The Codex app-server daemon starts off the event loop.

The transport client is vendored into the satellite as a stdlib-only module,
so it takes the spawner as a callable: the proxy's layer hands it
``pty_relay.spawn_piped`` inside a spawn slot; the satellite (and the client
alone) keep ``asyncio.create_subprocess_exec``.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from core.layers.codex.app_server_client import AppServerClient


class _Proc:
    """The part of a process the client touches, at EOF on both pipes."""

    def __init__(self):
        self.pid = 4242
        self._rc = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

        class _In:
            def write(self, b):
                pass

            async def drain(self):
                pass

            def close(self):
                pass
        self.stdin = _In()

    @property
    def returncode(self):
        return self._rc

    def send_signal(self, sig):
        self._rc = -sig

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9

    async def wait(self):
        if self._rc is None:
            self._rc = 0
        return self._rc


@pytest.mark.asyncio
async def test_start_uses_the_injected_spawner():
    seen = {}
    proc = _Proc()

    async def _spawn(argv, *, cwd, env, limit):
        seen.update(argv=list(argv), cwd=cwd, env=env, limit=limit)
        return proc

    client = AppServerClient(
        env={"A": "1"}, cwd="/tmp", sandbox_cmd_prefix=["nice", "-n", "10"],
        codex_bin="codex-bin", spawn=_spawn,
    )
    client.request = AsyncMock(return_value={"codexHome": "h", "platformOs": "linux"})
    await client.start({"clientInfo": {}})
    assert seen["argv"] == ["nice", "-n", "10", "codex-bin", "app-server"]
    assert seen["cwd"] == "/tmp" and seen["env"] == {"A": "1"}
    assert seen["limit"] == 200 * 1024 * 1024
    assert client.proc is proc and client.is_alive
    proc._rc = 0  # exited: close() has no group to signal
    await client.close()


@pytest.mark.asyncio
async def test_the_codex_layer_spawns_in_a_slot(monkeypatch):
    from core.layers.codex import session as cs
    from core.sandbox import pty_relay
    events: list = []

    @contextlib.asynccontextmanager
    async def _slot():
        events.append("slot-in")
        yield
        events.append("slot-out")

    async def _piped(argv, *, cwd, env, limit):
        events.append(("spawn", list(argv), cwd, limit))
        return "proc"
    monkeypatch.setattr(pty_relay, "spawn_slot", _slot)
    monkeypatch.setattr(pty_relay, "spawn_piped", _piped)
    out = await cs._spawn_app_server(["codex", "app-server"], cwd="/w", env={}, limit=5)
    assert out == "proc"
    assert events == ["slot-in", ("spawn", ["codex", "app-server"], "/w", 5), "slot-out"]
