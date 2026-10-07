"""The chat page's pre-warm gives back the pool seat it acquired whenever it
decides not to spawn, and never asks an engine for a foreign model.

`_handle_pre_warmup` builds the agent config FIRST (that is where the
subscription seat is acquired) and only then learns that the target is a
remote satellite or that the agent is interactive — both skip the spawn.
Returning without releasing leaked one seat per chat opened on such an
agent (T1 log 2026-09-10: two ``Pool: acquired`` lines per remote
interactive chat, counter 2 → 3, one seat never released — public issue
#3's "stale active_sessions"). Driven by direct method calls on a stub
controller; the WS-level flow is pinned by tests/session/test_ws_dashboard_*.
The detached pre-warm and the model-foreign check live in ws/dashboard_prewarm.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _controller():
    import ws.dashboard  # noqa: F401  (assembles the controller mixins first)
    from ws.dashboard_warmup import WarmupController

    conn = WarmupController()
    conn.user_sub = "user-pw"
    conn.user = {"username": "pw", "role": "manager"}
    conn._pre_warmed_sid = None
    conn._pre_warmed_agent = None
    conn._pre_warmed_exec_path = ""
    conn._pre_warmed_model = ""
    conn._pre_warmed_role = ""
    conn._pre_warmed_at = 0.0
    conn._can_access_agent = lambda name: True
    conn.sent = []
    conn.errors = []

    async def _send(data):
        conn.sent.append(data)

    async def _send_error(msg):
        conn.errors.append(msg)

    conn._send = _send
    conn._send_error = _send_error
    return conn


def _stub_seams(monkeypatch, *, built_cfg, model_layers=None):
    """Everything `_handle_pre_warmup` touches before and after the build."""
    from ws import dashboard_warmup as dw

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(dw, "run_db", _run_db)
    monkeypatch.setattr(dw.agent_store, "agent_exists", lambda name: True)
    monkeypatch.setattr(dw, "acting_role_of", lambda *a, **k: "manager")
    monkeypatch.setattr(dw.config, "get_cli_model", lambda name, **k: "some-model")
    monkeypatch.setattr(dw.config, "get_model_layers", lambda m: list(model_layers or []))
    # The ledger is not initialised here, and the real gate reads that as
    # "no room": the seat tests are about what happens AFTER the build.
    from core import concurrency
    monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: True)
    calls = []

    async def _build(**kwargs):
        calls.append(kwargs)
        return built_cfg

    monkeypatch.setattr(dw, "build_agent_config", _build)
    return calls


def _cfg(**over):
    base = dict(
        subscription_id="sub-S", execution_target="local",
        sandbox_host_claude_dir="/agents/a/.codex", execution_path="codex-cli",
        model="some-model", interactive=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_remote_target_skip_returns_the_seat(monkeypatch):
    from services.engines import subscription_pool as sp
    conn = _controller()
    calls = _stub_seams(monkeypatch, built_cfg=_cfg(execution_target="mach-790546a4"))
    with patch.object(sp, "subscription_store") as store:
        await conn._handle_pre_warmup({"agent": "alpha", "model": "some-model",
                                       "execution_path": "codex-cli"})
    assert len(calls) == 1
    store.decrement_active_sessions.assert_called_once_with("sub-S")
    assert conn.errors == []
    assert conn._pre_warmed_sid is None


@pytest.mark.asyncio
async def test_interactive_skip_returns_the_seat(monkeypatch):
    from services.engines import subscription_pool as sp
    from ws import dashboard_warmup as dw
    conn = _controller()
    _stub_seams(monkeypatch, built_cfg=_cfg())
    monkeypatch.setattr(dw, "_resolve_session_interactive", lambda cfg, *a: True)
    with patch.object(sp, "subscription_store") as store:
        await conn._handle_pre_warmup({"agent": "alpha", "model": "some-model",
                                       "execution_path": "codex-cli"})
    store.decrement_active_sessions.assert_called_once_with("sub-S")
    assert conn.errors == []


@pytest.mark.asyncio
async def test_foreign_model_skips_before_building(monkeypatch):
    # The frame pairs the page's Claude model with a Codex pre-warm: building
    # would filter the Codex candidates by provider anthropic and refuse
    # (the T1 "no borrowable platform credentials" of 2026-09-10 05:07:59).
    from services.engines import subscription_pool as sp
    conn = _controller()
    calls = _stub_seams(monkeypatch, built_cfg=_cfg(),
                        model_layers=["claude-code-cli", "direct-llm"])
    with patch.object(sp, "subscription_store") as store:
        await conn._handle_pre_warmup({"agent": "alpha", "model": "claude-sonnet-5",
                                       "execution_path": "codex-cli"})
    assert calls == []
    store.decrement_active_sessions.assert_not_called()
    assert conn.errors == []


def test_model_foreign_to_engine_trusts_unknown_models(monkeypatch):
    import ws.dashboard  # noqa: F401  (the pre-warm module needs it loaded first)
    from ws import dashboard_prewarm as dp
    monkeypatch.setattr(dp.config, "get_model_layers", lambda m: [])
    assert dp._model_foreign_to_engine("my-local-model", "codex-cli") is False
    monkeypatch.setattr(dp.config, "get_model_layers", lambda m: ["claude-code-cli"])
    assert dp._model_foreign_to_engine("claude-sonnet-5", "codex-cli") is True
    assert dp._model_foreign_to_engine("claude-sonnet-5", "claude-code-cli") is False
    assert dp._model_foreign_to_engine("", "codex-cli") is False


@pytest.mark.asyncio
async def test_detached_prewarm_skips_return_the_seat(monkeypatch):
    import ws.dashboard  # noqa: F401  (the pre-warm module needs it loaded first)
    from services.engines import subscription_pool as sp
    from ws import dashboard_prewarm as dp

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)

    # The detached spawn's own globals live on ws.dashboard_prewarm.
    monkeypatch.setattr(dp, "run_db", _run_db)
    monkeypatch.setattr(dp, "acting_role_of", lambda *a, **k: "manager")
    monkeypatch.setattr(dp, "resolve_execution_path", lambda agent, p: "codex-cli")
    monkeypatch.setattr(dp.config, "get_model_layers", lambda m: [])
    from core import concurrency
    monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: True)

    async def _build(**kwargs):
        return _cfg(execution_target="mach-1")

    monkeypatch.setattr(dp, "build_agent_config", _build)
    with patch.object(sp, "subscription_store") as store:
        sid = await dp.spawn_detached_prewarm(
            agent="alpha", user={"username": "pw", "role": "manager"},
            user_sub="user-pw", requested_model="some-model",
        )
    assert sid is None
    store.decrement_active_sessions.assert_called_once_with("sub-S")


@pytest.mark.asyncio
async def test_cancelled_prewarm_returns_the_seat_and_the_slot(monkeypatch):
    # A newer pre_warmup frame (or the socket closing) cancels the task
    # mid-spawn. CancelledError is no Exception: the rollback used to be
    # skipped and the seat the build took stayed taken until a restart.
    conn = _controller()
    from core import concurrency
    from services.engines import subscription_pool as sp
    from ws import dashboard_prewarm as dp
    from ws import dashboard_warmup as dw
    _stub_seams(monkeypatch, built_cfg=_cfg())
    monkeypatch.setattr(dw, "_resolve_session_interactive", lambda cfg, *a: False)
    spawning = asyncio.Event()
    closed, released = [], []

    class _Layer:
        async def start_session(self, sid, cfg):
            spawning.set()
            await asyncio.Event().wait()

        async def close_session(self, sid):
            closed.append(sid)

    async def _acquire(sid, **kw):
        return True

    monkeypatch.setattr(dw, "get_execution_layer", lambda *a, **k: _Layer())
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _acquire)
    monkeypatch.setattr(concurrency, "release_chat_slot", released.append)
    with patch.object(sp, "subscription_store") as store:
        task = asyncio.create_task(conn._handle_pre_warmup(
            {"agent": "alpha", "model": "some-model", "execution_path": "codex-cli"}))
        await spawning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.gather(*dp._ROLLBACK_TASKS)
        store.decrement_active_sessions.assert_called_once_with("sub-S")
    assert len(released) == 1
    assert closed == []  # never bound: the seat, not the session, is what goes back
    assert conn.errors == []


@pytest.mark.asyncio
async def test_prewarm_cancelled_inside_the_build_still_returns_the_seat(monkeypatch):
    # The seat is taken inside the build's thread; a cancel while awaiting
    # it must wait for the config and give the seat back.
    conn = _controller()
    from core import concurrency
    from services.engines import subscription_pool as sp
    from ws import dashboard_prewarm as dp
    from ws import dashboard_warmup as dw
    _stub_seams(monkeypatch, built_cfg=_cfg())
    building = asyncio.Event()
    finish = asyncio.Event()

    async def _build(**kwargs):
        building.set()
        await finish.wait()
        return _cfg()

    monkeypatch.setattr(dw, "build_agent_config", _build)
    monkeypatch.setattr(concurrency, "release_chat_slot", lambda sid: None)
    with patch.object(sp, "subscription_store") as store:
        task = asyncio.create_task(conn._handle_pre_warmup(
            {"agent": "alpha", "model": "some-model", "execution_path": "codex-cli"}))
        await building.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.decrement_active_sessions.assert_not_called()  # the build is still running
        finish.set()
        await asyncio.gather(*dp._ROLLBACK_TASKS)
        store.decrement_active_sessions.assert_called_once_with("sub-S")


@pytest.mark.asyncio
async def test_no_room_for_two_skips_before_the_build(monkeypatch):
    """A pre-warm asks the ledger before building (room for it and one
    more session, nobody waiting, the person below their cap) and skips
    without a word, without a seat, when the answer is no."""
    from core import concurrency
    conn = _controller()
    calls = _stub_seams(monkeypatch, built_cfg=_cfg())
    asked = []
    monkeypatch.setattr(concurrency, "prewarm_allowed",
                        lambda path=None, **kw: asked.append((path, kw)) or False)
    await conn._handle_pre_warmup({"agent": "alpha", "model": "some-model",
                                   "execution_path": "codex-cli"})
    assert asked == [("codex-cli", {"user_sub": "user-pw"})]
    assert calls == []
    assert conn.errors == [] and conn.sent == []
    assert conn._pre_warmed_sid is None


@pytest.mark.asyncio
async def test_a_speculative_refusal_returns_the_seat_quietly(monkeypatch):
    """A pre-warm's admit is speculative: refused for lack of room, it gives
    the seat back and shows nothing (nothing was asked for yet)."""
    from core import concurrency
    from services.engines import subscription_pool as sp
    from ws import dashboard_warmup as dw
    conn = _controller()
    _stub_seams(monkeypatch, built_cfg=_cfg())
    monkeypatch.setattr(dw, "_resolve_session_interactive", lambda cfg, *a: False)
    monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: True)
    seen = {}

    async def _acquire(sid, **kw):
        seen.update(kw)
        return concurrency.Admission(False, "speculative", None)
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _acquire)
    with patch.object(sp, "subscription_store") as store:
        await conn._handle_pre_warmup({"agent": "alpha", "model": "some-model",
                                       "execution_path": "codex-cli"})
    assert seen["speculative"] is True and seen["user_sub"] == "user-pw"
    store.decrement_active_sessions.assert_called_once_with("sub-S")
    assert conn.errors == [] and conn._pre_warmed_sid is None


@pytest.mark.asyncio
async def test_detached_prewarm_asks_the_ledger_before_building(monkeypatch):
    from core import concurrency
    from ws import dashboard_prewarm as dp
    calls = []

    async def _build(**kwargs):
        calls.append(kwargs)
        return _cfg()
    monkeypatch.setattr(dp, "build_agent_config", _build)

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(dp, "run_db", _run_db)
    monkeypatch.setattr(dp, "acting_role_of", lambda *a, **k: "manager")
    monkeypatch.setattr(dp, "resolve_execution_path", lambda agent, req: "codex-cli")
    monkeypatch.setattr(dp, "_model_foreign_to_engine", lambda m, p: False)
    monkeypatch.setattr(concurrency, "prewarm_allowed", lambda *a, **kw: False)
    sid = await dp.spawn_detached_prewarm(
        agent="alpha", user={"username": "pw"}, user_sub="user-pw")
    assert sid is None and calls == []


@pytest.mark.asyncio
async def test_a_refusal_below_the_editor_tier_is_left_to_the_send(monkeypatch, caplog):
    """A viewer or contributor opening a Shared-only chat: the pre-warm's
    build refuses, quietly (no "Pre-warmup failed" frame, no traceback); the
    first send's warmup card is the one place the refusal shows."""
    from core.sandbox.session_config_dir import AgentStateRefused
    from ws import dashboard_warmup as dw
    conn = _controller()
    _stub_seams(monkeypatch, built_cfg=_cfg())

    async def _refused(**kwargs):
        raise AgentStateRefused("This agent is set to Shared only (would run as contributor)")
    monkeypatch.setattr(dw, "build_agent_config", _refused)
    with caplog.at_level("INFO"):
        await conn._handle_pre_warmup({"agent": "alpha", "model": "some-model",
                                       "execution_path": "codex-cli"})
    assert conn.errors == [] and conn.sent == []
    assert conn._pre_warmed_sid is None
    assert not any(r.levelname == "ERROR" for r in caplog.records)
