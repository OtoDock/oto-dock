"""A meeting participant's placement (core-seams phase 7, D17).

The builder resolves the participant's placement once — the FULL object
gates the MCP set and the prompt — but its ``SecurityContext`` carries the
kind and the label only, exactly what it carried before the descriptor: a
remote participant fail-closes satellite paths and device tools, is reached
by neither live refresher nor the revocation check, and a Codex participant's
sandbox mode ignores the machine's flag. Carrying the full object would open
all five; that is the operator's decision, not this refactor's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from core import placement

_FULL = placement.PlacementCapabilities(
    kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1", label="Office", os="linux",
    home_dir="/home/svc", agents_dir="/home/svc/.oto-dock/agents", os_user="svc",
    allow_full_fs=True, device_grants={"computer"}, has_display=True,
)


def _stub(monkeypatch, captured: dict):
    from services.meetings import meeting_context as mc
    from core.config import task_config_builder as tcb
    import core.session.visibility as v
    import core.sandbox.oto_env as oe
    from storage import remote_store
    monkeypatch.setattr(tcb, "resolve_task_identity", lambda *a, **k: tcb.TaskIdentity(
        username="alice", role="manager", scope="user", creds_user_sub="alice-sub"))
    monkeypatch.setattr(mc.agent_store, "get_delegation_targets", lambda *a, **k: [])
    monkeypatch.setattr(mc.agent_store, "is_admin_only", lambda *a, **k: False)
    monkeypatch.setattr(mc.agent_store, "get_agent", lambda *a, **k: {"execution_path": "claude-code-cli"})

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(mc.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(remote_store, "resolve_execution_target", lambda *a, **k: ("m-1", None))
    monkeypatch.setattr(remote_store, "placement_of", lambda *a, **k: _FULL)
    monkeypatch.setattr(remote_store, "get_target_browser_settings", lambda *a, **k: None)

    def _build_mcp(*a, **k):
        captured["mcp_placement"] = k.get("placement")
        return (None, {}, {}, {}, set())
    monkeypatch.setattr(mc.mcp_registry, "build_session_mcp_config", _build_mcp)

    def _mcps(*a, **k):
        captured["mcps_placement"] = k.get("placement")
        return []
    monkeypatch.setattr(mc.mcp_registry, "get_agent_mcps", _mcps)
    vis = SimpleNamespace(mount_username="alice", mount_scope="user", config_visible=True,
                          available_scopes=("user", "agent"), memory_user_enabled=False,
                          memory_agent_enabled=True, effective_default_scope="user", mount_shared=True)
    monkeypatch.setattr(v, "resolve_visibility", lambda *a, **k: vis)

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(mc.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(mc.dynamic_context, "build_delegation_roster", lambda *a, **k: {})
    monkeypatch.setattr(mc.dynamic_context, "build_meetings_access", lambda *a, **k: [])

    def _prompt(*a, **k):
        captured["prompt_placement"] = k.get("placement")
        return "PROMPT"
    monkeypatch.setattr(mc.config, "build_agent_prompt", _prompt)
    monkeypatch.setattr(mc.config, "get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(mc.config, "get_cli_effort", lambda *a, **k: "")
    monkeypatch.setattr(mc.subscription_pool, "resolve_subscription_env", lambda *a, **k: ("sub-test", {}))
    monkeypatch.setattr(oe, "build_oto_env", lambda **k: {})
    monkeypatch.setattr(oe, "OTO_MULTI_VALUE_ENVS", {}, raising=False)
    from core.sandbox import sandbox as _sb
    monkeypatch.setattr(_sb, "ensure_persistent_agent_dir", lambda *a, **k: Path("/tmp/agents/x/users/alice/.claude"))


def test_the_participant_keeps_the_kind_and_the_label_only(monkeypatch):
    from services.meetings import meeting_context as mc
    captured: dict = {}
    _stub(monkeypatch, captured)
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert cfg.execution_target == "m-1"
    # the gates and the prompt saw the full placement
    assert captured["mcp_placement"] is _FULL and captured["prompt_placement"] is _FULL
    assert captured["mcps_placement"] is _FULL
    # the context did not: the kind and the label, no machine facts
    p = cfg.security_context.placement
    assert p == placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, label="Office")
    assert p.admin_paired and p.is_remote
    assert p.machine_id == "" and p.home_dir == "" and p.agents_dir == ""
    assert p.allow_full_fs is False and p.device_grants == set() and p.site == placement.SITE_LOCAL
