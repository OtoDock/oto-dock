"""Grep guard: DTMF digits and PINs must never be interpolated into logs.

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
    "pipeline/pin_gate.py",
    "calls/pin_failures.py",
)

# Any f-string / format interpolation of these identifiers is a leak.
BANNED = (
    "{digit", "{new", "{entered", "{candidate", "{expected",
    "{ch", "{pin}", "{pin!", "{pin:", "{self.route.pin",
    "{route.pin", "digits)", "% digit", "% pin",
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
