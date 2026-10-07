"""Turn endings in the CLI stream translator: a safety-classifier decline and
a usage limit become one typed ending, built from the event shapes Claude
Code 2.1.281 recorded on 2026-09-30.

A decline is ``message_delta.delta.stop_reason == "refusal"`` with
``stop_details``; Claude Code then goes on by itself ("continuing once with
that noted"), so the ending is what lets the turn's owner end the process. A
limit is a synthetic assistant message (model ``<synthetic>``,
``error: "rate_limit"``) followed by an error result.
"""

import pytest

from core.events import turn_ending
from core.events.common_events import DONE, ERROR, TEXT
from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator

_REFUSAL = {"stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber",
                             "explanation": "This request triggered restrictions."}}
_LIMIT_TEXT = ("You've reached your Fable limit. Switch to another model, or manage "
               "usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, to continue.")


def _delta_event(delta: dict, **extra) -> dict:
    return {"type": "stream_event", "event": {"type": "message_delta",
                                              "delta": delta, **extra}}


def _synthetic_limit(text: str = _LIMIT_TEXT) -> dict:
    return {"type": "assistant", "error": "rate_limit",
            "apiError": "model_requires_usage_credits",
            "message": {"model": "<synthetic>", "role": "assistant",
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": text}]}}


def _error_result(text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": True, "result": text}


def _endings(chunks) -> list[turn_ending.TurnEnding]:
    return [turn_ending.from_dict(c.event_data) for c in chunks if c.event_type == "turn_ending"]


def test_a_refusal_is_a_declined_ending():
    tr = ClaudeCLIEventTranslator("sess-1")
    chunks = tr.feed(_delta_event(_REFUSAL))
    [ending] = _endings(chunks)
    assert ending.reason == turn_ending.DECLINED and ending.detail == "cyber"
    assert chunks[-1].is_error and chunks[-1].text == ending.line()
    assert "declined" in ending.line() and "(cyber)" in ending.line()


def test_a_refusal_without_details_still_ends_the_turn():
    tr = ClaudeCLIEventTranslator("sess-1")
    [ending] = _endings(tr.feed(_delta_event({"stop_reason": "refusal"})))
    assert ending.reason == turn_ending.DECLINED and ending.detail == ""


def test_a_refusal_mid_tool_call_closes_the_open_tool_first():
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash"}}})
    chunks = tr.feed(_delta_event(_REFUSAL))
    assert [c.event_type for c in chunks] == ["tool_end", "turn_ending"]


def test_the_ending_rides_one_error_event_without_a_text_twin():
    tr = ClaudeCLIEventTranslator("sess-1")
    events = [e for c in tr.feed(_delta_event(_REFUSAL)) for e in cli_chunk_to_events(c)]
    assert [e.type for e in events] == [ERROR]
    assert turn_ending.from_dict(events[0].data["ending"]).reason == turn_ending.DECLINED
    assert events[0].data["message"] == turn_ending.from_dict(events[0].data["ending"]).line()


def test_the_error_result_after_a_refusal_is_not_repeated():
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed(_delta_event(_REFUSAL))
    chunks = tr.feed(_error_result("Request declined"))
    assert not any(c.is_error for c in chunks)


def test_the_synthetic_limit_message_and_its_result_are_a_limit_ending():
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 1.02, "resetsAt": 1790000000}}}})
    assert tr.feed(_synthetic_limit()) == []
    chunks = tr.feed(_error_result(_LIMIT_TEXT))
    [ending] = _endings(chunks)
    assert ending.reason == turn_ending.LIMIT
    assert ending.resets_at == turn_ending.epoch_to_iso(1790000000)
    assert ending.detail == _LIMIT_TEXT
    assert ending.window == "five_hour"
    events = [e for c in chunks for e in cli_chunk_to_events(c)]
    assert TEXT not in [e.type for e in events] and ERROR in [e.type for e in events]


@pytest.mark.parametrize("limit_type, window", [
    ("five_hour", "five_hour"),
    ("seven_day", "seven_day"),
    ("seven_day_opus", "scoped:opus"),
    ("seven_day_fable", "scoped:fable"),
    ("seven_day_oauth_apps", ""),
    ("", ""),
])
def test_the_limit_ending_names_the_reached_window(limit_type, window):
    # The pool rests the account by it: the session and the weekly window are
    # account-wide, a model family's weekly window rests that family only, and
    # a type the platform does not know names no window.
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": limit_type, "resetsAt": 1790000000,
        "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1789990000}}}})
    tr.feed(_synthetic_limit())
    [ending] = _endings(tr.feed(_error_result(_LIMIT_TEXT)))
    assert ending.window == window
    assert ending.resets_at == turn_ending.epoch_to_iso(1790000000)


def test_a_limit_result_alone_is_recognised_by_its_text():
    tr = ClaudeCLIEventTranslator("sess-1")
    text = "You've hit your session limit · resets 3pm (UTC)"
    [ending] = _endings(tr.feed(_error_result(text)))
    assert ending.reason == turn_ending.LIMIT and ending.detail == text
    assert ending.resets_at == "" and ending.window == ""


def test_any_other_error_result_is_the_error_ending():
    # The engine's own error result: the process answered and stays, the
    # ending names the reason and carries the engine's words.
    tr = ClaudeCLIEventTranslator("sess-1")
    chunks = tr.feed(_error_result("Not logged in · Please run /login"))
    [ending] = _endings(chunks)
    assert ending.reason == turn_ending.ERROR
    assert ending.detail == "Not logged in · Please run /login"
    assert ending.reason not in turn_ending.KILLS_PROCESS
    assert [c.is_error for c in chunks if c.text] == [True]


def test_normal_message_delta_stays_silent():
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed(_delta_event({"stop_reason": "end_turn"})) == []
    assert tr.feed(_delta_event({})) == []


def test_the_limit_matcher_knows_both_engines_notices():
    for text in (_LIMIT_TEXT, "You've hit your session limit · resets 3pm (UTC)",
                 "You've hit your usage limit. Upgrade to Pro or try again at 4:55 PM.",
                 "Claude AI usage limit reached|1790000000",
                 # The usage-credits wording of the same notice (first met live 2026-10-04).
                 "You're out of usage credits. Switch to another model, or manage usage "
                 "credits at claude.ai/settings/usage, to continue."):
        assert turn_ending.is_limit_text(text), text
    for text in ("Not logged in", "Prompt is too long", "rate limit exceeded, retrying"):
        assert not turn_ending.is_limit_text(text), text


def test_the_ending_lines():
    declined = turn_ending.TurnEnding(reason=turn_ending.DECLINED, detail="cyber")
    assert declined.line().startswith("⚠ The model's safety classifier declined this turn (cyber)")
    limit = turn_ending.TurnEnding(reason=turn_ending.LIMIT,
                                   resets_at="2026-10-01T15:00:00+00:00", detail=_LIMIT_TEXT)
    assert limit.line() == f"⚠ Usage limit reached, resets 2026-10-01 15:00 UTC. {_LIMIT_TEXT}"
    assert turn_ending.TurnEnding(reason=turn_ending.LIMIT).line() == (
        "⚠ Usage limit reached. Continue after the reset, or on another model.")
    assert turn_ending.from_dict(limit.as_dict()) == limit
    assert turn_ending.from_dict({"reason": "other"}) is None
    scoped = turn_ending.TurnEnding(reason=turn_ending.LIMIT,
                                    resets_at="2026-10-01T15:00:00+00:00", window="scoped:fable")
    assert scoped.as_dict()["window"] == "scoped:fable"
    assert turn_ending.from_dict(scoped.as_dict()) == scoped
    # An ending stored or sent before the window was carried reads as none.
    assert turn_ending.from_dict({"reason": "limit", "resets_at": ""}).window == ""
    assert scoped.line() == ("⚠ Usage limit reached, resets 2026-10-01 15:00 UTC. "
                             "Continue after the reset, or on another model.")


def test_done_is_not_part_of_the_ending_chunk():
    tr = ClaudeCLIEventTranslator("sess-1")
    events = [e for c in tr.feed(_delta_event(_REFUSAL)) for e in cli_chunk_to_events(c)]
    assert DONE not in [e.type for e in events]


@pytest.mark.asyncio
async def test_the_local_turn_ends_its_process_before_the_ending_leaves(monkeypatch):
    """A declined turn's process is interrupted (the pool entry stays, so the
    next start resumes) before the consumer sees the ending's ERROR; nothing
    the CLI prints after the refusal is read."""
    from core.layers.cli import layer as cli_layer
    order: list[str] = []
    tr = ClaudeCLIEventTranslator("sess-1")

    class FakeSession:
        async def send_message(self, message, **_kw):
            for raw in ({"type": "stream_event", "event": {"type": "content_block_delta",
                         "index": 0, "delta": {"type": "text_delta", "text": "On it."}}},
                        _delta_event(_REFUSAL),
                        {"type": "system", "subtype": "informational",
                         "content": "safeguards stopped the response above"}):
                for chunk in tr.feed(raw):
                    yield chunk
            order.append("read past the refusal")

    async def get_session(sid):
        return FakeSession()

    async def interrupt(sid):
        order.append(f"interrupt {sid}")
        return True

    monkeypatch.setattr(cli_layer, "get_persistent_session", get_session)
    monkeypatch.setattr(cli_layer, "interrupt_persistent_session", interrupt)
    seen = []
    async for event in cli_layer.CLIExecutionLayer()._send_turn("sess-1", "hello"):
        if event.type == ERROR:
            order.append("error")
        seen.append(event.type)
    assert order == ["interrupt sess-1", "error"]
    assert seen[-2:] == [ERROR, DONE]


@pytest.mark.asyncio
async def test_the_pump_rests_the_subscription_by_the_limit_ending(temp_db, monkeypatch):
    """The pump hands a turn's typed ending and its message to the pool's one
    entry point, which rests the account by the window the ending names."""
    import asyncio

    from core.events import stream_pump
    from core.events.common_events import CommonEvent
    from services.engines import subscription_pool

    rests: list[tuple] = []
    monkeypatch.setattr(subscription_pool, "rest_after_limit",
                        lambda sid, ending, msg: rests.append((sid, ending, msg)))
    temp_db.create_chat("lim1", "user-admin", "a1")
    ending = turn_ending.TurnEnding(reason=turn_ending.LIMIT,
                                    resets_at="2099-01-01T00:00:00+00:00",
                                    detail=_LIMIT_TEXT, window="seven_day")
    eq: asyncio.Queue = asyncio.Queue()
    await eq.put(CommonEvent(type=ERROR, data={"message": ending.line(),
                                               "ending": ending.as_dict()}))
    pump = stream_pump.ChatStreamPump(
        chat_id="lim1", session_id="sess-lim1",
        producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
        event_queue=eq, perm_queue=None,
    )
    task = pump.start()
    await asyncio.wait_for(task, 10)
    assert [(sid, e.reason, e.window, e.resets_at) for sid, e, _ in rests] == [
        ("sess-lim1", turn_ending.LIMIT, "seven_day", "2099-01-01T00:00:00+00:00")]
    assert rests[0][2] == ending.line()
