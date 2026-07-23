"""Shared-only chats bill the PAYER, not the agent bucket (sender-pays).

A Shared-only agent's human chat mounts agent scope and its chat row is owned
by the synthetic ``agent::{slug}``, but the session runs on the interacting
user's OWN subscription. Both usage writers (the pump and the interactive-
tailer flush) re-attribute such rows to the payer recorded by the session's
subscription binding (``get_session_payer_sub``): real ``user_sub``, scope
``user``. Platform-paid sources (agent-scope acquisitions — tasks, phone,
meetings, service sessions) bind no payer and stay in the agent bucket, so
"agent scope = platform-paid" holds at the recording layer.

Run: cd proxy && venv/bin/pytest tests/billing/test_shared_only_payer_attribution.py -v
"""

import asyncio

import pytest

from core.events.stream_pump import ChatStreamPump
from services.engines import subscription_pool as sp
from storage import agent_store
from storage import database as task_store

_AGENT = "shared-dev"
_PAYER = "local:alice"


@pytest.fixture(autouse=True)
def _shared_agent():
    agent_store.create_agent(
        _AGENT, "Shared Dev", default_scope="agent", collaborative=False,
    )
    yield
    with sp._session_maps_lock:
        sp._session_subscriptions.clear()
        sp._session_binding_ctx.clear()
        sp._session_scope_keys.clear()


def _make_shared_chat(chat_id: str) -> dict:
    task_store.create_chat(chat_id, f"agent::{_AGENT}", _AGENT)
    return task_store.get_chat(chat_id)


class TestGetSessionPayerSub:
    def test_user_scope_binding_returns_payer(self):
        sp.bind_session("sess-u", "sub-1", layer="claude-code-cli", user_sub=_PAYER)
        assert sp.get_session_payer_sub("sess-u") == _PAYER

    def test_agent_scope_binding_has_no_payer(self):
        sp.bind_session("sess-a", "sub-1", layer="claude-code-cli", user_sub="")
        assert sp.get_session_payer_sub("sess-a") == ""

    def test_unbound_session_has_no_payer(self):
        assert sp.get_session_payer_sub("sess-none") == ""

    def test_reads_through_to_persisted_binding(self):
        """Restart survival: the in-memory ctx is gone but the persisted
        binding still names the payer (same read-through contract as
        get_session_subscription)."""
        sp.bind_session("sess-p", "sub-1", layer="claude-code-cli", user_sub=_PAYER)
        with sp._session_maps_lock:
            sp._session_binding_ctx.clear()
            sp._session_subscriptions.clear()
        assert sp.get_session_payer_sub("sess-p") == _PAYER


class TestPumpPayerAttribution:
    def _run_record(self, monkeypatch, session_id: str, chat_id: str) -> list[dict]:
        captured: list[dict] = []
        from services.billing import usage_service
        monkeypatch.setattr(
            usage_service, "record_turn_usage",
            lambda rows: captured.extend(rows) or list(range(len(rows))),
        )

        async def _build_and_record():
            producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
            pump = ChatStreamPump(
                chat_id=chat_id,
                session_id=session_id,
                producer=producer,
                event_queue=asyncio.Queue(),
                perm_queue=None,
                scope="agent",
            )
            pump._llm_cost_delta = 0.01
            pump._input_tokens = 100
            pump._output_tokens = 50
            pump._record_usage(task_store.get_chat(chat_id))
            producer.cancel()

        # asyncio.run(): the deprecated get_event_loop().run_until_complete()
        # raised "no current event loop" (py3.13) whenever an async suite ran
        # earlier in the worker and pytest-asyncio tore its loop down — the
        # order-dependent flake behind the full-suite-only failures here.
        asyncio.run(_build_and_record())
        return captured

    def test_user_paid_session_bills_payer_user_scope(self, monkeypatch):
        chat = _make_shared_chat("chat-payer")
        assert chat["user_sub"] == f"agent::{_AGENT}"
        sp.bind_session("sess-paid", "sub-1", layer="claude-code-cli", user_sub=_PAYER)

        rows = self._run_record(monkeypatch, "sess-paid", "chat-payer")
        assert rows, "LLM row must be written"
        assert rows[0]["user_sub"] == _PAYER
        assert rows[0]["scope"] == "user"
        assert rows[0]["source_key"] == "sub-1"

    def test_platform_paid_session_stays_agent_scope(self, monkeypatch):
        _make_shared_chat("chat-platform")
        sp.bind_session("sess-svc", "sub-1", layer="claude-code-cli", user_sub="")

        rows = self._run_record(monkeypatch, "sess-svc", "chat-platform")
        assert rows[0]["user_sub"] == f"agent::{_AGENT}"
        assert rows[0]["scope"] == "agent"


class TestBatchUsagePayerAttribution:
    def _batch(self):
        return {"claude-sonnet-5": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read": 0, "cache_write": 0,
        }}

    def test_interactive_user_paid_bills_payer(self, monkeypatch):
        from core.session import transcript_tool_events as tte
        from services.billing import usage_service
        captured: list[dict] = []
        monkeypatch.setattr(
            usage_service, "record_turn_usage",
            lambda rows: captured.extend(rows) or list(range(len(rows))),
        )
        _make_shared_chat("chat-tail")
        sp.bind_session("sess-tail", "sub-2", layer="claude-code-cli", user_sub=_PAYER)

        wrote = tte.record_batch_usage("sess-tail", "chat-tail", self._batch(), task_store)
        assert wrote == 1
        assert captured[0]["user_sub"] == _PAYER
        assert captured[0]["scope"] == "user"

    def test_interactive_platform_paid_stays_agent_scope(self, monkeypatch):
        from core.session import transcript_tool_events as tte
        from services.billing import usage_service
        captured: list[dict] = []
        monkeypatch.setattr(
            usage_service, "record_turn_usage",
            lambda rows: captured.extend(rows) or list(range(len(rows))),
        )
        _make_shared_chat("chat-tail-svc")
        sp.bind_session("sess-tail-svc", "sub-2", layer="claude-code-cli", user_sub="")

        wrote = tte.record_batch_usage(
            "sess-tail-svc", "chat-tail-svc", self._batch(), task_store)
        assert wrote == 1
        assert captured[0]["user_sub"] == f"agent::{_AGENT}"
        assert captured[0]["scope"] == "agent"


class TestLastUserMessageAuthor:
    def test_returns_latest_user_author(self):
        _make_shared_chat("chat-authors")
        task_store.add_chat_message("chat-authors", "user", "hi", author_sub="local:alice")
        task_store.add_chat_message("chat-authors", "assistant", "hello")
        task_store.add_chat_message("chat-authors", "user", "next", author_sub="local:bob")
        assert task_store.get_last_user_message_author("chat-authors") == "local:bob"

    def test_empty_chat_returns_blank(self):
        _make_shared_chat("chat-empty")
        assert task_store.get_last_user_message_author("chat-empty") == ""
