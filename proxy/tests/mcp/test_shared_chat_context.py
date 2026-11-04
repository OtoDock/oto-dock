"""The ``shared_chat`` exclusion context (``mcp_registry.SHARED_CHAT_CONTEXT``):
a session that is not the token-named person's own chat. The
config build and the prompt side match it alike, an agent-scope mount
implies it, the chat builder passes it for the sessions that are not the
person's own, and every shipped manifest's ``exclude_from`` words are known
contexts."""

from __future__ import annotations

import json
import re
import sys

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import _stub_assembly
from tests.mcp.test_meeting_mcp_exclusion import _catalog_manifest, _manifest


def _contexts(**kw) -> set[str]:
    from services.mcp.mcp_registry import _exclusion_contexts
    args = dict(phone_mode=False, task_mode=False, interactive_local=False, meeting_mode=False, external=False)
    args.update(kw)
    return _exclusion_contexts(**args)


def test_the_context_joins_the_set_only_when_the_fact_holds():
    from services.mcp import mcp_registry
    assert mcp_registry.SHARED_CHAT_CONTEXT not in _contexts()
    assert _contexts(shared_chat=True) == {"dashboard", mcp_registry.SHARED_CHAT_CONTEXT}
    assert _contexts(task_mode=True, shared_chat=True) == {"task", mcp_registry.SHARED_CHAT_CONTEXT}
    assert _contexts(phone_mode=True, external=True, shared_chat=True) == {
        "phone", mcp_registry.EXTERNAL_CONTEXT, mcp_registry.SHARED_CHAT_CONTEXT}


def test_the_rule_names_the_context_as_the_reason():
    from services.mcp import mcp_registry
    m = _manifest("settings-clone", [mcp_registry.SHARED_CHAT_CONTEXT])
    assert mcp_registry.session_exclusion_reason(m, contexts={"dashboard"}, external=False) is None
    assert mcp_registry.session_exclusion_reason(
        m, contexts={"dashboard", mcp_registry.SHARED_CHAT_CONTEXT}, external=False,
    ) == "Excluded in shared_chat mode"


@pytest.mark.parametrize("kwargs", [{"shared_chat": True}, {"task_scope": "agent"}])
def test_the_config_build_leaves_the_manifest_out_and_the_prompt_agrees(monkeypatch, tmp_path, kwargs):
    """An agent-scope mount implies the context (the Direct LLM pool builds
    with the scope alone), and the prompt catalog and skills, asked with the
    same fact, advertise exactly what the config loads."""
    from services.mcp import mcp_registry

    manifests = [
        _catalog_manifest("always-on", [], skill=[], tmp_path=tmp_path),
        _catalog_manifest("settings-clone", [mcp_registry.SHARED_CHAT_CONTEXT],
                          skill=[mcp_registry.SHARED_CHAT_CONTEXT], tmp_path=tmp_path),
        _catalog_manifest("config-clone", ["task", "meeting"]),
    ]
    by_name = {m.name: m for m in manifests}
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: list(manifests))

    path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config("agent", None, **kwargs)
    loaded = set(json.loads(path.read_text())["mcpServers"])
    assert "settings-clone" not in loaded
    assert excluded["settings-clone"] == "Excluded in shared_chat mode"

    catalog = mcp_registry.build_available_mcps_section("agent", context="dashboard", shared_chat=True)
    advertised = set(re.findall(r"\(`([^`]+)`\)", catalog))
    skill_owners = {
        skill_id.removesuffix("-skill")
        for skill_id, _content, _loading in mcp_registry.get_skills_for_agent(
            "agent", context="dashboard", shared_chat=True)
    }
    assert "settings-clone" not in advertised and "settings-clone" not in skill_owners
    assert loaded == {by_name[n].server_name or n for n in advertised}

    # The person's own chat: the manifest and its skill are there.
    own = mcp_registry.build_available_mcps_section("agent", context="dashboard")
    assert "settings-clone" in set(re.findall(r"\(`([^`]+)`\)", own))
    assert any(sid == "settings-clone-skill" for sid, _c, _l in
               mcp_registry.get_skills_for_agent("agent", context="dashboard"))


def test_a_chat_lane_loads_the_settings_tool_and_a_task_lane_does_not(monkeypatch, tmp_path):
    """A chat lane a person delegated from their own chat builds with the
    chat's context (``task_config_builder``: ``chat_worker=True`` and
    ``shared_chat=True``) and loads the shipped settings-mcp: its cards ask
    the delegator, who is present. A task lane keeps the task context and
    goes without it; the cards routes refuse where nobody can press."""
    from services.mcp import mcp_registry
    from services.mcp.mcp_manifest_parse import _parse_manifest
    shipped = _parse_manifest(PROXY_DIR.parent / "mcps" / "custom" / "settings-mcp" / "manifest.json")
    assert mcp_registry.SHARED_CHAT_CONTEXT not in shipped.exclude_from
    manifests = [_catalog_manifest("settings-mcp", shipped.exclude_from)]
    _stub_assembly(monkeypatch, manifests, env_by_mcp={}, tmp_path=tmp_path)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: list(manifests))
    path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, task_mode=True, chat_worker=True, shared_chat=True)
    assert "settings-mcp" in set(json.loads(path.read_text())["mcpServers"])
    assert "settings-mcp" not in excluded
    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config("agent", None, task_mode=True)
    assert excluded["settings-mcp"] == "Excluded in task mode"


def test_every_shipped_manifest_names_known_contexts():
    """A word the resolver never matches excludes nothing: a typo in a
    shipped manifest would load an MCP where it opted out."""
    from core.session import session_kind
    from services.mcp import mcp_registry
    from services.mcp.mcp_manifest_parse import _parse_manifest
    known = {session_kind.DASHBOARD.name, session_kind.PHONE.name, session_kind.TASK.name,
             session_kind.MEETING.name, "terminal", mcp_registry.EXTERNAL_CONTEXT,
             mcp_registry.SHARED_CHAT_CONTEXT}
    root = PROXY_DIR.parent / "mcps" / "custom"
    seen = 0
    for manifest_path in sorted(root.glob("*/manifest.json")):
        m = _parse_manifest(manifest_path)
        unknown = set(m.exclude_from or []) - known
        assert not unknown, f"{manifest_path.parent.name}: {sorted(unknown)}"
        for skill in m.skills:
            assert not (set(skill.default_exclude_from or []) - known), manifest_path.parent.name
        seen += 1
    assert seen > 10


# ── the chat builder passes the fact ───────────────────────────────────────

AGENT = "char-agent"
SHARED = "char-shared"


def _mcp_calls(rig) -> list[dict]:
    return [c for c in rig.calls if c["call"] == "build_session_mcp_config"]


def _prompt_calls(rig) -> list[dict]:
    return [c for c in rig.calls if c["call"] == "build_agent_prompt"]


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    from tests.fixtures.loop_builder_rig import install
    return install(monkeypatch, tmp_path)


def _build(rig, agent: str, sub: str, role: str, **kw):
    import asyncio

    from core.config.config_builder import build_agent_config
    from storage import database as db
    rig.calls.clear()
    return asyncio.run(build_agent_config(
        agent, db.get_user(sub), sub, role, client_type="dashboard", session_id="s-shared", **kw))


def test_a_persons_own_chat_passes_nothing(rig):
    _build(rig, AGENT, "user-viewer", "editor")
    assert "shared_chat" not in _mcp_calls(rig)[0]["kwargs"]
    assert "shared_chat" not in _prompt_calls(rig)[0]["kwargs"]


def test_a_shared_only_chat_and_a_task_rewarm_pass_it(rig):
    from core.config.task_config_builder import TaskIdentity
    from storage.identity import db_users
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    _build(rig, SHARED, "user-manager", "editor")
    assert _mcp_calls(rig)[0]["kwargs"]["shared_chat"] is True
    assert _prompt_calls(rig)[0]["kwargs"]["shared_chat"] is True
    ident = TaskIdentity(username="vic", role="editor", scope="user", creds_user_sub="user-viewer")
    _build(rig, AGENT, "user-admin", "admin", task_identity=ident)
    assert _mcp_calls(rig)[0]["kwargs"]["shared_chat"] is True


def test_a_chat_another_person_owns_passes_it(rig):
    from storage import database as db
    db.create_chat("chat-of-vic", "user-viewer", AGENT, title="Vic's chat")
    _build(rig, AGENT, "user-admin", "admin", chat_id="chat-of-vic")
    assert _mcp_calls(rig)[0]["kwargs"]["shared_chat"] is True
    _build(rig, AGENT, "user-viewer", "editor", chat_id="chat-of-vic")
    assert "shared_chat" not in _mcp_calls(rig)[0]["kwargs"]
    # A chat with no row yet is the person's own.
    _build(rig, AGENT, "user-viewer", "editor", chat_id="chat-not-yet")
    assert "shared_chat" not in _mcp_calls(rig)[0]["kwargs"]
