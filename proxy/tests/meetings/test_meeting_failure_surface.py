"""Pre-pump meeting failures must surface into the requesting chat.

``start_meeting()``'s early-exit paths (usage pre-check, participant config
build, admission denial, participant spawn) used to fail the meeting with
only a proxy log line: the moderator's ack turn had already said the meeting
was set up, and the chat kept a live-looking meeting pill forever (found
2026-07-11 filming — a 3-participant meeting vetoed by the live-RAM gate).
``_notify_meeting_failed`` persists a ``meeting_failed`` system event through
a minimal meeting pump and flips the row to failed with the reason as the
summary (shown on the Meetings tabs).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.concurrency import Admission
from core.events.stream_pump import _active_pumps
from services.meetings import meeting_orchestrator as MO
from storage import database as task_store


def _make_meeting(chat_id: str = "chat-mf-1", scope: str = "user",
                  created_by: str | None = None) -> str:
    task_store.create_chat(chat_id, "u-1", "a1")
    m = task_store.create_meeting(
        "mtg-mf-1", "topic", json.dumps(["a1", "a2"]), "a1",
        "directed", 30, chat_id, None, None, scope, created_by,
    )
    return m["id"]


@pytest.mark.asyncio
async def test_notify_persists_event_and_fails_row(monkeypatch):
    monkeypatch.setattr(MO, "_FAIL_PUMP_LINGER_S", 0.2)
    mid = _make_meeting()

    notify = asyncio.create_task(
        MO._notify_meeting_failed(mid, "The platform host is low on memory."))
    # During the linger the failure pump is registered on the chat — that is
    # what the dashboard's idle 3s poll finds to attach and re-send history.
    for _ in range(50):
        if _active_pumps.get("chat-mf-1") is not None:
            break
        await asyncio.sleep(0.01)
    assert _active_pumps.get("chat-mf-1") is not None
    await notify
    # The pump unregisters itself at completion — nothing leaked.
    assert _active_pumps.get("chat-mf-1") is None

    meeting = task_store.get_meeting(mid)
    assert meeting["status"] == "failed"
    assert meeting["summary"] == "The platform host is low on memory."

    rows = task_store.get_chat_messages("chat-mf-1")
    sys_rows = [r for r in rows if r.get("event_type") == "system"]
    assert sys_rows, f"no system event persisted; rows={rows}"
    ed = json.loads(sys_rows[-1]["event_data"])
    assert ed["subtype"] == "meeting_failed"
    assert ed["message"] == "The platform host is low on memory."


@pytest.mark.asyncio
async def test_notify_waits_for_existing_pump(monkeypatch):
    """The usage pre-check fires while the moderator's ack turn may still be
    streaming — the helper must not stomp the live pump's registration."""
    monkeypatch.setattr(MO, "_FAIL_PUMP_LINGER_S", 0.0)
    mid = _make_meeting(chat_id="chat-mf-2")

    done = asyncio.Event()
    fake_pump = SimpleNamespace(
        is_done=False, _task=asyncio.create_task(done.wait()),
    )
    _active_pumps["chat-mf-2"] = fake_pump

    notify = asyncio.create_task(
        MO._notify_meeting_failed(mid, "reason"))
    await asyncio.sleep(0.05)
    # Still waiting on the ack pump — no failure pump registered yet.
    assert _active_pumps["chat-mf-2"] is fake_pump
    fake_pump.is_done = True
    done.set()
    await notify

    # The failure pump ran after the ack pump finished and unregistered itself.
    leftover = _active_pumps.pop("chat-mf-2", None)
    assert leftover is not fake_pump
    rows = task_store.get_chat_messages("chat-mf-2")
    assert any(r.get("event_type") == "system" for r in rows)


def _pending_meeting_row(**over):
    row = {
        "id": "m1", "status": "pending", "parent_chat_id": "chat-x",
        "parent_session_id": "", "scope": "agent", "moderator": "a1",
        "participants": json.dumps(["a1", "a2"]),
        "created_by": None, "topic": "t",
    }
    row.update(over)
    return row


def _cfg(slug):
    return SimpleNamespace(
        execution_target="local", execution_path="claude-code-cli",
        user_sub="", security_context=SimpleNamespace(role="manager"),
    )


@pytest.mark.asyncio
async def test_admission_denial_notifies_with_reason():
    denial = Admission(False, "host_memory",
                       "The platform host is low on memory (123 MB free).")
    notify = AsyncMock()
    with patch.object(MO.task_store, "get_meeting",
                      return_value=_pending_meeting_row()), \
         patch.object(MO.task_store, "get_chat", return_value={}), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO, "build_meeting_agent_config",
                      new=AsyncMock(side_effect=lambda slug, m, sid: _cfg(slug))), \
         patch("core.concurrency.acquire_meeting_slots",
               new=AsyncMock(return_value=denial)), \
         patch.object(MO, "_notify_meeting_failed", new=notify):
        await MO.start_meeting("m1")

    notify.assert_awaited_once_with(
        "m1", "The platform host is low on memory (123 MB free).")


@pytest.mark.asyncio
async def test_config_build_failure_notifies():
    notify = AsyncMock()
    with patch.object(MO.task_store, "get_meeting",
                      return_value=_pending_meeting_row()), \
         patch.object(MO.task_store, "get_chat", return_value={}), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO, "build_meeting_agent_config",
                      new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(MO, "_notify_meeting_failed", new=notify):
        await MO.start_meeting("m1")

    notify.assert_awaited_once()
    assert "failed to prepare participant sessions" in notify.await_args.args[1]


@pytest.mark.asyncio
async def test_usage_limit_block_notifies():
    from services.billing import usage_service
    notify = AsyncMock()
    row = _pending_meeting_row(scope="user", created_by="u-1")
    with patch.object(MO.task_store, "get_meeting", return_value=row), \
         patch.object(MO.task_store, "get_user",
                      return_value={"role": "member"}), \
         patch.object(usage_service, "check_user_limit",
                      return_value={"allowed": False}), \
         patch.object(MO, "_notify_meeting_failed", new=notify):
        await MO.start_meeting("m1")

    notify.assert_awaited_once_with("m1", "User usage limit exceeded")
