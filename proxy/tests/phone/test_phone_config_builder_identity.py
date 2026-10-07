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
from core.config import phone_config_builder as pcb
from core.execution_layer import AgentConfig
from core.session import external_identity
from services.phone.phone_identity import RouteIdentity
from core import placement

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
    from services.mcp import dynamic_context, mcp_registry
    from services.engines import subscription_pool
    from storage import remote_store
    seen: dict = {}
    monkeypatch.setattr(config, "get_cli_model", lambda name: "model-x")

    def _build(agent_name, user_sub, **kw):
        seen["mcp_kwargs"] = kw
        return None, {}, {"display-mcp": "Excluded in phone mode"}, {}, set()
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _build)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [
        SimpleNamespace(name="memory-mcp"), SimpleNamespace(name="display-mcp"),
    ])

    async def _dyn(agent_name, names, **kw):
        seen["dynamic_names"] = list(names)
        seen["dynamic_kwargs"] = kw
        return []
    monkeypatch.setattr(dynamic_context, "get_dynamic_contexts", _dyn)
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-1", {"_PROVIDER": "anthropic"}))
    monkeypatch.setattr(remote_store, "resolve_execution_target",
                        lambda agent, sub, role: (seen.setdefault("target", "local"), None))
    monkeypatch.setattr(remote_store, "placement_of",
                        lambda value, sub, agent: (
                            placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE)
                            if not placement.is_local(value) else placement.LOCAL_PLACEMENT))
    monkeypatch.setattr(remote_store, "get_target_browser_settings", lambda p: None)
    return seen


def _identity(agent, *, mode="caller", role="viewer", phone="+302101234567", **kw):
    ident = external_identity.resolve(
        external_identity.PHONE, phone, session_id=SID,
        shared=(mode == "shared"), **kw,
    )
    return RouteIdentity(mode=mode, role=role, external=ident)


@pytest.mark.asyncio
async def test_caller_mode_builds_the_caller_tree(agent, stubs):
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    ctx = cfg.security_context
    home = config.get_agent_dir(agent) / "externals" / "phone" / "302101234567"
    assert ctx.principal == "external" and ctx.role == "viewer"
    assert ctx.external_home == str(home) and ctx.external_claim == "phone:+302101234567"
    assert ctx.session_scope == "agent" and ctx.config_visible is False
    assert cfg.sandbox_host_claude_dir == str(home / ".claude")
    assert (home / ".claude" / "settings.json").is_file()
    assert cfg.client_type == "phone" and cfg.permission_mode == "auto"
    assert stubs["mcp_kwargs"]["external"] is True and stubs["mcp_kwargs"]["phone_mode"] is True
    # The caller is a viewer: an MCP or skill above that audience stays out.
    assert stubs["mcp_kwargs"]["user_role"] == "viewer" == stubs["dynamic_kwargs"]["user_role"]
    # Only attached MCPs describe themselves.
    assert stubs["dynamic_names"] == ["memory-mcp"]
    assert "external caller" in cfg.system_prompt and "/caller/workspace/" in cfg.system_prompt
    # The caller's private memory section is always announced (empty or not)
    # so the agent knows where this caller's notes live.
    assert "## Caller memory (private to this caller)" in cfg.system_prompt
    assert "## User memory" not in cfg.system_prompt
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

    def _skills(model, *, context="", placement=None, skip_http_mcps=False,
                external=False, user_role=None):
        seen["user_role"] = user_role
        out = [("everyone", "EVERYONE-SKILL-BODY", "always")]
        if mcp_registry.skill_audience_admits(SimpleNamespace(audience="editor"), user_role):
            out.append(("editors", "EDITOR-ONLY-SKILL-BODY", "always"))
        return out
    monkeypatch.setattr(mcp_registry, "get_skills_for_agent", _skills)
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent), session_id=SID,
    )
    assert seen["user_role"] == "viewer"
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
    assert ctx.role == "viewer" and ctx.knowledge_rw is False and ctx.config_visible is False
    assert "`/knowledge/` (RW)" not in cfg.system_prompt and "/config" not in cfg.system_prompt


@pytest.mark.asyncio
async def test_shared_mode_has_no_tree(agent, stubs):
    cfg = await pcb.build_phone_agent_config(
        agent, route_identity=_identity(agent, mode="shared"), session_id=SID,
    )
    ctx = cfg.security_context
    assert ctx.principal == "external" and ctx.external_home == ""
    assert ctx.external_claim == "phone:"
    assert cfg.sandbox_host_claude_dir.endswith("/workspace/.claude")
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
async def test_no_identity_is_the_shared_external_default(agent, stubs):
    cfg = await pcb.build_phone_agent_config(agent)
    ctx = cfg.security_context
    assert ctx.principal == "external" and ctx.role == "viewer"
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
    from storage import remote_store
    seen = {}
    monkeypatch.setattr(remote_store, "resolve_execution_target",
                        lambda agent, sub, role: (seen.update(sub=sub, role=role) or "local", None))
    assert pcb.resolve_phone_execution_target("a", role="manager", user_sub="u1") == "local"
    assert seen == {"sub": "u1", "role": "manager"}
    pcb.resolve_phone_execution_target("a")
    assert seen == {"sub": None, "role": "viewer"}


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
