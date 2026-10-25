"""Log hygiene: DTMF digits, PINs and dialed numbers never reach the log.

Source-level check (the test_grep_no_voice_strings convention): the files
that touch keypad digits or PIN values must not format them into any string
— one stray ``{digit!r}`` puts PIN material into the rotating log file.
"""

import ast
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


# The same values as lazy logging arguments: ``logger.info("%s", pin)`` leaks
# what ``f"{pin}"`` would. Every positional argument after the message and
# every value of an ``extra=`` mapping counts: one that opens with a banned
# expression, as the f-string bans read, and one holding a banned name
# anywhere (``self.state.pin``, ``str(digit)``, ``"".join(digits)``).
_LOG_CALLS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LEADING = (
    "digit", "new", "entered", "candidate", "expected", "ch", "self.route.pin",
    "route.pin", "self._channel", "dialable(", "number", "entry.label",
    "target.get('number')",
)
_NAMES_ANYWHERE = ("digit", "entered", "candidate", "expected", "number", "dialable")
_EXACT_ANYWHERE = {"pin", "_channel"}


def _log_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_CALLS):
            yield node


def _lazy_log_args(call):
    first = 1 if call.func.attr == "log" else 0
    yield from call.args[first + 1:]
    for kw in call.keywords:
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            yield from kw.value.values


def _banned_in(expr):
    text = ast.unparse(expr)
    if text == "pin" or text.startswith(_LEADING):
        return text
    for n in ast.walk(expr):
        ident = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
        if ident and (ident in _EXACT_ANYWHERE or ident.lstrip("_").startswith(_NAMES_ANYWHERE)):
            return ident
        if isinstance(n, ast.Constant) and n.value == "number":
            return "'number'"
    return None


def test_no_digit_or_pin_in_lazy_log_arguments():
    for rel in FILES:
        tree = ast.parse((_PHONE / rel).read_text(encoding="utf-8"))
        for call in _log_calls(tree):
            for arg in _lazy_log_args(call):
                hit = _banned_in(arg)
                assert hit is None, f"{rel}:{arg.lineno} logs {ast.unparse(arg)!r} ({hit})"


def test_twilio_dtmf_lazy_log_carries_no_value():
    tree = ast.parse((_PHONE / "telephony/twilio_media.py").read_text(encoding="utf-8"))
    for call in _log_calls(tree):
        msg = call.args[0] if call.args else None
        if isinstance(msg, ast.Constant) and isinstance(msg.value, str) and "DTMF" in msg.value:
            # A placeholder right of "DTMF" would carry the value.
            assert "%" not in msg.value.split("DTMF", 1)[1], msg.value


def test_the_lazy_argument_guard_catches_the_forms_it_names():
    for src in ('logger.info("x %s", pin)', 'logger.info("x %s", self.state.pin)',
                'logger.warning("x %s", str(digit))', 'logger.info("x %s", "".join(digits))',
                'logger.info("x %s", channel)', 'logger.info("x", extra={"n": entered})',
                'logger.log(20, "x %s", target.get("number"))'):
        call = next(_log_calls(ast.parse(src)))
        assert any(_banned_in(a) for a in _lazy_log_args(call)), src
    call = next(_log_calls(ast.parse('logger.info("x %s %s", peer_addr, len(channel))')))
    assert not any(_banned_in(a) for a in _lazy_log_args(call))


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
