"""The route identity "the agent itself" (S-F13): a call that runs as the
agent's own session (agent scope, its shared space and service accounts, no
per-caller folder), with the management tools off but those the route ticks
and no shell; the acknowledgement of no PIN; refused where the agent has no
shared space. And a route tied to a person of any role, but below the
editor role on a Shared-only agent.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api.phone import phone as phone_api
from core.config import phone_config_builder as pcb
from services.mcp import mcp_session_filter as rule
from services.phone import phone_identity as pi
from storage.phone import phone_route_store
from tests.fixtures.start_text import prompt_at_start
from tests.phone.test_phone_identity_resolution import _agent, _resolve, _route, _user


def test_the_agent_line_resolves_as_the_agent_itself(temp_db):
    agent = _agent()
    route = _route(agent, identity_mode="agent", line_tools=["schedules-mcp"])
    assert phone_route_store.get_route(route["id"])["identity_mode"] == "agent"
    ident = _resolve(route, agent)
    assert ident.mode == pi.IDENTITY_MODE_AGENT and ident.external is None
    assert ident.user is None and ident.user_role == "manager"
    assert ident.line_tools == ("schedules-mcp",) and ident.label == "agent"
    assert ident.caller_claim == "phone:+302101234567"
    assert pi.pin_allowed({**route, "direction": "outbound"})


def test_a_personal_only_agent_has_no_agent_line(temp_db):
    agent = _agent(collaborative=False, default_scope="user")
    ident = _resolve(_route(agent, identity_mode="agent"), agent)
    assert ident.mode == "caller" and ident.fallback_reason
    with pytest.raises(HTTPException) as exc:
        phone_api._validate_route_identity({"identity_mode": "agent", "agent": agent})
    assert exc.value.status_code == 400


def test_the_agent_line_ticks_only_what_it_may():
    offered = phone_api.line_tools_offered()
    assert "phone-mcp" not in offered and "computer-control" not in offered
    assert set(offered) <= rule.AGENT_LINE_TICKABLE


def test_an_unknown_tick_is_refused(temp_db):
    agent = _agent()
    with pytest.raises(HTTPException) as exc:
        phone_api._validate_route_identity(
            {"identity_mode": "agent", "agent": agent, "line_tools": ["phone-mcp"]})
    assert exc.value.status_code == 400


def test_no_pin_on_the_agent_line_takes_the_acknowledgement():
    route = {"identity_mode": "agent", "direction": "inbound"}
    with pytest.raises(HTTPException) as exc:
        phone_api._require_no_pin_acknowledged(route, has_pin=False, acknowledged=False)
    assert "acts as the agent itself" in exc.value.detail
    for reach in ("change its shared files", "its tools", "its accounts", "the tools you ticked"):
        assert reach in exc.value.detail
    assert "memory" not in exc.value.detail
    phone_api._require_no_pin_acknowledged(route, has_pin=False, acknowledged=True)
    phone_api._require_no_pin_acknowledged(route, has_pin=True, acknowledged=False)
    phone_api._require_no_pin_acknowledged({**route, "direction": "outbound"},
                                           has_pin=False, acknowledged=False)


def test_a_route_tied_to_a_viewer_is_refused_on_a_shared_only_agent(temp_db):
    shared = _agent(collaborative=False, default_scope="agent")
    viewer = _user(agents=[shared], agent_role="viewer")
    editor = _user(agents=[shared], agent_role="editor")
    with pytest.raises(HTTPException) as exc:
        phone_api._validate_route_identity(
            {"identity_mode": "user", "agent": shared, "identity_user_sub": viewer})
    assert "Shared-only" in exc.value.detail
    phone_api._validate_route_identity({"identity_mode": "user", "agent": shared, "identity_user_sub": editor})
    collaborative = _agent()
    viewer_here = _user(agents=[collaborative], agent_role="viewer")
    phone_api._validate_route_identity(
        {"identity_mode": "user", "agent": collaborative, "identity_user_sub": viewer_here})


def test_the_agent_lines_session_is_the_agents_with_its_rule_and_no_shell(monkeypatch):
    """The builder hands the dashboard builder an agent-scope identity, the
    line's rule with the route's ticks, and no shell."""
    from core.config import config_builder
    seen: dict = {}

    async def _build(agent_name, user, user_sub, user_role, **kw):
        seen.update(kw, user_sub=user_sub, user_role=user_role)
        return pcb.AgentConfig(agent_name=agent_name, system_prompt="p", mcp_config_path="")
    monkeypatch.setattr(config_builder, "build_agent_config", _build)
    from adapters.phone import PhoneAdapter
    monkeypatch.setattr(PhoneAdapter, "get_phone_context", staticmethod(lambda call_type: "call context"))
    ident = pi.RouteIdentity(mode=pi.IDENTITY_MODE_AGENT, role="external", external=None,
                             user_role="manager", caller_claim="phone:+30210",
                             line_tools=("schedules-mcp",))
    cfg = asyncio.run(pcb.build_phone_agent_config("a", route_identity=ident, session_id="s"))
    assert seen["user_sub"] == "" and seen["user_role"] == "manager"
    assert seen["task_identity"].scope == "agent" and seen["task_identity"].creds_user_sub is None
    assert seen["no_shell"] is True
    line = seen["kind_rule"]
    assert "schedules-mcp" not in line.denied and "delegation-mcp" in line.denied
    assert "computer-control" in line.denied and rule.CONVERSATIONS_MCP in line.denied
    assert "call context" in cfg.system_prompt


def test_an_installs_identity_check_takes_the_new_word_once(temp_db):
    """A 1.9.1 CREATE's CHECK knows three words: the boot widens it, and a
    second boot finds it widened."""
    from storage import schema
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("ALTER TABLE phone_routes DROP CONSTRAINT IF EXISTS phone_routes_identity_mode_check")
        conn.execute("ALTER TABLE phone_routes ADD CONSTRAINT phone_routes_identity_mode_check "
                     "CHECK (identity_mode IN ('caller', 'shared', 'user'))")
        schema._widen_identity_mode_check(conn)
        schema._widen_identity_mode_check(conn)
        conn.commit()
    agent = _agent()
    assert _route(agent, identity_mode="agent")["identity_mode"] == "agent"



def _line_claim(monkeypatch, ident) -> str:
    """The ``ext`` claim the builder hands the session's token."""
    from core.config import config_builder
    seen: dict = {}

    async def _build(agent_name, user, user_sub, user_role, **kw):
        seen.update(kw)
        return pcb.AgentConfig(agent_name=agent_name, system_prompt="p", mcp_config_path="")
    monkeypatch.setattr(config_builder, "build_agent_config", _build)
    from adapters.phone import PhoneAdapter
    monkeypatch.setattr(PhoneAdapter, "get_phone_context", staticmethod(lambda call_type: "call"))
    asyncio.run(pcb.build_phone_agent_config("a", route_identity=ident, session_id="s"))
    return seen["external_claim"]


def test_only_a_persons_route_keeps_the_callers_claim(monkeypatch):
    """A person's route keeps the claim for its audit trail (the token's
    person is the user); the agent's own line has no person to keep it for."""
    person = pi.RouteIdentity(mode=pi.IDENTITY_MODE_USER, role="external", external=None,
                              user={"sub": "u-1"}, user_role="editor", caller_claim="phone:+30210")
    assert _line_claim(monkeypatch, person) == "phone:+30210"
    line = pi.RouteIdentity(mode=pi.IDENTITY_MODE_AGENT, role="external", external=None,
                            user_role="manager", caller_claim="phone:+30210")
    assert _line_claim(monkeypatch, line) == ""


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/tasks/one-time"),          # the schedules tool, ticked on the route
    ("POST", "/v1/internal/memory/op"),      # the memory tool, ticked on the route
    ("POST", "/v1/hooks/file"),              # file-tools, a business tool the line keeps
    ("POST", "/v1/mcp-gateway/github/mcp"),  # a service account's server
])
def test_the_agent_lines_token_reaches_its_tools(method, path, monkeypatch):
    """The token the line's builder mints reaches what its tools call, as any
    agent-scope session's does. With an outside caller's ``ext`` claim the
    middleware confined it to a caller's routes and every tool answered 403."""
    from starlette.requests import Request

    import middleware
    from auth import roles
    from auth.path_policy import SecurityContext
    from auth.providers import _parse_external_claim
    from auth.session_token import create_session_token
    from core.session import session_state
    claim = _line_claim(monkeypatch, pi.RouteIdentity(
        mode=pi.IDENTITY_MODE_AGENT, role="external", external=None, user_role="manager",
        caller_claim="phone:+302101234567", line_tools=("schedules-mcp", "memory-mcp")))
    ctx = SecurityContext(role=roles.MANAGER, username="", agent="a", is_admin_agent=False,
                          session_scope="agent", no_shell=True, external_claim=claim)
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr(session_state, "session_token_refusal", lambda claims: "")
    monkeypatch.setattr(middleware, "off_session_listener", lambda scope: False)
    token = create_session_token("sid-line", "a", "", external=claim, subject="")
    request = Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                       "query_string": b"", "client": ("127.0.0.1", 9), "server": ("127.0.0.1", 8401),
                       "headers": [(b"authorization", f"Bearer {token}".encode())]})
    refused = middleware._session_refusal(request)
    assert refused is None, f"{method} {path} answered {refused.status_code}"
    assert _parse_external_claim(claim) == {}, "the line's principal is not an outside caller"


def test_a_stranger_on_the_agents_line_hears_no_notebook_and_no_memory(temp_db, tmp_path, monkeypatch):
    """Anyone who dials the agent's own line hears what its prompt carries:
    never the agent's Notebook, never its shared memory, PIN or no PIN. A
    member's chat with the same agent carries both."""
    import uuid

    import config as app_config
    from auth import roles
    from core.session import session_kind
    from storage.agents import agent_store
    from storage.notebook import db_notebook
    monkeypatch.setattr(app_config, "AGENTS_DIR", tmp_path / "agents")
    agent_store.create_agent("agent-line", "Line", collaborative=True, default_scope="user")
    agent_dir = app_config.get_agent_dir("agent-line")
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("# Line\\n\\nAn agent.\\n")
    (agent_dir / "knowledge" / "memory").mkdir(parents=True, exist_ok=True)
    (agent_dir / "knowledge" / "memory" / "deals.md").write_text(
        "# Deals\\n\\nThe price floor for Acme is 40 euros.\\n")
    db_notebook.insert_entry(str(uuid.uuid4()), "agent-line", None, "chat",
                             "Internal: the bank talks stay private.", max_per_hour=100)
    line = prompt_at_start(
        "agent-line", username=None, role=roles.MANAGER, client_type=session_kind.PHONE.name,
        kind_rule=rule.agent_line_rule(("memory-mcp",)))
    assert "bank talks" not in line and "price floor" not in line
    member = prompt_at_start(
        "agent-line", username=None, role=roles.MANAGER, client_type=session_kind.DASHBOARD.name)
    assert "bank talks" in member and "price floor" in member


# --- the line's own engine home, and what it never reaches -----------------------------

_STATE_PATHS = ("/workspace/.claude/projects/-workspace/earlier-call.jsonl",
                "/workspace/.codex/sessions/2026/11/rollout-earlier.jsonl")


@pytest.fixture
def line_tree(tmp_path, monkeypatch):
    import config as app_config
    from auth import path_policy
    agents = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    monkeypatch.setattr(path_policy, "_AGENTS_DIR", agents)
    agent_dir = agents / "agent-line"
    for sub in ("workspace/.claude/projects/-workspace", "workspace/.codex/sessions/2026/11",
                "knowledge/memory", "config"):
        (agent_dir / sub).mkdir(parents=True)
    (agent_dir / "workspace/.claude/projects/-workspace/earlier-call.jsonl").write_text("a caller's words\n")
    (agent_dir / "workspace/.codex/sessions/2026/11/rollout-earlier.jsonl").write_text("a task's words\n")
    (agent_dir / "knowledge/memory/deals.md").write_text("# Deals\n\nThe price floor is 40.\n")
    (agent_dir / "workspace/notes.md").write_text("shared\n")
    return agent_dir


def _line_ctx(agent_dir):
    from auth import roles
    from auth.path_policy import SecurityContext
    return SecurityContext(role=roles.MANAGER, username="", agent=agent_dir.name, is_admin_agent=False,
                           session_scope="agent", no_shell=True, open_line=True)


def test_the_line_never_reaches_earlier_transcripts_or_the_agents_memory(line_tree):
    """Anyone who dials the line acts as the agent: never the engine state every
    agent-scope session keeps (earlier calls', tasks' and workers' transcripts)
    nor the agent's shared memory, through its own tools or a Docker tool's
    path. The shared files stay the agent's to read and change."""
    from pathlib import Path

    from api.hooks import paths as hook_paths
    from auth.path_policy import check_host_path_access, check_tool_access
    ctx = _line_ctx(line_tree)
    refused = (*_STATE_PATHS, "/knowledge/memory/deals.md")
    for path in refused:
        for tool, args, writing in (("Read", {"file_path": path}, False),
                                    ("Write", {"file_path": path, "content": "x"}, True)):
            decision, _plan = check_tool_access(tool, args, ctx)
            assert not decision.allowed, (tool, path)
            host = Path(hook_paths._sandbox_to_host(path, ctx, line_tree)).resolve()
            assert not check_host_path_access(host, ctx, writing=writing).allowed, ("docker", tool, path)
    for tool, args in (("Read", {"file_path": "/workspace/notes.md"}),
                       ("Write", {"file_path": "/workspace/notes.md", "content": "x"}),
                       ("Read", {"file_path": "/knowledge/faq.md"})):
        decision, _plan = check_tool_access(tool, args, ctx)
        assert decision.allowed, (tool, args, decision.reason)


def test_the_lines_sandbox_runs_from_a_scratch_engine_home(line_tree, tmp_path):
    """The line's engine state is its own scratch home at /caller, the agent's
    own engine folders and its memory masked out of the shared mounts."""
    from auth import roles
    from core.sandbox.sandbox import (
        EXTERNAL_SANDBOX_HOME,
        SandboxBuilder,
        SandboxConfig,
    )
    scratch = line_tree / "externals" / "phone" / "_ephemeral" / "sid-line"
    (scratch / ".claude").mkdir(parents=True)
    builder = SandboxBuilder(SandboxConfig(
        role=roles.MANAGER, username="", agent_name=line_tree.name, is_admin_agent=False,
        host_agents_dir=line_tree.parent, host_mcps_dir=line_tree.parent,
        host_claude_dir=scratch / ".claude", config_visible=False, net_forwards=["8400"],
        engine_home=str(scratch)))
    mounts = {m.sandbox: m for m in builder.workspace_mount_table()}
    assert mounts["/workspace"].rw
    assert mounts[f"{EXTERNAL_SANDBOX_HOME}/.claude"].host == str(scratch / ".claude")
    assert mounts[f"{EXTERNAL_SANDBOX_HOME}/.claude"].rw
    for masked in ("/workspace/.claude", "/workspace/.codex", "/knowledge/memory"):
        assert not mounts[masked].host.startswith(str(line_tree)), masked
    assert builder.get_cwd() == "/workspace"
    assert builder.get_env_overrides()["CLAUDE_CONFIG_DIR"] == f"{EXTERNAL_SANDBOX_HOME}/.claude"


def test_the_line_builds_its_config_dir_in_a_scratch_home(temp_db, monkeypatch, tmp_path):
    """The line's builder asks for a scratch home keyed on the call, never the
    agent scope's own engine folder, and its context names it the line."""
    from tests._compute import serve
    from tests.fixtures.loop_builder_rig import AGENT, install, run
    rec = install(monkeypatch, tmp_path)
    # A compute machine serves the platform's place: the line's call is built on it.
    serve(monkeypatch)
    ident = pi.RouteIdentity(mode=pi.IDENTITY_MODE_AGENT, role="external", external=None,
                             user_role="manager", caller_claim="phone:+30210")
    sid = "4f1d2c3b-0000-4000-8000-00000000c499"
    cfg = run(pcb.build_phone_agent_config(AGENT, route_identity=ident, session_id=sid))
    (call,) = [c for c in rec.calls if c["call"] == "ensure_persistent_agent_dir"]
    assert call["kwargs"]["external_home"].endswith(f"externals/phone/_ephemeral/{sid}")
    assert call["kwargs"]["kind_rule"].open_line
    assert cfg.security_context.open_line


def test_the_lines_scratch_home_is_made_off_the_loop(temp_db, monkeypatch, tmp_path):
    """The scratch home's folder is made in the builder's worker thread, never
    as an argument evaluated on the loop before the hop."""
    import threading

    from core.session import external_identity
    from tests._compute import serve
    from tests.fixtures.loop_builder_rig import AGENT, install, run
    install(monkeypatch, tmp_path)
    serve(monkeypatch)
    loop_thread = threading.get_ident()
    made_on: list[int] = []
    real = external_identity.scratch_home

    def recording(*args, **kwargs):
        made_on.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(external_identity, "scratch_home", recording)
    ident = pi.RouteIdentity(mode=pi.IDENTITY_MODE_AGENT, role="external", external=None,
                             user_role="manager", caller_claim="phone:+30210")
    run(pcb.build_phone_agent_config(AGENT, route_identity=ident,
                                     session_id="4f1d2c3b-0000-4000-8000-00000000c500"))
    assert made_on and loop_thread not in made_on


def test_the_line_has_no_memory_tool_and_the_memory_api_refuses_it(temp_db, monkeypatch):
    """The agent's memory is not open on its phone line at all: never a tick,
    and the memory API answers the line's token with a sentence."""
    from api.agent_data import memory as memory_api
    from auth.path_policy import SecurityContext
    from auth.providers import UserContext
    from core.session import session_state
    assert "memory-mcp" not in rule.AGENT_LINE_TICKABLE
    assert "memory-mcp" in rule.agent_line_rule(["memory-mcp"]).denied
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: SecurityContext(
        role="manager", username="", agent="a", is_admin_agent=False, open_line=True))
    u = UserContext(sub="session:sid-line", email="", name="", role="service", is_api_key=True,
                    session_id="sid-line", agent="a")
    got = asyncio.run(memory_api._session_scopes(u, "a", __import__("pathlib").Path("/x"), None,
                                                 {"agent_memory_enabled": True}, {"agent_memory_enabled": True},
                                                 wrapup_chat="", line=memory_api._line_of(u)))
    assert isinstance(got, str) and "phone line" in got
