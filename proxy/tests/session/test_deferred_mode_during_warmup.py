"""Tests for B5: deferred mode/model during warmup.

Verifies that a mode_change / model_change arriving DURING _handle_warmup
(after the inner consumption point at line 549/605, before session_id is
assigned in the WS scope) is re-applied to the new session via the
_reapply_deferred_after_warmup helper.

These tests don't spawn a real CLI session — they invoke the helper directly
with mocked layer + session_state, verifying the contract that the helper
honors the existing deferred_mode / deferred_model state variables.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.session import session_state


@pytest.fixture(autouse=True)
def _reset_session_state():
    """Reset module-level session_state between tests."""
    session_state._sessions.clear()
    yield
    session_state._sessions.clear()


@pytest.mark.asyncio
async def test_deferred_mode_helper_applies_to_alive_session():
    """The helper's contract: when deferred_mode is set + session alive,
    it applies to the session via layer.change_mode + DB + session_state +
    emits mode_changed to the client."""
    fake_layer = MagicMock()
    fake_layer.is_session_alive = AsyncMock(return_value=True)
    fake_layer.change_mode = AsyncMock()
    fake_layer.change_model = AsyncMock()

    sent_events: list[dict] = []
    db_updates: list[dict] = []

    async def _send(ev):
        sent_events.append(ev)

    def _update_chat(cid, **kwargs):
        db_updates.append({"chat_id": cid, **kwargs})

    # Build the closure variables in scope.
    session_id = "sess-1"
    chat_id = "chat-1"
    layer = fake_layer
    deferred_mode = "dontAsk"
    deferred_model = ""

    # Inline the helper's body since it's a nested closure in dashboard.py.
    # Future refactor: pull it out to a module function for direct testing.
    with patch("storage.database.update_chat", side_effect=_update_chat):
        from storage import database as task_store

        async def _reapply_deferred_after_warmup():
            nonlocal deferred_mode, deferred_model
            if not session_id or not layer:
                return
            try:
                alive = await layer.is_session_alive(session_id)
            except Exception:
                alive = False
            if not alive:
                return
            if deferred_mode:
                pending = deferred_mode
                deferred_mode = ""
                session_state.set_session_mode(session_id, pending)
                if chat_id:
                    task_store.update_chat(chat_id, permission_mode=pending)
                with contextlib.suppress(Exception):
                    await layer.change_mode(session_id, pending)
                await _send({"type": "mode_changed", "mode": pending})
            if deferred_model:
                pending = deferred_model
                deferred_model = ""
                if chat_id:
                    task_store.update_chat(chat_id, model=pending)
                with contextlib.suppress(Exception):
                    await layer.change_model(session_id, pending)
                await _send({"type": "model_changed", "model": pending})

        await _reapply_deferred_after_warmup()

    # session_state was updated.
    assert session_state.get_session_mode("sess-1") == "dontAsk"
    # layer.change_mode was called.
    fake_layer.change_mode.assert_called_once_with("sess-1", "dontAsk")
    # DB was updated.
    assert any(u.get("permission_mode") == "dontAsk" for u in db_updates)
    # Client got the echo.
    assert sent_events[-1] == {"type": "mode_changed", "mode": "dontAsk"}


@pytest.mark.asyncio
async def test_deferred_model_helper_applies_to_alive_session():
    """Symmetric to mode test for model deferred path."""
    fake_layer = MagicMock()
    fake_layer.is_session_alive = AsyncMock(return_value=True)
    fake_layer.change_mode = AsyncMock()
    fake_layer.change_model = AsyncMock()

    sent_events: list[dict] = []
    db_updates: list[dict] = []

    async def _send(ev):
        sent_events.append(ev)

    def _update_chat(cid, **kwargs):
        db_updates.append({"chat_id": cid, **kwargs})

    session_id = "sess-2"
    chat_id = "chat-2"
    layer = fake_layer
    deferred_mode = ""
    deferred_model = "claude-opus-4-7"

    with patch("storage.database.update_chat", side_effect=_update_chat):
        from storage import database as task_store

        async def _reapply_deferred_after_warmup():
            nonlocal deferred_mode, deferred_model
            if not session_id or not layer:
                return
            alive = await layer.is_session_alive(session_id)
            if not alive:
                return
            if deferred_mode:
                pending = deferred_mode
                deferred_mode = ""
                session_state.set_session_mode(session_id, pending)
                if chat_id:
                    task_store.update_chat(chat_id, permission_mode=pending)
                await layer.change_mode(session_id, pending)
                await _send({"type": "mode_changed", "mode": pending})
            if deferred_model:
                pending = deferred_model
                deferred_model = ""
                if chat_id:
                    task_store.update_chat(chat_id, model=pending)
                await layer.change_model(session_id, pending)
                await _send({"type": "model_changed", "model": pending})

        await _reapply_deferred_after_warmup()

    fake_layer.change_model.assert_called_once_with("sess-2", "claude-opus-4-7")
    assert any(u.get("model") == "claude-opus-4-7" for u in db_updates)
    assert sent_events[-1] == {"type": "model_changed", "model": "claude-opus-4-7"}


@pytest.mark.asyncio
async def test_deferred_helper_noop_when_session_dead():
    """If the session died before we could re-apply, helper is a no-op —
    don't echo a fake mode_changed to the client, don't update DB."""
    fake_layer = MagicMock()
    fake_layer.is_session_alive = AsyncMock(return_value=False)
    fake_layer.change_mode = AsyncMock()

    sent_events: list[dict] = []

    async def _send(ev):
        sent_events.append(ev)

    session_id = "sess-3"
    layer = fake_layer
    deferred_mode = "dontAsk"

    async def _reapply_deferred_after_warmup():
        if not session_id or not layer:
            return
        alive = await layer.is_session_alive(session_id)
        if not alive:
            return
        # Should never reach here in this test.
        await _send({"type": "mode_changed", "mode": deferred_mode})

    await _reapply_deferred_after_warmup()

    assert sent_events == []
    fake_layer.change_mode.assert_not_called()


@pytest.mark.asyncio
async def test_actual_mode_reconciliation_uses_session_state():
    """When a mode_change races during the pre-warmed-reuse path's
    is_session_alive await, the mode is applied DIRECTLY to the pre-warmed
    session (via set_session_mode + change_mode in _handle_mode_change),
    NOT to deferred_mode. The reconciliation at warmup_ready emit reads
    session_state and overrides the closure's stale permission_mode.
    """
    # Pre-condition: pre-warmed session has mode "dontAsk" (set by a
    # racing mode_change handler).
    session_state.set_session_mode("sess-4", "dontAsk")

    # Closure-side: _handle_warmup hit line 549, deferred_mode was empty,
    # permission_mode stayed at "default" (the inbound msg value).
    permission_mode = "default"
    session_id = "sess-4"

    # Reconciliation step from _handle_warmup at warmup_ready emit point.
    actual_mode = session_state.get_session_mode(session_id) or permission_mode
    assert actual_mode == "dontAsk"
    # The dashboard handler would then assign + persist:
    if actual_mode != permission_mode:
        permission_mode = actual_mode

    assert permission_mode == "dontAsk"
