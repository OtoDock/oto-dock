"""Boundary tests for the phone HTTP API input validation.

Legitimate payloads (what phone-mcp and the Asterisk dialplan actually send)
must pass unchanged; hostile payloads (injection, oversize, malformed) must
be rejected.
"""

import pytest

from validation import validate_outbound_call, validate_register, ValidationError


def test_outbound_accepts_legit_phone_mcp_payload():
    out = validate_outbound_call({
        "phone_number": "+302101234567",
        "task_description": "Reserve a table for 4",
        "instructions": "be polite",
        "route_id": "",
    })
    assert out["phone_number"] == "+302101234567"
    assert out["task_description"].startswith("Reserve")


def test_outbound_accepts_bare_national_number():
    out = validate_outbound_call({"phone_number": "2101234567", "task_description": "hi"})
    assert out["phone_number"] == "2101234567"


@pytest.mark.parametrize("body", [
    {"phone_number": "30\r\nAction: Command", "task_description": "x"},   # CRLF injection
    {"phone_number": "2@from-internal/&", "task_description": "x"},        # dialplan metachars
    {"phone_number": "+302101234567"},                                    # missing task
    {"phone_number": "+30210", "task_description": "A" * 5000},           # oversize task
    {"phone_number": "", "task_description": "x"},                        # empty phone
])
def test_outbound_rejects_hostile(body):
    with pytest.raises(ValidationError):
        validate_outbound_call(body)


def test_register_accepts_legit_dialplan_payload():
    out = validate_register({
        "audiosocket_uuid": "00000000-0000-0000-0000-000000000200",
        "phone": "2101234567",
        "did": "200",
        "source": "phone-freepbx",
        "dial_event": {"channel": "SIP/x", "uniqueid": "123.45"},
    })
    assert out["audiosocket_uuid"].endswith("200")
    assert out["source"] == "phone-freepbx"


@pytest.mark.parametrize("body", [
    {"audiosocket_uuid": "bad\r\nx", "phone": "1"},                       # control chars / too short
    {"audiosocket_uuid": ""},                                             # missing uuid
    {"audiosocket_uuid": "0000-0000-0000-0200", "phone": "x" * 200},     # oversize phone
    {"audiosocket_uuid": "00000000-0000-0000-0000-000000000200", "dial_event": "notadict"},
])
def test_register_rejects_hostile(body):
    with pytest.raises(ValidationError):
        validate_register(body)
