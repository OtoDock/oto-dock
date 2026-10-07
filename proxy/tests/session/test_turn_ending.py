"""The typed endings: one reason per way a turn ends other than by its own
result, each with its chat line, its run summary and its callback note, the
sets the owners read, and a payload round trip."""

import pytest

from core.events import turn_ending as te


@pytest.mark.parametrize("reason", te.REASONS)
def test_every_reason_round_trips_through_its_payload(reason):
    ending = te.TurnEnding(reason, resets_at="2026-10-01T15:00:00+00:00", detail="d",
                           window="five_hour", exit_code=137 if reason == te.EXITED else None,
                           graceful=reason == te.LOST)
    assert te.from_dict(ending.as_dict()) == ending
    assert te.from_dict({"reason": "no-such-reason"}) is None
    assert te.from_dict(None) is None


def test_the_sets_the_owners_read():
    assert te.KILLS_PROCESS == {te.DECLINED, te.LIMIT, te.SILENT}
    assert te.RESEND == {te.ERROR, te.EXITED, te.SILENT, te.LOST}
    assert te.STOPPED not in te.RESEND and te.ERROR not in te.KILLS_PROCESS


def test_a_payload_without_the_new_fields_reads_as_before():
    old = {"reason": te.LIMIT, "resets_at": "", "detail": "x", "window": ""}
    ending = te.from_dict(old)
    assert ending.exit_code is None and ending.graceful is False
    assert "exit_code" not in ending.as_dict() and "graceful" not in ending.as_dict()


@pytest.mark.parametrize("reason, words", [
    (te.ERROR, ("reported an error", "Send the message again")),
    (te.EXITED, ("exited before it answered", "exit code 137", "Send the message again")),
    (te.SILENT, ("went silent", "Send the message again")),
    (te.LOST, ("connection to the machine", "lost mid-turn", "Send the message again")),
    (te.DECLINED, ("safety classifier declined", "another model")),
    (te.LIMIT, ("Usage limit reached",)),
])
def test_every_abnormal_line_says_what_happened_and_what_to_do(reason, words):
    line = te.TurnEnding(reason, detail="boom", exit_code=137).line()
    for w in words:
        assert w in line, (reason, line)
    assert ";" not in line


def test_the_error_line_carries_the_engines_words():
    text = "API Error: Connection dropped (ECONNRESET)"
    ending = te.TurnEnding(te.ERROR, detail=text)
    assert text in ending.line() and text in ending.summary()
    assert text in ending.callback_note("chat-1")


@pytest.mark.parametrize("reason", (te.ERROR, te.EXITED, te.SILENT, te.LOST, te.STOPPED))
def test_the_summary_and_the_callback_name_the_reason_not_the_limit(reason):
    ending = te.TurnEnding(reason, detail="why")
    assert "usage limit" not in ending.summary().lower()
    note = ending.callback_note("chat-1")
    assert "usage limit" not in note.lower()
    assert 'continue_id="chat-1"' in note
    assert "model=" not in note


def test_stopped_is_the_persons_own_word():
    ending = te.TurnEnding(te.STOPPED)
    assert ending.line() == "Stopped."
    assert ending.summary() == "Stopped by the person."
