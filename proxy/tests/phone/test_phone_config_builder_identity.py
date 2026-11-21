"""The phone config builder per identity mode.

Heavy collaborators (MCP config assembly, dynamic contexts, the remote store,
the subscription pool) are stubbed; the SecurityContext, the persistent dir,
the prompt composition and the user-route delegation to the dashboard
builder are the subject.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import config
from core import placement
from core.config import phone_config_builder as pcb
from core.execution_layer import AgentConfig
from core.session import external_identity
from services.phone.phone_identity import RouteIdentity
from services.remote.remote_status import (
    serving_compute_machine as _real_serving_compute_machine,
)
from tests._compute import serve

SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def agent(temp_db):
    from storage.agents import agent_store
    slug = f"pcb-{uuid.uuid4().hex[:6]}"
    agent_store.create_agent(slug, "Support")
    persona = config.get_agent_dir(slug) / "config" / "agent.md"
    persona.parent.mkdir(parents=True, exist_ok=True)
    persona.write_text("# Support\n\nA support agent.\n")
    return slug


@pytest.fixture
def stubs(monkeypatch):
    from services.engines import subscription_pool
    from services.mcp import dynamic_context, mcp_agent_skills, mcp_registry
    from services.remote import remote_status
    from storage import remote_store
    seen: dict = {}
    monkeypatch.setattr(config, "get_cli_model", lambda name: "model-x")

    def _build(agent_name, user_sub, **kw):
        seen["mcp_kwargs"] = kw
        return None, {}, {"display-mcp": "Excluded in phone mode"}, {}, set()
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _build)
    for module in (mcp_registry, mcp_agent_skills):
        monkeypatch.setattr(module, "get_agent_mcps", lambda *a, **k: [
            SimpleNamespace(name="memory-mcp"), SimpleNamespace(name="display-mcp"),
        ])

    async def _dyn(agent_name, names, **kw):
        seen["dynamic_names"] = list(names)
        seen["dynamic_kwargs"] = kw
        return []
    monkeypatch.setattr(dynamic_context, "get_dynamic_contexts", _dyn)
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-1", {"_PROVIDER": "anthropic"}))
    monkeypatch.setattr(remote_status, "resolve_execution_target",
                        lambda agent, sub, role: (seen.setdefault("target", "local"), None))
    # A compute machine serves the platform's place: a call resolved there
    # is built on it (the platform runs no session itself).
    serve(monkeypatch)

    def _placement_of(value, sub, agent, *, compute=None):
        if compute:
            return placement.from_machine(placement.KIND_COMPUTE, compute)
        return (placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE)
                if not placement.is_local(value) else placement.LOCAL_PLACEMENT)
    monkeypatch.setattr(remote_store, "placement_of", _placement_of)
    monkeypatch.setattr(remote_store, "get_target_browser_settings", lambda p: None)
    return seen


def _identity(agent, *, mode="caller", role="viewer", phone="+302101234567", **kw):
    ident = external_identity.resolve(
        external_identity.PHONE, phone, session_id=SID,
        shared=(mode == "shared"), **kw,
    )
    return RouteIdentity(mode=mode, role=role, external=ident)


@pytest.mark.asyncio
async def test_the_prompt_is_built_off_the_loop(agent, stubs, monkeypatch):
    """``build_agent_prompt`` (the files, the caller's memory and context,
    the tree walk) runs in a worker thread: the loop thread never builds it."""
    import asyncio
    import threading
    seen: dict = {}

    def recorder(*a, **k):
        seen["thread"] = threading.get_ident()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen["loop"] = None
        else:
            seen["loop"] = "running"
        return "PROMPT"

    monkeypatch.setattr(config, "build_agent_prompt", recorder)
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    assert cfg.system_prompt.startswith("PROMPT")
    assert seen["thread"] != threading.get_ident() and seen["loop"] is None


@pytest.mark.asyncio
async def test_caller_mode_builds_the_caller_tree(agent, stubs):
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    ctx = cfg.security_context
    home = config.get_agent_dir(agent) / "externals" / "phone" / "302101234567"
    assert ctx.principal == "external" and ctx.role == "external"
    assert ctx.external_home == str(home) and ctx.external_claim == "phone:+302101234567"
    assert ctx.session_scope == "agent" and ctx.config_visible is False
    assert cfg.sandbox_host_claude_dir == str(home / ".claude")
    assert (home / ".claude" / "settings.json").is_file()
    assert cfg.client_type == "phone" and cfg.permission_mode == "auto"
    assert stubs["mcp_kwargs"]["external"] is True and stubs["mcp_kwargs"]["phone_mode"] is True
    # The caller is a viewer: an MCP or skill above that audience stays out.
    assert stubs["mcp_kwargs"]["user_role"] == "external" == stubs["dynamic_kwargs"]["user_role"]
    assert stubs["mcp_kwargs"]["shared_chat"] is True and stubs["mcp_kwargs"]["outside_picks"] is not None
    # Only attached MCPs describe themselves; the not-picked ones keep their lookups.
    assert stubs["dynamic_names"] == ["memory-mcp"]
    assert stubs["dynamic_kwargs"]["lookup_mcp_names"] == []
    assert "external caller" in cfg.system_prompt and "/caller/workspace/" in cfg.system_prompt
    # The person's memory rides on the call's start (empty or not), read,
    # never written: the arguments the builder recorded for the session.
    from core.session import start_context
    at_start = start_context.inline_block(SID)
    assert "What you remember of this person comes with the context" in cfg.system_prompt
    assert "## This person's memory" in at_start and "## This person's memory" not in cfg.system_prompt
    assert "## User memory" not in cfg.system_prompt + at_start
    assert cfg.extra_env.get("_USER_SUB", None) in (None, "")


@pytest.mark.asyncio
async def test_a_skill_above_a_callers_viewer_role_stays_out_of_the_prompt(
    agent, stubs, monkeypatch,
):
    """A caller is a viewer: a skill whose ``audience`` is ``editor`` (on an
    MCP the caller keeps) stays out of the caller's prompt, and a skill for
    everyone stays in. The prompt's skill gate reads the role the builder
    passes; with none it filters nothing."""
    from services.mcp import mcp_registry
    seen: dict = {}

    def _skills(model, *, context="", placement=None,
                external=False, user_role=None, shared_chat=False, outside_picks=None,
                kind_rule=None):
        seen["user_role"] = user_role
        seen["shared_chat"] = shared_chat
        seen["picks"] = outside_picks
        out = [("everyone", "EVERYONE-SKILL-BODY", "always")]
        if mcp_registry.skill_audience_admits(SimpleNamespace(audience="editor"), user_role):
            out.append(("editors", "EDITOR-ONLY-SKILL-BODY", "always"))
        return out
    monkeypatch.setattr(mcp_registry, "get_skills_for_agent", _skills)
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    assert seen["user_role"] == "external"
    # An outside session is nobody's own chat, and the prompt's skill
    # readers get the picks the config build got.
    assert seen["shared_chat"] is True and seen["picks"] is not None
    assert "EVERYONE-SKILL-BODY" in cfg.system_prompt
    assert "EDITOR-ONLY-SKILL-BODY" not in cfg.system_prompt


@pytest.mark.asyncio
async def test_callers_are_viewers_whatever_the_identity_says(agent, stubs):
    """The per-route role selector was removed (2026-09-08): even an
    identity carrying a legacy manager role builds a viewer context —
    knowledge stays read-only and /config never appears."""
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent, role="manager"), session_id=SID,
    )
    ctx = cfg.security_context
    assert ctx.role == "external" and ctx.knowledge_rw is False and ctx.config_visible is False
    assert "`/knowledge/` (RW)" not in cfg.system_prompt and "/config" not in cfg.system_prompt


@pytest.mark.asyncio
async def test_shared_mode_gets_a_scratch_home(agent, stubs):
    """An identity with no tree of its own runs from a scratch home keyed
    on the session, never from the agent's own CLI state."""
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent, mode="shared"), session_id=SID,
    )
    ctx = cfg.security_context
    scratch = config.get_agent_dir(agent) / "externals" / "phone" / "_ephemeral" / SID
    assert ctx.principal == "external" and ctx.external_home == str(scratch)
    assert ctx.external_ephemeral is True and scratch.is_dir()
    assert ctx.external_claim == "phone:"
    assert cfg.sandbox_host_claude_dir == str(scratch / ".claude")
    assert "keeps no per-caller memory" in cfg.system_prompt


@pytest.mark.asyncio
async def test_remote_target_keeps_the_agent_scope(agent, stubs):
    stubs["target"] = "machine-1"
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    ctx = cfg.security_context
    assert ctx.principal == "external" and ctx.external_home == ""
    assert ctx.external_claim == "phone:+302101234567"   # memory still keyed on it
    assert ctx.placement.admin_paired


@pytest.mark.asyncio
async def test_a_compute_machine_keeps_the_callers_tree(agent, stubs, monkeypatch):
    """A compute machine works on the platform's own folders, where the
    caller's tree is: the call mounts it as on the platform's sandbox. Only a
    machine that works on a synced copy keeps the agent-scope mount."""
    from storage import remote_store
    stubs["target"] = "machine-c"
    monkeypatch.setattr(remote_store, "placement_of", lambda value, sub, agent: (
        placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="machine-c")))
    cfg = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    ctx = cfg.security_context
    assert ctx.principal == "external" and ctx.external_home.endswith("302101234567")


def _serving(monkeypatch, row):
    """The compute machine that serves, as the builders read it; what each
    read asked for."""
    from services.remote import remote_status
    from storage import remote_store
    asked: list = []
    monkeypatch.setattr(remote_status, "serving_compute_machine",
                        lambda path="", **kw: asked.append((path, kw)) or row)

    def _placement_of(value, sub, ag, *, compute=None):
        return (placement.from_machine(placement.KIND_COMPUTE, {**compute, "pairing_scope": "compute"})
                if compute else placement.LOCAL_PLACEMENT)
    monkeypatch.setattr(remote_store, "placement_of", _placement_of)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli", "direct-llm"])
async def test_a_call_is_placed_on_the_compute_machine_that_serves(agent, stubs, monkeypatch, engine):
    """Every engine's call: the session of a Claude Code or Codex call, the
    tools of a Direct LLM one. The machine stays in the call's record, and
    it is one that shares the platform's folders: a caller's tree never
    syncs."""
    from storage.agents import agent_store
    agent_store.update_agent(agent, execution_path=engine)
    asked = _serving(monkeypatch, {"id": "compute-1"})
    cfg = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert asked == [(engine, {"prefer": "", "shared_folder": True})]
    assert cfg.security_context.placement.machine_id == "compute-1"
    assert cfg.security_context.placement.kind == placement.KIND_COMPUTE
    # The target stays the platform's place: the machine is in the placement.
    assert placement.is_local(cfg.execution_target)


class _TwoMachines:
    """The connections as the balancer reads them: a compute machine beside
    the platform and one on another server with its own copy, which has the
    most room."""

    SHARED, OWN = "compute-shared", "compute-own"

    def __init__(self, connected=(SHARED, OWN)):
        self.connected = set(connected)

    def compute_machine_ids(self):
        return frozenset(self.connected)

    def tunnel_ok(self, mid):
        return True

    def shares_folders(self, mid):
        return mid == self.SHARED and mid in self.connected

    def owns_copy(self, mid):
        return mid == self.OWN and mid in self.connected

    def satellite_name(self, mid):
        return mid

    def satellite_version(self, mid):
        return "0.5.177"

    def compute_address(self, mid):
        return "192.168.50.7" if mid == self.OWN else ""

    def satellite_session_home_dirs(self, mid):
        return frozenset()

    def exit_state(self, mid):
        return 0, False


def _two_machines(monkeypatch, connected=(_TwoMachines.SHARED, _TwoMachines.OWN)):
    """The real decision of where, over two connected compute machines."""
    from core import concurrency
    from core.remote import satellite_connection
    from services.remote import remote_status
    from storage import remote_store
    manager = _TwoMachines(connected)
    # The fixture's one machine steps aside: the decision reads the connections.
    monkeypatch.setattr(remote_status, "serving_compute_machine", _real_serving_compute_machine)
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(concurrency, "box_room_mb", lambda mid: 9_000 if mid == _TwoMachines.OWN else 100)

    def _placement_of(value, sub, ag, *, compute=None):
        return (placement.from_machine(placement.KIND_COMPUTE, compute)
                if compute else placement.LOCAL_PLACEMENT)
    monkeypatch.setattr(remote_store, "placement_of", _placement_of)
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli", "direct-llm"])
async def test_an_outside_callers_call_never_lands_on_a_machine_with_its_own_copy(agent, stubs, monkeypatch, engine):
    """The machine on another server has far more room, and a new chat would
    go there. An outside person's call stays beside the platform, where their
    tree is. With only that other machine connected the call and its wrap-up
    are refused with the sentence: they run neither there nor on the
    platform itself."""
    from services.remote import remote_status
    from storage.agents import agent_store
    agent_store.update_agent(agent, execution_path=engine)
    manager = _two_machines(monkeypatch)
    if engine != "direct-llm":
        assert remote_status.serving_compute_machine(engine)["id"] == _TwoMachines.OWN
    cfg = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    where = cfg.security_context.placement
    assert where.machine_id == _TwoMachines.SHARED and not where.own_copy
    assert cfg.security_context.external_home.endswith("302101234567")
    wrapup = await pcb.build_wrapup_agent_config(agent, {"chat_id": "chat-1"}, str(uuid.uuid4()),
                                                 prefer=_TwoMachines.OWN)
    assert wrapup.security_context.placement.machine_id == _TwoMachines.SHARED
    manager.connected.discard(_TwoMachines.SHARED)
    with pytest.raises(placement.NoComputeMachine) as refused:
        await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
    with pytest.raises(placement.NoComputeMachine) as refused:
        await pcb.build_wrapup_agent_config(agent, {"chat_id": "chat-1"}, str(uuid.uuid4()),
                                            prefer=_TwoMachines.OWN)
    assert str(refused.value) == placement.NO_COMPUTE_SENTENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli"])
async def test_a_callers_sandbox_and_a_wrapups_render_on_a_compute_machine(agent, stubs, monkeypatch, engine):
    """What the compute machine gets for an outside caller's call and for
    its wrap-up: the spawn plan the engine prepares from the real config.
    Every source of its sandbox is inside the agent's own folder or the
    tools' (a machine refuses any other), the engine's home is the caller's
    tree or the run's scratch one, and the description survives the wire."""
    import json

    from core.sandbox import sandbox_render as render, session_config_dir
    from core.session.session_manager import get_layer_by_path
    from storage.agents import agent_store
    agent_store.update_agent(agent, execution_path=engine)
    _serving(monkeypatch, {"id": "compute-1"})

    async def _no_keys(*_a, **_k):
        return ""
    monkeypatch.setattr(session_config_dir, "provision_ssh_keys_for_sandbox", _no_keys)
    # The preparation reads the agent's real tool manifests: none here.
    from services.mcp import mcp_agent_skills, mcp_registry
    for module in (mcp_registry, mcp_agent_skills):
        monkeypatch.setattr(module, "get_agent_mcps", lambda *a, **k: [])
    layer = get_layer_by_path(engine)

    async def _plan(cfg, sid):
        plan = await layer.prepare_spawn(sid, cfg)
        desc = render.SandboxDescription.from_dict(json.loads(json.dumps(plan.sandbox)))
        every = (*desc.binds, *desc.extra_binds)
        assert every and all(b.root != render.ROOT_ABS for b in every), [b for b in every if b.root == render.ROOT_ABS]
        assert desc.agent == agent
        return plan

    call = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert call.security_context.placement.machine_id == "compute-1"
    plan = await _plan(call, SID)
    assert plan.config_dir_relative.startswith("externals/phone/302101234567/")
    wrapup_sid = str(uuid.uuid4())
    home = config.get_agent_dir(agent) / "externals" / "phone" / "302101234567"
    wrapup = await pcb.build_wrapup_agent_config(
        agent, {"chat_id": "chat-1", "identity_id": "", "home": str(home)}, wrapup_sid)
    assert wrapup.security_context.placement.machine_id == "compute-1"
    plan = await _plan(wrapup, wrapup_sid)
    assert plan.config_dir_relative.startswith("externals/phone/_ephemeral/")


@pytest.mark.asyncio
async def test_an_outside_callers_place_is_resolved_at_the_external_role(agent, stubs, monkeypatch):
    """The resolver keeps a session with no person below the owner tier off
    every admin's machine, whatever that machine's switch says. An outside
    caller's build asks it exactly so: the external role, nobody."""
    asked: list[tuple] = []

    def _resolve(agent_name, **kw):
        asked.append((agent_name, kw))
        return "local"
    monkeypatch.setattr(pcb, "resolve_phone_execution_target", _resolve)
    await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert asked == [(agent, {"role": "external"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli", "direct-llm"])
async def test_a_call_with_no_compute_machine_is_refused(agent, stubs, monkeypatch, engine):
    """Nothing serves the platform's place: the platform runs no call itself,
    so the build is refused with the sentence every door shows. It asked for
    a machine that shares the platform's folders, as a call always does."""
    from storage.agents import agent_store
    agent_store.update_agent(agent, execution_path=engine)
    asked = _serving(monkeypatch, None)
    with pytest.raises(placement.NoComputeMachine) as refused:
        await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
    assert asked == [(engine, {"prefer": "", "shared_folder": True})]


@pytest.mark.asyncio
async def test_no_identity_is_the_shared_external_default(agent, stubs):
    cfg = await pcb.build_phone_agent_config(agent)
    ctx = cfg.security_context
    assert ctx.principal == "external" and ctx.role == "external"
    assert ctx.external_claim == "phone:" and ctx.external_home == ""


@pytest.mark.asyncio
async def test_user_mode_delegates_to_the_dashboard_builder(agent, stubs, monkeypatch):
    from core.config import config_builder
    captured: dict = {}

    async def _fake_build(agent_name, user, user_sub, user_role, **kw):
        captured.update(agent=agent_name, user=user, user_sub=user_sub, role=user_role, **kw)
        return AgentConfig(agent_name=agent_name, system_prompt="BASE", client_type="phone",
                           permission_mode="auto", sandbox_host_claude_dir="/x")
    monkeypatch.setattr(config_builder, "build_agent_config", _fake_build)

    user = {"sub": "user-1", "username": "alice", "role": "member"}
    identity = RouteIdentity(mode="user", role="viewer", external=None, user=user,
                             user_role="editor", caller_claim="phone:+3021")
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=identity, session_id=SID, call_type="inbound",
        phone_context_override="Be brief.", trigger_payload={"phone": "+3021"},
    )
    assert captured["agent"] == agent and captured["user_sub"] == "user-1"
    assert captured["role"] == "editor"
    assert captured["client_type"] == "phone" and captured["permission_mode"] == "auto"
    assert captured["phone_mode"] is True and captured["subscription_pool_fallback"] is True
    assert captured["trigger_payload"] == {"phone": "+3021"}
    assert captured["external_claim"] == "phone:+3021"
    assert captured["session_id"] == SID
    assert cfg.system_prompt.startswith("BASE") and cfg.system_prompt.rstrip().endswith("Be brief.")


def test_execution_target_resolver_passes_role_and_user(monkeypatch):
    from services.remote import remote_status
    seen = {}
    monkeypatch.setattr(remote_status, "resolve_execution_target",
                        lambda agent, sub, role: (seen.update(sub=sub, role=role) or "local", None))
    assert pcb.resolve_phone_execution_target("a", role="manager", user_sub="u1") == "local"
    assert seen == {"sub": "u1", "role": "manager"}
    pcb.resolve_phone_execution_target("a")
    assert seen == {"sub": None, "role": "external"}


@pytest.mark.asyncio
async def test_pool_refusal_fails_the_build(agent, stubs, monkeypatch):
    """A pool cap refusal must reach the call handler (the warmup fails),
    never a spawn on whatever credential file the agent dir still holds."""
    from services.engines import subscription_pool

    def _refuse(*a, **k):
        raise subscription_pool.NoSubscriptionError("pool_cap")
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env", _refuse)
    with pytest.raises(subscription_pool.NoSubscriptionError):
        await pcb.build_phone_agent_config(
            agent, route_identity=_identity(agent), session_id=SID, call_type="inbound",
        )


@pytest.mark.asyncio
async def test_the_callers_config_dir_is_built_with_the_picks(agent, stubs, monkeypatch):
    """The outside session's config dir gets the picks, so the skills it
    materializes are the picked tools' alone."""
    seen: dict = {}

    def fake_dir(agent_name, **kw):
        seen.update(kw)
        d = config.get_agent_dir(agent_name) / "externals" / "phone" / "302101234567" / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        return d
    monkeypatch.setattr(pcb, "ensure_persistent_agent_dir", fake_dir)
    await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert seen["outside_picks"] is not None and seen["external_home"]
    assert seen["outside_picks"] is stubs["mcp_kwargs"]["outside_picks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli", "direct-llm"])
async def test_a_wrapup_runs_where_a_call_runs(agent, stubs, monkeypatch, engine):
    """On the compute machine that serves, one that shares the platform's
    folders (its scratch home and the person's tree are there), and the one
    its slot was taken on when the runner names it."""
    from storage.agents import agent_store
    agent_store.update_agent(agent, execution_path=engine)
    asked = _serving(monkeypatch, {"id": "compute-1"})
    cfg = await pcb.build_wrapup_agent_config(agent, {"chat_id": "chat-1"}, SID, prefer="compute-1")
    assert asked == [(engine, {"shared_folder": True, "prefer": "compute-1"})]
    assert cfg.security_context.placement.machine_id == "compute-1"
    assert placement.is_local(cfg.execution_target)


@pytest.mark.asyncio
async def test_a_wrapup_task_hands_its_slots_machine_to_its_build(monkeypatch):
    """The runner takes a wrap-up's slot on a machine and names it to the
    task's build: the build of a wrap-up hands it on to the wrap-up's own, so
    the build and the slot agree."""
    from core.config import task_config_builder
    built: list[dict] = []

    async def _wrapup(agent_name, wrapup, session_id, *, model="", prefer=""):
        built.append({"agent": agent_name, "wrapup": wrapup, "prefer": prefer})
        return "the wrap-up's config"
    monkeypatch.setattr(pcb, "build_wrapup_agent_config", _wrapup)
    task = SimpleNamespace(wrapup={"chat_id": "chat-1"})
    cfg = await task_config_builder.build_task_agent_config("unified", task, SID, prefer="compute-2")
    assert cfg == "the wrap-up's config"
    assert built == [{"agent": "unified", "wrapup": {"chat_id": "chat-1"}, "prefer": "compute-2"}]


@pytest.mark.asyncio
async def test_a_wrapup_with_no_compute_machine_is_refused(agent, stubs, monkeypatch):
    """A wrap-up is the platform's own run: with no compute machine to take
    it the build is refused with the sentence, never run by the platform."""
    asked = _serving(monkeypatch, None)
    with pytest.raises(placement.NoComputeMachine) as refused:
        await pcb.build_wrapup_agent_config(agent, {"chat_id": "chat-1"}, SID)
    assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
    assert asked == [("claude-code-cli", {"prefer": "", "shared_folder": True})]


@pytest.mark.asyncio
async def test_the_wrapup_builds_its_own_profile(agent, stubs):
    """The wrap-up after a conversation (``build_wrapup_agent_config``): the
    agent's business tools through its own rule with the record tool, agent
    scope, no shell, no web, the person's tree and identity on the context,
    a token whose only grant is that person, on the platform, never a
    terminal; without a person no memory and no grant."""
    from auth.session_token import validate_session_token
    from services.mcp import mcp_session_filter as rule
    from storage.identity import external_identity_store
    home = config.get_agent_dir(agent) / "externals" / "phone" / "302101234567"
    home.mkdir(parents=True)
    ident = external_identity_store.touch(agent, "phone", "+302101234567", "302101234567")
    cfg = await pcb.build_wrapup_agent_config(
        agent, {"chat_id": "chat-1", "identity_id": ident, "home": str(home)}, SID)
    kw = stubs["mcp_kwargs"]
    assert kw["kind_rule"] is rule.WRAPUP_RULE and kw["task_scope"] == "agent"
    assert kw["extra_mcps"] == [rule.CONVERSATIONS_MCP] and not kw.get("external")
    ctx = cfg.security_context
    assert ctx.no_shell and ctx.no_web and ctx.wrapup == "chat-1"
    assert ctx.wrapup_home == str(home) and ctx.subject_id == ident
    assert ctx.role == "manager" and ctx.config_visible is False and not ctx.is_external
    claims = validate_session_token(cfg.credential_env["PROXY_API_KEY"])
    assert claims["subj"] == ident and not claims.get("ext") and not claims.get("user_sub")
    assert cfg.execution_target == "local" and cfg.permission_mode == "auto" and not cfg.interactive
    assert "# After the conversation" in cfg.system_prompt and "`/caller/workspace/`" in cfg.system_prompt
    assert "/externals/phone/_ephemeral/" in cfg.sandbox_host_claude_dir

    alone = await pcb.build_wrapup_agent_config(
        agent, {"chat_id": "chat-2", "identity_id": ident, "home": ""}, str(uuid.uuid4()))
    assert "memory-mcp" in stubs["mcp_kwargs"]["kind_rule"].denied
    assert alone.security_context.subject_id == "" and alone.security_context.wrapup_home == ""
    assert "subj" not in validate_session_token(alone.credential_env["PROXY_API_KEY"])


@pytest.mark.asyncio
async def test_a_picked_app_rides_the_call_with_its_grant_env_and_prompt(agent, stubs, monkeypatch):
    """An app a manager opened to external identities: the session's
    start-time picks name it, the apps tool is granted every function, its
    env maps the app's name to its id, and the prompt names the app and the
    methods it opens. With no app picked none of it."""
    import json

    from services.mcp import mcp_registry, outside_picks
    from storage import database as task_store
    from storage.agents import outside_store
    manifest = SimpleNamespace(name=outside_picks.APPS_MCP, server_name=outside_picks.APPS_SERVER)
    monkeypatch.setattr(mcp_registry, "get_manifest",
                        lambda name: manifest if name == outside_picks.APPS_MCP else None)
    cfg = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert cfg.security_context.outside_apps == ()
    assert outside_picks.APPS_ENV not in (cfg.credential_env or {})
    assert "Apps you can use" not in cfg.system_prompt
    exports = {"methods": {"book": {"description": "Book a free time.", "min_role": "external"},
                           "admin": {"description": "Every booking", "min_role": "editor"}}}
    row = task_store.upsert_app(agent, "", None, "booking", title="Booking", rel_path="workspace/apps/booking",
                                kind="folder", blocks={"exports": json.dumps(exports)})
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "u")
    outside_store.update(agent, add_apps=[{"app_id": row["id"], "methods": ["book"]}])
    cfg = await pcb.build_phone_agent_config(agent, route_identity=_identity(agent), session_id=SID)
    assert cfg.security_context.outside_apps == (row["id"],)
    assert (outside_picks.APPS_SERVER, None) in cfg.security_context.outside_grants
    assert json.loads(cfg.credential_env[outside_picks.APPS_ENV]) == {"booking": row["id"]}
    assert "Apps you can use for this person" in cfg.system_prompt
    assert "`booking` (Booking). `book`: Book a free time." in cfg.system_prompt
    assert "admin" not in cfg.system_prompt.split("Apps you can use for this person", 1)[1]
    assert outside_picks.APPS_MCP in stubs["mcp_kwargs"]["outside_picks"].channel


def test_the_session_build_forces_the_apps_tool_only_with_an_app_picked(monkeypatch):
    from services.mcp import mcp_session_config, outside_picks
    forced: list = []

    def _force(assigned, extra_mcps, reasons, placement_):
        forced.append(list(extra_mcps or []))
    monkeypatch.setattr(mcp_session_config, "_force_include", _force)
    monkeypatch.setattr(mcp_session_config, "_get_agent_mcps_with_device_exclusions", lambda *a, **k: ([], {}))
    for picks in (outside_picks.OutsidePicks(),
                  outside_picks.OutsidePicks(apps=("a1",), channel=frozenset({outside_picks.APPS_MCP}))):
        mcp_session_config.build_session_mcp_config("support", None, external=True, outside_picks=picks,
                                                    placement=placement.LOCAL_PLACEMENT)
    assert forced == [[], [outside_picks.APPS_MCP]]
