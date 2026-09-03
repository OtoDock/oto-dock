"""WS ``switch_engine`` op — cross-engine resume
(``ws/dashboard_dispatch._handle_switch_engine``) + the ``probe_liveness``
op, the CAS rebind helper, and the ``engine_switch`` history-seed reason.

Drives the REAL handler through the ws_dashboard harness. Gate ladder in
handler order — chat bound, task/phone denied (admins included), owner/admin,
warmup-in-flight (GLOBAL registry, not just this connection), streaming, the
process-alive check (live PTY or registered headless session), engine
enabled + user-accessible, model enabled — then the happy rebind: engine +
model flipped, session ids dropped, abort flags/context cleared,
``pending_history_seed='engine_switch:<old>'`` (none for a direct-llm target,
which persists its cards immediately instead), the connection's binding
cleared, and the ``engine_switched`` ack emitted.
"""

import asyncio
import uuid
from types import SimpleNamespace

from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    FakeInteractiveSession,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
)


def _make_chat(agent: str, *, chat_id: str | None = None,
               session_id: str | None = None,
               messages: tuple[tuple[str, str], ...] = (),
               user_sub: str = "user-admin") -> str:
    from storage import database as task_store
    cid = chat_id or str(uuid.uuid4())
    task_store.create_chat(cid, user_sub, agent, "default",
                           model=TEST_MODEL,
                           execution_path="claude-code-cli")
    if session_id:
        task_store.update_chat(cid, session_id=session_id)
    for role, content in messages:
        task_store.add_chat_message(cid, role, content, author_sub=user_sub)
    return cid


def _enable_codex(slug: str) -> None:
    import json
    from storage import agent_store
    agent_store.update_agent(slug, execution_path="claude-code-cli",
                             execution_paths=json.dumps(["codex-cli"]))


def _grant(layer: str, sub: str = "user-admin") -> None:
    from storage import subscription_store
    subscription_store.add_subscription(
        layer, "openai" if layer == "codex-cli" else "anthropic", "api_key",
        owner_sub=sub, use_personal=True,
    )


def _capture_connections(monkeypatch):
    import ws.dashboard as wsd
    captured = []
    orig_init = wsd.DashboardConnection.__init__

    def _init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        captured.append(self)
    monkeypatch.setattr(wsd.DashboardConnection, "__init__", _init)
    return captured


async def _drain_until(ws, ftype, timeout: float = 3.0):
    seen = []
    while True:
        try:
            frame = await ws.next_frame(timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"no {ftype!r} frame; saw {seen}") from None
        if frame["type"] == ftype:
            return frame
        seen.append(frame)


def _switch(ws, path: str = "codex-cli", model: str = TEST_MODEL):
    ws.client_send({"type": "switch_engine",
                    "execution_path": path, "model": model})


# ---------------------------------------------------------------------------
# Gate ladder — every refusal in handler order.
# ---------------------------------------------------------------------------

class TestSwitchEngineGates:
    def test_no_open_chat(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert frame["message"] == "Chat not found."
        run_ws_scenario(scenario)

    # ── Task-chat rule (1.5): a task-run chat switches like any chat once
    # its RUN is over — follow-up turns resolve engine/model/seed from the
    # chat row. The gates that replaced the old blanket deny: run not
    # pending/running (the run lifecycle is invisible to session probes),
    # the continue tier, TASK-identity credentials, and no active
    # continuation targeting the chat.

    @staticmethod
    def _make_task_run(cid: str, *, status: str, scope: str = "user",
                       created_by: str = "user-admin",
                       agent: str = "") -> str:
        from storage import database as task_store
        run_id = cid.removeprefix("task-")
        task_store.create_run(run_id, f"dyn-{run_id[:8]}", agent,
                              "scheduled", None, "test prompt",
                              scope=scope, created_by=created_by)
        if status != "pending":
            task_store.update_run(run_id, status=status)
        return run_id

    def test_task_chat_dead_run_switch_allowed(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        cid = _make_chat(slug, chat_id=f"task-{uuid.uuid4()}")
        self._make_task_run(cid, status="completed", agent=slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "chat_history")
                _switch(ws)
                frame = await _drain_until(ws, "engine_switched")
                assert frame["execution_path"] == "codex-cli"
            from storage import database as task_store
            chat = task_store.get_chat(cid)
            assert chat["execution_path"] == "codex-cli"
            assert chat["pending_history_seed"].startswith("engine_switch:")
        run_ws_scenario(scenario)

    def test_task_chat_running_or_pending_run_denied(self, temp_db,
                                                     monkeypatch):
        # A running/parked run reads DEAD to every session probe (spawn
        # window, admission parking) — the run-status rung must deny with
        # alive=True so the FE re-locks the picker.
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        for status in ("running", "pending"):
            cid = _make_chat(slug, chat_id=f"task-{uuid.uuid4()}")
            self._make_task_run(cid, status=status, agent=slug)

            async def scenario(cid=cid):
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await _drain_until(ws, "chat_history")
                    _switch(ws)
                    frame = await _drain_until(ws, "switch_engine_denied")
                    assert frame.get("process_alive") is True
            run_ws_scenario(scenario)

    def test_task_chat_agent_scope_viewer_denied(self, temp_db, monkeypatch):
        # Agent-scope task chats follow the continue tier: editor+ only. A
        # per-agent viewer is denied even though the run is over.
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent(default_scope="agent", collaborative=False)
        _enable_codex(slug)
        task_store.set_user_agents("user-viewer", [slug], "user-admin",
                                   agent_roles={slug: "viewer"})
        cid = _make_chat(slug, chat_id=f"task-{uuid.uuid4()}",
                         user_sub=f"task::{slug}")
        self._make_task_run(cid, status="completed", scope="agent",
                            created_by=f"task::{slug}", agent=slug)

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "chat_history")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "editor" in frame["message"]
        run_ws_scenario(scenario)

    def test_task_chat_agent_scope_needs_platform_pool(self, temp_db,
                                                       monkeypatch):
        # Agent-scope follow-ups run on the PLATFORM POOL, not the viewer's
        # personal subscription — a personal codex grant must not green-light
        # the switch when the pool has nothing.
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")  # personal (use_personal, no contribute_platform)
        cid = _make_chat(slug, chat_id=f"task-{uuid.uuid4()}",
                         user_sub=f"task::{slug}")
        self._make_task_run(cid, status="completed", scope="agent",
                            created_by=f"task::{slug}", agent=slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "chat_history")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "platform pool" in frame["message"]
        run_ws_scenario(scenario)

    def test_continuation_target_chat_denied(self, temp_db, monkeypatch):
        # A chat wired as an ACTIVE continuation target keeps its row
        # coherent with the continuation's delivery ladder (a rebind NULLs
        # session_id and the oneshot delivery would silently starve — see
        # the plan doc's W2-F5; the pre-existing machine-removal starvation
        # is ROADMAP'd, this deny closes the user-triggerable path).
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        cid = _make_chat(slug)
        task_store.create_dynamic_task(
            f"dyn-{uuid.uuid4().hex[:8]}", slug, "wake", "continue", "chat",
            "continuation", None, None, None, 300, "user-admin",
            target_chat_id=cid,
        )

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "chat_history")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "follow-up" in frame["message"]
        run_ws_scenario(scenario)

    def test_non_owner_refused(self, temp_db, monkeypatch):
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent(default_scope="agent", collaborative=False)
        _enable_codex(slug)
        task_store.set_user_agents("user-viewer", [slug], "user-admin",
                                   agent_roles={slug: "viewer"})
        cid = _make_chat(slug, user_sub="user-admin")

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert frame["message"] == "Only the chat owner can switch its engine."
        run_ws_scenario(scenario)

    def test_global_warmup_registry_refused(self, temp_db, monkeypatch):
        # A SECOND connection's warmup registers globally — the gate must see
        # it (move_chat's connection-scoped blindness, audit finding).
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        cid = _make_chat(slug)

        async def scenario():
            from core.session import warmup_registry
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                await warmup_registry.register(cid, "user-admin", slug)
                try:
                    _switch(ws)
                    frame = await _drain_until(ws, "switch_engine_denied")
                    assert "getting ready" in frame["message"]
                finally:
                    await warmup_registry.unregister(cid)
        run_ws_scenario(scenario)

    def test_streaming_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        conns = _capture_connections(monkeypatch)
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                conn = conns[-1]
                conn.streaming = True
                try:
                    _switch(ws)
                    frame = await _drain_until(ws, "switch_engine_denied")
                    assert "Finish or stop" in frame["message"]
                    assert frame["process_alive"] is True
                finally:
                    conn.streaming = False
        run_ws_scenario(scenario)

    def test_live_pty_refused(self, temp_db, monkeypatch):
        # An idle-but-alive interactive PTY is an alive PROCESS — the chat
        # stays engine-locked until it ends (turn_open not required).
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid)

        async def scenario():
            from core.session import interactive_session as isess_mod
            fake = FakeInteractiveSession(session_id=sid, chat_id=cid)
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    isess_mod._sessions[sid] = fake
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await _drain_until(ws, "chat_history")
                    _switch(ws)
                    frame = await _drain_until(ws, "switch_engine_denied")
                    assert "still active" in frame["message"]
                    assert frame["process_alive"] is True
            finally:
                isess_mod._sessions.pop(sid, None)
        run_ws_scenario(scenario)

    def test_alive_headless_session_refused(self, temp_db, monkeypatch):
        # chat_process_alive checks REGISTRY membership (the stored path may
        # be stale) — a live entry in the cli pool refuses the switch.
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid)

        async def scenario():
            from core.layers.cli import session as cli_session
            monkeypatch.setitem(cli_session._persistent_sessions, sid,
                                SimpleNamespace(is_alive=True))
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "still active" in frame["message"]
        run_ws_scenario(scenario)

    def test_engine_not_enabled_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()  # claude only
        _grant("codex-cli")
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "isn't enabled for this agent" in frame["message"]
        run_ws_scenario(scenario)

    def test_engine_not_accessible_refused(self, temp_db, monkeypatch):
        # Enabled on the agent but the CALLER can't run it (no personal sub,
        # Platform Auth off by default) → user_can_run gate.
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws)
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "don't have access" in frame["message"]
        run_ws_scenario(scenario)

    def test_unknown_model_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws, model="not-a-model")
                frame = await _drain_until(ws, "switch_engine_denied")
                assert "isn't available" in frame["message"]
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------

class TestSwitchEngineHappyPath:
    def test_switch_rebinds_row_and_acks(self, temp_db, monkeypatch):
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        conns = _capture_connections(monkeypatch)
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        sid = str(uuid.uuid4())  # dead: not in any registry
        cid = _make_chat(slug, session_id=sid,
                         messages=(("user", "hi"), ("assistant", "hello")))
        task_store.update_chat(cid, codex_thread_id="thread-1",
                               context_used=90_000, last_turn_aborted=True)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws)
                frame = await _drain_until(ws, "engine_switched")
                assert frame == {"type": "engine_switched", "chat_id": cid,
                                 "execution_path": "codex-cli",
                                 "model": TEST_MODEL}
                row = task_store.get_chat(cid)
                assert row["execution_path"] == "codex-cli"
                assert row["model"] == TEST_MODEL
                assert row["session_id"] is None
                assert row["codex_thread_id"] is None
                assert row["context_used"] == 0
                assert not row["last_turn_aborted"]
                assert row["pending_history_seed"] == \
                    "engine_switch:claude-code-cli"
                assert conns[-1].session_id is None
        run_ws_scenario(scenario)

    def test_switch_back_supported(self, temp_db, monkeypatch):
        # Codex → Claude → Codex: each hop is a DB-history restart; the
        # return hop passes because the row is authoritative at every gate.
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _enable_codex(slug)
        _grant("codex-cli")
        _grant("claude-code-cli")
        cid = _make_chat(slug, messages=(("user", "hi"),))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws, path="codex-cli")
                await _drain_until(ws, "engine_switched")
                _switch(ws, path="claude-code-cli")
                await _drain_until(ws, "engine_switched")
                row = task_store.get_chat(cid)
                assert row["execution_path"] == "claude-code-cli"
                assert row["pending_history_seed"] == \
                    "engine_switch:codex-cli"
        run_ws_scenario(scenario)

    def test_direct_llm_target_no_flag_but_cards(self, temp_db, monkeypatch):
        # direct-llm never consumes seeds (full DB rebuild per turn): the
        # flag is cleared — a stale one would fire a wrong digest on a later
        # hop back — and BOTH the prior pending notice and the switch card
        # are persisted immediately instead of silently swallowed.
        import json as _json
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        from storage import agent_store
        agent_store.update_agent(slug, execution_path="claude-code-cli",
                                 execution_paths=_json.dumps(["direct-llm"]))
        _grant("direct-llm")
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "yo")))
        task_store.update_chat(cid, pending_history_seed="moved:Old-PC")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                _switch(ws, path="direct-llm")
                await _drain_until(ws, "engine_switched")
                row = task_store.get_chat(cid)
                assert row["execution_path"] == "direct-llm"
                assert row["pending_history_seed"] == ""
                events = [
                    _json.loads(m["event_data"])
                    for m in task_store.get_chat_messages(cid)
                    if m.get("event_type") == "system"
                ]
                reasons = [e.get("reason") for e in events
                           if e.get("subtype") == "session_reseeded"]
                assert reasons == ["moved", "engine_switch"]
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# probe_liveness + the CAS helper + the seed reason (unit-level).
# ---------------------------------------------------------------------------

class TestProbeLiveness:
    def test_probe_reports_dead_then_alive(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid)

        async def scenario():
            from core.session import interactive_session as isess_mod
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await _drain_until(ws, "queue_snapshot")
                    ws.client_send({"type": "probe_liveness"})
                    frame = await _drain_until(ws, "liveness")
                    assert frame == {"type": "liveness", "chat_id": cid,
                                     "process_alive": False}
                    isess_mod._sessions[sid] = FakeInteractiveSession(
                        session_id=sid, chat_id=cid)
                    ws.client_send({"type": "probe_liveness"})
                    frame = await _drain_until(ws, "liveness")
                    assert frame["process_alive"] is True
            finally:
                isess_mod._sessions.pop(sid, None)
        run_ws_scenario(scenario)


class TestRebindCas:
    def test_cas_mismatch_is_clean_noop(self, temp_db):
        from storage import database as task_store
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-current")
        # A concurrent warmup re-bound the row after the gates observed it —
        # the stale expected pair must lose without any partial write.
        ok = task_store.rebind_chat_for_engine_switch(
            cid, "codex-cli", TEST_MODEL,
            expected_old_path="claude-code-cli",
            expected_session_id="sid-STALE",
            pending_seed="engine_switch:claude-code-cli",
        )
        assert ok is False
        row = task_store.get_chat(cid)
        assert row["execution_path"] == "claude-code-cli"
        assert row["session_id"] == "sid-current"
        assert row["pending_history_seed"] == ""

    def test_cas_match_flips(self, temp_db):
        from storage import database as task_store
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-current")
        ok = task_store.rebind_chat_for_engine_switch(
            cid, "codex-cli", TEST_MODEL,
            expected_old_path="claude-code-cli",
            expected_session_id="sid-current",
            pending_seed="engine_switch:claude-code-cli",
        )
        assert ok is True
        assert task_store.get_chat(cid)["execution_path"] == "codex-cli"


class TestInteractiveSeedRestoreOnSpawnFailure:
    def test_failed_spawn_unburns_claimed_seed(self, temp_db, monkeypatch):
        # The interactive path claims the history digest BEFORE the spawn
        # try-block; a failed spawn (offline satellite, missing CLI after an
        # engine switch, config error) must re-stamp the claimed reason so
        # the NEXT successful warmup still restores context (audit finding —
        # previously the seed was silently burned).
        from storage import database as task_store
        from storage.db_settings import set_platform_setting
        from tests.fixtures.ws_dashboard_harness import set_username

        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        set_username("user-admin", "admin")
        set_platform_setting("interactive_cli_enabled", "true")
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "yo")))
        task_store.update_chat(
            cid, pending_history_seed="engine_switch:codex-cli")

        import ws.dashboard as wsd

        async def _boom(self, *a, **k):
            raise RuntimeError("spawn exploded")
        monkeypatch.setattr(
            wsd.DashboardConnection, "_create_or_resume_session", _boom)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({
                    "type": "warmup", "agent": slug, "chat_id": cid,
                    "text": "continue please",
                    "execution_mode": "interactive",
                })
                await _drain_until(ws, "warmup_failed", timeout=6.0)
                row = task_store.get_chat(cid)
                assert row["pending_history_seed"] == \
                    "engine_switch:codex-cli"
        run_ws_scenario(scenario)


class TestEngineSwitchSeedReason:
    def test_consume_engine_switch_reason(self, temp_db):
        import json as _json
        from core.session import history_seed
        from storage import database as task_store
        slug = make_test_agent()
        cid = _make_chat(slug, messages=(("user", "hello"),
                                         ("assistant", "world")))
        task_store.update_chat(cid,
                               pending_history_seed="engine_switch:codex-cli")
        digest, notice = history_seed.consume_pending_seed_digest(cid)
        assert "previous engine: Codex" in notice
        assert digest  # conversation restored
        # Claimed exactly once; card persisted with the machine-readable kind.
        assert task_store.get_chat(cid)["pending_history_seed"] == ""
        events = [
            _json.loads(m["event_data"])
            for m in task_store.get_chat_messages(cid)
            if m.get("event_type") == "system"
        ]
        assert any(e.get("reason") == "engine_switch" for e in events)
