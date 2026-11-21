"""What a task's session reaches of the routes agent-config-mcp's tools call
(``mcps/custom/agent-config-mcp/server.py``): a session token of an
agent-scope run (no person behind it, the service principal) and one of a
user-scope run (its creator, a manager), each sent to every route.

A real run's context carries the run's own pair on the self-config tool
(``tool_limits.TASK_SESSION_LIMIT``: ``reload_session`` and
``switch_account``), and the route wall reads it (``ROUTE_TOOLS``,
``middleware._session_refusal``): ``run_rig`` pins that, the limit's
sentence on every route but the reload's and the two lists a creator tool
shares. ``rig`` registers the context with no pair, which no real run has:
it pins the routes' own gates, what a token reaches once the wall lets it
through (a chat's shell on an agent with no limit)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from core.session import session_kind, session_state
from services.mcp import mcp_manifest_store
from services.mcp.mcp_manifest_types import CredentialConfig, McpManifest, ServerConfig
from storage import database as task_store
from storage.agents import agent_store
from tests.conftest import live_session_token

AGENT = "task-session-agent"
MANAGER = "user-manager-ts"
RUN_SID = "sid-agent-run-1"
USER_SID = "sid-user-run-1"


def _mk(name):
    return McpManifest(
        name=name, label=name.title(), description=f"{name}.", version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={}, exclude_from=[], skills=[],
        mcp_dir=Path(config.MCPS_DIR) / "custom" / name,
    )


def _rig(monkeypatch, tmp_path, limits: tuple):
    from app import app
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    agent_store.create_agent(AGENT, "Task Session")
    (tmp_path / "agents" / AGENT / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / AGENT / "config" / "agent.md").write_text("# Soul\n")
    task_store.upsert_user(MANAGER, "m@t.io", "manager-ts", "member", display_name="Mia")
    task_store.set_user_agents(MANAGER, [AGENT], MANAGER, agent_roles={AGENT: "manager"})
    enabled = [_mk("crm")]
    monkeypatch.setattr("api.agents.tool_limits.mcp_registry.get_agent_mcps_all_placements",
                        lambda name: list(enabled) if name == AGENT else [])
    monkeypatch.setattr("api.agents.outside.mcp_registry.get_agent_mcps_all_placements",
                        lambda name: list(enabled) if name == AGENT else [])
    monkeypatch.setattr(mcp_manifest_store, "_manifests", {m.name: m for m in enabled})
    # The two runs' chats and sessions: a task's chat of each scope, its
    # session marked a task session.
    for sid, owner, scope in ((RUN_SID, "", "agent"), (USER_SID, MANAGER, "user")):
        run_id = f"run-{sid}"
        cid = session_kind.task_chat_id(run_id)
        task_store.create_chat(cid, owner or f"agent::{AGENT}", AGENT, "default", source_type="task")
        task_store.update_chat(cid, session_id=sid)
        task_store.create_run(run_id, f"task-{sid}", AGENT, "manual", None, "go", "scheduled", scope,
                              owner or None)
        task_store.update_run(run_id, session_id=sid, chat_id=cid)
        session_state._record_session_use(sid, client_type=session_kind.TASK.name, agent=AGENT)
        # The run's registered context, as a real run has one: the agent's
        # own run mounts the agent scope with its config closed.
        from auth.path_policy import SecurityContext
        session_state.register_session_state(sid, "auto", SecurityContext(
            role="manager", username="" if scope == "agent" else "manager-ts", agent=AGENT,
            is_admin_agent=False, session_scope=scope, config_visible=scope == "user",
            tool_limits=limits))
    yield TestClient(app)
    for sid in (RUN_SID, USER_SID):
        session_state.cleanup_session_permission_state(sid)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    """The routes' own gates: a context with no pair, which no real run has."""
    yield from _rig(monkeypatch, tmp_path, ())


@pytest.fixture
def run_rig(temp_db, monkeypatch, tmp_path):
    """A real run's context: the run's own pair on the self-config tool."""
    from services.mcp import tool_limits
    yield from _rig(monkeypatch, tmp_path, tool_limits.TASK_SESSION_LIMIT)


def _headers(sid: str, person: str = "") -> dict[str, str]:
    return {"Authorization": f"Bearer {live_session_token(sid, AGENT, person)}"}


# Every route the MCP's tools call, in the order of ``server.py``.
ROUTES = [
    ("GET", f"/v1/agents/{AGENT}/info", None),
    ("PATCH", f"/v1/agents/{AGENT}", {"description": "changed by a run"}),
    ("GET", f"/v1/agents/{AGENT}/files", None),
    ("PUT", f"/v1/agents/{AGENT}/files/config/agent.md", {"content": "# Rewritten by a run\n"}),
    ("GET", f"/v1/agents/{AGENT}/outside", None),
    # A widening: the route reads no attachment, so a manager's run widens.
    ("PUT", f"/v1/agents/{AGENT}/outside", {"tools": [{"mcp": "crm", "access": "read_write"}]}),
    ("GET", f"/v1/agents/{AGENT}/tool-limits", None),
    ("PUT", f"/v1/agents/{AGENT}/tool-limits", {"tools": [{"mcp": "crm", "runs": {"access": "read_only"}}]}),
    ("POST", f"/v1/agents/{AGENT}/complete-setup", {"scope": "agent"}),
    ("POST", f"/v1/agents/{AGENT}/complete-setup", {"scope": "user"}),
    ("GET", f"/v1/agents/{AGENT}/knowledge-attachments", None),
    ("PUT", f"/v1/agents/{AGENT}/knowledge-attachments", {"source_agent": "other", "folder": ""}),
    ("DELETE", f"/v1/agents/{AGENT}/knowledge-attachments/other", None),
    ("PUT", f"/v1/agents/{AGENT}/knowledge-library", {"enabled": True}),
    ("GET", f"/v1/internal/memory/agent-settings/{AGENT}", None),
    ("PATCH", f"/v1/internal/memory/agent-settings/{AGENT}", {"key": "agent_memory_enabled", "value": True}),
    ("GET", "/v1/departments", None),
    ("GET", "/v1/knowledge-libraries", None),
    ("GET", "/v1/execution-layers", None),
    ("POST", f"/v1/agents/{AGENT}/reload-session", {"reason": "a test"}),
]


def _matrix(client, headers) -> dict[str, int]:
    out = {}
    for method, path, body in ROUTES:
        r = client.request(method, path, json=body, headers=headers)
        out[f"{method} {path.replace(AGENT, 'A')}" + (f" {body.get('scope')}" if body and "scope" in body else "")] = r.status_code
    return out


def _wall(client, headers) -> dict[str, tuple[int, str]]:
    """Per route: the status and, for the wall's refusal, the function the
    sentence names ("" for the route's own answer)."""
    import re
    out = {}
    for method, path, body in ROUTES:
        r = client.request(method, path, json=body, headers=headers)
        detail = (r.json() or {}).get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        m = re.match(r"agent-config-mcp is limited for this agent: it may not use (\w+)\. A manager can change this in "
                     r"Agent Settings, Configuration, What the agent may use\.$", str(detail))
        key = f"{method} {path.replace(AGENT, 'A')}" + (f" {body.get('scope')}" if body and "scope" in body else "")
        out[key] = (r.status_code, m.group(1) if m else "")
    return out


@pytest.mark.parametrize("who", ["agent", "person"])
def test_a_runs_token_meets_the_wall_on_every_self_config_route_but_the_reload(run_rig, who):
    """A real run, its own or a person's: the wall answers the limit's
    sentence naming the first tool of each route, before the route's own
    gate; the reload route (the two tools the run keeps) and the two lists a
    creator tool shares answer themselves."""
    headers = _headers(RUN_SID) if who == "agent" else _headers(USER_SID, MANAGER)
    got = _wall(run_rig, headers)
    assert got == {
        "GET /v1/agents/A/info": (403, "get_agent_config"),
        "PATCH /v1/agents/A": (403, "update_display_name"),
        "GET /v1/agents/A/files": (403, "list_context_files"),
        "PUT /v1/agents/A/files/config/agent.md": (403, "update_persona"),
        "GET /v1/agents/A/outside": (403, "get_outside_people"),
        "PUT /v1/agents/A/outside": (403, "set_outside_people"),
        "GET /v1/agents/A/tool-limits": (403, "get_tool_limits"),
        "PUT /v1/agents/A/tool-limits": (403, "set_tool_limits"),
        "POST /v1/agents/A/complete-setup agent": (403, "complete_setup"),
        "POST /v1/agents/A/complete-setup user": (403, "complete_setup"),
        "GET /v1/agents/A/knowledge-attachments": (403, "list_knowledge_libraries"),
        "PUT /v1/agents/A/knowledge-attachments": (403, "attach_knowledge_library"),
        "DELETE /v1/agents/A/knowledge-attachments/other": (403, "detach_knowledge_library"),
        "PUT /v1/agents/A/knowledge-library": (403, "share_knowledge_folder"),
        "GET /v1/internal/memory/agent-settings/A": (403, "get_memory_settings"),
        "PATCH /v1/internal/memory/agent-settings/A": (403, "update_user_memory_enabled"),
        "GET /v1/departments": (200, ""),
        "GET /v1/knowledge-libraries": (403, "list_knowledge_libraries"),
        "GET /v1/execution-layers": (200, ""),
        "POST /v1/agents/A/reload-session": (202, ""),
    }


def test_a_run_with_no_person_reaches_the_reads_and_one_write(rig):
    """The service principal: refused every write a manager's row or a
    person gates, the persona's file too (a run's context shows it no
    ``config/``, so the session's own grant writes nothing there, while the
    files list answers); admitted to the agent's own setup mark
    (``complete-setup`` with ``scope=agent``, whose gate admits the agent
    principal on purpose), to four reads, and to the reload route as the
    run's own identity (its next round)."""
    got = _matrix(rig, _headers(RUN_SID))
    assert got == {
        "GET /v1/agents/A/info": 200,
        "PATCH /v1/agents/A": 403,
        "GET /v1/agents/A/files": 200,
        "PUT /v1/agents/A/files/config/agent.md": 403,
        "GET /v1/agents/A/outside": 403,
        "PUT /v1/agents/A/outside": 403,
        "GET /v1/agents/A/tool-limits": 403,
        "PUT /v1/agents/A/tool-limits": 403,
        "POST /v1/agents/A/complete-setup agent": 200,
        "POST /v1/agents/A/complete-setup user": 400,
        "GET /v1/agents/A/knowledge-attachments": 403,
        "PUT /v1/agents/A/knowledge-attachments": 403,
        "DELETE /v1/agents/A/knowledge-attachments/other": 403,
        "PUT /v1/agents/A/knowledge-library": 403,
        "GET /v1/internal/memory/agent-settings/A": 403,
        "PATCH /v1/internal/memory/agent-settings/A": 403,
        "GET /v1/departments": 200,
        "GET /v1/knowledge-libraries": 403,
        "GET /v1/execution-layers": 200,
        "POST /v1/agents/A/reload-session": 202,
    }


def test_a_managers_run_acts_as_its_creator(rig):
    """A user-scope run's token is its creator's (decision 4): the manager's
    routes answer as they would to the manager at the dashboard, the
    platform-role routes refuse a member."""
    got = _matrix(rig, _headers(USER_SID, MANAGER))
    assert got == {
        "GET /v1/agents/A/info": 200,
        "PATCH /v1/agents/A": 200,
        "GET /v1/agents/A/files": 200,
        "PUT /v1/agents/A/files/config/agent.md": 200,
        "GET /v1/agents/A/outside": 200,
        "PUT /v1/agents/A/outside": 200,
        "GET /v1/agents/A/tool-limits": 200,
        "PUT /v1/agents/A/tool-limits": 200,
        "POST /v1/agents/A/complete-setup agent": 200,
        "POST /v1/agents/A/complete-setup user": 200,
        "GET /v1/agents/A/knowledge-attachments": 200,
        "PUT /v1/agents/A/knowledge-attachments": 403,
        "DELETE /v1/agents/A/knowledge-attachments/other": 403,
        "PUT /v1/agents/A/knowledge-library": 403,
        "GET /v1/internal/memory/agent-settings/A": 200,
        "PATCH /v1/internal/memory/agent-settings/A": 200,
        "GET /v1/departments": 200,
        "GET /v1/knowledge-libraries": 403,
        "GET /v1/execution-layers": 200,
        "POST /v1/agents/A/reload-session": 202,
    }
