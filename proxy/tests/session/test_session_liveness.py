"""The session liveness authority (``core/session/session_state.py``): what
makes a session id live, the token floor a session's current life sets, and
the closing marks every registry pop leaves.

Run: cd proxy && venv/bin/pytest tests/session/test_session_liveness.py -v
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from core.session import session_state


@pytest.fixture(autouse=True)
def _clean():
    session_state.reset_liveness_for_tests()
    yield
    session_state.reset_liveness_for_tests()


@pytest.fixture
def clock(monkeypatch):
    """The tables' clock, advanced by the test."""
    state = {"now": 1000.0}
    monkeypatch.setattr(session_state, "_now", lambda: state["now"])
    monkeypatch.setattr(session_state, "_next_sweep", 0.0)
    return state


def _sid() -> str:
    return str(uuid.uuid4())


class TestLiveness:
    def test_nothing_makes_an_unknown_id_live(self):
        assert not session_state.session_is_live(_sid())
        assert not session_state.session_is_live("")
        assert not session_state.session_is_held("")

    @pytest.mark.parametrize("registry", [
        "core.layers.cli.session._persistent_sessions",
        "core.layers.codex.session._codex_sessions",
        "core.layers.direct.session._direct_sessions",
        "core.session.interactive_session._sessions",
    ])
    def test_a_registry_entry_makes_the_id_held_and_live(self, registry):
        import importlib
        module_name, name = registry.rsplit(".", 1)
        table = getattr(importlib.import_module(module_name), name)
        sid = _sid()
        table[sid] = object()
        try:
            assert session_state.session_is_held(sid)
            assert session_state.session_is_live(sid)
        finally:
            table.pop(sid, None)
        assert not session_state.session_is_live(sid)

    def test_the_remote_registry_counts(self, monkeypatch):
        from core.session import session_manager

        class _Remote:
            def __init__(self):
                self.ids = set()

            def owns_session(self, sid):
                return sid in self.ids

        remote = _Remote()
        monkeypatch.setattr(session_manager, "_remote_layer", remote)
        sid = _sid()
        remote.ids.add(sid)
        assert session_state.session_is_held(sid)
        remote.ids.discard(sid)
        assert not session_state.session_is_live(sid)

    def test_the_headless_set_counts(self):
        sid = f"appx-{uuid.uuid4().hex[:12]}"
        session_state.mark_headless_live(sid)
        assert session_state.session_is_held(sid)
        session_state.clear_headless_live(sid)
        assert not session_state.session_is_live(sid)

    def test_starting_and_closing_are_live_inside_their_windows(self, clock):
        a, b = _sid(), _sid()
        session_state.mark_starting(a, 20)
        session_state.mark_closing(b)
        assert session_state.session_is_live(a) and not session_state.session_is_held(a)
        assert session_state.session_is_live(b) and not session_state.session_is_held(b)
        clock["now"] += 19
        assert session_state.session_is_live(a)
        assert session_state.session_is_live(b)
        clock["now"] += 2
        assert not session_state.session_is_live(a)
        assert session_state.session_is_live(b)
        clock["now"] += session_state.CLOSING_WINDOW_S
        assert not session_state.session_is_live(b)

    def test_clear_starting_ends_the_mark_and_a_re_mark_extends_it(self, clock):
        sid = _sid()
        session_state.mark_starting(sid, 10)
        clock["now"] += 8
        session_state.mark_starting(sid, 10)
        clock["now"] += 8
        assert session_state.session_is_live(sid)
        session_state.clear_starting(sid)
        assert not session_state.session_is_live(sid)

    def test_a_closing_mark_of_zero_ends_at_once(self, clock):
        sid = _sid()
        session_state.mark_closing(sid, 0)
        clock["now"] += 0.001
        assert not session_state.session_is_live(sid)

    def test_the_sweep_drops_expired_marks_and_orphaned_floors(self, clock):
        a, b = _sid(), _sid()
        session_state.mark_starting(a, 5)
        session_state.register_session_state(b, "auto", None, token_minted_at=5)
        assert b in session_state._session_token_floor
        clock["now"] += session_state._SWEEP_EVERY_S + 10
        session_state.session_is_live(_sid())
        assert a not in session_state._starting
        assert b not in session_state._session_token_floor


class TestFloor:
    def test_a_fresh_registration_sets_the_recorded_mint(self):
        sid = _sid()
        session_state.register_session_state(sid, "auto", None, token_minted_at=1_234)
        assert session_state._session_token_floor[sid] == 1_234

    def test_a_builder_that_minted_nothing_gets_the_registration_instant(self, monkeypatch):
        import time
        monkeypatch.setattr(time, "time", lambda: 5_000.7)
        sid = _sid()
        session_state.register_session_state(sid, "auto", None)
        assert session_state._session_token_floor[sid] == 5_000

    def test_a_re_warm_of_a_held_session_keeps_the_floor(self):
        from core.layers.direct.session import _direct_sessions
        sid = _sid()
        session_state.register_session_state(sid, "auto", None, token_minted_at=100)
        _direct_sessions[sid] = object()
        try:
            session_state.register_session_state(sid, "auto", None, token_minted_at=300)
            assert session_state._session_token_floor[sid] == 100
            assert session_state.session_token_refusal({"sid": sid, "iat": 100}) == ""
            assert session_state.session_token_refusal({"sid": sid, "iat": 300}) == ""
            assert session_state.session_token_refusal({"sid": sid, "iat": 99}) == "stale"
        finally:
            _direct_sessions.pop(sid, None)

    def test_a_starting_mark_alone_does_not_hold_the_floor(self):
        sid = _sid()
        session_state.mark_starting(sid, 60)
        session_state.register_session_state(sid, "auto", None, token_minted_at=100)
        session_state.register_session_state(sid, "auto", None, token_minted_at=300)
        assert session_state._session_token_floor[sid] == 300

    def test_cleanup_pops_the_floor_and_a_new_life_sets_a_new_one(self):
        sid = _sid()
        session_state.register_session_state(sid, "auto", None, token_minted_at=100)
        session_state.cleanup_session_permission_state(sid)
        assert sid not in session_state._session_token_floor
        session_state.register_session_state(sid, "auto", None, token_minted_at=200)
        assert session_state._session_token_floor[sid] == 200

    def test_the_verdicts(self, clock):
        from core.layers.direct.session import _direct_sessions
        sid = _sid()
        assert session_state.session_token_refusal({"sid": sid, "iat": 1}) == "not live"
        assert session_state.session_token_refusal({"sid": "", "iat": 1}) == "not live"
        assert session_state.session_token_refusal({"sid": 7, "iat": 1}) == "not live"
        session_state.register_session_state(sid, "auto", None, token_minted_at=100)
        session_state.mark_starting(sid, 60)
        # Only starting: the comparison is skipped.
        assert session_state.session_token_refusal({"sid": sid, "iat": 1}) == ""
        _direct_sessions[sid] = object()
        try:
            assert session_state.session_token_refusal({"sid": sid, "iat": 1}) == "stale"
            assert session_state.session_token_refusal({"sid": sid, "iat": 100}) == ""
            assert session_state.session_token_refusal({"sid": sid}) == ""
            assert session_state.session_token_refusal({"sid": sid, "iat": "1"}) == ""
        finally:
            _direct_sessions.pop(sid, None)
        session_state.clear_starting(sid)
        session_state.mark_closing(sid)
        # Only closing: the comparison is skipped too.
        assert session_state.session_token_refusal({"sid": sid, "iat": 1}) == ""


class TestStartMarks:
    """Every layer start marks its session starting for the whole start and
    clears the mark in a ``finally``; the floor is the config's recorded
    mint."""

    @pytest.mark.parametrize("kind", ["cli", "codex", "direct"])
    @pytest.mark.asyncio
    async def test_a_local_start_is_live_throughout_and_sets_the_floor(self, kind, monkeypatch):
        from tests.execution.test_session_permission_cleanup import _config, _layer
        layer = _layer(kind)
        sid = _sid()
        seen = {}

        async def _impl(session_id, config):
            seen["live"] = session_state.session_is_live(session_id)
            seen["starting"] = session_id in session_state._starting
            seen["floor"] = session_state._session_token_floor.get(session_id)

        monkeypatch.setattr(layer, "_start_session_impl", _impl)
        cfg = _config()
        cfg.token_minted_at = 4_242
        try:
            await layer.start_session(sid, cfg)
            assert seen == {"live": True, "starting": True, "floor": 4_242}
            assert sid not in session_state._starting
        finally:
            session_state.cleanup_session_permission_state(sid)

    @pytest.mark.parametrize("kind", ["cli", "codex", "direct"])
    @pytest.mark.asyncio
    async def test_a_failed_local_start_is_not_live(self, kind, monkeypatch):
        from tests.execution.test_session_permission_cleanup import _config, _layer
        layer = _layer(kind)
        sid = _sid()

        async def _boom(session_id, config):
            raise RuntimeError("spawn failed")

        monkeypatch.setattr(layer, "_start_session_impl", _boom)
        with pytest.raises(RuntimeError):
            await layer.start_session(sid, _config())
        assert not session_state.session_is_live(sid)
        assert sid not in session_state._session_token_floor

    @pytest.mark.asyncio
    async def test_a_remote_start_registers_before_the_payload_and_cleans_up_a_failure(self, monkeypatch):
        from unittest.mock import MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer
        from tests.execution.test_session_permission_cleanup import _config
        cm = MagicMock()
        cm.is_connected.return_value = True
        cm.machine_at_capacity.return_value = False
        cm.satellite_engines.return_value = {"claude-code-cli"}
        layer = RemoteExecutionLayer(cm)
        sid = _sid()
        seen = {}

        async def _payload(session_id, config, execution_path):
            seen["ctx"] = session_state.get_session_security(session_id)
            seen["mode"] = session_state.get_session_mode(session_id)
            seen["live"] = session_state.session_is_live(session_id)
            seen["floor"] = session_state._session_token_floor.get(session_id)
            raise RuntimeError("payload failed")

        monkeypatch.setattr(layer, "_build_start_payload", _payload)
        cfg = _config()
        cfg.execution_target = "machine-1"
        cfg.execution_path = "claude-code-cli"
        cfg.token_minted_at = 77
        with pytest.raises(RuntimeError):
            await layer.start_session(sid, cfg)
        assert seen["ctx"] is not None and seen["ctx"].external_claim == "phone:+3021"
        assert seen == {**seen, "mode": "auto", "live": True, "floor": 77}
        assert session_state.get_session_security(sid) is None
        assert not session_state.session_is_live(sid)


    @pytest.mark.parametrize("kind,registry", [
        ("cli", "core.layers.cli.session._persistent_sessions"),
        ("codex", "core.layers.codex.session._codex_sessions"),
        ("direct", "core.layers.direct.session._direct_sessions"),
    ])
    @pytest.mark.asyncio
    async def test_a_failed_re_warm_of_a_held_session_keeps_its_state(self, kind, registry, monkeypatch):
        import importlib
        from tests.execution.test_session_permission_cleanup import _config, _ctx, _layer
        layer = _layer(kind)
        module_name, name = registry.rsplit(".", 1)
        table = getattr(importlib.import_module(module_name), name)
        sid = _sid()
        session_state.register_session_state(sid, "default", _ctx(), token_minted_at=500)
        table[sid] = object()

        async def _boom(session_id, config):
            raise RuntimeError("re-warm failed")

        monkeypatch.setattr(layer, "_start_session_impl", _boom)
        try:
            with pytest.raises(RuntimeError):
                await layer.start_session(sid, _config())
            assert session_state.get_session_security(sid) is not None
            assert session_state._session_token_floor.get(sid) == 500
            assert session_state.session_is_live(sid)
        finally:
            table.pop(sid, None)
            session_state.cleanup_session_permission_state(sid)

    @pytest.mark.asyncio
    async def test_a_remote_start_refused_before_it_registers_leaves_state_alone(self):
        from unittest.mock import MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer
        from tests.execution.test_session_permission_cleanup import _config, _ctx
        cm = MagicMock()
        cm.is_connected.return_value = False
        layer = RemoteExecutionLayer(cm)
        sid = _sid()
        # A context the security index reloaded, its session awaiting re-adoption.
        session_state.set_session_security(sid, _ctx())
        cfg = _config()
        cfg.execution_target = "machine-1"
        try:
            with pytest.raises(RuntimeError):
                await layer.start_session(sid, cfg)
            assert session_state.get_session_security(sid) is not None
        finally:
            session_state.cleanup_session_permission_state(sid)

    @pytest.mark.asyncio
    async def test_a_failed_remote_re_warm_of_a_held_session_keeps_its_state(self, monkeypatch):
        from unittest.mock import MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.session import session_manager
        from tests.execution.test_session_permission_cleanup import _config, _ctx
        cm = MagicMock()
        cm.is_connected.return_value = True
        cm.machine_at_capacity.return_value = False
        cm.satellite_engines.return_value = {"claude-code-cli"}
        layer = RemoteExecutionLayer(cm)
        monkeypatch.setattr(session_manager, "_remote_layer", layer)
        sid = _sid()
        session_state.register_session_state(sid, "default", _ctx(), token_minted_at=500)
        layer._sessions[sid] = MagicMock()

        async def _payload(session_id, config, execution_path):
            raise RuntimeError("payload failed")

        monkeypatch.setattr(layer, "_build_start_payload", _payload)
        cfg = _config()
        cfg.execution_target = "machine-1"
        cfg.execution_path = "claude-code-cli"
        try:
            with pytest.raises(RuntimeError):
                await layer.start_session(sid, cfg)
            assert session_state.get_session_security(sid) is not None
            assert session_state._session_token_floor.get(sid) == 500
        finally:
            layer._sessions.pop(sid, None)
            session_state.cleanup_session_permission_state(sid)


class TestClosingMarks:
    """Every registry pop marks the id closing, only when an entry was popped."""

    @pytest.mark.asyncio
    async def test_cli_close_and_the_dead_drops(self):
        from core.layers.cli import session as cli
        from core.layers.cli.layer import CLIExecutionLayer

        class _Fake:
            is_alive = False
            is_starting = False
            proc = None

            async def close(self):
                pass

        sid = _sid()
        cli._persistent_sessions[sid] = _Fake()
        assert await cli.close_persistent_session(sid)
        assert session_state.session_is_live(sid) and not session_state.session_is_held(sid)
        session_state.reset_liveness_for_tests()
        assert not await cli.close_persistent_session(sid)
        assert not session_state.session_is_live(sid)

        cli._persistent_sessions[sid] = _Fake()
        assert await cli.get_persistent_session(sid) is None
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()

        cli._persistent_sessions[sid] = _Fake()
        await CLIExecutionLayer().prepare_resume(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()
        await CLIExecutionLayer().prepare_resume(sid)
        assert not session_state.session_is_live(sid)

        starting = _Fake()
        starting.is_starting = True
        cli._persistent_sessions[sid] = starting
        assert await cli.abort_persistent_session(sid)
        assert session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_cli_failed_start(self, monkeypatch):
        from core.layers.cli import session as cli

        async def boom(self):
            raise OSError("spawn failed")

        monkeypatch.setattr(cli.PersistentSession, "start", boom)
        monkeypatch.setattr("core.concurrency.release_chat_slot", lambda sid: None)
        monkeypatch.setattr("services.engines.subscription_pool.release_subscription",
                            lambda sid: None)
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        with pytest.raises(OSError):
            await cli.get_or_create_persistent_session(
                session_id=sid, agent_prompt=None, mcp_config_path=None,
                model="claude-opus-5", agent_name="agent",
            )
        assert sid not in cli._persistent_sessions
        assert session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_codex_close_the_closed_drop_and_a_failed_start(self, monkeypatch):
        from core.layers.codex import session as codex

        class _Fake:
            is_alive = False
            _closed = True
            config_dir = ""

            async def close(self):
                pass

        sid = _sid()
        codex._codex_sessions[sid] = _Fake()
        assert await codex.close_codex_session(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()
        assert not await codex.close_codex_session(sid)
        assert not session_state.session_is_live(sid)

        codex._codex_sessions[sid] = _Fake()
        await codex.get_codex_session(sid)   # a closed entry is dropped
        assert sid not in codex._codex_sessions
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()

        async def boom(self):
            raise OSError("spawn failed")

        async def quiet(self):
            pass

        monkeypatch.setattr(codex.CodexAppServerSession, "start", boom)
        monkeypatch.setattr(codex.CodexAppServerSession, "close", quiet)
        with pytest.raises(OSError):
            await codex.create_codex_session(sid, "agent", "m")
        assert sid not in codex._codex_sessions
        assert session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_direct_close_and_the_reaper(self, monkeypatch):
        import time
        from core.layers.direct import session as direct

        sid = _sid()
        direct._direct_sessions[sid] = object()
        assert await direct.close_direct_session(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()
        assert not await direct.close_direct_session(sid)
        assert not session_state.session_is_live(sid)

        async def _reap_idle(timeout=0):
            pass

        monkeypatch.setattr(direct.mcp_pool, "reap_idle", _reap_idle)
        monkeypatch.setattr("core.concurrency.release_chat_slot", lambda s: None)
        monkeypatch.setattr("services.engines.subscription_pool.release_subscription",
                            lambda s: None)
        direct._direct_sessions[sid] = type("S", (), {"last_activity": time.monotonic() - 10**6})()
        await direct._reap_idle_direct_pass()
        assert sid not in direct._direct_sessions
        assert session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_remote_close_and_prepare_resume(self):
        from unittest.mock import AsyncMock, MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo
        cm = MagicMock()
        cm.send_command = AsyncMock()
        layer = RemoteExecutionLayer(cm)

        def _info(sid):
            return RemoteSessionInfo(session_id=sid, machine_id="m1", agent_name="a",
                                     execution_path="claude-code-cli",
                                     event_queue=asyncio.Queue())

        sid = _sid()
        layer._sessions[sid] = _info(sid)
        session_state.mark_starting(sid, 60)
        await layer.close_session(sid)
        assert sid not in session_state._starting
        assert session_state.session_is_live(sid) and not session_state.session_is_held(sid)
        session_state.reset_liveness_for_tests()
        await layer.close_session(sid)
        assert not session_state.session_is_live(sid)

        layer._sessions[sid] = _info(sid)
        await layer.prepare_resume(sid)
        assert session_state.session_is_live(sid)
        session_state.reset_liveness_for_tests()
        await layer.prepare_resume(sid)
        assert not session_state.session_is_live(sid)

    @pytest.mark.asyncio
    async def test_headless_dispose(self):
        from services.apps import headless_exec as hx

        class _Mgr:
            async def close(self):
                pass

        sid = f"appx-{uuid.uuid4().hex[:12]}"
        session_state.mark_headless_live(sid)
        assert session_state.session_is_held(sid)
        await hx._dispose(hx._Entry(_Mgr(), sid, "", frozenset()))
        assert not session_state.session_is_held(sid)
        assert session_state.session_is_live(sid)


class TestReadoptionMarks:
    """A satellite's report after a restart marks the sessions it kept
    alive as starting, for the ones that have a reloaded context and an
    engine whose sessions are re-adopted; the adoption paths clear them."""

    def test_reported_sids_are_marked_by_context_and_engine(self):
        from auth.path_policy import SecurityContext
        from services.scheduler.run_recovery import _mark_reported_starting
        from core import placement
        with_ctx, no_ctx, codex, direct = _sid(), _sid(), _sid(), _sid()
        ctx = SecurityContext(role="manager", username="", agent="pa", is_admin_agent=False,
                              placement=placement.PlacementCapabilities(
                                  kind="admin_remote", machine_id="m1"))
        session_state.set_session_security(with_ctx, ctx)
        session_state.set_session_security(codex, ctx)
        session_state.set_session_security(direct, ctx)
        try:
            _mark_reported_starting({
                with_ctx: {"execution_path": "claude-code-cli"},
                no_ctx: {"execution_path": "claude-code-cli"},
                "": {"execution_path": "claude-code-cli"},
            }, "m1", {
                codex: {"execution_path": "codex-cli"},       # taken back idle
                direct: {"execution_path": "direct-llm"},     # never taken back
            })
            assert session_state.session_is_live(with_ctx)
            assert not session_state.session_is_live(no_ctx)
            assert session_state.session_is_live(codex)
            assert not session_state.session_is_live(direct)
            # A context placed on another machine is not this report's, nor
            # is a local one (it names no machine and runs on no satellite).
            elsewhere, local = _sid(), _sid()
            session_state.set_session_security(elsewhere, SecurityContext(
                role="manager", username="", agent="pa", is_admin_agent=False,
                placement=placement.PlacementCapabilities(kind="remote", machine_id="m2")))
            session_state.set_session_security(local, SecurityContext(
                role="manager", username="", agent="pa", is_admin_agent=False))
            _mark_reported_starting({elsewhere: {"execution_path": "claude-code-cli"},
                                     local: {"execution_path": "claude-code-cli"}}, "m1")
            assert not session_state.session_is_live(elsewhere)
            assert not session_state.session_is_live(local)
            session_state.cleanup_session_permission_state(elsewhere)
            session_state.cleanup_session_permission_state(local)
        finally:
            session_state.cleanup_session_permission_state(with_ctx)
            session_state.cleanup_session_permission_state(codex)
            session_state.cleanup_session_permission_state(direct)
            session_state.clear_starting(codex)

    @pytest.mark.asyncio
    async def test_a_readoption_restores_the_mode_and_the_running_tokens_floor(self, monkeypatch):
        """The session comes back in the mode it had (never the default
        ``auto``) and its floor is the persisted one, so the running
        process's token still passes and an earlier life's does not."""
        from unittest.mock import MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.session import session_manager
        layer = RemoteExecutionLayer(MagicMock())
        monkeypatch.setattr(session_manager, "_remote_layer", layer)
        monkeypatch.setattr(layer, "_restore_adopted_credentials", lambda *a, **k: None)
        sid = _sid()
        session_state.mark_starting(sid, 60)
        minted = int(time.time()) - 3600  # the process's token, an hour old
        await layer.adopt_idle_session(machine_id="m1", session_id=sid, agent_name="pa",
                                       execution_path="claude-code-cli",
                                       mode="default", token_floor=minted)
        try:
            assert session_state.get_session_mode(sid) == "default"
            assert layer._sessions[sid].mode == "default"
            assert session_state.session_token_refusal({"sid": sid, "iat": minted}) == ""
            assert session_state.session_token_refusal({"sid": sid, "iat": minted - 10}) == "stale"
        finally:
            layer._sessions.pop(sid, None)
            session_state.cleanup_session_permission_state(sid)

    @pytest.mark.asyncio
    async def test_the_adoption_clears_the_mark(self, monkeypatch):
        from unittest.mock import MagicMock
        from core.remote.remote_execution import RemoteExecutionLayer
        from core.session import session_manager
        layer = RemoteExecutionLayer(MagicMock())
        monkeypatch.setattr(session_manager, "_remote_layer", layer)
        monkeypatch.setattr(layer, "_restore_adopted_credentials", lambda *a, **k: None)
        sid = _sid()
        session_state.mark_starting(sid, 60)
        assert await layer.adopt_idle_session(machine_id="m1", session_id=sid, agent_name="pa",
                                              execution_path="claude-code-cli") is True
        assert sid in layer._sessions
        assert sid not in session_state._starting
        assert session_state.session_is_held(sid)
        layer._sessions.pop(sid, None)

        declined = _sid()
        session_state.mark_starting(declined, 60)
        assert await layer.adopt_idle_session(machine_id="m1", session_id=declined,
                                              agent_name="pa",
                                              execution_path="direct-llm") is False
        assert declined not in layer._sessions
        assert not session_state.session_is_live(declined)


    @pytest.mark.asyncio
    async def test_a_reported_turn_this_recovery_will_not_adopt_loses_its_mark(self, temp_db):
        from auth.path_policy import SecurityContext
        from services.scheduler import run_recovery
        sid = _sid()
        ctx = SecurityContext(role="manager", username="", agent="pa", is_admin_agent=False)
        session_state.set_session_security(sid, ctx)
        try:
            # A live turn with no chat row behind it: nothing adopts it.
            await run_recovery.on_sessions_alive("m1", [
                {"session_id": sid, "execution_path": "claude-code-cli", "turn_active": True},
            ])
            assert not session_state.session_is_live(sid)
        finally:
            session_state.cleanup_session_permission_state(sid)


class TestBuilderFloors:
    """The builders that mint before the layer registers record the mint
    instant, and it is the token's ``iat``."""

    def test_the_chat_builder_records_its_mint(self, temp_db, monkeypatch, tmp_path):
        from auth.session_token import validate_session_token
        from storage.agents import agent_store
        from tests.execution.test_direct_llm_target_local import _build, _mk_user, _stub_builder
        agent_store.create_agent("dl", "DL", execution_path="direct-llm")
        _mk_user("sub-ada", "Ada")
        _stub_builder(monkeypatch, tmp_path)
        cfg = _build("dl", session_id=str(uuid.uuid4()))
        payload = validate_session_token(cfg.credential_env["PROXY_API_KEY"])
        assert cfg.token_minted_at > 0
        assert payload["iat"] == cfg.token_minted_at

    def test_the_wake_respawn_records_its_mint(self, temp_db, monkeypatch):
        from types import SimpleNamespace
        from auth.session_token import validate_session_token
        from core import placement
        from services.mcp import mcp_registry
        from services.scheduler import delivery
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [])
        vis = SimpleNamespace(mount_username="", memory_user_enabled=True,
                              memory_agent_enabled=True, effective_default_scope="agent",
                              available_scopes=("agent",), config_visible=False)
        _cfg, env, _multi, minted_at = delivery._wake_credential_env(
            "pa", str(uuid.uuid4()), user_sub="", role="manager", vis=vis,
            where=placement.LOCAL_PLACEMENT, mcp_config=None, flat_env={},
            bash_env_keys=set(), mcp_format="json",
        )
        assert minted_at > 0
        assert validate_session_token(env["PROXY_API_KEY"])["iat"] == minted_at
