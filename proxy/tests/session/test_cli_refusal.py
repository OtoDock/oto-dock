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


_BRIEF_THROTTLE = ("API Error: Server is temporarily limiting requests (not your usage limit) \u00b7 "
                   "this may be a temporary capacity issue.")


def _api_error_frame(text: str) -> dict:
    return {"type": "assistant", "error": "rate_limit", "is_api_error_message": True, "api_error_status": 429,
            "message": {"model": "<synthetic>", "role": "assistant", "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": text}]}}


def test_a_brief_throttle_frame_is_no_limit_of_the_account():
    """The stream twin of the terminal row's. Claude Code says ``error: "rate_limit"`` for the vendor's brief
    throttle too. The transcript's row marks it ``apiErrorIsTransient``; the stream's frame carries no such
    field, only the CLI's own sentence. The turn ends with an error, in the engine's words."""
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1790000000}}}})
    assert tr.feed(_api_error_frame(_BRIEF_THROTTLE)) == []
    [ending] = _endings(tr.feed(_error_result(_BRIEF_THROTTLE)))
    assert ending.reason == turn_ending.ERROR and ending.detail == _BRIEF_THROTTLE
    assert ending.resets_at == "" and ending.window == ""


def test_a_rate_limit_frame_in_other_words_stays_a_limit():
    """Only the brief throttle's own sentence is read out: an account's own rate limit (an API key's 429) says
    ``rate_limit`` with no window event and no notice, and stays the limit the terminal's row is."""
    text = "API Error: Request rejected (429) \u00b7 This request would exceed your organization's rate limit"
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.feed(_api_error_frame(text))
    [ending] = _endings(tr.feed(_error_result(text)))
    assert ending.reason == turn_ending.LIMIT and ending.detail == text


def test_a_terminal_row_is_the_brief_throttle_by_its_mark_or_by_its_sentence():
    """The terminal's row and the stream's frame read the same signs. The row's mark alone says it, and so does
    the CLI's own sentence alone: a row that kept the sentence and lost the mark is still no limit of the
    account. A ``rate_limit`` row with neither stays the limit it is."""
    from core.layers.cli.translator import api_error_ending

    def row(text: str, **marks) -> dict:
        return {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", **marks,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    other = "API Error: Request rejected (429) \u00b7 This request would exceed your organization's rate limit"
    by_sentence = api_error_ending(row(_BRIEF_THROTTLE))
    assert by_sentence.reason == turn_ending.ERROR and by_sentence.detail == _BRIEF_THROTTLE
    by_mark = api_error_ending(row(other, apiErrorIsTransient=True))
    assert by_mark.reason == turn_ending.ERROR and by_mark.detail == other
    assert api_error_ending(row(other)).reason == turn_ending.LIMIT


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


def test_the_marker_of_an_interrupt_the_session_asked_for_is_no_error_ending():
    # A graceful interrupt (stop-and-send) landing before the turn's first
    # output closes it with an "[ede_diagnostic]" error result: the turn was
    # interrupted as asked, so its queued message drains next. The same
    # marker with no interrupt asked for stays the error ending.
    marker = "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null"
    tr = ClaudeCLIEventTranslator("sess-1")
    tr.interrupted = True
    assert _endings(tr.feed(_error_result(marker))) == []
    tr = ClaudeCLIEventTranslator("sess-1")
    [ending] = _endings(tr.feed(_error_result(marker)))
    assert ending.reason == turn_ending.ERROR


def test_normal_message_delta_stays_silent():
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed(_delta_event({"stop_reason": "end_turn"})) == []
    assert tr.feed(_delta_event({})) == []


def test_the_limit_matcher_knows_both_engines_notices():
    for text in (_LIMIT_TEXT, "You've hit your session limit · resets 3pm (UTC)",
                 # 2.1.312 names the 5-hour window "5-hour limit".
                 "You've hit your 5-hour limit · resets 3pm (UTC)",
                 "You've hit your usage limit. Upgrade to Pro or try again at 4:55 PM.",
                 "Claude AI usage limit reached|1790000000",
                 # The usage-credits wording of the same notice (first met live 2026-10-04),
                 # its 2.1.296 form and the org's wording on a Team or Enterprise login.
                 "You're out of usage credits. Switch to another model, or manage usage "
                 "credits at claude.ai/settings/usage, to continue.",
                 "You're out of usage credits. Run /usage-credits to keep using Fable 5.1 "
                 "or /model to switch models.",
                 "Your org is out of usage · add funds to continue",
                 "Your org is out of usage · contact your admin",
                 # 2.1.325 words Fable's weekly limit as a window with its reset, with
                 # and without the usage-credits tail.
                 "You've hit your Fable limit · resets 3pm (UTC). /model to switch models.",
                 "You've hit your Fable limit · resets Nov 16, 9am (UTC). Switch to another "
                 "model, or manage usage credits at claude.ai/settings/usage, to continue.",
                 "You've hit your weekly limit · resets Nov 16, 9am (UTC)",
                 # Codex writes its notice with a typographic apostrophe (both pins).
                 "You\u2019ve hit your usage limit. Upgrade to Pro or try again at 4:55 PM."):
        assert turn_ending.is_limit_text(text), text
    for text in ("Not logged in", "Prompt is too long", "rate limit exceeded, retrying",
                 # A plan fact, not a limit: resting the account would not help.
                 "Your seat type doesn't include usage credits"):
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
async def test_the_pump_rests_the_subscription_by_the_limit_ending(temp_db, monkeypatch):
    """The pump hands a turn's typed ending and its message to the pool's one
    entry point, which rests the account by the window the ending names."""
    import asyncio

    from core.events import stream_pump
    from core.events.common_events import CommonEvent
    from services.engines import pool_limits

    rests: list[tuple] = []
    monkeypatch.setattr(pool_limits, "rest_after_limit",
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


@pytest.mark.parametrize("bound, on_the_loop", [(("sub-1", ""), True), ((None, None), False)],
                         ids=["the-pool-knows-it", "only-the-store-knows-it"])
@pytest.mark.asyncio
async def test_a_failed_turn_rests_its_account_at_once_when_the_pool_knows_it(
        temp_db, monkeypatch, bound, on_the_loop):
    """A rest that landed later would miss a fast retry's selection, or the
    session's release. Only a session the store alone knows reads off the loop,
    and nothing in the turn's end ever waits for it."""
    import asyncio
    import threading

    from core import background
    from core.events import stream_pump
    from core.events.common_events import CommonEvent
    from services.engines import pool_limits, subscription_pool

    threads: list[bool] = []
    main = threading.current_thread()
    monkeypatch.setattr(subscription_pool, "bound_attribution", lambda sid: bound)
    monkeypatch.setattr(pool_limits, "bound_attribution", lambda sid: bound)
    monkeypatch.setattr(pool_limits, "rest_after_limit",
                        lambda sid, ending, msg: threads.append(threading.current_thread() is main))
    temp_db.create_chat("lim2", "user-admin", "a1")
    eq: asyncio.Queue = asyncio.Queue()
    await eq.put(CommonEvent(type=ERROR, data={"message": "overloaded"}))
    pump = stream_pump.ChatStreamPump(
        chat_id="lim2", session_id="sess-lim2",
        producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
        event_queue=eq, perm_queue=None,
    )
    await asyncio.wait_for(pump.start(), 10)
    for task in [t for t in background.running("stream_pump") if t.get_loop() is asyncio.get_running_loop()]:
        await task
    assert threads == [on_the_loop]


@pytest.mark.parametrize("subtype", ["checkpoint_done", "inherited_asks_settled",
                                     "turn_began_before_init", "alive_before_init",
                                     "rewind_stand_in"])
def test_the_new_system_frames_of_2_1_312_are_quiet(subtype):
    """Frames of cloud sessions, the runner and the rewind record: known, and
    never a system row in a chat."""
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed({"type": "system", "subtype": subtype, "uuid": "u-1"}) == []


_HOOK = {"type": "system", "hook_id": "h-1", "hook_name": "SessionStart:startup",
         "hook_event": "SessionStart", "uuid": "u-1", "session_id": "cli-1"}


@pytest.mark.parametrize("frame", [
    {**_HOOK, "subtype": "hook_started"},
    {**_HOOK, "subtype": "hook_progress", "stdout": "part", "stderr": "", "output": "part"},
    {**_HOOK, "subtype": "hook_response", "output": "the start's data", "stdout": "the start's data",
     "stderr": "", "exit_code": 0, "outcome": "success"},
    {**_HOOK, "hook_name": "SessionStart:compact", "subtype": "hook_response", "output": "data",
     "stdout": "data", "stderr": "", "exit_code": 0, "outcome": "success"},
])
def test_the_engines_own_hook_frames_are_quiet(frame):
    """Claude Code prints a start and a response frame for every ``SessionStart``
    command (ten per session, again at a compaction), and a response holds the
    hook's whole output: never a system row in a chat."""
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed(frame) == []


def test_a_failed_hook_is_logged_once_without_its_output(caplog):
    tr = ClaudeCLIEventTranslator("sess-1")
    failed = {**_HOOK, "subtype": "hook_response", "output": "secret start data", "stdout": "",
              "stderr": "secret start data", "exit_code": 2, "outcome": "error"}
    with caplog.at_level("WARNING", logger="otodock"):
        assert tr.feed(failed) == []
        assert tr.feed({**failed, "hook_id": "h-2"}) == []
    lines = [r.getMessage() for r in caplog.records if "SessionStart:startup" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "exit 2" in lines[0] and "secret" not in lines[0]
    # The next session start (its init frame) may log again.
    tr.feed({"type": "system", "subtype": "init", "mcp_servers": []})
    with caplog.at_level("WARNING", logger="otodock"):
        tr.feed({**failed, "hook_id": "h-3"})
    assert len([r for r in caplog.records if "SessionStart:startup" in r.getMessage()]) == 2


def test_a_hooks_heartbeat_is_no_chat_content_and_reads_as_background():
    """2.1.323 prints one every 30 s while a hook runs long (a card that waits
    on a person): the turn's reader drops it, a reader between turns counts it
    as nobody's answer."""
    from core.layers.cli.translator import FRAME_BACKGROUND, frame_kind
    beat = {"type": "tool_progress", "tool_use_id": "hook-PreToolUse-toolu_1-heartbeat-0",
            "tool_name": "PreToolUse", "parent_tool_use_id": "toolu_1",
            "elapsed_time_seconds": 30, "heartbeat": True, "session_id": "cli-1", "uuid": "u-2"}
    tr = ClaudeCLIEventTranslator("sess-1")
    assert tr.feed(beat) == []
    assert tr.open_tools == set() and tr.active_tool is None
    assert frame_kind(beat) == FRAME_BACKGROUND


# ---------------------------------------------------------------------------
# a refused sign-in: each engine's structured sign, never its words
# ---------------------------------------------------------------------------

_LOGIN = "Please run /login · API Error: 401 Invalid authentication credentials"


def _refused_frame(**extra) -> dict:
    return {"type": "assistant", "error": "authentication_failed", "parent_tool_use_id": None,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": _LOGIN}]}, **extra}


def test_a_claude_turn_refused_for_its_sign_in_is_its_own_ending():
    """Claude Code marks the refused request's message ``authentication_failed``
    and then ends the turn with an error result."""
    tr = ClaudeCLIEventTranslator("sess-1")
    chunks = tr.feed(_refused_frame()) + tr.feed(_error_result(_LOGIN))
    (ending,) = _endings(chunks)
    assert ending.reason == turn_ending.ERROR and ending.sign_in is True
    assert ending.line() == ("⚠ The AI account's sign-in was refused, so the turn ended here. "
                             "Send the message again in a minute.")


@pytest.mark.parametrize("frame", [
    _refused_frame(parent_tool_use_id="toolu_1"),
    _refused_frame(error="oauth_org_not_allowed"),
    {"type": "assistant", "message": {"content": [{"type": "text", "text": _LOGIN}]}},
], ids=["a sub-agent's", "an organisation not allowed", "the words alone"])
def test_no_other_claude_error_is_a_refused_sign_in(frame):
    tr = ClaudeCLIEventTranslator("sess-1")
    (ending,) = _endings(tr.feed(frame) + tr.feed(_error_result(_LOGIN)))
    assert ending.reason == turn_ending.ERROR and ending.sign_in is False


@pytest.mark.parametrize("info, refused", [
    ("unauthorized", True),
    ({"http_connection_failed": {"http_status_code": 401}}, True),
    ({"response_stream_connection_failed": {"http_status_code": 401}}, True),
    ({"http_connection_failed": {"http_status_code": 500}}, False),
    ("internal_server_error", False),
    (None, False),
])
def test_a_rollouts_refused_sign_in_is_its_own_ending(info, refused):
    """Codex ends a turn ``unauthorized`` when the vendor refused the token
    and it could not renew it itself, which with the platform's blank renewal
    token is every refusal, or names the 401 a request got."""
    from core.layers.codex.translator import task_complete_ending
    ending = task_complete_ending({"error": {"message": "refused", "codex_error_info": info}})
    assert ending.reason == turn_ending.ERROR and ending.detail == "refused"
    assert ending.sign_in is refused
    assert turn_ending.from_dict(ending.as_dict()) == ending
    if refused:
        assert ending.line() == ("⚠ The AI account's sign-in was refused, so the turn ended here. "
                                 "Send the message again in a minute.")


def test_a_frames_refused_sign_in_is_its_own_ending():
    from core.layers.codex.translator import CodexEventTranslator
    tr = CodexEventTranslator()
    for info, refused in (("unauthorized", True), ({"httpConnectionFailed": {"httpStatusCode": 401}}, True),
                          ("internalServerError", False)):
        event = tr._error_event({"message": "refused", "codexErrorInfo": info}, "")
        assert turn_ending.from_dict(event.data["ending"]).sign_in is refused, info
    limit = tr._error_event({"message": "x", "codexErrorInfo": "usageLimitExceeded"}, "")
    assert turn_ending.from_dict(limit.data["ending"]).reason == turn_ending.LIMIT

