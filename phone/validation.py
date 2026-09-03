"""Input validation for the phone server's HTTP API.

Boundary checks on the untrusted request bodies that reach ``/api/calls`` and
``/v1/calls/register``. The goal is to reject malformed or hostile input
before it reaches the AMI frame builder or the call registry — while never
rejecting the payloads the real callers (phone-mcp, the Asterisk dialplan)
actually send.

Error messages are safe to return to the caller: they name the field, never
echo the offending value.
"""

import json
import re

# Optional leading '+', then 2–20 digits. Matches phone-mcp's
# "+302101234567" and bare national numbers; rejects AMI/dialplan
# metacharacters (@ / & , ; | and CR/LF) by construction.
_PHONE_RE = re.compile(r"^\+?\d{2,20}$")

# Lenient identifier: hex/alphanumeric + dash/underscore, 8–64 chars. Covers
# a standard uuid4 (with dashes) and any driver-specific id the dialplan
# substitutes for ``audiosocket_uuid``.
_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_MAX_TASK = 4000
_MAX_INSTRUCTIONS = 8000
_MAX_SHORT = 128            # phone / did / source / route_id
_MAX_DIAL_EVENT_BYTES = 8192


class ValidationError(ValueError):
    """A request field failed validation. The message is caller-safe."""


def _no_control(s: str) -> bool:
    """True if the string contains no CR/LF/NUL (AMI/HTTP frame delimiters)."""
    return not any(c in s for c in ("\r", "\n", "\x00"))


def validate_outbound_call(body: dict) -> dict:
    """Validate a ``POST /api/calls`` body. Returns normalized fields.

    Raises ``ValidationError`` on the first failure.
    """
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")

    phone = body.get("phone_number") or ""
    task = body.get("task_description") or ""
    instructions = body.get("instructions") or ""
    route_id = body.get("route_id") or ""

    if not isinstance(phone, str) or not _PHONE_RE.match(phone):
        raise ValidationError("phone_number must be 2–20 digits with an optional leading +")
    if not isinstance(task, str) or not task.strip():
        raise ValidationError("task_description is required")
    if len(task) > _MAX_TASK:
        raise ValidationError("task_description is too long")
    if not isinstance(instructions, str) or len(instructions) > _MAX_INSTRUCTIONS:
        raise ValidationError("instructions are too long")
    if not isinstance(route_id, str) or len(route_id) > _MAX_SHORT or not _no_control(route_id):
        raise ValidationError("route_id is invalid")

    return {
        "phone_number": phone,
        "task_description": task,
        "instructions": instructions,
        "route_id": route_id,
    }


def validate_register(body: dict) -> dict:
    """Validate a ``POST /v1/calls/register`` body. Returns normalized fields.

    Raises ``ValidationError`` on the first failure.
    """
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")

    audiosocket_uuid = (body.get("audiosocket_uuid") or "").strip()
    if not audiosocket_uuid or not _UUID_RE.match(audiosocket_uuid):
        raise ValidationError("audiosocket_uuid is required and must be 8–64 alphanumeric/-/_ chars")

    phone = str(body.get("phone") or "")
    did = str(body.get("did") or "")
    source = str(body.get("source") or "phone")
    for name, val in (("phone", phone), ("did", did), ("source", source)):
        if len(val) > _MAX_SHORT or not _no_control(val):
            raise ValidationError(f"{name} is invalid")

    dial_event = body.get("dial_event")
    if dial_event is None:
        dial_event = {}
    if not isinstance(dial_event, dict):
        raise ValidationError("dial_event must be an object")
    try:
        if len(json.dumps(dial_event)) > _MAX_DIAL_EVENT_BYTES:
            raise ValidationError("dial_event is too large")
    except (TypeError, ValueError) as e:
        if isinstance(e, ValidationError):
            raise
        raise ValidationError("dial_event is not serializable")

    return {
        "audiosocket_uuid": audiosocket_uuid,
        "phone": phone,
        "did": did,
        "source": source,
        "dial_event": dial_event,
    }
