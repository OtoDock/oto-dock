"""A session's start on a compute machine: the engine's layer plans the
spawn (``prepare_spawn``), the remote start ships the plan and none of the
files a paired computer is sent, the record says its files were never
synced, and the close removes the prompt file the proxy wrote.

The plan carries the engine's command with no sandbox in front of it (the
binary by its bare name), its environment and the description of its
sandbox: the machine renders that description with its own values and
starts the command inside it.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import pytest

import config as app_config
from auth.path_policy import SecurityContext
from core import placement
from core.execution_layer import AgentConfig, SpawnPlan, SubscriptionHandle
from core.remote import tunnel_state
from core.remote.remote_execution import RemoteExecutionLayer
from core.sandbox import sandbox_render as render
from core.session.session_manager import get_layer_by_path

COMPUTE = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                          label="Compute one", satellite_version="0.5.173")
_CREDS = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                            "scopes": [], "subscriptionType": "", "rateLimitTier": ""}}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    pa = agents / "personal-assistant"
    for sub in ("config/context", "workspace", "users/alice/workspace", "users/alice/context",
                "users/alice/.claude", "users/alice/.codex"):
        (pa / sub).mkdir(parents=True)
    mcps = tmp_path / "mcps"
    (mcps / "custom" / "memory-mcp").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    monkeypatch.setattr(app_config, "MCPS_DIR", mcps)
    monkeypatch.setattr(app_config, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(app_config, "INTERNAL_LISTENER_PORT", 0, raising=False)
    # The preparation's readers that reach the store or the tree of a real install.
    from core.sandbox import session_config_dir
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *_a, **_k: [])

    async def _no_keys(*_a, **_k):
        return ""
    monkeypatch.setattr(session_config_dir, "provision_ssh_keys_for_sandbox", _no_keys)
    # Past a restart's first minutes: a test of that window says so.
    from services.engines import pool_bindings
    monkeypatch.setattr(pool_bindings, "within_boot_grace", lambda: False)
    return agents.resolve(), mcps.resolve()


def _on_disk(*parts: str) -> bool:
    return os.path.isfile(os.path.join(*parts))


def _names_in(folder: str) -> list[str]:
    return os.listdir(folder)


def _text_of(*parts: str) -> str:
    with open(os.path.join(*parts)) as fh:
        return fh.read()


def _ctx(**kw) -> SecurityContext:
    kw.setdefault("placement", COMPUTE)
    return SecurityContext(role="manager", username="alice", agent="personal-assistant",
                           is_admin_agent=False, **kw)


def _config(agents: Path, *, engine: str = ".claude", **overrides) -> AgentConfig:
    base = dict(
        agent_name="personal-assistant", execution_target="local",
        execution_path="claude-code-cli" if engine == ".claude" else "codex-cli",
        sandbox_host_claude_dir=str(agents / "personal-assistant" / "users" / "alice" / engine),
        system_prompt="You are the assistant.", model="claude-sonnet-5" if engine == ".claude" else "gpt-6.1-sol",
        effort="high", permission_mode="auto", client_type="dashboard", security_context=_ctx(),
        extra_env={}, user_sub="sub-alice",
    )
    base.update(overrides)
    return AgentConfig(**base)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_claude_plan_is_the_command_its_environment_and_its_files(roots):
    agents, _mcps = roots
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)})
    layer = get_layer_by_path("claude-code-cli")
    plan = await layer.prepare_spawn("s-1", config)
    assert isinstance(plan, SpawnPlan)
    # The binary by its bare name, the headless flags, the prompt file by its sandbox path.
    assert plan.argv[0] == "claude" and "-p" in plan.argv and "--session-id" in plan.argv
    prompt_arg = plan.argv[plan.argv.index("--append-system-prompt-file") + 1]
    assert prompt_arg.startswith("/users/alice/.claude/system-prompt-")
    assert _on_disk(plan.prompt_file) and Path(plan.prompt_file).name == Path(prompt_arg).name
    assert Path(plan.prompt_file).parent == Path(config.sandbox_host_claude_dir)
    # The environment: the proxy port, the sandbox config dir, no login blob.
    assert plan.env["PROXY_URL"] == f"http://127.0.0.1:{app_config.PORT}"
    assert plan.env["CLAUDE_CONFIG_DIR"] == "/users/alice/.claude"
    assert plan.env["OTO_SESSION_ID"] == "s-1" and "PROXY_API_KEY" in plan.env
    assert "_CLAUDE_CREDS_BLOB" not in plan.env and "CLAUDE_CODE_OAUTH_TOKEN" not in plan.env
    # The login went to its file in the platform's folder.
    assert plan.credential_file is True
    assert _on_disk(config.sandbox_host_claude_dir, ".credentials.json")
    assert plan.cwd == "/users/alice"
    assert plan.config_dir == config.sandbox_host_claude_dir
    assert plan.config_dir_relative == "users/alice/.claude"
    assert plan.local_model is False
    # The description survives the wire and names the proxy port by its marker.
    desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
    assert render.PROXY_FORWARD in desc.forwards and desc.agent == "personal-assistant"
    assert desc.cwd == plan.cwd
    # The host config dir is recorded for the session.
    from core.session.session_state import get_session_claude_dir
    assert get_session_claude_dir("s-1") == config.sandbox_host_claude_dir


@pytest.mark.asyncio
async def test_a_terminals_first_run_seed_runs_off_the_loop(roots, monkeypatch):
    import threading

    from services.engines import cli_settings_manager
    agents, _mcps = roots
    seen: list = []
    monkeypatch.setattr(cli_settings_manager, "seed_interactive_cli_config",
                        lambda *a, **k: seen.append(threading.current_thread()))
    await get_layer_by_path("claude-code-cli").prepare_spawn("s-3b", _config(agents, interactive=True))
    assert seen and seen[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_a_claude_terminals_plan_carries_the_tui_words(roots):
    agents, _mcps = roots
    config = _config(agents, interactive=True, interactive_theme="light")
    plan = await get_layer_by_path("claude-code-cli").prepare_spawn("s-3", config)
    assert "-p" not in plan.argv and "--permission-mode" in plan.argv
    assert plan.env["TERM"] == "xterm-256color" and plan.env["OTO_INTERACTIVE"] == "1"
    # The TUI's first-run seed went to the platform's folder.
    assert _names_in(config.sandbox_host_claude_dir)


@pytest.mark.asyncio
async def test_the_codex_plan_names_the_bare_binary_and_the_daemon_env(roots):
    agents, _mcps = roots
    config = _config(agents, engine=".codex")
    plan = await get_layer_by_path("codex-cli").prepare_spawn("s-4", config)
    # Headless: the binary alone (the machine's app-server client adds ``app-server``).
    assert plan.argv == ["codex"]
    assert plan.env["CODEX_HOME"] == "/users/alice/.codex"
    assert plan.env["OTO_HOOK_DENY_ONLY"] == "1" and plan.env["OTO_HOOK_NO_FORWARD"] == "1"
    assert plan.env["PROXY_URL"] == f"http://127.0.0.1:{app_config.PORT}"
    # ``config.toml`` and the session's own text and settings are the
    # platform folder's (the machine reads ``s-4.session.json``); no AGENTS.md.
    host = config.sandbox_host_claude_dir
    assert _on_disk(host, "config.toml") and _on_disk(host, "hooks.json")
    # At most the sandbox's empty mount point over the shared text's name.
    assert not _on_disk(host, "AGENTS.md") or _text_of(host, "AGENTS.md") == ""
    assert "CODEX_SQLITE_HOME" not in plan.env
    own = json.loads(_text_of(host, "s-4.session.json"))
    assert "You are the assistant." in own["developer_instructions"]
    assert plan.prompt_file == "" and plan.credential_file is False and plan.local_model is False
    assert plan.config_dir_relative == "users/alice/.codex" and plan.cwd == "/users/alice"
    assert list(plan.mcp_server_names) == list(own["config"].get("mcp_servers") or {})


@pytest.mark.asyncio
async def test_a_codex_terminals_plan_is_the_tui_argv(roots):
    agents, _mcps = roots
    config = _config(agents, engine=".codex", interactive=True, interactive_first_prompt="hello there")
    plan = await get_layer_by_path("codex-cli").prepare_spawn("s-5", config)
    assert plan.argv[0] == "codex" and "--no-alt-screen" in plan.argv
    assert plan.argv[plan.argv.index("-C") + 1] == "/users/alice"
    assert plan.argv[-1] == "hello there"
    assert plan.env["OTO_INTERACTIVE"] == "1"
    config = _config(agents, engine=".codex", interactive=True, resume=True, resume_handle="thread-9")
    plan = await get_layer_by_path("codex-cli").prepare_spawn("s-6", config)
    assert plan.argv[:3] == ["codex", "resume", "thread-9"]


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------

class _Cm:
    def __init__(self):
        from core.remote import satellite_link
        self.sent: list[dict] = []
        self.queues: dict[str, asyncio.Queue] = {}
        self.links = satellite_link.MachineLinks(None)

    def exit_state(self, machine_id):
        return 0, False

    def exit_report(self, machine_id):
        return {}

    def satellite_takes_start_context(self, mid):
        return True

    def is_connected(self, mid):
        return mid == "c-1"

    def tunnel_ok(self, mid):
        return True

    def tunnel_port(self, mid):
        return 43210

    def machine_at_capacity(self, mid):
        return False

    def satellite_engines(self, mid):
        return frozenset({"claude-code-cli", "codex-cli"})

    def satellite_name(self, mid):
        return "Compute one"

    def satellite_version(self, mid):
        return "0.5.172"

    def create_session_queue(self, mid, sid, path, *, maxsize=None):
        self.queues[sid] = asyncio.Queue()
        return self.queues[sid]

    def remove_session_queue(self, mid, sid):
        self.queues.pop(sid, None)

    def debit_reported_session(self, mid):
        pass

    async def send_command(self, mid, msg, timeout=60.0):
        self.sent.append(msg)
        return {}


def _plan(tmp_path: Path, **kw) -> SpawnPlan:
    prompt = tmp_path / "system-prompt-abc.json"
    prompt.write_text("{}")
    base = dict(
        sandbox={"agent": "personal-assistant", "forwards": [render.PROXY_FORWARD]},
        argv=["claude", "-p"], env={"PROXY_URL": f"http://127.0.0.1:{app_config.PORT}",
                                    "PROXY_API_KEY": "jwt", "OTO_SESSION_ID": "s-1"},
        cwd="/users/alice", config_dir=str(tmp_path / ".claude"), config_dir_relative="users/alice/.claude",
        prompt_file=str(prompt), credential_file=True, mcp_names=frozenset({"memory-mcp"}),
    )
    base.update(kw)
    return SpawnPlan(**base)


def _machine():
    return {"id": "c-1", "pairing_scope": "compute",
            "capabilities": json.dumps({"local_tunnel_port": 43210, "os": "linux"})}


@pytest.fixture
def layer():
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = _Cm()
    layer._sessions = {}
    layer._spawning = set()
    return layer


@pytest.mark.asyncio
async def test_the_compute_payload_carries_the_plan_and_none_of_the_files(layer, tmp_path):
    spawn = _plan(tmp_path)
    config = _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        plan = await layer._build_start_payload("s-1", config, "claude-code-cli", spawn=spawn)
    payload = plan.payload
    assert payload["env"] is spawn.env
    assert payload["sandbox"] == spawn.sandbox and payload["argv"] == spawn.argv
    assert payload["cwd"] == "/users/alice" and payload["tunnel_port"] == 43210
    assert tunnel_state.payload_port(payload) == 43210
    assert payload["claude_dir_relative"] == "users/alice/.claude"
    assert payload["resume"] is False and payload["session_id_for_resume"] == ""
    for absent in ("hook_scripts", "command_wrappers", "mcp_config", "credentials_json",
                   "system_prompt", "multi_value_envs", "disallowed_tools"):
        assert absent not in payload, absent
    assert plan.credential_file_delivered is True


@pytest.mark.asyncio
async def test_the_codex_compute_payload_names_what_the_thread_start_reads(layer, tmp_path):
    # The warm gate waits for the servers by the names config.toml gives them
    # (display-mcp's server is "display"), never the manifest names.
    spawn = _plan(tmp_path, argv=["codex"], config_dir_relative="users/alice/.codex",
                  mcp_names=frozenset({"memory-mcp", "display-mcp"}), mcp_server_names=("memory", "display"),
                  credential_file=False)
    config = _config(tmp_path, engine=".codex", resume_handle="thread-9")
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        plan = await layer._build_start_payload("s-1", config, "codex-cli", spawn=spawn)
    payload = plan.payload
    assert payload["codex_dir_relative"] == "users/alice/.codex" and payload["thread_id"] == "thread-9"
    assert payload["mcp_server_names"] == ["memory", "display"]
    assert payload["sandbox_mode"] and payload["effort"] and "codex_hooks_floor" in payload
    assert payload["local_model"] is False and plan.start_timeout_s == 60.0
    for absent in ("mcp_config_toml", "auth_json", "agents_md_content", "system_prompt", "hook_scripts",
                   "session_home"):
        assert absent not in payload, absent
    homed = _config(tmp_path, engine=".codex", resume_handle="thread-9",
                    session_home=str(tmp_path / ".codex" / "own" / "s-1"))
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        at_home = (await layer._build_start_payload("s-1", homed, "codex-cli", spawn=spawn)).payload
    spec = get_layer_by_path("codex-cli").capabilities.auth.session_home
    assert at_home["session_home"] == spec.to_dict()
    slow = await layer._build_start_payload("s-1", config, "codex-cli",
                                            spawn=_plan(tmp_path, argv=["codex"], local_model=True))
    assert slow.payload["local_model"] is True and slow.start_timeout_s > 60.0


# ---------------------------------------------------------------------------
# the start and the close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_compute_start_sends_the_plan_syncs_nothing_and_binds_the_local_file(
        layer, tmp_path, monkeypatch):
    spawn = _plan(tmp_path)
    config = _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir, subscription_id="sub-1",
                     chat_id="chat-1")
    claude = get_layer_by_path("claude-code-cli")
    recorded: list = []
    monkeypatch.setattr("storage.remote_store.set_chat_compute_machine", lambda c, m: recorded.append((c, m)))

    # A tool the build left out because the machine has not built it yet is
    # asked for before the spawn is prepared, and the start waits for nothing.
    asked: list[str] = []
    monkeypatch.setattr("services.remote.machine_tools.ask", asked.append)

    async def _prepared(_sid, _cfg):
        assert asked == ["c-1"]
        return spawn
    monkeypatch.setattr(claude, "prepare_spawn", _prepared)

    async def _never(*_a, **_k):
        pytest.fail("a compute start synced files to the machine")
    monkeypatch.setattr(layer, "_sync_machine_files", _never)
    provisioned: list[str] = []

    async def _gateway(sid, _cfg, _payload, mid):
        provisioned.append(f"{sid}@{mid}")
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)
    targets: list = []
    from services.engines import subscription_pool, token_fanout
    monkeypatch.setattr(token_fanout, "register_session_target", lambda sid, t: targets.append((sid, t)))

    def _bound(*_a, **_k):
        f: Future = Future()
        f.set_result(None)
        return f
    monkeypatch.setattr(subscription_pool, "bind_session", _bound)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        await layer._start_session_impl("s-1", config, [])

    frame = layer._cm.sent[-1]
    assert frame["type"] == "start_session" and frame["agent_slug"] == "personal-assistant"
    assert frame["config"]["sandbox"] == spawn.sandbox and frame["config"]["argv"] == spawn.argv
    assert frame["config"]["env"] is spawn.env
    info = layer._sessions["s-1"]
    assert info.machine_id == "c-1" and info.files_synced is False
    assert info.used_mcps == {"memory-mcp"} and info.tunnel_port == 43210
    assert info.prompt_file == spawn.prompt_file
    # The gateway is provisioned for the compute machine too: the push makes
    # the compute cut (no gateway lease, the exit's values alone).
    assert provisioned == ["s-1@c-1"]
    # The rotation target is the platform's own file.
    assert targets and targets[0][1].host_dir == spawn.config_dir and targets[0][1].machine_id == ""
    # The chat's engine record is on the platform's folder: none recorded.
    assert recorded == [("chat-1", "")]

    # The close removes the prompt file beneath its root (the plan's file
    # sits in the test's own folder: that folder is an agent's folder here).
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path.parent)
    await layer.close_session("s-1")
    assert not _on_disk(spawn.prompt_file)
    assert layer._cm.sent[-1]["type"] == "close_session" and "s-1" not in layer._sessions


@pytest.mark.asyncio
async def test_a_start_the_machine_fails_records_no_place_and_holds_no_session(layer, tmp_path, monkeypatch):
    """Where a chat's engine files are is written once the machine has
    started the session: a start the machine fails writes nothing for the
    chat and leaves no record and no queue of the session."""
    spawn = _plan(tmp_path)
    config = _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir, chat_id="chat-1")
    recorded: list = []
    monkeypatch.setattr("storage.remote_store.set_chat_compute_machine", lambda c, m: recorded.append((c, m)))
    monkeypatch.setattr("services.remote.machine_tools.ask", lambda _machine_id: None)

    async def _prepared(_sid, _cfg):
        return spawn
    monkeypatch.setattr(get_layer_by_path("claude-code-cli"), "prepare_spawn", _prepared)

    async def _gateway(*_a, **_k):
        pass
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)

    async def _fails(mid, msg, timeout=60.0):
        layer._cm.sent.append(msg)
        raise RuntimeError("the engine did not start")
    monkeypatch.setattr(layer._cm, "send_command", _fails)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()), \
            pytest.raises(RuntimeError, match="the engine did not start"):
        await layer._start_session_impl("s-1", config, [])
    assert layer._cm.sent[-1]["type"] == "start_session"
    assert recorded == [] and "s-1" not in layer._sessions and "s-1" not in layer._cm.queues


@pytest.mark.asyncio
async def test_a_paired_machines_start_still_syncs(layer, tmp_path, monkeypatch):
    """The same start for an admin's machine: no plan asked, the files
    synced, the record's files marked synced."""
    admin = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="c-1", label="Office")
    config = _config(tmp_path, execution_target="c-1", chat_id="chat-9",
                     security_context=SecurityContext(role="manager", username="alice",
                                                      agent="personal-assistant", is_admin_agent=False,
                                                      placement=admin))
    claude = get_layer_by_path("claude-code-cli")
    recorded: list = []
    monkeypatch.setattr("storage.remote_store.set_chat_compute_machine", lambda c, m: recorded.append((c, m)))

    async def _not_asked(*_a):
        pytest.fail("a paired machine's start asked for a spawn plan")
    monkeypatch.setattr(claude, "prepare_spawn", _not_asked)
    # Its own sync installs at the start: no ahead run is asked for here.
    monkeypatch.setattr("services.remote.machine_tools.ask",
                        lambda mid: pytest.fail("a paired machine's start asked for an ahead run"))
    synced: list[str] = []

    async def _sync(sid, _cfg, _mid, payload, _adapter, assigned, *, own_copy=False):
        synced.append(sid)
        return payload, assigned
    monkeypatch.setattr(layer, "_sync_machine_files", _sync)

    async def _gateway(*_a, **_k):
        pass
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)
    from core.remote import remote_session_start
    monkeypatch.setattr(remote_session_start, "placement_refuses", lambda *_a: False)
    with patch("storage.remote_store.get_remote_machine", return_value={**_machine(), "pairing_scope": "admin"}):
        await layer._start_session_impl("s-9", config, [])
    assert synced == ["s-9"]
    # A chat moved to a paired machine lets go of a machine on another host.
    assert recorded == [("chat-9", "")]
    info = layer._sessions["s-9"]
    assert info.files_synced is True and info.prompt_file == ""
    assert "sandbox" not in layer._cm.sent[-1]["config"] and "hook_scripts" in layer._cm.sent[-1]["config"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,door", [(".claude", "turn"), (".codex", "hook")])
async def test_a_fresh_start_on_a_compute_machine_holds_none_of_the_starts_data(layer, tmp_path, engine, door):
    """A fresh conversation under the session's id, on a compute machine
    too: its record goes (``start_context.reset``), and a Codex start there
    takes its data through its start hook (the machine reads the platform's
    hooks)."""
    from core.session import session_state, start_context
    spawn = _plan(tmp_path)
    config = _config(tmp_path, engine=engine, sandbox_host_claude_dir=spawn.config_dir, resume=False)
    path = "claude-code-cli" if engine == ".claude" else "codex-cli"
    session_state._session_start_args["s-fresh"] = {"agent": "personal-assistant"}
    start_context._held["s-fresh"] = {"memory": "hash-of-another-conversation"}
    try:
        with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
            await layer._build_start_payload("s-fresh", config, path, spawn=spawn)
        assert "s-fresh" not in start_context._held
        assert start_context._door("s-fresh") == door
    finally:
        session_state._session_start_args.pop("s-fresh", None)
        start_context.ended("s-fresh")


@pytest.mark.asyncio
async def test_a_resume_the_machine_refused_holds_none_of_the_starts_data(layer, tmp_path, monkeypatch):
    """The engine on the machine opened a new conversation (its ack's
    ``resume_refused``): its record goes, whatever chat it names."""
    from core.session import session_state, start_context
    admin = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="c-1", label="Office")
    config = _config(tmp_path, execution_target="c-1", resume=True,
                     security_context=SecurityContext(role="manager", username="alice",
                                                      agent="personal-assistant", is_admin_agent=False,
                                                      placement=admin))

    async def _sync(sid, _cfg, _mid, payload, _adapter, assigned, *, own_copy=False):
        return payload, assigned
    monkeypatch.setattr(layer, "_sync_machine_files", _sync)

    async def _gateway(*_a, **_k):
        pass
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)

    async def _refused(mid, msg, timeout=60.0):
        layer._cm.sent.append(msg)
        return {"resume_refused": True, "incarnation": "boot-7"}
    monkeypatch.setattr(layer._cm, "send_command", _refused)
    from core.remote import remote_session_start
    monkeypatch.setattr(remote_session_start, "placement_refuses", lambda *_a: False)
    session_state._session_start_args["s-rr"] = {"agent": "personal-assistant"}
    start_context._held["s-rr"] = {"memory": "hash-of-the-refused-conversation"}
    try:
        with patch("storage.remote_store.get_remote_machine", return_value={**_machine(), "pairing_scope": "admin"}):
            await layer._start_session_impl("s-rr", config, [])
        assert "s-rr" not in start_context._held and "s-rr" in start_context._pending
        # The record knows the object the machine started (its ack's
        # incarnation): a word about an earlier object of the id is told apart.
        assert layer._sessions["s-rr"].incarnation == "boot-7"
    finally:
        session_state._session_start_args.pop("s-rr", None)
        start_context.ended("s-rr")


def test_an_interactive_task_on_compute_asks_the_compute_machine_for_its_terminal(temp_db, monkeypatch, tmp_path):
    """The task builder's terminal check asks the machine that runs the
    task: for a compute placement that is the compute machine, whose
    target value is the platform's place (asking by it answered False, so
    an interactive task ran headless on compute)."""
    from types import SimpleNamespace

    from core.config.task_config_builder import build_task_agent_config
    from services.remote import remote_status
    from tests.fixtures.loop_builder_rig import AGENT as RIG_AGENT, install, run
    install(monkeypatch, tmp_path)
    monkeypatch.setattr(remote_status, "serving_compute_machine",
                        lambda path="", **_kw: {"id": "c-1", "name": "Compute one", "pairing_scope": "compute"})
    asked: list[str] = []

    from core.remote import satellite_link

    class _Cm:
        links = satellite_link.MachineLinks(None)

        def satellite_supports_pty(self, mid):
            asked.append(mid)
            return True

        def exit_report(self, mid):
            return {}

        def is_connected(self, mid):
            return True

        def tunnel_ok(self, mid):
            return True

        def is_compute(self, mid):
            return mid == "c-1"

        def exit_state(self, mid):
            return 0, False
    cm = _Cm()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: cm)
    task = SimpleNamespace(scope="agent", created_by=None, task_type="one_time", notification_mode="manual",
                           name="t", id="dyn-pty", override_execution_path="claude-code-cli",
                           override_execution_mode="interactive")
    cfg = run(build_task_agent_config(RIG_AGENT, task, "sid-compute-pty"))
    assert cfg.security_context.placement.kind == placement.KIND_COMPUTE
    assert cfg.interactive is True
    assert asked == ["c-1"]


# ---------------------------------------------------------------------------
# a compute machine below the exit's floor: the agent socket by name, no exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_listener_the_plan_names_the_agent_socket_and_no_exit(roots, monkeypatch, tmp_path):
    """A compute machine that reports no listener runs no exit: the plan's
    description carries the socket's name for the machine to open, its
    environment the socket's path, and no exit port or proxy variable."""
    from core.credentials import ssh_signer
    agents, _mcps = roots
    monkeypatch.setattr(app_config, "PLATFORM_DATA_DIR", tmp_path / "data")
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)})
    layer = get_layer_by_path("claude-code-cli")
    plan = await layer.prepare_spawn("s-1", config)
    assert plan.sandbox["ssh_agent"] == ssh_signer.socket_name("s-1")
    assert plan.env["SSH_AUTH_SOCK"] == "/tmp/oto-ssh-agent.sock"
    assert "exit_port" not in plan.sandbox and "HTTPS_PROXY" not in plan.env
    assert not (app_config.SESSIONS_DIR / "ssh-agent").exists()
    assert not _on_disk(config.sandbox_host_claude_dir, "exit-ca.pem")


@pytest.mark.asyncio
async def test_the_compute_payload_names_the_socket_for_the_machine_to_open(layer, tmp_path):
    spawn = _plan(tmp_path, sandbox={"agent": "personal-assistant", "forwards": [render.PROXY_FORWARD],
                                     "ssh_agent": "0123456789ab.sock"})
    config = _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        plan = await layer._build_start_payload("s-1", config, "claude-code-cli", spawn=spawn)
    assert plan.payload["ssh_agent"] == {"public_keys": {}}
    plain = _plan(tmp_path)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        plan = await layer._build_start_payload("s-1", config, "claude-code-cli", spawn=plain)
    assert "ssh_agent" not in plan.payload


# ---------------------------------------------------------------------------
# the credential exit on the compute machine (0.5.175)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_a_compute_exit_the_plan_names_the_splice_and_the_payload_carries_the_door(
        roots, layer, monkeypatch, tmp_path):
    """A compute machine that reports its exit gets ``exit_port`` on the
    description, the sandbox's bundle path and the session's secret in the
    plan env, and the start payload's ``exit`` block; one with no listener,
    or one that did not answer, gets the shape without it."""
    from core.credentials import credential_exit, mcp_broker, mcp_gateway
    from core.credentials.mcp_gateway import ExitCredential
    from core.remote.mcp_gateway_push import token_hash_of
    from core.sandbox.sandbox_render import EXIT_BUNDLE_PATH
    agents, _mcps = roots
    monkeypatch.setattr(app_config, "PLATFORM_DATA_DIR", tmp_path / "data")
    served = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
                                             satellite_version="0.5.175", exit_port=41112, exit_ok=True)
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)},
                     security_context=_ctx(placement=served))
    try:
        plan = await get_layer_by_path("claude-code-cli").prepare_spawn("s-9", config)
        # The splice's port inside the namespace (``credential_exit._sandbox_exit_port``).
        assert plan.sandbox["exit_port"] == 41443
        secret = credential_exit.machine_secret("s-9")
        assert secret and plan.env["HTTPS_PROXY"] == f"http://oto:{secret}@127.0.0.1:41443"
        assert plan.env["SSL_CERT_FILE"] == EXIT_BUNDLE_PATH and plan.env["NODE_EXTRA_CA_CERTS"] == EXIT_BUNDLE_PATH
        assert not _on_disk(config.sandbox_host_claude_dir, "exit-ca.pem")
        # The payload: the block with the session's secret and the hash of the plan's own token.
        cred = ExitCredential(placeholder=credential_exit.new_placeholder(), header="xi-api-key",
                              hosts=("api.elevenlabs.io",), allowlist_key="music-gen-mcp", manifest="music-gen-mcp",
                              env_name="ELEVENLABS_API_KEY", value="xk-9f3a")
        monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: True)
        config.mcp_secret_bundles = {"music-gen-mcp": mcp_broker.SecretBundle(env={}, exit=(cred,))}
        spawn = _plan(tmp_path, sandbox={**plan.sandbox}, env=dict(plan.env))
        with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
            built = await layer._build_start_payload("s-9", config, "claude-code-cli", spawn=spawn)
        block = built.payload["exit"]
        assert block["secret"] == secret and block["token_hash"] == token_hash_of(plan.env["PROXY_API_KEY"])
        assert block["entries"][0]["placeholder"] == cred.placeholder and "xk-9f3a" not in str(block)
        assert block["carves"] == sorted(plan.sandbox.get("allow_hosts", ()))
    finally:
        credential_exit.forget("s-9")
    # With no listener, or one that did not answer: the description without the exit, no block.
    for older in (placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
                                                  satellite_version="0.5.196", exit_port=41112),
                  placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
                                                  satellite_version="0.5.196")):
        config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)},
                         security_context=_ctx(placement=older))
        plan = await get_layer_by_path("claude-code-cli").prepare_spawn("s-10", config)
        assert "exit_port" not in plan.sandbox and "HTTPS_PROXY" not in plan.env
        assert credential_exit.machine_secret("s-10") == ""


@pytest.mark.asyncio
async def test_a_compute_session_with_no_declared_tool_still_gets_its_door(roots, layer, monkeypatch, tmp_path):
    """The compute sandbox names the machine's exit for every process, so a
    session whose tools hold no exit entry gets a door with no entry (a
    plain tunnel): without it each of its HTTPS connections is refused 407
    and, on a compute folder where no door was ever opened, the render
    refuses the start for want of a bundle."""
    from core.credentials import credential_exit
    from core.remote.mcp_gateway_push import token_hash_of
    agents, _mcps = roots
    monkeypatch.setattr(app_config, "PLATFORM_DATA_DIR", tmp_path / "data")
    served = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
                                             satellite_version="0.5.175", exit_port=41112, exit_ok=True)
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)},
                     security_context=_ctx(placement=served))
    try:
        plan = await get_layer_by_path("claude-code-cli").prepare_spawn("s-11", config)
        assert plan.sandbox["exit_port"] == 41443 and plan.env["HTTPS_PROXY"].endswith("@127.0.0.1:41443")
        config.mcp_secret_bundles = {}
        spawn = _plan(tmp_path, sandbox={**plan.sandbox}, env=dict(plan.env))
        with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
            built = await layer._build_start_payload("s-11", config, "claude-code-cli", spawn=spawn)
        block = built.payload["exit"]
        assert block["secret"] == credential_exit.machine_secret("s-11") and block["entries"] == []
        assert block["token_hash"] == token_hash_of(plan.env["PROXY_API_KEY"])
    finally:
        credential_exit.forget("s-11")


@pytest.mark.asyncio
async def test_the_gateway_provisioning_pushes_the_exits_values_to_a_compute_machine(layer, tmp_path, monkeypatch):
    """A compute session's provisioning pushes before the spawn frame as a
    paired machine's does: ``push_session`` makes the compute cut (no
    gateway lease there, the exit's values alone), so the first request at
    the machine's exit finds its value. Before this the compute start passed
    ``push=False`` and every swap at a compute exit answered the no-value
    refusal."""
    from core.credentials import credential_exit, mcp_broker
    from core.credentials.mcp_gateway import ExitCredential
    from core.remote import mcp_gateway_push
    from tests.conftest import live_session_token
    monkeypatch.setattr(app_config, "PLATFORM_DATA_DIR", tmp_path / "data")
    sid = "s-exit-push"
    cred = ExitCredential(placeholder=credential_exit.new_placeholder(), header="xi-api-key",
                          hosts=("api.elevenlabs.io",), allowlist_key="music-gen-mcp", manifest="music-gen-mcp",
                          env_name="ELEVENLABS_API_KEY", value="xk-9f3a")
    mcp_broker.provision(sid, {"music-gen-mcp": mcp_broker.SecretBundle(env={"ELEVENLABS_API_KEY": cred.placeholder},
                                                                         exit=(cred,))})
    token = live_session_token(sid, "personal-assistant", "user-admin")
    calls: list[tuple[str, str, str]] = []

    async def _push(session_id, machine_id, token_hash, **_kw):
        calls.append((session_id, machine_id, token_hash))
        return 1
    monkeypatch.setattr(mcp_gateway_push, "push_session", _push)
    try:
        await layer._provision_gateway(sid, _config(tmp_path), {"env": {"PROXY_API_KEY": token}}, "c-1")
    finally:
        mcp_broker.purge_session(sid)
    assert calls == [(sid, "c-1", mcp_gateway_push.token_hash_of(token))]


@pytest.mark.asyncio
async def test_a_machine_with_its_own_copy_gets_the_plan_the_sync_and_the_prepared_files(
        layer, tmp_path, monkeypatch):
    """A compute machine on another host: the plan as on any compute
    machine, then the paired machine's sync with its own-copy word, the
    prepared files through the broker after it, the record synced and of
    the compute kind, the login rotated on the machine, the chat recorded
    on the machine."""
    own = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1",
                                          label="Rack two", own_copy=True)
    config = _config(tmp_path, chat_id="chat-1", subscription_id="sub-1",
                     security_context=SecurityContext(role="manager", username="alice",
                                                      agent="personal-assistant", is_admin_agent=False,
                                                      placement=own))
    spawn = _plan(tmp_path, config_dir=config.sandbox_host_claude_dir)
    claude = get_layer_by_path("claude-code-cli")

    async def _prepared(_sid, _cfg):
        return spawn
    monkeypatch.setattr(claude, "prepare_spawn", _prepared)
    order: list[str] = []

    async def _sync(sid, _cfg, _mid, payload, _adapter, assigned, *, own_copy=False):
        order.append(f"sync:{own_copy}")
        return payload, assigned
    monkeypatch.setattr(layer, "_sync_machine_files", _sync)

    async def _files(sid, _cfg, plan, payload):
        order.append("files")
        payload["host_files"] = {"users/alice/.claude/settings.json": {"content_b64": "e30=", "mode": 0o600}}
    monkeypatch.setattr(layer, "_send_prepared_files", _files)

    async def _gateway(*_a, **_k):
        pass
    monkeypatch.setattr(layer, "_provision_gateway", _gateway)
    targets: list = []
    from services.engines import subscription_pool, token_fanout
    monkeypatch.setattr(token_fanout, "register_session_target", lambda sid, t: targets.append(t))

    def _bound(*_a, **_k):
        f: Future = Future()
        f.set_result(None)
        return f
    monkeypatch.setattr(subscription_pool, "bind_session", _bound)
    recorded: list = []
    monkeypatch.setattr("storage.remote_store.set_chat_compute_machine", lambda c, m: recorded.append((c, m)))
    with patch("storage.remote_store.get_remote_machine",
               return_value={**_machine(), "compute_address": "192.168.50.7"}):
        await layer._start_session_impl("s-1", config, [])
    assert order == ["sync:True", "files"]
    frame = layer._cm.sent[-1]
    assert frame["config"]["sandbox"] == spawn.sandbox and "users/alice/.claude/settings.json" in frame["config"]["host_files"]
    info = layer._sessions["s-1"]
    assert info.files_synced is True and info.compute is True
    assert targets and targets[0].machine_id == "c-1"
    assert recorded == [("chat-1", "c-1")]


@pytest.mark.asyncio
async def test_the_sync_for_a_machine_with_its_own_copy_carries_the_sessions_person_alone(layer, tmp_path, monkeypatch):
    seen: dict = {}

    async def _sync(machine_id, agent, **kw):
        seen.update(kw)
    monkeypatch.setattr(layer, "_initial_workspace_sync", _sync, raising=False)
    from core.remote import install_registry
    from services.mcp import mcp_sync

    async def _reg(*_a, **_k):
        pass
    monkeypatch.setattr(install_registry, "register", _reg)
    monkeypatch.setattr(install_registry, "unregister", _reg)

    async def _lifted(_cfg, assigned):
        return set()
    monkeypatch.setattr(mcp_sync, "lifted_by_context", _lifted)
    waited: dict = {}

    async def _mcps(mid, sid, names, **kw):
        waited.update(kw)
        from types import SimpleNamespace
        return SimpleNamespace(still_installing=set(), not_installed=set(), excluded_names=set(),
                               failed={}, warmup_failed={}, memoized=set(), watch=None)
    monkeypatch.setattr(mcp_sync, "sync_mcps_for_session", _mcps)
    own = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c-1", own_copy=True)
    config = _config(tmp_path, security_context=SecurityContext(
        role="viewer", username="bob", session_scope="user", agent="personal-assistant",
        is_admin_agent=False, placement=own))
    with patch("storage.remote_store.get_remote_machine",
               return_value={**_machine(), "compute_address": "192.168.50.7"}):
        await layer._sync_machine_files("s-2", config, "c-1", {}, None, {"memory-mcp"}, own_copy=True)
    # Only bob's own folder, with bob's role, merged as one of many people on
    # that machine; every install waited for.
    assert seen["target_username"] == "bob" and seen["target_role"] == "viewer"
    assert seen["shared_by_many"] is True
    assert waited["early_start_s"] is None


class _AliveLayer:
    def __init__(self, alive: bool):
        self.alive = alive
        self.asked: list[str] = []

    async def is_session_alive(self, sid):
        self.asked.append(sid)
        return self.alive


@pytest.mark.asyncio
async def test_after_a_restart_a_chat_takes_back_its_live_session_on_another_host(monkeypatch):
    # The chat's pin is the platform's place; its session lives on a compute
    # machine with its own copy, re-adopted after the restart. The warm-up
    # asks the layer that holds it, never the platform's own (which reads it
    # dead, and the resume probe then finds no record on the platform: the
    # chat would continue fresh).
    import ws.dashboard  # noqa: F401  (the ws package's circular import)
    from ws import dashboard_warmup as dw
    remote, local = _AliveLayer(True), _AliveLayer(False)
    chat = {"id": "c1", "agent": "personal-assistant", "session_id": "s-live",
            "execution_target": "local", "execution_path": "claude-code-cli",
            "compute_machine": "m-rack-two", "model": "claude-opus-5-5"}
    monkeypatch.setattr(dw.task_store, "get_chat", lambda cid: chat)
    monkeypatch.setattr(dw, "layer_for_chat", lambda agent, row, **kw: remote)
    monkeypatch.setattr(dw, "get_execution_layer", lambda *a, **kw: local)
    monkeypatch.setattr(dw, "grace_layer", lambda sid, fallback: fallback)
    monkeypatch.setattr(dw, "get_session_mode", lambda sid: "default")
    # A machine holds the chat's session: the warm-up's check for a compute
    # machine finds it held and the chat answers for itself.
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "live_session",
                        lambda sid: session_manager.LiveSession(remote, "m-rack-two") if sid == "s-live" else None)

    async def _mode(cid, mode, path):
        return mode
    monkeypatch.setattr(dw, "coerce_chat_mode", _mode)
    monkeypatch.setattr("ws.dashboard._rewarm_chat_allowed", lambda *a: True)

    async def _slot(*a, **kw):
        return True
    monkeypatch.setattr("core.concurrency.acquire_chat_slot", _slot)
    c = dw.WarmupController.__new__(dw.WarmupController)
    sent: list[dict] = []

    async def _send(frame):
        sent.append(frame)

    async def _persist(*a):
        return None
    c._send, c._persist_first_prompt = _send, _persist
    c.user_sub, c.user_agents, c._view_only = "dev-admin", {"personal-assistant"}, False
    c.chat_id, c.session_id, c.layer = "c1", None, None
    plan = dw._WarmupPlan(agent="personal-assistant", msg={"text": "hi"}, requested_model="",
                          requested_exec_mode="", requested_theme="", warmup_role="manager",
                          effective_exec_path="claude-code-cli", permission_mode="default",
                          cid="c1", pw_task=None)
    assert await c._warm_existing_chat(plan) == "inline"
    assert remote.asked == ["s-live"] and local.asked == []
    assert c.session_id == "s-live" and sent[-1]["session_id"] == "s-live"


# ---------------------------------------------------------------------------
# a session's own home on a compute machine (core/sandbox/session_home)
# ---------------------------------------------------------------------------

_SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
_SIBLING = "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_a_compute_sessions_plan_runs_from_its_own_home(roots):
    """The platform makes the home in the scope's folder the machine shares:
    the sign-in there and nowhere else, the scope's settings bytes, the links;
    the engine reads it; the hooks and the prompt stay named by the folder."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".claude"
    store.mkdir(parents=True, exist_ok=True)
    (store / "settings.json").write_text('{"hooks": {}}\n')   # the funnel's write
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)},
                     session_home=str(store / "own" / _SID))
    plan = await get_layer_by_path("claude-code-cli").prepare_spawn(_SID, config)
    home = store / "own" / _SID
    assert plan.env["CLAUDE_CONFIG_DIR"] == f"/users/alice/.claude/own/{_SID}"
    assert plan.env["OTO_HOOKS_DIR"] == "/users/alice/.claude"
    assert plan.argv[plan.argv.index("--append-system-prompt-file") + 1].startswith(
        "/users/alice/.claude/system-prompt-")
    assert json.loads((home / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "at"
    assert not (store / ".credentials.json").exists()
    assert (home / "settings.json").read_bytes() == (store / "settings.json").read_bytes()
    assert (home / "projects").is_symlink()
    desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
    assert desc.home == render.Bind(render.ROOT_AGENT, f"users/alice/.claude/own/{_SID}",
                                    f"/users/alice/.claude/own/{_SID}", True)
    assert render.Bind(render.ROOT_AGENT, "users/alice/.claude/own", "/users/alice/.claude/own",
                       False) in desc.home_masks
    # The agent scope's engine folders are masked for a person's session: no
    # mask inside a mask (bwrap could not make its place there).
    assert not [m for m in desc.home_masks if m.sandbox.startswith("/workspace/")]
    # A session that takes the agent's keys has its SSH agent socket bound.
    assert desc.ssh_agent
    _runs_under_bwrap(desc, agents)


_AUTH = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-11-14T00:00:00Z",
         "tokens": {"id_token": "stand-in", "access_token": "at", "refresh_token": "", "account_id": "acct-1"}}
_CODEX_LINKS = ("sessions", "skills", "prompts", "thread-writer-locks", "rules", "archived_sessions")


@pytest.mark.asyncio
async def test_a_codex_compute_session_runs_from_its_own_home(roots):
    """Codex declares a home: the platform writes the session's settings, hooks,
    profile, text and sign-in there, points the engine at it and its databases
    at the store, and leaves the store nothing a terminal reads as its
    project's (a 1.9.1 install's leftovers go at the first home start)."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".codex"
    for leftover in ("config.toml", "hooks.json", "auth.json", "models.json"):
        (store / leftover).write_text("left by an earlier version")
    (store / "installation_id").write_bytes(b"0b6f3c2e-1d2a-4f7e-9a51-3c4d5e6f7a8b")
    config = _config(agents, engine=".codex", interactive=True,
                     extra_env={"_CODEX_AUTH_JSON": json.dumps(_AUTH)},
                     session_home=str(store / "own" / _SID))
    plan = await get_layer_by_path("codex-cli").prepare_spawn(_SID, config)
    home = store / "own" / _SID
    assert plan.env["CODEX_HOME"] == f"/users/alice/.codex/own/{_SID}"
    assert plan.env["CODEX_SQLITE_HOME"] == "/users/alice/.codex"
    assert plan.env["OTO_HOOKS_DIR"] == "/users/alice/.codex"
    for name in ("config.toml", "hooks.json", "auth.json", f"{_SID}.config.toml", f"{_SID}.session.json"):
        assert (home / name).is_file(), name
    for name in ("config.toml", "hooks.json", "auth.json", "models.json",
                 f"{_SID}.config.toml", f"{_SID}.session.json"):
        assert not (store / name).exists(), name
    assert json.loads((home / "auth.json").read_text())["tokens"]["access_token"] == "at"
    # The hooks name the store's scripts, which stay there.
    assert '"/users/alice/.codex/permission_gate.py' in (home / "hooks.json").read_text()
    for link in _CODEX_LINKS:
        assert (home / link).is_symlink(), link
    assert (home / "installation_id").read_bytes() == (store / "installation_id").read_bytes()
    assert plan.credential_file is True
    assert plan.argv[plan.argv.index("-p") + 1] == _SID
    desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
    assert desc.home == render.Bind(render.ROOT_AGENT, f"users/alice/.codex/own/{_SID}",
                                    f"/users/alice/.codex/own/{_SID}", True)
    # A session that takes the agent's keys has its SSH agent socket bound.
    assert desc.ssh_agent
    _runs_under_bwrap(desc, agents)


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True], ids=["chat", "terminal"])
async def test_a_codex_session_on_an_openai_key_signs_in_from_its_file(roots, interactive):
    """An OpenAI key reaches Codex as the sign-in file in the session's home,
    the one place the app-server reads a key from, by the way a ChatGPT
    sign-in takes, and stays out of the session's environment. Handed in the
    environment alone it was never sent, and every turn ended unauthorized."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".codex"
    layer = get_layer_by_path("codex-cli")
    key = "sk-stand-in-openai-key"
    env = layer.subscription_env(SubscriptionHandle(
        subscription_id="sub-key", layer="codex-cli", provider="openai", auth_type="api_key",
        api_key=key, oauth_access_token=None, endpoint_url=None))
    config = _config(agents, engine=".codex", extra_env=env, interactive=interactive,
                     session_home=str(store / "own" / _SID))
    plan = await layer.prepare_spawn(_SID, config)
    auth = store / "own" / _SID / "auth.json"
    # The file Codex's own ``login --with-api-key`` writes.
    assert json.loads(auth.read_text()) == {"auth_mode": "apikey", "OPENAI_API_KEY": key}
    assert auth.stat().st_mode & 0o777 == 0o600
    assert key not in json.dumps(plan.env) and key not in json.dumps(plan.argv)
    assert plan.credential_file is True


@pytest.mark.asyncio
async def test_a_store_a_live_session_still_reads_keeps_its_files(roots):
    """A Codex session that runs from the store itself (started before this
    version, alive on a compute machine through the platform's restart) keeps
    the files it reads while a session in a home starts beside it."""
    from services.engines import pool_bindings
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    (store / "config.toml").write_text("the live session's")
    with pool_bindings._session_maps_lock:
        pool_bindings._session_scope_keys["s-old"] = f"c-1:{store}"
    try:
        config = _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID))
        await get_layer_by_path("codex-cli").prepare_spawn(_SID, config)
    finally:
        with pool_bindings._session_maps_lock:
            pool_bindings._session_scope_keys.pop("s-old", None)
    assert (store / "config.toml").read_text() == "the live session's"
    assert (store / "own" / _SID / "config.toml").is_file()


@pytest.mark.asyncio
async def test_a_live_session_in_a_home_keeps_none_of_the_stores_old_files(roots):
    """Only a session that runs from the store itself keeps the store's old
    files: one in a home of its own beside the start reads none of them."""
    from services.engines import pool_bindings
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    (store / "config.toml").write_text("an earlier version's")
    with pool_bindings._session_maps_lock:
        pool_bindings._session_scope_keys["s-other"] = f"c-1:{store / 'own' / _SIBLING}"
    try:
        config = _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID))
        await get_layer_by_path("codex-cli").prepare_spawn(_SID, config)
    finally:
        with pool_bindings._session_maps_lock:
            pool_bindings._session_scope_keys.pop("s-other", None)
    assert not (store / "config.toml").exists()


class _Machines:
    """The connection manager as the guard reads it."""

    def __init__(self, connected=(), sharing=()):
        self.connected, self.sharing = set(connected), set(sharing)

    def is_connected(self, mid):
        return mid in self.connected

    def shares_folders(self, mid):
        return mid in self.sharing


async def _start_beside(agents, monkeypatch, key_target: str, machines: _Machines) -> Path:
    """A home start while a session bound to the store itself, on
    ``key_target``, lives: the store, its old ``config.toml`` there or not."""
    from core.remote import satellite_connection
    from services.engines import pool_bindings
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: machines)
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    (store / "config.toml").write_text("an earlier version's")
    with pool_bindings._session_maps_lock:
        pool_bindings._session_scope_keys["s-old"] = f"{key_target}:{store}"
    try:
        config = _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID))
        await get_layer_by_path("codex-cli").prepare_spawn(_SID, config)
    finally:
        with pool_bindings._session_maps_lock:
            pool_bindings._session_scope_keys.pop("s-old", None)
    return store


@pytest.mark.asyncio
async def test_a_paired_computers_session_keeps_nothing_in_the_platforms_store(roots, monkeypatch):
    """A paired computer's session reads its own disk: the platform's store
    is cleared while it runs. A session of the platform's place, or of a
    compute machine beside it, keeps the files."""
    agents, _mcps = roots
    store = await _start_beside(agents, monkeypatch, "pc-1", _Machines(connected={"pc-1"}))
    assert not (store / "config.toml").exists()
    store = await _start_beside(agents, monkeypatch, "c-1", _Machines(connected={"c-1"}, sharing={"c-1"}))
    assert (store / "config.toml").is_file()
    store = await _start_beside(agents, monkeypatch, "local", _Machines())
    assert (store / "config.toml").is_file()


@pytest.mark.asyncio
async def test_after_a_restart_the_store_waits_for_each_compute_machines_report(roots, monkeypatch):
    """Until a compute machine beside the platform has reported the sessions
    it kept, one of them may run from the store: its files stay. A machine
    with its own copy reads its own disk, so it is not waited for."""
    from core.remote import satellite_link
    from services.engines import pool_bindings
    from storage import remote_store
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    monkeypatch.setattr(pool_bindings, "within_boot_grace", lambda: True)
    monkeypatch.setattr(remote_store, "list_compute_machines", lambda: [
        {"id": "compute-beside", "pairing_scope": "compute", "compute_address": ""},
        {"id": "compute-away", "pairing_scope": "compute", "compute_address": "10.0.0.9"}])
    reported: set[str] = set()
    monkeypatch.setattr(satellite_link, "reported_since_boot", lambda mid: mid in reported)
    layer = get_layer_by_path("codex-cli")
    (store / "config.toml").write_text("an earlier version's")
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert (store / "config.toml").is_file()
    reported.add("compute-beside")
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert not (store / "config.toml").exists()


@pytest.mark.asyncio
async def test_after_a_restart_a_machine_list_that_cannot_be_read_keeps_the_store(roots, monkeypatch):
    """Inside the grace the platform cannot tell which compute machines owe
    their report: every one counts as owing, so the store's files stay."""
    import psycopg

    from services.engines import pool_bindings
    from storage import remote_store
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    monkeypatch.setattr(pool_bindings, "within_boot_grace", lambda: True)

    def _down():
        raise psycopg.OperationalError("the database is away")
    monkeypatch.setattr(remote_store, "list_compute_machines", _down)
    layer = get_layer_by_path("codex-cli")
    (store / "config.toml").write_text("a kept session's")
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert (store / "config.toml").read_text() == "a kept session's"
    # Past the grace nothing is waited for, readable list or not.
    monkeypatch.setattr(pool_bindings, "within_boot_grace", lambda: False)
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert not (store / "config.toml").exists()


@pytest.mark.asyncio
async def test_a_kept_session_whose_adoption_has_not_bound_yet_still_holds_the_store(roots, monkeypatch):
    """The machine reported, and a kept terminal's adoption is still on its
    way to its binding: the session is in no map yet, so the store's files
    it runs from stay until it is."""
    from core.remote import satellite_link
    from services.engines import pool_bindings
    from storage import remote_store
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    monkeypatch.setattr(pool_bindings, "within_boot_grace", lambda: True)
    monkeypatch.setattr(remote_store, "list_compute_machines", lambda: [
        {"id": "compute-beside", "pairing_scope": "compute", "compute_address": ""}])
    monkeypatch.setattr(satellite_link, "_reported", {"compute-beside"})
    monkeypatch.setattr(satellite_link, "_adopting", {})
    satellite_link.adoption_begins("compute-beside", "s-kept")
    layer = get_layer_by_path("codex-cli")
    (store / "config.toml").write_text("the kept session's")
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert (store / "config.toml").read_text() == "the kept session's"
    satellite_link.adoption_ends("compute-beside", "s-kept")
    await layer.prepare_spawn(_SID, _config(agents, engine=".co" "dex", session_home=str(store / "own" / _SID)))
    assert not (store / "config.toml").exists()


@pytest.mark.asyncio
async def test_a_codex_home_session_with_no_chatgpt_sign_in_drops_only_its_own_file(roots):
    """A session with no sign-in file (a local model) removes one left in
    its own home, never another live session's."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".codex"
    sibling = store / "own" / _SIBLING
    sibling.mkdir(parents=True)
    (sibling / "auth.json").write_text('{"tokens": {"access_token": "theirs"}}')
    mine = store / "own" / _SID
    mine.mkdir(parents=True)
    (mine / "auth.json").write_text('{"tokens": {"access_token": "stale"}}')
    config = _config(agents, engine=".codex", session_home=str(mine))
    plan = await get_layer_by_path("codex-cli").prepare_spawn(_SID, config)
    assert plan.credential_file is False
    assert not (mine / "auth.json").exists()
    assert json.loads((sibling / "auth.json").read_text())["tokens"]["access_token"] == "theirs"


def test_a_local_models_catalog_is_the_sessions_own(tmp_path):
    """The catalog a local model's session reads is written in its home and
    named by the home's path: another session's start never removes it."""
    from core.layers.codex.spawn import _CodexSpawn
    home = tmp_path / "home"
    home.mkdir()
    _CodexSpawn._write_config_toml(
        home, "text", "", local_endpoint="http://127.0.0.1:11434/v1", local_provider="ollama",
        local_catalog_json='{"models": []}', sandbox_codex_dir="/users/alice/.codex",
        sandbox_home_dir=f"/users/alice/.codex/own/{_SID}")
    assert (home / "models.json").read_text() == '{"models": []}'
    assert f'"/users/alice/.codex/own/{_SID}/models.json"' in (home / "config.toml").read_text()


def _home_behind_a_link(agents: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A session's home whose own name is a link to a folder outside the
    agent, which holds another session's catalog and sign-in."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("models.json", "auth.json"):
        (outside / name).write_text("another folder's")
    own = agents / "personal-assistant" / "users" / "alice" / ".co" "dex" / "own"
    own.mkdir(parents=True)
    (own / _SID).symlink_to(outside, target_is_directory=True)
    return own / _SID, outside


def test_a_start_removes_no_catalog_through_a_link_at_its_folder(roots, tmp_path):
    """The catalog a session no longer needs is removed through a handle on
    its own folder beneath the agent's real folder: a link there refuses the
    start, and what it leads to keeps its file."""
    from core.layers.codex.spawn import _CodexSpawn
    agents, _mcps = roots
    home, outside = _home_behind_a_link(agents, tmp_path)
    with pytest.raises(OSError):
        _CodexSpawn._write_config_toml(home, "text", "", sandbox_codex_dir="/users/alice/.co" "dex")
    assert (outside / "models.json").read_text() == "another folder's"


def test_a_start_drops_no_sign_in_through_a_link_at_its_folder(roots, tmp_path):
    """The same for the sign-in a session on a key leaves behind."""
    from core.layers.codex.spawn import _CodexSpawn
    agents, _mcps = roots
    home, outside = _home_behind_a_link(agents, tmp_path)
    _CodexSpawn._drop_stale_auth_json(home)
    assert (outside / "auth.json").read_text() == "another folder's"
    real = agents / "personal-assistant" / "users" / "alice" / ".co" "dex" / "own" / _SIBLING
    real.mkdir()
    (real / "auth.json").write_text("stale")
    (real / "models.json").symlink_to(outside / "models.json")
    _CodexSpawn._drop_stale_auth_json(real)
    assert not (real / "auth.json").exists()
    from core.sandbox import session_config_dir
    session_config_dir.unlink_in(real, "models.json")
    assert not (real / "models.json").is_symlink() and (outside / "models.json").is_file()
    session_config_dir.unlink_in(real, "models.json")
    session_config_dir.unlink_in(real / "nowhere", "models.json")


def _compute_host(desc, agents: Path) -> render.RenderHost:
    """The host a compute machine renders on: its satellite serves the SSH
    agent sockets from a folder of its own and opens a session's socket
    before the render (``satellite/sessions/sandbox_host.py``), a plain
    file standing in for it here."""
    empty = agents.parent / "empty"
    empty.mkdir(exist_ok=True)
    sockets = agents.parent / "ssh-agent"
    sockets.mkdir(exist_ok=True)
    if desc.ssh_agent:
        (sockets / desc.ssh_agent).touch()
    return render.RenderHost(agents_root=str(agents), mcps_root=str(agents.parent / "mcps"),
                             empty_dir=str(empty), launcher="/bin/true", uid=os.getuid(), gid=os.getgid(),
                             ssh_agent_dir=str(sockets))


def _runs_under_bwrap(desc, agents: Path) -> None:
    """The description as a compute machine renders it starts under bwrap."""
    import shutil
    import subprocess
    if shutil.which("bwrap") is None:
        return
    host = _compute_host(desc, agents)
    cmd = render.render(desc, host, ["/bin/true"])
    out = subprocess.run(cmd[cmd.index("bwrap"):], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


@pytest.mark.asyncio
async def test_a_compute_session_without_a_home_still_masks_the_others(roots):
    """A session that keeps its scope's folder (Codex, a judge) still sees no
    other session's home there."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".claude"
    (store / "own" / _SIBLING).mkdir(parents=True)
    plan = await get_layer_by_path("claude-code-cli").prepare_spawn("s-2", _config(agents))
    desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
    assert desc.home is None
    assert {m.sandbox for m in desc.home_masks if m.sandbox.startswith("/users/alice")} >= {
        "/users/alice/.claude/own"}


@pytest.mark.asyncio
async def test_the_platforms_own_start_makes_no_home_and_still_masks_the_others(roots):
    """The platform's own sandbox gets no home (the decision gives it none),
    and like every sandbox over a store it sees no session's home there."""
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".claude"
    (store / "own" / _SIBLING).mkdir(parents=True)
    local = _config(agents, security_context=_ctx(placement=placement.PlacementCapabilities()))
    ready = await get_layer_by_path("claude-code-cli")._prepare("s-3", local)
    desc = ready.sandbox_builder.describe()
    assert desc.home is None and "home" not in desc.to_dict()
    assert "/users/alice/.claude/own" in {m.sandbox for m in desc.home_masks}
    assert [p.name for p in (store / "own").iterdir()] == [_SIBLING]


@pytest.mark.asyncio
async def test_the_compute_payload_asks_a_machine_for_the_homes_links(layer, tmp_path):
    """A compute machine with its own copy gets the home's files and makes
    its links by the engine's word in the start payload; with no home the
    payload carries none."""
    spawn = _plan(tmp_path)
    home = str(tmp_path / ".claude" / "own" / _SID)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        with_home = (await layer._build_start_payload(
            _SID, _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir, session_home=home),
            "claude-code-cli", spawn=spawn)).payload
        without = (await layer._build_start_payload(
            _SID, _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir), "claude-code-cli",
            spawn=spawn)).payload
    spec = get_layer_by_path("claude-code-cli").capabilities.auth.session_home
    assert with_home["session_home"] == spec.to_dict()
    assert "session_home" not in without


def test_an_own_copy_machine_gets_its_homes_files_and_no_one_elses(tmp_path):
    from core.remote import compute_files
    agent = tmp_path / "agents" / "pa"
    store = agent / "workspace" / ".claude"
    for sid in (_SID, _SIBLING):
        (store / "own" / sid).mkdir(parents=True)
        (store / "own" / sid / ".credentials.json").write_text(sid)
    (store / "own" / _SID / "projects").symlink_to("../../projects")
    # A link in the home to a sibling's sign-in is never shipped.
    (store / "own" / _SID / "borrowed.json").symlink_to(f"../{_SIBLING}/{_SIGN_IN}")
    files = compute_files.collect(agent, str(store), "", _SID, None)
    assert not any(rel.endswith("/borrowed.json") for rel in files)
    assert f"workspace/.claude/own/{_SID}/.credentials.json" in files
    assert not any(_SIBLING in rel for rel in files)
    assert not any(rel.endswith("/projects") for rel in files)


@pytest.mark.asyncio
async def test_a_compute_sessions_home_goes_at_its_close(layer, tmp_path, monkeypatch):
    """The platform made the home in the shared folder: its close removes it
    with its sign-in, and the conversation stays in the scope's folder."""
    spawn = _plan(tmp_path)
    store, home = _home_on_disk(spawn.config_dir)
    config = _config(tmp_path, sandbox_host_claude_dir=spawn.config_dir, session_home=str(home))
    claude = get_layer_by_path("claude-code-cli")

    async def _prepared(_sid, _cfg):
        return spawn
    monkeypatch.setattr(claude, "prepare_spawn", _prepared)

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(layer, "_provision_gateway", _none)
    monkeypatch.setattr("storage.remote_store.set_chat_compute_machine", lambda c, m: None)
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        await layer._start_session_impl(_SID, config, [])
    _homed_close_checks(layer, store, home, monkeypatch)
    await layer.close_session(_SID)
    _homed_close_after(store, home)


@pytest.mark.parametrize("held, stays", [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_a_failed_start_keeps_no_home_of_a_session_that_never_ran(layer, roots, monkeypatch, held, stays):
    """The machine's answer, the payload or the frame failed after the
    platform made the home: it goes now, with its sign-in. A failed re-warm
    of a session the layer still holds leaves the home it runs from."""
    from core.remote import remote_session_start
    agents, _mcps = roots
    store, home = _home_on_disk(str(agents / "personal-assistant" / "users" / "alice" / ".claude"))
    config = _config(agents, session_home=str(home))

    async def _fails(_sid, _config, _registered):
        raise RuntimeError("the machine did not answer")
    monkeypatch.setattr(layer, "_start_session_impl", _fails)
    monkeypatch.setattr(remote_session_start, "session_is_held", lambda sid: held)
    with pytest.raises(RuntimeError):
        await layer.start_session(_SID, config)
    assert home.exists() is stays
    assert (store / "projects" / "conv.jsonl").read_text() == "kept"


def _homed_close_checks(layer, store: Path, home: Path, monkeypatch) -> None:
    """The session's binding names its home (as the start's bind, or the
    adoption after a restart, records it) and its renewal target is the home;
    the drop is watched for what it sees, the releases for their order."""
    from core.sandbox import session_home
    from services.engines import pool_bindings, token_fanout
    # The store's own folder stands for the agent's: a home is removed only in one.
    monkeypatch.setattr(app_config, "AGENTS_DIR", store.parent.parent)
    assert app_config.is_safe_agent_name(store.parent.name)
    pool_bindings._session_scope_keys[_SID] = pool_bindings.scope_key_for("c-1", str(store), str(home))
    token_fanout.register_session_target(_SID, token_fanout.CredentialFileTarget(
        layer="claude-code-cli", host_dir=str(home)))
    real_drop = session_home.drop
    _CLOSE_SEEN.clear()

    def _drop(*a, **k):
        _CLOSE_SEEN.append(("drop", token_fanout.session_target(_SID)))
        return real_drop(*a, **k)
    monkeypatch.setattr(session_home, "drop", _drop)
    monkeypatch.setattr("services.engines.subscription_pool.release_subscription",
                        lambda sid: _CLOSE_SEEN.append(("seat", sid)) or pool_bindings._session_scope_keys.pop(sid, None))
    monkeypatch.setattr("core.concurrency.release_chat_slot", lambda sid: _CLOSE_SEEN.append(("slot", sid)))


_CLOSE_SEEN: list = []


def _homed_close_after(store: Path, home: Path) -> None:
    assert _SID not in _names_in(str(store / "own"))
    assert _text_of(str(store), "projects", "conv.jsonl") == "kept"
    # The renewal target goes before the home, the seat and the slot after.
    assert _CLOSE_SEEN == [("drop", None), ("seat", _SID), ("slot", _SID)]


@pytest.mark.asyncio
async def test_an_adopted_sessions_home_goes_at_its_close(layer, tmp_path, monkeypatch):
    """A session the platform took back after a restart has a record built
    from the machine's report, with nothing about a home: its binding,
    restored first, names the home, and the close removes it."""
    from core.remote.remote_session_info import RemoteSessionInfo
    spawn = _plan(tmp_path)
    store, home = _home_on_disk(spawn.config_dir)
    layer._sessions[_SID] = RemoteSessionInfo(session_id=_SID, machine_id="c-1", agent_name="pa",
                                              execution_path="claude-code-cli", event_queue=asyncio.Queue())
    _homed_close_checks(layer, store, home, monkeypatch)
    await layer.close_session(_SID)
    _homed_close_after(store, home)


@pytest.mark.asyncio
async def test_a_close_releases_the_seat_and_the_slot_whatever_fails_before(layer, tmp_path, monkeypatch):
    """A tree far deeper than the interpreter's recursion limit in the home,
    and an engine whose own close fails: the seat and the slot go back."""
    from core.remote.remote_session_info import RemoteSessionInfo
    spawn = _plan(tmp_path)
    store, home = _home_on_disk(spawn.config_dir)
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(1200):
        os.mkdir("d", dir_fd=fd)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nxt
    os.close(fd)
    layer._sessions[_SID] = RemoteSessionInfo(session_id=_SID, machine_id="c-1", agent_name="pa",
                                              execution_path="claude-code-cli", event_queue=asyncio.Queue())
    _homed_close_checks(layer, store, home, monkeypatch)
    await layer.close_session(_SID)
    _homed_close_after(store, home)

    layer._sessions[_SID] = RemoteSessionInfo(session_id=_SID, machine_id="c-1", agent_name="pa",
                                              execution_path="claude-code-cli", event_queue=asyncio.Queue())
    _CLOSE_SEEN.clear()

    async def _fails(_info):
        raise RuntimeError("the engine's own close failed")
    monkeypatch.setattr(type(layer._adapter(layer._sessions[_SID])), "close_state",
                        lambda self, info: _fails(info))
    with pytest.raises(RuntimeError):
        await layer.close_session(_SID)
    assert ("seat", _SID) in _CLOSE_SEEN and ("slot", _SID) in _CLOSE_SEEN


def _home_on_disk(config_dir: str) -> tuple[Path, Path]:
    """A store with a session's home holding a sign-in, and a conversation."""
    from core.sandbox import session_home
    store = Path(config_dir)
    store.mkdir(parents=True, exist_ok=True)
    home = session_home.prepare(store, _SID, ("projects",))
    (home / ".credentials.json").write_text("token")
    (store / "projects" / "conv.jsonl").write_text("kept")
    return store, home


_SIGN_IN = ".credentials" + ".json"


@pytest.mark.asyncio
@pytest.mark.parametrize("role, username, admin_agent", [
    ("manager", "alice", False), ("admin", "alice", False), ("admin", "alice", True),
    ("editor", "alice", False), ("viewer", "alice", False),
    ("manager", "", False), ("editor", "", False),
])
async def test_every_sign_in_a_session_sees_is_its_own(roots, role, username, admin_agent):
    """The real description of a session with a home, for each role and
    scope, run under bwrap: every sign-in file found on every mount of the
    agent's folders is the session's own (its home's), or empty. A
    sibling's home, another person's store and the agent's store each hold
    one; none is readable."""
    import shutil
    import subprocess
    if shutil.which("bwrap") is None:
        pytest.skip("requires bwrap on PATH")
    agents, _mcps = roots
    pa = agents / "personal-assistant"
    stores = {"agent": pa / "workspace" / ".claude", "alice": pa / "users" / "alice" / ".claude",
              "bob": pa / "users" / "bob" / ".claude"}
    for who, store in stores.items():
        (store / "own" / _SIBLING).mkdir(parents=True, exist_ok=True)
        (store / "own" / _SIBLING / _SIGN_IN).write_text(f"SIBLING-{who}")
        (store / _SIGN_IN).write_text(f"STORE-{who}")
    store = stores["alice" if username else "agent"]
    (store / "settings.json").write_text('{"hooks": {}}\n')
    ctx = SecurityContext(role=role, username=username, agent="personal-assistant",
                          is_admin_agent=admin_agent, placement=COMPUTE)
    config = _config(agents, extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CREDS)}, security_context=ctx,
                     sandbox_host_claude_dir=str(store), session_home=str(store / "own" / _SID))
    plan = await get_layer_by_path("claude-code-cli").prepare_spawn(_SID, config)
    desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
    assert bool(desc.ssh_agent) is (role != "viewer")
    host = _compute_host(desc, agents)
    script = ("find /workspace /users /knowledge \\( -name .credentials.json -o -name auth.json \\) "
              "2>/dev/null | while read f; do printf '%s=%s\\n' \"$f\" \"$(cat \"$f\" 2>/dev/null)\"; done")
    cmd = render.render(desc, host, ["/bin/sh", "-c", script])
    out = await asyncio.to_thread(subprocess.run, cmd[cmd.index("bwrap"):], capture_output=True, text=True,
                                  timeout=30)
    assert out.returncode == 0, out.stderr
    seen = dict(line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line)
    readable = {path: text for path, text in seen.items() if text}
    assert readable, out.stdout
    for path, text in readable.items():
        assert path.endswith(f"/own/{_SID}/{_SIGN_IN}") and json.loads(text) == _CREDS, (path, text)


def test_the_stores_empty_sign_in_stays_at_a_home_start(roots):
    """The store's empty sign-in is the mount point a live sandbox holds its
    mask over (``sandbox_render.home_args``): a home start that removed it
    would detach that mask in every live sandbox. A sign-in with content (an
    earlier version's) goes."""
    from core.layers.codex.config_dir import drop_store_leftovers
    agents, _mcps = roots
    store = agents / "personal-assistant" / "users" / "alice" / ".co" "dex"
    (store / "auth.json").write_bytes(b"")
    inode = (store / "auth.json").stat().st_ino
    drop_store_leftovers(store)
    assert (store / "auth.json").stat().st_ino == inode
    (store / "auth.json").write_text("an earlier version's sign-in")
    drop_store_leftovers(store)
    assert not (store / "auth.json").exists()
