"""The chat send's subscription pool cap gate (``ws/dashboard_chat_send.py``
``_pool_cap_gate`` + ``_recycle_if_on_own_oauth``): ``stop`` answers
``limit_reached`` with the pool reading and never warms; a warning rides a
``limit_warning``; ``continue`` with a key warns, lets the turn through and
recycles a warm session bound to one of the sender's own OAuth accounts;
``continue`` without a key blocks.

Run: cd proxy && python -m pytest tests/session/test_ws_dashboard_pool_cap.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import patch

import pytest

from services.billing import pool_caps
from tests.fixtures.ws_dashboard_harness import TEST_MODEL


class _Stop(Exception):
    pass


def _status(*, allowed, on_reached="stop", warning=False):
    s = pool_caps.CapStatus(scope="user", target="user-admin", layer="codex-cli",
                            configured=True, accounts=1, allowed=allowed,
                            on_reached=on_reached, warning=warning or not allowed,
                            hits=[] if allowed else ["week_pct"])
    s.caps["week_pct"], s.readings["week_pct"] = 50.0, 52.0
    return s


@pytest.fixture
def conn(monkeypatch):
    """A ``ChatSendMixin`` stub whose warmup and stream are sentinels."""
    import ws.dashboard  # noqa: F401  # assembles the controller first
    from ws import dashboard_chat_send as mod

    chat_row = {
        "user_sub": "user-admin", "agent": "alpha", "execution_path": "codex-cli",
        "execution_target": "local", "model": TEST_MODEL, "permission_mode": "auto",
    }
    sent: list[dict] = []
    calls: dict = {"warmups": 0, "closed": []}

    class _Layer:
        # The real Codex descriptor: the send path now asks it real questions
        # (attach_images_inline, rebuilds_history_from_db), not just its name.
        from core.layers.codex.layer import _CODEX_CAPABILITIES as capabilities

        def capabilities_for(self, sid):
            # A local engine's descriptor is the same for every session.
            return self.capabilities

        async def is_session_alive(self, sid):
            return True

        async def close_session(self, sid):
            calls["closed"].append(sid)

    class _Conn(mod.ChatSendMixin):
        user_sub = "user-admin"
        user_role = "admin"
        user_agents: set[str] = set()
        user = {"username": "admin"}
        notify_connection_id = "conn-1"
        chat_id = "chat-1"
        agent_name = "alpha"
        session_id = "sess-1"
        layer = _Layer()

        async def _send(self, frame):
            sent.append(frame)

        async def _send_error(self, text):
            sent.append({"type": "error", "message": text})

        async def _deny_task_continue(self, chat_id):
            return False

        async def _handle_warmup(self, msg, background=False):
            calls["warmups"] += 1
            raise _Stop()

        async def _deliver_chat_to_live_interactive(self, msg):
            return False

        async def _prepare_turn_input(self, *a, **k):
            # A stream after the gates is out of scope: stop there.
            raise _Stop()

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(mod, "run_db", _run_db)
    monkeypatch.setattr(mod.task_store, "get_chat", lambda cid: chat_row)
    monkeypatch.setattr(mod.notification_manager, "set_chat_turn_origin",
                        lambda *a, **k: None)
    monkeypatch.setattr(mod._vis, "is_shared_only", lambda agent: False)
    monkeypatch.setattr(mod.interactive_session, "get", lambda sid: None)
    from services.billing import usage_service
    monkeypatch.setattr(usage_service, "check_user_limit",
                        lambda *a, **k: {"allowed": True, "warning": False, "periods": {}})
    return _Conn(), sent, calls


def _drive(conn, status, *, key_available=True, bound=None):
    from services.engines import subscription_pool
    from storage.billing import subscription_store

    async def run():
        with patch.object(pool_caps, "evaluate", return_value=status) as ev, \
             patch.object(subscription_pool, "cap_continue_available",
                          return_value=key_available), \
             patch.object(subscription_pool, "get_session_subscription",
                          return_value="sub-x" if bound else None), \
             patch.object(subscription_store, "get_subscription",
                          return_value=bound), \
             contextlib.suppress(_Stop):
            await conn._handle_chat({"text": "hi", "chat_id": "chat-1"})
        return ev
    return asyncio.run(run())


class TestGate:
    def test_stop_sends_limit_reached_and_returns(self, conn):
        c, sent, calls = conn
        ev = _drive(c, _status(allowed=False))
        ev.assert_called_once_with("user", "user-admin", "codex-cli")
        assert [f["type"] for f in sent] == ["limit_reached"]
        assert sent[0]["pool"]["hits"] == ["week_pct"]
        assert sent[0]["pool"]["readings"]["week_pct"] == 52.0
        assert calls["warmups"] == 0 and calls["closed"] == []

    def test_warning_rides_limit_warning_and_the_turn_runs(self, conn):
        c, sent, calls = conn
        _drive(c, _status(allowed=True, warning=True))
        assert [f["type"] for f in sent] == ["limit_warning"]
        assert sent[0]["pool"]["allowed"] is True
        assert calls["closed"] == []

    def test_allowed_sends_nothing(self, conn):
        c, sent, calls = conn
        _drive(c, _status(allowed=True))
        assert sent == []

    def test_continue_without_a_key_blocks(self, conn):
        c, sent, calls = conn
        _drive(c, _status(allowed=False, on_reached="continue"), key_available=False)
        assert [f["type"] for f in sent] == ["limit_reached"]
        assert calls["closed"] == []

    def test_continue_recycles_a_session_on_an_own_oauth_account(self, conn):
        c, sent, calls = conn
        own = {"id": "sub-x", "auth_type": "oauth", "owner_sub": "user-admin"}
        _drive(c, _status(allowed=False, on_reached="continue"), bound=own)
        assert [f["type"] for f in sent] == ["limit_warning"]
        assert sent[0]["pool"]["hits"] == ["week_pct"]
        assert calls["closed"] == ["sess-1"] and calls["warmups"] == 1
        assert c.session_id is None

    def test_continue_keeps_a_session_already_on_a_key(self, conn):
        c, sent, calls = conn
        key = {"id": "sub-x", "auth_type": "api_key", "owner_sub": "user-admin"}
        _drive(c, _status(allowed=False, on_reached="continue"), bound=key)
        assert [f["type"] for f in sent] == ["limit_warning"]
        assert calls["closed"] == [] and calls["warmups"] == 0

    def test_continue_never_recycles_mid_turn(self, conn, monkeypatch):
        from ws import dashboard_chat_send as mod

        class _Pump:
            is_done = False
        monkeypatch.setitem(mod._active_pumps, "chat-1", _Pump())
        c, sent, calls = conn
        own = {"id": "sub-x", "auth_type": "oauth", "owner_sub": "user-admin"}
        _drive(c, _status(allowed=False, on_reached="continue"), bound=own)
        assert calls["closed"] == []

    def test_gate_failure_never_blocks(self, conn):
        c, sent, calls = conn

        async def run():
            with patch.object(pool_caps, "evaluate", side_effect=RuntimeError("db")), \
                 contextlib.suppress(_Stop):
                await c._handle_chat({"text": "hi", "chat_id": "chat-1"})
        asyncio.run(run())
        assert sent == []

    def test_refused_turn_input_ends_the_turn(self, conn):
        """A send the attachment gate refuses (a viewer's photo into a
        Shared-only chat) was answered with an error frame by the builder;
        the client armed its streaming state on the send, so the handler
        ends the turn it never started instead of leaving the chat busy."""
        c, sent, calls = conn

        async def _refused(*a, **k):
            sent.append({"type": "error", "message": "refused"})
            return None
        c._prepare_turn_input = _refused
        _drive(c, _status(allowed=True))
        assert [f["type"] for f in sent] == ["error", "done"]
        assert sent[-1]["chat_id"] == "chat-1"
        assert calls["warmups"] == 0
