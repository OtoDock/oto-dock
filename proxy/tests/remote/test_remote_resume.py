"""Regression tests for RemoteExecutionLayer.can_resume_session.

Bug being guarded against: clicking Stop on a remote CLI session set
``info.cli_dead = True``, the dashboard's auto-resume path then called
``prepare_resume`` (popping ``_sessions[session_id]``) BEFORE
``can_resume_session``. The old naive ``can_resume_session`` simply
returned ``True`` when ``info`` existed in the dict — and ``False`` when
it didn't. After ``prepare_resume`` popped the entry, ``can_resume_session``
always returned False, the dashboard fell through to the
fresh-session branch, and the user's chat memory was silently wiped.

Two-part fix being tested here:
1. Order in ``ws/dashboard.py``: ``can_resume_session`` is now called
   BEFORE ``prepare_resume`` so the in-memory info is still available
   for the fast path.
2. ``can_resume_session`` no longer trusts in-memory state alone — it
   RPCs the satellite's ``check_session_resumable`` handler to actually
   stat the JSONL on disk. Also has a fallback path via
   ``resolve_execution_target`` for when ``_sessions`` is empty (idle
   reap / proxy restart) so the bug can't sneak in through any other
   call site.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo


def _make_info(
    session_id: str = "sess-1",
    machine_id: str = "m-1",
    execution_path: str = "claude-code-cli",
) -> RemoteSessionInfo:
    translator = ClaudeCLIEventTranslator(session_id)
    settle = SettleController(session_id, 0, translator)
    info = RemoteSessionInfo(
        session_id=session_id,
        machine_id=machine_id,
        agent_name="agent-1",
        execution_path=execution_path,
        event_queue=asyncio.Queue(),
    )
    info.cli_translator = translator
    info.cli_settle = settle
    return info


def _make_layer() -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._cm.is_connected = MagicMock(return_value=True)
    layer._cm.send_command = AsyncMock()
    layer._sessions = {}
    return layer


@pytest.mark.asyncio
async def test_can_resume_session_rpcs_satellite_when_info_present():
    """Fast path: info is in _sessions → use its machine_id, RPC the satellite."""
    layer = _make_layer()
    info = _make_info()
    layer._sessions[info.session_id] = info
    layer._cm.send_command = AsyncMock(return_value={"resumable": True})

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )

    assert result is True
    layer._cm.send_command.assert_called_once()
    args, kwargs = layer._cm.send_command.call_args
    assert args[0] == "m-1"
    payload = args[1]
    assert payload["type"] == "check_session_resumable"
    assert payload["session_id"] == "sess-1"
    assert payload["agent_slug"] == "agent-1"
    assert payload["username"] == "alice"


@pytest.mark.asyncio
async def test_can_resume_session_returns_false_when_satellite_says_no():
    """RPC returns resumable=False → False. Forces dashboard to fresh-session branch."""
    layer = _make_layer()
    info = _make_info()
    layer._sessions[info.session_id] = info
    layer._cm.send_command = AsyncMock(return_value={"resumable": False})

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is False


@pytest.mark.asyncio
async def test_can_resume_session_falls_back_to_target_resolution():
    """Slow path: _sessions empty (e.g. after prepare_resume or idle reap)
    → derive machine_id from resolve_execution_target. The post-abort
    auto-resume path in ws/dashboard.py reaches this branch when the
    reorder + cache races; the idle-reap path always hits it because the
    reaper already popped _sessions.
    """
    layer = _make_layer()  # _sessions is empty
    layer._cm.send_command = AsyncMock(return_value={"resumable": True})

    with patch("storage.database.get_user_sub_by_username", return_value="user-sub-X"), \
         patch("storage.remote_store.resolve_execution_target",
               return_value=("m-2", None)):
        result = await layer.can_resume_session(
            "sess-missing", agent_name="agent-1", username="alice",
        )

    assert result is True
    args, _ = layer._cm.send_command.call_args
    assert args[0] == "m-2"  # used the resolved target, not _sessions


@pytest.mark.asyncio
async def test_can_resume_session_returns_false_when_target_is_local():
    """If target resolves to 'local', we're not on a satellite — refuse."""
    layer = _make_layer()
    with patch("storage.database.get_user_sub_by_username", return_value=None), \
         patch("storage.remote_store.resolve_execution_target",
               return_value=("local", None)):
        result = await layer.can_resume_session(
            "sess-x", agent_name="agent-1", username="",
        )
    assert result is False
    layer._cm.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_can_resume_session_returns_false_when_satellite_offline():
    """is_connected=False → no RPC, return False so dashboard takes
    fresh-session branch instead of issuing --resume against an
    unreachable satellite (which would silently spawn an empty session
    on reconnect)."""
    layer = _make_layer()
    info = _make_info()
    layer._sessions[info.session_id] = info
    layer._cm.is_connected = MagicMock(return_value=False)

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is False
    layer._cm.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_can_resume_session_returns_false_on_rpc_failure():
    """RPC timeout / connection error → False. Better to spawn fresh than
    issue --resume against an unknown state and risk silent memory loss."""
    layer = _make_layer()
    info = _make_info()
    layer._sessions[info.session_id] = info
    layer._cm.send_command = AsyncMock(side_effect=asyncio.TimeoutError())

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is False


@pytest.mark.asyncio
async def test_can_resume_session_codex_uses_thread_id_no_rpc():
    """Codex execution path: resume keys on codex_thread_id (the Codex CLI
    itself stats .codex/sessions/<thread>.jsonl at spawn time). No RPC."""
    layer = _make_layer()
    info = _make_info(execution_path="codex-cli")
    info.codex_thread_id = "thr-123"
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(
        info.session_id, agent_name="agent-1", username="alice",
    )
    assert result is True
    layer._cm.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_can_resume_session_codex_without_thread_id_returns_false():
    """Codex with no thread_id captured yet → not resumable."""
    layer = _make_layer()
    info = _make_info(execution_path="codex-cli")
    info.codex_thread_id = ""
    layer._sessions[info.session_id] = info

    result = await layer.can_resume_session(info.session_id)
    assert result is False
    layer._cm.send_command.assert_not_called()


# --- _resume_username_for_chat (ws/dashboard.py) -----------------------------
#
# The username handed to can_resume_session decides WHICH .claude/ dir the
# check probes (users/<u>/ vs the shared workspace/). A Shared-only agent's
# human chats mount the agent scope — the JSONL lives under workspace/.claude —
# so passing the viewer's username made the check probe a dir that doesn't
# exist and refuse EVERY resume: each re-warm (satellite restart, proxy
# restart, idle reap) silently rotated the chat to a fresh, context-less
# session. Task chats had the same bug fixed earlier via resolve_task_identity;
# these tests pin down all four identity branches.

from types import SimpleNamespace


def test_resume_username_regular_chat_per_user_agent(monkeypatch):
    """Per-user agent: the viewer's dir holds the session file."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(dashboard._vis, "is_shared_only", lambda name: False)
    assert dashboard._resume_username_for_chat("chat-1", "agent-1", "alice") == "alice"


def test_resume_username_regular_chat_shared_only_agent(monkeypatch):
    """Shared-only agent: human chats mount the agent scope → ''."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard._vis, "is_shared_only", lambda name: name == "shared-bot",
    )
    assert dashboard._resume_username_for_chat("chat-1", "shared-bot", "alice") == ""


def test_resume_username_task_chat_agent_scope(monkeypatch):
    """Agent-scope task: the run's identity (no user dir) wins over the viewer."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard.task_store, "get_run",
        lambda run_id: {"scope": "agent", "created_by": "sub-1"},
    )
    monkeypatch.setattr(
        dashboard, "resolve_task_identity",
        lambda agent, scope, created_by, **kw: SimpleNamespace(username=""),
    )
    assert dashboard._resume_username_for_chat("task-r1", "agent-1", "alice") == ""


def test_resume_username_task_chat_user_scope(monkeypatch):
    """User-scope task: the CREATOR's dir, not the viewer's."""
    import ws.dashboard as dashboard
    monkeypatch.setattr(
        dashboard.task_store, "get_run",
        lambda run_id: {"scope": "user", "created_by": "sub-bob"},
    )
    monkeypatch.setattr(
        dashboard, "resolve_task_identity",
        lambda agent, scope, created_by, **kw: SimpleNamespace(username="bob"),
    )
    assert dashboard._resume_username_for_chat("task-r1", "agent-1", "alice") == "bob"


# ---------------------------------------------------------------------------
# Resume affinity: the layer used for liveness/resume checks must be resolved
# from the chat's PINNED execution_target, never re-resolved from the agent's
# current default. Regression: an agent retargeted local↔remote between
# sessions made the dashboard consult the wrong machine about the old session
# — a live local session read dead, an on-disk resume was refused, and the
# chat lost its context to a fresh reseeded spawn.
# ---------------------------------------------------------------------------


def test_get_execution_layer_pinned_local_wins_over_remote_agent_default(monkeypatch):
    """Pin 'local' → CLI layer even when the agent's default target is remote."""
    from core.session import session_manager as sm
    monkeypatch.setattr(
        sm.agent_store, "get_agent",
        lambda name: {"execution_path": "claude-code-cli"},
    )
    from storage import remote_store
    def _boom(*a, **k):
        raise AssertionError("pinned call must not re-resolve the target")
    monkeypatch.setattr(remote_store, "resolve_execution_target", _boom)
    layer = sm.get_execution_layer(
        "agent-1", user_sub="sub-1", execution_target="local",
    )
    assert layer is sm._cli_layer


def test_get_execution_layer_pinned_remote_wins_over_local_agent_default(monkeypatch):
    """Pin a machine id → remote layer even when the agent now resolves local."""
    from core.session import session_manager as sm
    monkeypatch.setattr(
        sm.agent_store, "get_agent",
        lambda name: {"execution_path": "claude-code-cli"},
    )
    from storage import remote_store
    monkeypatch.setattr(
        remote_store, "resolve_execution_target",
        lambda *a, **k: ("local", ""),
    )
    monkeypatch.setattr(
        remote_store, "get_remote_machine",
        lambda mid: {"pairing_scope": "admin", "registered_by": "sub-a"},
    )
    sentinel = MagicMock()
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: sentinel)
    layer = sm.get_execution_layer(
        "agent-1", user_sub="sub-1", execution_target="m-remote-1",
    )
    assert layer is sentinel


# ---------------------------------------------------------------------------
# Restart fallback must consult the CHAT ROW: a remote CODEX chat resumes by
# thread id and must never be probed through check_session_resumable (a
# .claude JSONL stat that always fails for codex). Regression: after a proxy
# restart, every remote codex resume was refused and the chat silently
# reseeded from DB history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_resume_codex_after_restart_uses_chat_row_thread_id():
    layer = _make_layer()  # _sessions empty (proxy restarted)
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "codex-cli",
        "codex_thread_id": "thread-123",
        "execution_target": "m-win",
    }):
        result = await layer.can_resume_session(
            "sess-codex", agent_name="pa", username="alice",
        )
    assert result is True
    layer._cm.send_command.assert_not_called()  # codex never RPCs the JSONL stat


@pytest.mark.asyncio
async def test_can_resume_codex_after_restart_without_thread_id_refuses():
    layer = _make_layer()
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "codex-cli",
        "codex_thread_id": "",
        "execution_target": "m-win",
    }):
        result = await layer.can_resume_session(
            "sess-codex", agent_name="pa", username="alice",
        )
    assert result is False
    layer._cm.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_can_resume_claude_after_restart_rpcs_the_pinned_machine():
    """A Claude chat row with a pinned machine RPCs THAT machine — not
    whatever the agent's target resolves to today."""
    layer = _make_layer()
    layer._cm.send_command = AsyncMock(return_value={"resumable": True})
    with patch("storage.database.get_chat_by_session", return_value={
        "execution_path": "claude-code-cli",
        "codex_thread_id": "",
        "execution_target": "m-pinned",
    }), patch("storage.remote_store.resolve_execution_target",
              side_effect=AssertionError("must not re-resolve when pinned")):
        result = await layer.can_resume_session(
            "sess-claude", agent_name="pa", username="alice",
        )
    assert result is True
    args, _ = layer._cm.send_command.call_args
    assert args[0] == "m-pinned"
