"""Delegation wake recovery.

- A wake whose delivery fails on EVERY rung is stored durably on the parent
  chat (``chats.pending_delegate_wake``) and the terminal badge frame is
  broadcast chat-scoped regardless of the delivery rung.
- The stored wake is claimed atomically (exactly one claimer) and injected on
  the chat's next turn/warmup.
- An interactive-pinned parent routes the one-shot to the interactive
  re-warm (``_rewarm_interactive_and_wake``); '' / '-p' chats keep the
  headless echo path unchanged.
- ``submit_prompt(settle=True)`` arms the settle Enter (echo-quiet + max
  backstop) instead of the warm fixed-gap Enter that large pastes lose.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_delegate_wake_recovery.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from services.scheduler import scheduler
from services.scheduler import delivery
from services.scheduler.scheduler import TaskDefinition
from storage import database as task_store


# The ws rung now has a LIVENESS GATE: queue presence alone no longer routes —
# the target session must be alive. Tests that exercise the ws route register
# a fake alive CLI session alongside their notify queue.
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def _alive_cli_session(sid):
    from core.layers.cli import layer as _cli_layer
    _cli_layer._persistent_sessions[sid] = SimpleNamespace(is_alive=True)
    try:
        yield
    finally:
        _cli_layer._persistent_sessions.pop(sid, None)


def _task() -> TaskDefinition:
    return TaskDefinition(id="task-1", name="sub", agent="pa", prompt="p", scope="agent")


async def _fail(*a, **k):
    return None


@pytest.fixture(autouse=True)
def ledger(monkeypatch):
    """The admission ledger a wake reserves in: room for ten 1000 MB sessions,
    a short wake wait, a fresh condition (it binds to this test's loop)."""
    import config
    import core.concurrency as C
    monkeypatch.setattr(config, "SESSION_EST_HEAVY_MB", 1000)
    monkeypatch.setattr(config, "OTODOCK_MAX_LOCAL_SESSIONS", 0)
    monkeypatch.setattr(config, "MAX_SESSIONS_PER_USER", 0)
    monkeypatch.setattr(config, "ADMISSION_WAKE_WAIT_S", 0.3)
    monkeypatch.setattr(C, "_WAKE_SLICE_S", 0.05)
    for name, value in (("_sessions", {}), ("_session_est", {}), ("_session_added_at", {}),
                        ("_session_owner", {}), ("_line", []), ("_reserved_mb", 0),
                        ("_parked_tasks", 0), ("_budget_mb", 10_000), ("_floor_mb", 100)):
        monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(C, "_cond", asyncio.Condition())
    monkeypatch.setattr(C, "_live_available_mb", lambda: 100_000)

    def fill(n_heavy: int) -> None:
        """Leave room for exactly ``n_heavy`` more sessions (a wake needs two:
        its own and the headroom a person keeps)."""
        monkeypatch.setattr(C, "_budget_mb", C._reserved_mb + 1000 * n_heavy)
    return {"C": C, "fill": fill}


class _SpawnLayer:
    """A headless layer that records the ledger at spawn."""

    def __init__(self, *, raises: bool = False):
        self.raises = raises
        self.spawns: list[dict] = []
        self.cfgs: list = []

    async def can_resume_session(self, sid, agent_name="", username=""):
        return True

    async def is_session_alive(self, sid):
        return False

    async def start_session(self, sid, cfg):
        import core.concurrency as C
        self.spawns.append({"kind": C._sessions.get(sid), "owner": C._session_owner.get(sid)})
        self.cfgs.append(cfg)
        if self.raises:
            raise RuntimeError("spawn failed")


def _stub_spawn(monkeypatch, layer):
    from storage.agents import agent_store
    agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **k: layer)
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "build_session_mcp_config",
                        lambda *a, **k: (None, {}, [], {}, []))

    async def _echo(layer, session_id, chat_id, agent, result_prompt):
        return ""
    monkeypatch.setattr(delivery, "_run_echo_turn_pumped", _echo)


_SID = "11111111-2222-3333-4444-555555555555"


def _member(sub: str, agent: str, role: str = "contributor") -> None:
    """A person who holds the agent (seeded users have no agent rows)."""
    from datetime import datetime, timezone
    from storage.agents import agent_store
    from storage.identity import db_users
    from storage.pg import get_conn
    if not agent_store.get_agent(agent):
        agent_store.create_agent(agent, agent.upper(), collaborative=True, default_scope="user")
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login) "
            "VALUES (%s, %s, %s, 'member', %s, %s) ON CONFLICT DO NOTHING",
            (sub, f"{sub}@test.com", sub, now, now))
        conn.commit()
    db_users.add_user_agent(sub, agent, role, "user-admin")


class TestPendingWakeStore:
    def test_append_and_claim_roundtrip(self, temp_db):
        task_store.create_chat("chat-w", "user-1", "pa")
        assert task_store.append_pending_delegate_wake("chat-w", "wake ONE")
        assert task_store.append_pending_delegate_wake("chat-w", "wake TWO")
        assert task_store.claim_pending_delegate_wake("chat-w") == [
            "wake ONE", "wake TWO",
        ]
        # Claimed exactly once — the second claimer gets nothing.
        assert task_store.claim_pending_delegate_wake("chat-w") == []

    def test_claim_empty_and_missing(self, temp_db):
        task_store.create_chat("chat-e", "user-1", "pa")
        assert task_store.claim_pending_delegate_wake("chat-e") == []
        assert task_store.claim_pending_delegate_wake("no-such-chat") == []

    def test_append_missing_chat_is_noop(self, temp_db):
        assert not task_store.append_pending_delegate_wake("no-such-chat", "w")


class TestWakeAdmission:
    """A wake reserves a chat slot before it spawns, parks with task
    headroom, gives way to a person, and never frees a slot it did not take."""

    def test_oneshot_reserves_before_spawn_as_chat_with_no_owner(self, temp_db, ledger,
                                                                 monkeypatch):
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        task_store.create_chat("chat-r1", "user-1", "pa")
        out = asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-r1"))
        assert out == ""
        assert layer.spawns == [{"kind": "chat", "owner": None}]
        # The spawned session's lifecycle releases it, never the rung.
        assert ledger["C"]._sessions.get(_SID) == "chat"

    def test_rewarm_reserves_before_spawn(self, temp_db, ledger, monkeypatch):
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        task_store.create_chat("chat-r2", "user-1", "pa")
        task_store.update_chat("chat-r2", execution_mode="interactive")
        seen: list = []

        async def _fake_rewarm(layer, session_id, agent, result_prompt,
                               *, chat_id, base_cfg, chat_row):
            seen.append(ledger["C"]._sessions.get(session_id))
            return ""
        monkeypatch.setattr(delivery, "_rewarm_interactive_and_wake", _fake_rewarm)
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-r2")) == ""
        assert seen == ["chat"]

    def test_oneshot_parks_with_task_headroom_and_stores_on_timeout(self, temp_db, ledger,
                                                                    monkeypatch):
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        _member("user-1", "pa")          # a person's chat wakes while they hold the agent
        task_store.create_chat("chat-p1", "user-1", "pa")
        task_store.update_chat("chat-p1", session_id=_SID)

        async def _run():
            C = ledger["C"]
            assert await C.acquire("person", "chat")
            ledger["fill"](1)                       # one slot left: a person's
            await scheduler._do_deliver(_SID, "pa", "THE WAKE", _task(),
                                        chat_id="chat-p1", output_text="OUT")
        asyncio.run(_run())
        assert layer.spawns == []                   # parked, never spawned
        assert set(ledger["C"]._sessions) == {"person"}
        assert task_store.claim_pending_delegate_wake("chat-p1") == ["THE WAKE"]

    def test_parked_wake_gives_way_when_a_person_opens_the_chat(self, temp_db, ledger,
                                                                monkeypatch):
        import config
        from core.session import warmup_registry
        monkeypatch.setattr(config, "ADMISSION_WAKE_WAIT_S", 5.0)
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        task_store.create_chat("chat-g1", "user-1", "pa")

        async def _run():
            ledger["fill"](1)
            wake = asyncio.create_task(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-g1"))
            await asyncio.sleep(0.2)
            assert not wake.done()
            monkeypatch.setitem(warmup_registry._inflight, "chat-g1", object())
            return await asyncio.wait_for(wake, 2.0)
        assert asyncio.run(_run()) is None
        assert layer.spawns == [] and _SID not in ledger["C"]._sessions

    def test_a_failed_spawn_releases_only_its_own_reservation(self, temp_db, ledger,
                                                              monkeypatch):
        layer = _SpawnLayer(raises=True)
        _stub_spawn(monkeypatch, layer)
        monkeypatch.setattr(ledger["C"], "_live_local_sids", set)
        task_store.create_chat("chat-f1", "user-1", "pa")
        with pytest.raises(RuntimeError, match="spawn failed"):
            asyncio.run(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-f1"))
        assert layer.spawns == [{"kind": "chat", "owner": None}]
        assert _SID not in ledger["C"]._sessions    # its own: released
        monkeypatch.setattr(ledger["C"], "_cond", asyncio.Condition())

        # A slot someone else holds under the id: no spawn, and it stays theirs.
        async def _theirs():
            assert await ledger["C"].acquire(_SID, "chat", user_sub="user-1")
            return await scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-f1")
        assert asyncio.run(_theirs()) is None
        assert len(layer.spawns) == 1
        assert ledger["C"]._session_owner.get(_SID) == "user-1"

    def test_oneshot_reads_its_placement_off_the_loop(self, temp_db, ledger, monkeypatch):
        import threading
        from storage import remote_store
        from services.engines import subscription_pool as sp
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        # A user-scope wake spawns on the person's subscription; this test
        # is about the reads, so the pool answers with one.
        monkeypatch.setattr(sp, "resolve_subscription_env", lambda *a, **k: ("sub-T", {}))
        task_store.create_chat("chat-o1", "user-1", "pa")
        on_loop: dict[str, bool] = {}

        def _record(name, fn):
            def _wrapped(*a, **k):
                on_loop[name] = threading.current_thread() is threading.main_thread()
                return fn(*a, **k)
            return _wrapped
        for name in ("resolve_execution_target", "placement_of", "get_target_browser_settings"):
            monkeypatch.setattr(remote_store, name, _record(name, getattr(remote_store, name)))
        monkeypatch.setattr(task_store, "get_username_by_sub",
                            _record("get_username_by_sub", task_store.get_username_by_sub))
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub="user-admin", role="viewer",
            chat_id="chat-o1")) == ""
        assert on_loop and not any(on_loop.values()), on_loop

    def test_standing_rechecked_after_the_park(self, temp_db, ledger, monkeypatch):
        from storage.identity import db_users
        from storage.pg import get_conn
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        db_users.add_user_agent("user-viewer", "pa", "contributor", "user-admin")
        task_store.create_chat("chat-s1", "user-viewer", "pa")

        async def _run():
            C = ledger["C"]
            assert await C.acquire("person", "chat")
            ledger["fill"](1)
            wake = asyncio.create_task(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub="user-viewer", role="contributor",
                chat_id="chat-s1"))
            await asyncio.sleep(0.1)
            assert not wake.done()                         # parked
            with get_conn() as conn:                       # removed while it waits
                conn.execute("DELETE FROM user_agents WHERE sub='user-viewer' AND agent='pa'")
                conn.commit()
            C.release("person")
            return await asyncio.wait_for(wake, 2.0)
        monkeypatch.setattr(ledger["C"], "_live_local_sids", set)
        assert asyncio.run(_run()) is None
        assert layer.spawns == [] and _SID not in ledger["C"]._sessions

    def test_oneshot_builds_the_mcp_config_in_the_engine_format(self, temp_db, ledger,
                                                                monkeypatch):
        # A Codex session reads its MCP servers from TOML: a JSON config made
        # the resumed session fail its config validation and never start.
        from services.mcp import mcp_registry
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        formats: dict[str, object] = {}

        def _capture(*a, **k):
            formats[k.get("mcp_config_format", "<default>")] = True
            return None, {}, [], {}, []
        monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _capture)
        for chat_id, path in (("chat-fx", "codex-cli"), ("chat-fy", "claude-code-cli")):
            task_store.create_chat(chat_id, "user-1", "pa")
            task_store.update_chat(chat_id, execution_path=path)
            monkeypatch.setattr(ledger["C"], "_cond", asyncio.Condition())
            asyncio.run(scheduler._deliver_via_oneshot(
                f"sess-{chat_id}", "pa", "wake!", user_sub=None, role="manager",
                chat_id=chat_id))
        assert set(formats) == {"toml", "json"}

    def test_continuation_wake_parks_then_returns_none(self, temp_db, ledger, monkeypatch):
        from core.session import session_delivery
        from services.scheduler import firing
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        task_store.create_chat("chat-c1", "user-1", "pa")
        task_store.update_chat("chat-c1", session_id=_SID)
        paths: list[str] = []
        real = session_delivery.deliver_prompt

        async def _spy(*a, **k):
            out = await real(*a, **k)
            paths.append(out.path)
            return out
        monkeypatch.setattr(session_delivery, "deliver_prompt", _spy)
        cont = TaskDefinition(id="cont-1", name="later", agent="pa", prompt="check back",
                              scope="agent", target_chat_id="chat-c1")

        async def _run():
            ledger["fill"](1)
            await firing._fire_continuation(cont)
        asyncio.run(_run())
        assert paths == ["none"] and layer.spawns == []


class TestFailedDeliveryRecovery:
    def test_failed_delivery_stores_wake(self, temp_db, monkeypatch):
        _member("user-1", "pa")          # a person's chat wakes while they hold the agent
        task_store.create_chat("chat-f", "user-1", "pa")
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _fail)

        asyncio.run(scheduler._do_deliver(
            "sess-f", "pa", "THE WAKE PROMPT", _task(),
            chat_id="chat-f", output_text="OUT",
        ))

        assert task_store.claim_pending_delegate_wake("chat-f") == [
            "THE WAKE PROMPT",
        ]

    def test_a_raising_rung_stores_the_wake(self, temp_db, monkeypatch):
        """A rung that raises (a broken spawn, a config or DB error) never
        reaches the ladder's hook: the wake is stored all the same, and the
        event row is on the chat once."""
        _member("user-1", "pa")
        task_store.create_chat("chat-rx", "user-1", "pa")

        async def _raise(*a, **k):
            raise RuntimeError("config build failed")
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _raise)
        asyncio.run(scheduler._do_deliver("sess-rx", "pa", "THE WAKE", _task(),
                                          chat_id="chat-rx", output_text="OUT"))
        assert task_store.claim_pending_delegate_wake("chat-rx") == ["THE WAKE"]
        assert len([m for m in task_store.get_chat_messages("chat-rx")
                    if m.get("event_type") == "delegate_result"]) == 1

    def test_a_raising_continuation_delivery_stores_the_wake(self, temp_db, monkeypatch):
        from core.session import session_delivery
        from services.scheduler import firing
        _member("user-1", "pa")
        task_store.create_chat("chat-cx", "user-1", "pa")

        async def _raise(chat_id, text, **kw):
            kw["persist_event"](chat_id)
            raise RuntimeError("spawn failed")
        monkeypatch.setattr(session_delivery, "deliver_prompt", _raise)
        cont = TaskDefinition(id="cont-x", name="later", agent="pa", prompt="CHECK BACK",
                              scope="user", created_by="user-1", target_chat_id="chat-cx")
        asyncio.run(firing._fire_continuation(cont))
        assert task_store.claim_pending_delegate_wake("chat-cx") == ["CHECK BACK"]
        assert len([m for m in task_store.get_chat_messages("chat-cx")
                    if m.get("event_type") == "schedule_wake"]) == 1

    def test_failed_delivery_broadcasts_badge_frame(self, temp_db, monkeypatch):
        """The terminal delegate_result frame reaches a viewer's notify queue
        even when the wake delivery lands on the none rung — the queue here is
        a BYSTANDER socket (registered under an unrelated session id), which
        rung 2 can't select, so only the chat-scoped broadcast explains it."""
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-b", "user-1", "pa")
        monkeypatch.setattr(delivery, "_deliver_via_persistent", _fail)
        monkeypatch.setattr(delivery, "_deliver_via_oneshot", _fail)
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-bystander"] = q
        try:
            asyncio.run(scheduler._do_deliver(
                "sess-b", "pa", "wake", _task(),
                chat_id="chat-b", output_text="OUT", status="failed",
            ))
            frames = []
            while not q.empty():
                frames.append(q.get_nowait())
            ui = [f for f in frames if f.get("type") == "chat_ui_frame"]
            assert len(ui) == 1
            assert ui[0]["chat_id"] == "chat-b"
            assert ui[0]["frame"]["type"] == "delegate_result"
            assert ui[0]["frame"]["task_id"] == "task-1"
            assert ui[0]["frame"]["status"] == "failed"
        finally:
            _dashboard_notify_queues.pop("sess-bystander", None)

    def test_ws_delivery_stores_no_wake(self, temp_db, monkeypatch):
        """A wake that DID deliver (ws rung) must not be double-stored."""
        from core.session.session_state import _dashboard_notify_queues
        task_store.create_chat("chat-ok", "user-1", "pa")
        q: asyncio.Queue = asyncio.Queue()
        _dashboard_notify_queues["sess-ok"] = q
        try:
            with _alive_cli_session("sess-ok"):
                asyncio.run(scheduler._do_deliver(
                    "sess-ok", "pa", "wake", _task(),
                    chat_id="chat-ok", output_text="OUT",
                ))
            assert task_store.claim_pending_delegate_wake("chat-ok") == []
        finally:
            _dashboard_notify_queues.pop("sess-ok", None)


class TestHandbackStore:
    def test_handback_none_stores_the_wake(self, temp_db, monkeypatch):
        # Queued on a live PTY that then closed before injecting: the
        # hand-back re-runs the ladder, and a failure there must still leave
        # the wake on the chat.
        from core.session import session_delivery
        _member("user-1", "pa")
        task_store.create_chat("chat-h1", "user-1", "pa")
        hooks: list = []

        async def _fake(chat_id, text, **kw):
            hooks.append(kw["on_outcome"])
            outcome = session_delivery.DeliveryOutcome(
                "pty", chat_id=chat_id, session_id="sess-h1")
            res = kw["on_outcome"](outcome)
            if asyncio.iscoroutine(res):
                await res
            return outcome
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)

        async def _run():
            await scheduler._do_deliver("sess-h1", "pa", "WAKE", _task(),
                                        chat_id="chat-h1", output_text="OUT")
            assert task_store.claim_pending_delegate_wake("chat-h1") == []
            res = hooks[0](session_delivery.DeliveryOutcome(
                "none", chat_id="chat-h1", session_id="sess-h1"))
            if asyncio.iscoroutine(res):
                await res
        asyncio.run(_run())
        assert task_store.claim_pending_delegate_wake("chat-h1") == ["WAKE"]


class TestInteractiveRouting:
    def _fake_layer(self, captured: dict):
        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return True

            async def start_session(self, sid, cfg):
                captured["headless_cfg"] = cfg

            def session_lock(self, sid):
                class _Lock:
                    async def __aenter__(self):
                        return None

                    async def __aexit__(self, *a):
                        return False
                return _Lock()

            async def send_message(self, sid, prompt):
                from core.events.common_events import CommonEvent, TEXT
                yield CommonEvent(type=TEXT, data={"content": "ok"})
        return _FakeLayer()

    def _stub_env(self, monkeypatch, captured: dict):
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True,
                                 default_scope="user")
        from core.session import session_manager
        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda *a, **k: self._fake_layer(captured))
        from services.mcp import mcp_registry
        monkeypatch.setattr(mcp_registry, "build_session_mcp_config",
                            lambda *a, **k: (None, {}, [], {}, []))

    def test_interactive_pin_routes_to_rewarm(self, temp_db, monkeypatch):
        captured: dict = {}
        self._stub_env(monkeypatch, captured)
        task_store.create_chat("chat-i", "user-1", "pa")
        task_store.update_chat("chat-i", execution_mode="interactive")

        rewarm_calls: list[dict] = []

        async def _fake_rewarm(layer, session_id, agent, result_prompt,
                               *, chat_id, base_cfg, chat_row):
            rewarm_calls.append({
                "chat_id": chat_id, "prompt": result_prompt,
                "interactive": base_cfg.interactive, "resume": base_cfg.resume,
            })
            return ""
        monkeypatch.setattr(delivery, "_rewarm_interactive_and_wake", _fake_rewarm)

        out = asyncio.run(scheduler._deliver_via_oneshot(
            "11111111-2222-3333-4444-555555555555", "pa", "wake!",
            user_sub=None, role="manager", chat_id="chat-i",
        ))
        assert out == ""
        assert len(rewarm_calls) == 1
        assert rewarm_calls[0]["chat_id"] == "chat-i"
        assert rewarm_calls[0]["prompt"] == "wake!"
        assert rewarm_calls[0]["resume"] is True
        # The headless spawn must NOT have run — routing happened before it.
        assert "headless_cfg" not in captured

    def test_headless_chat_keeps_echo_path(self, temp_db, monkeypatch):
        captured: dict = {}
        self._stub_env(monkeypatch, captured)
        task_store.create_chat("chat-h", "user-1", "pa")  # execution_mode ''

        echo_calls: list[str] = []

        async def _fake_echo(layer, session_id, chat_id, agent, result_prompt):
            echo_calls.append(chat_id)
            return ""
        monkeypatch.setattr(delivery, "_run_echo_turn_pumped", _fake_echo)

        out = asyncio.run(scheduler._deliver_via_oneshot(
            "11111111-2222-3333-4444-555555555555", "pa", "wake!",
            user_sub=None, role="manager", chat_id="chat-h",
        ))
        assert out == ""
        assert echo_calls == ["chat-h"]
        assert captured["headless_cfg"].resume is True


class TestRewarmInteractiveAndWake:
    class _FakeIsess:
        def __init__(self, *, opens_on_submit=True):
            self.alive = True
            self.has_viewer = False
            self.turn_open = False
            self.opens_on_submit = opens_on_submit
            self.on_turn_complete = None
            self.submitted: list[tuple[str, bool]] = []

        def submit_prompt(self, text, *, settle=False):
            self.submitted.append((text, settle))
            if not self.opens_on_submit:
                return  # the paste never landed — turn_open stays False
            # A landed submit opens the turn (transcript-derived) and the
            # tailer fires turn-complete once the wake turn ends.
            self.turn_open = True
            if self.on_turn_complete:
                self.on_turn_complete("done")

    def _run(self, monkeypatch, isess, *, start_raises=False):
        from core.session import interactive_session
        from core.execution_layer import AgentConfig

        closed: list[str] = []

        class _FakeLayer:
            async def start_session(self, sid, cfg):
                if start_raises:
                    raise RuntimeError("spawn failed")

        monkeypatch.setattr(interactive_session, "get", lambda sid: isess)
        monkeypatch.setattr(delivery, "_WAKE_TURN_OPEN_S", 1.0)

        async def _fake_close(sid, reason=""):
            closed.append(reason)
        monkeypatch.setattr(interactive_session, "close_session", _fake_close)

        cfg = AgentConfig(agent_name="pa", user_sub="", system_prompt="",
                          mcp_config_path="", permission_mode="auto",
                          client_type="", resume=True)
        chat_row = {"execution_mode": "interactive", "tui_theme": "dark"}
        out = asyncio.run(scheduler._rewarm_interactive_and_wake(
            _FakeLayer(), "sess-r", "pa", "WAKE",
            chat_id="chat-r", base_cfg=cfg, chat_row=chat_row,
        ))
        return out, cfg, closed

    def test_wake_submitted_settle_terminal_left_to_reaper(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        out, cfg, closed = self._run(monkeypatch, isess)
        assert out == ""
        assert cfg.interactive is True
        assert cfg.interactive_theme == "dark"
        # Chat binding — the tailer persistence, turn signals and the
        # dashboard's pty_attach guard all key on it.
        assert cfg.chat_id == "chat-r"
        assert isess.submitted == [("WAKE", True)]
        # The finished terminal stays up for the idle reaper's window so a
        # user opening the chat shortly after still attaches to it.
        assert closed == []

    def test_viewer_attached_keeps_session_live(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        isess.has_viewer = True
        out, _cfg, closed = self._run(monkeypatch, isess)
        assert out == ""
        assert closed == []  # the live terminal belongs to the viewer now

    def test_unopened_turn_stores_for_replay(self, temp_db, monkeypatch):
        # The paste never submits: after one bare-Enter nudge the wake is NOT
        # counted delivered — None routes it to the durable pending store.
        isess = self._FakeIsess(opens_on_submit=False)
        out, _cfg, closed = self._run(monkeypatch, isess)
        assert out is None
        assert isess.submitted == [("WAKE", True), ("", True)]
        assert closed == ["delegate_wake_unsubmitted"]

    def test_spawn_failure_returns_none(self, temp_db, monkeypatch):
        isess = self._FakeIsess()
        out, _cfg, closed = self._run(monkeypatch, isess, start_raises=True)
        assert out is None
        assert isess.submitted == []


class TestSettleSubmit:
    class _FakePty:
        closed = False

        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    @pytest.mark.asyncio
    async def test_settle_arms_deferred_enter(self):
        from core.session import interactive_session as isess_mod
        s = isess_mod.InteractiveSession(
            session_id="st-1", chat_id="c", agent_name="agent")
        s.pty = self._FakePty()
        s._ready = True
        s._submitted_once = True  # warm session — the bug's precondition

        s.submit_prompt("line one\nline two", settle=True)

        joined = b"".join(s.pty.writes)
        assert b"line one\nline two" in joined
        # The body went out WITHOUT an immediate trailing Enter …
        assert not joined.endswith(b"\r")
        # … because the Enter is armed on the settle machinery instead.
        assert s._submit_settle_handle is not None
        assert s._submit_max_handle is not None
        s._cancel_deferred_submit()

    @pytest.mark.asyncio
    async def test_plain_submit_unchanged_for_user_path(self):
        from core.session import interactive_session as isess_mod
        s = isess_mod.InteractiveSession(
            session_id="st-2", chat_id="c", agent_name="agent")
        s.pty = self._FakePty()
        s._ready = True
        s._submitted_once = True

        s.submit_prompt("hello", settle=False)

        # Body written; the fixed-gap Enter is scheduled (not the settle arm).
        assert any(b"hello" in w for w in s.pty.writes)
        assert s._submit_settle_handle is None
        s._cancel_deferred_submit()


class TestHeadlessChokepointReplay:
    """A wake stored while the chat was dead rides the chat's NEXT headless
    turn — prepended ahead of the user's prompt at the _start_new_stream
    chokepoint (same claim shape as the history seed)."""

    def test_pending_wake_prepended_to_next_turn(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from tests.fixtures.ws_dashboard_harness import (
            FakeExecutionLayer, dashboard_connection, drain_startup,
            make_test_agent, run_ws_scenario, session_cookie, set_username,
            stub_dashboard_seams, warm_new_chat,
        )

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"text": "ok"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                task_store.append_pending_delegate_wake(chat_id, "PENDING WAKE X")
                ws.client_send({"type": "chat", "text": "user message"})
                for _ in range(40):
                    frame = await ws.next_frame()
                    if frame.get("type") == "done":
                        return
                raise AssertionError("turn never completed")

        run_ws_scenario(scenario)
        sent_prompt = layer.messages[-1][1]
        assert "PENDING WAKE X" in sent_prompt
        assert sent_prompt.index("PENDING WAKE X") < sent_prompt.index("user message")


class TestRedeliverPendingWakes:
    """Startup / satellite-reconnect sweep (``scheduler.redeliver_pending_wakes``)."""

    def _fake_ladder(self, monkeypatch, outcomes: list, path="oneshot"):
        from core.session import session_delivery

        async def _fake(chat_id, text, **kw):
            outcomes.append((chat_id, text, kw.get("user_sub"), kw.get("role")))
            outcome = session_delivery.DeliveryOutcome(
                path=path, response=None, chat_id=chat_id, session_id="",
            )
            # The real ladder runs the caller's hook with the final outcome.
            hook = kw.get("on_outcome")
            if hook is not None:
                res = hook(outcome)
                if asyncio.iscoroutine(res):
                    await res
            return outcome
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)

    def test_sweep_claims_and_delivers_all_wakes_joined(self, temp_db, monkeypatch):
        _member("user-1", "pa")
        task_store.create_chat("chat-s1", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-s1", "W1")
        task_store.append_pending_delegate_wake("chat-s1", "W2")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 1
        assert delivered[0][0] == "chat-s1"
        assert delivered[0][1] == "W1\n\nW2"
        # Claimed — nothing left for a second sweep.
        assert task_store.claim_pending_delegate_wake("chat-s1") == []

    def test_failed_delivery_repersists_original_wakes(self, temp_db, monkeypatch):
        _member("user-1", "pa")
        task_store.create_chat("chat-s2", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-s2", "A")
        task_store.append_pending_delegate_wake("chat-s2", "B")
        self._fake_ladder(monkeypatch, [], path="none")

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 0
        assert task_store.claim_pending_delegate_wake("chat-s2") == ["A", "B"]

    def test_parked_mode_c_chat_is_skipped(self, temp_db, monkeypatch):
        from services.scheduler import run_recovery
        task_store.create_chat("chat-s3", "user-1", "pa")
        task_store.update_chat("chat-s3", session_id="sid-parked")
        task_store.append_pending_delegate_wake("chat-s3", "W")
        run_recovery._parked["sid-parked"] = {"chat_id": "chat-s3"}
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)
        try:
            woken = asyncio.run(scheduler.redeliver_pending_wakes())
        finally:
            run_recovery._parked.clear()
        assert woken == 0 and delivered == []
        # Wake untouched — the post-adopt machine pass claims it later.
        assert task_store.claim_pending_delegate_wake("chat-s3") == ["W"]

    def test_machine_scope_targets_only_that_machines_chats(self, temp_db, monkeypatch):
        _member("user-1", "pa")
        task_store.create_chat("chat-m1", "user-1", "pa")
        task_store.update_chat("chat-m1", execution_target="mach-A")
        task_store.append_pending_delegate_wake("chat-m1", "WA")
        task_store.create_chat("chat-m2", "user-1", "pa")
        task_store.update_chat("chat-m2", execution_target="mach-B")
        task_store.append_pending_delegate_wake("chat-m2", "WB")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        woken = asyncio.run(scheduler.redeliver_pending_wakes(machine_id="mach-A"))
        assert woken == 1
        assert [d[0] for d in delivered] == ["chat-m1"]
        assert task_store.claim_pending_delegate_wake("chat-m2") == ["WB"]

    def test_sweep_shared_owner_delivers_without_a_user_as_viewer(self, temp_db, monkeypatch):
        task_store.create_chat("chat-sh1", "agent::pa", "pa")
        task_store.append_pending_delegate_wake("chat-sh1", "W")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)
        asyncio.run(scheduler.redeliver_pending_wakes())
        assert delivered[0][2] is None and delivered[0][3] == "viewer"

    def test_sweep_drops_the_wakes_of_an_owner_who_lost_the_agent(self, temp_db, monkeypatch):
        task_store.create_chat("chat-lost", "user-viewer", "pa")   # no row on "pa"
        task_store.append_pending_delegate_wake("chat-lost", "W")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)
        assert asyncio.run(scheduler.redeliver_pending_wakes()) == 0
        assert delivered == []
        assert task_store.claim_pending_delegate_wake("chat-lost") == []

    def test_task_owner_chats_deliver_agent_scoped(self, temp_db, monkeypatch):
        task_store.create_chat("chat-t1", "task::sub-x", "pa")
        task_store.append_pending_delegate_wake("chat-t1", "W")
        delivered: list = []
        self._fake_ladder(monkeypatch, delivered)

        asyncio.run(scheduler.redeliver_pending_wakes())
        assert delivered[0][2] is None  # user_sub
        assert delivered[0][3] == "manager"  # role

    def test_raising_delivery_repersists_and_continues(self, temp_db, monkeypatch):
        # Live-hit on T1 (2026-07-19): a corrupt user config.toml made the
        # oneshot spawn raise — the sweep died and the CLAIMED wake was lost.
        from core.session import session_delivery
        _member("user-1", "pa")
        task_store.create_chat("chat-x1", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-x1", "LOST?")
        task_store.create_chat("chat-x2", "user-1", "pa")
        task_store.append_pending_delegate_wake("chat-x2", "NEXT")
        calls: list = []

        async def _boom_then_ok(chat_id, text, **kw):
            calls.append(chat_id)
            if chat_id == "chat-x1":
                raise RuntimeError("spawn failed")
            return session_delivery.DeliveryOutcome(
                path="oneshot", response=None, chat_id=chat_id, session_id="",
            )
        monkeypatch.setattr(session_delivery, "deliver_prompt", _boom_then_ok)

        woken = asyncio.run(scheduler.redeliver_pending_wakes())
        assert woken == 1
        assert set(calls) == {"chat-x1", "chat-x2"}
        assert task_store.claim_pending_delegate_wake("chat-x1") == ["LOST?"]
        assert task_store.claim_pending_delegate_wake("chat-x2") == []

    def test_sweep_runs_chats_concurrently_bounded(self, temp_db, monkeypatch):
        # A chat whose wake waits (a full box) holds up nobody else's.
        from core.session import session_delivery
        for i in range(6):
            task_store.create_chat(f"chat-k{i}", "task::sub-x", "pa")
            task_store.append_pending_delegate_wake(f"chat-k{i}", f"W{i}")

        async def _run():
            gate = asyncio.Event()
            first: list[str] = []
            done: list[str] = []
            flight = {"now": 0, "peak": 0}

            async def _fake(chat_id, text, **kw):
                flight["now"] += 1
                flight["peak"] = max(flight["peak"], flight["now"])
                try:
                    if not first:
                        first.append(chat_id)
                        await gate.wait()
                    else:
                        await asyncio.sleep(0.01)
                finally:
                    flight["now"] -= 1
                done.append(chat_id)
                return session_delivery.DeliveryOutcome(
                    path="oneshot", response=None, chat_id=chat_id, session_id="")
            monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)
            sweep = asyncio.create_task(scheduler.redeliver_pending_wakes())
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(done) == 5:
                    break
            assert len(done) == 5 and first[0] not in done
            gate.set()
            return await sweep, flight["peak"]
        woken, peak = asyncio.run(_run())
        assert woken == 6 and peak <= 4

    def test_oneshot_resolves_chat_pinned_layer(self, temp_db, monkeypatch):
        # Live-hit on T1 (2026-07-19): agent default codex + chat pinned
        # claude-code-cli → the oneshot resolved the CODEX layer, whose
        # resumability pre-check refused the claude session every time.
        from core.session import session_manager
        from storage import remote_store

        task_store.create_chat("chat-l1", "user-1", "pa")
        task_store.update_chat("chat-l1", execution_path="claude-code-cli")
        seen = {}

        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return False

        def _gel(agent, execution_path="", **kw):
            seen["path"] = execution_path
            return _FakeLayer()

        monkeypatch.setattr(session_manager, "get_execution_layer", _gel)
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda agent, user_sub, role: ("local", ""))

        res = asyncio.run(scheduler._deliver_via_oneshot(
            "sess-l1", "pa", "hello", user_sub=None, role="manager",
            chat_id="chat-l1",
        ))
        assert res is None
        assert seen["path"] == "claude-code-cli"

    def test_oneshot_carries_chat_pinned_model(self, temp_db, monkeypatch):
        # Same T1 live-hit family: empty cfg.model falls back to the AGENT
        # default model, which belongs to the other layer → API 400.
        from core.session import session_manager
        from storage import remote_store

        task_store.create_chat("chat-m5", "user-1", "pa")
        task_store.update_chat("chat-m5", execution_path="claude-code-cli",
                               model="claude-sonnet-5")
        seen = {}

        class _FakeLayer:
            async def can_resume_session(self, sid, agent_name="", username=""):
                return True

            async def start_session(self, sid, cfg):
                seen["model"] = cfg.model
                raise RuntimeError("stop-after-cfg")

        monkeypatch.setattr(session_manager, "get_execution_layer",
                            lambda agent, **kw: _FakeLayer())
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda agent, user_sub, role: ("local", ""))

        with pytest.raises(RuntimeError, match="stop-after-cfg"):
            asyncio.run(scheduler._deliver_via_oneshot(
                "sess-m5", "pa", "hello", user_sub=None, role="manager",
                chat_id="chat-m5",
            ))
        assert seen["model"] == "claude-sonnet-5"


class TestWakeSeat:
    """A wake's session draws on a subscription like every other spawn: the
    seat is taken before the start (the person's own for a user-scope wake,
    the platform pool otherwise), bound to the session by the layer, and
    given back when nothing starts."""

    def _pool(self, monkeypatch, *, sub_id="sub-W", raises=None):
        from services.engines import subscription_pool as sp
        seen: dict = {"resolve": [], "released": []}

        def _resolve(exec_path, user_sub, model="", agent_info=None, sticky_scope=""):
            seen["resolve"].append((exec_path, user_sub, model, sticky_scope))
            if raises is not None:
                raise raises
            return sub_id, {"TOKEN": "x"}
        monkeypatch.setattr(sp, "resolve_subscription_env", _resolve)
        monkeypatch.setattr(sp, "release_unbound_seat",
                            lambda sub, scope="": seen["released"].append((sub, scope)))
        return seen

    def test_oneshot_takes_a_seat_and_the_spawn_carries_it(self, temp_db, ledger, monkeypatch):
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s1", "user-1", "pa", model="model-pinned")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s1")) == ""
        cfg = layer.cfgs[0]
        assert cfg.subscription_id == "sub-W"
        assert cfg.extra_env["TOKEN"] == "x"
        assert cfg.subscription_user_sub == ""
        assert cfg.model == "model-pinned"
        (path, sub, model, scope), = seen["resolve"]
        assert (path, sub, model) == ("claude-code-cli", None, "model-pinned")
        assert scope.startswith("local:") and cfg.sandbox_host_claude_dir in scope
        assert seen["released"] == []

    def test_a_user_scope_wake_uses_the_persons_subscription(self, temp_db, ledger, monkeypatch):
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        _member("user-admin", "pa")
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s2", "user-admin", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub="user-admin", role="contributor",
            chat_id="chat-s2")) == ""
        assert seen["resolve"][0][1] == "user-admin"
        assert layer.cfgs[0].subscription_user_sub == "user-admin"

    def test_a_wake_without_credentials_spawns_nothing(self, temp_db, ledger, monkeypatch):
        from services.engines.subscription_pool import NoSubscriptionError
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        self._pool(monkeypatch, raises=NoSubscriptionError("none", "no credentials"))
        task_store.create_chat("chat-s3", "user-1", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s3")) is None
        assert layer.spawns == []
        assert _SID not in ledger["C"]._sessions   # the reservation went back too

    def test_a_failed_start_returns_the_seat(self, temp_db, ledger, monkeypatch):
        layer = _SpawnLayer(raises=True)
        _stub_spawn(monkeypatch, layer)
        monkeypatch.setattr(ledger["C"], "_live_local_sids", set)
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s4", "user-1", "pa")
        with pytest.raises(RuntimeError, match="spawn failed"):
            asyncio.run(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s4"))
        assert [s for s, _ in seen["released"]] == ["sub-W"]

    def test_a_raise_between_the_seat_and_the_start_returns_the_seat(self, temp_db, ledger,
                                                                      monkeypatch):
        from core import execution_mode
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s6", "user-1", "pa")

        def _broken(*a, **k):
            raise RuntimeError("settings unreadable")
        monkeypatch.setattr(execution_mode, "is_interactive", _broken)
        with pytest.raises(RuntimeError, match="settings unreadable"):
            asyncio.run(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s6"))
        assert layer.spawns == []
        assert [s for s, _ in seen["released"]] == ["sub-W"]

    def test_a_cancel_before_the_rewarm_spawns_returns_the_seat(self, temp_db, ledger,
                                                                 monkeypatch):
        from core.session import warmup_registry
        layer = _SpawnLayer()
        _stub_spawn(monkeypatch, layer)
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s7", "user-1", "pa")
        task_store.update_chat("chat-s7", execution_mode="interactive")
        entered = asyncio.Event()

        async def _hang(*a, **k):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(warmup_registry, "register", _hang)

        async def _run():
            wake = asyncio.create_task(scheduler._deliver_via_oneshot(
                _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s7"))
            await asyncio.wait_for(entered.wait(), 2.0)
            wake.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wake
        asyncio.run(_run())
        assert layer.spawns == []
        assert [s for s, _ in seen["released"]] == ["sub-W"]

    def test_a_failed_interactive_rewarm_returns_the_seat(self, temp_db, ledger, monkeypatch):
        layer = _SpawnLayer(raises=True)
        _stub_spawn(monkeypatch, layer)
        seen = self._pool(monkeypatch)
        task_store.create_chat("chat-s5", "user-1", "pa")
        task_store.update_chat("chat-s5", execution_mode="interactive")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-s5")) is None
        assert [s for s, _ in seen["released"]] == ["sub-W"]


def _person(sub: str, username: str, *, agent: str = "", agent_role: str = "",
            platform_role: str = "member") -> None:
    """A users row with a username, and an agent row when ``agent_role``."""
    from datetime import datetime, timezone
    from storage.identity import db_users
    from storage.pg import get_conn
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, username) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, f"{username}@test.com", username, platform_role, now, now, username))
        conn.commit()
    if agent_role:
        db_users.add_user_agent(sub, agent, agent_role, "user-admin")


class TestWakeRespawnConfig:
    """The respawned session carries the credential environment and the
    secret bundles its MCP build computed, for the person the delivery
    runs as."""

    class _Layer(_SpawnLayer):
        def __init__(self):
            super().__init__()
            self.resume_usernames: list[str] = []

        async def can_resume_session(self, sid, agent_name="", username=""):
            self.resume_usernames.append(username)
            return True

    def _stub(self, monkeypatch, *, config_path=None):
        """The layer and a build that returns a flat env with a manifest
        injected variable, one bundle, and the bash-only key list."""
        from core.credentials.mcp_broker import SecretBundle
        from core.session import session_manager
        from services.mcp import mcp_registry
        layer = self._Layer()
        monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **k: layer)
        builds: list[tuple] = []

        def _build(*a, **k):
            builds.append((a, k))
            return (config_path, {"GH_TOKEN": "tok-flat"}, {},
                    {"github-mcp": SecretBundle(http_bearer="bearer-x")}, {"GH_TOKEN"})
        monkeypatch.setattr(mcp_registry, "build_session_mcp_config", _build)

        async def _echo(layer, session_id, chat_id, agent, result_prompt):
            return ""
        monkeypatch.setattr(delivery, "_run_echo_turn_pumped", _echo)
        return layer, builds

    @staticmethod
    def _token_sub(cfg) -> str:
        from auth.session_token import validate_session_token
        return (validate_session_token(cfg.credential_env["PROXY_API_KEY"]) or {})["user_sub"]

    def _assert_credentials(self, cfg, *, person: str) -> None:
        assert cfg.credential_env["GH_TOKEN"] == "tok-flat"
        assert set(cfg.mcp_secret_bundles) == {"github-mcp"}
        assert cfg.mcp_secret_bundles["github-mcp"].http_bearer == "bearer-x"
        assert cfg.credential_env["OTO_AGENT_NAME"] == cfg.agent_name
        assert cfg.credential_env["OTO_SESSION_ID"] == _SID
        assert cfg.credential_env["OTO_USER_SUB"] == person
        assert self._token_sub(cfg) == person
        assert "OTO_ALLOWED_ROOTS" in cfg.multi_value_envs

    def test_a_headless_respawn_carries_the_env_and_the_bundles(self, temp_db, ledger,
                                                                monkeypatch):
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        layer, builds = self._stub(monkeypatch)
        task_store.create_chat("chat-w1", "task::pa", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-w1")) == ""
        cfg, = layer.cfgs
        self._assert_credentials(cfg, person="")
        (args, kw), = builds
        assert args[1] is None and kw["task_scope"] == "agent"

    def test_the_interactive_rewarm_carries_the_same(self, temp_db, ledger, monkeypatch):
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        self._stub(monkeypatch)
        task_store.create_chat("chat-w2", "task::pa", "pa")
        task_store.update_chat("chat-w2", execution_mode="interactive")
        seen: list = []

        async def _fake_rewarm(layer, session_id, agent, result_prompt,
                               *, chat_id, base_cfg, chat_row):
            seen.append(base_cfg)
            return ""
        monkeypatch.setattr(delivery, "_rewarm_interactive_and_wake", _fake_rewarm)
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-w2")) == ""
        cfg, = seen
        self._assert_credentials(cfg, person="")

    def test_a_codex_respawn_injects_the_env_into_its_toml(self, temp_db, ledger,
                                                           monkeypatch, tmp_path):
        from services.mcp import mcp_registry
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        toml = tmp_path / "mcp.toml"
        toml.write_text("")
        layer, _ = self._stub(monkeypatch, config_path=toml)
        injected: list = []

        def _inject(path, env, *, exclude_keys=None):
            injected.append((path, dict(env), set(exclude_keys or ())))
            return tmp_path / "mcp-injected.toml"
        monkeypatch.setattr(mcp_registry, "inject_credential_env_into_toml", _inject)
        task_store.create_chat("chat-w3", "task::pa", "pa")
        task_store.update_chat("chat-w3", execution_path="codex-cli")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-w3")) == ""
        (path, env, exclude), = injected
        assert path == toml and exclude == {"GH_TOKEN"}
        assert env["OTO_AGENT_NAME"] == "pa" and "PROXY_API_KEY" in env
        assert layer.cfgs[0].mcp_config_path == str(tmp_path / "mcp-injected.toml")

    def test_a_shared_only_chat_respawns_as_the_person_at_agent_scope(self, temp_db, ledger,
                                                                     monkeypatch):
        from services.engines import subscription_pool as sp
        from storage.agents import agent_store
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        _person("user-ed", "eddie", agent="so", agent_role="editor")
        layer, builds = self._stub(monkeypatch)
        subs: list = []
        monkeypatch.setattr(sp, "resolve_subscription_env",
                            lambda path, sub, **k: (subs.append(sub), ("sub-P", {}))[1])
        task_store.create_chat("chat-w4", "agent::so", "so")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "so", "wake!", user_sub="user-ed", role="editor", chat_id="chat-w4")) == ""
        cfg, = layer.cfgs
        self._assert_credentials(cfg, person="user-ed")
        (args, kw), = builds
        assert args[1] == "user-ed" and kw["task_scope"] == "agent"
        assert kw["username"] == "eddie"
        assert cfg.user_sub == "user-ed"
        assert cfg.credential_env["OTO_USERNAME"] == ""          # the agent-scope mount
        assert cfg.security_context.username == "eddie"
        assert cfg.security_context.session_scope == "agent"
        assert "/users/" not in cfg.sandbox_host_claude_dir
        assert layer.resume_usernames == [""]
        assert subs == ["user-ed"]                  # the sender pays, as on their own turn

    def test_a_personal_chat_respawns_with_the_owners_own_accounts(self, temp_db, ledger,
                                                                   monkeypatch):
        from services.engines import subscription_pool as sp
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        _person("user-own", "owen", agent="pa", agent_role="editor")
        layer, builds = self._stub(monkeypatch)
        monkeypatch.setattr(sp, "resolve_subscription_env", lambda *a, **k: ("sub-O", {}))
        task_store.create_chat("chat-w5", "user-own", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub="user-own", role="editor", chat_id="chat-w5")) == ""
        cfg, = layer.cfgs
        self._assert_credentials(cfg, person="user-own")
        (args, kw), = builds
        assert args[1] == "user-own" and kw["task_scope"] == "user"
        assert cfg.credential_env["OTO_USERNAME"] == "owen"
        assert "/users/owen/" in cfg.sandbox_host_claude_dir
        assert layer.resume_usernames == ["owen"]


    def _capture_prompt(self, monkeypatch) -> list[tuple[bool, dict]]:
        import threading
        import config
        seen: list[tuple[bool, dict]] = []

        def _prompt(agent, **kw):
            seen.append((threading.current_thread() is threading.main_thread(), kw))
            return "PERSONA"
        monkeypatch.setattr(config, "build_agent_prompt", _prompt)
        return seen

    def test_the_respawns_prompt_is_built_off_the_loop(self, temp_db, ledger, monkeypatch):
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        self._stub(monkeypatch)
        seen = self._capture_prompt(monkeypatch)
        task_store.create_chat("chat-w8", "task::pa", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-w8")) == ""
        assert [on_loop for on_loop, _ in seen] == [False]

    def test_the_respawns_prompt_tells_the_session_who_it_runs_as(self, temp_db, ledger,
                                                                  monkeypatch):
        """The prompt is built from the same visibility and role as the
        session's config: a contributor's own chat is told their name, their
        role and their folders, never the manager's view of the agent."""
        from services.engines import subscription_pool as sp
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        _person("user-own", "owen", agent="pa", agent_role="contributor")
        layer, _ = self._stub(monkeypatch)
        monkeypatch.setattr(sp, "resolve_subscription_env", lambda *a, **k: ("sub-O", {}))
        seen = self._capture_prompt(monkeypatch)
        task_store.create_chat("chat-w9", "user-own", "pa")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub="user-own", role="contributor",
            chat_id="chat-w9")) == ""
        (_on_loop, kw), = seen
        vis = delivery._wake_visibility("pa", "owen", "contributor", "user-own")
        assert kw["username"] == "owen" and kw["role"] == "contributor"
        assert kw["mount_shared"] == vis.mount_shared
        assert kw["execution_path"] == "claude-code-cli"
        prompt = layer.cfgs[0].system_prompt
        assert prompt.startswith("PERSONA") and "# Session Context" in prompt
        assert "# Folders" in prompt and "contributor" in prompt

    def test_a_shared_only_respawn_runs_where_the_chat_runs(self, temp_db, ledger,
                                                            monkeypatch):
        # The chat's own placement, as the dashboard resumes it; the person's
        # own resolution would name another place.
        from storage import remote_store
        from services.engines import subscription_pool as sp
        from storage.agents import agent_store
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        _person("user-ed", "eddie", agent="so", agent_role="editor")
        layer, _ = self._stub(monkeypatch)
        monkeypatch.setattr(sp, "resolve_subscription_env", lambda *a, **k: ("sub-P", {}))
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda *a, **k: ("machine-elsewhere", None))
        task_store.create_chat("chat-w7", "agent::so", "so")
        task_store.update_chat("chat-w7", execution_target="local")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "so", "wake!", user_sub="user-ed", role="editor", chat_id="chat-w7")) == ""
        assert layer.cfgs[0].execution_target == "local"

    def test_a_codex_respawn_resumes_the_chats_own_thread(self, temp_db, ledger,
                                                          monkeypatch):
        from storage.agents import agent_store
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        layer, _ = self._stub(monkeypatch)
        task_store.create_chat("chat-w8", "task::pa", "pa")
        task_store.update_chat("chat-w8", execution_path="codex-cli",
                               codex_thread_id="thread-of-the-chat")
        assert asyncio.run(scheduler._deliver_via_oneshot(
            _SID, "pa", "wake!", user_sub=None, role="manager", chat_id="chat-w8")) == ""
        assert layer.cfgs[0].resume_handle == "thread-of-the-chat"

    def test_a_live_session_takes_the_wake_without_a_respawn(self, temp_db, ledger,
                                                             monkeypatch):
        # Inside the idle window the process is alive; the wake rides it
        # and nothing is built or started, whoever the delivery names.
        from storage.agents import agent_store
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        _person("user-ed", "eddie", agent="so", agent_role="editor")
        layer, builds = self._stub(monkeypatch)

        async def _alive(sid):
            return True
        layer.is_session_alive = _alive
        task_store.create_chat("chat-w6", "agent::so", "so")
        assert asyncio.run(scheduler._deliver_via_persistent(
            _SID, "so", "wake!", user_sub="user-ed", role="editor", chat_id="chat-w6")) == ""
        assert layer.cfgs == [] and builds == []


class TestSharedChatDeliveryIdentity:
    """A delegate callback into a Shared-only chat runs as the
    person whose session delegated the work, at their current standing; a
    person who lost the agent gets the result as an event only."""

    def _capture(self, monkeypatch) -> list[dict]:
        from core.session import session_delivery
        calls: list[dict] = []

        async def _fake(chat_id, text, **kw):
            calls.append({"chat_id": chat_id, **kw})
            return session_delivery.DeliveryOutcome("oneshot", chat_id=chat_id,
                                                    session_id=kw.get("session_id", ""))
        monkeypatch.setattr(session_delivery, "deliver_prompt", _fake)
        return calls

    def _shared_chat(self, chat_id: str) -> None:
        from storage.agents import agent_store
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        task_store.create_chat(chat_id, "agent::so", "so")
        task_store.update_chat(chat_id, session_id=_SID)

    def _deliver(self, chat_id: str, created_by: str) -> None:
        task = TaskDefinition(id="task-d", name="sub", agent="so", prompt="p",
                              scope="agent", created_by=created_by)
        asyncio.run(scheduler._do_deliver(_SID, "so", "THE RESULT", task,
                                          chat_id=chat_id, output_text="OUT"))

    def test_the_delegator_at_their_standing(self, temp_db, monkeypatch):
        calls = self._capture(monkeypatch)
        self._shared_chat("chat-i1")
        _person("user-ed", "eddie", agent="so", agent_role="editor")
        _person("user-adm", "addie", platform_role="admin")
        self._deliver("chat-i1", "user-ed")
        self._deliver("chat-i1", "user-adm")
        assert [(c["user_sub"], c["role"]) for c in calls] == [
            ("user-ed", "editor"), ("user-adm", "admin")]

    def test_a_delegator_without_the_agent_gets_the_event_only(self, temp_db, monkeypatch):
        calls = self._capture(monkeypatch)
        self._shared_chat("chat-i2")
        _person("user-gone", "gone")
        self._deliver("chat-i2", "user-gone")
        assert calls == []
        events = [m for m in task_store.get_chat_messages("chat-i2")
                  if m.get("event_type") == "delegate_result"]
        assert len(events) == 1
        assert task_store.claim_pending_delegate_wake("chat-i2") == []

    def test_below_the_editor_tier_counts_as_gone(self, temp_db, monkeypatch):
        # A Shared-only chat runs from the agent's own state: offboarding
        # treats a demotion below editor as leaving it, and so does the wake.
        calls = self._capture(monkeypatch)
        self._shared_chat("chat-i4")
        _person("user-con", "connie", agent="so", agent_role="contributor")
        self._deliver("chat-i4", "user-con")
        assert calls == []
        assert task_store.claim_pending_delegate_wake("chat-i4") == []

    def test_a_persons_own_chat_wakes_as_its_owner(self, temp_db, monkeypatch):
        # Whatever the worker's scope: an agent-scope worker's result into a
        # personal chat runs as the chat's owner, at their row.
        from storage.agents import agent_store
        calls = self._capture(monkeypatch)
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        _person("user-own", "owen", agent="pa", agent_role="editor")
        task_store.create_chat("chat-i5", "user-own", "pa")
        task = TaskDefinition(id="task-p", name="sub", agent="so", prompt="p",
                              scope="agent", created_by="user-own")
        asyncio.run(scheduler._do_deliver(_SID, "pa", "R", task,
                                          chat_id="chat-i5", output_text="OUT"))
        assert [(c["user_sub"], c["role"]) for c in calls] == [("user-own", "editor")]

    def test_no_person_on_record_keeps_the_agent_identity(self, temp_db, monkeypatch):
        calls = self._capture(monkeypatch)
        self._shared_chat("chat-i3")
        self._deliver("chat-i3", "so")
        assert [(c["user_sub"], c["role"]) for c in calls] == [(None, "manager")]

    def test_other_chats_keep_todays_rule(self, temp_db, monkeypatch):
        from storage.agents import agent_store
        calls = self._capture(monkeypatch)
        agent_store.create_agent("so", "SO", collaborative=False, default_scope="agent")
        agent_store.create_agent("pa", "PA", collaborative=True, default_scope="user")
        _person("user-ed", "eddie", agent="so", agent_role="editor")
        task_store.create_chat("chat-t", "task::so", "so")
        task_store.create_chat("chat-ph", "phone", "so")
        # A chat left from before a mode change keeps today's path too.
        task_store.create_chat("chat-old", "agent::pa", "pa")
        for chat_id, agent in (("chat-t", "so"), ("chat-ph", "so"), ("chat-old", "pa")):
            task = TaskDefinition(id="task-o", name="sub", agent=agent, prompt="p",
                                  scope="agent", created_by="user-ed")
            asyncio.run(scheduler._do_deliver(_SID, agent, "R", task,
                                              chat_id=chat_id, output_text="OUT"))
        assert [(c["user_sub"], c["role"]) for c in calls] == [(None, "manager")] * 3
