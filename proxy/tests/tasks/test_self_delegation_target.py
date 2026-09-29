"""The agent's own slug is ALWAYS included in the resolved delegation
targets, so the delegate_task roster + DELEGATION_TARGETS env list self
(self-delegation is always permitted). Covers the chat builder
(``build_agent_config``); the task builder (``build_task_agent_config``) applies
the identical self-prepend.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_self_delegation_target.py -q
"""

from __future__ import annotations

import asyncio

from storage.agents import agent_store
from storage import database as task_store
from core import placement


def _mk_user(sub: str, name: str, role: str = "member") -> str:
    task_store.upsert_user(sub, f"{sub}@x.test", name, role)
    return task_store.get_username_by_sub(sub)


def _stub_capture(monkeypatch, tmp_path) -> dict:
    """Stub heavy collaborators of build_agent_config; capture the
    delegation_targets passed to build_session_mcp_config."""
    from core.config import config_builder as cb
    captured: dict = {}

    def _capture_mcp(*a, **k):
        captured["delegation_targets"] = list(k.get("delegation_targets") or [])
        return (None, {}, {}, {}, set())  # 5th = bash_env_keys

    monkeypatch.setattr(cb.mcp_registry, "build_session_mcp_config", _capture_mcp)
    monkeypatch.setattr(cb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(cb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    # Return a non-empty subscription_id: build_agent_config now blocks
    # user-scoped work with no resolved subscription (NoSubscriptionError).
    monkeypatch.setattr(cb.subscription_pool, "resolve_subscription_env", lambda *a, **k: ("sub-test", {}))
    monkeypatch.setattr(cb.remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))
    monkeypatch.setattr(cb.remote_store, "placement_of", lambda *a, **k: placement.LOCAL_PLACEMENT)
    monkeypatch.setattr(cb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(cb.config, "get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(cb.config, "get_cli_effort", lambda *a, **k: "")
    # build_agent_config resolves the persistent config dir via
    # core.sandbox.sandbox.ensure_persistent_agent_dir (a call-time local import), so
    # patch it on the source module — config_builder no longer re-exports the
    # old ensure_persistent_claude_dir symbol.
    from core.sandbox import sandbox as _sb
    monkeypatch.setattr(_sb, "ensure_persistent_agent_dir", lambda *a, **k: tmp_path)
    import adapters.dashboard as dash
    monkeypatch.setattr(dash.DashboardAdapter, "build_client_context", lambda self, mc: "")
    return captured


_ADMIN = {"username": "ada", "display_name": "Ada", "email": "a@x", "role": "admin"}


class TestSelfDelegationTarget:
    def test_self_added_with_peers(self, temp_db, monkeypatch, tmp_path):
        agent_store.create_agent("pa", "PA")
        agent_store.create_agent("admin-bot", "Admin Bot")
        agent_store.create_agent("ha-bot", "HA Bot")
        agent_store.set_delegation_targets("pa", ["admin-bot", "ha-bot"])
        _mk_user("sub-ada", "Ada", role="admin")
        captured = _stub_capture(monkeypatch, tmp_path)

        from core.config.config_builder import build_agent_config
        asyncio.run(build_agent_config(
            agent_name="pa", user=_ADMIN, user_sub="sub-ada", user_role="admin",
            client_type="dashboard", chat_id="chat-1",
        ))
        targets = captured["delegation_targets"]
        assert "pa" in targets                       # self present
        assert "admin-bot" in targets and "ha-bot" in targets  # peers kept
        assert targets.count("pa") == 1              # no duplicate

    def test_self_added_with_no_peers(self, temp_db, monkeypatch, tmp_path):
        agent_store.create_agent("solo", "Solo")
        _mk_user("sub-ada", "Ada", role="admin")
        captured = _stub_capture(monkeypatch, tmp_path)

        from core.config.config_builder import build_agent_config
        asyncio.run(build_agent_config(
            agent_name="solo", user=_ADMIN, user_sub="sub-ada", user_role="admin",
            client_type="dashboard", chat_id="chat-2",
        ))
        # An agent with zero configured peers still gets itself as a target.
        assert captured["delegation_targets"] == ["solo"]
