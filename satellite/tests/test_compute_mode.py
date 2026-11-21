"""Compute mode: this process runs the platform's own sessions in the
platform's sandbox (``sessions/sandbox_host.py``).

A compute start writes nothing into the agents folder, spawns the rendered
command with the plan's environment and no cwd of this machine's,
translates no path, scans no file and wipes nothing at close. The link
takes no code, no files, no tools and no lease; a paired satellite refuses
to start on the platform's marked folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from satellite import config as satconfig
from satellite._vendored import sandbox_render as render
from satellite.config import SatelliteConfig, load_config
from satellite.sessions import sandbox_host
from satellite.sessions.cli_session import CLISession
from satellite.sessions.codex_session import CodexSession
from satellite.sessions.session_manager import SessionManager
from satellite.terminal.codex_pty_session import CodexPtySession
from satellite.terminal.pty_session import PtySession

PORT = 8400
CLAUDE_DIR = "users/alice/.claude"
CODEX_DIR = "users/alice/.codex"
PROMPT_NAME = "system-prompt-abc.json"


def _description(agents: Path, mcps: Path) -> dict:
    return render.SandboxDescription(
        agent="personal-assistant",
        binds=(render.Bind(render.ROOT_AGENT, "users/alice", "/users/alice", True),
               render.Bind(render.ROOT_EMPTY, "", "/users/alice/memory", False)),
        mcp_binds=("custom/memory-mcp",), mcps_root=str(mcps), extra_binds=(),
        cwd="/users/alice", proxy_port=PORT, forwards=(render.PROXY_FORWARD, "8931"),
        allow_hosts=(), tmp_size_mb=4096,
    ).to_dict()


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    agents = tmp_path / "platform" / "agents"
    agent = agents / "personal-assistant"
    for sub in (CLAUDE_DIR, CODEX_DIR, "workspace", "config"):
        (agent / sub).mkdir(parents=True)
    (agent / CLAUDE_DIR / PROMPT_NAME).write_text("{}")
    mcps = tmp_path / "platform" / "mcps"
    (mcps / "custom" / "memory-mcp").mkdir(parents=True)
    return agents, mcps, agent


def _agent_stub(monkeypatch, order: list, render_fn: str) -> None:
    """ssh_agent's entry points as recorders: ``order`` gets ``"agent"`` at
    start, ``"render"`` when the render runs and ``("stop", sid)`` at close;
    the start answers this machine's paths, which the plan's env outranks."""
    from satellite.sessions import ssh_agent

    def _start(_sid, _config):
        order.append("agent")
        return {"SSH_AUTH_SOCK": "/home/x/.oto-dock/run/ssh-agent/s1.sock", "OTO_SSH_KEY_DIR": "/home/x/ssh"}
    real_render = getattr(sandbox_host, render_fn)

    async def _render(*a, **k):
        order.append("render")
        return await real_render(*a, **k)
    monkeypatch.setattr(ssh_agent, "start", _start)
    monkeypatch.setattr(ssh_agent, "stop", lambda sid: order.append(("stop", sid)))
    monkeypatch.setattr(sandbox_host, render_fn, _render)


def _snapshot(folder: Path) -> dict[str, int]:
    out = {}
    for p in folder.rglob("*"):
        out[str(p)] = p.stat().st_mtime_ns if p.is_file() else -1
    return out


@pytest.fixture
def compute(tmp_path, monkeypatch) -> SatelliteConfig:
    agents, mcps, _agent = _tree(tmp_path)
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: tmp_path / "machine" / ".oto-dock")
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    monkeypatch.setattr(render, "mem_total_mb", lambda: 16000)
    monkeypatch.setattr(render, "host_resolv_has_loopback_ns", lambda *_a: True)

    async def _bin(name, _hint, for_pty=False):
        return f"/opt/{name}/bin/{name}"
    monkeypatch.setattr("satellite.host.cli_versions.resolve_spawn_bin_async", _bin)
    return SatelliteConfig(machine_id="c-1", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                           agents_dir=agents, mcps_dir=mcps, compute=True)


def _payload(cfg: SatelliteConfig, **kw) -> dict:
    base = {
        "sandbox": _description(cfg.agents_dir, cfg.mcps_dir),
        "argv": ["claude", "-p", "--session-id", "s-1", "--append-system-prompt-file",
                 f"/{CLAUDE_DIR}/{PROMPT_NAME}"],
        "env": {"PROXY_URL": f"http://127.0.0.1:{PORT}", "PROXY_API_KEY": "jwt", "OTO_SESSION_ID": "s-1",
                "CLAUDE_CONFIG_DIR": f"/{CLAUDE_DIR}", "PATH": "/usr/bin:/bin",
                "SSH_AUTH_SOCK": render.SSH_AGENT_SOCKET, "OTO_SSH_KEY_DIR": f"/{CLAUDE_DIR}/ssh"},
        "ssh_agent": {},
        "cwd": "/users/alice", "tunnel_port": 43210, "cwd_relative": "users/alice",
        "claude_dir_relative": CLAUDE_DIR, "codex_dir_relative": CODEX_DIR,
        "model": "m", "effort": "high", "permission_mode": "auto", "resume": False,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# the configuration and the render
# ---------------------------------------------------------------------------

def test_the_conf_names_the_kind(tmp_path):
    conf = tmp_path / "satellite.conf"
    conf.write_text("[satellite]\nmachine_id = c-1\nmachine_secret = s\n"
                    "platform_url = ws://127.0.0.1:8400/v1/satellite\n"
                    f"agents_dir = {tmp_path}/a\nmcps_dir = {tmp_path}/m\nkind = compute\n")
    assert load_config(conf).compute is True
    conf.write_text(conf.read_text().replace("kind = compute\n", ""))
    assert load_config(conf).compute is False


@pytest.mark.asyncio
async def test_the_render_uses_this_machines_values_around_the_plans_argv(compute, tmp_path):
    full = await sandbox_host.spawn_command(_payload(compute), compute, "claude")
    # The render runs below the satellite's priority, behind the raise of
    # the session's OOM score.
    nice = sandbox_host.niced([])
    assert nice[1:] == ["-n", "10"]
    assert full[:7] == [*nice, "/bin/sh", "-c", sandbox_host._RAISE_OOM, "oto-session"]
    cmd = full[7:]
    assert cmd[0] == str(sandbox_host.launcher_path())
    assert cmd[1:3] == ["--beneath", str((compute.agents_dir / "personal-assistant").resolve())]
    i = cmd.index("--forward")
    assert cmd[i + 1] == f"{PORT}:43210"
    assert ["--dns-forward", render.DNS_FORWARD_ADDR] == cmd[cmd.index("--dns-forward"):cmd.index("--dns-forward") + 2]
    resolv = tmp_path / "machine" / ".oto-dock" / "compute" / "netns-resolv.conf"
    assert resolv.read_text() == f"nameserver {render.DNS_FORWARD_ADDR}\n"
    assert str(tmp_path / "machine" / ".oto-dock" / "compute" / ".empty") in cmd
    assert ["--size", str(4096 * 1024 * 1024), "--tmpfs", "/tmp"] == cmd[cmd.index("--size"):cmd.index("--size") + 4]
    assert ["--uid", str(os.getuid())] == cmd[cmd.index("--uid"):cmd.index("--uid") + 2]
    # The plan's argv after the separator, the binary resolved to this machine's install.
    sep = len(cmd) - 1 - cmd[::-1].index("--")
    assert cmd[sep + 1:] == ["/opt/claude/bin/claude", "-p", "--session-id", "s-1",
                             "--append-system-prompt-file", f"/{CLAUDE_DIR}/{PROMPT_NAME}"]
    prefix, codex_bin = await sandbox_host.sandbox_prefix(_payload(compute), compute, "codex")
    assert prefix[-1] == "--" and codex_bin == "/opt/codex/bin/codex"
    assert prefix[:7] == [*nice, "/bin/sh", "-c", sandbox_host._RAISE_OOM, "oto-session"]


def test_the_preflight_refuses_missing_folders_before_any_probe(compute, monkeypatch):
    monkeypatch.setattr(render, "preflight", lambda *_a, **_k: pytest.fail("probed"))
    gone = SatelliteConfig(machine_id="c", machine_secret="s", platform_url="ws://x/",
                           agents_dir=compute.agents_dir / "nope", mcps_dir=compute.mcps_dir, compute=True)
    assert "agents folder" in sandbox_host.preflight(gone)
    monkeypatch.setattr(render, "preflight", lambda *_a, **_k: None)
    assert sandbox_host.preflight(compute) is None


def test_the_spawn_env_is_the_plans_exactly(compute, monkeypatch):
    monkeypatch.setenv("HOME", "/home/somebody")
    monkeypatch.setenv("SECRET_OF_THE_BOX", "x")
    env = sandbox_host.spawn_env(_payload(compute))
    assert env == _payload(compute)["env"] and "HOME" not in env and "SECRET_OF_THE_BOX" not in env


# ---------------------------------------------------------------------------
# the sessions
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.stdin = SimpleNamespace(write=lambda b: None, drain=AsyncMock(), close=lambda: None)
        self.stdout = None
        self.stderr = None

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_a_claude_compute_start_spawns_the_render_and_writes_nothing(compute, monkeypatch):
    agent = compute.agents_dir / "personal-assistant"
    before = _snapshot(agent)
    spawned: dict = {}

    async def _exec(*cmd, **kw):
        spawned["cmd"] = list(cmd)
        spawned.update(kw)
        return _Proc()
    monkeypatch.setattr("satellite.sessions.cli_session.asyncio.create_subprocess_exec", _exec)
    monkeypatch.setattr("satellite.host.path_translator.translate_paths_in_text",
                        lambda *_a, **_k: pytest.fail("a compute session translated a path"))
    from satellite.sessions import session_files
    monkeypatch.setattr(session_files, "wipe", lambda _sid: pytest.fail("a compute session wiped files"))
    order: list = []
    _agent_stub(monkeypatch, order, "spawn_command")
    payload = _payload(compute)
    session = CLISession("s-1", agent, payload, compute)
    await session.start()
    assert order == ["agent", "render"]
    # The render, below the satellite's priority and behind the raise of the
    # session's OOM score.
    assert spawned["cmd"][:6] == [*sandbox_host.niced([]), "/bin/sh", "-c", sandbox_host._RAISE_OOM]
    assert spawned["cmd"][7] == str(sandbox_host.launcher_path()) and spawned["cmd"][-5:] == payload["argv"][1:]
    assert spawned["cmd"][-6] == "/opt/claude/bin/claude"
    assert spawned["env"] == payload["env"] and "cwd" not in spawned
    assert session.pid == 4242 and session._claude_dir is None
    assert _snapshot(agent) == before, "the compute start wrote into the platform's folder"
    assert session.detect_file_changes() == []
    # The steer goes to the pipe untranslated.
    session._turn_active = True
    assert await session.steer("read /users/alice/workspace/a.txt") is True
    await session.close()
    assert order == ["agent", "render", ("stop", "s-1")]
    assert (agent / CLAUDE_DIR / PROMPT_NAME).is_file()
    assert _snapshot(agent) == before


class _Client:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.proc = SimpleNamespace(pid=77, returncode=None)
        self.is_alive = True
        self.turn_done = None  # the session's main-turn event, set by a test that runs a turn

    async def request(self, method, params, timeout=None):
        self.requests.append((method, params))
        if method == "turn/start" and self.turn_done is not None:
            self.turn_done.set()
            return {"turn": {"id": "turn-9"}}
        return {"thread": {"id": "t-new"}}

    async def close(self):
        self.proc.returncode = 0

    def set_server_request_handler(self, _h):
        pass

    async def start(self, _init):
        return None


@pytest.mark.asyncio
async def test_a_codex_compute_start_runs_the_daemon_in_the_render_against_the_platforms_files(compute, monkeypatch):
    agent = compute.agents_dir / "personal-assistant"
    (agent / CODEX_DIR / "config.toml").write_text("# the platform's\n")
    before = _snapshot(agent)
    made: dict = {}

    class _Recorder(_Client):
        def __init__(self, *, env, cwd=None, sandbox_cmd_prefix=None, codex_bin=None, label=""):
            super().__init__()
            made.update(env=env, cwd=cwd, prefix=sandbox_cmd_prefix, codex_bin=codex_bin)
    monkeypatch.setattr("satellite.sessions.codex_session.AppServerClient", _Recorder)
    order: list = []
    _agent_stub(monkeypatch, order, "sandbox_prefix")
    payload = _payload(compute, argv=["codex"], mcp_server_names=["memory", "display"],
                       local_model=True, codex_hooks_floor=True, sandbox_mode="workspace-write")
    session = CodexSession("s-2", agent, payload, compute)
    with patch.object(session, "_warm_mcps", new=AsyncMock()), \
            patch.object(session, "_run_forwarder", new=AsyncMock()):
        await session.start()
        assert order == ["agent", "render"]
        assert made["cwd"] is None and made["codex_bin"] == "/opt/codex/bin/codex"
        assert made["prefix"][:6] == [*sandbox_host.niced([]), "/bin/sh", "-c", sandbox_host._RAISE_OOM]
        assert made["prefix"][7] == str(sandbox_host.launcher_path()) and made["prefix"][-1] == "--"
        assert made["env"] == payload["env"]
        method, params = session._client.requests[0]
        assert method == "thread/start" and params["cwd"] == "/users/alice"
        assert params["config"] == {"bypass_hook_trust": True}
        assert session._proxy_url == "http://127.0.0.1:43210" and session._proxy_api_key == "jwt"
        assert session.mcp_server_names == ["memory", "display"] and session._local_model is True
        assert session.thread_id == "t-new" and session.detect_file_changes() == []
        # A steer's paths are the sandbox's own: sent as they are, never
        # rewritten to this machine's folder.
        session._current_turn_id = "turn-1"
        monkeypatch.setattr("satellite.host.path_translator.translate_paths_in_text",
                            lambda *_a, **_k: pytest.fail("a compute session translated a path"))
        assert await session.steer("read /users/alice/workspace/a.txt") is True
        method, params = session._client.requests[-1]
        assert method == "turn/steer" and "/users/alice/workspace/a.txt" in json.dumps(params)
        # The turn's prompt the same: the daemon runs in the sandbox, where
        # the paths are right as sent.
        session._client.turn_done = session._main_turn_done
        await session.run_turn("read /users/alice/workspace/b.txt")
        starts = [(m, p) for m, p in session._client.requests if m == "turn/start"]
        assert starts and starts[-1][1]["input"][0]["text"].endswith("read /users/alice/workspace/b.txt")
        assert starts[-1][1]["sandboxPolicy"] is not None
        await session.close()
    assert order == ["agent", "render", ("stop", "s-2")]
    assert _snapshot(agent) == before, "the compute start wrote into the platform's folder"


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)

    async def enqueue_pty(self, msg):
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_a_claude_terminal_on_compute_spawns_the_plan_with_no_cwd(compute, monkeypatch):
    agent = compute.agents_dir / "personal-assistant"
    before = _snapshot(agent)
    wired: dict = {}

    async def _wire(self, cmd, env, cwd):
        wired.update(cmd=cmd, env=env, cwd=cwd)
        self.pid = 9
    monkeypatch.setattr(PtySession, "_wire_and_spawn", _wire)
    payload = _payload(compute, argv=["claude", "--permission-mode", "default"], resume=True,
                       session_id_for_resume="s-old", interactive_theme="light")
    session = PtySession("s-3", agent, payload, compute, _Ws())
    await session.start()
    assert wired["cwd"] is None and wired["env"] == payload["env"]
    assert wired["cmd"][-3:] == ["/opt/claude/bin/claude", "--permission-mode", "default"]
    assert session._claude_dir == agent / CLAUDE_DIR
    assert session._transcript_target_id == "s-old" and session._transcript_skip_existing
    await session.close()
    assert _snapshot(agent) == before


@pytest.mark.asyncio
async def test_a_codex_terminal_on_compute_resets_no_state_and_matches_the_rollout_by_the_sandbox_cwd(
        compute, monkeypatch):
    agent = compute.agents_dir / "personal-assistant"
    sessions_dir = agent / CODEX_DIR / "sessions" / "2026"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-2026-11-08T10-00-00-t-9.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "cwd": "/users/alice", "originator": "codex-tui", "id": "t-9"}}) + "\n")
    before = _snapshot(agent)
    wired: dict = {}

    async def _wire(self, cmd, env, cwd):
        wired.update(cmd=cmd, env=env, cwd=cwd)
    monkeypatch.setattr(CodexPtySession, "_wire_and_spawn", _wire)
    payload = _payload(compute, argv=["codex", "--no-alt-screen", "-C", "/users/alice", "hello"])
    session = CodexPtySession("s-4", agent, payload, compute, _Ws())
    await session.start()
    assert wired["cwd"] is None and wired["env"] == payload["env"]
    assert wired["cmd"][-5:] == ["/opt/codex/bin/codex", "--no-alt-screen", "-C", "/users/alice", "hello"]
    session._started_at = 0.0
    assert session._locate_transcript_file() == rollout.relative_to(agent).as_posix()
    await session.close()
    assert _snapshot(agent) == before


def test_the_manager_makes_no_agent_folder_on_compute(compute):
    sm = SessionManager(compute)
    assert sm._agent_dir_for("personal-assistant") == compute.agents_dir / "personal-assistant"
    with pytest.raises(ValueError):
        sm._agent_dir_for("unknown-agent")
    assert not (compute.agents_dir / "unknown-agent").exists()
    sm.cleanup_orphans()  # nothing of the platform's folder is swept


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_link_refuses_files_tools_leases_and_code_on_compute(compute, monkeypatch):
    from satellite.transport import ws_client
    sm = SessionManager(compute)
    client = ws_client.SatelliteWSClient(compute, sm)
    sent: list[dict] = []

    async def _send(msg):
        sent.append(msg)
    monkeypatch.setattr(client, "enqueue_send", _send)
    monkeypatch.setattr(sm, "file_push", AsyncMock(side_effect=AssertionError("a file reached a compute machine")))
    monkeypatch.setattr(sm, "sync_mcps", AsyncMock(side_effect=AssertionError("a tool reached a compute machine")))
    await client._dispatch_frame("file_push", {"command_id": "c-1", "agent_slug": "a"})
    await client._dispatch_frame("sync_mcps", {"command_id": "c-2"})
    lease = {"type": "mcp_gateway_token", "command_id": "c-3", "session_id": "s", "mcp": "github-mcp", "token_hash": "h" * 64,
             "header": "Authorization", "value": "Bearer v", "upstream": "https://api.example.test", "expires_in": 60}
    await client._dispatch_frame("mcp_gateway_token", lease)
    assert [m["command_id"] for m in sent] == ["c-1", "c-2", "c-3"]
    assert all(m["status"] == "error" and "compute machine" in m["error"] for m in sent)
    # The exit's values ride the same frame keyed by a placeholder: taken into
    # the table the machine's exit reads, and a wipe takes them out again.
    from types import SimpleNamespace

    from satellite.transport import mcp_gateway as gw
    table = gw.GatewayTable()
    client.tunnel = SimpleNamespace(gateway=SimpleNamespace(table=table))
    placeholder = "oto-exit-" + "a" * 32
    sent.clear()
    await client._dispatch_frame("mcp_gateway_token", {**lease, "command_id": "c-4", "mcp": placeholder,
                                                       "header": "xi-api-key", "value": "v"})
    assert sent[-1]["command_id"] == "c-4" and sent[-1]["status"] == "ok"
    assert table.get("h" * 64, placeholder) is not None
    await client._dispatch_frame("mcp_gateway_wipe", {"type": "mcp_gateway_wipe", "session_id": "s", "token_hash": "h" * 64})
    assert table.get("h" * 64, placeholder) is None
    client.tunnel = None
    # A request the platform waits on by its own id gets its answer frame
    # with the error, so the wait ends now and not at its stall.
    sent.clear()
    await client._dispatch_frame("file_pull", {"request_id": "r-1", "path_kind": "agent_tree",
                                               "agent_slug": "a", "path": "workspace/x.docx"})
    await client._dispatch_frame("request_manifest", {"request_id": "r-2", "agent_slug": "a"})
    assert [(m["type"], m["request_id"]) for m in sent] == [("file_content", "r-1"), ("file_manifest", "r-2")]
    assert all("compute machine" in m["error"] for m in sent) and sent[0]["path"] == "workspace/x.docx"
    sent.clear()
    # A step is a sandboxed run on a compute machine: one without its sandbox
    # is refused by the handler, never run in the agent's folder.
    await sm.step_run({"command_id": "c-3", "delivery_id": "d"}, client)
    assert sent and sent[0]["command_id"] == "c-3" and sent[0]["status"] == "error"
    monkeypatch.setattr(ws_client, "_self_uninstall_and_exit",
                        AsyncMock(side_effect=AssertionError("a compute machine removed itself")))
    monkeypatch.setattr(ws_client, "_self_update_and_restart",
                        AsyncMock(side_effect=AssertionError("a compute machine took code")))
    await client._dispatch_frame("uninstall", {})
    await client._dispatch_frame("update_required", {"target_version": "9"})
    assert client._takes_no_code is True


@pytest.mark.asyncio
async def test_the_sign_in_claims_compute(compute):
    from satellite.transport import ws_client
    sm = SessionManager(compute)
    client = ws_client.SatelliteWSClient(compute, sm)
    sent: list[str] = []

    class _Socket:
        async def send(self, text):
            sent.append(text)

        async def recv(self):
            raise ConnectionError("enough")
    client._ws = _Socket()
    with pytest.raises(ConnectionError):
        await client._authenticate()
    auth = json.loads(sent[0])
    assert auth["capabilities"]["compute"] is True


def _refusing_socket(reason: str):
    class _Socket:
        async def send(self, text):
            pass

        async def recv(self):
            return json.dumps({"type": "auth_result", "status": "rejected", "reason": reason})
    return _Socket()


@pytest.mark.asyncio
async def test_a_secret_the_platform_refuses_ends_a_compute_machine_to_read_its_sign_in_again(
        compute, monkeypatch, tmp_path):
    """A restore or a lost volume makes the proxy write a new sign-in at its
    boot; a compute machine that read the old one ends with its own exit code,
    and its start (the container's or the unit's restart) reads the new one. A
    paired machine, and a compute machine refused for another reason (an
    install on local), keep knocking."""
    from satellite.transport import compute_link, ws_client
    client = ws_client.SatelliteWSClient(compute, SessionManager(compute))
    client._ws = _refusing_socket("Invalid credentials")
    with pytest.raises(compute_link.ComputeSignInRefused):
        await client._authenticate()
    client._ws = _refusing_socket("This install does not run sessions on compute machines.")
    with pytest.raises(ConnectionError):
        await client._authenticate()
    paired = SatelliteConfig(machine_id="m", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                             agents_dir=tmp_path / "a", mcps_dir=tmp_path / "m")
    other = ws_client.SatelliteWSClient(paired, SessionManager(paired))
    other._ws = _refusing_socket("Invalid credentials")
    with pytest.raises(ConnectionError) as refused:
        await other._authenticate()
    assert not isinstance(refused.value, compute_link.ComputeSignInRefused)

    # The loop ends with the code the start reads it by.
    class _Connect:
        async def __aenter__(self):
            return _refusing_socket("Invalid credentials")

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(ws_client.websockets, "connect", lambda *a, **kw: _Connect())

    async def _no_check(_why):
        return None
    monkeypatch.setattr(client, "_check_tunnel", _no_check)
    await asyncio.wait_for(client.connect_forever(), 5)
    assert client.exit_code == compute_link.COMPUTE_REREAD_EXIT != 0


def _refusing_uninstall():
    class _Socket:
        async def send(self, text):
            pass

        async def recv(self):
            return json.dumps({"type": "auth_result", "status": "rejected", "reason": "machine_deleted",
                               "action": "uninstall"})
    return _Socket()


@pytest.mark.asyncio
async def test_a_deleted_row_ends_a_compute_machine_to_read_its_sign_in_again(compute, monkeypatch):
    """A restore brings another install's database: the credential the
    compute machine read names a row that is gone, which the platform says
    with an uninstall answer and close 4006. A compute machine never
    uninstalls itself: it ends with the code its start reads the credential
    again by."""
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close

    from satellite.transport import compute_link, ws_client
    client = ws_client.SatelliteWSClient(compute, SessionManager(compute))
    client._ws = _refusing_uninstall()
    with pytest.raises(compute_link.ComputeSignInRefused):
        await client._authenticate()

    async def _no_check(_why):
        return None
    monkeypatch.setattr(client, "_check_tunnel", _no_check)
    uninstalled: list[bool] = []

    async def _uninstall():
        uninstalled.append(True)
    monkeypatch.setattr(ws_client, "_self_uninstall_and_exit", _uninstall)

    class _Closed:
        async def __aenter__(self):
            raise ConnectionClosed(Close(4006, "machine_deleted"), None)

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(ws_client.websockets, "connect", lambda *a, **kw: _Closed())
    await asyncio.wait_for(client.connect_forever(), 5)
    assert client.exit_code == compute_link.COMPUTE_REREAD_EXIT and not uninstalled


@pytest.mark.asyncio
@pytest.mark.parametrize("own_copy", [True, False])
async def test_a_refused_sign_in_on_another_host_is_left_for_its_start(compute, tmp_path, monkeypatch, own_copy):
    """A machine on another host whose sign-in the platform refused leaves
    its mark, so its start waits for a new sign-in instead of restarting
    into the same refusal; one beside the platform reads the credential the
    proxy writes again."""
    import dataclasses
    import hashlib

    from satellite.transport import compute_link, ws_client
    config = dataclasses.replace(compute, own_copy=own_copy)
    client = ws_client.SatelliteWSClient(config, SessionManager(config))

    class _Connect:
        async def __aenter__(self):
            return _refusing_socket("Invalid credentials")

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(ws_client.websockets, "connect", lambda *a, **kw: _Connect())

    async def _no_check(_why):
        return None
    monkeypatch.setattr(client, "_check_tunnel", _no_check)
    await asyncio.wait_for(client.connect_forever(), 5)
    assert client.exit_code == compute_link.COMPUTE_REREAD_EXIT
    mark = sandbox_host.otodock_dir() / compute_link.REFUSED_FILE
    assert compute_link.sign_in_mark("c-1", "s") == hashlib.sha256(b"c-1\ns").hexdigest()
    if own_copy:
        assert mark.read_text().strip() == compute_link.sign_in_mark("c-1", "s")
    else:
        assert not mark.exists()


@pytest.mark.asyncio
async def test_a_removed_machine_with_its_own_copy_leaves_no_mark_at_the_close(compute, monkeypatch):
    """The close 4006 comes before the platform's delete commits: a delete
    that then fails leaves the row and its sign-in good, so the close marks
    nothing. The next sign-in of a machine really removed is refused and
    marks it then."""
    import dataclasses

    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close

    from satellite.transport import compute_link, ws_client
    own = dataclasses.replace(compute, own_copy=True)
    client = ws_client.SatelliteWSClient(own, SessionManager(own))

    async def _no_check(_why):
        return None
    monkeypatch.setattr(client, "_check_tunnel", _no_check)

    class _Closed:
        async def __aenter__(self):
            raise ConnectionClosed(Close(4006, "machine_deleted"), None)

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(ws_client.websockets, "connect", lambda *a, **kw: _Closed())
    await asyncio.wait_for(client.connect_forever(), 5)
    assert client.exit_code == compute_link.COMPUTE_REREAD_EXIT
    assert not (sandbox_host.otodock_dir() / compute_link.REFUSED_FILE).exists()


def _answer(resp: dict):
    class _Socket:
        async def send(self, text):
            pass

        async def recv(self):
            return json.dumps({"type": "auth_result", "status": "rejected", **resp})
    return _Socket()


@pytest.fixture
def hosted(monkeypatch):
    """A machine on another server started by its host service: the host's
    shape and the image it runs, in its environment."""
    from satellite.transport import compute_link
    monkeypatch.setenv(compute_link.HOST_SHAPE_ENV, "1")
    monkeypatch.setenv(compute_link.IMAGE_ENV, "ghcr.io/otodock/otodock-proxy:2.0.0")


def test_a_machine_on_another_server_claims_its_image_and_its_host_shape(compute, hosted):
    import dataclasses

    from satellite.transport import compute_link
    own = dataclasses.replace(compute, own_copy=True)
    assert compute_link.claims(own) == {"compute": True, "compute_files": "own", "compute_host": 1,
                                        "compute_image": "ghcr.io/otodock/otodock-proxy:2.0.0"}
    # One beside the platform claims no image and no host: its version is the install's.
    assert compute_link.claims(compute) == {"compute": True, "compute_files": "shared"}


@pytest.mark.parametrize("word", ["", "x", "-1", "12345", "1 2"])
def test_a_host_shape_that_is_not_a_small_number_is_no_host(compute, monkeypatch, word):
    import dataclasses

    from satellite.transport import compute_link
    monkeypatch.setenv(compute_link.HOST_SHAPE_ENV, word)
    own = dataclasses.replace(compute, own_copy=True)
    assert compute_link.host_shape(own) == 0 and "compute_host" not in compute_link.claims(own)


@pytest.mark.asyncio
async def test_a_new_image_tag_rings_the_host_service_with_no_refused_mark(compute, hosted, monkeypatch, tmp_path):
    """The platform names a new tag: the machine ends with 76 and writes
    nothing (its host asks the platform which tag), and leaves no
    refused-sign-in mark, so its start on the new image signs in."""
    import dataclasses

    from satellite.transport import compute_link, ws_client
    own = dataclasses.replace(compute, own_copy=True)
    client = ws_client.SatelliteWSClient(own, SessionManager(own))

    class _Connect:
        async def __aenter__(self):
            return _answer({"reason": "update", "action": "update_image", "image_tag": "../evil"})

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(ws_client.websockets, "connect", lambda *a, **kw: _Connect())

    async def _no_check(_why):
        return None
    monkeypatch.setattr(client, "_check_tunnel", _no_check)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    await asyncio.wait_for(client.connect_forever(), 5)
    assert client.exit_code == compute_link.UPDATE_EXIT == 76
    assert not (sandbox_host.otodock_dir() / compute_link.REFUSED_FILE).exists()
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_no_doorbell_without_the_host_service(compute, monkeypatch):
    """A compute machine started otherwise, or one beside the platform,
    never ends for a tag: it signs in again later."""
    import dataclasses

    from satellite.transport import compute_link
    monkeypatch.delenv(compute_link.HOST_SHAPE_ENV, raising=False)
    own = dataclasses.replace(compute, own_copy=True)
    assert compute_link.refusal(own, {"action": "update_image", "image_tag": "2.0.1"}) is None
    monkeypatch.setenv(compute_link.HOST_SHAPE_ENV, "1")
    assert compute_link.refusal(compute, {"action": "update_image", "image_tag": "2.0.1"}) is None


@pytest.mark.asyncio
async def test_a_removed_machine_on_another_server_rings_its_host_service(compute, hosted):
    """Its sign-in finds no row: it ends with 77 and no refused mark; its
    host asks the platform before it removes anything. With no host
    service it ends as before (75)."""
    import dataclasses

    from satellite.transport import compute_link, ws_client
    own = dataclasses.replace(compute, own_copy=True)
    client = ws_client.SatelliteWSClient(own, SessionManager(own))
    client._ws = _refusing_uninstall()
    with pytest.raises(compute_link.ComputeSignInRefused) as refused:
        await client._authenticate()
    assert refused.value.exit_code == compute_link.REMOVED_EXIT == 77 and not refused.value.mark
    assert compute_link.ended(own, refused.value) == 77
    assert not (sandbox_host.otodock_dir() / compute_link.REFUSED_FILE).exists()


def test_a_refused_secret_still_leaves_its_mark_on_another_server(compute, hosted):
    import dataclasses

    from satellite.transport import compute_link
    own = dataclasses.replace(compute, own_copy=True)
    refused = compute_link.refusal(own, {"reason": compute_link.SECRET_REFUSED})
    assert refused is not None and refused.mark and refused.exit_code == compute_link.COMPUTE_REREAD_EXIT
    assert compute_link.ended(own, refused) == 75
    assert (sandbox_host.otodock_dir() / compute_link.REFUSED_FILE).exists()


def test_a_machine_on_another_server_takes_its_process_out_of_reach(monkeypatch):
    """``prctl(PR_SET_DUMPABLE, 0)``: the call the proxy makes for itself. A
    failing call is said and does not stop the machine."""
    import ctypes

    from satellite.transport import compute_link
    calls: list[tuple] = []

    class _Libc:
        def prctl(self, *args):
            calls.append(args)
            return 0
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: _Libc())
    assert compute_link.not_dumpable() is True
    assert calls == [(4, 0, 0, 0, 0)]

    class _Refusing:
        def prctl(self, *args):
            return -1
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: _Refusing())
    assert compute_link.not_dumpable() is False


def test_the_refused_mark_sits_where_the_start_reads_it():
    # Unpatched: the start reads ``~/.oto-dock/compute-refused``
    # (``proxy/compute_start.REFUSED``).
    from satellite.transport import compute_link
    assert compute_link.REFUSED_FILE == "compute-refused"
    assert sandbox_host.otodock_dir() == Path.home() / ".oto-dock"


def test_a_paired_satellite_never_starts_on_the_platforms_folder(compute, tmp_path):
    from satellite.__main__ import _refuse_platform_folder
    (compute.agents_dir / render.PLATFORM_MARKER).touch()
    paired = SatelliteConfig(machine_id="m", machine_secret="s", platform_url="ws://x/",
                             agents_dir=compute.agents_dir, mcps_dir=compute.mcps_dir)
    with pytest.raises(SystemExit):
        _refuse_platform_folder(paired)
    _refuse_platform_folder(compute)
    own = SatelliteConfig(machine_id="m", machine_secret="s", platform_url="ws://x/",
                          agents_dir=tmp_path / "own", mcps_dir=compute.mcps_dir)
    _refuse_platform_folder(own)


# ---------------------------------------------------------------------------
# one satellite per home folder
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]

# A start as its launcher makes it: a child imports the satellite under the
# launcher's HOME (the default config follows it, as in a real start) and
# runs the start's refusal on the --config the launcher passes. Nothing else
# of the start runs and no satellite signs in. ``compute`` names the config
# the way the compute machine's own start does (proxy/compute_start.py).
_START = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[2])\n"
    "import compute_start\n"
    "from satellite.__main__ import _refuse_second_satellite\n"
    "arg = sys.argv[1]\n"
    "path = compute_start.CONF if arg == 'compute' else (Path(arg) if arg else None)\n"
    "_refuse_second_satellite(path)\n"
    "_refuse_second_satellite(path)\n"
    "print('starts')\n"
)


def _conf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[satellite]\nmachine_id = m\nmachine_secret = s\nplatform_url = ws://x/\n")
    return path


def _start(home: Path, config: str) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_REPO)}
    return subprocess.run([sys.executable, "-c", _START, config, str(_REPO / "proxy")],
                          capture_output=True, text=True, timeout=60, env=env, cwd=home)


def _listing(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _starts(home: Path, config: str) -> None:
    before = _listing(home.parent)
    done = _start(home, config)
    assert done.returncode == 0 and done.stdout.strip() == "starts", done.stderr[-600:]
    # No lock and no pid file: a start leaves nothing a crash could leave behind.
    assert _listing(home.parent) == before


def test_a_persons_one_satellite_starts(tmp_path):
    # Every installer starts it with no --config (install.sh's unit and its
    # launchd plist, runner.ps1), and a relaunch after an update or a crash
    # runs the same command: the HOME's own config.
    home = tmp_path / "alice"
    _conf(home / ".oto-dock" / "satellite.conf")
    _starts(home, "")
    _starts(home, "")
    # Its own file named outright, or through a link to the home, is its own.
    _starts(home, str(home / ".oto-dock" / "satellite.conf"))
    (tmp_path / "link").symlink_to(home)
    _starts(home, str(tmp_path / "link" / ".oto-dock" / "satellite.conf"))


def test_a_bare_metal_compute_unit_starts_beside_its_users_own_satellite(tmp_path):
    # The unit runs under the same OS user as that user's own satellite, with
    # HOME=/var/lib/otodock-compute (proxy/compute.service): its config is
    # its HOME's own, so the person's ~/.oto-dock/satellite.conf is not its.
    person = tmp_path / "owner"
    _conf(person / ".oto-dock" / "satellite.conf")
    unit_home = tmp_path / "var-lib-otodock-compute"
    _conf(unit_home / ".oto-dock" / "satellite.conf")
    _starts(unit_home, "compute")
    _starts(person, "")


def test_a_compute_container_starts(tmp_path):
    # The container's start writes its config under the container's HOME and
    # names it: the first start finds no file there yet, a restart finds its own.
    home = tmp_path / "container"
    home.mkdir()
    _starts(home, "compute")
    _conf(home / ".oto-dock" / "satellite.conf")
    _starts(home, "compute")


def test_a_drill_with_a_home_of_its_own_starts(tmp_path):
    home = tmp_path / "drill-home"
    home.mkdir()
    _starts(home, str(_conf(tmp_path / "drill" / "satellite.conf")))
    _starts(home, str(_conf(home / ".oto-dock" / "satellite.conf")))


def test_the_start_refuses_a_second_satellite_before_its_other_checks(tmp_path, monkeypatch):
    # The start itself asks: a config that is not the HOME's own ends it
    # with code 2 right after the config is read. The next check stands in
    # as a wall, so nothing past the refusal runs here either way.
    import satellite.__main__ as main_mod

    class Past(Exception):
        pass

    def past(_config):
        raise Past
    own = _conf(tmp_path / "home" / ".oto-dock" / "satellite.conf")
    other = _conf(tmp_path / "drill" / "satellite.conf")
    monkeypatch.setattr(satconfig, "_DEFAULT_CONFIG_PATH", own)
    monkeypatch.setattr(main_mod, "_verify_installer_drift", lambda: None)
    monkeypatch.setattr(main_mod, "_refuse_platform_folder", past)
    with pytest.raises(SystemExit) as refused:
        asyncio.run(main_mod._main(other))
    assert refused.value.code == 2
    with pytest.raises(Past):
        asyncio.run(main_mod._main(own))
    with pytest.raises(Past):
        asyncio.run(main_mod._main(None))


def test_a_second_satellite_under_one_home_is_refused(tmp_path):
    # A drill paired to another install, started with the box's HOME: its
    # CLIs would land in the prefix the first satellite's chats run from.
    home = tmp_path / "owner"
    own = _conf(home / ".oto-dock" / "satellite.conf")
    other = _conf(tmp_path / "drill" / "satellite.conf")
    before = _listing(tmp_path)
    done = _start(home, str(other))
    assert done.returncode == 2 and "starts" not in done.stdout
    line = done.stderr.strip().splitlines()[-1]
    assert str(own) in line and "home folder of its own" in line
    # A leftover file of a satellite that is gone refuses too: the line says what to do.
    assert "If no satellite uses that file any more, remove it." in line
    assert ";" not in line and "—" not in line and " - " not in line
    assert _listing(tmp_path) == before


# ---------------------------------------------------------------------------
# the tunnel
# ---------------------------------------------------------------------------

def test_the_tunnel_forwards_the_gateway_path_and_translates_no_body_on_compute(compute):
    from satellite.transport.http_tunnel import LocalTunnelServer, _is_allowed_path
    ws = SimpleNamespace(config=compute, _authenticated=False, tunnel=None)
    tunnel = LocalTunnelServer(ws)
    assert tunnel._compute is True
    for path in ("/v1/mcp-gateway/github/repos", "/v1/knowledge-libraries", "/v1/mcps/local-package/validate",
                 "/v1/conversations/abc", "/v1/phone/call-control/hangup"):
        assert _is_allowed_path(path), path
    assert not _is_allowed_path("/v1/mcp-gateway/../admin")
    ws.config = SimpleNamespace(compute=False)
    assert tunnel._compute is False
    assert satconfig.SATELLITE_VERSION == "0.5.197"


def test_the_boot_refuses_a_render_or_a_launcher_that_is_not_the_proxys(monkeypatch):
    """The vendored render and the launcher are hashed at boot: a copy that
    differs from the proxy's exits with code 2 and never serves a session."""
    import satellite.__main__ as main_mod
    main_mod._verify_installer_drift()  # the shipped copies agree
    monkeypatch.setattr(main_mod, "SHARED_SANDBOX_RENDER_HASH", "0" * 64)
    with pytest.raises(SystemExit) as e:
        main_mod._verify_installer_drift()
    assert e.value.code == 2
    monkeypatch.undo()
    monkeypatch.setattr(main_mod, "SHARED_SANDBOX_NET_HASH", "0" * 64)
    with pytest.raises(SystemExit) as e:
        main_mod._verify_installer_drift()
    assert e.value.code == 2
    monkeypatch.undo()
    monkeypatch.setattr(main_mod, "SHARED_HOST_RESOURCES_HASH", "0" * 64)
    with pytest.raises(SystemExit) as e:
        main_mod._verify_installer_drift()
    assert e.value.code == 2


def test_a_compute_machine_reports_its_memory_and_a_paired_one_does_not(monkeypatch):
    """The admission gates of the platform read a compute machine's memory
    from its heartbeat (and its sign-in): the vendored reader's numbers, the
    limit read again each time. A paired machine sends none."""
    from satellite._vendored import host_resources
    from satellite.transport import ws_client
    monkeypatch.setattr(host_resources, "detect_memory_limit_bytes", lambda: 8 << 30)
    monkeypatch.setattr(host_resources, "_live_available", lambda: 5 << 30)
    monkeypatch.setattr(host_resources, "_proc_mem_total_bytes", lambda: 16 << 30)
    monkeypatch.setattr(host_resources, "swap_free_bytes", lambda: 0)
    monkeypatch.setattr(host_resources, "_capped_limit", lambda: (8 << 30, "v2", "/sys/fs/cgroup"))

    def frame(compute: bool) -> dict:
        client = object.__new__(ws_client.SatelliteWSClient)
        client._compute = compute
        client.sm = SimpleNamespace(sessions={}, pty_sessions={})
        client._agent_fingerprints = {}
        client.tunnel = None
        return client._heartbeat_frame()

    assert frame(True)["memory"] == {"total_bytes": 8 << 30, "available_bytes": 5 << 30,
                                     "swap_free_bytes": 0, "capped": True, "known": True}
    assert "memory" not in frame(False)
    monkeypatch.setattr(host_resources, "detect_memory_limit_bytes", lambda: 6 << 30)
    assert frame(True)["memory"]["total_bytes"] == 6 << 30  # a changed limit is read again


@pytest.mark.asyncio
async def test_a_compute_machine_drops_its_link_before_it_ends_its_sessions():
    """At its stop a compute machine ends its link first, so the platform
    reads a machine it lost and continues the turns at its return; a paired
    machine closes its sessions over the live link, as before."""
    import asyncio

    import satellite.__main__ as main_mod
    order: list[str] = []

    async def link():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("link")
            raise

    class _Session:
        async def close(self):
            order.append("session")

    for compute, first in ((True, "link"), (False, "session")):
        order.clear()
        task = asyncio.create_task(link())
        await asyncio.sleep(0)
        closed: list[str] = []
        sm = SimpleNamespace(sessions={"s-1": _Session()}, pty_sessions={"p-1": _Session()},
                             tool_relay=SimpleNamespace(close_all=closed.append))
        left = await main_mod._close_sessions(sm, {task}, compute=compute)
        await main_mod._cancel(left)
        assert order[0] == first and order.count("session") == 2, (compute, order)
        assert closed == ["the satellite stopped"]


@pytest.mark.asyncio
async def test_the_machine_takes_its_own_kind_of_session_alone(compute, tmp_path):
    """A compute machine refuses a paired payload (it would write the
    session's files into the platform's folder and spawn with no sandbox),
    and a paired machine refuses a compute payload, each with an ack error
    before anything is made."""
    ws = _Ws()
    sm = SessionManager(compute)
    paired_payload = {"cwd_relative": "users/alice", "claude_dir_relative": CLAUDE_DIR, "env": {}}
    await sm.start_session({"session_id": "s-x", "execution_path": "claude-code-cli", "config": paired_payload,
                            "command_id": "c-1", "agent_slug": "personal-assistant"}, ws)
    assert ws.sent[-1]["status"] == "error" and "compute sessions alone" in ws.sent[-1]["error"]
    assert "s-x" not in sm.sessions
    own = tmp_path / "own"
    (own / "agents").mkdir(parents=True)
    paired = SatelliteConfig(machine_id="m", machine_secret="s", platform_url="ws://x/",
                             agents_dir=own / "agents", mcps_dir=own / "mcps")
    sm2 = SessionManager(paired)
    ws2 = _Ws()
    await sm2.pty_open({"session_id": "s-y", "execution_path": "claude-code-cli", "config": _payload(compute),
                        "command_id": "c-2", "agent_slug": "personal-assistant"}, ws2)
    assert ws2.sent[-1]["status"] == "error" and "takes no compute session" in ws2.sent[-1]["error"]
    assert "s-y" not in sm2.pty_sessions


@pytest.mark.asyncio
async def test_a_payload_with_no_link_port_is_refused(compute):
    """Without a link port the render would splice the bare proxy port onto
    this host's loopback, the platform's own listener on bare metal."""
    payload = _payload(compute, tunnel_port=0)
    with pytest.raises(render.RenderRefused, match="link port"):
        await sandbox_host.spawn_command(payload, compute, "claude")
    with pytest.raises(render.RenderRefused, match="link port"):
        await sandbox_host.sandbox_prefix(payload, compute, "codex")


# -- a session's OOM score ------------------------------------------------------

def _score_of(cmd: list[str], env: dict | None = None) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True, env=env)
    return done.stdout.strip()


@pytest.mark.skipif(sys.platform != "linux", reason="the OOM score is Linux's")
def test_the_raise_gives_a_session_its_oom_score():
    """A real run, no privilege: the raise, then a process that reads its own
    score: the value every process of the session inherits."""
    assert _score_of(sandbox_host.with_oom_raise(["cat", "/proc/self/oom_score_adj"])) == "500"


@pytest.mark.skipif(sys.platform != "linux", reason="the OOM score is Linux's")
def test_the_raise_never_lowers_a_score_already_higher():
    wrapped = ["/bin/sh", "-c", 'echo 700 > /proc/self/oom_score_adj; exec "$@"', "x",
               *sandbox_host.with_oom_raise(["cat", "/proc/self/oom_score_adj"])]
    assert _score_of(wrapped) == "700"


@pytest.mark.skipif(sys.platform != "linux", reason="the OOM score is Linux's")
def test_the_raise_keeps_the_environment_and_the_exit_code():
    """The session's environment is the plan's; the shell execs the command,
    so its exit code is the command's."""
    env = {"PATH": "/usr/bin:/bin", "OTO_MARK": "kept"}
    out = _score_of(sandbox_host.with_oom_raise(["sh", "-c", 'echo "$OTO_MARK"']), env=env)
    assert out == "kept"
    assert subprocess.run(sandbox_host.with_oom_raise(["sh", "-c", "exit 7"]), check=False).returncode == 7


@pytest.mark.skipif(sys.platform != "linux", reason="the OOM score is Linux's")
def test_the_raise_is_silent_where_it_cannot_read_its_score():
    """A failing raise says nothing on the session's stderr and still runs
    the command."""
    cmd = list(sandbox_host.with_oom_raise(["sh", "-c", "echo ran"]))
    cmd[2] = cmd[2].replace("/proc/self/oom_score_adj", "/nonexistent/oom")
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True)
    assert done.stdout.strip() == "ran" and done.stderr == ""


# ---------------------------------------------------------------------------
# a compute machine on another host: its own copy of the folders
# ---------------------------------------------------------------------------

@pytest.fixture
def own(compute):
    import dataclasses
    return dataclasses.replace(compute, own_copy=True)


def test_the_conf_names_its_own_copy_with_the_kind_alone(tmp_path):
    conf = tmp_path / "satellite.conf"
    base = ("[satellite]\nmachine_id = c-1\nmachine_secret = s\nplatform_url = ws://10.0.0.2:8400/v1/satellite\n"
            f"agents_dir = {tmp_path}/a\nmcps_dir = {tmp_path}/m\n")
    conf.write_text(base + "kind = compute\nfiles = own\n")
    assert load_config(conf).own_copy is True
    conf.write_text(base + "files = own\n")
    assert load_config(conf).own_copy is False
    conf.write_text(base + "kind = compute\n")
    assert load_config(conf).own_copy is False


@pytest.mark.asyncio
async def test_the_link_of_a_machine_with_its_own_copy_takes_files_and_tools_but_no_move(own, monkeypatch):
    from satellite.transport import ws_client
    sm = SessionManager(own)
    client = ws_client.SatelliteWSClient(own, sm)
    sent: list[dict] = []

    async def _send(msg):
        sent.append(msg)
    monkeypatch.setattr(client, "enqueue_send", _send)
    taken: list[str] = []

    async def _took(msg, *_a):
        taken.append(msg.get("command_id"))
    monkeypatch.setattr(sm, "file_push", _took)
    monkeypatch.setattr(sm, "sync_mcps", _took)
    await client._dispatch_frame("file_push", {"command_id": "c-1", "agent_slug": "a"})
    await client._dispatch_frame("sync_mcps", {"command_id": "c-2"})
    await client._dispatch_frame("session_move", {"command_id": "c-3"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sorted(taken) == ["c-1", "c-2"]
    assert [m["command_id"] for m in sent] == ["c-3"] and sent[0]["status"] == "error"


@pytest.mark.asyncio
async def test_the_sign_in_claims_its_file_mode(compute, own):
    from satellite.transport import ws_client
    claims = []
    for cfg in (compute, own):
        client = ws_client.SatelliteWSClient(cfg, SessionManager(cfg))
        sent: list[str] = []

        class _Socket:
            async def send(self, text, sent=sent):
                sent.append(text)

            async def recv(self):
                raise ConnectionError("enough")
        client._ws = _Socket()
        with pytest.raises(ConnectionError):
            await client._authenticate()
        claims.append(json.loads(sent[0])["capabilities"]["compute_files"])
    assert claims == ["shared", "own"]


def test_a_machine_with_its_own_copy_makes_its_folders_and_counts_no_sessions(own, monkeypatch):
    sm = SessionManager(own)
    assert sm._agent_dir_for("new-agent").is_dir()
    # The platform admits a compute machine's sessions by memory: no count.
    monkeypatch.setattr("satellite.sessions.session_manager._recommended_max_sessions", lambda: 0)
    sm._check_capacity("s-9")


def _host_files(files: dict) -> dict:
    import base64
    return {k: {"content_b64": base64.b64encode(v.encode()).decode(), "mode": 0o640} for k, v in files.items()}


def test_the_prepared_files_land_in_its_own_agent_folder_and_go_with_their_last_session(own, monkeypatch, tmp_path):
    from satellite.sessions import session_files
    agent = own.agents_dir / "personal-assistant"
    outside = tmp_path / "outside"
    outside.mkdir()
    (agent / "users" / "alice" / "linked").symlink_to(outside)
    payload = {"host_files": _host_files({
        f"{CLAUDE_DIR}/settings.json": "{}",
        f"{CLAUDE_DIR}/bin/git": "#!/bin/sh",
        "../escape.txt": "x",
        "/etc/x": "x",
        "users/alice/linked/through.txt": "x",
    })}
    payload["host_folders"] = ["users/alice/.agents", "../out", "/etc/x", "users/alice/linked/made", ""]
    payload["host_files"][f"{CLAUDE_DIR}/bin/git"]["mode"] = 0o750
    assert session_files.materialize_agent_files(payload, "s-1", agent) == 2
    assert (agent / "users" / "alice" / ".agents").is_dir()
    assert not (own.agents_dir / "out").exists() and not (outside / "made").exists()
    assert (agent / CLAUDE_DIR / "settings.json").read_text() == "{}"
    # The frame's mode, the machine's umask applied: the owner's bits are the
    # frame's (the wrapper stays runnable, the settings stay unrunnable).
    assert oct((agent / CLAUDE_DIR / "bin" / "git").stat().st_mode & 0o700) == oct(0o700)
    assert oct((agent / CLAUDE_DIR / "settings.json").stat().st_mode & 0o700) == oct(0o600)
    assert not (own.agents_dir / "escape.txt").exists() and not (outside / "through.txt").exists()
    # A second session of the same scope uses the same settings: they stay
    # until its close too.
    session_files.materialize_agent_files(payload, "s-2", agent)
    session_files.wipe_agent_files("s-1")
    assert (agent / CLAUDE_DIR / "settings.json").exists()
    session_files.wipe_agent_files("s-2")
    assert not (agent / CLAUDE_DIR / "settings.json").exists()


def test_a_link_inside_the_agent_folder_is_never_written_through(own, tmp_path):
    # The link stays inside the folder (another person's), which the
    # folder check alone lets pass: the guard on the way refuses it.
    from satellite.sessions import session_files
    agent = own.agents_dir / "personal-assistant"
    (agent / "users" / "bob").mkdir(parents=True, exist_ok=True)
    (agent / "users" / "alice").mkdir(parents=True, exist_ok=True)
    (agent / "users" / "alice" / "linked").symlink_to("../bob")
    payload = {"host_files": _host_files({"users/alice/linked/through.txt": "x"}),
               "host_folders": ["users/alice/linked/made"]}
    assert session_files.materialize_agent_files(payload, "s-1", agent) == 0
    assert not (agent / "users" / "bob" / "through.txt").exists()
    assert not (agent / "users" / "bob" / "made").exists()


def test_the_close_removes_only_what_it_wrote_where_it_wrote_it(own):
    # A folder on the way swapped for a link after the start: the close
    # removes nothing through it.
    import shutil

    from satellite.sessions import session_files
    agent = own.agents_dir / "personal-assistant"
    session_files.materialize_agent_files(
        {"host_files": _host_files({"users/alice/.claude/settings.json": "{}"})}, "s-1", agent)
    bob = agent / "users" / "bob" / ".claude"
    bob.mkdir(parents=True, exist_ok=True)
    (bob / "settings.json").write_text("bob's")
    shutil.rmtree(agent / "users" / "alice" / ".claude")
    (agent / "users" / "alice" / ".claude").symlink_to("../bob/.claude")
    session_files.wipe_agent_files("s-1")
    assert (bob / "settings.json").read_text() == "bob's"


@pytest.mark.asyncio
async def test_a_claude_start_on_its_own_copy_writes_the_prepared_files_and_reports_changes(own, monkeypatch):
    agent = own.agents_dir / "personal-assistant"

    async def _exec(*cmd, **kw):
        return _Proc()
    monkeypatch.setattr("satellite.sessions.cli_session.asyncio.create_subprocess_exec", _exec)
    _agent_stub(monkeypatch, [], "spawn_command")
    files = _host_files({f"{CLAUDE_DIR}/settings.json": "{}"})
    session = CLISession("s-1", agent, _payload(own, host_files=files), own)
    await session.start()
    assert (agent / CLAUDE_DIR / "settings.json").is_file()
    (agent / "workspace" / "made.txt").write_text("done")
    assert [c["path"] for c in session.detect_file_changes()] == ["workspace/made.txt"]
    await session.close()
    assert not (agent / CLAUDE_DIR / "settings.json").exists()


def test_a_compute_machines_tool_python_is_one_its_sandbox_binds(own, tmp_path):
    """A community tool's venv lands on a uv-managed interpreter in the
    community folder (no new image removes it); a bundled tool may use the
    system's too."""
    community = sandbox_host.interpreter_roots(own, own.mcps_dir / "community" / "x.new")
    assert community == [(own.mcps_dir / "community" / ".uv-python").resolve(),
                         (own.mcps_dir / "community" / "x.new").resolve()]
    bundled = sandbox_host.interpreter_roots(own, own.mcps_dir / "custom" / "y.new")
    assert Path("/usr").resolve() in bundled
    assert own.mcps_dir.resolve() / ".uv-python" in bundled
    assert (own.mcps_dir / "custom" / "y.new").resolve() in bundled
    assert Path.home().resolve() not in bundled


def _build_args(argv):
    return [(a, argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]


def test_a_build_step_on_a_compute_machine_writes_only_its_outputs(own, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: tmp_path / "state")
    tool = own.mcps_dir / "community" / "x.new"
    (tool / "keys").mkdir(parents=True)
    (tool / "manifest.json").write_text("{}")
    step = tmp_path / "step"
    step.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HTTPS_PROXY": "http://10.7.7.7:3128", "HOME": "/root"}
    argv, step_env = sandbox_host.build_command(own, ["/usr/bin/true"], env, "pip", tool, step)
    writable = {d for f, _, d in _build_args(argv) if f == "--bind"}
    assert writable == {str(tool.resolve() / "venv"), str(step.resolve())}
    assert argv[:3] == ["/bin/sh", "-c", sandbox_host._RAISE_OOM]
    head = argv[:argv.index("bwrap")]
    assert "--forward" not in head and head[head.index("--allow-host") + 1] == "10.7.7.7"
    assert head[head.index("--beneath") + 1] == str(own.mcps_dir.resolve())
    assert step_env["HOME"].startswith(str(step))


@pytest.mark.asyncio
async def test_a_compute_machine_builds_in_its_sandbox_and_warms_nothing(own, monkeypatch, tmp_path):
    """A paired computer's install runs as the satellite's child and warms the
    tool; a compute machine's runs every step in its sandbox and starts no
    tool outside it."""
    import base64
    import io
    import tarfile
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from satellite.sessions.session_manager import SessionManager
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"name": "t", "server": {"transport": "stdio"}}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    spec = {"name": "t", "category": "community", "runtime": "python", "source": "pypi:t@1.0",
            "tarball_b64": base64.b64encode(buf.getvalue()).decode(), "version_hash": "h1"}
    seen = {}

    async def _install(*a, **kw):
        seen.update(kw)
        return SimpleNamespace(ok=True, log="", version_hash="h1", venv_python="")
    warm = AsyncMock(side_effect=lambda root, name, sem: (name, "ok"))
    sm = SessionManager(own)
    ws = AsyncMock()
    with patch("satellite._vendored.mcp_installer.install_mcp", _install), \
         patch("satellite.sessions.session_manager._warm_one_mcp", warm):
        await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [spec], "mcps_to_remove": []}, ws)
    assert seen["run"].__qualname__.startswith("build_run")
    assert seen["python_spec"] == f"{sys.version_info[0]}.{sys.version_info[1]}"
    warm.assert_not_called()


@pytest.mark.asyncio
async def test_a_compute_build_whose_output_names_a_file_lock_is_never_retried_unconfined(own, monkeypatch):
    """The lock retry is a Windows scanner's case, decided by the build's own
    output: a compute machine never takes it, and no step of its builds
    reaches the unconfined runner."""
    import base64
    import io
    import tarfile

    from satellite._vendored import mcp_installer
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"name": "t", "server": {"transport": "stdio"}}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    spec = {"name": "t", "category": "community", "runtime": "python", "source": "pypi:t@1.0",
            "tarball_b64": base64.b64encode(buf.getvalue()).decode(), "version_hash": "h1"}
    runs: list = []

    async def _install(*a, **kw):
        runs.append(kw.get("run"))
        return SimpleNamespace(ok=False, log="error: failed to rename: Access is denied (os error 5)",
                               version_hash="", venv_python="")

    async def _never(*a, **kw):
        raise AssertionError("a compute build reached the unconfined runner")
    monkeypatch.setattr(mcp_installer, "run_unconfined", _never)
    sm = SessionManager(own)
    with patch("satellite._vendored.mcp_installer.install_mcp", _install):
        await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [spec], "mcps_to_remove": []}, AsyncMock())
    assert len(runs) == 1 and runs[0].__qualname__.startswith("build_run")


def _plant_build(tool: Path, outside: Path, named: Path) -> None:
    """What a build can leave in its outputs for the satellite's own code
    after it: a script whose first line names ``named``, the temporary name
    the shebang fix writes as a link out, a file and a folder link out."""
    bin_dir = tool / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text(f"#!{named}/venv/bin/python\nprint('tool')\n")
    (bin_dir / "tool.shebang-tmp").symlink_to(outside / "entry")
    (bin_dir / "linked").symlink_to(outside / "script")
    (bin_dir / "folder").symlink_to(outside)
    (tool / "venv" / "lib").mkdir()
    (tool / "venv" / "lib" / "leak").symlink_to(outside)


def _outside(tmp_path: Path, named: Path) -> tuple[Path, dict[str, str]]:
    outside = tmp_path / "another-tool"
    outside.mkdir()
    (outside / "entry").write_text("#!/usr/bin/python3\nprint('another tool')\n")
    (outside / "script").write_text(f"#!{named}/venv/bin/python\nprint('script')\n")
    return outside, {p.name: p.read_text() for p in outside.iterdir()}


def test_the_shebang_fix_writes_through_no_link_a_build_left(tmp_path):
    from satellite.sessions import mcp_install_support as mis
    final = tmp_path / "community" / "t"
    staged = tmp_path / "community" / "t.new"
    outside, before = _outside(tmp_path, staged)
    _plant_build(final, outside, staged)
    mis._fix_shebangs_after_swap(staged, final)
    assert {p.name: p.read_text() for p in outside.iterdir()} == before
    assert (final / "venv" / "bin" / "tool").read_text().startswith(f"#!{final}/venv/bin/python\n")
    assert all((final / "venv" / "bin" / n).is_symlink() for n in ("linked", "folder"))


def test_the_shebang_fix_leaves_a_bin_folder_that_is_a_link(tmp_path):
    from satellite.sessions import mcp_install_support as mis
    final = tmp_path / "t"
    staged = tmp_path / "t.new"
    outside, before = _outside(tmp_path, staged)
    (final / "venv").mkdir(parents=True)
    (final / "venv" / "bin").symlink_to(outside)
    mis._fix_shebangs_after_swap(staged, final)
    assert {p.name: p.read_text() for p in outside.iterdir()} == before


@pytest.mark.asyncio
async def test_nothing_the_satellite_does_after_a_build_writes_outside_the_tool(own, tmp_path):
    """Every write of an install after its build (the preserve moves, the
    swap, the shebang fix, the marker, the scan) on what a build can leave."""
    import base64
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"name": "t", "server": {"transport": "stdio"}}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    spec = {"name": "t", "category": "community", "runtime": "python", "source": "pypi:t@1.0",
            "tarball_b64": base64.b64encode(buf.getvalue()).decode(), "version_hash": "h1"}
    final = own.mcps_dir / "community" / "t"
    staged = own.mcps_dir / "community" / "t.new"
    outside, before = _outside(tmp_path, staged)

    async def _install(mcp_dir, *a, **kw):
        _plant_build(Path(mcp_dir), outside, staged)
        return SimpleNamespace(ok=True, log="", version_hash="h1", venv_python="")
    sm = SessionManager(own)
    with patch("satellite._vendored.mcp_installer.install_mcp", _install):
        await sm.sync_mcps({"command_id": "c1", "mcps_to_install": [spec], "mcps_to_remove": []}, AsyncMock())
    assert {p.name: p.read_text() for p in outside.iterdir()} == before
    assert (final / "venv" / "bin" / "tool").read_text().startswith(f"#!{final}/venv/bin/python\n")


def test_a_hosted_platforms_word_reaches_this_machines_builds(own, tmp_path, monkeypatch):
    """The sync frame says a hosted install; the machine keeps it, so its
    builds (the next start's rebuilds too) block the hosted ranges."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    tool = own.mcps_dir / "community" / "x.new"
    tool.mkdir(parents=True)
    (tool / "manifest.json").write_text("{}")
    step = tmp_path / "step"
    step.mkdir()

    def _head():
        argv, _env = sandbox_host.build_command(own, ["/usr/bin/true"], {"PATH": "/usr/bin"}, "pip", tool, step)
        return argv[:argv.index("bwrap")]
    assert "--hosted" not in _head()
    sandbox_host.note_hosted(True)
    assert "--hosted" in _head() and sandbox_host.hosted_install()
    sandbox_host.note_hosted(False)
    assert "--hosted" not in _head()


def test_a_build_scratch_a_stopped_satellite_left_goes_at_its_start(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    (tmp_path / "state" / "builds" / "pip-left" / "cache").mkdir(parents=True)
    locked = tmp_path / "state" / "builds" / "pip-locked" / "venv" / "lib"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    os.chmod(locked, 0)  # a build left a folder with no permission bits
    assert sandbox_host.sweep_build_scratch() == 2
    assert list((tmp_path / "state" / "builds").iterdir()) == []


def test_no_build_function_has_a_runner_or_a_machine_kind_by_default():
    """A caller that forgets the runner, or that it runs on a compute
    machine, cannot build unconfined: a default would."""
    import inspect

    from satellite._vendored import mcp_installer
    from satellite.sessions import mcp_install_support as mis
    for fn, name in ((mcp_installer.install_mcp, "run"), (mcp_installer._create_venv, "run"),
                     (mcp_installer._apply_patches, "run"), (mis._uv_venv_pinned, "run"),
                     (mis._reconcile_node_addons, "run"), (mis.reconcile_mcp_runtimes, "compute"),
                     (mis.install, "compute")):
        assert inspect.signature(fn).parameters[name].default is inspect.Parameter.empty, fn.__name__


def _stopped_mid_swap(mcps_dir: Path, *, old_gone: bool) -> tuple[Path, Path]:
    """What a satellite stopped in the middle of a swap leaves: the tool's
    keys and data already moved into ``<tool>.new``, the old folder kept or
    already removed."""
    tool = mcps_dir / "community" / "t"
    staged = mcps_dir / "community" / "t.new"
    (staged / "keys").mkdir(parents=True)
    (staged / "keys" / "api.key").write_text("secret\n")
    (staged / "config").mkdir()
    (staged / "config" / "state.json").write_text("{}\n")
    (staged / "manifest.json").write_text('{"name": "t", "version": "2"}')
    if not old_gone:
        tool.mkdir(parents=True)
        (tool / "manifest.json").write_text('{"name": "t", "version": "1"}')
    return tool, staged


@pytest.mark.parametrize("old_gone", [False, True])
def test_a_stop_during_the_swap_never_costs_a_tools_keys_and_data(own, old_gone):
    """The start sweep drops a stale ``<tool>.new``: the keys and data a
    swap had moved into it go back into the tool first (made when it is
    gone, with no manifest, so the next install carries them over)."""
    tool, staged = _stopped_mid_swap(own.mcps_dir, old_gone=old_gone)
    SessionManager(own).cleanup_orphans()
    assert not staged.exists()
    assert (tool / "keys" / "api.key").read_text() == "secret\n"
    assert (tool / "config" / "state.json").read_text() == "{}\n"
    assert (tool / "manifest.json").is_file() is (not old_gone)


@pytest.mark.asyncio
async def test_an_install_over_a_stale_staged_folder_keeps_the_keys_in_it(own):
    """A stale ``<tool>.new`` the start could not sweep: the next install
    of the tool carries its keys and data over, never wipes them."""
    import base64
    import io
    import tarfile
    tool, staged = _stopped_mid_swap(own.mcps_dir, old_gone=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"name": "t", "server": {"transport": "stdio"}}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    spec = {"name": "t", "category": "community", "runtime": "python", "source": "pypi:t@1.0",
            "tarball_b64": base64.b64encode(buf.getvalue()).decode(), "version_hash": "h1"}

    async def _install(*a, **kw):
        return SimpleNamespace(ok=True, log="", version_hash="h1", venv_python="")
    with patch("satellite._vendored.mcp_installer.install_mcp", _install):
        await SessionManager(own).sync_mcps({"command_id": "c1", "mcps_to_install": [spec],
                                             "mcps_to_remove": []}, AsyncMock())
    assert (tool / "keys" / "api.key").read_text() == "secret\n"
    assert (tool / "manifest.json").is_file() and not staged.exists()


def test_the_satellite_builds_through_one_door():
    """``mcp_install_support.install`` is the satellite's one caller of
    ``install_mcp`` and ``build_runner`` the one place that picks a runner:
    a module that calls the installer or names the unconfined runner itself
    could build on a compute machine with no sandbox."""
    import ast
    root = Path(__file__).resolve().parent.parent
    doors = {"install_mcp": {"sessions/mcp_install_support.py"},
             "run_unconfined": {"sessions/mcp_install_support.py"},
             "_run_bounded": {"sessions/sandbox_host.py"}}
    found: dict[str, set[str]] = {name: set() for name in doors}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("_vendored/", "tests/", "venv/")) or "/site-packages/" in rel:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
            if name in found:
                found[name].add(rel)
    assert found == doors


def test_a_machine_with_its_own_copy_runs_tools_only_at_the_platforms_tools_path(own, compute):
    import dataclasses
    desc = render.SandboxDescription.from_dict(_description(own.agents_dir, own.mcps_dir))
    sandbox_host.refuse_other_tools_path(desc, own)
    elsewhere = dataclasses.replace(own, mcps_dir=own.mcps_dir.parent / "other-tools")
    with pytest.raises(render.RenderRefused, match="platform's tools path"):
        sandbox_host.refuse_other_tools_path(desc, elsewhere)
    # A machine beside the platform reads the platform's folder: not judged.
    sandbox_host.refuse_other_tools_path(desc, dataclasses.replace(compute, mcps_dir=elsewhere.mcps_dir))


# ---------------------------------------------------------------------------
# the install's own machine keeps the community tools (``tools = own``)
# ---------------------------------------------------------------------------

@pytest.fixture
def tools(compute):
    import dataclasses
    return dataclasses.replace(compute, own_tools=True)


def _tar_spec(manifest: dict, **over) -> dict:
    import base64
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    server = manifest.get("server") or {}
    return {"name": manifest["name"], "category": "community", "runtime": server.get("runtime", "python"),
            "source": server.get("source", ""), "tarball_b64": base64.b64encode(buf.getvalue()).decode(),
            "version_hash": "", **over}


async def _sync(cfg, frame: dict, install=None) -> dict:
    """One ``sync_mcps`` frame on ``cfg``, the ack's results."""
    sent: list[dict] = []

    async def _send(msg):
        sent.append(msg)

    async def _no_install(*a, **kw):
        raise AssertionError("an install ran")
    ws = SimpleNamespace(enqueue_send=_send)
    with patch("satellite._vendored.mcp_installer.install_mcp", install or _no_install):
        await SessionManager(cfg).sync_mcps({"command_id": "c1", "mcps_to_install": [], "mcps_to_remove": [],
                                             **frame}, ws)
    return next(m for m in sent if m["type"] == "ack")["results"]


def test_the_conf_names_its_own_tools_beside_the_platform_alone(tmp_path):
    conf = tmp_path / "satellite.conf"
    base = ("[satellite]\nmachine_id = c-1\nmachine_secret = s\nplatform_url = ws://127.0.0.1:8400/v1/satellite\n"
            f"agents_dir = {tmp_path}/a\nmcps_dir = {tmp_path}/m\n")
    conf.write_text(base + "kind = compute\ntools = own\n")
    cfg = load_config(conf)
    assert cfg.own_tools is True and cfg.own_copy is False
    # On another server every folder is the machine's own: the word says nothing there.
    conf.write_text(base + "kind = compute\nfiles = own\ntools = own\n")
    cfg = load_config(conf)
    assert cfg.own_tools is False and cfg.own_copy is True
    conf.write_text(base + "tools = own\n")
    assert load_config(conf).own_tools is False
    conf.write_text(base + "kind = compute\n")
    assert load_config(conf).own_tools is False


@pytest.mark.asyncio
async def test_a_machine_that_keeps_its_tools_takes_the_tool_frames_and_nothing_else(compute, own, tools, monkeypatch):
    from satellite.transport import compute_link, ws_client
    assert {t for t in compute_link.SHARED_REFUSES if not compute_link.refuses(tools, t)} == {
        "sync_mcps", "sync_mcps_verify"}
    assert all(compute_link.refuses(compute, t) for t in compute_link.SHARED_REFUSES)
    assert compute_link.refuses(own, "session_move") and not compute_link.refuses(own, "file_push")
    sm = SessionManager(tools)
    client = ws_client.SatelliteWSClient(tools, sm)
    sent: list[dict] = []

    async def _send(msg):
        sent.append(msg)
    monkeypatch.setattr(client, "enqueue_send", _send)
    taken: list[str] = []

    async def _took(msg, *_a):
        taken.append(msg.get("command_id"))
    monkeypatch.setattr(sm, "sync_mcps", _took)
    monkeypatch.setattr(sm, "sync_mcps_verify", _took)
    monkeypatch.setattr(sm, "file_push", AsyncMock(side_effect=AssertionError("a file reached the platform's folder")))
    for command_id, kind in (("c-1", "sync_mcps"), ("c-2", "sync_mcps_verify"), ("c-3", "file_push"),
                             ("c-4", "session_move"), ("c-5", "credentials_update"),
                             ("c-6", "session_files_retention")):
        await client._dispatch_frame(kind, {"command_id": command_id, "agent_slug": "a"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sorted(taken) == ["c-1", "c-2"]
    assert [m["command_id"] for m in sent] == ["c-3", "c-4", "c-5", "c-6"]
    assert all(m["status"] == "error" for m in sent)


@pytest.mark.asyncio
async def test_the_sign_in_claims_that_it_keeps_its_tools(compute, own, tools):
    from satellite.transport import ws_client
    claims = []
    for cfg in (compute, own, tools):
        client = ws_client.SatelliteWSClient(cfg, SessionManager(cfg))
        sent: list[str] = []

        class _Socket:
            async def send(self, text, sent=sent):
                sent.append(text)

            async def recv(self):
                raise ConnectionError("enough")
        client._ws = _Socket()
        with pytest.raises(ConnectionError):
            await client._authenticate()
        caps = json.loads(sent[0])["capabilities"]
        claims.append((caps["compute_files"], caps.get("compute_tools")))
    assert claims == [("shared", None), ("own", None), ("shared", "own")]


def test_the_start_picks_its_sweeps_by_whose_folders_it_works_on(compute, own, tools):
    import dataclasses

    from satellite import __main__ as main
    paired = dataclasses.replace(compute, compute=False)
    assert main._start_sweeps(paired) is main._start_time_sweeps
    assert main._start_sweeps(own) is main._start_time_sweeps
    assert main._start_sweeps(tools) is main._tool_sweeps
    assert main._start_sweeps(compute) is None


@pytest.mark.asyncio
async def test_the_tool_sweeps_touch_the_community_folder_alone(tools, monkeypatch, tmp_path):
    """The start of the install's own machine sweeps its community tools and
    its own build scratch. The agents folder and the other tools folders are
    the platform's: its marker, a session's token folder, its tool configs,
    a pid file and another category's staged folder are all still there."""
    from satellite import __main__ as main
    from satellite.sessions import mcp_install_support as mis
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    agents, mcps = tools.agents_dir, tools.mcps_dir
    agent = agents / "personal-assistant"
    platforms = [agents / render.PLATFORM_MARKER,
                 agent / "users" / "alice" / ".credentials" / "google.json",
                 agent / "users" / "alice" / ".claude" / "mcp-config-s1.json",
                 agent / "users" / "alice" / ".codex" / "config.toml",
                 agents / ".satellite-pids" / "s1.pid",
                 mcps / "custom" / "y.new" / "manifest.json",
                 mcps / "custom" / ".quarantine-y-0a1b2c3d" / "manifest.json",
                 mcps / ".uv-python" / "cpython-3.9.1-linux" / "bin" / "python",
                 mcps / ".venv-aside-custom-memory-mcp" / "pyvenv.cfg"]
    for path in platforms:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kept\n")
    # A bundled tool whose venv lags this interpreter: the platform's to rebuild.
    stale = mcps / "custom" / "memory-mcp"
    (stale / "manifest.json").write_text(json.dumps({"name": "memory-mcp", "server": {"runtime": "python"}}))
    (stale / "requirements.txt").write_text("mcp\n")
    (stale / "venv").mkdir()
    (stale / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.8.0\n")
    # The machine's own: a swap that stopped, a quarantine, a tool whose
    # interpreter is gone, an interpreter no tool uses, a build's scratch.
    tool, staged = _stopped_mid_swap(mcps, old_gone=False)
    (mcps / "community" / ".quarantine-t-0a1b2c3d").mkdir()
    gone = mcps / "community" / "d"
    (gone / "venv" / "bin").mkdir(parents=True)
    (gone / "manifest.json").write_text(json.dumps(
        {"name": "d", "server": {"runtime": "python", "source": "pypi:d@1.0"}}))
    (gone / "venv" / "bin" / "python").symlink_to(tmp_path / "no-such-interpreter")
    (mcps / "community" / ".uv-python" / "cpython-3.9.1-linux" / "bin").mkdir(parents=True)
    (tmp_path / "state" / "builds" / "pip-left").mkdir(parents=True)
    built: list[Path] = []

    async def _install(mcp_dir, runtime, source, *, compute, **kw):
        assert compute is tools
        built.append(Path(mcp_dir))
        return SimpleNamespace(ok=True, log="", version_hash="h", venv_python="")
    monkeypatch.setattr(mis, "install", _install)
    await main._tool_sweeps(SessionManager(tools))
    assert all(path.read_text() == "kept\n" for path in platforms)
    assert (stale / "venv" / "pyvenv.cfg").is_file()
    # The tool whose interpreter is gone is built by no start: the
    # platform's run builds it, in a frame that names where its build goes.
    assert built == [] and (gone / "venv" / "bin" / "python").is_symlink()
    assert not staged.exists() and (tool / "keys" / "api.key").read_text() == "secret\n"
    assert not (mcps / "community" / ".quarantine-t-0a1b2c3d").exists()
    assert not (mcps / "community" / ".uv-python" / "cpython-3.9.1-linux").exists()
    assert list((tmp_path / "state" / "builds").iterdir()) == []


@pytest.mark.asyncio
async def test_a_removal_reaches_nothing_but_a_tool_of_the_community_folder(tools, tmp_path):
    """A removal's name becomes a path on this machine. Sixteen names, a
    link named like a tool among them: the names that are no tool's are
    each refused, nothing outside the community folder is touched, the
    link is not followed, and the one real tool goes. The frame's word that
    any category may go changes nothing here."""
    mcps = tools.mcps_dir
    outside = tmp_path / "outside"
    kept = [mcps / "community" / "x.new", mcps / "community" / ".quarantine-x-0a1b2c3d",
            mcps / "community" / "-tool", mcps / "community" / "_tool", mcps / "community" / ("a" * 65),
            mcps / "community" / "a" / "b", mcps / "custom" / "bundled", mcps / "core" / "bundled", outside]
    for folder in [*kept, mcps / "community" / "real"]:
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}")
    (mcps / "community" / "linked").symlink_to(outside, target_is_directory=True)
    refused = ["..", "../custom", "../custom/bundled", "/etc", str(outside), "a/b", "x.new",
               ".quarantine-x-0a1b2c3d", "-tool", "_tool", "a" * 65, "", "."]
    names = [*refused, "bundled", "linked", "real"]
    assert len(names) == 16
    results = await _sync(tools, {"mcps_to_remove": names, "remove_any_category": True})
    assert results["real"] == {"status": "removed"} and not (mcps / "community" / "real").exists()
    for name in refused:
        assert results[name or "?"]["status"] == "error", name
    # A bundled tool's name is looked for in the community folder alone, and a link is no folder to remove.
    assert results["bundled"] == {"status": "not_found"} and results["linked"] == {"status": "not_found"}
    assert all((folder / "manifest.json").is_file() for folder in kept)
    assert (mcps / "community" / "linked").is_symlink()


@pytest.mark.asyncio
async def test_the_start_removes_the_trust_files_a_killed_machine_left(tools, tmp_path, monkeypatch):
    """A machine killed inside a frame leaves that frame's trust files in
    its state folder (certificates, no secret). Its value went with the
    process: the files go at the next start, not at the next frame."""
    from satellite import __main__ as main
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    left = tmp_path / "state" / "egress-trust"
    (left / "cert-dir").mkdir(parents=True)
    (left / "ca.pem").write_bytes(_CA)
    (left / "cert-dir" / "0a1b2c3d.0").write_bytes(_CA)
    (tmp_path / "state" / "kept").write_text("the machine's own\n")
    await main._tool_sweeps(SessionManager(tools))
    assert not left.exists() and (tmp_path / "state" / "kept").is_file()
    assert sandbox_host.platform_egress({}) == ({}, (), ())


def test_the_staged_sweep_takes_what_a_stopped_copy_left(tmp_path):
    """The update's copy stages each tool under ``.copying-<tool>``. One a
    stopped copy left holds a manifest, so the verify would name it at
    every run for good: the start's sweep removes it with the other
    staged folders and touches no tool."""
    from satellite.sessions import mcp_install_support as mis
    community = tmp_path / "community"
    for name in (".copying-x", "x", "copying-y"):
        (community / name).mkdir(parents=True)
        (community / name / "manifest.json").write_text("{}")
    assert mis.sweep_staged(community) == 1
    assert sorted(p.name for p in community.iterdir()) == ["copying-y", "x"]


_UNPINNED = {"name": "t", "version": "", "category": "community",
             "server": {"runtime": "node", "source": "npm:@scope/pkg"}}


def _node_build(version: str):
    """The installer's part of a Node build: the package file it writes, the
    version it read back."""
    from satellite._vendored import mcp_installer

    async def _install(mcp_dir, runtime, source, **kw):
        (Path(mcp_dir) / "package.json").write_bytes(mcp_installer._node_package_json("@scope/pkg", "latest"))
        return SimpleNamespace(ok=True, log="", version_hash="unpinned", venv_python="", resolved_version=version)
    return _install


@pytest.mark.asyncio
async def test_the_platforms_own_install_pins_the_staged_folder_before_the_swap(tools, monkeypatch, tmp_path):
    """``pin`` in the spec: the machine settles its copy to the version its
    build read back, hashes it again and names the version. The platform,
    which built nothing, settles its own copy the same way and reads the
    same hash."""
    from satellite._vendored import mcp_installer, mcp_pin
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    results = await _sync(tools, {"mcps_to_install": [_tar_spec(_UNPINNED, pin=True)]}, _node_build("3.1.4"))
    installed = tools.mcps_dir / "community" / "t"
    manifest = json.loads((installed / "manifest.json").read_text())
    assert manifest["version"] == "3.1.4" and manifest["server"]["source"] == "npm:@scope/pkg@3.1.4"
    assert (installed / "package.json").read_bytes() == mcp_installer._node_package_json("@scope/pkg", "3.1.4")
    platform = tmp_path / "the-platforms-copy"
    platform.mkdir()
    (platform / "manifest.json").write_text(json.dumps(_UNPINNED, indent=2))
    mcp_pin.settle_built(platform, "npm:@scope/pkg", "3.1.4")
    assert results["t"] == {"status": "ok", "resolved_version": "3.1.4",
                            "version_hash": mcp_installer.compute_version_hash(platform)}
    assert results["t"]["version_hash"] == mcp_installer.compute_version_hash(installed)


@pytest.mark.asyncio
async def test_a_sync_without_the_pin_names_the_version_and_writes_no_manifest(own):
    """Every machine names the version its build read back. Only the
    platform's own install pins: a manifest that arrived pinned is never
    written again, so no hash drifts."""
    results = await _sync(own, {"mcps_to_install": [_tar_spec(_UNPINNED)]}, _node_build("3.1.4"))
    installed = own.mcps_dir / "community" / "t"
    assert results["t"] == {"status": "ok", "version_hash": "unpinned", "resolved_version": "3.1.4"}
    assert json.loads((installed / "manifest.json").read_text()) == _UNPINNED


@pytest.mark.asyncio
async def test_a_version_that_is_not_one_is_neither_pinned_nor_named(tools, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    results = await _sync(tools, {"mcps_to_install": [_tar_spec(_UNPINNED, pin=True)]},
                          _node_build("1.0.0; rm -rf /"))
    assert results["t"] == {"status": "ok", "version_hash": "unpinned"}
    assert json.loads((tools.mcps_dir / "community" / "t" / "manifest.json").read_text()) == _UNPINNED


@pytest.mark.asyncio
async def test_a_machine_that_keeps_its_tools_installs_and_removes_community_tools_alone(tools, monkeypatch, tmp_path):
    """The bundled tools' folders are the platform's: a spec of another
    category is refused before anything is written, and a removal looks in
    the community folder alone, whatever the frame says."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    custom = tools.mcps_dir / "custom"
    (tools.mcps_dir / "community" / "memory-mcp").mkdir(parents=True)
    (tools.mcps_dir / "core" / "memory-mcp").mkdir(parents=True)
    before = _snapshot(custom)
    spec = _tar_spec({"name": "t", "server": {"runtime": "python", "source": "pypi:t@1.0"}}, category="custom")
    results = await _sync(tools, {"mcps_to_install": [spec], "mcps_to_remove": ["memory-mcp"],
                                  "remove_any_category": True})
    assert results["t"] == {"status": "error", "error": "This machine keeps the community tools alone."}
    assert results["memory-mcp"] == {"status": "removed"}
    assert _snapshot(custom) == before and (custom / "memory-mcp").is_dir()
    assert (tools.mcps_dir / "core" / "memory-mcp").is_dir()
    assert not (tools.mcps_dir / "community" / "memory-mcp").exists()
    # A machine with its own copy keeps every category, as before.
    (custom / "memory-mcp" / "manifest.json").write_text("{}")
    import dataclasses
    own = dataclasses.replace(tools, own_tools=False, own_copy=True)
    assert (await _sync(own, {"mcps_to_remove": ["memory-mcp"]}))["memory-mcp"] == {"status": "removed"}
    assert not (custom / "memory-mcp").exists()


_CA = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _egress_block(**over) -> dict:
    return {"env": {"HTTPS_PROXY": "http://10.7.7.7:3128"},
            "trust": [{"env": "SSL_CERT_FILE", "name": "ca.pem", "data_b64": _b64(_CA)}],
            "forwards": ["3128"], "allow_hosts": ["10.7.7.7"], **over}


def _staged_tool(cfg, tmp_path):
    tool = cfg.mcps_dir / "community" / "x.new"
    tool.mkdir(parents=True, exist_ok=True)
    (tool / "manifest.json").write_text("{}")
    step = tmp_path / "step"
    step.mkdir(exist_ok=True)
    return tool, step


def test_the_platforms_egress_block_is_what_this_machines_builds_open(tools, tmp_path, monkeypatch):
    """The install's own machine carves what the platform's frame named and
    reads the platform's proxy and trust file. Its own environment's proxy
    is not what a build of the platform's tools uses, and the render's rule,
    which opens any private address, is never asked."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(render, "build_carves",
                        lambda env: (_ for _ in ()).throw(AssertionError("the render's own rule was asked")))
    tool, step = _staged_tool(tools, tmp_path)
    own_env = {"PATH": os.environ.get("PATH", ""), "https_proxy": "http://192.168.9.9:8080"}

    def _build():
        argv, env = sandbox_host.build_command(tools, ["/usr/bin/true"], dict(own_env), "pip", tool, step)
        return argv[:argv.index("bwrap")], argv, env
    # No block: this machine's own proxy is carved by nobody.
    head, _argv, env = _build()
    assert "--allow-host" not in head and "--forward" not in head
    assert env["https_proxy"] == "http://192.168.9.9:8080"
    sandbox_host.note_egress(_egress_block())
    head, argv, env = _build()
    assert head[head.index("--allow-host") + 1] == "10.7.7.7" and head.count("--allow-host") == 1
    assert head[head.index("--forward") + 1] == "3128" and head.count("--forward") == 1
    assert env["HTTPS_PROXY"] == "http://10.7.7.7:3128" and "https_proxy" not in env
    kept = Path(env["SSL_CERT_FILE"])
    assert kept.parent == tmp_path / "state" / "egress-trust" and kept.read_bytes() == _CA
    assert oct(kept.stat().st_mode & 0o777) == "0o600"
    assert ("--ro-bind", str(kept), str(kept)) in _build_args(argv)
    # The frame ended: a build after it gets none of the platform's settings.
    sandbox_host.clear_egress()
    head, _argv, env = _build()
    assert "--allow-host" not in head and "--forward" not in head and "SSL_CERT_FILE" not in env
    assert not kept.exists() and not (tmp_path / "state" / "egress-trust").exists()


def test_an_egress_block_keeps_nothing_outside_its_bounds(tools, tmp_path, monkeypatch, caplog):
    """What a frame may name is the render's proxy, index and trust names,
    spliced ports and carved addresses. A loopback or a metadata address,
    another variable, a path as a file name and an oversized file are all
    dropped, and a record changed on disk is read through the same bounds."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    big = _b64(b"x" * ((1 << 20) + 1))
    sandbox_host.note_egress({
        "env": {"HTTPS_PROXY": "http://10.7.7.7:3128", "LD_PRELOAD": "/tmp/x.so", "PATH": "/evil",
                "UV_INDEX_URL": "https://i/" + "x" * 4096, "NPM_CONFIG_REGISTRY": 7},
        "trust": [{"env": "SSL_CERT_FILE", "name": "../../etc/passwd", "data_b64": _b64(_CA)},
                  {"env": "REQUESTS_CA_BUNDLE", "name": "/etc/ssl/ca.pem", "data_b64": _b64(_CA)},
                  {"env": "SSL_CERT_DIR", "name": "certs", "data_b64": _b64(_CA)},
                  {"env": "NODE_EXTRA_CA_CERTS", "name": "big.pem", "data_b64": big},
                  {"env": "PATH", "name": "p.pem", "data_b64": _b64(_CA)},
                  {"env": "CURL_CA_BUNDLE", "name": "bad.pem", "data_b64": "not base64 !!"},
                  "ca.pem"],
        "forwards": ["0", "65536", "22x", 8400, "-1", "443"],
        "allow_hosts": ["127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.169.254", "fd00:ec2::254",
                        "0.0.0.0", "example.com", 7, "10.0.0.5"],
    })
    env, forwards, hosts = sandbox_host.platform_egress({"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", "HTTPS_PROXY": "http://10.7.7.7:3128"}
    assert forwards == ("443",) and hosts == ("10.0.0.5",)
    assert not (tmp_path / "state" / "egress-trust").exists()
    assert "named what this machine does not take" in caplog.text
    # The counts are bounded too.
    sandbox_host.note_egress({
        "forwards": [str(2000 + i) for i in range(40)],
        "allow_hosts": [f"10.0.0.{i}" for i in range(1, 60)],
        "trust": [{"env": name, "name": f"{i}.pem", "data_b64": _b64(_CA)} for i, name in enumerate(
            ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
             "NPM_CONFIG_CAFILE", "GIT_SSL_CAINFO", "SSL_CERT_FILE"])],
    })
    env, forwards, hosts = sandbox_host.platform_egress({})
    assert len(forwards) == 16 and len(hosts) == 32
    assert sorted(env) == ["CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS", "NPM_CONFIG_CAFILE",
                           "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"]
    assert Path(env["SSL_CERT_FILE"]).name == "SSL_CERT_FILE-0.pem"   # the first of a name is kept
    # A frame with no block holds none of the frame before it.
    sandbox_host.note_egress(None)
    assert sandbox_host.platform_egress({"PATH": "/usr/bin"}) == ({"PATH": "/usr/bin"}, (), ())
    assert not (tmp_path / "state" / "egress-trust").exists()


def test_the_settings_a_build_had_on_the_platform_travel_and_no_cache_folder(tmp_path, monkeypatch):
    """An install behind a company proxy: the hosts that skip it, a trusted
    host, a timeout. A cache folder is each host's own."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    settings = {"NO_PROXY": "index.corp,10.0.0.0/8", "PIP_TRUSTED_HOST": "index.corp",
                "NPM_CONFIG_STRICT_SSL": "false", "UV_NATIVE_TLS": "1", "UV_HTTP_TIMEOUT": "120",
                "UV_OFFLINE": "0", "UV_PYTHON_DOWNLOADS": "manual",
                "UV_PYPY_INSTALL_MIRROR": "https://mirror.corp/pypy"}
    sandbox_host.note_egress({"env": {**settings, "UV_CACHE_DIR": "/platform/cache", "PIP_CACHE_DIR": "/p",
                                      "NPM_CONFIG_CACHE": "/n", "XDG_CACHE_HOME": "/x"}})
    env, forwards, hosts = sandbox_host.platform_egress({"UV_CACHE_DIR": "/machine/cache", "no_proxy": "mine"})
    assert env == {**settings, "UV_CACHE_DIR": "/machine/cache"} and (forwards, hosts) == ((), ())


_HASHED = ("0a1b2c3d.0", "0a1b2c3d.1", "ffffffff.12")


def test_the_platforms_certificate_folder_is_kept_as_a_folder_a_build_reads(tools, tmp_path, monkeypatch):
    """``SSL_CERT_DIR`` names a folder a TLS library looks up by file name:
    its certificates travel under those names, and a build reads the folder
    kept here."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    tool, step = _staged_tool(tools, tmp_path)
    sandbox_host.note_egress({"cert_dir": [{"name": n, "data_b64": _b64(_CA + n.encode())} for n in _HASHED]})
    argv, env = sandbox_host.build_command(tools, ["/usr/bin/true"], {"PATH": os.environ.get("PATH", "")},
                                           "pip", tool, step)
    kept = Path(env["SSL_CERT_DIR"])
    assert kept == tmp_path / "state" / "egress-trust" / "cert-dir"
    assert sorted(p.name for p in kept.iterdir()) == sorted(_HASHED)
    assert (kept / "ffffffff.12").read_bytes() == _CA + b"ffffffff.12"
    assert {oct(p.stat().st_mode & 0o777) for p in kept.iterdir()} == {"0o600"}
    assert oct(kept.stat().st_mode & 0o777) == "0o700"
    assert ("--ro-bind", str(kept), str(kept)) in _build_args(argv)
    # The next frame names none: the folder goes.
    sandbox_host.note_egress({"env": {"HTTPS_PROXY": "http://10.7.7.7:3128"}})
    assert not kept.exists() and "SSL_CERT_DIR" not in sandbox_host.platform_egress({})[0]


@pytest.mark.parametrize("entry", [
    {"name": "../../etc/passwd", "data_b64": _b64(_CA)},
    {"name": "ca.pem", "data_b64": _b64(_CA)},                      # no name a library looks up
    {"name": "0a1b2c3d.0", "data_b64": _b64(_CA)},                  # a name twice
    {"name": "0A1B2C3D.0", "data_b64": _b64(_CA)},
    {"name": "0a1b2c3e.0", "data_b64": "not base64 !!"},
    {"name": "0a1b2c3e.0", "data_b64": ""},
    {"name": "0a1b2c3e.0", "data_b64": _b64(b"x" * ((1 << 20) + 1))},
    {"name": "0a1b2c3e.0"},
    "0a1b2c3e.0",
])
def test_a_certificate_folder_is_kept_whole_or_not_at_all(tmp_path, monkeypatch, caplog, entry):
    """A part of a trust store would read as the whole of it."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    good = {"name": "0a1b2c3d.0", "data_b64": _b64(_CA)}
    sandbox_host.note_egress({"env": {"HTTPS_PROXY": "http://10.7.7.7:3128"}, "cert_dir": [good, entry]})
    env = sandbox_host.platform_egress({})[0]
    assert env == {"HTTPS_PROXY": "http://10.7.7.7:3128"}
    assert not (tmp_path / "state" / "egress-trust").exists() and "cert_dir" in caplog.text


def test_a_certificate_folder_past_its_bounds_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    many = [{"name": f"{i:08x}.0", "data_b64": _b64(_CA)} for i in range(513)]
    sandbox_host.note_egress({"cert_dir": many})
    assert sandbox_host.platform_egress({}) == ({}, (), ())
    half = _b64(b"x" * (600 * 1024))
    sandbox_host.note_egress({"cert_dir": [{"name": "0a1b2c3d.0", "data_b64": half},
                                           {"name": "0a1b2c3d.1", "data_b64": half}]})
    assert sandbox_host.platform_egress({}) == ({}, (), ())
    assert not (tmp_path / "state" / "egress-trust").exists()


_SECRET_INDEX = "https://builder:s3cr3t-index@index.corp/simple"
_SECRET_PROXY = "http://builder:s3cr3t-proxy@proxy.corp:3128"


def _holds(root: Path, *needles: bytes) -> list[str]:
    """Every file under ``root`` that holds one of ``needles``, and every
    name that does."""
    found = []
    for path in sorted(root.rglob("*")) if root.exists() else []:
        if any(n in str(path).encode() for n in needles):
            found.append(f"name {path}")
        if path.is_file() and any(n in path.read_bytes() for n in needles):
            found.append(f"file {path}")
    return found


@pytest.mark.asyncio
async def test_a_builds_index_and_proxy_sign_in_stay_the_platforms(tools, tmp_path, monkeypatch, caplog):
    """The platform owns an address that carries a user and a password. It
    sends it with each frame. This machine gives it to that frame's builds
    as their environment and keeps it nowhere: no file of its state folder,
    no log line, no argument, no environment of a later build or of this
    process."""
    import logging
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    seen: dict = {}

    async def _install(mcp_dir, runtime, source, **kw):
        step = tmp_path / "step"
        step.mkdir(exist_ok=True)
        argv, env = sandbox_host.build_command(tools, ["/usr/bin/true"], {"PATH": os.environ.get("PATH", "")},
                                               "pip", Path(mcp_dir), step)
        seen.update(argv=argv, env=env, files=_holds(tmp_path / "state", b"s3cr3t"))
        return SimpleNamespace(ok=True, log="", version_hash="h", venv_python="", resolved_version="")
    spec = _tar_spec({"name": "t", "version": "1.0", "category": "community",
                      "server": {"runtime": "python", "source": "pypi:t@1.0"}}, version_hash="")
    egress = {"env": {"PIP_INDEX_URL": _SECRET_INDEX, "HTTPS_PROXY": _SECRET_PROXY},
              "trust": [{"env": "SSL_CERT_FILE", "name": "ca.pem", "data_b64": _b64(_CA)}],
              "allow_hosts": ["10.7.7.7"]}
    await _sync(tools, {"mcps_to_install": [spec], "egress": egress}, _install)
    # The frame's build had them, as its environment alone.
    assert seen["env"]["PIP_INDEX_URL"] == _SECRET_INDEX and seen["env"]["HTTPS_PROXY"] == _SECRET_PROXY
    assert not any("s3cr3t" in arg for arg in seen["argv"])
    assert seen["files"] == []
    # After the frame: nothing on disk, in the log or in this process, and a later build gets none.
    assert _holds(tmp_path / "state", b"s3cr3t", b"builder:") == []
    assert _holds(tools.mcps_dir, b"s3cr3t") == []
    assert "s3cr3t" not in caplog.text and "builder:" not in caplog.text
    assert not any("s3cr3t" in value for value in os.environ.values())
    later = sandbox_host.platform_egress({"PATH": "/usr/bin"})
    assert later == ({"PATH": "/usr/bin"}, (), ()) and not (tmp_path / "state" / "egress-trust").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["a removal", "an install", "no block"])
async def test_a_frames_egress_outlives_a_second_frame(tools, tmp_path, monkeypatch, second):
    """The platform lets go of a machine while the machine still builds: a
    restart of the platform, a build past the platform's budget, a request
    that ended early. A second frame then arrives inside the first one's
    build. A machine handles one frame at a time: the second waits, so every
    step of a build opens what its own frame named, and nothing of the first
    frame is left for the second's build."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    between, go = asyncio.Event(), asyncio.Event()
    steps: dict[str, list] = {}

    def _step(tool: str) -> None:
        env, forwards, hosts = sandbox_host.platform_egress({})
        trust = Path(env["SSL_CERT_FILE"]).read_bytes() if "SSL_CERT_FILE" in env else None
        steps.setdefault(tool, []).append((env.get("HTTPS_PROXY"), env.get("PIP_INDEX_URL"), trust, forwards, hosts))

    async def _install(mcp_dir, runtime, source, **kw):
        tool = Path(mcp_dir).name.removesuffix(".new")
        _step(tool)
        if tool == "first":
            between.set()
            await go.wait()
            _step(tool)
        return SimpleNamespace(ok=True, log="", version_hash="h", venv_python="", resolved_version="")

    def _spec(name: str) -> dict:
        return _tar_spec({"name": name, "version": "1.0", "category": "community",
                          "server": {"runtime": "python", "source": f"pypi:{name}@1.0"}}, version_hash="")

    async def _send(_msg):
        return None
    ws = SimpleNamespace(enqueue_send=_send)
    frames = {"a removal": {"mcps_to_remove": ["x.new"], "egress": _egress_block()},
              "an install": {"mcps_to_install": [_spec("other")],
                             "egress": {"env": {"PIP_INDEX_URL": "https://other.index/simple"}}},
              "no block": {"mcps_to_remove": ["gone"]}}
    manager = SessionManager(tools)
    with patch("satellite._vendored.mcp_installer.install_mcp", _install):
        first = asyncio.ensure_future(manager.sync_mcps(
            {"command_id": "c1", "mcps_to_install": [_spec("first")], "mcps_to_remove": [],
             "egress": _egress_block()}, ws))
        await asyncio.wait_for(between.wait(), 10)
        later = asyncio.ensure_future(manager.sync_mcps(
            {"command_id": "c2", "mcps_to_install": [], "mcps_to_remove": [], **frames[second]}, ws))
        await asyncio.sleep(0.3)
        held = later.done()
        go.set()
        await asyncio.wait_for(asyncio.gather(first, later), 20)
    named = ("http://10.7.7.7:3128", None, _CA, ("3128",), ("10.7.7.7",))
    assert steps["first"] == [named, named]
    assert held is False, "the second frame ran inside the first one's build"
    if second == "an install":
        assert steps["other"] == [(None, "https://other.index/simple", None, (), ())]
    assert sandbox_host.platform_egress({}) == ({}, (), ())
    assert not (tmp_path / "state" / "egress-trust").exists()


@pytest.mark.asyncio
async def test_a_frame_that_fails_forgets_its_egress_too(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")

    held: list = []
    real = sandbox_host.note_egress

    def _note(block):
        real(block)
        held.append(sandbox_host.platform_egress({})[0])
    monkeypatch.setattr(sandbox_host, "note_egress", _note)
    with pytest.raises(AttributeError):
        await SessionManager(tools).sync_mcps(
            {"command_id": "c1", "mcps_to_install": ["no spec"], "egress": {"env": {"PIP_INDEX_URL": _SECRET_INDEX}}},
            SimpleNamespace())
    assert held == [{"PIP_INDEX_URL": _SECRET_INDEX}]
    assert sandbox_host.platform_egress({}) == ({}, (), ())


@pytest.mark.asyncio
async def test_only_the_installs_own_machine_takes_the_frames_egress(tools, own, tmp_path, monkeypatch):
    """A sync frame's block reaches the builds of the machine beside the
    platform, for that frame. A machine on another server and a paired
    computer keep their own rule: the block is not read, and the render's
    carves are theirs."""
    monkeypatch.setattr(sandbox_host, "state_dir", lambda: tmp_path / "state")
    spec = _tar_spec({"name": "t", "version": "1.0", "category": "community",
                      "server": {"runtime": "python", "source": "pypi:t@1.0"}}, version_hash="")
    during: list = []

    async def _install(mcp_dir, runtime, source, **kw):
        during.append(sandbox_host.platform_egress({})[2])
        return SimpleNamespace(ok=True, log="", version_hash="h", venv_python="", resolved_version="")
    await _sync(tools, {"mcps_to_install": [spec], "egress": _egress_block()}, _install)
    assert during == [("10.7.7.7",)] and sandbox_host.platform_egress({})[2] == ()
    # A frame on a machine with its own copy holds nothing: its build asks the render.
    asked: list[dict] = []

    def _carves(env):
        asked.append(env)
        return (), ("10.3.3.3",)
    monkeypatch.setattr(render, "build_carves", _carves)

    async def _own_install(mcp_dir, runtime, source, **kw):
        during.append(sandbox_host.platform_egress({})[2])
        step = tmp_path / "step"
        step.mkdir(exist_ok=True)
        argv, env = sandbox_host.build_command(own, ["/usr/bin/true"], {"PATH": "/usr/bin"}, "pip",
                                               Path(mcp_dir), step)
        head = argv[:argv.index("bwrap")]
        assert head[head.index("--allow-host") + 1] == "10.3.3.3"
        assert "HTTPS_PROXY" not in env and "SSL_CERT_FILE" not in env
        return SimpleNamespace(ok=True, log="", version_hash="h", venv_python="", resolved_version="")
    await _sync(own, {"mcps_to_install": [spec], "egress": _egress_block(allow_hosts=["10.9.9.9"])}, _own_install)
    assert during[-1] == () and len(asked) == 1


def _tool(mcps: Path, category: str, name: str, server: dict) -> Path:
    folder = mcps / category / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"name": name, "server": server}))
    return folder


def test_a_tool_with_no_environment_reads_as_missing_it(tmp_path):
    """A folder copied from elsewhere (an upgrade, a restored backup) may
    hold a tool's source and no environment this machine can run."""
    from satellite.sessions import mcp_install_support as mis
    python = {"runtime": "python", "source": "pypi:t@1.0"}
    never_built = _tool(tmp_path, "community", "never-built", python)
    dangling = _tool(tmp_path, "community", "dangling", {"runtime": "python", "source": "git+https://e/x@" + "a" * 40})
    (dangling / "venv" / "bin").mkdir(parents=True)
    (dangling / "venv" / "bin" / "python").symlink_to(tmp_path / "no-such-interpreter")
    built = _tool(tmp_path, "community", "built", python)
    (built / "venv" / "bin").mkdir(parents=True)
    (built / "venv" / "bin" / "python").symlink_to(sys.executable)
    node = {"runtime": "node", "source": "npm:@scope/pkg@1.0.0"}
    no_modules = _tool(tmp_path, "community", "no-modules", node)
    with_modules = _tool(tmp_path, "community", "with-modules", node)
    (with_modules / "node_modules" / "@scope" / "pkg").mkdir(parents=True)
    (with_modules / "node_modules" / "@scope" / "pkg" / "package.json").write_text('{"version": "1.0.0"}')
    remote = _tool(tmp_path, "community", "remote", {"runtime": "remote", "source": "remote:https://e/mcp"})
    bundled = _tool(tmp_path, "community", "bundled", {"runtime": "python"})

    def _missing(folder: Path) -> bool:
        return mis.environment_missing(folder, json.loads((folder / "manifest.json").read_text()))
    assert [_missing(f) for f in (never_built, dangling, no_modules)] == [True, True, True]
    assert [_missing(f) for f in (built, with_modules, remote, bundled)] == [False, False, False, False]
    assert mis.environment_missing(built, {"server": "not a table"}) is False


def test_an_environment_that_cannot_run_here_reads_as_missing_too(tmp_path):
    """What the installer's own readbacks read, with no process started: a
    Node tool whose modules do not hold its package, and a venv made for
    another Python minor that now resolves to this host's Python (a folder
    copied from a host with another Python)."""
    from satellite.sessions import mcp_install_support as mis
    node = {"runtime": "node", "source": "npm:pkg"}
    other_package = _tool(tmp_path, "community", "other-package", node)
    (other_package / "node_modules" / "left-pad").mkdir(parents=True)
    linked_out = _tool(tmp_path, "community", "linked-out", node)
    (linked_out / "node_modules").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "package.json").write_text('{"version": "1.0.0"}')
    (linked_out / "node_modules" / "pkg").symlink_to(tmp_path / "elsewhere")

    python = {"runtime": "python", "source": "pypi:t"}
    here = f"{sys.version_info[0]}.{sys.version_info[1]}"
    other = f"{sys.version_info[0]}.{sys.version_info[1] - 1}"

    def _venv(name: str, interpreter: Path, version: str | None) -> Path:
        folder = _tool(tmp_path, "community", name, python)
        (folder / "venv" / "bin").mkdir(parents=True)
        (folder / "venv" / "bin" / "python").symlink_to(interpreter)
        if version is not None:
            (folder / "venv" / "pyvenv.cfg").write_text(f"home = /x\nversion = {version}.4\n")
        return folder
    kept = tmp_path / "kept-interpreter" / "bin" / "python3"
    kept.parent.mkdir(parents=True)
    kept.write_text("#!/bin/sh\n")
    this_host = _venv("this-host", Path(sys.executable), here)
    copied = _venv("copied", Path(sys.executable), other)
    unknown = _venv("unknown", Path(sys.executable), None)
    own_interpreter = _venv("own-interpreter", kept, other)

    def _missing(folder: Path) -> bool:
        return mis.environment_missing(folder, json.loads((folder / "manifest.json").read_text()))
    assert [_missing(f) for f in (other_package, linked_out, copied)] == [True, True, True]
    # A venv on an interpreter kept for the tools was made for it, whatever its minor.
    assert [_missing(f) for f in (this_host, unknown, own_interpreter)] == [False, False, False]


@pytest.mark.asyncio
async def test_the_verify_reads_the_community_folder_alone_and_judges_each_tools_environment(tools, own, monkeypatch):
    import threading

    from satellite.sessions import session_manager as smod
    mcps = tools.mcps_dir
    python = {"runtime": "python", "source": "pypi:t@1.0"}
    built = _tool(mcps, "community", "built", python)
    (built / "venv" / "bin").mkdir(parents=True)
    (built / "venv" / "bin" / "python").symlink_to(sys.executable)
    _tool(mcps, "community", "copied", python)
    (mcps / "community" / "unreadable").mkdir()
    (mcps / "community" / "unreadable" / "manifest.json").write_text("{ not json")
    half = _tool(mcps, "community", "half", {"runtime": "remote"})
    (mcps / "community" / f".install-in-progress-{half.name}").write_text("pending")
    (mcps / "custom" / "memory-mcp" / "manifest.json").write_text(json.dumps(
        {"name": "memory-mcp", "server": {"runtime": "python", "source": "pypi:m@1"}}))
    threads: list[threading.Thread] = []
    real = smod.verify_installed

    def _verify(*a):
        threads.append(threading.current_thread())
        return real(*a)
    monkeypatch.setattr(smod, "verify_installed", _verify)

    async def _ack(cfg) -> dict:
        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)
        await SessionManager(cfg).sync_mcps_verify({"command_id": "v1"}, SimpleNamespace(enqueue_send=_send))
        assert sent[0]["type"] == "ack" and sent[0]["command_id"] == "v1" and sent[0]["status"] == "ok"
        return sent[0]["mcps"]
    mine = await _ack(tools)
    assert {n: (m["healthy"], m["category"]) for n, m in mine.items()} == {
        "built": (True, "community"), "copied": (False, "community"),
        "unreadable": (False, "community"), "half": (False, "community")}
    assert all(m["version_hash"] for m in mine.values())
    assert threads and all(t is not threading.main_thread() for t in threads)
    # A machine with its own copy reports every category by the marker alone, as before.
    theirs = await _ack(own)
    assert {n: m["healthy"] for n, m in theirs.items()} == {
        "built": True, "copied": True, "unreadable": True, "half": False, "memory-mcp": True}


def test_the_vendored_render_refuses_a_tool_folder_that_leaves_the_tools_folder(tmp_path):
    """The machine's own check: a description whose tool folder climbs out
    of the tools folder is refused here, whatever the sender built."""
    agents, mcps, _agent = _tree(tmp_path)
    desc = dict(_description(agents, mcps), mcp_binds=["." + "./sessions"])
    host = render.RenderHost(agents_root=str(agents), mcps_root=str(mcps), empty_dir=str(tmp_path),
                             launcher="/x", uid=1000, gid=1000)
    with pytest.raises(render.RenderRefused):
        render.render(render.SandboxDescription.from_dict(desc), host, ["true"])


def test_a_folder_swapped_for_a_link_after_any_check_is_never_written_or_removed_through(
        own, monkeypatch, tmp_path):
    """The writes and the close's removal go beneath the agent folder with no
    link followed, so a folder swapped for a link after any check made by
    name (here: one that always passes) still holds nothing back."""
    from satellite.sessions import session_files
    monkeypatch.setattr(session_files, "_no_link_on_the_way", lambda *a: True, raising=False)
    agent = own.agents_dir / "personal-assistant"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("stand-in")
    (agent / "users" / "alice").mkdir(parents=True, exist_ok=True)
    (agent / "users" / "alice" / "linked").symlink_to(outside)
    payload = {"host_files": _host_files({"users/alice/linked/keep.txt": "x", "users/alice/linked/new.txt": "x"}),
               "host_folders": ["users/alice/linked/made"]}
    assert session_files.materialize_agent_files(payload, "s-1", agent) == 0
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "stand-in"
    # The close: written while no link stood there, swapped before the close.
    session_files.materialize_agent_files(
        {"host_files": _host_files({"users/alice/wrote/keep.txt": "{}"})}, "s-2", agent)
    import shutil
    shutil.rmtree(agent / "users" / "alice" / "wrote")
    (agent / "users" / "alice" / "wrote").symlink_to(outside)
    session_files.wipe_agent_files("s-2")
    assert (outside / "keep.txt").read_text() == "stand-in"
