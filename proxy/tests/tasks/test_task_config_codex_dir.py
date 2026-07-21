"""build_task_agent_config must pick the right persistent config dir per layer.

Regression guard for an interactive-task bug: the codex layer reads
``config.sandbox_host_claude_dir`` AS its CODEX_HOME, so a Codex agent's task must
create the ``.codex`` dir (via ``ensure_persistent_codex_dir``) — NOT ``.claude``.
``core/config/config_builder.py`` (the chat path) already branches per execution_path;
``task_config_builder`` diverged and always made ``.claude``, so a Codex task wrote
its config.toml/AGENTS.md into ``.claude`` while Codex ran with ``CODEX_HOME=.codex``
→ no config → hang at init. Heavy collaborators are stubbed so the test isolates the
dir-selection path. DB-free (resolve_task_identity is stubbed).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace


from core.config import task_config_builder as tcb


def _stub(monkeypatch, *, execution_path: str):
    """Stub every heavy collaborator so build_task_agent_config runs offline, and
    spy on BOTH persistent-dir factories. Returns the spy-call record."""
    calls = {"claude": 0, "codex": 0}

    def _claude_dir(agent_name, *, username="", scope="user"):
        calls["claude"] += 1
        return Path("/tmp/agents/x/users/u/.claude")

    def _codex_dir(agent_name, *, username="", scope="user"):
        calls["codex"] += 1
        return Path("/tmp/agents/x/users/u/.codex")

    # The builders call ensure_persistent_agent_dir, which dispatches to these
    # two — patch them at their definition site (core.sandbox.session_config_dir) so the
    # in-module dispatch hits the spies.
    import core.sandbox.session_config_dir as scd
    monkeypatch.setattr(scd, "ensure_persistent_claude_dir", _claude_dir)
    monkeypatch.setattr(scd, "ensure_persistent_codex_dir", _codex_dir)

    monkeypatch.setattr(
        tcb, "resolve_task_identity",
        lambda *a, **k: tcb.TaskIdentity(username="", role="manager", scope="agent", creds_user_sub=None),
    )
    monkeypatch.setattr(tcb.agent_store, "get_delegation_targets", lambda *a, **k: [])
    monkeypatch.setattr(tcb.agent_store, "is_admin_only", lambda *a, **k: False)
    monkeypatch.setattr(
        tcb.agent_store, "get_agent",
        lambda *a, **k: {"execution_path": execution_path, "default_execution_mode": ""},
    )

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(tcb.asyncio, "to_thread", _to_thread)

    monkeypatch.setattr(tcb.remote_store, "resolve_execution_target", lambda *a, **k: ("local", None))
    monkeypatch.setattr(tcb.remote_store, "get_target_metadata", lambda *a, **k: ("local", "Local"))
    monkeypatch.setattr(tcb.remote_store, "get_target_has_display", lambda *a, **k: False)
    monkeypatch.setattr(tcb.remote_store, "get_target_device_grants", lambda *a, **k: set())

    def _build_mcp(*a, **k):
        calls["mcp_format"] = k.get("mcp_config_format")
        return (None, {}, {}, {}, set())
    monkeypatch.setattr(tcb.mcp_registry, "build_session_mcp_config", _build_mcp)
    monkeypatch.setattr(tcb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    vis = SimpleNamespace(
        mount_username="", mount_scope="agent", config_visible=False,
        available_scopes=["agent"], memory_user_enabled=False, memory_agent_enabled=True,
        effective_default_scope="agent", mount_shared=True,
    )
    import core.session.visibility as v
    monkeypatch.setattr(v, "resolve_visibility", lambda *a, **k: vis)

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(tcb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(tcb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(tcb.config, "get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(tcb.config, "get_cli_effort", lambda *a, **k: "")
    monkeypatch.setattr(tcb.subscription_pool, "resolve_subscription_env", lambda *a, **k: ("", {}))
    # oto_env + path_roles are imported INSIDE the function (from core import ...).
    import core.sandbox.oto_env as oe
    monkeypatch.setattr(oe, "build_oto_env", lambda **k: {})
    monkeypatch.setattr(oe, "OTO_MULTI_VALUE_ENVS", {}, raising=False)
    return calls


def _task():
    return SimpleNamespace(
        scope="agent", created_by=None, task_type="", notification_mode="manual",
        continue_session=None, on_complete_agent=None, name="t", id="dyn-x",
    )


def test_codex_agent_task_uses_codex_dir_and_toml(monkeypatch):
    calls = _stub(monkeypatch, execution_path="codex-cli")
    cfg = asyncio.run(tcb.build_task_agent_config("test", _task(), "sess-1"))
    assert calls["codex"] == 1 and calls["claude"] == 0
    # The codex layer uses this AS CODEX_HOME — it must be the .codex dir.
    assert cfg.sandbox_host_claude_dir.endswith(".codex")
    assert cfg.execution_path == "codex-cli"
    # The MCP config must be TOML (config.toml [mcp_servers.*]), not Claude JSON —
    # JSON written into config.toml makes Codex's parser exit 1 (the blank TUI).
    assert calls["mcp_format"] == "toml"


def test_claude_agent_task_uses_claude_dir_and_json(monkeypatch):
    calls = _stub(monkeypatch, execution_path="claude-code-cli")
    cfg = asyncio.run(tcb.build_task_agent_config("test", _task(), "sess-2"))
    assert calls["claude"] == 1 and calls["codex"] == 0
    assert cfg.sandbox_host_claude_dir.endswith(".claude")
    assert calls["mcp_format"] == "json"
