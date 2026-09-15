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
    monkeypatch.setattr(dw, "_effective_agent_role", lambda *a, **k: "manager")
    monkeypatch.setattr(dw.config, "get_cli_model", lambda name, **k: "some-model")
    monkeypatch.setattr(dw.config, "get_model_layers", lambda m: list(model_layers or []))
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
    from ws import dashboard_warmup as dw
    monkeypatch.setattr(dw.config, "get_model_layers", lambda m: [])
    assert dw._model_foreign_to_engine("my-local-model", "codex-cli") is False
    monkeypatch.setattr(dw.config, "get_model_layers", lambda m: ["claude-code-cli"])
    assert dw._model_foreign_to_engine("claude-sonnet-5", "codex-cli") is True
    assert dw._model_foreign_to_engine("claude-sonnet-5", "claude-code-cli") is False
    assert dw._model_foreign_to_engine("", "codex-cli") is False


@pytest.mark.asyncio
async def test_detached_prewarm_skips_return_the_seat(monkeypatch):
    from services.engines import subscription_pool as sp
    from ws import dashboard_warmup as dw

    async def _run_db(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(dw, "run_db", _run_db)
    monkeypatch.setattr(dw, "_effective_agent_role", lambda *a, **k: "manager")
    monkeypatch.setattr(dw, "resolve_execution_path", lambda agent, p: "codex-cli")
    monkeypatch.setattr(dw.config, "get_model_layers", lambda m: [])

    async def _build(**kwargs):
        return _cfg(execution_target="mach-1")

    monkeypatch.setattr(dw, "build_agent_config", _build)
    with patch.object(sp, "subscription_store") as store:
        sid = await dw.spawn_detached_prewarm(
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
        await asyncio.gather(*dw._ROLLBACK_TASKS)
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
        await asyncio.gather(*dw._ROLLBACK_TASKS)
        store.decrement_active_sessions.assert_called_once_with("sub-S")
