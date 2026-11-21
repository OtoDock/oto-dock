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
    assert te.RESEND == {te.ERROR, te.EXITED, te.SILENT, te.LOST, te.STOPPED}
    assert te.STOPPED not in te.KILLS_PROCESS and te.ERROR not in te.KILLS_PROCESS


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
    (te.STOPPED, ("stopped before this message ran", "Send it again")),
    (te.RESTARTED, ("cut short when the platform restarted", "Send a message to continue")),
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


@pytest.mark.parametrize("reason", (te.ERROR, te.EXITED, te.SILENT, te.LOST, te.STOPPED,
                                    te.RESTARTED))
def test_the_summary_and_the_callback_name_the_reason_not_the_limit(reason):
    ending = te.TurnEnding(reason, detail="why")
    assert "usage limit" not in ending.summary().lower()
    note = ending.callback_note("chat-1")
    assert "usage limit" not in note.lower()
    assert 'continue_id="chat-1"' in note
    assert "model=" not in note


def test_stopped_is_the_persons_own_word():
    ending = te.TurnEnding(te.STOPPED)
    assert ending.summary() == "Stopped by the person."
    assert ending.callback_note("chat-1").startswith("The worker's turn was stopped by a person.")


def test_a_stopped_message_stamps_the_flags_the_send_consumed():
    """The message that met the stopped process consumed the Stop's own
    abort flags while it built its batch: the ending stamps them again, as a
    lost machine does, so the next turn re-injects the cancelled context."""
    stamps = {"last_turn_aborted": True, "last_abort_graceful": False}
    assert te.TurnEnding(te.STOPPED).abort_stamps() == stamps
    assert te.TurnEnding(te.LOST).abort_stamps() == stamps
    assert te.TurnEnding(te.ERROR).abort_stamps() is None


def test_a_restart_cut_is_its_own_ending():
    """The closing row of a reply the restart cut: the process went with the
    platform but the engine's own record holds the turn (the next message
    repeats none of it), and the card offers no Send again (the work so far
    is in the chat)."""
    ending = te.TurnEnding(te.RESTARTED)
    assert ending.abort_stamps() == {"last_turn_aborted": True, "last_abort_graceful": True}
    assert te.RESTARTED not in te.RESEND and te.RESTARTED not in te.KILLS_PROCESS
    assert ending.summary() == "Cut short when the platform restarted."


class TestTheWordsOfACutThatDidNotContinue:
    """A cut names what cut it: the machine, not the platform, when the
    machine restarted, and a follow-up that never started says so."""

    def test_a_machine_restart_is_not_called_a_platform_restart(self):
        ending = te.TurnEnding(reason=te.RESTARTED, detail=te.MACHINE)
        for text in (ending.line(), ending.summary(), ending.callback_note("chat-1")):
            assert "machine" in text and "platform" not in text
        assert 'delegate(continue_id="chat-1")' in ending.callback_note("chat-1")

    def test_a_follow_up_that_never_started_says_so_and_how_to_send_it_again(self):
        ending = te.TurnEnding(reason=te.RESTARTED, detail=te.NOT_STARTED)
        note = ending.callback_note("chat-1")
        assert "before the worker read this follow-up" in note
        assert 'Send it again with delegate(continue_id="chat-1")' in note
        assert "so far is in its chat" not in note      # nothing of it ran
        assert ending.summary() == "Not started. The platform restarted first."

    def test_a_machine_away_too_long_has_its_own_sentence(self):
        ending = te.TurnEnding(reason=te.LOST, detail=te.AWAY)
        assert "away too long" in ending.line()
        assert ending.summary() == "The machine stayed away too long, so the turn was ended."
        assert "(away)" not in ending.summary()
        assert 'delegate(continue_id="chat-1")' in ending.callback_note("chat-1")

    def test_the_plain_restart_and_the_plain_loss_keep_their_words(self):
        assert te.TurnEnding(reason=te.RESTARTED).summary() == "Cut short when the platform restarted."
        assert te.TurnEnding(reason=te.LOST, detail="x").summary() == \
            "The machine's stream was lost mid-turn (x)."

    def test_the_details_survive_the_wire(self):
        for reason, detail in ((te.RESTARTED, te.MACHINE), (te.RESTARTED, te.NOT_STARTED),
                               (te.LOST, te.AWAY)):
            ending = te.TurnEnding(reason=reason, detail=detail)
            assert te.from_dict(ending.as_dict()) == ending


@pytest.mark.parametrize("reason", te.REASONS)
def test_only_an_engine_that_stopped_has_a_line_for_a_caller(reason):
    """A call's next turn starts a stopped engine again, so the caller is
    asked to say it again. Every other ending keeps the phone service's own
    apology: a limit, a decline and an error are not healed by repeating."""
    said = te.TurnEnding(reason, detail=te.MEMORY if reason == te.EXITED else "").spoken()
    assert said == (te.SPOKEN_STOPPED if reason in (te.EXITED, te.LOST) else "")
    assert te.SPOKEN_STOPPED == "Sorry, I had to stop for a moment. Could you say that again?"


# -- a provider outage reads as an outage -------------------------------------

_OUTAGE = "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"


@pytest.mark.parametrize("text", [
    _OUTAGE,
    "<html><title>Just a moment...</title></html>",
    "API Error: 529 Overloaded",
    "Overloaded",
    "API Error: 500 Internal Server Error",
    "API Error: 503 Service Unavailable",
    "unexpected status 502 Bad Gateway",
    'anthropic API error: {"type":"error","error":{"type":"overloaded_error"}}',
])
def test_a_provider_that_is_down_reads_as_an_outage(text):
    assert te.is_provider_down_text(text)
    ending = te.TurnEnding(te.ERROR, detail=text)
    line = ending.line()
    assert line.startswith("⚠ The AI provider did not answer this request, so the turn ended here.")
    assert ("usually an outage on the provider's side, not a problem with your sign-in or with this OtoDock "
            "install.") in line
    assert line.endswith("Send the message again in a few minutes.")
    assert " ".join(text.split())[:40] in line
    assert ";" not in line.replace(text, "")
    assert "AI provider did not answer" in ending.summary()
    note = ending.callback_note("chat-1")
    assert "AI provider did not answer" in note and 'continue_id="chat-1"' in note


@pytest.mark.parametrize("text", [
    "Not logged in · Please run /login",
    "Please run /login · API Error: 401 Invalid authentication credentials",
    "API Error: 401 {\"type\":\"authentication_error\"}",
    "OAuth token has expired",
    "API Error: Connection dropped (ECONNRESET)",
    "API Error: 400 messages.1: thinking block has an invalid signature",
    "API Error: 429 rate limit exceeded",
    "unexpected status 401 Unauthorized",
    "Your refresh token has been revoked (the token endpoint answered status 500)",
    "Not logged in · the login service answered 503 Service Unavailable",
    "Error: line 529 of config.toml is invalid",
    "request failed after 529 ms",
    "thread main panicked at src/main.rs:529:5",
    "",
])
def test_a_sign_in_fault_or_another_error_keeps_its_own_line(text):
    assert not te.is_provider_down_text(text)
    line = te.TurnEnding(te.ERROR, detail=text).line()
    assert line.startswith("⚠ The engine reported an error and the turn ended here")


def test_a_usage_limit_stays_a_limit_and_a_block_page_is_clipped():
    assert te.TurnEnding(te.LIMIT, detail="You've hit your limit").line().startswith("⚠ Usage limit reached")
    page = "Attention Required! | Cloudflare " + "x" * 5000
    line = te.TurnEnding(te.ERROR, detail=page).line()
    assert len(line) < 700 and line.endswith("Send the message again in a few minutes.")
