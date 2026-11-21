"""Meeting participant-spawn rollback (``start_meeting`` pass 2).

A meeting spawns N participant sessions in parallel; when one fails, the
siblings that DID spawn must be closed and every reservation released. The
pre-fix rollback iterated ``agent_sessions`` — populated only on FULL success,
so always empty in the except — and gathered without ``return_exceptions``,
so still-running sibling spawns could ``start_session`` AFTER the rollback had
released their reservations (2026-07-06 audit MUST-FIX 2).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.meetings import meeting_orchestrator as MO, meeting_sessions


def _meeting_row():
    return {
        "id": "m1", "status": "pending", "parent_chat_id": "chat-rollback-1",
        "parent_session_id": "parent-sid", "scope": "agent", "moderator": "",
        "participants": json.dumps(["good1", "bad", "good2"]),
    }


def _cfg(slug):
    return SimpleNamespace(
        execution_target="local", execution_path="claude-code-cli",
        user_sub="", security_context=SimpleNamespace(role="manager"),
        mcp_config_path="",
    )


@pytest.mark.asyncio
async def test_partial_spawn_failure_closes_spawned_and_releases_all():
    MO._meeting_session_layers.clear()
    layers: dict[str, MagicMock] = {}

    def get_layer(slug, **kw):
        layer = layers.get(slug)
        if layer is None:
            layer = MagicMock()
            layer.start_session = AsyncMock(
                side_effect=RuntimeError("boom") if slug == "bad" else None)
            layer.close_session = AsyncMock()
            layers[slug] = layer
        return layer

    released: list[str] = []
    notify = AsyncMock()
    with patch.object(MO.task_store, "get_meeting", return_value=_meeting_row()), \
         patch.object(MO.task_store, "get_chat", return_value={}), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "update_meeting_if", return_value=True), \
         patch.object(MO, "build_meeting_agent_config",
                      new=AsyncMock(side_effect=lambda slug, m, sid: _cfg(slug))), \
         patch.object(MO, "get_execution_layer", side_effect=get_layer), \
         patch("core.concurrency.acquire_meeting_slots",
               new=AsyncMock(return_value=True)), \
         patch("core.concurrency.release_meeting_slots",
               side_effect=released.extend), \
         patch.object(MO, "_notify_meeting_failed", new=notify):
        await MO.start_meeting("m1")

    # The meeting failed — surfaced into the chat (the helper flips the row
    # to failed and persists the meeting_failed banner)…
    notify.assert_awaited_once()
    assert "failed to start" in notify.await_args.args[1]
    # …every sibling that spawned was closed (the failing participant's close
    # is a harmless no-op on its never-started session)…
    assert layers["good1"].close_session.await_count == 1
    assert layers["good2"].close_session.await_count == 1
    # …all three reservations were released and the layer map has no leftovers
    # (pre-fix: zero closes, and slots freed while siblings still spawning).
    assert len(released) == 3
    assert not MO._meeting_session_layers


@pytest.mark.asyncio
async def test_a_participants_tools_on_a_compute_machine_reserve_on_its_box():
    """A Direct LLM participant's stored target stays ``local``; its reservation
    is named by the machine its tools run on."""
    from core import placement
    compute = placement.from_machine(placement.KIND_COMPUTE, {"id": "compute-1", "pairing_scope": "compute"})

    def cfg(slug):
        built = _cfg(slug)
        if slug == "good1":
            built.security_context = SimpleNamespace(role="manager", placement=compute)
        return built

    def get_layer(slug, **kw):
        layer = MagicMock()
        layer.start_session = AsyncMock(side_effect=RuntimeError("stop here"))
        layer.close_session = AsyncMock()
        return layer

    MO._meeting_session_layers.clear()
    acquire = AsyncMock(return_value=True)
    with patch.object(MO.task_store, "get_meeting", return_value=_meeting_row()), \
         patch.object(MO.task_store, "get_chat", return_value={}), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "update_meeting_if", return_value=True), \
         patch.object(MO, "build_meeting_agent_config", new=AsyncMock(side_effect=lambda slug, m, sid: cfg(slug))), \
         patch.object(MO, "get_execution_layer", side_effect=get_layer), \
         patch("core.concurrency.acquire_meeting_slots", new=acquire), \
         patch("core.concurrency.release_meeting_slots"), \
         patch.object(MO, "_notify_meeting_failed", new=AsyncMock()):
        await MO.start_meeting("m1")
    # A config with no compute placement (which the real builder refuses)
    # asks admission with its own word, never the platform's own box.
    assert sorted(acquire.await_args.kwargs["targets"].values()) == [
        placement.NO_COMPUTE, placement.NO_COMPUTE, "compute-1"]
    assert not MO._meeting_session_layers


@pytest.mark.asyncio
async def test_a_participant_starts_on_the_layer_its_config_names():
    """A participant whose config the builder placed on a compute machine is
    started through the layer of that placement (the builder's one decision,
    handed over whole), a participant with none through its target's."""
    from core import placement
    compute = placement.from_machine(placement.KIND_COMPUTE, {"id": "compute-1", "pairing_scope": "compute"})

    def cfg(slug):
        built = _cfg(slug)
        if slug != "bad":
            built.security_context = SimpleNamespace(role="manager", placement=compute)
        return built

    asked: dict[str, dict] = {}

    def get_layer(slug, **kw):
        asked[slug] = kw
        layer = MagicMock()
        layer.start_session = AsyncMock(side_effect=RuntimeError("stop here"))
        layer.close_session = AsyncMock()
        return layer

    MO._meeting_session_layers.clear()
    with patch.object(MO.task_store, "get_meeting", return_value=_meeting_row()), \
         patch.object(MO.task_store, "get_chat", return_value={}), \
         patch.object(MO.task_store, "update_meeting"), \
         patch.object(MO.task_store, "update_meeting_if", return_value=True), \
         patch.object(MO, "build_meeting_agent_config", new=AsyncMock(side_effect=lambda slug, m, sid: cfg(slug))), \
         patch.object(MO, "get_execution_layer", side_effect=get_layer), \
         patch("core.concurrency.acquire_meeting_slots", new=AsyncMock(return_value=True)), \
         patch("core.concurrency.release_meeting_slots"), \
         patch.object(MO, "_notify_meeting_failed", new=AsyncMock()):
        await MO.start_meeting("m1")
    assert asked["good1"]["placement_built"] is compute and asked["good2"]["placement_built"] is compute
    assert "placement_built" not in asked["bad"]
    assert all(kw["execution_target"] == "local" for kw in asked.values())


def test_a_participants_later_turns_and_its_close_reach_the_layer_that_holds_it(monkeypatch):
    """The layer a participant was started on answers first. When that
    record is gone, the layer that holds the session does (a session on the
    compute machine is never asked of the proxy's own layer), and only an id
    nobody holds falls to the agent's own."""
    started, holder, own = object(), object(), object()
    MO._meeting_session_layers.clear()
    monkeypatch.setattr(meeting_sessions, "find_layer_for_session", lambda sid: holder if sid == "s-held" else None)
    monkeypatch.setattr(meeting_sessions, "get_execution_layer", lambda slug: own)
    MO._meeting_session_layers["s-started"] = started
    try:
        assert MO._session_layer("s-started", "pa") is started
        assert MO._session_layer("s-held", "pa") is holder
        assert MO._session_layer("s-nobody", "pa") is own
    finally:
        MO._meeting_session_layers.clear()


def test_every_later_site_of_a_meeting_reads_the_one_lookup():
    """No turn, monitor or close of a participant resolves a layer of its
    own from the agent alone: the later sites read ``_session_layer`` (the
    six of a turn, a monitor and a close, and the turn start's check that
    the session is still there)."""
    import inspect
    assert MO._session_layer is meeting_sessions._session_layer
    orchestrator = inspect.getsource(MO)
    sessions = inspect.getsource(meeting_sessions).replace(inspect.getsource(meeting_sessions._session_layer), "")
    assert "or get_execution_layer(" not in orchestrator and "or get_execution_layer(" not in sessions
    # A turn (alone and in a batch), a monitor and three closes in the
    # orchestrator, and the turn start's check that the session is there.
    assert orchestrator.count("_session_layer(") == 6 and sessions.count("_session_layer(") == 1
