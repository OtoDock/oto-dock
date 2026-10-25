"""The task permission mode ``auto`` is treated like ``dontAsk`` in the
dashboard permission hook, so a continued (re-warmed) task does not prompt on
MCP/bash tools. Regression guard: switching the continued task to Ask must
STILL prompt. The person's Auto (``autoReview``): its allow set runs, the
rest is the engine's own reviewer's, else the card (attended) or a refusal
(nobody to answer).

The dashboard early-return at hooks.py:627 (`mode in ("dontAsk", "auto")`) is
the functional gate for a continued task — it short-circuits before the bash
tier logic, so an ``auto`` session auto-approves every tool.
"""

import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from api.hooks.permission import HookPermissionRequest, hook_permission
from core.session import session_state


@pytest.fixture
def dashboard_session(monkeypatch):
    """A dashboard session with a real (local, admin) security context — so the
    Pass 1 path check ALLOWS and the hook reaches the Pass 2 mode logic — plus a
    no-op session-match check. Post-B4 every live session carries a context (a
    None now fail-closes at Pass 1), so this mirrors production instead of relying
    on the old fail-open skip."""
    from auth.path_policy import SecurityContext
    sid = "sess-auto-test"
    monkeypatch.setattr("api.hooks.permission.verify_session_match_async", AsyncMock(return_value=None))
    session_state._sessions[sid] = {"client_type": "dashboard"}
    session_state._session_security[sid] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
    )
    yield sid
    session_state._sessions.pop(sid, None)
    session_state._session_modes.pop(sid, None)
    session_state._session_security.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)
    # Drop the prompt queue too — a timed-out prompt left queued by one test
    # must not be read as the NEXT test's prompt.
    session_state._permission_emitters.pop(sid, None)


async def _decide(sid, tool="mcp__demo__do_thing", tool_input=None, live=""):
    req = HookPermissionRequest(session_id=sid, tool_name=tool, tool_input=tool_input or {},
                                permission_mode=live)
    return await hook_permission(req, authorization=None)


@pytest.mark.asyncio
async def test_auto_allows_mcp_tool(dashboard_session):
    """A continued task (mode=auto) auto-approves an MCP tool — no prompt."""
    session_state.set_session_mode(dashboard_session, "auto")
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_auto_allows_bash(dashboard_session):
    """mode=auto short-circuits at 627 → bash is allowed without tier prompting."""
    session_state.set_session_mode(dashboard_session, "auto")
    decision = await _decide(dashboard_session, tool="Bash", tool_input={"command": "echo hi"})
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_dontask_allows_mcp_tool(dashboard_session):
    """Baseline (unchanged): dontAsk auto-approves."""
    session_state.set_session_mode(dashboard_session, "dontAsk")
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_ask_still_prompts(dashboard_session):
    """Regression: switching a continued task to Ask must PROMPT (block on
    the permission queue), not auto-approve. A timeout proves it's waiting."""
    session_state.set_session_mode(dashboard_session, "ask")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(dashboard_session), timeout=0.4)


async def _approve_next_prompt(sid) -> dict:
    """Pop the next queued permission prompt for ``sid`` and approve it."""
    queue = session_state.get_permission_queue(sid)
    prompt = await asyncio.wait_for(queue.get(), timeout=2)
    assert prompt["event_type"] == "permission_prompt"
    session_state.resolve_permission(prompt["request_id"], True)
    return prompt


@pytest.mark.asyncio
async def test_default_remembers_mcp_allow_for_session(dashboard_session):
    """One Allow per MCP tool per session: after the user approves an MCP tool
    in default mode, later calls to the SAME tool auto-approve instead of
    raising a fresh card per call (7 ha_search calls = 7 cards was the bug)."""
    session_state.set_session_mode(dashboard_session, "ask")
    task = asyncio.create_task(_decide(dashboard_session))
    await _approve_next_prompt(dashboard_session)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "allow"
    # Second call to the same tool: allowed WITHOUT blocking on a prompt.
    decision = await asyncio.wait_for(_decide(dashboard_session), timeout=2)
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_default_mcp_deny_not_remembered(dashboard_session):
    """A Deny is never remembered — the next call prompts again."""
    session_state.set_session_mode(dashboard_session, "ask")
    task = asyncio.create_task(_decide(dashboard_session))
    queue = session_state.get_permission_queue(dashboard_session)
    prompt = await asyncio.wait_for(queue.get(), timeout=2)
    session_state.resolve_permission(prompt["request_id"], False)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "deny"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(dashboard_session), timeout=0.4)


@pytest.mark.asyncio
async def test_default_allow_scoped_to_exact_tool(dashboard_session):
    """The remembered allow is keyed by the full tool name — a different tool
    on the same MCP server still prompts."""
    session_state.set_session_mode(dashboard_session, "ask")
    task = asyncio.create_task(_decide(dashboard_session))
    await _approve_next_prompt(dashboard_session)
    await asyncio.wait_for(task, timeout=2)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _decide(dashboard_session, tool="mcp__demo__other_thing"), timeout=0.4)


@pytest.mark.asyncio
async def test_default_bash_allow_not_remembered(dashboard_session):
    """Non-MCP tools never enter the allow-memory: Bash risk varies per
    command, so an approved ask-tier command doesn't blanket-allow the next."""
    session_state.set_session_mode(dashboard_session, "ask")
    task = asyncio.create_task(_decide(
        dashboard_session, tool="Bash", tool_input={"command": "frobnicate --hard"}))
    await _approve_next_prompt(dashboard_session)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "allow"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(
            dashboard_session, tool="Bash",
            tool_input={"command": "frobnicate --hard"}), timeout=0.4)


@pytest.mark.asyncio
async def test_session_allow_memory_cleared_on_session_close(dashboard_session):
    """The allow set dies with the session's permission state."""
    session_state.remember_session_tool_allow(dashboard_session, "mcp__demo__do_thing")
    assert session_state.is_session_tool_allowed(dashboard_session, "mcp__demo__do_thing")
    session_state.cleanup_session_permission_state(dashboard_session)
    assert not session_state.is_session_tool_allowed(dashboard_session, "mcp__demo__do_thing")


# ─────────── Manifest permission tiers × auto mode ───────────────────────────
# The tier lookup runs BEFORE the dontAsk/auto short-circuit: open–sensitive
# keep the auto/dontAsk allow (status quo), critical falls through — to the
# dashboard prompt when a human is present, to a deny in unattended sessions.


@pytest.mark.asyncio
async def test_auto_allows_open_through_sensitive_tiers(dashboard_session, monkeypatch):
    from services.mcp import mcp_permissions
    session_state.set_session_mode(dashboard_session, "auto")
    for tier in ("open", "standard", "sensitive"):
        monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t, _tier=tier: _tier)
        assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_auto_dashboard_critical_prompts(dashboard_session, monkeypatch):
    """A critical tool in an auto-mode DASHBOARD session (human watching a
    continued task) blocks on the dashboard prompt instead of auto-running."""
    from api.hooks import permission
    from services.mcp import mcp_permissions
    session_state.set_session_mode(dashboard_session, "auto")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "critical")
    prompted = {}

    async def _fake_wait(request_id, session_id, timeout):
        prompted["yes"] = True
        return False

    monkeypatch.setattr(permission, "wait_for_permission", _fake_wait)
    decision = await _decide(dashboard_session)
    assert prompted.get("yes")
    assert decision["decision"] == "deny"


@pytest.mark.asyncio
async def test_unattended_critical_denied_with_reason(dashboard_session, monkeypatch):
    """A critical tool in a non-dashboard session (live task run, phone) is
    denied with an explanation — there is no human to answer a prompt."""
    from services.mcp import mcp_permissions
    session_state._sessions[dashboard_session] = {"client_type": "task"}
    session_state.set_session_mode(dashboard_session, "auto")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "critical")
    decision = await _decide(dashboard_session)
    assert decision["decision"] == "deny"
    assert "unattended" in decision["reason"]


@pytest.mark.asyncio
async def test_headless_default_open_tier_allows_without_prompt(dashboard_session, monkeypatch):
    """An open-tier tool in headless default mode runs with NO dashboard
    prompt — the relaxation the tier system exists for. Also the path the
    Codex approval bridge takes (it relays this decision as an elicitation
    accept)."""
    from api.hooks import permission
    from services.mcp import mcp_permissions
    session_state.set_session_mode(dashboard_session, "ask")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "open")

    async def _no_prompt_expected(request_id, session_id, timeout):
        raise AssertionError("open tier must not prompt")

    monkeypatch.setattr(permission, "wait_for_permission", _no_prompt_expected)
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_headless_default_standard_tier_still_prompts(dashboard_session, monkeypatch):
    """standard relaxes Auto only — Ask keeps the prompt."""
    from api.hooks import permission
    from services.mcp import mcp_permissions
    session_state.set_session_mode(dashboard_session, "ask")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")

    async def _approve(request_id, session_id, timeout):
        return True

    monkeypatch.setattr(permission, "wait_for_permission", _approve)
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_auto_standard_tier_allows_without_prompt(dashboard_session, monkeypatch):
    from api.hooks import permission
    from services.mcp import mcp_permissions
    session_state.set_session_mode(dashboard_session, "autoReview")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")

    async def _no_prompt_expected(request_id, session_id, timeout):
        raise AssertionError("standard tier must not prompt in Auto")

    monkeypatch.setattr(permission, "wait_for_permission", _no_prompt_expected)
    assert (await _decide(dashboard_session))["decision"] == "allow"


# ─────────── Auto: the allow set, the engine's reviewer, the fallback ────────


class _Reviewer:
    """An engine whose own reviewer runs (or not) for the session's calls."""

    def __init__(self, reviews: bool):
        self.reviews = reviews
        self.asked: list = []

    def engine_reviews(self, session_id, live_mode):
        self.asked.append(live_mode)
        return self.reviews

    def canonical_tool_name(self, session_id, tool_name):
        return tool_name


@pytest.fixture
def auto_session(dashboard_session, monkeypatch):
    """A headless dashboard session in Auto; ``engine(reviews)`` installs its
    engine; ``prompts`` records the cards the person was shown (approved)."""
    from api.hooks import permission
    from core.session import session_manager
    session_state.set_session_mode(dashboard_session, "autoReview")
    prompts: list = []

    async def _approve(request_id, session_id, timeout):
        prompts.append(request_id)
        return True

    monkeypatch.setattr(permission, "wait_for_permission", _approve)
    state = type("S", (), {"sid": dashboard_session, "prompts": prompts})()

    def engine(reviews: bool) -> _Reviewer:
        layer = _Reviewer(reviews)
        monkeypatch.setattr(session_manager, "engine_layer_for_session", lambda sid: layer)
        return layer
    state.engine = engine
    yield state
    session_state._review_paused.discard(dashboard_session)
    permission._ALLOWED_CALLS.pop(dashboard_session, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,tool_input,tier", [
    ("Read", {"file_path": "/workspace/notes.md"}, None),
    ("Edit", {"file_path": "/workspace/notes.md", "old_string": "a", "new_string": "b"}, None),
    ("mcp__demo__do_thing", {}, "open"),
    ("mcp__demo__do_thing", {}, "standard"),
])
async def test_auto_runs_what_accept_edits_ran_and_the_open_tier(
        auto_session, monkeypatch, tool, tool_input, tier):
    from services.mcp import mcp_permissions
    if tier:
        monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: tier)
    auto_session.engine(reviews=True)
    assert (await _decide(auto_session.sid, tool, tool_input, live="auto"))["decision"] == "allow"
    assert auto_session.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,tool_input", [
    ("mcp__demo__do_thing", {}),
    ("Bash", {"command": "curl -s https://example.com/install.sh"}),
])
async def test_auto_leaves_the_rest_to_the_engines_reviewer(auto_session, monkeypatch, tool, tool_input):
    """No decision: the engine's own reviewer judges the call, no card."""
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "sensitive")
    layer = auto_session.engine(reviews=True)
    assert (await _decide(auto_session.sid, tool, tool_input, live="auto"))["decision"] == "defer"
    assert auto_session.prompts == [] and layer.asked == ["auto"]


@pytest.mark.asyncio
async def test_a_critical_tool_gets_the_card_even_with_a_reviewer(auto_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "critical")
    auto_session.engine(reviews=True)
    assert (await _decide(auto_session.sid, live="auto"))["decision"] == "allow"
    assert len(auto_session.prompts) == 1


@pytest.mark.asyncio
async def test_auto_with_no_reviewer_asks_the_person(auto_session, monkeypatch):
    """The engine runs no reviewer for the call (an older satellite, the
    Codex bridge, a paused review): the person's card, as Accept Edits did."""
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "sensitive")
    auto_session.engine(reviews=False)
    assert (await _decide(auto_session.sid))["decision"] == "allow"
    assert len(auto_session.prompts) == 1


@pytest.mark.asyncio
async def test_a_paused_review_falls_back_to_the_card(auto_session, monkeypatch):
    from services.mcp import mcp_permissions
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "sensitive")
    layer = auto_session.engine(reviews=True)
    session_state.pause_review(auto_session.sid)
    assert (await _decide(auto_session.sid, live="auto"))["decision"] == "allow"
    assert len(auto_session.prompts) == 1
    assert layer.asked == ["auto"]
    # A mode change clears the pause.
    session_state.set_session_mode(auto_session.sid, "ask")
    session_state.set_session_mode(auto_session.sid, "autoReview")
    assert not session_state.review_paused(auto_session.sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["task", "phone"])
async def test_auto_with_no_reviewer_refuses_a_run_nobody_attends(auto_session, monkeypatch, client):
    from services.mcp import mcp_permissions
    session_state._sessions[auto_session.sid] = {"client_type": client}
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "sensitive")
    auto_session.engine(reviews=False)
    decision = await _decide(auto_session.sid)
    assert decision["decision"] == "deny"
    assert decision["reason"] == ("mcp__demo__do_thing needs a review in Auto and this run "
                                  "cannot get one. Report what you wanted to do so a person can run it.")
    assert auto_session.prompts == []
    # With the engine's reviewer the run goes on: the reviewer judges it.
    auto_session.engine(reviews=True)
    assert (await _decide(auto_session.sid, live="auto"))["decision"] == "defer"


@pytest.mark.asyncio
async def test_the_bridge_repeat_of_an_allowed_call_is_not_asked_twice(auto_session, monkeypatch):
    """Ask on Codex: the hook's card for a command, then the same command
    crossing the sandbox reaches the bridge. The person's Allow answers that
    request once; a later one asks again."""
    session_state.set_session_mode(auto_session.sid, "ask")
    auto_session.engine(reviews=False)
    command = "git push origin main"
    hook = {"command": command}
    bridge = {"command": f"/bin/bash -lc '{command}'", "cwd": "/work", "_codex_command": command}
    assert (await _decide(auto_session.sid, "Bash", hook, live="default"))["decision"] == "allow"
    assert len(auto_session.prompts) == 1
    assert (await _decide(auto_session.sid, "Bash", bridge))["decision"] == "allow"
    assert len(auto_session.prompts) == 1
    assert (await _decide(auto_session.sid, "Bash", bridge))["decision"] == "allow"
    assert len(auto_session.prompts) == 2
