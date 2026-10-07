"""Permission-state lifecycle across the three local layers.

The mode + security context are registered BEFORE the spawn (so the session
JWT minted into the process env can carry the external claim), dropped again
when the spawn fails, and dropped when the session closes — a session token
must not find a live context once its session is gone.
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


LAYERS = ["cli", "codex", "direct"]


def _layer(kind):
    if kind == "cli":
        from core.layers.cli.layer import CLIExecutionLayer
        return CLIExecutionLayer()
    if kind == "codex":
        from core.layers.codex.layer import CodexCLIExecutionLayer
        return CodexCLIExecutionLayer()
    from core.layers.direct.layer import DirectLLMExecutionLayer
    return DirectLLMExecutionLayer()


@pytest.mark.parametrize("kind", LAYERS)
@pytest.mark.asyncio
async def test_registered_before_spawn_and_kept_on_success(kind, monkeypatch):
    layer = _layer(kind)
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


@pytest.mark.parametrize("kind", LAYERS)
@pytest.mark.asyncio
async def test_failed_spawn_drops_the_registration(kind, monkeypatch):
    layer = _layer(kind)
    sid = str(uuid.uuid4())

    async def _boom(session_id, config):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(layer, "_start_session_impl", _boom)
    with pytest.raises(RuntimeError):
        await layer.start_session(sid, _config())
    assert session_state.get_session_security(sid) is None
    assert sid not in session_state._session_modes


@pytest.mark.parametrize("kind", LAYERS)
@pytest.mark.asyncio
async def test_close_drops_the_registration(kind):
    layer = _layer(kind)
    sid = str(uuid.uuid4())
    session_state.register_session_state(sid, "auto", _ctx())
    assert session_state.get_session_security(sid) is not None
    # No process exists for this id — every close step is a no-op except the
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
