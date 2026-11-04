"""A chat lane a person delegated from their own chat is built with the
chat's tool context (``task_config_builder.attended_chat_worker``): the MCPs
whose manifests leave out ``task`` (the agent's settings, the agent creator,
the MCP catalog, location) load in it, the prompt lists them, and the
shared-chat context keeps the person's own settings tool out. Every other
run keeps the task context: a scheduled task, a task-surface worker, a lane
whose round a task, a call or another lane delegated, an agent-scope lane,
and a delegator below the editor tier."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.fixtures.loop_builder_rig import AGENT, install, run

PERSON = "user-viewer"   # an editor on AGENT (the rig)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


def _chat(chat_id: str, owner: str = PERSON, **fields) -> str:
    from storage import database as task_store
    task_store.create_chat(chat_id, owner, AGENT, "auto", **fields)
    return chat_id


def _lane(parent_chat_id: str | None, *, scope: str = "user", created_by: str = PERSON,
          task_type: str | None = None, target: str = "lane-chat"):
    from services.scheduler import task_kinds
    return SimpleNamespace(
        scope=scope, created_by=created_by, task_type=task_type or task_kinds.DELEGATE,
        target_chat_id=target, parent_chat_id=parent_chat_id, agent=AGENT,
        notification_mode="none", name="lane", id="dyn-lane",
        override_execution_path="claude-code-cli",
    )


def _attended(task) -> bool:
    from core.config.task_config_builder import attended_chat_worker
    return attended_chat_worker(task, PERSON if task.scope == "user" else None)


def test_a_lane_from_the_persons_own_chat_is_attended(rig):
    _chat("own-chat")
    assert _attended(_lane("own-chat"))


@pytest.mark.parametrize("case", [
    "task-chat", "phone-chat", "worker-chat", "another-persons-chat", "no-parent",
    "missing-parent", "agent-scope", "task-surface", "scheduled-task",
])
def test_every_other_round_keeps_the_task_context(rig, case):
    from services.scheduler import task_kinds
    _chat("own-chat")
    _chat("task-run1")
    _chat("phone-1", source_type="phone")
    _chat("lane-a", origin="delegated", delegate_role="worker", parent_chat_id="own-chat")
    _chat("max-chat", owner="user-manager")
    task = {
        "task-chat": lambda: _lane("task-run1"),
        "phone-chat": lambda: _lane("phone-1"),
        "worker-chat": lambda: _lane("lane-a"),
        "another-persons-chat": lambda: _lane("max-chat"),
        "no-parent": lambda: _lane(None),
        "missing-parent": lambda: _lane("gone-chat"),
        "agent-scope": lambda: _lane("own-chat", scope="agent"),
        "task-surface": lambda: _lane("own-chat", target="task-run2"),
        "scheduled-task": lambda: _lane("own-chat", task_type=task_kinds.SCHEDULED),
    }[case]()
    assert not _attended(task)


def test_a_delegator_below_the_editor_tier_keeps_the_task_context(rig):
    from storage.identity import db_users
    _chat("own-chat")
    db_users.set_user_agent_role(PERSON, AGENT, "contributor")
    assert not _attended(_lane("own-chat"))


def _calls(rec, name: str) -> dict:
    return [c for c in rec.calls if c["call"] == name][-1]["kwargs"]


def test_the_builder_gives_the_attended_lane_the_chats_context(rig):
    from core.config.task_config_builder import build_task_agent_config
    _chat("own-chat")
    run(build_task_agent_config(AGENT, _lane("own-chat"), "sid-lane-own"))
    mcp = _calls(rig, "build_session_mcp_config")
    assert mcp["task_mode"] is True and mcp["chat_worker"] is True and mcp["shared_chat"] is True
    prompt = _calls(rig, "build_agent_prompt")
    assert prompt["client_type"] == "dashboard" and prompt["shared_chat"] is True


def test_the_builder_keeps_a_task_run_in_the_task_context(rig):
    from core.config.task_config_builder import build_task_agent_config
    _chat("task-run1")
    run(build_task_agent_config(AGENT, _lane("task-run1"), "sid-lane-task"))
    mcp = _calls(rig, "build_session_mcp_config")
    assert mcp["chat_worker"] is False and mcp["shared_chat"] is False
    assert _calls(rig, "build_agent_prompt")["client_type"] == "task"


def test_the_contexts_of_a_chat_lane_and_a_task():
    from services.mcp import mcp_registry
    from services.mcp.mcp_registry import _exclusion_contexts
    base = dict(phone_mode=False, interactive_local=False, meeting_mode=False, external=False)
    assert _exclusion_contexts(task_mode=True, **base) == {"task"}
    assert _exclusion_contexts(task_mode=True, chat_worker=True, shared_chat=True, **base) == {
        "dashboard", mcp_registry.SHARED_CHAT_CONTEXT}


def test_the_config_and_the_prompt_agree_for_a_chat_lane(monkeypatch, tmp_path):
    """A manifest that leaves out ``task`` loads and is advertised; one that
    leaves out ``shared_chat`` (the person's own settings tool) does
    neither; the same build as a task leaves the first out."""
    import re

    from services.mcp import mcp_registry
    from tests.mcp.test_mcp_broker_activation import _stub_assembly
    from tests.mcp.test_meeting_mcp_exclusion import _catalog_manifest

    manifests = [
        _catalog_manifest("config-clone", ["task", "meeting"], skill=["task"], tmp_path=tmp_path),
        _catalog_manifest("settings-clone", ["task", mcp_registry.SHARED_CHAT_CONTEXT],
                          skill=[mcp_registry.SHARED_CHAT_CONTEXT], tmp_path=tmp_path),
    ]
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: list(manifests))

    path, _env, excluded, _b, _k = mcp_registry.build_session_mcp_config(
        "agent", "sub", task_mode=True, chat_worker=True, shared_chat=True)
    loaded = set(json.loads(path.read_text())["mcpServers"])
    assert loaded == {"config-clone"}
    assert excluded["settings-clone"] == "Excluded in shared_chat mode"
    catalog = mcp_registry.build_available_mcps_section("agent", context="dashboard", shared_chat=True)
    assert set(re.findall(r"\(`([^`]+)`\)", catalog)) == {"config-clone"}

    path, _env, excluded, _b, _k = mcp_registry.build_session_mcp_config("agent", "sub", task_mode=True)
    assert path is None or "config-clone" not in json.loads(path.read_text())["mcpServers"]
    assert excluded["config-clone"] == "Excluded in task mode"
