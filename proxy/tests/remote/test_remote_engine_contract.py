"""The remote path against the engine contract (engine-contract lane, phase 4).

The remote layer is a PLACEMENT: a session on a satellite still runs one of
the registered engines, and every per-session question is answered by that
engine's descriptor or its remote adapter — never by an engine-id compare in
``core/remote``. These tests pin the seams shared code relies on.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.remote import remote_turn
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
from core.remote.satellite_connection import SatelliteConnection, SatelliteConnectionManager
from core.session.session_manager import get_layer_by_path
from core import placement

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _layer(cm=None) -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = cm if cm is not None else MagicMock()
    layer._sessions = {}
    return layer


def _info(sid: str, path: str, machine: str = "m-1") -> RemoteSessionInfo:
    return RemoteSessionInfo(
        session_id=sid, machine_id=machine, agent_name="agent-1",
        execution_path=path, event_queue=asyncio.Queue(),
    )


# --- capabilities_for: the engine's descriptor, per session ------------------

class TestCapabilitiesFor:
    def test_a_headless_session_answers_its_engines_descriptor(self):
        layer = _layer()
        layer._sessions["s-codex"] = _info("s-codex", "codex-cli")
        layer._sessions["s-claude"] = _info("s-claude", "claude-code-cli")
        assert layer.capabilities_for("s-codex") is get_layer_by_path("codex-cli").capabilities
        assert layer.capabilities_for("s-claude") is get_layer_by_path("claude-code-cli").capabilities
        # What the placement's own flags would have hidden (the live defect:
        # a remote chat's mode change never reached the CLI).
        assert "set_permission_mode" in layer.capabilities_for("s-codex").control_commands
        assert layer.capabilities_for("s-codex").permission_modes

    def test_a_remote_interactive_session_answers_from_the_interactive_registry(self):
        from core.session import interactive_session
        layer = _layer()
        fake = MagicMock()
        fake.target = "m-1"
        fake.execution_path = "codex-cli"
        with patch.object(interactive_session, "get", return_value=fake):
            assert layer.capabilities_for("s-tui").name == "codex-cli"
        local = MagicMock()
        local.target = "local"
        local.execution_path = "codex-cli"
        with patch.object(interactive_session, "get", return_value=local):
            assert layer.capabilities_for("s-local").name == "remote"

    def test_an_unknown_session_answers_the_placement(self):
        layer = _layer()
        caps = layer.capabilities_for("nope")
        assert caps.name == "remote" and caps.supports_control_commands
        assert layer.capabilities_for("").name == "remote"

    def test_a_local_layer_is_its_own_engine(self):
        for path in ("claude-code-cli", "codex-cli", "direct-llm"):
            lay = get_layer_by_path(path)
            assert lay.capabilities_for("any") is lay.capabilities


# --- session_self_wakes: the engine's runtime fact, per session ---------------

class TestSelfWakes:
    @pytest.mark.asyncio
    async def test_remote_answers_for_the_engine_it_runs(self):
        layer = _layer()
        layer._sessions["s-claude"] = _info("s-claude", "claude-code-cli")
        layer._sessions["s-codex"] = _info("s-codex", "codex-cli")
        assert await layer.session_self_wakes("s-claude") is True
        assert await layer.session_self_wakes("s-codex") is False
        assert await layer.session_self_wakes("unknown") is False

    @pytest.mark.asyncio
    async def test_local_layers_read_their_flag_and_ownership(self):
        cli = get_layer_by_path("claude-code-cli")
        codex = get_layer_by_path("codex-cli")
        with patch.object(type(cli), "owns_session", return_value=True):
            assert await cli.session_self_wakes("s") is True
        with patch.object(type(cli), "owns_session", return_value=False):
            assert await cli.session_self_wakes("s") is False
        with patch.object(type(codex), "owns_session", return_value=True):
            assert await codex.session_self_wakes("s") is False


# --- the remote interactive spawn (the phase-2 NameError) ---------------------

class TestRemoteInteractiveSpawn:
    @pytest.mark.asyncio
    async def test_reaches_register_remote_with_the_engines_first_prompt_rule(self):
        from core.session import interactive_session
        layer = _layer()
        config = AgentConfig(
            agent_name="agent-1", execution_target="m-1", interactive=True,
            chat_id="chat-1", interactive_first_prompt="hello there",
            security_context=None, subscription_id="",
        )
        reg = AsyncMock()
        with patch.object(interactive_session, "register_remote", reg), \
             patch("core.session.session_state._record_session_use"), \
             patch("core.session.session_state.set_session_mode"):
            await layer._start_interactive_remote(
                "s-1", config, "codex-cli", {"k": "v"}, "m-1",
                first_prompt_via_argv=True, credential_file_delivered=False,
            )
            kw = reg.await_args.kwargs
            assert kw["execution_path"] == "codex-cli"
            assert kw["prompt_in_argv"] is True
            assert kw["config_payload"] == {"k": "v"}
            # Claude's TUI takes no launch prompt — the rule comes from the caller's descriptor read.
            await layer._start_interactive_remote(
                "s-2", config, "claude-code-cli", {}, "m-1",
                first_prompt_via_argv=False, credential_file_delivered=False,
            )
            assert reg.await_args.kwargs["prompt_in_argv"] is False


# --- the connection manager reads the registry, not engine ids ---------------

class TestConnectionManagerEngineReads:
    def _cm(self) -> SatelliteConnectionManager:
        cm = SatelliteConnectionManager()
        cm._connections["m-1"] = SatelliteConnection(
            machine_id="m-1", ws=None, satellite_version="0.5.123",
        )
        return cm

    def test_session_queue_depth_is_the_engines(self):
        cm = self._cm()
        assert cm.create_session_queue("m-1", "s-codex", "codex-cli").maxsize == 4096
        assert cm.create_session_queue("m-1", "s-claude", "claude-code-cli").maxsize == 1000
        # An explicit size (the adopt replay burst) still wins.
        assert cm.create_session_queue("m-1", "s-x", "claude-code-cli", maxsize=4096).maxsize == 4096

    @pytest.mark.asyncio
    async def test_the_resume_handle_frame_becomes_the_generic_marker(self):
        cm = self._cm()
        q = cm.create_session_queue("m-1", "s-1", "codex-cli")
        # The satellite's frame keeps Codex's wire spelling; the queue marker
        # is the one every engine's remote adapter understands.
        await cm.handle_message("m-1", {
            "type": "codex_thread_id", "session_id": "s-1", "thread_id": "thread-9",
        })
        assert q.get_nowait() == {"type": "_resume_handle", "handle": "thread-9"}


# --- the resume handle: recorded at the marker, persisted on the first turn ---
#
# The satellite's frame lands right behind session_started, before the
# warmup binds the chat to the session — a chat lookup at that instant finds
# nothing (the released code did that, and every remote Codex chat since
# 2026-09-17 lost its thread id; found on T1 in phase 4). So the marker only
# records the handle on the session, and the adapter yields the local
# layer's METADATA event on the first turn — the pump writes the column.

class TestResumeHandle:
    def test_the_marker_records_on_the_session_and_touches_no_table(self):
        info = _info("s-1", "codex-cli")
        with patch("storage.database.get_chat_by_session") as lookup, \
             patch("storage.database.update_chat") as upd:
            remote_turn.record_resume_handle(info, "thread-9")
            remote_turn.record_resume_handle(info, "thread-9")
            remote_turn.record_resume_handle(info, "")
        assert info.resume_handle == "thread-9"
        lookup.assert_not_called()
        upd.assert_not_called()

    @staticmethod
    def _codex_turn(info, handle: str):
        from core.layers.codex.remote import CodexRemoteState
        from core.layers.codex.translator import CodexEventTranslator
        info.resume_handle = handle
        info.current_send_command_id = "cmd-1"
        if info.engine_state is None:
            info.engine_state = CodexRemoteState(
                translator=CodexEventTranslator(model="m", supervised_bg=True))
        info.engine_state.default_consumer = asyncio.Queue()
        info.engine_state.default_consumer.put_nowait(
            {"type": "_turn_ended", "command_id": "cmd-1"})
        return get_layer_by_path("codex-cli").remote_adapter().stream_turn(info, MagicMock())

    @pytest.mark.asyncio
    async def test_the_first_turn_yields_the_frozen_metadata_key_once(self):
        from core.events.common_events import DONE, METADATA
        info = _info("s-1", "codex-cli")
        first = [ev async for ev in self._codex_turn(info, "thread-9")]
        assert [ev.type for ev in first] == [METADATA, DONE]
        assert first[0].data == {"codex_thread_id": "thread-9"}   # what the pump persists
        second = [ev async for ev in self._codex_turn(info, "thread-9")]
        assert [ev.type for ev in second] == [DONE]

    @pytest.mark.asyncio
    async def test_a_turn_before_the_frame_leaves_the_emit_for_the_next_one(self):
        from core.events.common_events import DONE, METADATA
        info = _info("s-1", "codex-cli")
        early = [ev async for ev in self._codex_turn(info, "")]
        assert [ev.type for ev in early] == [DONE]
        later = [ev async for ev in self._codex_turn(info, "thread-9")]
        assert [ev.type for ev in later] == [METADATA, DONE]


# --- the old-satellite contract: the start payload, byte for byte -------------
#
# The payloads below were snapshotted from the builder BEFORE the engine
# adapters existed (phase 4, tests/remote/fixtures/remote_start_payloads.json),
# for nine configurations across both engines. A released satellite reads
# exactly these keys, so the adapters must reproduce them dict-for-dict —
# with the session token, the hook scripts, the machine row and the MCP
# registry pinned (the only inputs that vary; a registry an earlier test on
# the worker loaded adds a manifest's tool_arg_paths to the display entry) and
# no secret bundles (the MCP rewriters would mint per-call cap tokens).

_CLAUDE_BLOB = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                                  "scopes": [], "subscriptionType": "", "rateLimitTier": ""}}
_CODEX_AUTH = {"auth_mode": "chatgpt", "tokens": {"id_token": "ID", "access_token": "NEW",
                                                   "refresh_token": "", "account_id": "A"}}
_MCP_JSON = {"mcpServers": {
    "display": {"command": "/opt/otodock/mcps/custom/display-mcp/venv/bin/python",
                "args": ["/opt/otodock/mcps/custom/display-mcp/server.py"],
                "env": {"PROXY_URL": "http://10.0.0.1:8400"}},
    "file-tools": {"type": "http", "url": "http://localhost:8901/mcp/",
                   "headers": {"Authorization": "Bearer OTO_SESSION_JWT"}},
}}
_MCP_TOML = (
    '[mcp_servers.display]\ncommand = "/opt/otodock/mcps/custom/display-mcp/venv/bin/python"\n'
    'args = ["/opt/otodock/mcps/custom/display-mcp/server.py"]\n'
    'env = { PROXY_URL = "http://10.0.0.1:8400" }\n\n'
    '[mcp_servers.file-tools]\nurl = "http://localhost:8901/mcp/"\n'
)


def _snapshot_config(tmp_path: Path, **over) -> AgentConfig:
    base = dict(agent_name="test-agent", execution_target="machine-1",
                system_prompt="You are a test agent.", model="claude-sonnet-5",
                effort="high", permission_mode="default", client_type="dashboard",
                extra_env={}, multi_value_envs={"OTO_ALLOWED_ROOTS": ":"},
                credential_env={"OTO_ALLOWED_ROOTS": "/users/alice/workspace:/workspace"},
                security_context=SimpleNamespace(mount_username="alice", username="alice",
                                                 role="manager", read_only=False, placement=placement.PlacementCapabilities(allow_full_fs=False)))
    base.update(over)
    return AgentConfig(**base)


def _snapshot_cases(tmp_path: Path) -> dict[str, tuple[str, AgentConfig]]:
    mcp_json = tmp_path / "mcp-config.json"
    mcp_json.write_text(json.dumps(_MCP_JSON))
    mcp_toml = tmp_path / "config.toml"
    mcp_toml.write_text(_MCP_TOML)
    judge_ctx = SimpleNamespace(mount_username="", username="", role="admin",
                                read_only=True, placement=placement.PlacementCapabilities(allow_full_fs=True))
    c = lambda **kw: _snapshot_config(tmp_path, **kw)  # noqa: E731
    return {
        "claude_oauth_dashboard": ("claude-code-cli", c(
            extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(_CLAUDE_BLOB)},
            mcp_config_path=str(mcp_json))),
        "claude_api_key_ultra_effort": ("claude-code-cli", c(
            extra_env={"ANTHROPIC_API_KEY": "sk-test"}, effort="ultra")),
        "claude_resume_judge": ("claude-code-cli", c(
            resume=True, permission_mode="judge", security_context=judge_ctx)),
        "claude_interactive_first_prompt": ("claude-code-cli", c(
            interactive=True, interactive_first_prompt="hello", use_native_permissions=True,
            work_cwd="/home/u/proj", term="xterm-kitty")),
        "codex_oauth_dashboard": ("codex-cli", c(
            model="gpt-5.6-sol", effort="ultra",
            extra_env={"_CODEX_AUTH_JSON": json.dumps(_CODEX_AUTH)},
            mcp_config_path=str(mcp_toml))),
        "codex_task_hooks_floor": ("codex-cli", c(
            model="gpt-5.6-sol", effort="high", client_type="task",
            permission_mode="acceptEdits", extra_env={"CODEX_API_KEY": "sk-openai"})),
        "codex_local_endpoint_ollama": ("codex-cli", c(
            model="qwen3.6-35b-a3b", effort="medium",
            extra_env={"_CODEX_ENDPOINT_URL": "http://127.0.0.1:11434/v1",
                       "_CODEX_LOCAL_API_KEY": "k", "_CODEX_ENDPOINT_PROVIDER": "ollama"})),
        "codex_interactive_resume_thread": ("codex-cli", c(
            model="gpt-5.6-sol", effort="high", interactive=True, resume=True,
            resume_handle="thread-abc", permission_mode="plan")),
        "codex_judge_full_fs": ("codex-cli", c(
            model="gpt-5.6-sol", effort="high", permission_mode="judge",
            security_context=judge_ctx)),
    }


def _snapshot_layer() -> RemoteExecutionLayer:
    lay = RemoteExecutionLayer(MagicMock())
    cm = lay._cm
    cm.satellite_supports_local_model_provider.return_value = True
    cm.satellite_supports_local_model_catalog.return_value = True
    cm.satellite_supports_codex_hooks_floor.return_value = True
    cm.satellite_supports_hook_parity.return_value = True
    cm.satellite_supports_checks.return_value = True
    cm.satellite_version.return_value = "0.5.123"
    cm.satellite_name.return_value = "drill-sat"
    return lay


def _comparable(payload: dict) -> dict:
    # ``disallowed_tools`` is built from a set — order is per-process.
    out = dict(payload)
    if "disallowed_tools" in out:
        out["disallowed_tools"] = sorted(out["disallowed_tools"])
    return out


@pytest.mark.asyncio
async def test_start_payloads_match_the_pre_adapter_snapshot(tmp_path):
    from services.mcp import mcp_registry
    expected = json.loads((_FIXTURES / "remote_start_payloads.json").read_text())
    cases = _snapshot_cases(tmp_path)
    assert set(cases) == set(expected)
    machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
               "pairing_scope": "admin"}
    with patch.dict(mcp_registry._manifests, clear=True), \
         patch("storage.remote_store.get_remote_machine", return_value=machine), \
         patch("auth.session_token.create_session_token", return_value="JWT"), \
         patch("storage.database.get_user_sub_by_username", return_value="sub-alice"), \
         patch("core.remote.remote_start_payload._load_hook_scripts",
               return_value={"permission_gate.py": "#gate"}):
        for name, (path, cfg) in cases.items():
            plan = await _snapshot_layer()._build_start_payload("sess-1", cfg, path)
            assert _comparable(plan.payload) == _comparable(expected[name]), name
            # The plan's own answers.
            file_keys = {"credentials_json", "auth_json"}
            assert plan.credential_file_delivered == bool(file_keys & set(plan.payload)), name
            assert plan.start_timeout_s == (
                180.0 if "local_model_provider" in plan.payload else 60.0), name


@pytest.mark.asyncio
async def test_no_engine_ships_another_engines_private_variable(tmp_path):
    # The union strip: a Claude session's env carrying Codex's private
    # carriers (impossible today, cheap to guarantee) never ships them.
    cfg = _snapshot_config(tmp_path, extra_env={
        "_CODEX_ENDPOINT_URL": "http://x/v1", "_CODEX_LOCAL_API_KEY": "k",
        "_CODEX_ENDPOINT_PROVIDER": "ollama", "_CODEX_AUTH_JSON": "{}",
        "_CLAUDE_CREDS_BLOB": json.dumps(_CLAUDE_BLOB),
    })
    machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
               "pairing_scope": "admin"}
    with patch("storage.remote_store.get_remote_machine", return_value=machine), \
         patch("core.remote.remote_start_payload._load_hook_scripts", return_value={}):
        plan = await _snapshot_layer()._build_start_payload("sess-1", cfg, "claude-code-cli")
    private = set().union(*(
        lay.remote_adapter().private_env_keys
        for lay in (get_layer_by_path("claude-code-cli"), get_layer_by_path("codex-cli"))
    ))
    assert not private & set(plan.payload["env"]), private & set(plan.payload["env"])
    assert plan.credential_file_delivered is True   # its own blob became the file


# --- the frames: each engine's, sent through the adapter ----------------------

class TestControlFrames:
    def _layer_with(self, path: str):
        layer = _layer()
        layer._cm.send_fire_and_forget = AsyncMock()
        info = _info("s-1", path)
        info.allow_full_fs = True
        layer._sessions["s-1"] = info
        return layer, info

    @pytest.mark.asyncio
    async def test_claude_forwards_model_and_mode_to_the_stdin_control_channel(self):
        layer, info = self._layer_with("claude-code-cli")
        await layer.change_model("s-1", "claude-opus-5")
        await layer.change_mode("s-1", "plan")
        frames = [c.args[1] for c in layer._cm.send_fire_and_forget.call_args_list]
        assert frames == [
            {"type": "control_request", "session_id": "s-1", "subtype": "set_model",
             "kwargs": {"model": "claude-opus-5"}},
            {"type": "control_request", "session_id": "s-1", "subtype": "set_permission_mode",
             "kwargs": {"mode": "plan"}},
        ]
        assert (info.model, info.mode) == ("claude-opus-5", "plan")

    @pytest.mark.asyncio
    async def test_codex_sends_the_model_and_the_sandbox_mode(self):
        # The live defect: the remote layer sent NOTHING for a Codex model
        # change while the satellite's CodexSession has honoured set_model as
        # a per-turn override in every release.
        from core.layers.codex.helpers import permission_to_sandbox
        layer, info = self._layer_with("codex-cli")
        await layer.change_model("s-1", "gpt-5.6-sol")
        await layer.change_mode("s-1", "plan")
        frames = [c.args[1] for c in layer._cm.send_fire_and_forget.call_args_list]
        assert frames == [
            {"type": "control_request", "session_id": "s-1", "subtype": "set_model",
             "kwargs": {"model": "gpt-5.6-sol"}},
            {"type": "control_request", "session_id": "s-1", "subtype": "set_permission_mode",
             "kwargs": {"sandbox_mode": permission_to_sandbox("plan", allow_full_fs=True)}},
        ]
        assert (info.model, info.mode) == ("gpt-5.6-sol", "plan")

    @pytest.mark.asyncio
    async def test_a_codex_start_never_freezes_the_plan_mode(self, tmp_path):
        """A satellite (0.5.123+) plans iff a shipped ``permission_mode`` says
        plan, else iff the live sandbox is read-only; a mode change carries
        the sandbox alone. A chat started in plan (or default) must ship no
        mode, or Implement kept Codex planning; a judge ships its own."""
        machine = {"capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
                   "pairing_scope": "admin"}
        judge_ctx = SimpleNamespace(mount_username="", username="", role="admin", read_only=True,
                                    placement=placement.PlacementCapabilities(allow_full_fs=False))
        shipped = {}
        with patch("storage.remote_store.get_remote_machine", return_value=machine), \
             patch("auth.session_token.create_session_token", return_value="JWT"), \
             patch("storage.database.get_user_sub_by_username", return_value="sub-alice"):
            for mode in ("plan", "default", "acceptEdits", "judge"):
                over = {"security_context": judge_ctx} if mode == "judge" else {}
                cfg = _snapshot_config(tmp_path, model="gpt-6-sol", permission_mode=mode, **over)
                plan = await _snapshot_layer()._build_start_payload("sess-1", cfg, "codex-cli")
                shipped[mode] = (plan.payload["permission_mode"], plan.payload["sandbox_mode"])
        assert shipped == {"plan": ("", "read-only"), "default": ("", "workspace-write"),
                           "acceptEdits": ("", "workspace-write"), "judge": ("judge", "read-only")}

    @pytest.mark.asyncio
    async def test_a_queued_control_request_keeps_the_session_record_in_step(self):
        # The dashboard flushes queued requests through send_control_request;
        # the Codex plan card reads info.mode, so the flush must route through
        # change_mode rather than fire a bare frame.
        layer, info = self._layer_with("codex-cli")
        await layer.send_control_request("s-1", "set_permission_mode", mode="plan")
        assert info.mode == "plan"
        await layer.send_control_request("s-1", "set_model", model="gpt-5.6-terra")
        assert info.model == "gpt-5.6-terra"
        # An unknown subtype is the engine's to forward or ignore (Codex ignores).
        n = layer._cm.send_fire_and_forget.call_count
        await layer.send_control_request("s-1", "set_thinking_tokens", tokens=1)
        assert layer._cm.send_fire_and_forget.call_count == n

    def test_soft_interrupt_frames_are_the_engines(self):
        assert get_layer_by_path("claude-code-cli").remote_adapter().soft_interrupt_frame() == "interrupt_turn"
        assert get_layer_by_path("codex-cli").remote_adapter().soft_interrupt_frame() == "abort"
        assert get_layer_by_path("codex-cli").remote_adapter().owns_event_queue is True
        assert get_layer_by_path("claude-code-cli").remote_adapter().owns_event_queue is False


# --- the engines gate ----------------------------------------------------------

class TestSatelliteEngines:
    def _cm(self, caps: dict | None) -> SatelliteConnectionManager:
        cm = SatelliteConnectionManager()
        conn = SatelliteConnection(machine_id="m-1", ws=None, satellite_version="0.5.123")
        if caps is not None:
            conn.capabilities = caps
        cm._connections["m-1"] = conn
        return cm

    def test_absent_and_malformed_read_as_the_legacy_pair(self):
        pair = frozenset({"claude-code-cli", "codex-cli"})
        assert self._cm({}).satellite_engines("m-1") == pair
        assert self._cm({"engines": "codex-cli"}).satellite_engines("m-1") == pair
        assert self._cm({"engines": []}).satellite_engines("m-1") == pair
        assert self._cm({"engines": [42, ""]}).satellite_engines("m-1") == pair
        assert SatelliteConnectionManager().satellite_engines("nope") == frozenset()

    def test_a_list_is_the_set_bounded(self):
        cm = self._cm({"engines": ["codex-cli", "acme-cli", "x" * 65]})
        assert cm.satellite_engines("m-1") == {"codex-cli", "acme-cli"}

    @pytest.mark.asyncio
    async def test_start_refuses_an_engine_the_satellite_cannot_run_before_any_frame(self):
        layer = _layer()
        cm = layer._cm
        cm.is_connected.return_value = True
        cm.machine_at_capacity.return_value = False
        cm.satellite_engines.return_value = frozenset({"codex-cli"})
        cm.satellite_name.return_value = "old-sat"
        cm.satellite_version.return_value = "0.5.123"
        cm.send_command = AsyncMock()
        from auth.path_policy import SecurityContext
        config = AgentConfig(agent_name="agent-1", execution_target="m-1",
                             execution_path="claude-code-cli",
                             security_context=SecurityContext(role="manager", username="", agent="agent-1",
                                                              is_admin_agent=False, session_scope="agent"))
        with pytest.raises(RuntimeError, match="not available on old-sat"):
            await layer.start_session("s-1", config)
        cm.send_command.assert_not_awaited()


# --- the resume probe: the engine's, on the resolved engine --------------------

class TestCanResume:
    @pytest.mark.asyncio
    async def test_a_codex_chat_with_an_empty_engine_column_resolves_the_agent_engine(self):
        # Delegate worker chats never stamp chats.execution_path; the raw
        # compare sent them through Claude's JSONL probe and refused the
        # thread-id resume. The engine is resolved the way the spawn does.
        layer = _layer()
        layer._cm.send_command = AsyncMock()
        chat = {"agent": "codex-agent", "execution_path": "", "codex_thread_id": "thread-9",
                "execution_target": "m-1"}
        with patch("storage.database.get_chat_by_session", return_value=chat), \
             patch("core.session.session_manager.resolve_execution_path",
                   return_value="codex-cli") as resolve:
            assert await layer.can_resume_session("s-gone") is True
        resolve.assert_called_once_with("codex-agent", "")
        layer._cm.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_claude_chat_asks_the_machine_it_ran_on(self):
        layer = _layer()
        layer._cm.is_connected.return_value = True
        layer._cm.send_command = AsyncMock(return_value={"resumable": True})
        chat = {"agent": "pa", "execution_path": "claude-code-cli", "codex_thread_id": "",
                "execution_target": "m-1"}
        with patch("storage.database.get_chat_by_session", return_value=chat):
            assert await layer.can_resume_session("s-gone", username="alice") is True
        msg = layer._cm.send_command.await_args.args[1]
        assert msg["type"] == "check_session_resumable"
        assert msg["agent_slug"] == "pa" and msg["username"] == "alice"
