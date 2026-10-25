"""WebSocket STT endpoint for the chat mic icon (``/ws/audio/stt``).

Handshake: the client opens the socket WITHOUT a token in the URL
(query strings leak into logs) and sends a first JSON ``init`` frame carrying the
short-lived token from ``POST /v1/audio/stt/session``. The server validates +
one-time-consumes the token, opens the STT stream, then receives binary PCM
frames and streams back ``ready``, ``interim``, ``final``, ``replace`` (the
language switched) and ``error`` JSON frames.

Bounds: the token bakes in ``max_seconds`` — when the audio sent to the
provider's streams exceeds it the server closes 1011 (cost cap). Backpressure is via ``await`` (forwarding each
frame to the provider blocks the receive loop → TCP backpressure), so there's no
unbounded buffer to overflow. Registered by app.py as ``/ws/audio/stt``.

A weak link: the receive loop ticks every ``_TICK_S`` while nothing
arrives. The vendor's stream closes after about 10 s without audio, so a
person's link going quiet gets the provider's keepalive from
``_IDLE_KEEPALIVE_S`` on; every ``_PROGRESS_EVERY_S`` a ``progress`` frame
tells the client how much audio arrived and the seconds left before the
cap (``ready`` says so, and that the
stop's ``final`` is marked ``last``), so a client sees a link that stalls
or dies instead of talking into nothing. The stop, the cost cap, a vendor
stream that died and ``_IDLE_END_S`` without audio end the same way: the
words owed go out as one last ``final`` (the cap, the dead stream and the
idle end send their error first, so the client stops its mic at once).
"""

import asyncio
import contextlib
import json
import logging
import math
import time

import numpy as np
from audio.providers.lid.ecapa import EcapaIdentifier
from audio.providers.lid.rule import FINAL, LEAN, OPEN, SWITCH, Event, LanguageRule
from audio.providers.lid.tracker import LanguageTracker
from audio.providers.stt.streams import SWITCH_READY_S, LanguageStreams
from audio.streaming.lang import SUPPORTED_BASES, stt_language_param, supported_base
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from core import background
from services.media import audio_service, ws_audio_token
from storage.pg import run_db
from ws import wire_events as wire

logger = logging.getLogger("otodock")

# WS close codes.
_CLOSE_BAD_INIT = 4400
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_INTERNAL = 1011

# The Automatic pick with the local identifier (``audio/providers/lid``):
# the stream opens in the browser's language when the install lists it,
# else in the first listed one, the identifier reads the language from the
# voiced audio while the person dictates, a candidate stream opens on a lean
# and the shown text switches to it once its words beat the shown stream's
# (on a provider that reports no word confidence: the moment it has caught
# up), a ``replace`` frame carrying the new stream's words, as often as the
# leader changes until the decision is final. Nothing is held. A chunk counts
# as voiced from this int16 RMS (the Scribe silence gate's floor); the silence
# before the first word is trimmed from the replay to a pre-roll. On a
# provider that compares the words the stop is the dictation's take (one
# fresh reading, the call's take rule: a short dictation is read there or
# nowhere); a switch pending at the stop waits at most
# ``_STOP_SWITCH_WAIT_S`` for the words; a stop within ``SWITCH_READY_S`` of
# a switch waits ``_STOP_FINAL_WAIT_S`` for the new stream's final, and one
# while the shown stream is still catching up with its replay
# ``_STOP_CATCH_UP_S`` for it. All of it shares ``_STOP_BUDGET_S``
# before the provider's own finish (Deepgram's takes about 0.6 s): the client
# waits 10 s for the last final, more on a slow uplink (its socket's backlog,
# at most 20 s).
_VOICED_RMS = 300
_PREROLL_S = 0.3
_STOP_SWITCH_WAIT_S = 4.0
_STOP_FINAL_WAIT_S = 1.0
_STOP_CATCH_UP_S = 1.5
_STOP_BUDGET_S = 5.5

# The link: the loop's tick while nothing arrives, the keepalive once the
# client has sent no audio this long, the progress frame's period, the idle
# end, and every ending's budget before its last final (the switch's stop
# takes up to ``_STOP_BUDGET_S`` of it, the provider's held windows the
# rest; the client waits 10 s, more on a slow uplink). Deepgram sends at
# most one KeepAlive per 5 s, so on a quiet link one goes out every 6 to 7 s
# against the vendor's 10 s timeout: keep the cadences consistent when either
# changes.
_TICK_S = 1.0
_IDLE_KEEPALIVE_S = 3.0
_PROGRESS_EVERY_S = 2.0
_IDLE_END_S = 60.0
_END_BUDGET_S = 8.0

# What the person reads (the vendor's own text goes to the log only).
_SAY_NO_START = "Dictation could not start. An admin can check the speech settings in Setup, Audio."
_SAY_CAP = "Dictation reached its time limit and stopped. What was heard is in the box. Tap the mic to continue."
_SAY_STOPPED = ("Dictation stopped because the speech service stopped answering. "
                "What was heard is in the box. Tap the mic to continue.")
_SAY_KEY = ("Dictation stopped because the speech service refused its key. "
            "An admin can check it in Setup, Audio.")
_SAY_IDLE = ("Dictation stopped because no sound reached it for a minute. "
             "What was heard is in the box. Tap the mic to continue.")

# The usage rows a dictation writes: the stream, and the second listen's
# batch seconds (``relisten.py``) apart.
_USAGE_STREAM = "audio-stt-chat"
_USAGE_RELISTEN = "audio-stt-chat-relisten"


async def _no_keepalive() -> None:
    return


def _rms(chunk: bytes) -> float:
    samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype=np.int16).astype(np.float32)
    if not len(samples):
        return 0.0
    return math.sqrt(float(np.mean(samples * samples)))


class _LanguageSwitch:
    """The dictation socket's side of the language rule: the tracker fed per
    chunk, its events applied to the streams between chunks, a resolved
    switch turned into the ``replace`` frame the composer applies."""

    def __init__(self, streams: LanguageStreams, tracker: LanguageTracker, sample_rate: int):
        self.streams = streams
        self.tracker = tracker
        self._rate = sample_rate
        self._heard = False
        self._failures: dict[str, int] = {}
        self.switched_at = 0.0
        self.replaced: str | None = None
        self._final_pending = False

    def chunk(self, chunk: bytes) -> None:
        """A chunk went to the streams: the tracker hears it too, then the
        rule's events are applied."""
        voiced = _rms(chunk) >= _VOICED_RMS
        if not self._heard:
            if not voiced:
                self.streams.trim_replay(_PREROLL_S)
                return
            self._heard = True
        self.tracker.feed(chunk, voiced=voiced)
        self.poll()

    def poll(self) -> None:
        for event in self.tracker.poll():
            self._event(event)
        self._words()
        resolved = self.streams.switched()
        if resolved is not None:
            language, text = resolved
            self.switched_at = time.monotonic()
            self.replaced = text
            logger.info("dictation language switched to %s, %d chars shown again", language, len(text))
        if self._final_pending and self.streams.pending is None:
            self._final_pending = False
            self._settle(self.streams.active)
        for language in self.streams.dropped():
            self._failures[language] = self._failures.get(language, 0) + 1
            if self._failures[language] >= 2:
                self.tracker.rule.unavailable(language)
            else:
                self.tracker.rule.candidate_closed()
        rule = self.tracker.rule
        if rule.current != self.streams.active and self.streams.pending is None and rule.final is None:
            rule.revert(self.streams.active)

    def _words(self) -> None:
        """The words' outcomes, before the rule is reconciled with the
        streams: a refused switch goes back (and a decisive refusal
        finalises), a call-back switches to the default."""
        refused = self.streams.refused()
        if refused is not None:
            language, verdict = refused
            for event in self.tracker.rule.refused(language, decisive=verdict.decisive):
                self._event(event)
        called = self.streams.called_back()
        if called is not None:
            for event in self.tracker.rule.words_switch(called):
                self._event(event)

    def _event(self, event: Event) -> None:
        if event.kind == OPEN:
            if not self.streams.streams_language(event.language):
                self.tracker.rule.unavailable(event.language)
                return
            # Registered now, connected by the task: a switch in the same
            # batch finds the stream and goes pending.
            if self.streams.register(event.language):
                background.spawn(self.streams.open(event.language), name=f"stream-{event.language}", owner="ws.audio")
        elif event.kind == SWITCH:
            if not self.streams.switch(event.language):
                self.tracker.rule.revert(self.streams.active)
        elif event.kind == FINAL:
            # A switch still pending settles once its candidate answered (or
            # was abandoned): the final language is whatever is active then.
            if self.streams.pending is not None:
                self._final_pending = True
            else:
                self._settle(self.streams.active)

    def _settle(self, language: str) -> None:
        self.streams.settle()
        self.tracker.rule.final = language
        self.tracker.rule.current = language
        logger.info("dictation language final: %s (%.1f s of extra stream)", language, self.streams.extra_seconds)

    def take_replace(self) -> str | None:
        text, self.replaced = self.replaced, None
        return text

    async def stop(self) -> tuple[str | None, list[str]]:
        """The socket's stop: on a provider whose words are compared, the
        dictation's take (one fresh reading and the take rule, so a dictation
        too short to have been read gets its one reading), then the rule
        stops; a switch still pending gets its
        bounded chance (the candidate is catching up with the words just
        said), every stream finalised and one word on a side taken as
        evidence (the speech has ended); a switch that resolved hands back
        its ``replace`` for the frame before the final, and its stream's
        final is awaited (``finish`` returns only what is queued), after it
        caught up with its replay when it is still behind. A short
        dictation in another language stopped at once is exactly this
        window. Every wait takes what is left of ``_STOP_BUDGET_S``."""
        deadline = time.monotonic() + _STOP_BUDGET_S

        def left(bound: float) -> float:
            return max(0.0, min(bound, deadline - time.monotonic()))

        if self.streams.words_compared and not self.tracker.rule.done:
            for event in await self.tracker.take():
                self._event(event)
        self.tracker.freeze()
        self.poll()
        if self.streams.pending is not None:
            self.streams.settle_point()
            if self.streams.words_compared:
                with contextlib.suppress(Exception):
                    await self.streams.force_endpoint()
            await self.streams.wait_switched(left(_STOP_SWITCH_WAIT_S))
            self.poll()
        replace = self.take_replace()
        out: list[str] = []
        behind = self.streams.active_behind
        if behind or (self.switched_at and time.monotonic() - self.switched_at < SWITCH_READY_S):
            with contextlib.suppress(Exception):
                await self.streams.force_endpoint()
                if behind:
                    await self.streams.wait_answered(left(_STOP_CATCH_UP_S))
                    late = self.streams.drain_transcript()
                else:
                    late = await self.streams.wait_for_transcript(left(_STOP_FINAL_WAIT_S))
                if late:
                    out.append(late)
        return replace, out

    def close(self) -> None:
        self.tracker.close()


def _language_switch(factory, default: str, sample_rate: int,
                     allowed: list[str]) -> tuple[LanguageStreams, _LanguageSwitch] | None:
    """The streams and the switch for an Automatic pick, or None when no
    other language is listed, the identifier is off or unavailable, or the
    rate is not 8 or 16 kHz (the provider's automatic stream then). Runs off
    the loop (the settings reads, the model load)."""
    candidates = [lang for lang in allowed if lang != default]
    if not candidates:
        return None
    try:
        enabled = audio_service.language_identify_enabled()
        threads = audio_service.language_identify_threads()
    except Exception as e:  # noqa: BLE001  # a settings read that failed: the automatic stream, never a dead mic
        logger.warning("dictation: the identifier settings could not be read, the automatic stream stands in: %s", e)
        return None
    if not enabled:
        return None
    if sample_rate not in (8000, 16000):
        logger.info("dictation at %d Hz: the identifier reads 8 or 16 kHz, the automatic stream stands in", sample_rate)
        return None
    try:
        identifier = EcapaIdentifier(num_threads=threads)
    except Exception as e:  # noqa: BLE001  # onnxruntime loading the identifier raises anything its runtime does
        logger.warning("dictation: the language identifier failed to load, the automatic stream stands in: %s", e)
        return None
    streams = LanguageStreams(factory, default)
    # The stop reads a short dictation only where the words check the ear:
    # on a provider without word confidence a lone reading would switch an
    # accented "yes, send it".
    rule = LanguageRule(default, candidates, switch_at=LEAN, lean_at_take=streams.words_compared)
    tracker = LanguageTracker(identifier, rule, rate_in=sample_rate)
    return streams, _LanguageSwitch(streams, tracker, sample_rate)


class _Dictation:
    """One dictation socket after ``ready``: the receive loop, the link's
    pulse (the provider's keepalive while the client is quiet, the
    ``progress`` frame) and the endings. The receive is one task the tick
    never cancels: a quiet link gets the keepalive, the progress goes out on
    time, and held windows a provider releases go out without waiting for
    the next frame."""

    def __init__(self, websocket: WebSocket, provider, switch: _LanguageSwitch | None, *,
                 provider_name: str, max_seconds: int, sample_rate: int) -> None:
        self.ws = websocket
        self.provider = provider
        self.switch = switch
        self.provider_name = provider_name
        self.max_seconds = max_seconds
        self.rate = sample_rate
        self.total_bytes = 0
        self.fatal: str | None = None
        self.last_audio = self.last_keepalive = time.monotonic()
        self.next_progress = self.last_audio + _PROGRESS_EVERY_S

    def billed_seconds(self) -> float:
        # Every stream the socket opened counts (the replay included).
        seconds = getattr(self.provider, "billed_seconds", None)
        return float(seconds) if seconds is not None else self.total_bytes / (self.rate * 2)

    async def run(self) -> None:
        recv: asyncio.Future | None = None
        try:
            while True:
                if recv is None:
                    recv = asyncio.ensure_future(self.ws.receive())
                msg = None
                try:
                    msg = await asyncio.wait_for(asyncio.shield(recv), timeout=_TICK_S)
                    recv = None
                except TimeoutError:
                    pass
                now = time.monotonic()
                if now >= self.next_progress:
                    self.next_progress = now + _PROGRESS_EVERY_S
                    # ``left_s``: the seconds before the time limit, every
                    # stream counted (the client warns once near it).
                    await self.ws.send_json({"type": "progress", "audio_s": round(self.total_bytes / (self.rate * 2), 2),
                                             "left_s": round(max(0.0, self.max_seconds - self.billed_seconds()), 1)})
                if msg is None:
                    if not await self._quiet(now):
                        return
                    continue
                if msg.get("type") == "websocket.disconnect":
                    return
                chunk = msg.get("bytes")
                if chunk is not None:
                    if not await self._chunk(chunk, now):
                        return
                    continue
                if self._is_stop(msg.get("text")):
                    # One final frame: the client resolves on the final
                    # marked last (an older client on the first final after
                    # its stop) and closes, so a switched stream's late final
                    # and the stream's own tail travel together, and a switch
                    # resolved by the stop sends its replace first.
                    await self._end()
                    return
        finally:
            if recv is not None and not recv.done():
                recv.cancel()

    @staticmethod
    def _is_stop(raw: str | None) -> bool:
        if raw is None:
            return False
        try:
            ctrl = json.loads(raw)
        except (ValueError, TypeError):
            return False
        return isinstance(ctrl, dict) and ctrl.get("type") == "stop"

    async def _quiet(self, now: float) -> bool:
        """A tick with nothing received: the idle end, the keepalive, and
        whatever the provider released meanwhile. False when it ended."""
        if now - self.last_audio >= _IDLE_END_S:
            logger.info("dictation ended: no audio for %.0f s", now - self.last_audio)
            await self._end("idle", _SAY_IDLE)
            await self.ws.close(code=_CLOSE_INTERNAL)
            return False
        if now - self.last_audio >= _IDLE_KEEPALIVE_S and now - self.last_keepalive >= _IDLE_KEEPALIVE_S:
            self.last_keepalive = now
            try:
                await getattr(self.provider, "keepalive", _no_keepalive)()
            except Exception as e:  # noqa: BLE001  # a keepalive that failed: the provider's own death report follows
                logger.debug("dictation keepalive failed: %s", e)
        if self.switch is not None:
            self.switch.poll()
        if not await self._send_words():
            await self._end_dead()
            return False
        return True

    async def _chunk(self, chunk: bytes, now: float) -> bool:
        self.total_bytes += len(chunk)
        self.last_audio = now
        await self.provider.send_audio(chunk)
        if self.switch is not None:
            self.switch.chunk(chunk)
        if self.billed_seconds() > self.max_seconds:
            await self._end("max_seconds", _SAY_CAP)
            await self.ws.close(code=_CLOSE_INTERNAL)
            return False
        if not await self._send_words():
            await self._end_dead()
            return False
        return True

    async def _send_words(self) -> bool:
        """What the provider has for the client: a switch's ``replace``, the
        finals, the live partial. False when the provider died."""
        provider = self.provider
        # The language switched under the person: the composer shows the new
        # stream's words for this dictation instead of the old stream's (its
        # finals already went out as finals). A switch that resolved before
        # this drain makes the drained text the new stream's words after the
        # replace's snapshot: it travels inside the replace, never as a final
        # the replace would then overwrite. One that resolves inside the
        # drain hands back the old stream's text, which the next replace
        # covers.
        replaced = self.switch.take_replace() if self.switch is not None else None
        text = provider.drain_transcript()
        # Live partials → the client shows text as the user speaks and
        # commits it when a final arrives (the native-dictation feel). A
        # replace repaints the composer from the placed words, so the new
        # stream's partial, sent before the switch resolved, goes out once
        # more with it. Read before any send: a held window released during
        # a send must stay in this display until its final is drained.
        interim = getattr(provider, "pop_interim", lambda: None)()
        if not interim and replaced is not None:
            interim = getattr(provider, "display_interim", None) or getattr(provider, "latest_interim", "") or None
        if replaced is not None:
            if text:
                replaced, text = f"{replaced} {text}".strip(), None
            await self.ws.send_json({"type": "replace", "text": replaced})
        if text:
            await self.ws.send_json({"type": "final", "text": text})
        if interim:
            await self.ws.send_json({"type": "interim", "text": interim})
        # A provider that died after connecting (an auth or quota error, a
        # closed stream) would otherwise look like a mic that hears nothing.
        self.fatal = getattr(provider, "pop_fatal_error", lambda: None)()
        return not self.fatal

    async def _end(self, code: str | None = None, message: str = "") -> None:
        """Every ending: for the cap, a dead stream and the idle end the
        error first (a client stops its mic on it); then the switch's stop,
        the provider's finish within what is left of ``_END_BUDGET_S`` (its
        held windows included), the ``replace`` a switch resolved, and ONE
        final marked ``last`` (sent even empty: the client resolves its stop
        on it)."""
        if code is not None:
            await self.ws.send_json({"type": wire.ERROR, "code": code, "message": message})
        deadline = time.monotonic() + _END_BUDGET_S
        replace, parts = await self.switch.stop() if self.switch is not None else (None, [])
        getattr(self.provider, "bound_finish", lambda _s: None)(max(0.5, deadline - time.monotonic()))
        final = await self.provider.finish()
        if final:
            parts.append(final)
        if replace is not None:
            await self.ws.send_json({"type": "replace", "text": replace})
        await self.ws.send_json({"type": "final", "text": " ".join(parts), "last": True})

    async def _end_dead(self) -> None:
        fatal = self.fatal or ""
        logger.warning("chat STT provider %s stopped answering: %s", self.provider_name,
                       audio_service.provider_error_message(fatal, self.provider_name))
        await self._end("provider", _SAY_KEY if audio_service.is_key_refusal(fatal) else _SAY_STOPPED)
        await self.ws.close(code=_CLOSE_INTERNAL)


async def ws_audio_stt_handler(websocket: WebSocket):
    await websocket.accept()

    provider = None
    provider_name = ""
    user_sub = ""
    sample_rate = 16000
    sampwidth = 2  # 16-bit PCM
    switch: _LanguageSwitch | None = None
    dictation: _Dictation | None = None

    try:
        # 1. First frame: init + token (validated before any audio is
        # accepted). Bounded — an unauthenticated client must not park the
        # socket open forever waiting for a frame it never sends.
        init = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        if not isinstance(init, dict) or init.get("type") != "init":
            await websocket.close(code=_CLOSE_BAD_INIT)
            return
        claims = ws_audio_token.validate_ws_audio_token(init.get("token", ""))
        if not claims or not ws_audio_token.consume_jti(claims.get("jti", "")):
            await websocket.close(code=_CLOSE_UNAUTHORIZED)
            return

        user_sub = claims["sub"]
        max_seconds = int(claims.get("max_seconds", 180))
        provider_id = claims.get("provider_id")
        sample_rate = int(init.get("sample_rate", 16000)) or 16000
        # The client-supplied rate feeds BOTH the max_seconds cost cap and the
        # usage record below — clamp to the plausible PCM range so a negative
        # or absurd value can't disable the cap or log negative seconds.
        if not (8000 <= sample_rate <= 48000):
            sample_rate = 16000
        # The Automatic pick ("auto") is the providers' detection sentinel.
        language = stt_language_param(init.get("language") or "en")

        # 2. Resolve the provider (the token pre-selected which one): the row
        # and its credential once, off the loop; the factory builds a stream.
        try:
            factory, row = await asyncio.to_thread(
                audio_service.chat_provider_factory, "stt", provider_id=provider_id,
            )
        except audio_service.AudioUnavailableError as e:
            logger.warning("chat STT unavailable: %s", e)
            await websocket.send_json({"type": wire.ERROR, "message": _SAY_NO_START})
            await websocket.close(code=_CLOSE_INTERNAL)
            return
        provider_name = row["provider_name"]

        async def _could_not_start(what: str, e: Exception) -> None:
            # A dead mic with no feedback is the worst outcome: tell the
            # client (a bad credential or an outage lands here for Deepgram,
            # a provider row whose constructor raises too).
            logger.warning("chat STT provider %s %s: %s", provider_name, what, e)
            await websocket.send_json({"type": wire.ERROR, "code": "provider", "message": _SAY_NO_START})
            await websocket.close(code=_CLOSE_INTERNAL)

        # Configure the provider for the chat stream's actual rate (16 kHz; the
        # provider default is the 8 kHz telephony rate, which would make Deepgram
        # decode the PCM as garbage → empty transcripts) and request interim
        # results so the mic shows live text as the user speaks. Dictation gets
        # its own endpointing (advanced.chat_endpointing_ms) so low-latency call
        # tuning never tightens chat commits; unset → the provider's call value.
        chat_endpointing = (row.get("advanced") or {}).get("chat_endpointing_ms")
        # The Automatic pick: the stream opens in the browser's language when
        # the install lists it (the init frame's default_language, a
        # supported base code), else in the first listed one, and the
        # identifier reads the rest. One listed language is a fixed stream
        # in it: nothing to detect.
        allowed: list[str] = []
        if language == "multi":
            try:
                allowed = await run_db(audio_service.language_candidates)
            except Exception as e:  # noqa: BLE001  # the list unreadable: every supported language, the mic still starts
                logger.warning("dictation: the language list could not be read, every supported language stands in: %s", e)
                allowed = list(SUPPORTED_BASES)
        try:
            if language == "multi":
                default = supported_base(str(init.get("default_language") or ""), allowed[0])
                if default not in allowed:
                    default = allowed[0]
                if len(allowed) == 1:
                    language = default
                else:
                    built = await asyncio.to_thread(_language_switch, factory, default, sample_rate, allowed)
                    if built is not None:
                        provider, switch = built
                        language = default
            if provider is None:
                provider = factory()
        except Exception as e:  # noqa: BLE001  # a provider whose constructor raises (a bad row) answers like a failed start
            await _could_not_start("could not be built", e)
            return
        # Dictation semantics: the composer paints interims as committed text,
        # so the provider must never let painted words die with an empty/short
        # final (it promotes the shown interim instead). Chat-relay-only — the
        # call/duplex pipelines share the provider class and must not get this.
        getattr(provider, "enable_dictation_mode", lambda: None)()
        try:
            await provider.start(
                language, sample_rate=sample_rate, interim_results=True,
                endpointing_ms=int(chat_endpointing) if chat_endpointing else None,
            )
        except Exception as e:  # noqa: BLE001  # a provider's start raises whatever its SDK does
            await _could_not_start("failed to start", e)
            return
        await websocket.send_json({"type": "ready", "progress_every_s": _PROGRESS_EVERY_S, "marks_last": True})
        dictation = _Dictation(websocket, provider, switch, provider_name=provider_name,
                               max_seconds=max_seconds, sample_rate=sample_rate)
        await dictation.run()

    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as e:  # noqa: BLE001  # the handler's last line: never a stack trace to the socket
        logger.warning("audio STT ws error: %s", e)
    finally:
        if switch is not None:
            switch.close()
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
            total_bytes = dictation.total_bytes if dictation is not None else 0
            if user_sub and total_bytes:
                billed = getattr(provider, "billed_seconds", None)
                seconds = float(billed) if billed is not None else total_bytes / (sample_rate * sampwidth)
                # The second listen's batch seconds are billed audio too, on a
                # row of their own, told from the stream's in the database.
                relisten = float(getattr(provider, "relisten_seconds", 0.0) or 0.0)
                logger.info("dictation closed: %.1f s streamed, %.1f s listened again", seconds, relisten)
                rows = [(_USAGE_STREAM, seconds)] + ([(_USAGE_RELISTEN, relisten)] if relisten else [])
                for source, row_seconds in rows:
                    try:
                        await asyncio.to_thread(
                            audio_service.record_audio_usage,
                            user_sub, source, provider_name, provider, seconds=row_seconds,
                        )
                    except Exception as e:  # noqa: BLE001  # usage records are best effort, the transcript is already sent
                        logger.debug("audio STT usage record failed: %s", e)


# ---------------------------------------------------------------------------
# TTS WebSocket — streaming speech for chat voice mode.
# This is the PLATFORM sink; native browser /
# Android sinks are pure frontend and never reach this handler.
# ---------------------------------------------------------------------------

async def ws_audio_tts_handler(websocket: WebSocket):
    """Stream synthesized speech for voice mode: the client pushes sentences of
    the assistant's reply as they generate and receives 24 kHz PCM to play back.

    Handshake mirrors the STT WS — token in the first ``init`` frame, validated +
    one-time-consumed before any synthesis. Then TWO concurrent tasks share ONE
    Cartesia continuation context (so prosody carries across sentences): a READER
    loop turning ``{type:"text"}`` / ``{type:"done"}`` / ``{type:"cancel"}``
    control frames into ``send_text_chunk`` / ``cancel`` calls, and a PUMP task
    draining ``receive_audio`` → ``ws.send_bytes``. The token bakes ``max_chars``
    (cost cap); usage is recorded once on close.
    """
    await websocket.accept()

    provider = None
    provider_name = ""
    user_sub = ""
    total_chars = 0
    pump: asyncio.Task | None = None

    try:
        # 1. First frame: init + token (validated before any synthesis).
        # Bounded like the STT twin — no unauthenticated socket parking.
        init = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        if not isinstance(init, dict) or init.get("type") != "init":
            await websocket.close(code=_CLOSE_BAD_INIT)
            return
        claims = ws_audio_token.validate_ws_audio_token(
            init.get("token", ""), purpose=ws_audio_token.PURPOSE_TTS,
        )
        if not claims or not ws_audio_token.consume_jti(claims.get("jti", "")):
            await websocket.close(code=_CLOSE_UNAUTHORIZED)
            return

        user_sub = claims["sub"]
        max_chars = int(claims.get("max_chars", 0))
        provider_id = claims.get("provider_id")
        language = init.get("language") or None
        voice_id = init.get("voice_id") or None

        # 2. Build + connect the TTS provider (the token pre-selected which one).
        try:
            provider, row = await asyncio.to_thread(
                audio_service.build_chat_provider, "tts", provider_id=provider_id,
            )
        except audio_service.AudioUnavailableError as e:
            await websocket.send_json({"type": "error", "message": str(e)})
            await websocket.close(code=_CLOSE_INTERNAL)
            return
        provider_name = row["provider_name"]
        # Explicit voice wins; else the per-language voice for the reply's language.
        if voice_id:
            provider.voice_id = voice_id
        elif language:
            provider.select_voice(language)
        await provider.connect()
        provider.start_streaming_context(
            output_sample_rate=audio_service.CHAT_AUDIO_TARGET_RATE, language=language,
        )
        await websocket.send_json({"type": "ready"})

        # 3a. Pump: synthesized PCM → client until the context ends or cancels.
        async def _pump():
            async for pcm in provider.receive_audio():
                await websocket.send_bytes(pcm)

        pump = asyncio.create_task(_pump())

        # 3b. Reader: control frames → the streaming context.
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("text")
            if raw is None:
                continue  # the client sends only JSON control frames here
            try:
                ctrl = json.loads(raw)
            except (ValueError, TypeError):
                continue
            kind = ctrl.get("type")
            if kind == "text":
                text = ctrl.get("text") or ""
                if not text:
                    continue
                total_chars += len(text)
                if max_chars and total_chars > max_chars:
                    await websocket.send_json({"type": "error", "code": "max_chars",
                                               "message": "Maximum speech length reached"})
                    provider.cancel()
                    break
                # Same per-user character budget as the HTTP TTS routes —
                # without it, looping mint → WS → per-session cap would
                # burn provider spend past the admin's per-minute limit.
                ok, retry = await asyncio.to_thread(
                    audio_service.check_tts_rate, user_sub, len(text),
                )
                if not ok:
                    await websocket.send_json({
                        "type": "error", "code": "rate_limited",
                        "message": f"TTS character budget exceeded — retry in {retry}s",
                    })
                    provider.cancel()
                    break
                await provider.send_text_chunk(text)
            elif kind == "done":
                # No more text — flush, let the pump drain the rest, then signal end.
                await provider.send_text_chunk("", is_last=True)
                with contextlib.suppress(Exception):
                    await pump
                with contextlib.suppress(Exception):
                    await websocket.send_json({"type": "ended"})
                break
            elif kind == "cancel":
                provider.cancel()
                break

    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as e:  # noqa: BLE001  # the handler's last line: never a stack trace to the socket
        logger.warning("audio TTS ws error: %s", e)
    finally:
        if pump is not None and not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
            if user_sub and total_chars:
                try:
                    await asyncio.to_thread(
                        audio_service.record_audio_usage,
                        user_sub, "audio-tts-chat", provider_name, provider, chars=total_chars,
                    )
                except Exception as e:  # noqa: BLE001  # usage records are best effort, the speech is already sent
                    logger.debug("audio TTS usage record failed: %s", e)
