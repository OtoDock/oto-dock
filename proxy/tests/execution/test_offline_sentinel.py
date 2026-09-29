"""The offline sentinel's reach (core-seams phase 7, D14 / D16).

``resolve_execution_target`` answers ``__offline__:<machine_id>`` when the
intended machine is unreachable and no fallback is allowed; a builder keeps
the sentinel on ``AgentConfig.execution_target`` while
``remote_store.placement_of`` answers the LOCAL placement for it. That is
harmless only because every consumer refuses BEFORE a session starts — these
are the first unit pins of that order (the affinity test holds the column's
persistence only; the dashboard warmup and the task runner carry the same
guard, exercised on T1 by the offline card).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import placement
from storage import remote_store


def test_the_resolver_mints_the_sentinel_and_the_builder_places_it_local(monkeypatch):
    from storage.agents import agent_store
    from storage import database as _db
    monkeypatch.setattr(agent_store, "get_agent",
                        lambda slug: {"slug": slug, "execution_path": "claude-code-cli",
                                      "execution_target": "m-dead"})
    monkeypatch.setattr(_db, "get_platform_setting", lambda key: "0")  # no fallback either way
    with patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_store.resolve_execution_target("pa", None, "manager")
    assert placement.is_offline_sentinel(target) and placement.offline_machine_of(target) == "m-dead"
    assert reason == "agent-default-offline-hard-fail"
    assert remote_store.placement_of(target, None, "pa") is placement.LOCAL_PLACEMENT
    # the layer registry refuses the sentinel outright
    from core.session import session_manager
    with pytest.raises(RuntimeError, match="offline"):
        session_manager.get_execution_layer("pa", execution_path="claude-code-cli",
                                            execution_target=target)


@pytest.mark.asyncio
async def test_the_headless_heal_refuses_before_any_registration(monkeypatch):
    """``resume_dead_session_headless``: a pinned machine that is offline
    raises the tailored error right after the build — before the slot, the
    layer and ``set_session_security``."""
    from ws import headless_resume as hr
    from core.session import session_state

    cfg = SimpleNamespace(execution_target=placement.offline_sentinel("m-dead"),
                          execution_path="claude-code-cli", interactive=False,
                          security_context=SimpleNamespace(placement=placement.LOCAL_PLACEMENT),
                          subscription_id="")
    registered: list = []
    layers: list = []

    async def _build(**kw):
        assert kw["pinned_target"] == "m-dead"
        return cfg

    class _Layer:
        async def can_resume_session(self, *a, **k):
            return True

        async def prepare_resume(self, *a, **k):
            return None

    monkeypatch.setattr(hr.task_store, "get_chat", lambda cid: {
        "id": cid, "agent": "pa", "user_sub": "u", "session_id": "dead", "execution_target": "m-dead",
        "execution_path": "claude-code-cli", "permission_mode": "auto", "model": "", "execution_mode": "",
    })
    monkeypatch.setattr(hr.task_store, "get_user", lambda sub: {"sub": sub, "username": "alice", "role": "admin"})
    monkeypatch.setattr(hr, "_resume_username_for_chat", lambda *a, **k: "alice")
    monkeypatch.setattr(hr, "acting_role_of", lambda *a, **k: "admin")
    monkeypatch.setattr(hr, "build_agent_config", _build)
    monkeypatch.setattr(hr, "release_config_seat", lambda *a, **k: None)
    monkeypatch.setattr(hr, "get_execution_layer", lambda *a, **k: layers.append(a) or _Layer())
    monkeypatch.setattr(session_state, "set_session_security", lambda *a, **k: registered.append(a))
    with pytest.raises(RuntimeError, match="session_machine_offline"):
        await hr.resume_dead_session_headless("c-1", "dead", _Layer(), user_sub="u")
    assert registered == [] and layers == []
