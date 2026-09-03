"""Tool-boundary frames on the phone/duplex WS + spoken-mode exit semantics.

Live-hit 2026-08-14: the stream pump emits ``tool_start``/``tool_end`` ws
events, but the phone translator matched only ``tool_use``/``tool_result`` —
every tool_start was silently dropped, so the phone pipeline never got the
early pre-tool TTS flush and callers sat through silent tool runs (10 s of
dead air on the inbound home-assistant call) before hearing the pre-tool
sentence. Same session: exiting phone mode acted like Stop — teardown
aborted the in-flight turn; the operator wants it to keep producing into
the chat with a queued mode-exit note instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from ws.phone import _pump_item_to_phone_ws
from ws import duplex_attach


def _item(etype: str, **data) -> dict:
    return {"pump_type": "ws_event", "event": {"type": etype, **data}}


class TestToolFrameTranslation:
    def test_pump_tool_start_translates(self):
        # The name the pump ACTUALLY emits (stream_pump TOOL_USE handler).
        msg = _pump_item_to_phone_ws(
            _item("tool_start", name="ha_get_state", tool_id="t1"), 3)
        assert msg == {"type": "tool_start", "turn": 3,
                       "data": {"name": "ha_get_state", "tool_use_id": "t1"}}

    def test_pump_tool_end_translates(self):
        msg = _pump_item_to_phone_ws(_item("tool_end", tool_id="t1"), 3)
        assert msg is not None and msg["type"] == "tool_end"

    def test_legacy_names_still_translate(self):
        assert _pump_item_to_phone_ws(
            _item("tool_use", name="x", tool_id="t"), 1)["type"] == "tool_start"
        assert _pump_item_to_phone_ws(
            _item("tool_result", tool_id="t"), 1)["type"] == "tool_end"

    def test_text_and_skips_unchanged(self):
        assert _pump_item_to_phone_ws(
            _item("text", content="hi"), 2)["data"]["content"] == "hi"
        assert _pump_item_to_phone_ws(_item("thinking", content="…"), 2) is None


class TestTeardownResume:
    def test_inflight_turn_left_running_with_note(self):
        st = SimpleNamespace(pump=SimpleNamespace(system_queue=[]))
        duplex_attach._states["dx-test"] = st
        try:
            assert duplex_attach.on_teardown_resume("dx-test") is True
            assert len(st.pump.system_queue) == 1
            assert "exited spoken phone mode" in st.pump.system_queue[0]
        finally:
            duplex_attach._states.pop("dx-test", None)

    def test_no_state_or_no_pump_is_a_noop(self):
        assert duplex_attach.on_teardown_resume("dx-missing") is False
        duplex_attach._states["dx-idle"] = SimpleNamespace(pump=None)
        try:
            assert duplex_attach.on_teardown_resume("dx-idle") is False
        finally:
            duplex_attach._states.pop("dx-idle", None)
