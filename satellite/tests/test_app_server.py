"""App servers on a compute machine (``sessions/app_server.py``).

Load-bearing: a server starts only here, only as the platform planned it, in
the sandbox this machine renders; the platform reaches it through one relay
that admits one address; its output cannot crowd the control lane; a stop, a
sweep and a link that stays down end it; a start in flight is seen by a sweep
and aborted by a stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from satellite._vendored import sandbox_render as render
from satellite.config import SatelliteConfig
from satellite.sessions import app_server, sandbox_host
from satellite.sessions.app_server import AppServers

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="compute machines are Linux")

LISTENER = 45555
PY_SERVER = textwrap.dedent("""
    import socket, sys
    print("server starting", flush=True)
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(8)
    while True:
        c, _ = s.accept()
        c.recv(65536)
        c.sendall(b"HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok")
        c.close()
""")


class _WS:
    def __init__(self, local="127.0.0.1", remote="127.0.0.1", listener=LISTENER):
        self.sent: list[dict] = []
        self.bulk: list[dict] = []
        self._authenticated = True
        self._ws = SimpleNamespace(local_address=(local, 40000), remote_address=(remote, 8400))
        self.tunnel = SimpleNamespace(app_port=listener)

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    async def enqueue_bulk(self, msg):
        if not self._authenticated:
            return False
        self.bulk.append(msg)
        return True

    def ack(self) -> dict:
        acks = [m for m in self.sent if m.get("type") == "ack"]
        assert len(acks) == 1, self.sent
        return acks[0]


def _desc(**over) -> dict:
    d = {"agent": "a", "binds": [], "mcp_binds": [], "mcps_root": "/m", "extra_binds": [], "cwd": "/app",
         "proxy_port": 8400, "forwards": ["proxy"], "allow_hosts": [], "hosted": False, "inbound": "0:3000",
         "egress_deny": True, "beneath": True, "tmp_size_mb": 64}
    d.update(over)
    return d


def _frame(server_id: str | None = None, boot: str = "b1", **over) -> dict:
    msg = {"type": "app_server_start", "command_id": "c-1", "server_id": server_id or str(uuid.uuid4()), "boot": boot,
           "app_id": str(uuid.uuid4()), "instance": "live", "sandbox": _desc(),
           "argv": ["bun", "run", "server/index.ts"], "env": {"PORT": "3000", "SECRET": "s3cret"}}
    msg.update(over)
    return msg


def _cfg(tmp_path, compute=True) -> SatelliteConfig:
    (tmp_path / "agents").mkdir(exist_ok=True)
    (tmp_path / "mcps").mkdir(exist_ok=True)
    return SatelliteConfig(machine_id="c", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                           agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps", compute=compute)


@pytest.fixture
def fake_run(monkeypatch):
    """A plain Python server in place of the sandbox: it binds the loopback
    port the description was placed on. Records what the render was given."""
    seen: dict = {}

    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        seen.update(desc=desc, tunnel_port=tunnel_port, argv=argv, bin_path=bin_path, swap=swap_binary)
        return [sys.executable, "-c", PY_SERVER, desc.inbound.split(":")[0]]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun" if name == "bun" else None)
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.3)
    return seen


async def _get(host: str, port: int, timeout: float = 5.0) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.write(b"GET /_health HTTP/1.0\r\n\r\n")
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout)
    except ConnectionResetError:
        return b""
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_server_is_refused_off_a_compute_machine(tmp_path):
    ws = _WS()
    await AppServers(_cfg(tmp_path, compute=False)).start(_frame(), ws)
    assert ws.ack()["status"] == "error" and "compute machines" in ws.ack()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"sandbox": "nope"},
    {"argv": ["sh", "-c", "id"]},
    {"argv": ["bun", "run", "../../etc/x.ts"]},
    {"env": "x"},
    {"server_id": "not-a-uuid"},
    {"sandbox": _desc(inbound="")},
])
async def test_a_plan_this_machine_does_not_run_is_refused(tmp_path, over):
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(**over), ws)
    assert ws.ack()["status"] == "error"
    assert servers.servers == {}


@pytest.mark.asyncio
async def test_a_description_with_a_field_this_render_does_not_know_is_refused(tmp_path, fake_run):
    ws = _WS()
    await AppServers(_cfg(tmp_path)).start(_frame(sandbox=_desc(new_mask=True)), ws)
    assert ws.ack()["status"] == "error" and "new_mask" in ws.ack()["error"]


@pytest.mark.asyncio
async def test_a_machine_without_bun_says_so(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(app_server.shutil, "which", lambda name: None)
    ws = _WS()
    await AppServers(_cfg(tmp_path)).start(_frame(), ws)
    assert ws.ack()["status"] == "error" and "Bun" in ws.ack()["error"]


@pytest.mark.asyncio
async def test_no_app_listener_refuses_rather_than_splice_the_platforms_port(tmp_path, fake_run):
    """A zero port would splice the sandbox onto the bare proxy port."""
    ws = _WS(listener=0)
    await AppServers(_cfg(tmp_path)).start(_frame(), ws)
    assert ws.ack()["status"] == "error"
    assert "desc" not in fake_run


@pytest.mark.asyncio
async def test_a_link_with_no_ipv4_address_refuses(tmp_path, fake_run):
    ws = _WS(local="::1", remote="::1")
    await AppServers(_cfg(tmp_path)).start(_frame(), ws)
    assert ws.ack()["status"] == "error" and "IPv4" in ws.ack()["error"]


@pytest.mark.asyncio
async def test_a_server_starts_in_the_rendered_sandbox_behind_its_relay(tmp_path, fake_run):
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    ack = ws.ack()
    assert ack["status"] == "ok", ack
    try:
        # The app listener carries its platform calls, its own Bun runs it,
        # and the loopback port the description was placed on is not the relay's.
        assert fake_run["tunnel_port"] == LISTENER
        assert fake_run["bin_path"] == "/usr/local/bin/bun" and fake_run["swap"] is True
        assert fake_run["argv"] == ["bun", "run", "server/index.ts"]
        loopback, port = fake_run["desc"].inbound.split(":")
        assert port == "3000" and int(loopback) > 0
        assert ack["host_port"] != int(loopback)
        assert b"200 OK" in await _get("127.0.0.1", ack["host_port"])
    finally:
        assert servers.kill_all() == 1


def test_an_app_tables_render_binds_the_release_and_its_data_alone_behind_a_closed_network(tmp_path, monkeypatch):
    """The command this machine renders for an app's table: the release
    read-only and the data writable and no other folder of the agent, the
    platform's port spliced onto the local link, one port in, the private
    ranges and the public internet closed but the approved address."""
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: tmp_path / ".oto-dock")
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    cfg = _cfg(tmp_path)
    agent = cfg.agents_dir / "a"
    rel, data = agent / "app-releases" / "shared" / "x" / "1", agent / "app-data" / "shared" / "x"
    for folder in (rel, data, agent / "workspace", agent / "knowledge", agent / "users", agent / "config"):
        folder.mkdir(parents=True)
    desc = render.SandboxDescription.from_dict(_desc(
        binds=[{"root": "agents", "rel": "app-releases/shared/x/1", "sandbox": "/app", "rw": False},
               {"root": "agents", "rel": "app-data/shared/x", "sandbox": "/app/data", "rw": True}],
        mcps_root=str(cfg.mcps_dir), inbound="18081:3000", allow_hosts=["203.0.113.5"]))
    cmd = sandbox_host.render_command(desc, cfg, tunnel_port=LISTENER, bin_path="/usr/local/bin/bun",
                                      argv=["bun", "run", "server/index.ts"], swap_binary=True)
    launcher, bwrap = cmd[:cmd.index("bwrap")], cmd[cmd.index("bwrap"):]
    assert "--block-private" in launcher and "--egress-deny" in launcher
    assert launcher[launcher.index("--inbound") + 1] == "18081:3000"
    assert launcher[launcher.index("--forward") + 1] == f"{_desc()['proxy_port']}:{LISTENER}"
    assert launcher[launcher.index("--allow-host") + 1] == "203.0.113.5"
    assert launcher[launcher.index("--beneath") + 1] == str(agent.resolve())
    joined = " ".join(bwrap)
    assert f"--ro-bind {rel.resolve()} /app" in joined and f"--bind {data.resolve()} /app/data" in joined
    assert "--chdir /app" in joined and "--cap-drop ALL" in joined and "--unshare-pid" in joined
    for other in ("workspace", "knowledge", "users", "config"):
        assert str(agent / other) not in bwrap and f"/{other}" not in bwrap
    assert bwrap[-3:] == ["/usr/local/bin/bun", "run", "server/index.ts"]


@pytest.mark.asyncio
async def test_the_relay_admits_only_the_far_end_of_the_link(tmp_path, fake_run):
    ws = _WS(remote="10.9.9.9")
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    try:
        assert await _get("127.0.0.1", ws.ack()["host_port"]) == b""
    finally:
        servers.kill_all()


@pytest.mark.asyncio
async def test_the_relay_binds_the_links_own_address_alone(tmp_path, fake_run):
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    try:
        server = next(iter(servers.servers.values()))
        assert [s.getsockname()[0] for s in server.relay.sockets] == ["127.0.0.1"]
    finally:
        servers.kill_all()


@pytest.mark.asyncio
async def test_output_rides_the_bulk_lane_and_the_exit_follows_the_last_line(tmp_path, monkeypatch):
    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        return [sys.executable, "-c", "import time;print('hello',flush=True);time.sleep(.6);print('bye',flush=True)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.2)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    assert ws.ack()["status"] == "ok"
    for _ in range(100):
        if any(m["type"] == "app_server_exit" for m in ws.bulk):
            break
        await asyncio.sleep(0.05)
    kinds = [m["type"] for m in ws.bulk]
    assert kinds[-1] == "app_server_exit" and ws.bulk[-1]["code"] == 0
    assert "bye" in "".join(m.get("text", "") for m in ws.bulk)
    assert not any(m["type"] == "app_server_output" for m in ws.sent)
    assert servers.servers == {}


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the memory guard runs on Linux")
async def test_a_server_the_memory_guard_stops_says_so_in_its_log(tmp_path, fake_run):
    """The guard's stop of a running app server: the last line of its output,
    the app's log, is the sentence, and its exit follows (mutation: no sentence
    written)."""
    from satellite.sessions import memory_guard as mg
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    assert ws.ack()["status"] == "ok"
    guard = mg.Guard(lambda: [mg.Unit(s.proc.pid, "", "app") for s in servers.servers.values()], compute=True)
    [app] = [w for w in await asyncio.to_thread(guard.walk) if w.unit.kind == "app"]
    await asyncio.to_thread(guard.stop, app, app.parts[0], guard.numbers(8192), 0.0, why="reserve", now=0.0)
    for _ in range(100):
        if any(m["type"] == "app_server_exit" for m in ws.bulk):
            break
        await asyncio.sleep(0.05)
    assert ws.bulk[-1]["type"] == "app_server_exit" and ws.bulk[-1]["code"] == -9
    log = "".join(m.get("text", "") for m in ws.bulk)
    assert log.rstrip().endswith(mg.SENTENCES["app"]), log


@pytest.mark.asyncio
async def test_a_line_reaches_the_platform_while_the_server_stays_quiet(tmp_path, monkeypatch):
    """A server prints once and goes silent: its line must not wait for its
    next print or its exit."""
    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        return [sys.executable, "-c", "import time;print('listening',flush=True);time.sleep(30)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.2)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    try:
        assert ws.ack()["status"] == "ok"
        for _ in range(100):
            if any("listening" in m.get("text", "") for m in ws.bulk):
                break
            await asyncio.sleep(0.05)
        assert any("listening" in m.get("text", "") for m in ws.bulk)
    finally:
        servers.kill_all()


@pytest.mark.asyncio
async def test_an_exit_with_the_link_down_falls_back_to_the_control_lane(tmp_path, monkeypatch):
    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        return [sys.executable, "-c", "import time;time.sleep(.5)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.2)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    ws._authenticated = False
    for _ in range(100):
        if any(m["type"] == "app_server_exit" for m in ws.sent):
            break
        await asyncio.sleep(0.05)
    assert any(m["type"] == "app_server_exit" for m in ws.sent)


@pytest.mark.asyncio
async def test_a_server_that_dies_at_once_reports_why(tmp_path, monkeypatch):
    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        return [sys.executable, "-c", "print('boom: a syntax error');raise SystemExit(2)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.5)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    ack = ws.ack()
    assert ack["status"] == "error" and "boom" in ack["error"]
    assert servers.servers == {}


@pytest.mark.asyncio
async def test_a_bind_failure_tries_another_port(tmp_path, monkeypatch):
    ports: list[str] = []

    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        ports.append(desc.inbound.split(":")[0])
        return [sys.executable, "-c", "print('bind: address in use');raise SystemExit(1)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.3)
    ws = _WS()
    await AppServers(_cfg(tmp_path)).start(_frame(), ws)
    assert ws.ack()["status"] == "error"
    assert len(ports) == app_server.BIND_RETRIES


@pytest.mark.asyncio
async def test_a_stop_ends_the_group_and_closes_the_relay(tmp_path, fake_run):
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    msg = _frame()
    await servers.start(msg, ws)
    port = ws.ack()["host_port"]
    proc = next(iter(servers.servers.values())).proc
    ws.sent.clear()
    await servers.stop({"command_id": "c-2", "server_id": msg["server_id"]}, ws)
    assert ws.ack()["stopped"] is True
    assert proc.returncode is not None or proc.pid
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
    assert servers.servers == {}


@pytest.mark.asyncio
async def test_a_server_that_exits_by_itself_closes_its_relay(tmp_path, monkeypatch):
    def render_command(desc, cfg, *, tunnel_port, argv, bin_path="", swap_binary=False):
        return [sys.executable, "-c", "import time;time.sleep(.6)"]

    monkeypatch.setattr(sandbox_host, "render_command", render_command)
    monkeypatch.setattr(app_server.shutil, "which", lambda name: "/usr/local/bin/bun")
    monkeypatch.setattr(app_server, "START_WAIT_S", 0.2)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    port = ws.ack()["host_port"]
    for _ in range(100):
        if any(m["type"] == "app_server_exit" for m in ws.bulk):
            break
        await asyncio.sleep(0.05)
    assert any(m["type"] == "app_server_exit" for m in ws.bulk)
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)


@pytest.mark.asyncio
async def test_a_stop_for_a_server_not_here_is_answered(tmp_path):
    ws = _WS()
    await AppServers(_cfg(tmp_path)).stop({"command_id": "c-2", "server_id": str(uuid.uuid4())}, ws)
    assert ws.ack() == {"type": "ack", "command_id": "c-2", "status": "ok", "stopped": False}


@pytest.mark.asyncio
async def test_a_stop_aborts_a_start_in_progress(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(app_server, "START_WAIT_S", 2.0)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    msg = _frame()
    start = asyncio.create_task(servers.start(msg, ws))
    await asyncio.sleep(0.5)
    await servers.stop({"command_id": "c-2", "server_id": msg["server_id"]}, ws)
    await start
    acks = {m["command_id"]: m for m in ws.sent if m["type"] == "ack"}
    assert acks["c-1"]["status"] == "error"
    assert servers.servers == {}


@pytest.mark.asyncio
async def test_a_sweep_stops_another_boots_servers_and_lists_this_boots(tmp_path, fake_run):
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    old, new = _frame(boot="b0"), _frame(boot="b1")
    await servers.start(old, ws)
    await servers.start(new, ws)
    ws.sent.clear()
    await servers.sweep({"command_id": "c-3", "boot": "b1"}, ws)
    assert ws.ack()["live"] == [new["server_id"]]
    assert list(servers.servers) == [new["server_id"]]
    servers.kill_all()


@pytest.mark.asyncio
async def test_a_sweep_lists_a_start_still_in_flight(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(app_server, "START_WAIT_S", 1.0)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    msg = _frame(boot="b1")
    start = asyncio.create_task(servers.start(msg, ws))
    await asyncio.sleep(0.2)
    await servers.sweep({"command_id": "c-3", "boot": "b1"}, ws)
    sweep_ack = next(m for m in ws.sent if m["command_id"] == "c-3")
    assert sweep_ack["live"] == [msg["server_id"]]
    await start
    servers.kill_all()


@pytest.mark.asyncio
async def test_a_link_down_past_its_bound_stops_every_server(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(app_server, "_LINK_WATCH_S", 0.05)
    monkeypatch.setattr(app_server, "LINK_DOWN_STOP_S", 0.1)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    assert servers.servers
    server = next(iter(servers.servers.values()))
    ws._authenticated = False
    for _ in range(100):
        if not servers.servers:
            break
        await asyncio.sleep(0.05)
    assert servers.servers == {}
    await asyncio.wait_for(server.watch, 5)
    # A stop is no crash: this frame would reach the platform first when the
    # link returns, ahead of the sign-in's sweep.
    assert not [m for m in (*ws.sent, *ws.bulk) if m["type"] == "app_server_exit"]


@pytest.mark.asyncio
async def test_a_link_that_returns_inside_its_bound_stops_nothing(tmp_path, fake_run, monkeypatch):
    monkeypatch.setattr(app_server, "_LINK_WATCH_S", 0.05)
    monkeypatch.setattr(app_server, "LINK_DOWN_STOP_S", 30.0)
    ws = _WS()
    servers = AppServers(_cfg(tmp_path))
    await servers.start(_frame(), ws)
    ws._authenticated = False
    await asyncio.sleep(0.2)
    ws._authenticated = True
    await asyncio.sleep(0.2)
    assert servers.servers
    servers.kill_all()


_NEEDS_TOOLS = not all(shutil.which(t) for t in ("pasta", "bwrap", "bun"))


@pytest.mark.asyncio
@pytest.mark.skipif(_NEEDS_TOOLS, reason="needs pasta, bwrap and bun")
async def test_a_real_server_runs_in_the_sandbox_and_answers_through_the_relay(tmp_path):
    """The whole path on this machine: the vendored launcher and render, the
    held binds of an app table, the data folder writable and the release not."""
    cfg = _cfg(tmp_path)
    rel = cfg.agents_dir / "a" / "app-releases" / "shared" / "x" / "1"
    data = cfg.agents_dir / "a" / "app-data" / "shared" / "s"
    (rel / "server").mkdir(parents=True)
    (rel / "data").mkdir()
    data.mkdir(parents=True)
    (rel / "server" / "index.ts").write_text(textwrap.dedent("""
        import { writeFileSync } from "fs";
        let ro = "denied"; try { writeFileSync("/app/x", "1"); ro = "WRITABLE"; } catch {}
        writeFileSync("/app/data/seen", "1");
        console.log("up", ro);
        Bun.serve({port: Number(process.env.PORT), hostname: "0.0.0.0", fetch() { return new Response("ok " + ro); }});
    """))
    desc = _desc(binds=[{"root": "agents", "rel": "app-releases/shared/x/1", "sandbox": "/app", "rw": False},
                        {"root": "agents", "rel": "app-data/shared/s", "sandbox": "/app/data", "rw": True}],
                 mcps_root=str(cfg.mcps_dir), tmp_size_mb=0)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PORT": "3000"}
    ws = _WS()
    servers = AppServers(cfg)
    await servers.start(_frame(sandbox=desc, env=env), ws)
    ack = ws.ack()
    assert ack["status"] == "ok", ack
    try:
        for _ in range(40):
            body = await _get("127.0.0.1", ack["host_port"])
            if body:
                break
            await asyncio.sleep(0.25)
        assert b"ok denied" in body
        assert (data / "seen").exists()
    finally:
        server = next(iter(servers.servers.values()))
        pgid = server.proc.pid
        servers.kill_all()
        with pytest.raises(ProcessLookupError):
            for _ in range(40):
                os.killpg(pgid, 0)
                await asyncio.sleep(0.1)
            raise ProcessLookupError


PROBE_SERVER = textwrap.dedent("""
    Bun.serve({port: Number(process.env.PORT), hostname: "0.0.0.0", async fetch(req) {
      const target = new URL(req.url).searchParams.get("url");
      if (!target) return new Response("ok");
      try {
        const r = await fetch(target, {signal: AbortSignal.timeout(2500)});
        return new Response("reached:" + r.status);
      } catch (e) { return new Response("err:" + String(e).slice(0, 80)); }
    }});
""")


def _machine_route() -> tuple[str, str]:
    """``(this machine's own address on its network, its gateway)``, each ""
    when the route table names none."""
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return "", ""
    return (out[out.index("src") + 1] if "src" in out else "",
            out[out.index("via") + 1] if "via" in out else "")


async def _ask(port: int, path: str = "/", host: str = "127.0.0.1", timeout: float = 12.0) -> str:
    """The body of one GET, "" when nothing answers."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except OSError:
        return ""
    try:
        writer.write(f"GET {path} HTTP/1.0\r\nHost: app\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    except ConnectionResetError:
        return ""
    finally:
        writer.close()
    return raw.split(b"\r\n\r\n", 1)[-1].decode("utf-8", "replace")


@pytest.mark.asyncio
@pytest.mark.skipif(_NEEDS_TOOLS, reason="needs pasta, bwrap and bun")
async def test_a_real_server_reaches_no_sibling_no_address_of_this_machine_and_no_public_host(tmp_path, monkeypatch):
    """Two servers in their own sandboxes: the second dials the first's port
    on loopback, a listener on this machine's own address, and a public
    host. Each one answers from here and none answers from inside, where the
    platform's port is the one door that opens."""
    cfg = _cfg(tmp_path)
    placed: list[int] = []
    real_render = sandbox_host.render_command

    def recording(desc, sat_config, **kw):
        placed.append(int(desc.inbound.split(":")[0]))
        return real_render(desc, sat_config, **kw)

    monkeypatch.setattr(sandbox_host, "render_command", recording)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PORT": "3000"}

    def answering(text: bytes):
        async def answer(reader, writer):
            # The request is read first: a close over unread bytes resets the dial.
            with contextlib.suppress(asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(text), text))
                await writer.drain()
            writer.close()
        return answer

    platform = await asyncio.start_server(answering(b"platform"), "127.0.0.1", 0)
    listener = await asyncio.start_server(answering(b"host"), "0.0.0.0", 0)
    host_port = listener.sockets[0].getsockname()[1]
    ws = _WS(listener=platform.sockets[0].getsockname()[1])
    servers = AppServers(cfg)
    ports: list[int] = []
    for slug in ("one", "two"):
        rel = cfg.agents_dir / "a" / "app-releases" / "shared" / slug / "1"
        data = cfg.agents_dir / "a" / "app-data" / "shared" / slug
        (rel / "server").mkdir(parents=True)
        (rel / "data").mkdir()
        data.mkdir(parents=True)
        (rel / "server" / "index.ts").write_text(PROBE_SERVER)
        desc = _desc(binds=[{"root": "agents", "rel": f"app-releases/shared/{slug}/1", "sandbox": "/app", "rw": False},
                            {"root": "agents", "rel": f"app-data/shared/{slug}", "sandbox": "/app/data", "rw": True}],
                     mcps_root=str(cfg.mcps_dir), tmp_size_mb=0)
        ws.sent.clear()
        await servers.start(_frame(sandbox=desc, env=env), ws)
        ack = ws.ack()
        assert ack["status"] == "ok", ack
        ports.append(ack["host_port"])
    address, gateway = _machine_route()
    try:
        for port in ports:
            for _ in range(40):
                if await _ask(port) == "ok":
                    break
                await asyncio.sleep(0.25)
            assert await _ask(port) == "ok"
        sibling, second = placed[0], ports[1]

        async def probe(url: str) -> str:
            return await _ask(second, "/?url=" + quote(url, safe=""))

        assert await probe(f"http://127.0.0.1:{_desc()['proxy_port']}/") == "reached:200"
        # Each target answers a dial from this machine itself.
        assert await _ask(sibling) == "ok"
        assert await _ask(host_port) == "host"
        assert (await probe(f"http://127.0.0.1:{sibling}/")).startswith("err")
        assert (await probe(f"http://127.0.0.1:{ports[0]}/")).startswith("err")
        assert (await probe(f"http://127.0.0.1:{host_port}/")).startswith("err")
        if address:
            assert await _ask(host_port, host=address) == "host"
            assert (await probe(f"http://{address}:{host_port}/")).startswith("err")
            assert (await probe(f"http://{address}:{sibling}/")).startswith("err")
        if gateway:
            # Unless told otherwise pasta maps the gateway's address onto this machine's loopback.
            assert (await probe(f"http://{gateway}:{host_port}/")).startswith("err")
        assert (await probe("https://example.com/")).startswith("err")
    finally:
        platform.close()
        listener.close()
        groups = [s.proc.pid for s in servers.servers.values()]
        servers.kill_all()
        for pgid in groups:
            with pytest.raises(ProcessLookupError):
                for _ in range(40):
                    os.killpg(pgid, 0)
                    await asyncio.sleep(0.1)
                raise ProcessLookupError
