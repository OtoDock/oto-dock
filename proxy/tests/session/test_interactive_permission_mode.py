"""Interactive-TUI permission enforcement: explicit "ask" + live-mode override.

The interactive spawn keeps the CLI's own permission UI, but the platform is
the decision authority: the residual ask-tier returns an explicit
``permissionDecision:"ask"`` (hook silence is treated as ALLOW by the CLI in a
trusted workspace dir — the pre-fix "defer" silently allowed the whole
ask-tier), and the CLI-reported LIVE mode (Shift+Tab) overrides the chat's
stored mode so an in-TUI acceptEdits/bypass choice still auto-allows.
"""
import pytest

from api.hooks import hooks
from auth.path_policy import SecurityContext


def _local_ctx(role: str = "manager") -> SecurityContext:
    return SecurityContext(
        role=role,
        username="dave",
        agent="my-agent",
        is_admin_agent=False,
    )


@pytest.fixture
def _interactive(monkeypatch):
    """DB-free decide_tool_permission for a local interactive dashboard chat
    in stored 'default' mode."""
    monkeypatch.setattr(hooks, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(hooks, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(hooks, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(hooks, "get_session_security", lambda sid: _local_ctx())
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")


@pytest.mark.asyncio
async def test_interactive_default_write_asks(_interactive):
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
    )
    assert res["decision"] == "ask"
    assert "approval" in res["reason"]


@pytest.mark.asyncio
async def test_live_accept_edits_allows_write(_interactive):
    # User pressed Shift+Tab to acceptEdits in the TUI: the hook reports it,
    # and edits auto-allow even though the chat's stored mode is 'default'.
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_live_accept_edits_still_asks_destructive_bash(_interactive):
    # acceptEdits allows edits, not destruction: destructive shell commands
    # keep prompting (platform semantics, same as headless).
    res = await hooks.decide_tool_permission(
        "s", "Bash", {"command": "rm -rf /workspace/scratch"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "ask"


@pytest.mark.asyncio
async def test_live_bypass_maps_to_dont_ask(_interactive):
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="bypassPermissions",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_stored_dont_ask_ignores_live_default(_interactive, monkeypatch):
    # 'dontAsk' is an explicit dashboard choice the interactive spawn can't
    # express (it spawns as CLI 'default') — the CLI-reported 'default' must
    # not resurrect prompts.
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "dontAsk")
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="default",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_stored_auto_ignores_live_mode(_interactive, monkeypatch):
    # Task sessions ('auto') have no human at the TUI — never block them on a
    # native prompt, whatever mode the CLI reports.
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "auto")
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="default",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_headless_ignores_live_mode(monkeypatch):
    # Non-interactive sessions keep the dashboard block-and-wait; the live
    # mode field (never sent by headless spawns anyway) must not shortcut it.
    monkeypatch.setattr(hooks, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(hooks, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(hooks, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(hooks, "get_session_security", lambda sid: _local_ctx())
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: False)
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")

    async def _fake_prompt(request_id, session_id, timeout):
        return False  # user denied on the dashboard

    monkeypatch.setattr(hooks, "wait_for_permission", _fake_prompt)
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "deny"


# ─────────── PostToolUse feeds the session allow-memory (interactive) ────────
# Headless parity: on the dashboard one Allow covers an mcp__ tool's later
# calls. The native TUI prompt's outcome is invisible to the platform, but an
# ask-tier mcp__ tool that RAN was approved — so the tool-result hook feeds
# the same memory.

from unittest.mock import patch

from fastapi.testclient import TestClient

from app import app
from core.session import session_state

_client = TestClient(app)


def _post_tool_result(sid: str, tool: str, is_error: bool = False):
    with patch("api.hooks.hooks.verify_session_match"):
        resp = _client.post(
            "/v1/hooks/tool-result",
            json={"session_id": sid, "tool_name": tool,
                  "summary": "ok", "is_error": is_error},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200


def test_interactive_mcp_tool_result_feeds_allow_memory(monkeypatch):
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-1"
    _post_tool_result(sid, "mcp__display__display_ui")
    assert session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_failed_mcp_tool_result_not_remembered(monkeypatch):
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-2"
    _post_tool_result(sid, "mcp__display__display_ui", is_error=True)
    assert not session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_high_risk_device_tool_never_remembered(monkeypatch):
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "is_high_risk_device_tool",
                        lambda server, tool: True)
    sid = "int-sess-mem-3"
    _post_tool_result(sid, "mcp__blender__execute_blender_code")
    assert not session_state.is_session_tool_allowed(
        sid, "mcp__blender__execute_blender_code")


def test_headless_tool_result_not_remembered(monkeypatch):
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: False)
    sid = "int-sess-mem-4"
    _post_tool_result(sid, "mcp__display__display_ui")
    assert not session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_memory_only_feeds_memory_without_rendering(monkeypatch):
    # The interactive forwarder sends memory_only pings for mcp__ tools (the
    # TUI already rendered the result) — memory is fed, no pump event lands.
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-5"
    with patch("api.hooks.hooks.verify_session_match"):
        resp = _client.post(
            "/v1/hooks/tool-result",
            json={"session_id": sid, "tool_name": "mcp__ssh-hosts__list_ssh_hosts",
                  "summary": "", "memory_only": True},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    assert session_state.is_session_tool_allowed(sid, "mcp__ssh-hosts__list_ssh_hosts")
    queue = session_state.get_permission_queue(sid)
    assert queue.empty()


# ─────────── Manifest permission tiers × interactive modes ──────────────────
# The mcp__ branch consults services/mcp/mcp_permissions before prompting:
# open never asks (any mode, incl. plan), standard is silent in acceptEdits,
# sensitive keeps the pre-tier prompting, critical asks even in dontAsk and
# is denied outright where nobody can answer (task/phone sessions).

from services.mcp import mcp_permissions as _perm_mod


def _pin_tier(monkeypatch, tier: str):
    monkeypatch.setattr(_perm_mod, "resolve_tool_tier", lambda s, t: tier)


@pytest.mark.asyncio
async def test_open_tier_allows_in_interactive_default(_interactive, monkeypatch):
    _pin_tier(monkeypatch, "open")
    res = await hooks.decide_tool_permission("s", "mcp__demo__list_things", {})
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_standard_tier_asks_in_default_allows_in_live_accept_edits(
    _interactive, monkeypatch,
):
    _pin_tier(monkeypatch, "standard")
    res = await hooks.decide_tool_permission("s", "mcp__demo__delegate", {})
    assert res["decision"] == "ask"
    res = await hooks.decide_tool_permission(
        "s", "mcp__demo__delegate", {}, live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_sensitive_tier_still_asks_in_live_accept_edits(_interactive, monkeypatch):
    _pin_tier(monkeypatch, "sensitive")
    res = await hooks.decide_tool_permission(
        "s", "mcp__demo__send_email", {}, live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "ask"


@pytest.mark.asyncio
async def test_critical_tier_asks_even_in_stored_dont_ask(_interactive, monkeypatch):
    # The tier lookup precedes the dontAsk short-circuit, so a critical tool
    # prompts in a Don't Ask chat (human present) instead of auto-running.
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "dontAsk")
    _pin_tier(monkeypatch, "critical")
    res = await hooks.decide_tool_permission("s", "mcp__demo__launch", {})
    assert res["decision"] == "ask"


@pytest.mark.asyncio
async def test_critical_tier_denied_in_unattended_session(_interactive, monkeypatch):
    # Task/phone sessions (non-dashboard client) cannot answer a prompt —
    # critical tools deny-and-inform instead of hanging the run.
    monkeypatch.setattr(hooks, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "auto")
    _pin_tier(monkeypatch, "critical")
    res = await hooks.decide_tool_permission("s", "mcp__demo__launch", {})
    assert res["decision"] == "deny"
    assert "unattended" in res["reason"]


@pytest.mark.asyncio
async def test_open_tier_allowed_in_plan_mode(_interactive, monkeypatch):
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "plan")
    _pin_tier(monkeypatch, "open")
    res = await hooks.decide_tool_permission("s", "mcp__demo__list_things", {})
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_standard_tier_denied_in_plan_mode(_interactive, monkeypatch):
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "plan")
    _pin_tier(monkeypatch, "standard")
    res = await hooks.decide_tool_permission("s", "mcp__demo__delegate", {})
    assert res["decision"] == "deny"


@pytest.mark.asyncio
async def test_high_risk_device_tool_outranks_open_tier(_interactive, monkeypatch):
    # A manifest tier (even from a bundled MCP) must never un-pin a high-risk
    # device tool's per-call prompt.
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "is_high_risk_device_tool", lambda s, t: True)
    _pin_tier(monkeypatch, "open")
    res = await hooks.decide_tool_permission("s", "mcp__blender__execute_blender_code", {})
    assert res["decision"] == "ask"


def test_critical_tool_result_never_remembered(monkeypatch):
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    _pin_tier(monkeypatch, "critical")
    sid = "int-sess-mem-6"
    _post_tool_result(sid, "mcp__demo__launch")
    assert not session_state.is_session_tool_allowed(sid, "mcp__demo__launch")
