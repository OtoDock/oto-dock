"""Inbound hooks (APPS.md "Inbound hooks"): the verifiers behind
``POST /v1/apps/{id}/inbound/{name}`` — a vendor's signed event checked by
the platform with the app's declared secret and the named scheme, the
vendor's event id lifted for de-duplication, and the delivery payload the
handler receives. The route (``api/apps/app_inbound.py``) does nothing
else: verify, then enqueue.

Four schemes: ``stripe`` (``Stripe-Signature: t=<unix>,v1=<hex>[,v1=…]``,
HMAC-SHA256 over ``{t}.{body}``, any ``v1`` may match, a five-minute
tolerance; the event id is the body's ``id``), ``github``
(``X-Hub-Signature-256: sha256=<hex>`` over the body; the event id is
``X-GitHub-Delivery``), ``hmac_sha256`` (a declared header, an optional
prefix, hex over the body; an optional ``id_header``) and ``bearer``
(``Authorization: Bearer <secret>`` in constant time; an optional
``id_header``). Everything runs on the raw bytes before any parsing.
"""

from __future__ import annotations

import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from storage.db_app_deliveries import PAYLOAD_MAX_BYTES, canonical_payload

# The raw body's cap, so the wrapped delivery (the body as text, the header
# subset, the ids) fits the store's 64 KB.
BODY_MAX_BYTES = 48 * 1024
STRIPE_TOLERANCE_S = 300
# The same shape the trigger route accepts for `X-OtoDock-Event-Id`.
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,128}$")
# The headers a handler receives beside the body: the content type, the
# vendor's event headers — never the signature, never the bearer.
KEEP_HEADERS = ("content-type", "user-agent")
VENDOR_HEADERS = {
    "github": ("x-github-event", "x-github-delivery", "x-github-hook-id"),
    "stripe": (),
    "hmac_sha256": (),
    "bearer": (),
}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""        # missing_header | malformed_header | timestamp_too_old | signature_mismatch
    event_id: str = ""


def _lower(headers) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _stripe(raw_body: bytes, headers: dict[str, str], secret: str) -> Verdict:
    header = headers.get("stripe-signature", "")
    if not header:
        return Verdict(False, "missing_header")
    ts = ""
    sigs: list[str] = []
    for part in header.split(","):
        k, _sep, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1" and v:
            sigs.append(v)
    if not ts or not sigs or not (ts.isascii() and ts.isdigit()) or len(ts) > 12:
        return Verdict(False, "malformed_header")
    if abs(time.time() - int(ts)) > STRIPE_TOLERANCE_S:
        return Verdict(False, "timestamp_too_old")
    if not secret:
        return Verdict(False, "signature_mismatch")
    expected = hmac.new(secret.encode("utf-8"), ts.encode("utf-8") + b"." + raw_body,
                        "sha256").hexdigest()
    if not any(_same_hex(expected, s) for s in sigs):
        return Verdict(False, "signature_mismatch")
    event_id = ""
    try:
        doc = json.loads(raw_body.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("id"), str):
            event_id = doc["id"]
    except (ValueError, UnicodeDecodeError):
        pass
    return Verdict(True, event_id=event_id)


def _same_hex(expected: str, got: str) -> bool:
    """Constant time over bytes: a header is text the sender chose, and
    ``compare_digest`` raises on a non-ASCII str."""
    return hmac.compare_digest(expected.encode("ascii"),
                               got.strip().lower().encode("utf-8", "replace"))


def _hmac_hex(raw_body: bytes, headers: dict[str, str], secret: str, header: str,
              prefix: str = "") -> Verdict:
    """HMAC-SHA256 of the raw body, hex, in ``header`` after an optional
    prefix — the bytes as they arrived, never a decoded copy."""
    got = headers.get(header, "") if header else ""
    if not got:
        return Verdict(False, "missing_header")
    if prefix and got.startswith(prefix):
        got = got[len(prefix):]
    if not secret:
        return Verdict(False, "signature_mismatch")
    expected = hmac.new(secret.encode("utf-8"), raw_body, "sha256").hexdigest()
    if not _same_hex(expected, got):
        return Verdict(False, "signature_mismatch")
    return Verdict(True)


def _bearer(headers: dict[str, str], secret: str) -> Verdict:
    auth = headers.get("authorization", "")
    parts = auth.split(" ", 1)
    token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not token:
        return Verdict(False, "missing_header")
    if not secret or not hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8")):
        return Verdict(False, "signature_mismatch")
    return Verdict(True)


def verify(spec: dict, raw_body: bytes, headers, secret: str) -> Verdict:
    """The scheme the hook declares, over the raw bytes; the vendor's event
    id in the verdict when the scheme has one."""
    h = _lower(headers)
    scheme = spec.get("verify")
    if scheme == "stripe":
        return _stripe(raw_body, h, secret)
    if scheme == "github":
        v = _hmac_hex(raw_body, h, secret, "x-hub-signature-256", "sha256=")
        v.event_id = h.get("x-github-delivery", "") if v.ok else ""
        return v
    if scheme == "hmac_sha256":
        v = _hmac_hex(raw_body, h, secret, str(spec.get("header") or "").lower(),
                      str(spec.get("prefix") or ""))
    elif scheme == "bearer":
        v = _bearer(h, secret)
    else:
        return Verdict(False, "unsupported_scheme")
    if v.ok and spec.get("id_header"):
        v.event_id = h.get(str(spec["id_header"]).lower(), "")
    return v


def event_id_for(name: str, vendor_id: str) -> str:
    """The store's idempotency key: the vendor's id scoped by the hook name
    (the unique index is per handler); empty — no de-duplication — when the
    vendor gave none or an odd one."""
    vendor_id = (vendor_id or "").strip()
    if not vendor_id or not EVENT_ID_RE.match(vendor_id):
        return ""
    key = f"{name}:{vendor_id}"
    return key if EVENT_ID_RE.match(key) else ""


def payload_for(name: str, spec: dict, raw_body: bytes, headers, event_id: str) -> dict:
    """What the handler receives: the hook, the scheme, the body as text,
    the header subset, the vendor's event id and when it arrived."""
    h = _lower(headers)
    keep = {k: h[k] for k in (*KEEP_HEADERS, *VENDOR_HEADERS.get(spec.get("verify"), ())) if k in h}
    return {"hook": name, "verify": spec.get("verify"), "body": raw_body.decode("utf-8", "replace"),
            "headers": keep, "event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat()}


def wrapped_size_ok(payload: dict) -> bool:
    """The delivery as the store would write it fits its cap (a body of
    quotes and backslashes grows when escaped)."""
    return len(canonical_payload(payload).encode("utf-8")) <= PAYLOAD_MAX_BYTES
