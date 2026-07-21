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

from services.meetings import meeting_orchestrator as MO


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
