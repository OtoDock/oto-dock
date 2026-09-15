"""A Direct LLM session is always placed LOCAL.

The engine runs in-process on the server (``session_manager.get_layer`` routes
it local unconditionally, ``remote_execution`` refuses it), but until
2026-09-07 every builder resolved the agent's pinned / default machine BEFORE
it looked at the execution path: the prompt's placement section named the
satellite and its home dir and the MCP set carried the satellite's device
grants for a chat that then ran on the server ("I'm running on My Desktop
(C:/Users/…)" from a Direct LLM chat on qwen3.6). Three layers now agree:

1. ``remote_store.resolve_execution_target`` answers ``local`` for a
   direct-llm AGENT (covers the chat/task/meeting/phone builders, the
   scheduler and the dashboard's pin logic);
2. ``build_agent_config`` places a chat local when its EFFECTIVE path is
   direct-llm — a chat-level override on another agent, or a pin stored
   before the fix;
3. the dashboard's locality-mismatch banner skips direct-llm chats (there
   is nowhere to move them).

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/execution/test_direct_llm_target_local.py -q
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from storage.agents import agent_store
from storage import remote_store
from storage import database as task_store

_ADMIN = {"username": "ada", "display_name": "Ada", "email": "a@x", "role": "admin"}
_DIRECT = {"execution_target": "machine-admin", "execution_path": "direct-llm"}
_CLI = {"execution_target": "machine-admin", "execution_path": "claude-code-cli"}


def _mk_user(sub: str, name: str, role: str = "admin") -> str:
    task_store.upsert_user(sub, f"{sub}@x.test", name, role)
    return task_store.get_username_by_sub(sub)


# ---------------------------------------------------------------------------
# 1. The resolver
# ---------------------------------------------------------------------------

def test_resolver_answers_local_for_a_direct_llm_agent(temp_db):
    with patch("storage.agents.agent_store.get_agent", return_value=_DIRECT), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        assert remote_store.resolve_execution_target("pa", None, "manager") == ("local", None)
        assert remote_store.resolve_execution_target("pa", None, "admin") == ("local", None)


def test_resolver_ignores_a_user_override_for_a_direct_llm_agent(temp_db):
    # The user's own paired machine wins for every other layer (viewers
    # included) — not for an engine that cannot run there.
    with patch("storage.agents.agent_store.get_agent", return_value=_DIRECT), \
         patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        assert remote_store.resolve_execution_target("pa", "user-1", "viewer") == ("local", None)


def test_resolver_keeps_the_machine_for_a_cli_agent(temp_db):
    # Regression guard: the short-circuit is keyed on the path, nothing else.
    with patch("storage.agents.agent_store.get_agent", return_value=_CLI), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        assert remote_store.resolve_execution_target("pa", None, "manager") == ("machine-admin", None)


# ---------------------------------------------------------------------------
# 2. build_agent_config — the EFFECTIVE path decides
# ---------------------------------------------------------------------------

def _stub_builder(monkeypatch, tmp_path) -> dict:
    """Stub the heavy collaborators of build_agent_config; the resolver names
    a MACHINE so the builder's own placement decision is what is under test.
    Captures the ``is_remote`` flag handed to the MCP config builder."""
    from core.config import config_builder as cb
    captured: dict = {}

    def _capture_mcp(*a, **k):
        captured["is_remote"] = k.get("is_remote")
        return (None, {}, {}, {}, set())

    monkeypatch.setattr(cb.mcp_registry, "build_session_mcp_config", _capture_mcp)
    monkeypatch.setattr(cb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(cb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(cb.subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-test", {}))
    monkeypatch.setattr(cb.remote_store, "resolve_execution_target",
                        lambda *a, **k: ("machine-1", None))
    monkeypatch.setattr(
        cb.remote_store, "get_target_metadata",
        lambda target, *a, **k: ("admin_remote", "My Desktop") if target == "machine-1" else ("local", ""),
    )
    monkeypatch.setattr(cb.remote_store, "get_remote_machine",
                        lambda *a, **k: {"id": "machine-1", "capabilities": "{}"})
    monkeypatch.setattr(cb.remote_store, "get_target_browser_settings", lambda *a, **k: None)
    monkeypatch.setattr(cb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(cb.config, "get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(cb.config, "get_cli_effort", lambda *a, **k: "")
    from core.sandbox import sandbox as _sb
    monkeypatch.setattr(_sb, "ensure_persistent_agent_dir", lambda *a, **k: tmp_path)
    import adapters.dashboard as dash
    monkeypatch.setattr(dash.DashboardAdapter, "build_client_context", lambda self, mc: "")
    return captured


def _build(agent: str, **kw):
    from core.config.config_builder import build_agent_config
    return asyncio.run(build_agent_config(
        agent_name=agent, user=_ADMIN, user_sub="sub-ada", user_role="admin",
        client_type="dashboard", chat_id=kw.pop("chat_id", "chat-1"), **kw,
    ))


class TestBuilderPlacesDirectLlmLocal:
    def test_default_resolution_names_a_machine_but_the_chat_is_local(self, temp_db, monkeypatch, tmp_path):
        agent_store.create_agent("dl", "DL", execution_path="direct-llm")
        _mk_user("sub-ada", "Ada")
        captured = _stub_builder(monkeypatch, tmp_path)
        cfg = _build("dl")
        assert cfg.execution_target == "local"
        assert cfg.security_context.target_kind == "local"
        assert captured["is_remote"] is False

    def test_a_pin_stored_before_the_fix_is_placed_local(self, temp_db, monkeypatch, tmp_path):
        agent_store.create_agent("dl", "DL", execution_path="direct-llm")
        _mk_user("sub-ada", "Ada")
        captured = _stub_builder(monkeypatch, tmp_path)
        with patch("services.remote.remote_status.is_reachable", return_value=True):
            cfg = _build("dl", pinned_target="machine-1")
        assert cfg.execution_target == "local"
        assert cfg.security_context.target_kind == "local"
        assert captured["is_remote"] is False

    def test_a_chat_level_override_to_direct_llm_is_local_too(self, temp_db, monkeypatch, tmp_path):
        # The agent defaults to the CLI layer (the resolver names its machine);
        # the chat picked the Direct LLM engine.
        agent_store.create_agent("cx", "CX", execution_path="claude-code-cli")
        _mk_user("sub-ada", "Ada")
        captured = _stub_builder(monkeypatch, tmp_path)
        cfg = _build("cx", execution_path="direct-llm")
        assert cfg.execution_target == "local"
        assert captured["is_remote"] is False

    def test_a_cli_chat_keeps_its_machine(self, temp_db, monkeypatch, tmp_path):
        # Regression guard for every other layer.
        agent_store.create_agent("cx", "CX", execution_path="claude-code-cli")
        _mk_user("sub-ada", "Ada")
        captured = _stub_builder(monkeypatch, tmp_path)
        cfg = _build("cx")
        assert cfg.execution_target == "machine-1"
        assert cfg.security_context.target_kind == "admin_remote"
        assert captured["is_remote"] is True


# ---------------------------------------------------------------------------
# 3. The dashboard's locality-mismatch banner
# ---------------------------------------------------------------------------

def _controller(user_sub: str):
    import ws.dashboard  # noqa: F401 — circular-import order
    from ws.dashboard_warmup import WarmupController
    ctl = WarmupController.__new__(WarmupController)
    ctl.user_sub = user_sub
    return ctl


def test_no_move_banner_for_a_direct_llm_chat(temp_db):
    agent_store.create_agent("cx", "CX", execution_path="claude-code-cli")
    _mk_user("sub-ada", "Ada")
    task_store.create_chat("c-dl", "sub-ada", "cx", execution_path="direct-llm")
    task_store.update_chat("c-dl", execution_target="local")
    task_store.create_chat("c-cli", "sub-ada", "cx")
    task_store.update_chat("c-cli", execution_target="local")
    ctl = _controller("sub-ada")
    with patch.object(remote_store, "resolve_execution_target", return_value=("machine-1", None)), \
         patch.object(remote_store, "get_remote_machine", return_value={"name": "My Desktop"}), \
         patch("ws.dashboard_warmup._effective_agent_role", return_value="admin"):
        assert ctl._target_mismatch_fields("c-dl") == {}
        # The same pin on a CLI chat still advertises the move.
        assert ctl._target_mismatch_fields("c-cli") == {
            "pinned_target": "local", "pinned_label": "local sandbox",
            "resolved_target": "machine-1", "resolved_label": "My Desktop",
        }
