"""Fable 5 safety-refusal surfacing in the CLI stream translator.

In non-interactive stream-json Claude Code does not auto-fall-back when
Fable 5's safety classifiers decline a request — the turn just ends with
``message_delta.delta.stop_reason == "refusal"``. The translator must turn
that into a visible error chunk instead of a silently empty reply.
"""

from core.layers.cli.translator import ClaudeCLIEventTranslator


def _delta_event(delta: dict, **extra) -> dict:
    return {"type": "stream_event", "event": {"type": "message_delta",
                                              "delta": delta, **extra}}


def test_refusal_emits_visible_error():
    tr = ClaudeCLIEventTranslator("sess-1")
    chunks = tr.feed(_delta_event(
        {"stop_reason": "refusal",
         "stop_details": {"type": "refusal", "category": "cyber",
                          "explanation": "Request flagged by safety classifiers."}},
    ))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.is_error is True
    assert "declined" in c.text
    assert "cyber" in c.text
    assert "Request flagged by safety classifiers." in c.text
    assert "Opus 5" in c.text


def test_refusal_without_details_still_surfaces():
    tr = ClaudeCLIEventTranslator("sess-1")
    chunks = tr.feed(_delta_event({"stop_reason": "refusal"}))
    assert len(chunks) == 1
    assert chunks[0].is_error is True
    assert "declined" in chunks[0].text


def test_normal_message_delta_stays_silent():
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed(_delta_event({"stop_reason": "end_turn"})) == []
    assert tr.feed(_delta_event({})) == []
