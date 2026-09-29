"""The satellite twin of the Codex pre-turn MCP warm gate: the vendored
``wait_for_mcp_startup`` (its behaviour is covered proxy-side in
``tests/session/test_codex_warm_gate.py``) wired into ``CodexSession`` with the
satellite's caps, plus the vendored header parser."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from satellite._vendored.app_server_client import (
    MCP_STARTUP_METHOD, mcp_server_names_from_toml, wait_for_mcp_startup,
)
from satellite.sessions import codex_session as mod
from satellite.sessions.codex_session import CodexSession


@pytest.fixture
def sat_config():
    from satellite.config import SatelliteConfig
    return SatelliteConfig(
        machine_id="test-machine", machine_secret="test-secret",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=Path("/tmp/test-agents"), mcps_dir=Path("/tmp/test-mcps"),
    )


def _session(sat_config, names, local_model=False):
    config = {"cwd_relative": "workspace", "codex_dir_relative": "workspace/.codex"}
    if local_model:
        config["local_model_provider"] = {"base_url": "http://127.0.0.1:11434/v1", "env_key": ""}
    s = CodexSession("sess-warm", Path("/tmp/test-agents/a"), config, sat_config)
    s._mcp_server_names = list(names)
    s._client = SimpleNamespace(notif_queue=asyncio.Queue())
    return s


def _status(name, status):
    return (MCP_STARTUP_METHOD, {"threadId": "t1", "name": name, "status": status})


@pytest.mark.asyncio
async def test_vendored_gate_waits_for_expected_servers_only():
    client = SimpleNamespace(notif_queue=asyncio.Queue())

    async def feed():
        await client.notif_queue.put(_status("codex_apps", "starting"))
        await client.notif_queue.put(_status("a", "starting"))
        await asyncio.sleep(0.3)
        await client.notif_queue.put(_status("a", "ready"))

    feeder = asyncio.create_task(feed())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await wait_for_mcp_startup(client, ["a"], cap_s=2.0, poll_s=0.05, no_status_grace_s=1.0)
    feeder.cancel()
    assert 0.3 <= loop.time() - t0 < 0.8
    assert res.pending == [] and res.ready == ["a"] and res.unexpected == ["codex_apps"]


@pytest.mark.asyncio
async def test_session_picks_the_cap_by_model_kind(sat_config, monkeypatch, caplog):
    monkeypatch.setattr(mod, "_WARM_POLL_S", 0.05)
    monkeypatch.setattr(mod, "_WARM_CAP_S", 0.2)
    monkeypatch.setattr(mod, "_WARM_CAP_LOCAL_MODEL_S", 0.6)
    monkeypatch.setattr(mod, "_WARM_NO_STATUS_S", 5.0)
    assert mod._WARM_CAP_LOCAL_MODEL_S > mod._WARM_CAP_S

    async def run(local_model):
        s = _session(sat_config, ["stuck"], local_model=local_model)
        assert s._local_model is local_model
        await s._client.notif_queue.put(_status("stuck", "starting"))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await s._warm_mcps()
        return loop.time() - t0

    assert 0.2 <= await run(False) < 0.5
    assert 0.6 <= await run(True) < 0.9
    assert any("hit the" in r.message and "stuck" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_session_with_no_servers_exits_on_the_first_silence(sat_config, monkeypatch):
    monkeypatch.setattr(mod, "_WARM_POLL_S", 0.05)
    s = _session(sat_config, [])
    await s._client.notif_queue.put(("thread/started", {}))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await s._warm_mcps()
    assert loop.time() - t0 < 0.2


@pytest.mark.asyncio
async def test_daemon_exit_returns_at_once(sat_config):
    s = _session(sat_config, ["a"])
    await s._client.notif_queue.put(_status("a", "starting"))
    await s._client.notif_queue.put(("__daemon_exit__", {}))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await s._warm_mcps()
    assert loop.time() - t0 < 0.05


def test_mcp_server_names_from_toml():
    toml = (
        'model_provider = "oto_local"\n\n[features]\nplugins = false\n\n'
        '[mcp_servers.task-mcp]\ncommand = "python3"\n\n'
        '[mcp_servers.task-mcp.env]\nB = "2"\n\n'
        '[mcp_servers."file.tools"]\nurl = "http://x"\n\n'
        '[mcp_servers.github.http_headers]\nAuthorization = "Bearer x"\n\n'
        '[mcp_servers.github]\nurl = "http://y"\n\n'
        '[model_providers.oto_local]\nbase_url = "http://h/v1"\n'
    )
    assert mcp_server_names_from_toml(toml) == ["task-mcp", "file.tools", "github"]
    assert mcp_server_names_from_toml("") == []
