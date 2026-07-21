"""Warmup agent rebind: the chat row is the agent of record.

Observed 2026-07-09 on the internal install: a post-restart redirect opened
agent A's chat under agent B's URL slug (/chat/B/<chatId-of-A> renders A's
chat — the route param is trusted for the UI shell), and the frontend's
warmup frame carries the URL's agent. Re-warming an EXISTING chat under the
frame's agent would spawn a B session and overwrite A's session binding on
the chat row. ``_handle_warmup`` now rebinds to the chat's stored agent and
re-runs the same access gates against it, fail-closed. New chats (no row)
keep the frame agent.
"""

from unittest.mock import AsyncMock

import pytest

import ws.dashboard  # noqa: F401 — resolves the ws package's circular import
from ws.dashboard_warmup import WarmupController


def _controller(*, accessible: set[str], do_warmup: AsyncMock):
    c = WarmupController.__new__(WarmupController)
    c._can_access_agent = lambda name: name in accessible
    c._send_error = AsyncMock()
    c._do_warmup = do_warmup
    c._warmup_in_flight = False
    c._warmup_task = None
    c.session_id = None
    c.chat_id = None
    return c


def _patch_stores(monkeypatch, *, chat_row, existing_agents):
    from ws import dashboard_warmup as dw
    monkeypatch.setattr(dw.task_store, "get_chat", lambda cid: chat_row)
    monkeypatch.setattr(
        dw.agent_store, "agent_exists", lambda name: name in existing_agents)


@pytest.mark.asyncio
async def test_existing_chat_rebinds_to_chat_agent(monkeypatch):
    do_warmup = AsyncMock(return_value="skip")
    c = _controller(accessible={"agent-a", "agent-b"}, do_warmup=do_warmup)
    _patch_stores(monkeypatch,
                  chat_row={"id": "c1", "agent": "agent-a"},
                  existing_agents={"agent-a", "agent-b"})

    await c._handle_warmup({"agent": "agent-b", "chat_id": "c1"})

    do_warmup.assert_awaited_once()
    assert do_warmup.await_args.args[0] == "agent-a"  # rebound to chat's agent
    c._send_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebind_fails_closed_without_access_to_chat_agent(monkeypatch):
    do_warmup = AsyncMock(return_value="skip")
    # Viewer has access to agent-b only; the chat belongs to agent-a.
    c = _controller(accessible={"agent-b"}, do_warmup=do_warmup)
    _patch_stores(monkeypatch,
                  chat_row={"id": "c1", "agent": "agent-a"},
                  existing_agents={"agent-a", "agent-b"})

    await c._handle_warmup({"agent": "agent-b", "chat_id": "c1"})

    do_warmup.assert_not_awaited()
    c._send_error.assert_awaited_once()
    assert "agent-a" in c._send_error.await_args.args[0]


@pytest.mark.asyncio
async def test_rebind_fails_closed_when_chat_agent_deleted(monkeypatch):
    do_warmup = AsyncMock(return_value="skip")
    c = _controller(accessible={"agent-a", "agent-b"}, do_warmup=do_warmup)
    _patch_stores(monkeypatch,
                  chat_row={"id": "c1", "agent": "agent-a"},
                  existing_agents={"agent-b"})  # agent-a deleted

    await c._handle_warmup({"agent": "agent-b", "chat_id": "c1"})

    do_warmup.assert_not_awaited()
    c._send_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_chat_keeps_frame_agent(monkeypatch):
    do_warmup = AsyncMock(return_value="skip")
    c = _controller(accessible={"agent-b"}, do_warmup=do_warmup)
    _patch_stores(monkeypatch, chat_row=None, existing_agents={"agent-b"})

    await c._handle_warmup({"agent": "agent-b"})  # no chat_id — new chat

    do_warmup.assert_awaited_once()
    assert do_warmup.await_args.args[0] == "agent-b"


@pytest.mark.asyncio
async def test_matching_agent_passes_untouched(monkeypatch):
    do_warmup = AsyncMock(return_value="skip")
    c = _controller(accessible={"agent-a"}, do_warmup=do_warmup)
    _patch_stores(monkeypatch,
                  chat_row={"id": "c1", "agent": "agent-a"},
                  existing_agents={"agent-a"})

    await c._handle_warmup({"agent": "agent-a", "chat_id": "c1"})

    do_warmup.assert_awaited_once()
    assert do_warmup.await_args.args[0] == "agent-a"
    c._send_error.assert_not_awaited()
