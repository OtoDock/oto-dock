"""Inbound PIN gate — DTMF access control before the agent engages.

Runs inside ``CallPipeline.run()`` right after the TTS connects and BEFORE
the turn classifier, STT pre-connect, LLM backend, and session warmup: a
call that fails the PIN never opens an STT socket, never opens the per-call
proxy WS, and never spins up an agent session — while staying inside
``call_max_duration_s`` and the ``_active_calls`` capacity accounting (both
wrap ``pipeline.run()``).

Digits arrive from three sources: ``FRAME_DTMF`` transport frames (Twilio
maps its native ``dtmf`` events), the ``poll_dtmf`` side-channel (AudioSocket
calls bound to an AMI event listener — signalled RFC2833/SIP-INFO digits the
audio never carries), and the in-band Goertzel detector fed with the gate's
audio frames (transports without digit events). AMI + detector run together
on AudioSocket with cross-source dedupe (``_pin_accept``). The prompt plays
CONCURRENTLY with the frame-read loop — Twilio's inbound queue is 100 frames
drop-oldest, so a sequential prompt would starve the queue and lose
type-ahead digits.

Security posture (see dev ROADMAP + phone SECURITY doc): constant-time
compare over fixed-width padding; wrong attempts feed the per-number
cooldown and per-route circuit breaker in ``calls.pin_failures``; digits and
PINs are never logged and never stored — only attempt counts and outcomes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time

from calls import pin_failures
from telephony.dtmf_detect import DtmfDetector
from transport.base import FRAME_AUDIO, FRAME_DTMF, FRAME_HANGUP, TransportError

logger = logging.getLogger("pin_gate")

#: Fixed compare width — ``hmac.compare_digest`` is constant-time only for
#: equal-length inputs, and 8 > the 6-digit maximum.
_PAD_WIDTH = 8
_PAD_CHAR = "x"

#: Longest a caller can go mid-entry with no digit before we evaluate what
#: they typed is measured from prompt end / last digit (config knob); the
#: read loop polls at this granularity.
_READ_POLL_S = 0.1


class PinGateMixin:
    """Mixed into CallPipeline; only ``_pin_gate`` is called from run()."""

    async def _pin_gate(self) -> bool:
        """Speak prompts, collect digits, verify. True → proceed with the
        normal call flow; False → the call was refused (goodbye spoken,
        hangup sent) and run() must return. Caller hangup mid-gate raises
        ``TransportError`` into run()'s existing handler."""
        store = pin_failures.store
        caller = self._caller_info.get("phone", "")
        peer = self.conn.peer_addr

        if store.route_locked(self.route.id) or store.number_locked(caller):
            logger.warning(f"[{peer}] PIN gate: cooldown active, refusing call")
            self.state.call_outcome = "pin_cooldown"
            await self._pin_say("pin_lockout")
            await self._pin_hangup()
            return False

        try:
            ok = await asyncio.wait_for(
                self._pin_attempt_loop(caller),
                timeout=self.cfg.pin_entry_timeout_s,
            )
        except asyncio.TimeoutError:
            # A silent redial must not be a free probe — the timeout counts
            # one failure toward the cooldown windows.
            store.record_failure(caller, self.route.id)
            logger.info(
                f"[{peer}] PIN gate: entry timed out "
                f"({self._pin_counts_note()})"
            )
            self.state.call_outcome = "pin_timeout"
            await self._pin_say("pin_fail_goodbye")
            await self._pin_hangup()
            return False

        if ok:
            logger.info(
                f"[{peer}] PIN gate: passed "
                f"(attempt {self.state.pin_attempts}; "
                f"{self._pin_counts_note()})"
            )
            return True

        logger.warning(
            f"[{peer}] PIN gate: failed after "
            f"{self.state.pin_attempts} attempts "
            f"({self._pin_counts_note()})"
        )
        self.state.call_outcome = "pin_failed"
        await self._pin_say("pin_fail_goodbye")
        await self._pin_hangup()
        return False

    # -- attempt loop ---------------------------------------------------------

    async def _pin_attempt_loop(self, caller: str) -> bool:
        store = pin_failures.store
        detector = (
            None if getattr(self.conn, "has_dtmf_events", False)
            else DtmfDetector(self._rate_in)
        )
        # Cross-source dedupe state + per-source counts span the whole gate
        # (a late AMI duplicate of a held key must not leak into the NEXT
        # attempt and terminate it with a partial buffer).
        self._pin_last_accept: tuple[str, str, float] | None = None
        self._pin_src_counts: dict[str, int] = {}
        max_attempts = self.cfg.pin_max_attempts
        for attempt in range(1, max_attempts + 1):
            self.state.pin_attempts = attempt
            entered = await self._pin_collect(
                "pin_prompt" if attempt == 1 else "pin_retry", detector)
            if self._pin_matches(entered):
                store.clear_number(caller)
                return True
            store.record_failure(caller, self.route.id)
            logger.info(
                f"[{self.conn.peer_addr}] PIN gate: wrong code "
                f"(attempt {attempt}/{max_attempts})"
            )
        return False

    async def _pin_collect(
        self, phrase_key: str, detector: DtmfDetector | None,
    ) -> str:
        """Play one prompt while collecting digits; return the entry.

        Entry terminates on ``#``, on the 6-digit maximum (evaluation never
        keys off the real PIN's length), or on the inter-digit timeout —
        which arms only once the prompt finished or the first digit landed,
        so a caller listening to the whole prompt isn't timed out by it.
        ``*`` clears the buffer (IVR convention). Returns "" for a silent
        attempt.
        """
        if detector is not None:
            detector.reset()
        digits: list[str] = []
        poll = getattr(self.conn, "poll_dtmf", None)
        prompt_task = asyncio.create_task(self._pin_say(phrase_key))
        # The silence window arms at prompt end (or first digit) and re-arms
        # on every digit — a caller listening to the whole prompt is never
        # timed out by it.
        armed_at: float | None = None

        def consume(accepted: str) -> bool:
            """Apply one accepted char; True ends the entry ('#')."""
            nonlocal armed_at
            if accepted == "#":
                return True
            if accepted == "*":
                digits.clear()
            elif accepted.isdigit():
                digits.append(accepted)
            armed_at = time.monotonic()
            return False

        try:
            while True:
                if len(digits) >= 6:
                    break
                # Drain AMI side-channel digits BEFORE the interdigit-timeout
                # check — a just-arrived digit must not be discarded at the
                # window boundary.
                entry_done = False
                while poll is not None:
                    item = poll()
                    if item is None:
                        break
                    injected, duration_ms = item
                    if not self._pin_accept(injected, "ami", duration_ms):
                        continue
                    if consume(injected):
                        entry_done = True
                        break
                if entry_done:
                    return "".join(digits)
                now = time.monotonic()
                if armed_at is None:
                    if prompt_task.done():
                        armed_at = now
                elif now - armed_at > self.cfg.pin_interdigit_timeout_s:
                    break
                try:
                    kind, payload = await asyncio.wait_for(
                        self.conn.read_frame(), timeout=_READ_POLL_S)
                except asyncio.TimeoutError:
                    continue
                if kind == FRAME_HANGUP:
                    self.state.call_outcome = "hangup"
                    raise TransportError("hangup during PIN entry")
                new = ""
                source = "frame"
                if kind == FRAME_DTMF:
                    new = payload.decode(errors="ignore")
                elif kind == FRAME_AUDIO and detector is not None:
                    new = detector.feed(payload)
                    source = "inband"
                for ch in new:
                    if not self._pin_accept(ch, source, 0):
                        continue
                    if consume(ch):
                        return "".join(digits)
        finally:
            prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prompt_task
        return "".join(digits)

    def _pin_accept(self, digit: str, source: str, duration_ms: int) -> bool:
        """Cross-source dedupe: one key press can surface both in-band
        (Goertzel, fires ~40 ms into the tone) and as an AMI ``DTMFEnd``
        (fires at tone END, so a held key trails by its full duration).

        An AMI digit identical to the last in-band-accepted digit is a
        duplicate within ``DurationMs + 300 ms``; the reverse direction
        (detector trailing a signalled digit's audio residue) within a plain
        300 ms. Same-source repeats are never deduped — real double-presses
        arrive via one source consistently.
        """
        now = time.monotonic()
        last = self._pin_last_accept
        if last is not None:
            last_source, last_digit, last_t = last
            if last_digit == digit and last_source != source:
                if source == "ami" and last_source == "inband":
                    window = min(duration_ms, 5000) / 1000.0 + 0.3
                    if now - last_t <= window:
                        return False
                elif source == "inband" and last_source == "ami":
                    if now - last_t <= 0.3:
                        return False
        self._pin_last_accept = (source, digit, now)
        self._pin_src_counts[source] = (
            self._pin_src_counts.get(source, 0) + 1)
        return True

    # -- helpers --------------------------------------------------------------

    def _pin_counts_note(self) -> str:
        """Count-only per-source digit tallies for the gate-outcome logs —
        the observability hook for silent AMI-permission failures (an
        AMI-registered call whose keypad entry shows ami=0 means the manager
        user lacks the dtmf read class or the trunk delivers inband only)."""
        counts = getattr(self, "_pin_src_counts", None) or {}
        return (
            f"digits ami={counts.get('ami', 0)} "
            f"inband={counts.get('inband', 0)} "
            f"frame={counts.get('frame', 0)}"
        )

    def _pin_matches(self, entered: str) -> bool:
        if not entered:
            return False
        expected = self.route.pin.ljust(_PAD_WIDTH, _PAD_CHAR)
        candidate = entered[:_PAD_WIDTH].ljust(_PAD_WIDTH, _PAD_CHAR)
        return hmac.compare_digest(expected.encode(), candidate.encode())

    async def _pin_say(self, phrase_key: str) -> None:
        """Synthesize + play one gate phrase at real-time rate. Best-effort:
        a TTS failure must not strand the caller in silence forever — the
        collect loop's timeouts still run."""
        text = self.cfg.pin_phrase(self._locked_language, phrase_key)
        try:
            pcm = await self.tts.synthesize(
                text, language=self._locked_language,
                output_sample_rate=self._rate_out,
            )
            await self._play_filler_audio(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"[{self.conn.peer_addr}] PIN gate: prompt synthesis "
                f"failed: {e}"
            )

    async def _pin_hangup(self) -> None:
        """Polite teardown after the goodbye played (paced sender already
        real-time; Twilio's eos-mark echo in close() protects the tail)."""
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.5)
            self.conn.send_hangup()
        self.state._running = False
