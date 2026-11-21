"""A mode or model picked on a chat with no live session is kept.

The pick reaches the chat's row at once in every branch (a live session,
one that is away, none at all), so the next start, on another socket too
and as a terminal, runs in it; a session that was away is owed the mode
once; a task chat keeps its mode while its run is active.
"""

import asyncio
import uuid

import pytest

from core.session.interactive_output import InteractiveOutputMixin
from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
    sync_dispatch,
)
from tests.session.test_ws_dashboard_resume_controls import (
    _bare_connection,
    _frames_until_pong,
    _make_chat,
)


class TestAPickWithNoSession:
    def test_a_pick_on_a_chat_with_no_session_is_stored_at_once(self, temp_db, monkeypatch):
        """A chat whose session (or terminal) closed: the pick reaches the row
        at once, so the next start, on another socket too and as a terminal,
        runs in it, never in the row's earlier mode."""
        from core.permission_modes import terminal_mode
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "hello")))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _frames_until_pong(ws)
                ws.client_send({"type": "mode_change", "mode": "autoReview", "chat_id": cid})
                await ws.expect({"type": "mode_changed", "mode": "autoReview", "chat_id": cid})
                await sync_dispatch(ws)
                assert temp_db.get_chat(cid)["permission_mode"] == "autoReview"
            # Another socket (the page reconnected) warms the chat as a
            # terminal; the frame's stale word loses to the row.
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug, "chat_id": cid,
                                "permission_mode": "ask", "execution_mode": "interactive"})
                while (await ws.next_frame())["type"] != "warmup_ready":
                    pass
        run_ws_scenario(scenario)
        cfg = layer.started[-1][1]
        assert cfg.permission_mode == "autoReview"
        assert terminal_mode(cfg.permission_mode) == "auto"

    def test_a_pick_while_the_session_is_away_owes_the_engine_its_mode(self, temp_db,
                                                                       monkeypatch):
        from ws import dashboard_chat_support as support
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-away")
        layer = FakeExecutionLayer()    # sid-away is not alive: its machine is away

        async def scenario():
            conn = _bare_connection(cid, "sid-away", user_sub="user-admin", agent=slug,
                                    layer=layer)
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            return conn
        conn = asyncio.run(scenario())
        assert temp_db.get_chat(cid)["permission_mode"] == "autoReview"
        assert conn.deferred_mode == "autoReview" and conn.deferred_for == cid
        assert support._engine_mode_owed.get("sid-away") == "autoReview"

        # The session answers again; whichever socket starts its next turn
        # gives its engine the mode, once.
        layer.alive.add("sid-away")
        asyncio.run(support.apply_owed_engine_mode(layer, "sid-away"))
        asyncio.run(support.apply_owed_engine_mode(layer, "sid-away"))
        assert layer.mode_changes == [("sid-away", "autoReview")]

    def test_a_live_pick_after_one_made_while_away_holds_at_the_next_turn(self, temp_db,
                                                                         monkeypatch):
        """Auto picked while the machine reconnects, then Ask once it is back,
        then a message: the turn's start re-applies nothing, so the row, the
        hook and the engine read Ask (for this socket and for another one)."""
        from core.session.session_state import get_session_mode
        from ws import dashboard_chat_support as support
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-back")
        layer = FakeExecutionLayer()

        async def scenario():
            conn = _bare_connection(cid, "sid-back", user_sub="user-admin", agent=slug,
                                    layer=layer)
            conn.deferred_model, conn.deferred_for = "", ""
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            layer.alive.add("sid-back")             # the machine is back
            await conn._handle_mode_change({"mode": "ask", "chat_id": cid})
            # The next message: both doors a turn start uses.
            await conn._reapply_deferred_after_warmup(cid)
            await support.apply_owed_engine_mode(layer, "sid-back")
            return conn
        conn = asyncio.run(scenario())
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert get_session_mode("sid-back") == "ask"
        assert layer.mode_changes == [("sid-back", "ask")]
        assert not conn.deferred_mode and conn.deferred_for == ""
        assert "sid-back" not in support._engine_mode_owed

    def test_a_task_chat_keeps_its_mode_while_its_run_is_active(self, temp_db, monkeypatch):
        from storage import database as task_store
        from ws import dashboard as ws_dashboard
        slug = make_test_agent()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        task_store.create_run(run_id, "dyn-x", slug, "manual", None, "go",
                              "one-time", "user", "user-admin")
        cid = f"task-{run_id}"
        task_store.create_chat(cid, "user-admin", slug, "auto", execution_path="claude-code-cli")

        async def _active(chat_id):
            return chat_id == cid
        monkeypatch.setattr(ws_dashboard, "task_run_active_async", _active)

        async def scenario(bound_to):
            conn = _bare_connection(bound_to, None, user_sub="user-admin", agent=slug)
            conn.user = {"role": "admin", "username": "admin"}
            conn.user_role = "admin"
            conn.user_agents, conn.agent_roles = [slug], {}
            await conn._handle_mode_change({"mode": "ask", "chat_id": cid})
            return conn.sent
        # The bound chat, and a frame naming it from a socket bound elsewhere.
        for bound_to in (cid, "chat-elsewhere"):
            sent = asyncio.run(scenario(bound_to))
            assert sent == [{"type": "mode_changed", "chat_id": cid, "mode": "auto"}], bound_to
            assert temp_db.get_chat(cid)["permission_mode"] == "auto"

    def test_a_mode_the_chats_engine_does_not_declare_is_refused_with_no_session(
            self, temp_db, monkeypatch):
        slug = make_test_agent()
        from storage import database as task_store
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "ask", execution_path="direct-llm")

        async def scenario():
            conn = _bare_connection(cid, None, user_sub="user-admin", agent=slug)
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            return conn.sent
        assert asyncio.run(scenario()) == [{"type": "error", "message": "Invalid mode: autoReview"}]
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"

    def test_a_model_pick_on_a_chat_with_no_session_is_stored_at_once(self, temp_db,
                                                                      monkeypatch):
        """The model pick has no gap of the mode pick's kind: it writes the
        row before it asks for a session, so a closed chat keeps it."""
        from storage.billing import subscription_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
            {"model_id": TEST_MODEL, "enabled": True}, {"model_id": "other-model", "enabled": True}])
        slug = make_test_agent()
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "hello")))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _frames_until_pong(ws)
                ws.client_send({"type": "model_change", "model": "other-model", "chat_id": cid})
                await ws.expect({"type": "model_changed", "model": "other-model"})
                await sync_dispatch(ws)
        run_ws_scenario(scenario)
        assert temp_db.get_chat(cid)["model"] == "other-model"
        assert layer.model_changes == []


class _LiveTerminal:
    """A chat's live terminal as the pick reads it: who may drive it (the
    real rule), what keeps it busy, and how it was closed."""

    may_drive = InteractiveOutputMixin.may_drive
    runs_for_nobody = InteractiveOutputMixin.runs_for_nobody
    sole_driver = InteractiveOutputMixin.sole_driver

    def __init__(self, chat_id, session_id, *, driver="user-admin"):
        self.chat_id, self.session_id, self.user_sub = chat_id, session_id, driver
        self.agent_name, self.target, self.is_remote = "agent", "local", False
        self.username, self.chat_is_lane = driver, False
        self.alive, self.turn_open, self.question_parked = True, False, False
        self._background_reported, self.otodock_attached = False, False
        self.submit_settling, self.holds_unsent_text = False, False
        self.idle_seconds, self.token_expires_at, self.created_at = 0.0, 1e12, 0.0
        self.closed: list[str] = []
        self.stays_live = False

    async def close(self, *, reason: str = "closed") -> None:
        self.closed.append(reason)
        # A close that fails leaves the terminal live (``close_for_chat``
        # swallows its error).
        self.alive = self.stays_live


def _terminal_chat(temp_db, monkeypatch, *, agent="", owner="user-admin", driver="user-admin"):
    """A chat stored in Ask with its live terminal, and its agent."""
    from core.session import interactive_session
    from tests.session.test_rebuild_characterization import _chat
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    agent = agent or make_test_agent()
    cid = _chat(agent, session_id="sid-term", user_sub=owner)
    term = _LiveTerminal(cid, "sid-term", driver=driver)
    monkeypatch.setitem(interactive_session._sessions, "sid-term", term)
    return cid, agent, layer, term


def _pick(cid, agent, layer, mode, *, user_sub="user-admin", bound=True, theme="dark",
          role=""):
    from tests.session.test_rebuild_characterization import _connection
    conn = _connection(cid if bound else "chat-elsewhere", "sid-term" if bound else None,
                       agent=agent, layer=layer, user_sub=user_sub)
    if role:
        conn.agent_roles = {agent: role}
    asyncio.run(conn._handle_mode_change({"mode": mode, "chat_id": cid, "theme": theme}))
    return conn


class TestAPickOnALiveTerminal:
    """A pick on a chat whose terminal is live starts it again in the mode,
    whichever socket the page holds; a busy terminal, a colleague's, a task
    run's or one built for nobody takes no pick, and the row stays."""

    def test_a_pick_starts_the_terminal_again_in_it(self, temp_db, monkeypatch):
        from services.remote import chat_reload
        watched: list[str] = []
        monkeypatch.setattr(chat_reload, "_watch_terminal", watched.append)
        for bound in (True, False):
            cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
            conn = _pick(cid, agent, layer, "dontAsk", bound=bound)
            assert term.closed == ["mode pick"], bound
            # The chat's other viewers are told when the new terminal is up.
            assert watched[-1] == cid, bound
            row = temp_db.get_chat(cid)
            assert (row["permission_mode"], row["execution_mode"]) == ("dontAsk", "")
            assert [{k: w[k] for k in ("chat_id", "permission_mode", "execution_mode", "theme",
                                       "background")} for w in conn.warmups] == [
                {"chat_id": cid, "permission_mode": "dontAsk", "execution_mode": "",
                 "theme": "dark", "background": False}], bound
            history = [f for f in conn.sent if f["type"] == "chat_history"]
            assert [f["mode"] for f in history] == ["dontAsk"]
            assert conn.detached == 1 and conn.session_id is None

    def test_the_mode_already_stored_restarts_nothing(self, temp_db, monkeypatch):
        """The row and the terminal both run the pick (no record yet reads as
        the row): nothing to do."""
        from core.session import session_state
        for recorded in ("", "ask"):
            cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
            if recorded:
                monkeypatch.setitem(session_state._session_modes, "sid-term", recorded)
            conn = _pick(cid, agent, layer, "ask")
            assert term.closed == [] and conn.warmups == [], recorded
            assert conn.sent == [{"type": "mode_changed", "mode": "ask", "chat_id": cid}]

    def test_a_pick_equal_to_the_row_restarts_a_terminal_that_runs_another_mode(
            self, temp_db, monkeypatch):
        """The row says Ask while the terminal runs Don't Ask (the button
        shows the terminal's): a pick of Ask starts it again in Ask, never an
        ack that leaves Don't Ask running."""
        from core.session import session_state
        cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
        monkeypatch.setitem(session_state._session_modes, "sid-term", "dontAsk")
        conn = _pick(cid, agent, layer, "ask")
        assert term.closed == ["mode pick"]
        assert [w["permission_mode"] for w in conn.warmups] == ["ask"]
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"

    def test_a_close_that_leaves_the_terminal_live_writes_nothing(self, temp_db, monkeypatch):
        from ws import dashboard_restart as restart
        cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
        term.stays_live = True
        conn = _pick(cid, agent, layer, "dontAsk")
        assert term.closed == ["mode pick"] and conn.warmups == []
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask",
                              "refusal": restart.BUSY}]

    def test_a_task_chats_terminal_keeps_its_mode_after_its_run(self, temp_db, monkeypatch):
        """A task run's chat whose run has ended, its terminal still live: the
        pick is answered with the stored mode, the terminal left alone."""
        from core.session import interactive_session
        from storage import database as task_store
        from tests.session.test_rebuild_characterization import _connection
        from ws import dashboard as ws_dashboard
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        task_store.create_run(run_id, "dyn-x", slug, "manual", None, "go",
                              "one-time", "user", "user-admin")
        cid = f"task-{run_id}"
        task_store.create_chat(cid, "user-admin", slug, "ask", execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id="sid-task")

        async def _ended(chat_id):
            return False
        monkeypatch.setattr(ws_dashboard, "task_run_active_async", _ended)
        term = _LiveTerminal(cid, "sid-task")
        monkeypatch.setitem(interactive_session._sessions, "sid-task", term)
        conn = _connection(cid, "sid-task", agent=slug, layer=layer)
        asyncio.run(conn._handle_mode_change({"mode": "dontAsk", "chat_id": cid}))
        assert term.closed == [] and conn.warmups == []
        assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask"}]
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"

    def test_a_busy_terminal_takes_no_pick(self, temp_db, monkeypatch):
        from core.remote import satellite_connection
        from core.session import session_state, warmup_registry
        from ws import dashboard_restart as restart

        def turn(term, m):
            term.turn_open = True

        def question(term, m):
            term.question_parked = True

        def card(term, m):
            m.setattr(session_state, "has_pending_prompt", lambda sid: sid == "sid-term")

        def background(term, m):
            term._background_reported = True
            session_state.record_hook_activity("sid-term")

        def held(term, m):
            asyncio.run(warmup_registry.register_new(term.chat_id, "user-other", "agent"))

        def reconnecting(term, m):
            term.is_remote, term.target = True, "m-away"
            mgr = satellite_connection.get_connection_manager()
            m.setattr(mgr, "is_pty_in_grace", lambda machine: machine == "m-away")

        def local(term, m):
            term.otodock_attached = True
            term.holds_unsent_text = True   # an attached otodock terminal holds, too

        def settling(term, m):
            term.submit_settling = True     # an Enter whose turn is not open yet

        def unsent(term, m):
            term.holds_unsent_text = True

        cases = {turn: restart.WORKING, question: restart.ASKING, card: restart.CARD,
                 background: restart.BACKGROUND, held: restart.BUSY,
                 reconnecting: restart.RECONNECTING, local: restart.LOCAL,
                 settling: restart.WORKING, unsent: restart.UNSENT}
        from services.remote import chat_reload
        monkeypatch.setattr(chat_reload, "_watch_terminal", lambda cid: pytest.fail("no restart"))
        for make_busy, note in cases.items():
            cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
            with monkeypatch.context() as m:
                make_busy(term, m)
                try:
                    conn = _pick(cid, agent, layer, "dontAsk")
                finally:
                    asyncio.run(warmup_registry.unregister(cid))
                    session_state._session_hook_activity.pop("sid-term", None)
            assert term.closed == [] and conn.warmups == [], make_busy.__name__
            assert temp_db.get_chat(cid)["permission_mode"] == "ask", make_busy.__name__
            assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask",
                                  "refusal": note}], make_busy.__name__

    def test_what_comes_while_the_hold_is_taken_refuses_the_pick(self, temp_db, monkeypatch):
        """Text typed into the terminal, its machine's reconnect grace or the
        otodock command taking the session, after the pick's first read and
        before the close (the hold's own awaits): the pick is refused with
        the same sentence, nothing closed or written."""
        from core.remote import satellite_connection
        from core.session import warmup_registry
        from services.remote import chat_reload
        from ws import dashboard_restart as restart

        def unsent(term, m):
            term.holds_unsent_text = True

        def reconnecting(term, m):
            term.is_remote, term.target = True, "m-away"
            mgr = satellite_connection.get_connection_manager()
            m.setattr(mgr, "is_pty_in_grace", lambda machine: machine == "m-away")

        def local(term, m):
            term.otodock_attached = True

        monkeypatch.setattr(chat_reload, "_watch_terminal", lambda cid: pytest.fail("no restart"))
        real_register = warmup_registry.register_new
        cases = {unsent: restart.UNSENT, reconnecting: restart.RECONNECTING, local: restart.LOCAL}
        for comes, note in cases.items():
            cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
            with monkeypatch.context() as m:
                async def _register(chat_id, by, agent_name, *, _comes=comes, _m=m, _term=term):
                    _comes(_term, _m)
                    return await real_register(chat_id, by, agent_name)
                m.setattr(warmup_registry, "register_new", _register)
                conn = _pick(cid, agent, layer, "dontAsk")
            assert term.closed == [] and conn.warmups == [], comes.__name__
            assert temp_db.get_chat(cid)["permission_mode"] == "ask", comes.__name__
            assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask",
                                  "refusal": note}], comes.__name__

    def test_a_colleagues_terminal_on_a_shared_chat_takes_no_pick(self, temp_db, monkeypatch):
        """An editor may drive the Shared-only agent's chat, never the terminal
        a colleague runs on it: by the bound frame and by the chat's id alike,
        the read-only answer and the stored mode, the row and the terminal
        untouched."""
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        from tests.fixtures.ws_dashboard_harness import make_test_agent as _agent
        from ws import dashboard_restart as restart
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = _agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer2", slug, "editor", "test")
        for bound in (False, True):
            cid, agent, layer, term = _terminal_chat(
                temp_db, monkeypatch, agent=slug, owner=shared_chat_owner(slug), driver="user-admin")
            conn = _pick(cid, slug, layer, "dontAsk", user_sub="user-viewer2", bound=bound,
                         role="editor")
            assert term.closed == [] and conn.warmups == [], bound
            assert temp_db.get_chat(cid)["permission_mode"] == "ask", bound
            assert any(f.get("state") == "read_only" for f in conn.sent), conn.sent
            assert {"type": "mode_changed", "chat_id": cid, "mode": "ask",
                    "refusal": restart.DRIVEN} in conn.sent

    def test_a_terminal_built_for_nobody_takes_no_pick(self, temp_db, monkeypatch):
        """A wake built it the unattended way (its mode is a word no person
        picks): nobody drives it, so nobody's pick restarts it."""
        from core.session import session_state
        from ws import dashboard_restart as restart
        cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
        term.user_sub = ""
        monkeypatch.setitem(session_state._session_modes, "sid-term", "auto")
        conn = _pick(cid, agent, layer, "dontAsk")
        assert term.closed == [] and conn.warmups == []
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert {"type": "mode_changed", "chat_id": cid, "mode": "ask",
                "refusal": restart.DRIVEN} in conn.sent

    def test_a_word_the_engine_does_not_declare_is_refused(self, temp_db, monkeypatch):
        for word in ("auto", "judge"):
            cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
            conn = _pick(cid, agent, layer, word)
            assert term.closed == [] and conn.warmups == [], word
            assert {"type": "error", "message": f"Invalid mode: {word}"} in conn.sent

    def test_a_restart_from_another_chats_turn_waits(self, temp_db, monkeypatch):
        from ws import dashboard_restart as restart
        cid, agent, layer, term = _terminal_chat(temp_db, monkeypatch)
        from tests.session.test_rebuild_characterization import _connection
        conn = _connection("chat-streaming", None, agent=agent, layer=layer)
        conn.streaming = True
        asyncio.run(conn._handle_mode_change({"mode": "dontAsk", "chat_id": cid}))
        assert term.closed == [] and conn.warmups == []
        assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask",
                              "refusal": restart.BUSY}]


class TestAPickWhileTheTerminalStarts:
    """A chat whose terminal is being started has no live terminal yet, and
    its start read the row already: a pick then is refused and writes
    nothing, so the terminal and the row never part. A chat whose next start
    is headless, or with nothing starting, keeps the row write."""

    def _chat(self, temp_db, monkeypatch, execution_mode):
        from tests.session.test_rebuild_characterization import _chat
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        agent = make_test_agent()
        return _chat(agent, execution_mode=execution_mode), agent, layer

    def _pick(self, cid, agent, layer, bound):
        from tests.session.test_rebuild_characterization import _connection
        conn = _connection(cid if bound else "chat-elsewhere", None, agent=agent, layer=layer)
        asyncio.run(conn._handle_mode_change({"mode": "dontAsk", "chat_id": cid}))
        return conn

    def test_a_pick_while_the_terminal_starts_is_refused(self, temp_db, monkeypatch):
        from core.session import warmup_registry
        from ws import dashboard_restart as restart
        for bound in (True, False):
            cid, agent, layer = self._chat(temp_db, monkeypatch, "interactive")
            asyncio.run(warmup_registry.register_new(cid, "user-admin", agent))
            try:
                conn = self._pick(cid, agent, layer, bound)
            finally:
                asyncio.run(warmup_registry.unregister(cid))
            assert temp_db.get_chat(cid)["permission_mode"] == "ask", bound
            assert conn.sent == [{"type": "mode_changed", "chat_id": cid, "mode": "ask",
                                  "refusal": restart.STARTING}], bound
            assert conn.deferred_mode == "", bound

    def test_the_agents_terminal_default_counts_as_a_terminal_start(self, temp_db, monkeypatch):
        from core.session import warmup_registry
        from storage.agents import agent_store
        cid, agent, layer = self._chat(temp_db, monkeypatch, "")
        real = agent_store.get_agent
        monkeypatch.setattr(agent_store, "get_agent", lambda slug: {
            **(real(slug) or {}), "default_execution_mode": "interactive"})
        asyncio.run(warmup_registry.register_new(cid, "user-admin", agent))
        try:
            conn = self._pick(cid, agent, layer, True)
        finally:
            asyncio.run(warmup_registry.unregister(cid))
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert conn.sent[-1].get("refusal")

    def test_a_headless_start_or_none_keeps_the_row_write(self, temp_db, monkeypatch):
        from core.session import warmup_registry
        for exec_mode, starting in (("-p", True), ("interactive", False)):
            cid, agent, layer = self._chat(temp_db, monkeypatch, exec_mode)
            if starting:
                asyncio.run(warmup_registry.register_new(cid, "user-admin", agent))
            try:
                self._pick(cid, agent, layer, True)
            finally:
                asyncio.run(warmup_registry.unregister(cid))
            assert temp_db.get_chat(cid)["permission_mode"] == "dontAsk", exec_mode


class TestAStalePickNeverRaisesTheChatAgain:
    """A pick made before a terminal started reached the row; the terminal
    started in it and a Shift+Tab lowered the chat. Turning the terminal off
    then never brings the old pick back: the row's mode wins over the
    socket's deferral and over a mode owed to the session."""

    def test_the_deferral_yields_to_a_lowered_row(self, temp_db, monkeypatch):
        from core.session.session_state import get_session_mode, set_session_mode
        from storage import database as task_store
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-low")
        layer = FakeExecutionLayer()

        async def scenario():
            conn = _bare_connection(cid, None, user_sub="user-admin", agent=slug, layer=layer)
            conn.deferred_model = ""
            await conn._handle_mode_change({"mode": "dontAsk", "chat_id": cid})
            assert conn.deferred_mode == "dontAsk" and conn.deferred_for == cid
            # The terminal ran and a Shift+Tab lowered the chat to Ask.
            task_store.update_chat(cid, permission_mode="ask")
            # The terminal off: a headless session starts in the row's mode.
            conn.session_id = "sid-low"
            layer.alive.add("sid-low")
            set_session_mode("sid-low", "ask")
            await conn._reapply_deferred_after_warmup(cid, from_warmup=True)
            return conn
        conn = asyncio.run(scenario())
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert get_session_mode("sid-low") == "ask"
        assert layer.mode_changes == [] and conn.deferred_mode == ""

    def test_an_owed_mode_yields_to_the_sessions_own(self, temp_db, monkeypatch):
        from core.session.session_state import set_session_mode
        from ws import dashboard_chat_support as support
        layer = FakeExecutionLayer()
        layer.alive.add("sid-owed")
        support.owe_engine_mode("sid-owed", "dontAsk")
        set_session_mode("sid-owed", "ask")  # the session started again, lowered
        asyncio.run(support.apply_owed_engine_mode(layer, "sid-owed"))
        assert layer.mode_changes == [] and "sid-owed" not in support._engine_mode_owed
