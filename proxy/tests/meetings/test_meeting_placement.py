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

import pytest

from core import placement

_FULL = placement.PlacementCapabilities(
    kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1", label="Office", os="linux",
    home_dir="/home/svc", agents_dir="/home/svc/.oto-dock/agents", os_user="svc",
    allow_full_fs=True, device_grants={"computer"}, has_display=True,
)


def _stub(monkeypatch, captured: dict):
    import core.sandbox.oto_env as oe
    import core.session.visibility as v
    from core.config import task_config_builder as tcb
    from services.meetings import meeting_context as mc
    from services.remote import remote_status
    from storage import remote_store
    monkeypatch.setattr(tcb, "resolve_task_identity", lambda *a, **k: tcb.TaskIdentity(
        username="alice", role="manager", scope="user", creds_user_sub="alice-sub"))
    # The meeting's person holds the agent (who pays is not this file's).
    from core.config import spender
    monkeypatch.setattr(spender, "may_spend", lambda *a, **k: True)
    monkeypatch.setattr(mc.agent_store, "get_delegation_targets", lambda *a, **k: [])
    monkeypatch.setattr(mc.agent_store, "is_admin_only", lambda *a, **k: False)
    monkeypatch.setattr(mc.agent_store, "get_agent", lambda *a, **k: {"execution_path": "claude-code-cli"})

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(mc.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("m-1", None))
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


_COMPUTE = placement.from_machine(placement.KIND_COMPUTE, {"id": "compute-1", "name": "Compute", "pairing_scope": "compute"})


def _direct_world(monkeypatch, captured, *, row, engine="direct-llm"):
    from services.meetings import meeting_context as mc
    from services.remote import remote_status
    from storage import remote_store
    _stub(monkeypatch, captured)
    monkeypatch.setattr(mc.agent_store, "get_agent", lambda *a, **k: {"execution_path": engine})
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))

    def _serving(path="", **kw):
        captured["path"], captured["asked"] = path, kw
        return row
    monkeypatch.setattr(remote_status, "serving_compute_machine", _serving)

    def _placement_of(value, sub, agent, *, compute=None):
        return _COMPUTE if compute else placement.LOCAL_PLACEMENT
    monkeypatch.setattr(remote_store, "placement_of", _placement_of)


def test_a_direct_llm_participants_tools_go_to_the_compute_machine(monkeypatch):
    """Its record keeps the machine: the relay opens by it and the tools'
    callbacks are admitted by it (the stripped form would lose the id)."""
    from services.meetings import meeting_context as mc
    captured: dict = {}
    _direct_world(monkeypatch, captured, row={"id": "compute-1"})
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert captured["path"] == "direct-llm"
    assert cfg.security_context.placement.machine_id == "compute-1"
    assert cfg.security_context.placement.kind == placement.KIND_COMPUTE
    assert captured["mcp_placement"].kind == placement.KIND_COMPUTE
    # Its target stays the platform's: the machine is in the placement.
    assert placement.is_local(cfg.execution_target)


@pytest.mark.parametrize("engine", ["claude-code-cli", "codex-cli"])
def test_a_cli_participant_runs_on_the_compute_machine(monkeypatch, engine):
    """A Claude Code or Codex participant's session goes to the compute
    machine that serves, one that shares the platform's folders. Its record
    keeps the machine whole: the layer and admission read it there."""
    from services.meetings import meeting_context as mc
    captured: dict = {}
    _direct_world(monkeypatch, captured, row={"id": "compute-1"}, engine=engine)
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert captured["path"] == engine and captured["asked"] == {"prefer": "", "shared_folder": True}
    assert cfg.security_context.placement.machine_id == "compute-1"
    assert cfg.security_context.placement.kind == placement.KIND_COMPUTE
    assert placement.is_local(cfg.execution_target)


def test_a_participant_on_a_paired_machine_never_asks_for_a_compute_machine(monkeypatch):
    """Only the platform's place is served by a compute machine: a
    participant whose target is a person's or an admin's machine keeps it."""
    from services.meetings import meeting_context as mc
    from services.remote import remote_status
    captured: dict = {}
    _stub(monkeypatch, captured)
    monkeypatch.setattr(remote_status, "serving_compute_machine",
                        lambda *a, **k: captured.__setitem__("asked", True))
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert cfg.execution_target == "m-1" and "asked" not in captured


@pytest.mark.parametrize("engine", ["direct-llm", "claude-code-cli", "codex-cli"])
def test_with_no_serving_machine_a_participant_is_refused(monkeypatch, engine):
    """The platform runs no session itself: with no compute machine a
    participant's build is refused with the one sentence, on every engine
    (a Direct LLM participant's tools are part of its session), before the
    meeting takes any slot."""
    from services.meetings import meeting_context as mc
    captured: dict = {}
    _direct_world(monkeypatch, captured, row=None, engine=engine)
    with pytest.raises(placement.NoComputeMachine) as refused:
        asyncio.run(mc.build_meeting_agent_config(
            "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert str(refused.value) == placement.NO_COMPUTE_SENTENCE
    assert "mcp_placement" not in captured


def test_the_meeting_build_judges_the_audience_with_the_participants_role(monkeypatch):
    """A Direct LLM participant's tools are this build's: the audience gate
    needs the role, which the manager's own build used to apply."""
    from services.meetings import meeting_context as mc
    captured: dict = {}
    _stub(monkeypatch, captured)
    seen: dict = {}

    def _build_mcp(*a, **k):
        seen.update(k)
        return (None, {}, {}, {}, set())
    monkeypatch.setattr(mc.mcp_registry, "build_session_mcp_config", _build_mcp)
    asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert seen["user_role"] == "manager" and seen["username"] == "alice"


_REAL_TO_THREAD = asyncio.to_thread


def test_the_prompt_is_built_off_the_loop(monkeypatch):
    """``build_agent_prompt`` runs in a worker thread (the stub above runs
    ``to_thread`` inline; this test puts the real one back): the loop thread
    never builds a participant's prompt."""
    import threading

    from services.meetings import meeting_context as mc
    captured: dict = {}
    _stub(monkeypatch, captured)
    monkeypatch.setattr(mc.asyncio, "to_thread", _REAL_TO_THREAD)
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

    monkeypatch.setattr(mc.config, "build_agent_prompt", recorder)
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-2"}, "sess-2"))
    assert "PROMPT" in cfg.system_prompt
    assert seen["thread"] != threading.get_ident() and seen["loop"] is None


def test_a_person_with_no_engine_is_refused_by_the_meetings_build(monkeypatch):
    """The refusal a chat and a task meet, with its reason: the meeting's
    card offers the connection instead of a participant starting empty."""
    import pytest

    from services.meetings import meeting_context as mc
    _stub(monkeypatch, {})
    monkeypatch.setattr(mc.subscription_pool, "resolve_subscription_env", lambda *a, **k: ("", {}))
    monkeypatch.setattr(mc.subscription_pool, "user_scope_block_reason", lambda *a, **k: "auth_off")
    with pytest.raises(mc.subscription_pool.NoSubscriptionError) as refused:
        asyncio.run(mc.build_meeting_agent_config(
            "pa", {"scope": "user", "created_by": "alice-sub", "id": "mt-1"}, "sess-1"))
    assert refused.value.reason == "auth_off"


def test_an_agent_scope_meeting_with_no_account_is_left_to_the_pool(monkeypatch):
    """No person to connect anything: as an agent-scope task is, it is not
    refused here."""
    from services.meetings import meeting_context as mc
    _stub(monkeypatch, {})
    monkeypatch.setattr(mc.subscription_pool, "resolve_subscription_env", lambda *a, **k: ("", {}))
    cfg = asyncio.run(mc.build_meeting_agent_config(
        "pa", {"scope": "agent", "created_by": "", "id": "mt-1"}, "sess-1"))
    assert cfg.subscription_id in ("", None)
