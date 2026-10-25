"""Interactive-TUI permission posture: defer-to-CLI + live-mode override.

Operator decision 2026-07-26: with a human at the keyboard, the platform has
NO OPINION on the residual ask-tier — ``{"decision": "defer"}`` makes the
gate script emit nothing and the CLI's own permission engine (the live
Shift+Tab mode) governs, exactly like Claude Code outside the platform.
Hard denies (Pass-1) hold on every surface, and three carve-outs still
return the platform "ask" (which feeds the CLI's ONE native prompt, never a
second dialog): critical-tier MCP tools and, in Ask,
destructive Bash. The CLI-reported LIVE mode (Shift+Tab) moves the chat's
mode (the chat follows the terminal: ``auto`` and ``acceptEdits`` read as
Auto, ``default`` as Ask); stored dontAsk stays a hard allow (explicit
silence the TUI cannot express); unattended interactive TASKS are excluded
from the flip.
"""
import pytest

from api.hooks import permission, routing
from auth.path_policy import SecurityContext
from core.session import session_state


@pytest.fixture(autouse=True)
def _fresh_terminal():
    """Each test's terminal starts from its own spawn word: the mirror keeps
    the last one per session."""
    yield
    session_state._live_words.pop("s", None)
    session_state._session_modes.pop("s", None)
    session_state._session_token_floor.pop("s", None)


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
    in stored 'default' mode, its terminal spawned in the CLI's default."""
    session_state._live_words["s"] = "default"
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(permission, "get_session_security", lambda sid: _local_ctx())
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")

    async def _no_chat(sid):
        return ""
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _no_chat)
    # A Claude Code terminal (the engine whose terminal reports its mode).
    from core.session import session_manager
    claude = session_manager.get_all_layers()["claude-code-cli"]
    monkeypatch.setattr(session_manager, "engine_layer_for_session", lambda sid: claude)


@pytest.mark.asyncio
async def test_interactive_default_write_defers_to_cli(_interactive):
    # Post-flip: the platform has no opinion — the CLI's native trusted-dir
    # behavior governs (Write/Edit run promptless in default mode).
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
    )
    assert res["decision"] == "defer"


@pytest.mark.asyncio
async def test_live_accept_edits_allows_write(_interactive):
    # User pressed Shift+Tab to acceptEdits in the TUI: the hook reports it,
    # the chat follows to Auto, and edits run though it was stored in Ask.
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_live_accept_edits_keeps_the_destructive_card(_interactive):
    # acceptEdits reads as Auto, but the CLI is not in its auto mode, so no
    # reviewer judges a destructive command: the terminal asks for it.
    res = await permission.decide_tool_permission(
        "s", "Bash", {"command": "rm -rf /workspace/scratch"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("live,decision", [("auto", "defer"), ("acceptEdits", "ask")])
async def test_an_auto_terminal_defers_destructive_bash_only_to_a_running_reviewer(
        _interactive, monkeypatch, live, decision):
    # Stored Auto: in the CLI's auto mode its classifier judges the command;
    # Shift+Tabbed to acceptEdits (or spawned there by a satellite before
    # 0.5.156) nothing would, so the terminal asks.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "autoReview")
    session_state._live_words["s"] = live
    res = await permission.decide_tool_permission(
        "s", "Bash", {"command": "rm -rf /workspace/scratch"}, live_permission_mode=live)
    assert res["decision"] == decision
    # A non-destructive command outside Auto's allow set is the terminal's.
    res = await permission.decide_tool_permission(
        "s", "Bash", {"command": "curl -s https://example.com"}, live_permission_mode=live)
    assert res["decision"] == "defer"


@pytest.mark.asyncio
async def test_live_bypass_maps_to_dont_ask(_interactive):
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="bypassPermissions",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_stored_dont_ask_ignores_live_default(_interactive, monkeypatch):
    # 'dontAsk' is an explicit dashboard choice the interactive spawn can't
    # express (it spawns as CLI 'default') — the CLI-reported 'default' must
    # not resurrect prompts.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "dontAsk")
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="default",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_stored_auto_ignores_live_mode(_interactive, monkeypatch):
    # Task sessions ('auto') have no human at the TUI — never block them on a
    # native prompt, whatever mode the CLI reports.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="default",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_headless_ignores_live_mode(monkeypatch):
    # Non-interactive sessions keep the dashboard block-and-wait; the live
    # mode field (never sent by headless spawns anyway) must not shortcut it.
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(permission, "get_session_security", lambda sid: _local_ctx())
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: False)
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")

    async def _fake_prompt(request_id, session_id, timeout):
        return False  # user denied on the dashboard

    monkeypatch.setattr(permission, "wait_for_permission", _fake_prompt)
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"},
        live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "deny"


# ─────────── PostToolUse allow-memory inference: REMOVED ────────────────────
# Post-flip the platform defers the interactive residual tier to the CLI, so
# a tool that ran may reflect a native "allow once" — feeding OUR memory from
# it would hard-allow every later call and suppress the CLI's own re-prompt.
# The CLI's own allow-memory now owns interactive persistence.

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import app

_client = TestClient(app)


def _post_tool_result(sid: str, tool: str, is_error: bool = False):
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock):
        resp = _client.post(
            "/v1/hooks/tool-result",
            json={"session_id": sid, "tool_name": tool,
                  "summary": "ok", "is_error": is_error},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200


def test_interactive_mcp_tool_result_no_longer_feeds_allow_memory(monkeypatch):
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-1"
    _post_tool_result(sid, "mcp__display__display_ui")
    assert not session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_failed_mcp_tool_result_not_remembered(monkeypatch):
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-2"
    _post_tool_result(sid, "mcp__display__display_ui", is_error=True)
    assert not session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_headless_tool_result_not_remembered(monkeypatch):
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: False)
    sid = "int-sess-mem-4"
    _post_tool_result(sid, "mcp__display__display_ui")
    assert not session_state.is_session_tool_allowed(sid, "mcp__display__display_ui")


def test_memory_only_ping_renders_nothing_and_feeds_nothing(monkeypatch):
    # The interactive forwarder's memory_only ping stays a silent no-op: no
    # pump event AND (post-flip) no allow-memory feed.
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    sid = "int-sess-mem-5"
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock):
        resp = _client.post(
            "/v1/hooks/tool-result",
            json={"session_id": sid, "tool_name": "mcp__ssh-hosts__list_ssh_hosts",
                  "summary": "", "memory_only": True},
            headers={"Authorization": "Bearer dummy"},
        )
    assert resp.status_code == 200
    assert not session_state.is_session_tool_allowed(sid, "mcp__ssh-hosts__list_ssh_hosts")
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
    res = await permission.decide_tool_permission("s", "mcp__demo__list_things", {})
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_standard_tier_defers_in_default_allows_in_live_accept_edits(
    _interactive, monkeypatch,
):
    _pin_tier(monkeypatch, "standard")
    res = await permission.decide_tool_permission("s", "mcp__demo__delegate", {})
    assert res["decision"] == "defer"
    res = await permission.decide_tool_permission(
        "s", "mcp__demo__delegate", {}, live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_sensitive_tier_defers_to_cli_in_interactive(_interactive, monkeypatch):
    # Stated, accepted residual of the flip: sensitive-tier (including every
    # undeclared community tool, which defaults to sensitive) becomes
    # CLI-governed in interactive terminals.
    _pin_tier(monkeypatch, "sensitive")
    res = await permission.decide_tool_permission(
        "s", "mcp__demo__send_email", {}, live_permission_mode="acceptEdits",
    )
    assert res["decision"] == "defer"


@pytest.mark.asyncio
async def test_critical_tier_asks_even_in_stored_dont_ask(_interactive, monkeypatch):
    # The tier lookup precedes the dontAsk short-circuit, so a critical tool
    # prompts in a Don't Ask chat (human present) instead of auto-running.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "dontAsk")
    _pin_tier(monkeypatch, "critical")
    res = await permission.decide_tool_permission("s", "mcp__demo__launch", {})
    assert res["decision"] == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,mode,decision", [
    ("claude-code-cli", "autoReview", "ask"),
    ("codex-cli", "autoReview", "deny"),
    ("codex-cli", "ask", "ask"),
])
async def test_a_critical_tool_in_an_auto_codex_terminal_is_refused(
        _interactive, monkeypatch, engine, mode, decision):
    # Codex's hook only denies, and in Auto its own reviewer would answer the
    # terminal's prompt: a critical tool never reaches a person there, so it
    # is refused with the way to run it. Claude's terminal asks the person;
    # Codex in Ask prompts the person in its own terminal.
    from core.session import session_manager
    layer = session_manager.get_all_layers()[engine]
    monkeypatch.setattr(session_manager, "engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: mode)
    _pin_tier(monkeypatch, "critical")
    res = await permission.decide_tool_permission(
        "s", "mcp__agent-config-mcp__share_knowledge_folder", {}, live_permission_mode="default")
    assert res["decision"] == decision
    if decision == "deny":
        assert res["reason"] == ("OtoDock: this action needs a person's approval; a Codex terminal "
                                 "in Auto cannot ask for it. Switch the chat to Ask, or run it "
                                 "from the chat.")


@pytest.mark.asyncio
async def test_critical_tier_denied_in_unattended_session(_interactive, monkeypatch):
    # Task/phone sessions (non-dashboard client) cannot answer a prompt —
    # critical tools deny-and-inform instead of hanging the run.
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    _pin_tier(monkeypatch, "critical")
    res = await permission.decide_tool_permission("s", "mcp__demo__launch", {})
    assert res["decision"] == "deny"
    assert "unattended" in res["reason"]


@pytest.mark.asyncio
async def test_open_tier_allowed_in_plan_mode(_interactive, monkeypatch):
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "plan")
    _pin_tier(monkeypatch, "open")
    res = await permission.decide_tool_permission("s", "mcp__demo__list_things", {})
    assert res["decision"] == "allow"


@pytest.mark.asyncio
async def test_standard_tier_denied_in_plan_mode(_interactive, monkeypatch):
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "plan")
    _pin_tier(monkeypatch, "standard")
    res = await permission.decide_tool_permission("s", "mcp__demo__delegate", {})
    assert res["decision"] == "deny"


def test_critical_tool_result_never_remembered(monkeypatch):
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    _pin_tier(monkeypatch, "critical")
    sid = "int-sess-mem-6"
    _post_tool_result(sid, "mcp__demo__launch")
    assert not session_state.is_session_tool_allowed(sid, "mcp__demo__launch")


# ─────────── Flip boundary cases (operator decision 2026-07-26) ──────────────


@pytest.mark.asyncio
async def test_headless_interactive_switch_changes_posture(monkeypatch):
    # The SAME session decided under both postures: interactive defers the
    # residual tier; headless keeps the dashboard block-and-wait.
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(permission, "get_session_security", lambda sid: _local_ctx())
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")

    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"})
    assert res["decision"] == "defer"

    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: False)

    async def _fake_prompt(request_id, session_id, timeout):
        return True

    monkeypatch.setattr(permission, "wait_for_permission", _fake_prompt)
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"})
    assert res["decision"] == "allow"  # via the dashboard approval


@pytest.mark.asyncio
async def test_satellite_path_rewrite_survives_the_flip(_interactive, monkeypatch):
    # A Pass-1 path rewrite (satellite sessions) only rides "allow"/"ask" —
    # deferring would lose it and the tool would run against the
    # sandbox-virtual path. The flip must return allow + updated_input.
    from auth.path_policy import PathDecision
    rewritten = {"file_path": "C:/Users/erin/.oto-dock/agents/a/users/d/workspace/x.md"}
    monkeypatch.setattr(
        permission, "check_tool_access",
        lambda tool, args, ctx: (
            PathDecision(allowed=True, updated_input=rewritten), None),
    )
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"})
    assert res["decision"] == "allow"
    assert res["updated_input"] == rewritten


@pytest.mark.asyncio
async def test_viewer_gets_the_same_native_posture(_interactive, monkeypatch):
    # Operator decision: NO role gate — viewers keep interactive terminals
    # with the CLI-native posture. Hard denies still bind them: the residual
    # defers, but a Pass-1 role denial stays a deny.
    monkeypatch.setattr(
        permission, "get_session_security", lambda sid: _local_ctx(role="viewer"))
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/users/dave/workspace/x.md", "content": "x"})
    assert res["decision"] == "defer"
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/knowledge/notes.md", "content": "x"})
    assert res["decision"] == "deny"


@pytest.mark.asyncio
async def test_interactive_task_keeps_the_auto_posture(_interactive, monkeypatch):
    # Interactive TASKS are excluded from the flip structurally: a task
    # client never enters the dashboard branch, so it keeps the unattended
    # auto posture — allow, with the Pass-1 hard-deny floor still binding.
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"})
    assert res["decision"] == "allow"
    res = await permission.decide_tool_permission("s", "Bash", {"command": "rm -rf /"})
    assert res["decision"] == "deny"


@pytest.mark.asyncio
async def test_destructive_bash_carve_out_in_interactive_default(_interactive):
    # The carve-out floor: destructive Bash keeps the platform "ask" (which
    # feeds the CLI's single native prompt) in every live mode.
    res = await permission.decide_tool_permission(
        "s", "Bash", {"command": "rm -rf /workspace/scratch"})
    assert res["decision"] == "ask"


@pytest.mark.asyncio
async def test_exit_plan_mode_defers_to_the_native_dialog(_interactive, monkeypatch):
    # A human-driven interactive session in plan mode: the plan review IS
    # the CLI's native ExitPlanMode dialog. The dashboard plan_review gate
    # (a week-long wait_for_permission on a card) held the hook, froze the
    # TUI and kept the turn open (2026-09-10) — the interactive posture
    # applies here like to every other residual ask.
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "plan")
    res = await permission.decide_tool_permission(
        "s", "ExitPlanMode", {"plan": "1. do it"}, live_permission_mode="plan")
    assert res["decision"] == "defer"


@pytest.mark.asyncio
async def test_native_dialog_tools_park_the_interactive_turn(_interactive, monkeypatch):
    # Both dialog tools park the session's turn from the hook — the
    # deterministic trigger (the transcript line can be journaled late).
    parked = []
    monkeypatch.setattr(permission, "_park_interactive_on_dialog",
                        lambda sid, tool: parked.append((sid, tool)))
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "plan")
    res = await permission.decide_tool_permission(
        "s", "ExitPlanMode", {"plan": "x"}, live_permission_mode="plan")
    assert res["decision"] == "defer"
    res = await permission.decide_tool_permission(
        "s", "AskUserQuestion", {"questions": []})
    assert res["decision"] == "allow"
    assert parked == [("s", "ExitPlanMode"), ("s", "AskUserQuestion")]
    # Codex's picker parks the same way (the hook is deny-only there, so
    # "allow" is the no-op it expects).
    parked.clear()
    res = await permission.decide_tool_permission(
        "s", "request_user_input", {"questions": [{"id": "q1"}]})
    assert res["decision"] == "allow"
    assert parked == [("s", "request_user_input")]
    # A task client never parks (its AskUserQuestion is denied-and-informed).
    parked.clear()
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    await permission.decide_tool_permission("s", "ExitPlanMode", {"plan": "x"})
    await permission.decide_tool_permission("s", "request_user_input", {"questions": []})
    assert parked == []


@pytest.mark.asyncio
async def test_leaving_plan_mode_in_the_tui_persists_the_live_mode(_interactive, monkeypatch):
    # Stored mode 'plan' (the dashboard chose it before the spawn); the
    # human approved the plan natively, so the CLI now reports 'default'.
    # The chat follows to Ask — the bookkeeping the headless plan_review
    # approval did — so a re-warm does not re-enter plan mode.
    recorded = []
    session_state._live_words["s"] = "plan"
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "plan")
    monkeypatch.setattr(permission, "set_session_mode",
                        lambda sid, m: recorded.append((sid, m)))

    async def _no_chat(sid):
        return ""
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _no_chat)
    res = await permission.decide_tool_permission(
        "s", "Read", {"file_path": "/workspace/x.md"}, live_permission_mode="default")
    assert res["decision"] == "allow"
    assert recorded == [("s", "ask")]
    # Still in plan mode (live 'plan'): nothing persisted.
    recorded.clear()
    await permission.decide_tool_permission(
        "s", "Read", {"file_path": "/workspace/x.md"}, live_permission_mode="plan")
    assert recorded == []


@pytest.fixture
def _followed(_interactive, monkeypatch):
    """The chat's side of a terminal's mode change: the session's mode, the
    row writes and the frames to the chat's viewers."""
    from storage import database as task_store
    seen = {"mode": "ask", "rows": [], "frames": []}
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: seen["mode"])
    monkeypatch.setattr(permission, "set_session_mode",
                        lambda sid, m: seen.update(mode=m))

    async def _chat(sid):
        return "chat-1"
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _chat)
    monkeypatch.setattr(task_store, "update_chat",
                        lambda cid, **kw: seen["rows"].append((cid, kw)))
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda cid, frame: seen["frames"].append(frame))
    return seen


async def _read(live: str) -> dict:
    return await permission.decide_tool_permission(
        "s", "Read", {"file_path": "/workspace/x.md"}, live_permission_mode=live)


@pytest.mark.asyncio
async def test_the_chat_follows_a_shift_tab_once_per_change(_followed):
    # Spawned in Ask: the terminal starts in `default`, no change.
    await _read("default")
    assert (_followed["mode"], _followed["rows"], _followed["frames"]) == ("ask", [], [])
    # Shift+Tab to the CLI's auto mode: the chat moves to Auto, the row once,
    # and its viewers get a frame naming the chat.
    await _read("auto")
    await _read("auto")
    assert _followed["mode"] == "autoReview"
    assert _followed["rows"] == [("chat-1", {"permission_mode": "autoReview"})]
    assert _followed["frames"] == [{"type": "mode_changed", "mode": "autoReview", "chat_id": "chat-1"}]
    for live, mode in (("acceptEdits", "autoReview"), ("plan", "plan"),
                       ("bypassPermissions", "dontAsk"), ("default", "ask")):
        await _read(live)
        assert _followed["mode"] == mode, live


@pytest.mark.asyncio
async def test_a_pick_made_after_the_spawn_is_not_a_shift_tab(_followed, monkeypatch):
    """The terminal was spawned in Auto (recorded at the session's
    registration); the chat was then moved to Ask from another surface. The
    terminal still reports `auto`: that is its spawn word, not a change, so
    the chat stays in Ask."""
    session_state._live_words.pop("s", None)
    session_state.register_session_state("s", "autoReview", None)
    assert session_state._live_words["s"] == "auto"
    _followed["mode"] = "ask"
    await _read("auto")
    assert (_followed["mode"], _followed["rows"], _followed["frames"]) == ("ask", [], [])
    # A re-warm of the live session keeps the running terminal's word.
    monkeypatch.setattr(session_state, "session_is_held", lambda sid: True)
    session_state.register_session_state("s", "plan", None)
    assert session_state._live_words["s"] == "auto"


@pytest.mark.asyncio
async def test_after_a_restart_the_first_report_is_recorded_not_followed(_followed):
    """A session re-adopted after a proxy restart has no word recorded: its
    first report is recorded and moves nothing; a later change is followed."""
    session_state._live_words.pop("s", None)
    _followed["mode"] = "ask"
    await _read("auto")
    assert (_followed["mode"], _followed["rows"]) == ("ask", [])
    await _read("plan")
    assert _followed["mode"] == "plan"
    assert _followed["rows"] == [("chat-1", {"permission_mode": "plan"})]


@pytest.mark.asyncio
async def test_a_dont_ask_terminal_reports_default_from_its_spawn(_followed):
    _followed["mode"] = "dontAsk"
    res = await permission.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/x.md", "content": "x"}, live_permission_mode="default")
    assert res["decision"] == "allow"
    assert (_followed["mode"], _followed["rows"]) == ("dontAsk", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,live", [("autoReview", "default"), ("ask", "bypassPermissions"),
                                         ("plan", "default")])
async def test_a_codex_terminal_never_moves_its_chat(_followed, monkeypatch, stored, live):
    """Codex's terminal reports its approval policy (``default`` under
    on-request, ``bypassPermissions`` under never), not a mode the person
    picked: its chat keeps its mode."""
    from core.session import session_manager
    codex = session_manager.get_all_layers()["codex-cli"]
    monkeypatch.setattr(session_manager, "engine_layer_for_session", lambda sid: codex)
    _followed["mode"] = stored
    await _read(live)
    assert (_followed["mode"], _followed["rows"], _followed["frames"]) == (stored, [], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("client,stored", [("task", "auto"), ("task", "autoReview"),
                                           ("dashboard", "judge"), ("dashboard", "auto")])
async def test_only_the_persons_own_terminal_moves_its_chat(_followed, monkeypatch, client, stored):
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: client)
    _followed["mode"] = stored
    await _read("plan")
    assert (_followed["mode"], _followed["rows"], _followed["frames"]) == (stored, [], [])


@pytest.mark.asyncio
async def test_exit_plan_mode_task_client_never_defers(_interactive, monkeypatch):
    # An autonomous interactive TASK has nobody at the dialog: it keeps the
    # non-interactive rule (auto-approve outside the dashboard plan gate).
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "auto")
    res = await permission.decide_tool_permission("s", "ExitPlanMode", {"plan": "x"})
    assert res["decision"] == "allow"
