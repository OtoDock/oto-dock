"""Log hygiene: DTMF digits, PINs and dialed numbers never reach the log.

Source-level check (the test_grep_no_voice_strings convention): the files
that touch keypad digits or PIN values must not format them into any string
— one stray ``{digit!r}`` puts PIN material into the rotating log file.
"""

import pathlib

_PHONE = pathlib.Path(__file__).resolve().parent.parent

FILES = (
    "telephony/twilio_media.py",
    "telephony/dtmf_detect.py",
    "telephony/ami_events.py",
    "telephony/transfer.py",
    "pipeline/pin_gate.py",
    "pipeline/call_control.py",
    "calls/pin_failures.py",
    "calls/twilio_http.py",
)

# Any f-string / format interpolation of these identifiers is a leak. A
# channel name carries the dialled number, a transfer target's number is
# the colleague's, and an unlabelled target's label IS its number
# (``transfer.loggable_label`` is what a log line may name).
BANNED = (
    "{digit", "{new", "{entered", "{candidate", "{expected",
    "{ch", "{pin}", "{pin!", "{pin:", "{self.route.pin",
    "{route.pin", "digits)", "% digit", "% pin",
    "{self._channel", "{dialable(", "{number", "{entry.label",
    "{target.get('number')", "{target.get(\"number\")",
)


def test_no_digit_or_pin_interpolation_anywhere():
    for rel in FILES:
        src = (_PHONE / rel).read_text(encoding="utf-8")
        for token in BANNED:
            # ``"".join(digits)`` is the value flowing to compare_digest —
            # allow it only outside string literals by banning the
            # interpolated forms, which all start with '{' or '%'.
            if token == "digits)":
                continue
            assert token not in src, f"{rel} interpolates {token!r}"


def test_twilio_dtmf_log_carries_no_value():
    src = (_PHONE / "telephony/twilio_media.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if "logger." in line and "DTMF" in line:
            # The literal word "digit" is fine; interpolating the variable
            # (any brace right of "DTMF") is not.
            assert "{" not in line.split("DTMF", 1)[1], line


def test_call_registration_log_carries_no_number(caplog):
    """The dialed number is personal data — it lives on the call record,
    never in the log line."""
    import logging
    from calls.call_manager import CallManager
    with caplog.at_level(logging.INFO, logger="call_manager"):
        call = CallManager().register_call("+15559998888", "say hi")
    assert call.phone_number == "+15559998888"
    assert call.call_id in caplog.text
    assert "9998888" not in caplog.text
