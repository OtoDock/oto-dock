"""The loop-side store inventory of the engines' rows: a turn's price, a Direct
session's start, a session's environment, a meeting turn's prompts, the
updater's notices, an app surface's approver, a terminal's chat owner.

Each flow runs on the test thread's loop with the recorder around the
exercised call and must record nothing (``_check``, ``_record``): the agents'
table cache starts cold and every read named in the flow's words is real.
"""
import asyncio
import functools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.events.common_events import DONE, METADATA, TEXT, CommonEvent
from core.layers.providers.base import (
    ProviderAdapter,
    ProviderStreamEvent,
    ProviderUsage,
)
from core.layers.providers.registry import register_adapter
from tests._engines import stub_builder as _builder
from tests.core.test_loop_store_inventory import _check, _record

AGENT = "eng-agent"
USERNAME = "ada"
ADMIN = "user-admin"
_DIRECT_PROVIDER = "stub-engines-inventory"
_DIRECT_MODEL = "direct-inv-1"
_CODEX_MODEL = "gpt-inv-1"
# (input, output, cache write, cache read) per 1M tokens.
_DIRECT_RATES = dict(pricing_input=4.0, pricing_output=8.0, pricing_cache_write=0.0, pricing_cache_read=0.0)
_CODEX_RATES = dict(pricing_input=2.0, pricing_output=6.0, pricing_cache_write=0.0, pricing_cache_read=0.0)


def _set_username(sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _add_agent(slug: str = AGENT, display: str = "Eng Agent") -> None:
    from storage.agents import agent_store
    agent_store.create_agent(slug, display)


def _off_loop(fn, seen: list):
    """``fn``, refusing to run on a thread with a running event loop."""
    def guarded(*a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append(fn.__name__)
            return fn(*a, **kw)
        raise AssertionError(f"{fn.__name__} ran on the event loop")
    return guarded


# ── a turn's price ───────────────────────────────────────────────────────────


class _StubAdapter(ProviderAdapter):
    """One call of 1M input tokens. ``during_call`` runs inside it. The first
    ``tool_rounds`` calls end in a tool call."""
    during_call = None
    tool_rounds = 0

    @property
    def provider_name(self) -> str:
        return _DIRECT_PROVIDER

    async def stream_response(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        if self.during_call is not None:
            await self.during_call()
        yield ProviderStreamEvent(type="usage", usage=ProviderUsage(input_tokens=1_000_000))
        if self.tool_rounds:
            self.tool_rounds -= 1
            yield ProviderStreamEvent(type="tool_stop", tool_id="t1", tool_name="Read", tool_input_json="{}")
            yield ProviderStreamEvent(type="stop", stop_reason="tool_use")

    def format_tool_results(self, results):
        return []

    def serialize_assistant_content(self, raw_content):
        return ""

    def format_image_content_block(self, *, media_type: str, base64_data: str) -> dict:
        return {}


_ADAPTER = _StubAdapter()
register_adapter(_ADAPTER)


def _reprice(model: str, layer: str, pricing_input: float) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE execution_layer_models SET pricing_input=%s WHERE layer=%s AND model_id=%s",
                     (pricing_input, layer, model))
        conn.commit()


@pytest.fixture
def direct_turn(temp_db):
    from core.layers.direct.session import DirectSession
    from storage.billing import subscription_store
    _add_agent()
    subscription_store.add_model("direct-llm", _DIRECT_MODEL, "Inv", provider=_DIRECT_PROVIDER, tier=2,
                                 context_window=123_000, **_DIRECT_RATES)
    session = DirectSession("direct-inv", AGENT, "prompt", provider=_DIRECT_PROVIDER, model=_DIRECT_MODEL)
    session.api_key = "stub-key"
    _ADAPTER.during_call = None
    yield session
    _ADAPTER.during_call = None


def _direct_metadata(loop_db_guard, session) -> tuple[set, dict]:
    from core.layers.direct.session import run_direct_stream
    frames: list = []

    async def turn():
        async for frame in run_direct_stream(session, "hi"):
            frames.append(frame)

    recorded = _record(loop_db_guard, turn)
    assert [f["type"] for f in frames][-2:] == ["metadata", "done"], frames
    return recorded, frames[-2]["data"]


def test_direct_turn_end(direct_turn, loop_db_guard):
    """The rates and the window of a store row, read with the turn's start."""
    recorded, meta = _direct_metadata(loop_db_guard, direct_turn)
    _check("direct turn end", recorded)
    assert meta["cost_usd"] == 4.0 and meta["context_max"] == 123_000


def test_a_direct_turn_is_priced_as_it_started(direct_turn, loop_db_guard):
    """A read at the turn's end would see this change, and an await there lets
    a barge-in lose a finished turn's cost."""
    async def change():
        await asyncio.to_thread(_reprice, _DIRECT_MODEL, "direct-llm", 400.0)
        direct_turn.model = "another-model"
    _ADAPTER.during_call = change
    recorded, meta = _direct_metadata(loop_db_guard, direct_turn)
    _check("direct turn end", recorded)
    assert meta["cost_usd"] == 4.0 and meta["context_max"] == 123_000


def test_a_model_picked_in_a_tool_loop_prices_what_ran_on_it_at_its_own_rates(direct_turn, loop_db_guard, monkeypatch):
    """Two calls, a pick between them: each stretch at the model that ran it,
    and the window of the model the turn ended on. A read for the new model
    happens off the loop, at the top of the call it takes effect at."""
    from core.layers.direct import session as direct
    from storage.billing import subscription_store
    subscription_store.add_model("direct-llm", "second-model", "Second", provider=_DIRECT_PROVIDER, tier=2,
                                 context_window=456_000, pricing_input=8.0, pricing_output=1.0,
                                 pricing_cache_write=1.0, pricing_cache_read=1.0)
    monkeypatch.setattr(direct, "_gate_tool_calls", AsyncMock(return_value=([], [], [])))
    monkeypatch.setattr(direct, "_execute_tool_calls", AsyncMock(return_value=[]))
    models: list = []
    real = _ADAPTER.stream_response

    async def stream_response(**kwargs):
        models.append(kwargs["model"])
        async for event in real(**kwargs):
            yield event

    async def pick():
        if len(models) == 1:
            direct_turn.model = "second-model"

    monkeypatch.setattr(_ADAPTER, "stream_response", stream_response)
    _ADAPTER.during_call = pick
    _ADAPTER.tool_rounds = 1
    recorded, meta = _direct_metadata(loop_db_guard, direct_turn)
    _check("direct turn end", recorded)
    assert models == [_DIRECT_MODEL, "second-model"]
    assert meta["cost_usd"] == 4.0 + 8.0 and meta["context_max"] == 456_000


def test_a_model_picked_during_the_last_call_prices_nothing_at_it(direct_turn, loop_db_guard):
    """No call ran on it in this turn: the next turn starts on it."""
    async def pick():
        direct_turn.model = "another-model"

    _ADAPTER.during_call = pick
    _, meta = _direct_metadata(loop_db_guard, direct_turn)
    assert meta["cost_usd"] == 4.0 and meta["context_max"] == 123_000


def test_the_long_context_line_stays_judged_on_the_whole_turn(monkeypatch):
    """Each stretch alone is under the line, the turn is over it: as before a
    pick, the multipliers apply. Moving this is a billing decision."""
    from configuration import models as models_mod
    from core.layers.direct.session import _TurnCost
    from core.layers.providers.base import ProviderUsage
    monkeypatch.setattr(models_mod, "get_model_long_context", lambda model: (150_000, 2.0, 1.5))
    costed = _TurnCost(_ADAPTER)
    total = ProviderUsage(input_tokens=100_000)
    costed.close_stretch("a", (10.0, 0.0, 0.0, 0.0), total)
    total.input_tokens += 100_000
    costed.close_stretch("b", (20.0, 0.0, 0.0, 0.0), total)
    assert costed.total(total) == pytest.approx(1.0 * 2 + 2.0 * 2)
    alone = _TurnCost(_ADAPTER)
    small = ProviderUsage(input_tokens=100_000)
    alone.close_stretch("a", (10.0, 0.0, 0.0, 0.0), small)
    assert alone.total(small) == pytest.approx(1.0)


def _codex_usage():
    from core.layers.codex.events import CodexEvent
    return CodexEvent(type="thread/tokenUsage/updated", data={"tokenUsage": {
        "last": {"inputTokens": 1_000_000, "cachedInputTokens": 0, "outputTokens": 0},
        "modelContextWindow": 500_000}})


def _codex_completed():
    from core.layers.codex.events import CodexEvent
    return CodexEvent(type="turn/completed", data={"turn": {"status": "completed"}})


def _cost_of(events: list) -> float:
    assert events[-1].type == DONE
    return next(ev.data["cost_usd"] for ev in events if ev.type == METADATA and "cost_usd" in ev.data)


@pytest.fixture
def codex_model(temp_db):
    from storage.billing import subscription_store
    subscription_store.add_model("codex-cli", _CODEX_MODEL, "Inv", provider="openai", tier=2, **_CODEX_RATES)


def test_a_remote_codex_model_change_prices_the_next_turn_at_it():
    """The machine applies the pick at its next turn/start, and the price of
    that turn is read from the translator."""
    from core.layers.codex import remote as codex_remote
    from core.layers.codex.translator import CodexEventTranslator
    state = codex_remote.CodexRemoteState(translator=CodexEventTranslator(model="spawn-model"))
    info = SimpleNamespace(session_id="s-remote", machine_id="m-1", engine_state=state)
    cm = SimpleNamespace(send_fire_and_forget=AsyncMock())
    adapter = codex_remote.CodexRemoteAdapter(MagicMock())
    assert asyncio.run(adapter.control_request(info, cm, "set_model", model="picked-model")) is True
    assert state.translator._model == "picked-model"
    assert cm.send_fire_and_forget.await_args.args[1]["kwargs"] == {"model": "picked-model"}


def _remote_codex_turn(*, during=None):
    """One turn of a Codex session on a machine that plays a usage line and
    the completion; ``during(info)`` runs once the turn has started."""
    from core.layers.codex.remote import CodexRemoteState
    from core.layers.codex.translator import CodexEventTranslator
    from core.remote.remote_execution import RemoteSessionInfo
    from core.session.session_manager import get_layer_by_path
    events: list = []

    async def turn():
        info = RemoteSessionInfo(session_id="codex-inv-remote", machine_id="m-1", agent_name=AGENT,
                                 execution_path="codex-cli", event_queue=asyncio.Queue())
        info.current_send_command_id = "cmd-1"
        translator = CodexEventTranslator(model=_CODEX_MODEL, supervised_bg=True)
        info.engine_state = CodexRemoteState(translator=translator)
        info.engine_state.default_consumer = q = asyncio.Queue()
        if during is not None:
            read_rates = translator.read_turn_rates

            async def started():
                await read_rates()
                await during(info)
            translator.read_turn_rates = started
        usage, done = _codex_usage(), _codex_completed()
        q.put_nowait({"method": usage.type, "params": usage.data})
        q.put_nowait({"method": done.type, "params": done.data})
        async for ev in get_layer_by_path("codex-cli").remote_adapter().stream_turn(info, MagicMock()):
            events.append(ev)
    return turn, events


def test_codex_turn_end_remote(codex_model, loop_db_guard):
    turn, events = _remote_codex_turn()
    _check("codex turn end, remote", _record(loop_db_guard, turn))
    assert _cost_of(events) == 2.0


def test_a_remote_codex_turn_is_priced_at_the_model_it_ran_on(codex_model, loop_db_guard):
    """A model picked during a turn applies on the machine's next
    ``turn/start``: the turn under way keeps the rates it started with."""
    async def pick(info):
        cm = SimpleNamespace(send_fire_and_forget=AsyncMock())
        from core.session.session_manager import get_layer_by_path
        assert await get_layer_by_path("codex-cli").remote_adapter().control_request(
            info, cm, "set_model", model="gpt-6.1-sol")
        assert info.engine_state.translator._model == "gpt-6.1-sol"
    turn, events = _remote_codex_turn(during=pick)
    _check("codex turn end, remote", _record(loop_db_guard, turn))
    assert _cost_of(events) == 2.0


# ── a Direct session's start ─────────────────────────────────────────────────


def test_direct_session_start(temp_db, monkeypatch, loop_db_guard):
    """With the layer's model and with none: the agent's own, real reads."""
    from core.layers.direct import session as direct
    from storage.billing import subscription_store
    _add_agent()
    subscription_store.add_model("claude-code-cli", "claude-inv-1", "Inv", provider="anthropic", tier=2)

    class _Manager:
        servers: dict = {}

        def get_tools(self):
            return []

    async def get_or_create(session_id, agent_name, **kw):
        return _Manager()
    monkeypatch.setattr(direct.mcp_pool, "get_or_create", get_or_create)
    made = {}

    async def start(sid, **kw):
        made[sid] = await direct.create_direct_session(sid, AGENT, system_prompt="P", **kw)

    try:
        recorded = _record(loop_db_guard, functools.partial(start, "direct-given", model=_DIRECT_MODEL))
        recorded |= _record(loop_db_guard, functools.partial(start, "direct-empty"))
    finally:
        for sid in ("direct-given", "direct-empty"):
            direct._direct_sessions.pop(sid, None)
    _check("direct session start", recorded)
    assert made["direct-given"].model == _DIRECT_MODEL
    assert made["direct-empty"].model == "claude-inv-1"


@pytest.mark.asyncio
async def test_the_direct_layer_hands_its_model_to_the_session(monkeypatch, tmp_path):
    from core.execution_layer import AgentConfig
    from core.layers.direct import layer as direct_layer
    from core.sandbox import sandbox as sb
    from services.mcp import mcp_registry
    seen: dict = {}

    async def fake_create(session_id, **kw):
        seen.update(kw)
        return SimpleNamespace(session_id=session_id, model=kw["model"], effort="", user_sub="", sandbox_cfg=None)

    monkeypatch.setattr(direct_layer, "create_direct_session", fake_create)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
    monkeypatch.setattr(sb, "resolve_sandbox_config", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sb, "SandboxBuilder", _builder)
    monkeypatch.setattr(direct_layer, "_record_session_use", lambda *a, **k: None)
    await direct_layer.DirectLLMExecutionLayer()._start_session_impl("s-model", AgentConfig(
        agent_name="ag", model=_DIRECT_MODEL, sandbox_host_claude_dir=str(tmp_path)))
    assert seen["model"] == _DIRECT_MODEL


# ── a session's environment ──────────────────────────────────────────────────


@pytest.fixture
def env_person(temp_db):
    """The person the spawn's environment looks up by username, the agent's
    row and its memory settings: the builder's reads, all real."""
    from storage.identity import db_users
    _add_agent()
    _set_username(ADMIN, USERNAME)
    db_users.add_user_agent(ADMIN, AGENT, "manager", ADMIN)


@pytest.fixture
def ssh_hosts_installed(env_person, monkeypatch, tmp_path):
    """What a real install holds at a start: ssh-hosts loaded (the startup's
    scan) and, here, one instance giving this agent a key. An empty registry
    would never reach the keys' reads, and the flow's answer would follow
    whichever test last scanned the real manifests. Returns the key's
    private bytes."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from services.mcp import mcp_manifest_store
    from services.mcp.mcp_manifest_types import (
        CredentialConfig,
        McpManifest,
        ServerConfig,
    )
    from storage.mcp import mcp_store
    mcp_dir = tmp_path / "ssh-hosts"
    (mcp_dir / "keys").mkdir(parents=True)
    private = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
    (mcp_dir / "keys" / "prod").write_bytes(private)
    manifest = McpManifest(
        name="ssh-hosts", label="SSH", description="", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"), credentials=CredentialConfig(),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[],
        assignment_mode="explicit", mcp_dir=mcp_dir)
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {"ssh-hosts": manifest})
    mcp_store.set_mcp_enabled("ssh-hosts", True)
    mcp_store.add_agent_mcp(AGENT, "ssh-hosts")
    mcp_store.upsert_mcp_instance("ssh-hosts", {
        "instance_name": "prod", "agents": [AGENT],
        "field_values": {"host": "prod.example", "key_name": "prod"}})
    return private


def _keys_held(folder: Path, private: bytes) -> None:
    """The session's key dir: the public half alone (the proxy signs), and
    nothing of the private key."""
    assert sorted(p.name for p in folder.iterdir()) == ["prod.pub"]
    assert (folder / "prod.pub").read_text().startswith("ssh-ed25519 ")
    assert private not in (folder / "prod.pub").read_bytes()


def _scope_dir(name: str) -> Path:
    """The person's config folder of the agent, beneath the suite's agents
    root as every start's is: the keys and the tools' copy are written
    beneath the agent's folder."""
    import config
    folder = config.AGENTS_DIR / AGENT / "users" / USERNAME / name
    folder.mkdir(parents=True)
    return folder


def _token_owner(env: dict) -> str:
    from auth.session_token import validate_session_token
    return (validate_session_token(env["PROXY_API_KEY"]) or {}).get("user_sub") or ""


def _spawn_plan(loop_db_guard, layer, session_id: str, cfg) -> tuple[set, object]:
    """The engine's spawn plan for a machine, with what its preparation and
    its environment read on the loop."""
    plans: list = []

    async def plan():
        plans.append(await layer.prepare_spawn(session_id, cfg))
    return _record(loop_db_guard, plan), plans[0]


def test_cli_terminal_environment(ssh_hosts_installed, tmp_path, loop_db_guard, monkeypatch):
    from auth.path_policy import SecurityContext
    from core.execution_layer import AgentConfig
    from core.layers.cli.layer import CLIExecutionLayer
    claude_dir = _scope_dir(".claude")
    cfg = AgentConfig(
        agent_name=AGENT, user_sub=ADMIN, model="claude-opus-5", effort="high", chat_id="chat-env",
        client_type="interactive_cli", permission_mode="default", interactive=True,
        sandbox_host_claude_dir=str(claude_dir),
        security_context=SecurityContext(role="manager", username=USERNAME, agent=AGENT,
                                         is_admin_agent=False, session_scope="user"))
    recorded, plan = _spawn_plan(loop_db_guard, CLIExecutionLayer(), "sess-env-cli", cfg)
    _check("cli terminal environment", recorded)
    assert _token_owner(plan.env) == ADMIN
    _keys_held(claude_dir / "ssh", ssh_hosts_installed)
    assert plan.env["OTO_SSH_KEY_DIR"].endswith("/ssh")


def test_codex_daemon_environment(env_person, tmp_path, loop_db_guard):
    """A headless session's plan. Its environment is given no username, so
    its own mint names nobody: the person is the builder's token's
    (``credential_env``), laid over it."""
    from auth.path_policy import SecurityContext
    from auth.session_token import create_session_token
    from core.execution_layer import AgentConfig
    from core.layers.codex.layer import CodexCLIExecutionLayer
    codex_dir = _scope_dir(".codex")
    sid = "sess-env-daemon"
    cfg = AgentConfig(
        agent_name=AGENT, user_sub=ADMIN, model="gpt-6", chat_id="chat-env", system_prompt="P",
        client_type="dashboard", permission_mode="default",
        sandbox_host_claude_dir=str(codex_dir),
        credential_env={"PROXY_API_KEY": create_session_token(sid, AGENT, ADMIN)},
        security_context=SecurityContext(role="manager", username=USERNAME, agent=AGENT,
                                         is_admin_agent=False, session_scope="user"))
    recorded, plan = _spawn_plan(loop_db_guard, CodexCLIExecutionLayer(), sid, cfg)
    _check("codex daemon environment", recorded)
    assert _token_owner(plan.env) == ADMIN
    assert plan.env["OTO_HOOK_NO_FORWARD"] == "1" and "OTO_INTERACTIVE" not in plan.env


def test_codex_terminal_environment(ssh_hosts_installed, tmp_path, loop_db_guard, monkeypatch):
    from auth.path_policy import SecurityContext
    from core.execution_layer import AgentConfig
    from core.layers.codex.layer import CodexCLIExecutionLayer
    codex_dir = _scope_dir(".codex")
    cfg = AgentConfig(
        agent_name=AGENT, user_sub=ADMIN, model="gpt-6", chat_id="chat-env", system_prompt="P",
        client_type="interactive_cli", permission_mode="default", interactive=True,
        sandbox_host_claude_dir=str(codex_dir),
        security_context=SecurityContext(role="manager", username=USERNAME, agent=AGENT,
                                         is_admin_agent=False, session_scope="user"))
    recorded, plan = _spawn_plan(loop_db_guard, CodexCLIExecutionLayer(), "sess-env-codex", cfg)
    _check("codex terminal environment", recorded)
    assert _token_owner(plan.env) == ADMIN
    _keys_held(codex_dir / "ssh", ssh_hosts_installed)
    assert plan.env["OTO_SSH_KEY_DIR"].endswith("/ssh")


def test_direct_layer_start(env_person, monkeypatch, tmp_path, loop_db_guard):
    """The Direct layer's start with the MCP pool stubbed: the sandbox's reads
    and the session's model handed in."""
    from auth.path_policy import SecurityContext
    from core.execution_layer import AgentConfig
    from core.layers.direct import layer as direct_layer, session as direct

    class _Manager:
        servers: dict = {}

        def get_tools(self):
            return []

    async def get_or_create(session_id, agent_name, **kw):
        return _Manager()
    monkeypatch.setattr(direct.mcp_pool, "get_or_create", get_or_create)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    cfg = AgentConfig(
        agent_name=AGENT, user_sub=ADMIN, model=_DIRECT_MODEL, system_prompt="P", client_type="dashboard",
        permission_mode="default", sandbox_host_claude_dir=str(claude_dir),
        extra_env={"_API_KEY": "k", "_PROVIDER": _DIRECT_PROVIDER},
        security_context=SecurityContext(role="manager", username=USERNAME, agent=AGENT,
                                         is_admin_agent=False, session_scope="user"))
    try:
        recorded = _record(loop_db_guard, lambda: direct_layer.DirectLLMExecutionLayer().start_session("sess-env-direct", cfg))
        assert direct._direct_sessions["sess-env-direct"].model == _DIRECT_MODEL
    finally:
        direct._direct_sessions.pop("sess-env-direct", None)
    _check("direct layer start", recorded)


# ── a meeting turn's prompts ─────────────────────────────────────────────────


class _ScriptedLayer:
    def session_lock(self, sid):
        return asyncio.Lock()

    async def send_message(self, sid, prompt):
        yield CommonEvent(type=TEXT, data={"content": f"{sid} says hello."})


def test_meeting_turn_prompts(temp_db, monkeypatch, loop_db_guard):
    """The live turn and a parallel batch, the batch's prompts one job, with
    the participants' names read for a real prompt."""
    from services.meetings import meeting_orchestrator as MO
    for slug, name in (("mod", "Moderator"), ("p1", "Pat One"), ("p2", "Pat Two")):
        _add_agent(slug, name)
    meeting = {"id": "m1", "topic": "t", "moderator": "mod", "parent_chat_id": "chat-inv",
               "participants": json.dumps(["mod", "p1", "p2"])}
    transcript = [{"agent": "mod", "content": "Welcome.", "role": "agent"}]
    pending = {"p1": [{"type": "start"}]}
    sessions = {"p1": "sid-p1", "p2": "sid-p2"}
    # The one dict both modules read: a turn asks its layer of it.
    for sid in sessions.values():
        monkeypatch.setitem(MO._meeting_session_layers, sid, _ScriptedLayer())
    jobs: list = []
    real_run_db = MO.run_db

    async def counting_run_db(fn, /, *a, **kw):
        jobs.append(getattr(fn, "__name__", repr(fn)))
        return await real_run_db(fn, *a, **kw)
    monkeypatch.setattr(MO, "run_db", counting_run_db)
    prompts: dict = {}
    real_build = MO.build_turn_prompt

    def noting_build(meeting, slug, *a, **kw):
        prompts[slug] = real_build(meeting, slug, *a, **kw)
        return prompts[slug]
    monkeypatch.setattr(MO, "build_turn_prompt", noting_build)
    queue_of: dict = {}

    async def live():
        queue_of["q"] = asyncio.Queue()
        await MO._run_live_turn("p1", sessions, meeting, transcript, pending, queue_of["q"], "m1")

    async def batch():
        await MO._run_parallel_batch(["p1", "p2"], sessions, meeting, transcript, {}, asyncio.Queue(), "m1")

    _check("meeting turn prompts", _record(loop_db_guard, live))
    assert jobs.count("noting_build") == 1 and "Moderator" in prompts["p1"]
    prompts.clear()
    _check("meeting turn prompts", _record(loop_db_guard, batch))
    assert jobs.count("_batch_prompts") == 1 and set(prompts) == {"p1", "p2"}
    assert "Moderator" in prompts["p2"]


# ── the updater's label ──────────────────────────────────────────────────────


@pytest.fixture
def notices(monkeypatch):
    """What ``fire_notification`` wrote: the real one reads in its worker
    threads, with its deliveries going to nobody connected."""
    import services.notifications.notification_manager as nm
    real = nm.fire_notification
    titles: list = []

    async def noting(*a, **kw):
        titles.append(kw["title"])
        return await real(*a, **kw)
    monkeypatch.setattr(nm, "fire_notification", noting)
    monkeypatch.setattr(nm, "get_active_connections", lambda u: [])
    monkeypatch.setattr(nm, "get_all_connections", lambda u: [])
    return titles


def test_updater_notices(temp_db, notices, loop_db_guard):
    from services.community import community_agent_updater as upd
    _add_agent()
    report = {"to_version": "2.0.0", "replaced": [], "kept": [], "pending_apps": [], "offered_checks": []}
    recorded = _record(loop_db_guard, functools.partial(upd._notify_report, AGENT, ADMIN, report))
    recorded |= _record(loop_db_guard, functools.partial(upd._notify_failed, AGENT, ADMIN, "it broke"))
    _check("updater notices", recorded)
    assert notices == ["Eng Agent updated to 2.0.0", "The update of Eng Agent failed"]


def test_updater_sweep(temp_db, notices, monkeypatch, loop_db_guard):
    """The daily sweep, one label for all of an agent's managers."""
    from services.community import (
        community_agent_updater as upd,
        community_agents_catalog,
    )
    from storage.agents import agent_store
    from storage.identity import db_users
    from storage.pg import get_conn
    _add_agent()
    db_users.add_user_agent(ADMIN, AGENT, "manager", ADMIN)
    db_users.add_user_agent("user-manager", AGENT, "manager", ADMIN)
    with get_conn() as conn:
        conn.execute("UPDATE agents SET community_template=%s, community_template_version=%s WHERE slug=%s",
                     ("uptpl", "1.0.0", AGENT))
        conn.commit()
    agent_store._invalidate_cache()
    agent_store.set_community_template_data(AGENT, {"slug": "uptpl", "version": "1.0.0"})
    monkeypatch.setattr(community_agents_catalog, "fetch_registry",
                        AsyncMock(return_value={"agents": [{"slug": "uptpl", "version": "2.0.0"}]}))
    monkeypatch.setattr(upd, "_last_notify_sweep", float("-inf"))
    told: list = []

    async def sweep():
        told.append(await upd.maybe_notify_updates())

    _check("updater sweep", _record(loop_db_guard, sweep))
    assert told == [2] and notices == ["A new version of Eng Agent is ready"] * 2


# ── an app surface's approver ────────────────────────────────────────────────


@pytest.fixture
def app_rig(temp_db, monkeypatch, tmp_path):
    """A consenting person who may approve, and the import stubbed: what it
    is handed is ``approve_as``."""
    from services.apps import app_blueprints
    from services.community import template_app_seeder as seeder
    from storage.agents import agent_store
    from storage.identity import db_users
    monkeypatch.setattr("config.AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / AGENT).mkdir(parents=True)
    agent_store.create_agent(AGENT, "Eng Agent", collaborative=True)
    _set_username(ADMIN, USERNAME)
    db_users.add_user_agent(ADMIN, AGENT, "manager", ADMIN)
    approvals: list = []

    async def import_folder(agent, username, owner, slug, source, **kw):
        approvals.append(kw["approve_as"])
        return {"status": "ok", "app_id": "", "release": 1}
    monkeypatch.setattr(app_blueprints, "import_folder", import_folder)
    monkeypatch.setattr(seeder, "copy_matches", lambda item, folder: True)
    return SimpleNamespace(approvals=approvals, seeder=seeder, tmp_path=tmp_path)


def _app_item(tmp_path: Path, slug: str, visibility: str) -> SimpleNamespace:
    folder = tmp_path / "items" / slug
    folder.mkdir(parents=True)
    return SimpleNamespace(slug=slug, dir=folder, visibility=visibility, owner_approval=False, sig="sig-1",
                           blueprint={}, doc={}, tree_sha="")


def test_seed_shared_approver(app_rig, loop_db_guard):
    item = _app_item(app_rig.tmp_path, "board", "agent")
    template = SimpleNamespace(slug="ops", version="1.0.0", apps=[item])
    data = {"consent": {"apps": {"board": {"sig": "sig-1", "by": ADMIN}}}}
    out: dict = {}

    async def seed():
        out.update(await app_rig.seeder.seed_shared(AGENT, template, data))

    _check("seed shared approver", _record(loop_db_guard, seed))
    assert out["seeded"] == ["board"] and app_rig.approvals == [ADMIN]


def _stored_user_copy(app_rig, monkeypatch) -> dict:
    """The record of a per-user app and its stored seed."""
    seeder = app_rig.seeder
    item = _app_item(app_rig.tmp_path, "home", "user")
    template = SimpleNamespace(slug="ops", version="1.0.0", apps=[item])
    monkeypatch.setattr(seeder, "load_template_from_dict", lambda data: template)
    source = seeder.seed_source_dir(AGENT, "home")
    source.mkdir(parents=True)
    return {"slug": "ops", "version": "1.0.0", "consent": {"apps": {"home": {"sig": "sig-1", "by": ADMIN}}}}


def test_seed_user_copy_approver(app_rig, monkeypatch, loop_db_guard):
    data = _stored_user_copy(app_rig, monkeypatch)
    out: list = []

    async def seed():
        out.append(await app_rig.seeder.seed_user_copy(AGENT, ADMIN, "home", data=data))

    _check("seed user copy approver", _record(loop_db_guard, seed))
    assert out[0][0] == "seeded" and app_rig.approvals == [ADMIN]


def test_restore_approver(app_rig, monkeypatch, loop_db_guard):
    from storage.agents import agent_store
    data = _stored_user_copy(app_rig, monkeypatch)
    agent_store.set_community_template_data(AGENT, data)
    row = {"agent": AGENT, "slug": "home", "username": USERNAME, "owner_sub": ADMIN, "template_ref": "ops:home"}
    _check("restore approver", _record(loop_db_guard, functools.partial(app_rig.seeder.restore, row)))
    assert app_rig.approvals == [ADMIN]


def test_an_update_and_take_judge_the_approver_off_the_loop(tmp_path, temp_db, monkeypatch):
    """Narrower than a flow: an update's and a take's other reads are not this
    row's, so the approver alone refuses the loop."""
    from services.community import (
        community_agent_updater as upd,
        template_app_seeder as seeder,
    )
    from storage import database as db
    from tests.mcp.test_community_agent_updater import (
        ADMIN_SUB,
        AGENT as UP_AGENT,
        _agent_dir,
        _apply,
        _install_v1,
        _v2,
    )
    _install_v1(tmp_path)
    seen: list = []
    monkeypatch.setattr(seeder, "admits", _off_loop(seeder.admits, seen))
    _apply(_v2(tmp_path))
    assert seen, "the update never judged an app"
    # A copy edited after the update: the update keeps it, the take swaps it.
    admin = db.get_username_by_sub(ADMIN_SUB)
    (_agent_dir() / "users" / admin / "workspace" / "apps" / "home" / "client" / "index.html").write_text("<p>mine</p>")
    report = _apply(_v2(tmp_path, sub="v3", home_html="<p>v3</p>"))
    assert any(k["new_path"].endswith("user-apps/home") for k in report["kept"]), report
    seen.clear()
    with patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()):
        asyncio.run(upd.take(UP_AGENT, "config/community/2.0.0/user-apps/home", ADMIN_SUB, "admin"))
    assert seen, "the take never judged the app"


# ── a terminal's chat owner ──────────────────────────────────────────────────


def _terminal(chat_row) -> object:
    from core.session.interactive_session import InteractiveSession
    return InteractiveSession(session_id="term-inv", chat_id="chat-term", agent_name=AGENT, user_sub=ADMIN,
                              chat_row=chat_row)


def test_terminal_chat_owner(temp_db, loop_db_guard):
    """A row that failed to load (``{}``) answers with the person, a loaded
    row with its owner, and neither reads, whatever the store holds."""
    from storage import database as db
    db.create_chat("chat-term", "agent::eng-agent", AGENT, "default")
    owners: list = []

    async def ask(chat_row):
        owners.append(_terminal(chat_row)._chat_owner())

    recorded = _record(loop_db_guard, functools.partial(ask, {}))
    recorded |= _record(loop_db_guard, functools.partial(ask, {"user_sub": "agent::eng-agent"}))
    _check("terminal chat owner", recorded)
    assert owners == [ADMIN, "agent::eng-agent"]
