"""Regression tests for the stale-turn_ended filter in RemoteExecutionLayer.

Bug being guarded against: the satellite's per-turn ``detect_file_changes``
sweep (sha256 of every file under the agent_dir) can exceed the proxy's 2s
``_drain_until_turn_ended`` budget. When that happens the late ``turn_ended``
ends up in ``info.event_queue`` AFTER the user's next message has already
kicked off the next turn. Without a turn-id, ``_stream_cli_turn`` would read
that stale marker and yield DONE immediately, terminating the new turn with
zero events — the CLI keeps running on the satellite and tools/notifications
still fire, but nothing is persisted to ``chat_messages``.

The fix: every ``send_message`` pre-mints a ``command_id``, the satellite
echoes it in ``turn_ended``, and ``_stream_cli_turn`` / ``_stream_codex_turn``
/ ``_drain_until_turn_ended`` only honor a turn_ended whose command_id matches
the current turn — stale ones are discarded so the real turn can stream.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.events.common_events import DONE
from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.remote.remote_execution import RemoteExecutionLayer, RemoteSessionInfo


def _make_info(session_id: str = "sess-1", machine_id: str = "m-1") -> RemoteSessionInfo:
    translator = ClaudeCLIEventTranslator(session_id)
    settle = SettleController(session_id, 0, translator)
    info = RemoteSessionInfo(
        session_id=session_id,
        machine_id=machine_id,
        agent_name="agent-1",
        execution_path="claude-code-cli",
        event_queue=asyncio.Queue(),
    )
    info.cli_translator = translator
    info.cli_settle = settle
    return info


@pytest.mark.asyncio
async def test_stale_turn_ended_is_discarded_until_real_one_arrives():
    """Drop a turn_ended from a previous turn, then deliver the matching one.

    Mirrors the real-world bug: prior turn's late file-scan delivers its
    turn_ended into the queue after the new turn has started reading. The
    stream loop must skip the stale marker and keep waiting for the matching
    turn_ended for the new turn.
    """
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._cm.send_fire_and_forget = AsyncMock()
    layer._sessions = {}

    info = _make_info()
    info.current_send_command_id = "new-cmd"

    # Pre-load the queue with a stale turn_ended (from a previous turn's
    # delayed file-scan) followed by the real one for our turn.
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "old-cmd"})
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "new-cmd"})

    events = []
    async for event in layer._stream_cli_turn(info):
        events.append(event)

    # Stream should yield exactly one DONE — the stale turn_ended is dropped,
    # the matching one terminates the turn.
    assert len(events) == 1
    assert events[0].type == DONE


@pytest.mark.asyncio
async def test_matching_turn_ended_terminates_turn():
    """Sanity check: a turn_ended whose command_id matches ends the turn."""
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._sessions = {}

    info = _make_info()
    info.current_send_command_id = "cmd-A"
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "cmd-A"})

    events = []
    async for event in layer._stream_cli_turn(info):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == DONE


@pytest.mark.asyncio
async def test_turn_ended_without_command_id_still_honored():
    """An older satellite (or any path that omits command_id) must still
    terminate the turn — the filter only rejects an EXPLICIT mismatch, never
    a missing id, so we don't break the read loop if the field is absent.
    """
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._sessions = {}

    info = _make_info()
    info.current_send_command_id = "cmd-A"
    info.event_queue.put_nowait({"type": "_turn_ended"})  # no command_id

    events = []
    async for event in layer._stream_cli_turn(info):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == DONE


@pytest.mark.asyncio
async def test_drain_until_turn_ended_skips_stale_marker():
    """_drain_until_turn_ended must keep draining past a stale turn_ended
    so a late file-scan from the previous turn can't satisfy the drain budget
    for the current turn's cleanup.
    """
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._sessions = {}

    info = _make_info()
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "old"})
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "current"})

    await layer._drain_until_turn_ended(
        info, timeout=1.0, expected_command_id="current",
    )
    # Both markers consumed — nothing left in the queue.
    assert info.event_queue.empty()


@pytest.mark.asyncio
async def test_drain_until_turn_ended_times_out_on_only_stale():
    """If only a stale marker is in the queue, drain should time out (not
    short-circuit on the stale one).
    """
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._sessions = {}

    info = _make_info()
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "old"})

    await layer._drain_until_turn_ended(
        info, timeout=0.2, expected_command_id="current",
    )
    # Stale was drained; queue is empty; we returned via the timeout path.
    assert info.event_queue.empty()


@pytest.mark.asyncio
async def test_codex_turn_filters_stale_turn_ended():
    """Same filter for the Codex stream path."""
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._sessions = {}

    info = _make_info()
    info.execution_path = "codex-cli"
    info.current_send_command_id = "new-cmd"
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "old-cmd"})
    info.event_queue.put_nowait({"type": "_turn_ended", "command_id": "new-cmd"})

    events = []
    async for event in layer._stream_codex_turn(info):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == DONE


def _text_event(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _result_event(result: str = "ok", *, command_id: str = "",
                  is_error: bool = False, subtype: str = "success") -> dict:
    ev = {"type": "result", "subtype": subtype, "is_error": is_error,
          "result": result}
    if command_id:
        ev["_command_id"] = command_id
    return ev


def _mk_layer() -> RemoteExecutionLayer:
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._cm.send_fire_and_forget = AsyncMock()
    # A bare MagicMock returns truthy — the valve branch would read "in
    # grace" and re-arm forever. Default to a healthy attached stream.
    layer._cm.is_session_in_grace = MagicMock(return_value=False)
    layer._sessions = {}
    return layer


class TestForeignResultGate:
    """A `result` that cannot belong to the driven prompt (resume handshake,
    stale flush from a replaced process) must re-arm the turn instead of
    closing it — the Mode D incident ended the pump blocks=0 one second
    after send while the real answer streamed 50s later with no consumer."""

    @pytest.mark.asyncio
    async def test_empty_foreign_result_rearms_until_real_answer(self):
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        # Incident shape: a zero-content success result races in first, the
        # driven turn streams after it.
        info.event_queue.put_nowait(_result_event("No response requested."))
        info.event_queue.put_nowait(_text_event("THE REAL ANSWER"))
        info.event_queue.put_nowait(_result_event("THE REAL ANSWER"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        texts = [e for e in events if e.type not in (DONE,)
                 and e.data.get("content") == "THE REAL ANSWER"]
        assert texts, f"real answer lost: {events}"
        assert [e.type for e in events].count(DONE) == 1
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_handshake_sentinel_with_text_rearms(self):
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        # Handshake variant that streams its reply text before its result.
        info.event_queue.put_nowait(_text_event("No response requested."))
        info.event_queue.put_nowait(_result_event("No response requested."))
        info.event_queue.put_nowait(_text_event("THE REAL ANSWER"))
        info.event_queue.put_nowait(_result_event("THE REAL ANSWER"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        assert any(e.data.get("content") == "THE REAL ANSWER"
                   for e in events if e.type not in (DONE,))
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_stale_tagged_result_dropped_despite_content(self):
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        # Content already streamed, then a result TAGGED with the previous
        # turn's command arrives (dying flush) — dropped on the tag alone.
        info.event_queue.put_nowait(_text_event("streaming"))
        info.event_queue.put_nowait(
            _result_event("old turn", command_id="old-cmd"))
        info.event_queue.put_nowait(
            _result_event("real", command_id="new-cmd"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        assert [e.type for e in events].count(DONE) == 1
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_error_result_never_skipped(self):
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        info.event_queue.put_nowait(
            _result_event("boom", is_error=True, subtype="error"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        # Zero content, but an error result must still close the turn.
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_burst_beyond_cap_still_streams_real_answer(self):
        """The 2026-07-20 incident: a poisoned --resume flushes MORE
        zero-content settlement results than the old skip cap (5); the 6th
        used to close the turn empty while the real answer ran orphaned.
        The count must never end the turn — only the real result does."""
        from core.layers.cli.settle import FOREIGN_RESULT_SKIP_CAP
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        for _ in range(FOREIGN_RESULT_SKIP_CAP + 3):
            info.event_queue.put_nowait(_result_event("No response requested."))
        info.event_queue.put_nowait(_text_event("THE REAL ANSWER"))
        info.event_queue.put_nowait(_result_event("THE REAL ANSWER"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        assert any(e.data.get("content") == "THE REAL ANSWER"
                   for e in events if e.type not in (DONE,))
        assert [e.type for e in events].count(DONE) == 1
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_silence_after_skip_closes_turn(self, monkeypatch):
        # The gate resolves the valve constant from the settle module.
        from core.layers.cli import settle as settle_mod
        monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 0.3)
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        # One foreign result, then nothing — the valve must close the turn
        # instead of waiting forever.
        info.event_queue.put_nowait(_result_event("No response requested."))

        events = await asyncio.wait_for(
            _collect(layer._stream_cli_turn(info)), timeout=5.0)
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_noise_slides_but_does_not_disarm_valve(self, monkeypatch):
        """Non-content noise after a skip must not remove the valve (the old
        code disarmed on ANY event — with no cap that would hang the turn
        forever); the turn still closes a valve-width after the last event."""
        from core.layers.cli import settle as settle_mod
        monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 0.3)
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        info.event_queue.put_nowait(_result_event("No response requested."))
        for _ in range(3):
            info.event_queue.put_nowait({"type": "system",
                                         "subtype": "status"})

        events = await asyncio.wait_for(
            _collect(layer._stream_cli_turn(info)), timeout=5.0)
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_valve_rearms_while_in_reconnect_grace(self, monkeypatch):
        """Valve expiry during a WS-drop grace window must NOT close the
        turn — Mode A may still deliver it; the answer arriving after the
        grace-length silence streams normally."""
        from core.layers.cli import settle as settle_mod
        monkeypatch.setattr(settle_mod, "FOREIGN_SKIP_SILENCE_S", 0.2)
        layer = _mk_layer()
        layer._cm.is_session_in_grace = MagicMock(return_value=True)
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        info.event_queue.put_nowait(_result_event("No response requested."))

        async def _late_delivery():
            await asyncio.sleep(0.7)  # several valve-widths of silence
            info.event_queue.put_nowait(_text_event("THE REAL ANSWER"))
            info.event_queue.put_nowait(_result_event("THE REAL ANSWER"))
            info.event_queue.put_nowait({"type": "_turn_ended",
                                         "command_id": "new-cmd"})

        delivery = asyncio.create_task(_late_delivery())
        events = await asyncio.wait_for(
            _collect(layer._stream_cli_turn(info)), timeout=5.0)
        await delivery
        assert any(e.data.get("content") == "THE REAL ANSWER"
                   for e in events if e.type not in (DONE,))
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_task_mode_foreign_result_does_not_enter_settle(self):
        """Task turns (settle_after_result > 0) must not enter settle on a
        foreign result — the job-done 5s fast-grace would close the turn
        before the driven prompt's first token (incident empty turn #2)."""
        layer = _mk_layer()
        info = _make_info()
        info.cli_settle = SettleController(
            info.session_id, 30, info.cli_translator)
        info.current_send_command_id = "new-cmd"
        info.event_queue.put_nowait(_result_event("No response requested."))

        collector = asyncio.create_task(
            _collect(layer._stream_cli_turn(info)))
        await asyncio.sleep(0.3)  # let the loop consume the foreign result
        try:
            assert info.cli_settle.settling is False
        finally:
            collector.cancel()
            with pytest.raises(asyncio.CancelledError):
                await collector

    @pytest.mark.asyncio
    async def test_stale_tagged_content_does_not_unlock_close(self):
        """Content-typed events from a PRIOR command pass the stale filter by
        design, but must not count as driven-turn content — otherwise a dying
        process's junk text lets the next zero-content result close the turn."""
        layer = _mk_layer()
        info = _make_info()
        info.current_send_command_id = "new-cmd"
        info.event_queue.put_nowait(_result_event("No response requested."))
        stale_text = _text_event("junk from the old turn")
        stale_text["_command_id"] = "old-cmd"
        info.event_queue.put_nowait(stale_text)
        info.event_queue.put_nowait(_result_event(""))  # still foreign
        info.event_queue.put_nowait(_text_event("THE REAL ANSWER"))
        info.event_queue.put_nowait(_result_event("THE REAL ANSWER"))
        info.event_queue.put_nowait({"type": "_turn_ended",
                                     "command_id": "new-cmd"})

        events = [e async for e in layer._stream_cli_turn(info)]
        assert any(e.data.get("content") == "THE REAL ANSWER"
                   for e in events if e.type not in (DONE,))
        assert [e.type for e in events].count(DONE) == 1
        assert events[-1].type == DONE


async def _collect(agen):
    return [e async for e in agen]


class TestAdoptSession:
    """Mode C: re-adopt a satellite-alive turn by replaying its retained
    buffer through _stream_cli_turn."""

    @pytest.mark.asyncio
    async def test_adopt_replays_finished_turn(self):
        sent = []

        class _CM:
            def create_session_queue(self, machine_id, sid, path, *, maxsize=1000):
                self.q = asyncio.Queue(maxsize=maxsize)
                return self.q

            async def send_fire_and_forget(self, machine_id, msg):
                sent.append(msg)
                # Emulate the satellite's replay onto the queue.
                self.q.put_nowait({"type": "_resume_replay_begin",
                                   "truncated": False, "count": 2})
                self.q.put_nowait(_text_event("recovered"))
                self.q.put_nowait(_result_event("recovered",
                                                command_id="cmd-1"))
                self.q.put_nowait({"type": "_turn_ended",
                                   "command_id": "cmd-1"})

        layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
        layer._cm = _CM()
        layer._sessions = {}

        events = []
        async for e in layer.adopt_session(
            machine_id="m-1", session_id="s-1", agent_name="pa",
            command_id="cmd-1",
        ):
            events.append(e)
        assert sent and sent[0]["type"] == "resume_session_stream"
        assert any(e.data.get("content") == "recovered"
                   for e in events if e.type not in (DONE,))
        assert events[-1].type == DONE

    @pytest.mark.asyncio
    async def test_adopt_truncated_injects_marker(self):
        class _CM:
            def create_session_queue(self, machine_id, sid, path, *, maxsize=1000):
                self.q = asyncio.Queue(maxsize=maxsize)
                return self.q

            async def send_fire_and_forget(self, machine_id, msg):
                self.q.put_nowait({"type": "_resume_replay_begin",
                                   "truncated": True, "count": 1})
                self.q.put_nowait(_result_event("x", command_id="cmd-1"))
                self.q.put_nowait({"type": "_turn_ended",
                                   "command_id": "cmd-1"})

        layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
        layer._cm = _CM()
        layer._sessions = {}
        events = [e async for e in layer.adopt_session(
            machine_id="m-1", session_id="s-1", agent_name="pa",
            command_id="cmd-1")]
        assert any("truncated" in (e.data.get("content") or "")
                   for e in events if e.type not in (DONE,))


@pytest.mark.asyncio
async def test_abort_does_not_block_on_satellite_ack():
    """Abort must use fire-and-forget so the WS dispatcher can emit
    ``aborted`` immediately. Regression: send_command would wait up to 30s
    for an ack the satellite never sends (its abort handler is ack-less),
    leaving the dashboard Stop button stuck even though the agent had
    already stopped generating.
    """
    layer = RemoteExecutionLayer.__new__(RemoteExecutionLayer)
    layer._cm = MagicMock()
    layer._cm.send_command = AsyncMock(
        side_effect=AssertionError(
            "abort must NOT call send_command (satellite never acks)"
        )
    )
    layer._cm.send_fire_and_forget = AsyncMock()
    layer._sessions = {}

    info = _make_info()
    layer._sessions[info.session_id] = info

    await layer.abort(info.session_id)

    layer._cm.send_fire_and_forget.assert_called_once()
    args, _ = layer._cm.send_fire_and_forget.call_args
    assert args[1] == {"type": "abort", "session_id": info.session_id}
