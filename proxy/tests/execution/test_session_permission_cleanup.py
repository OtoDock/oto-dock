"""Permission-state lifecycle of the sessions the proxy holds itself (Direct
LLM's).

The mode + security context are registered BEFORE the session's tools start
(so the session JWT minted into their env can carry the external claim),
dropped again when the start fails, and dropped when the session closes — a
session token must not find a live context once its session is gone.

A session on a machine: ``tests/session/test_session_liveness.py``
(registered before the payload, dropped on a failure),
``tests/remote/test_chat_reload.py`` (the remote layer's close takes the
context) and ``tests/remote/test_machine_sessions.py`` (a terminal's close
forgets it).
"""

from __future__ import annotations

import uuid

import pytest

from auth.path_policy import SecurityContext
from core.execution_layer import AgentConfig
from core.session import session_state


def _ctx() -> SecurityContext:
    return SecurityContext(role="viewer", username="", agent="support",
                           is_admin_agent=False, principal="external",
                           external_claim="phone:+3021")


def _config() -> AgentConfig:
    return AgentConfig(agent_name="support", permission_mode="auto",
                       security_context=_ctx(), sandbox_host_claude_dir="/tmp/x")


def _layer():
    from core.layers.direct.layer import DirectLLMExecutionLayer
    return DirectLLMExecutionLayer()


@pytest.mark.asyncio
async def test_registered_before_the_start_and_kept_on_success(monkeypatch):
    layer = _layer()
    sid = str(uuid.uuid4())
    seen = {}

    async def _impl(session_id, config):
        seen["ctx"] = session_state.get_session_security(session_id)
        seen["mode"] = session_state.get_session_mode(session_id)

    monkeypatch.setattr(layer, "_start_session_impl", _impl)
    try:
        await layer.start_session(sid, _config())
        assert seen["ctx"] is not None and seen["ctx"].external_claim == "phone:+3021"
        assert seen["mode"] == "auto"
        assert session_state.get_session_security(sid) is not None
    finally:
        session_state.cleanup_session_permission_state(sid)


@pytest.mark.asyncio
async def test_a_failed_start_drops_the_registration(monkeypatch):
    layer = _layer()
    sid = str(uuid.uuid4())

    async def _boom(session_id, config):
        raise RuntimeError("start failed")

    monkeypatch.setattr(layer, "_start_session_impl", _boom)
    with pytest.raises(RuntimeError):
        await layer.start_session(sid, _config())
    assert session_state.get_session_security(sid) is None
    assert sid not in session_state._session_modes


@pytest.mark.asyncio
async def test_close_drops_the_registration():
    layer = _layer()
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "auto", _ctx())
    assert session_state.get_session_security(sid) is not None
    # No session exists for this id — every close step is a no-op except the
    # permission-state cleanup, which is what this pins.
    await layer.close_session(sid)
    assert session_state.get_session_security(sid) is None
    assert sid not in session_state._session_modes


def test_session_is_live_reads_the_layer_registries():
    from core.layers.direct.session import _direct_sessions
    sid = str(uuid.uuid4())
    assert not session_state.session_is_live(sid)
    assert not session_state.session_is_live("")
    _direct_sessions[sid] = object()
    try:
        assert session_state.session_is_live(sid)
    finally:
        _direct_sessions.pop(sid, None)
