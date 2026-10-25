"""Transfer handles: how one live call's caller is moved to a colleague.

A blind transfer: the agent names a target the admin configured on the
route, the daemon moves the caller and leaves the call. Asterisk: AMI
``Redirect`` of the caller's channel to ``<number>@from-internal`` (the
context the Originate uses too; a FreePBX extension and an outbound number
both route from there) on the server's live event connection. Twilio: a
live-call TwiML update that dials the target, sent once the farewell's
end-of-stream mark has echoed so the last words are not clipped; the
``<Dial>`` presents the route's own Twilio number (a carrier refuses the
caller's number, which Twilio would present by default) and names an action
URL, so the daemon learns how the Dial ended and decides what the caller
hears next (``pending_dials``, ``dial_action_twiml``).

The handle is built where the call is accepted (``main._handle_connection``
for AudioSocket, ``calls/twilio_http.py`` for Twilio) and asked at the tool's
ack whether a transfer is possible at all; the pipeline executes it at the
completion point (``pipeline/call_control.py``).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger("transfer")

#: The dialplan context a redirected caller enters (FreePBX: extensions and
#: outbound routes).
REDIRECT_CONTEXT = "from-internal"
_CALL_SID_RE = re.compile(r"CA[0-9a-f]{32}")
_EOS_WAIT_S = 3.0
#: Twilio's call ceiling: a dial still unanswered past it is forgotten.
PENDING_DIAL_TTL_S = 4 * 3600
#: Twilio's ``DialCallStatus`` words that mean the colleague took the call.
_DIAL_REACHED = frozenset({"completed", "answered"})
#: The outcome recorded when the caller hung up while the Dial ran: the
#: action still fires, with the parent call ``completed``, and the TwiML it
#: answers is never heard.
CALLER_GONE = "caller_gone"

#: The ``<Say>`` of the goodbye after a ``<Dial>`` nobody took, per base
#: language: Twilio's locale and one of its Google voices for it. A
#: ``language`` alone keeps the account's default voice, which read a Greek
#: goodbye letter by letter; the voice names are the vendor's reference.
_SAY_VOICES: dict[str, tuple[str, str]] = {
    "en": ("en-US", "Google.en-US-Standard-C"),
    "el": ("el-GR", "Google.el-GR-Standard-B"),
    "de": ("de-DE", "Google.de-DE-Standard-G"),
    "es": ("es-ES", "Google.es-ES-Standard-A"),
    "fr": ("fr-FR", "Google.fr-FR-Standard-F"),
    "it": ("it-IT", "Google.it-IT-Standard-A"),
}


def say_voice(language: str) -> tuple[str, str] | None:
    """The ``(language, voice)`` attributes of a ``<Say>`` for a base
    language code, None for a language the table does not name (the
    account's default voice then reads it)."""
    return _SAY_VOICES.get((language or "").split("-", 1)[0].lower())


class TransferError(Exception):
    """The PBX or the vendor refused the move."""


def dialable(number: str) -> str:
    """The digits an Asterisk ``Exten`` takes: a leading ``+`` goes (the
    route's targets are extensions or numbers the outbound routes
    understand)."""
    return (number or "").strip().lstrip("+")


_NUMBER_SHAPE = re.compile(r"\+?\d{2,20}")


def loggable_label(label: str) -> str:
    """A transfer target as the log may name it: its label, or a fixed word
    when the admin left none and the label is the number itself (a phone
    number is personal data; the call record carries it, the log never)."""
    text = (label or "").strip()
    if not text or _NUMBER_SHAPE.fullmatch(text):
        return "an unlabelled target"
    return text


def e164(number: str) -> str:
    """The number as a vendor dials it: the ``+`` stays (Twilio reads a
    plain number as national), and the ``00`` international prefix becomes
    one (a number saved as a phone shows it; Twilio would dial the zeros)."""
    text = (number or "").strip()
    if text.startswith("00"):
        return "+" + text[2:]
    return text


@dataclass
class PendingDial:
    """A Twilio transfer whose ``<Dial>`` has not reported its end."""

    call_sid: str
    report_uuid: str          # the call log row's call_uuid
    label: str
    language: str
    unanswered_text: str
    unreachable_text: str
    outcome: str = ""         # Twilio's DialCallStatus, or CALLER_GONE
    at: float = field(default_factory=time.monotonic)

    @property
    def failed(self) -> bool:
        """The Dial ended and the colleague never had the call."""
        return bool(self.outcome) and self.outcome != CALLER_GONE and self.outcome not in _DIAL_REACHED


class PendingDials:
    """The dials waiting for their action callback, by the parent CallSid
    (the action names it) and by the call log row's uuid (the report reads
    it: the CallSid on an inbound call, the audio uuid on an outbound one,
    whose record never learns the CallSid). Entries past the TTL go on every
    access."""

    def __init__(self, ttl_s: float = PENDING_DIAL_TTL_S):
        self._ttl = ttl_s
        self._by_sid: dict[str, PendingDial] = {}
        self._by_report: dict[str, PendingDial] = {}

    def _sweep(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for table in (self._by_sid, self._by_report):
            for key in [k for k, e in table.items() if e.at < cutoff]:
                del table[key]

    def add(self, entry: PendingDial) -> None:
        self._sweep()
        self._by_sid[entry.call_sid] = entry
        if entry.report_uuid:
            self._by_report[entry.report_uuid] = entry

    def by_call_sid(self, call_sid: str) -> PendingDial | None:
        self._sweep()
        return self._by_sid.get(call_sid or "")

    def by_report_uuid(self, report_uuid: str) -> PendingDial | None:
        self._sweep()
        return self._by_report.get(report_uuid or "")

    def clear(self) -> None:
        """Tests only."""
        self._by_sid.clear()
        self._by_report.clear()


pending_dials = PendingDials()


def dial_outcome(report_uuid: str) -> str:
    """``transfer_failed`` when the Dial of the call's transfer has already
    ended without the colleague (busy, unanswered, refused), else ``""``: the
    call log report reads it at the call's end, and an answer that lands
    after the report corrects the row through the proxy."""
    entry = pending_dials.by_report_uuid(report_uuid)
    return "transfer_failed" if entry is not None and entry.failed else ""


def _say(text: str, language: str) -> str:
    if not text:
        return ""
    voice = say_voice(language)
    attrs = f' language="{voice[0]}" voice="{voice[1]}"' if voice else ""
    return f"<Say{attrs}>{escape(text)}</Say>"


def dial_action_twiml(entry: PendingDial, dial_status: str, caller_gone: bool) -> str:
    """What the caller hears once the Dial ended: nothing when the colleague
    had the call or the caller is gone (a Hangup ends what is left), the
    unanswered goodbye after a ring nobody took, the unreachable goodbye for
    a busy or refused line. The agent left with the transfer, so every text
    is a goodbye."""
    if caller_gone or dial_status in _DIAL_REACHED:
        return "<Response><Hangup/></Response>"
    text = entry.unanswered_text if dial_status == "no-answer" else entry.unreachable_text
    return f"<Response>{_say(text, entry.language)}<Hangup/></Response>"


class AmiTransfer:
    """Redirect of a registered AudioSocket call's channel."""

    def __init__(self, manager, server_key: str, uniqueid: str, channel: str):
        self._manager = manager
        self._server_key = server_key
        self._uniqueid = uniqueid
        self._channel = channel

    def unavailable_reason(self) -> str:
        if not self._channel:
            return "the PBX did not name this call's channel"
        client = self._manager.action_client(self._server_key)
        if client is None or not client.connected:
            return "the PBX connection is down"
        return ""

    async def execute(self, number: str, *, unanswered_text: str = "",
                      unreachable_text: str = "", language: str = "", label: str = "") -> None:
        reason = self.unavailable_reason()
        if reason:
            raise TransferError(reason)
        client = self._manager.action_client(self._server_key)
        response = await client.send_action({
            "Action": "Redirect",
            "Channel": self._channel,
            "Context": REDIRECT_CONTEXT,
            "Exten": dialable(number),
            "Priority": "1",
        })
        if "Success" not in str(response.get("Response", "")):
            raise TransferError(f"Redirect refused: {response.get('Message', 'unknown')}")
        logger.info("AMI Redirect to %s@%s accepted", loggable_label(label), REDIRECT_CONTEXT)


class TwilioTransfer:
    """A live-call TwiML update: ``<Dial>`` the target from the route's own
    number, with the action URL that reports the Dial's end. The agent's
    stream left with the update, so the goodbyes the action may speak are
    the call's last words: a blind transfer has no way back."""

    def __init__(self, server_cfg: dict, call_sid: str, conn, *, caller_id: str = "",
                 action_url: str = "", report_uuid: str = "", rest_client_factory=None):
        self._server_cfg = server_cfg or {}
        self._call_sid = call_sid or ""
        self._conn = conn
        self._caller_id = caller_id or ""
        self._action_url = action_url or ""
        self._report_uuid = report_uuid or ""
        self._rest_client_factory = rest_client_factory

    def unavailable_reason(self) -> str:
        if not _CALL_SID_RE.fullmatch(self._call_sid):
            return "the call has no usable call id"
        if not (self._server_cfg.get("account_sid") and self._server_cfg.get("auth_token")):
            return "the Twilio credentials are missing"
        return ""

    def _rest_client(self):
        if self._rest_client_factory is not None:
            return self._rest_client_factory(self._server_cfg)
        from telephony.twilio_rest import TwilioRestClient
        return TwilioRestClient(
            str(self._server_cfg.get("account_sid") or ""),
            str(self._server_cfg.get("auth_token") or ""),
        )

    async def execute(self, number: str, *, unanswered_text: str = "",
                      unreachable_text: str = "", language: str = "", label: str = "") -> None:
        reason = self.unavailable_reason()
        if reason:
            raise TransferError(reason)
        # The farewell's audio was handed to Twilio, not played: the mark's
        # echo says it has been.
        wait_drained = getattr(self._conn, "wait_playback_drained", None)
        if wait_drained is not None:
            await wait_drained(_EOS_WAIT_S)
        target = e164(number)
        attrs = ""
        if self._caller_id:
            attrs += f" callerId={quoteattr(self._caller_id)}"
        if self._action_url:
            attrs += f' action={quoteattr(self._action_url)} method="POST"'
        twiml = f"<Response><Dial{attrs}>{escape(target)}</Dial></Response>"
        # Registered before the update: the Dial can end, and its action
        # arrive, within the request's own round trip.
        pending_dials.add(PendingDial(
            call_sid=self._call_sid, report_uuid=self._report_uuid or self._call_sid,
            label=label or target, language=language,
            unanswered_text=unanswered_text, unreachable_text=unreachable_text,
        ))
        await self._rest_client().update_call(self._call_sid, twiml)
        logger.info(
            "Twilio call %s redirected to a <Dial> to "
            "%s%s",
            self._call_sid[-8:], loggable_label(label),
            ' from the route' if self._caller_id else ' (no callerId)')
