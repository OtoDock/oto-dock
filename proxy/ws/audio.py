"""WebSocket STT endpoint for the chat mic icon (``/ws/audio/stt``).

Handshake: the client opens the socket WITHOUT a token in the URL
(query strings leak into logs) and sends a first JSON ``init`` frame carrying the
short-lived token from ``POST /v1/audio/stt/session``. The server validates +
one-time-consumes the token, opens the STT stream, then receives binary PCM
frames and streams back ``{type:"final", text}`` JSON.

Bounds: the token bakes in ``max_seconds`` — when the cumulative audio exceeds it
the server closes 1011 (cost cap). Backpressure is via ``await`` (forwarding each
frame to the provider blocks the receive loop → TCP backpressure), so there's no
unbounded buffer to overflow. Registered by app.py as ``/ws/audio/stt``.
"""

import asyncio
import contextlib
import json
import logging
import math
import time

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from audio.streaming.lang import SUPPORTED_LANGUAGES, base_lang, pcm16_to_wav, stt_language_param
from services.media import audio_service
from services.media import ws_audio_token
from ws import wire_events as wire

logger = logging.getLogger("otodock")

# WS close codes.
_CLOSE_BAD_INIT = 4400
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_INTERNAL = 1011

# The dictation lock on the Automatic pick, for a provider whose automatic
# stream covers a fixed set of languages (Deepgram's ``multi``: ten, Greek
# not among them): the phone route's identify, lock, restart and replay
# (``phone/pipeline/language.py``) applied to the chat mic. Voiced audio
# before an identification, the extra voiced audio each later attempt waits
# for, the attempts, the confidence that moves the stream, the int16 RMS a
# frame counts as voiced at (the Scribe silence gate's floor), the audio
# kept for the replay, the replay's pieces, and how long after a replay a
# stop still waits for the replay's final.
_LOCK_MIN_VOICED_S = 1.2
_LOCK_RETRY_VOICED_S = 1.0
# Answers that carried words count as attempts; requests count whatever
# they answered, so room noise over the RMS floor cannot spend the attempts
# before the first word and cannot ask forever either.
_LOCK_MAX_ATTEMPTS = 3
_LOCK_MAX_REQUESTS = 5
# Requests in flight at once: the vendor answers in 0.8 to 2 s, and a
# longer clip's answer landing first is acted on; the earlier one is dropped.
_LOCK_MAX_INFLIGHT = 2
# A covered language keeps the stream at the lower confidence (the automatic
# stream already handles it, holding words any longer only costs); a move
# to another stream needs two words and the higher one. The first clip of
# continuous Greek answers Greek at about 0.75, and a wrong move can only
# reach a menu language the automatic stream lacks, so 0.7 is the move.
_LOCK_MOVE_CONFIDENCE = 0.7
_LOCK_STAY_CONFIDENCE = 0.6
_LOCK_VOICED_RMS = 300
_LOCK_BUFFER_S = 20.0
_LOCK_REPLAY_PIECE_S = 0.2
_LOCK_REPLAY_FINAL_WAIT_S = 1.5
# The silence before the first word is neither identified nor replayed (a
# pause before speaking would ride both); this much of it is kept so the
# first syllable's onset is.
_LOCK_PREROLL_S = 0.3
# The stream moves only to a language the dictation menu offers: an
# accented speaker identified as a language nobody here dictates in stays
# on the automatic stream instead of locking a whole session to it.
_LOCK_LANGUAGES = frozenset(base_lang(code) for code, _label in SUPPORTED_LANGUAGES)

LOCK_RELEASED = "released"
LOCK_RESTARTED = "restarted"


def _rms(chunk: bytes) -> float:
    samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype=np.int16).astype(np.float32)
    if not len(samples):
        return 0.0
    return math.sqrt(float(np.mean(samples * samples)))


class _LanguageLock:
    """One identification, one lock, for a dictation socket.

    The receive loop feeds every chunk; while the lock is open the loop
    holds the stream's finals here and lets its partials flow (the composer
    shows text as the person speaks; a partial in the wrong language stays
    until the reopened stream's first replaces it), so the automatic
    stream's reading of a language it does not cover never lands as a
    final. The loop asks ``settle`` before it sends a chunk
    on: a confident answer in a covered language releases the held finals
    and keeps the stream; one in a language the stream transcribes reopens
    the stream in it and replays the buffered audio (the chunk that
    triggered the settle included), and the held finals are dropped, since
    the replay re-delivers them in the right language. Only the receive
    loop ever sends on the stream, so a restart never races a live chunk.
    """

    def __init__(self, provider, sample_rate: int, covered: frozenset[str]):
        self._provider = provider
        self._rate = sample_rate
        self._covered = covered
        self._buffer = bytearray()
        self._max_bytes = int(_LOCK_BUFFER_S * sample_rate) * 2
        self._heard = False
        self._preroll: list[bytes] = []
        self._preroll_bytes = 0
        self._preroll_max = int(_LOCK_PREROLL_S * sample_rate) * 2
        self._voiced_s = 0.0
        self._next_voiced_s = _LOCK_MIN_VOICED_S
        self._attempts = 0
        self._requests = 0
        # The buffer reached its cap: whatever is still in flight is the
        # last chance, then the stream stays as it is.
        self._full = False
        self._tasks: list[asyncio.Task] = []
        self.locked = False
        self.held: list[str] = []
        self.replayed_at = 0.0
        # The seconds the identification requests carried (a separate,
        # billable request each), recorded with the socket's usage.
        self.identified_seconds = 0.0

    def feed(self, chunk: bytes) -> None:
        """Keep the chunk for the replay, count its voiced audio, and start
        an identification once enough is in (and the last one answered)."""
        voiced = _rms(chunk) >= _LOCK_VOICED_RMS
        if not self._heard:
            if not voiced:
                # Before the first word only the pre-roll is kept.
                self._preroll.append(chunk)
                self._preroll_bytes += len(chunk)
                while len(self._preroll) > 1 and self._preroll_bytes - len(self._preroll[0]) >= self._preroll_max:
                    self._preroll_bytes -= len(self._preroll.pop(0))
                return
            self._heard = True
            for kept in self._preroll:
                self._buffer.extend(kept)
            self._preroll.clear()
        self._buffer.extend(chunk)
        if len(self._buffer) > self._max_bytes:
            del self._buffer[: len(self._buffer) - self._max_bytes]
            self._full = True
        if voiced:
            self._voiced_s += len(chunk) / (self._rate * 2)
        if (len(self._tasks) < _LOCK_MAX_INFLIGHT and self._requests < _LOCK_MAX_REQUESTS
                and self._attempts < _LOCK_MAX_ATTEMPTS and self._voiced_s >= self._next_voiced_s):
            self._requests += 1
            self._next_voiced_s = self._voiced_s + _LOCK_RETRY_VOICED_S
            self.identified_seconds += len(self._buffer) / (self._rate * 2)
            self._tasks.append(asyncio.create_task(
                self._provider.identify_language(pcm16_to_wav(bytes(self._buffer), self._rate))))

    def _spent(self) -> bool:
        """No request will answer and none may be sent: the budgets are
        gone or the buffer is full."""
        return not self._tasks and (
            self._full or self._requests >= _LOCK_MAX_REQUESTS or self._attempts >= _LOCK_MAX_ATTEMPTS)

    async def settle(self) -> str | None:
        """Act on an identification that answered: ``LOCK_RELEASED`` (the
        held finals go out, the stream stays), ``LOCK_RESTARTED`` (the
        stream reopened and the buffer was replayed), None while the lock
        is still open. Raises when the stream could not be reopened at all."""
        if self.locked:
            return None
        task = next((t for t in self._tasks if t.done()), None)
        if task is None:
            if self._spent():
                logger.info("dictation language: nothing identified in time, the automatic stream stays")
                return await self._release()
            return None
        self._tasks.remove(task)
        guess = None
        try:
            guess = task.result()
        except Exception as e:
            logger.warning("dictation language identification failed: %s", e)
        lang = base_lang(guess.language) if guess is not None and guess.language else ""
        words = len((guess.text or "").split()) if guess is not None else 0
        if words:
            self._attempts += 1
        logger.info("dictation language guess: %s %.2f, %d words (request %d, attempt %d)",
                    lang or "none", guess.confidence if guess is not None else 0.0, words,
                    self._requests, self._attempts)
        if lang and lang in self._covered and words >= 1 and guess.confidence >= _LOCK_STAY_CONFIDENCE:
            logger.info("dictation language %s (%.2f): the automatic stream covers it",
                        lang, guess.confidence)
            return await self._release()
        if lang and guess.confidence >= _LOCK_MOVE_CONFIDENCE and words >= 2:
            if lang in _LOCK_LANGUAGES and self._provider.streams_language(lang):
                return await self._restart(lang, guess.confidence)
            logger.info("dictation language %s (%.2f): not a dictation language of this stream, kept",
                        lang, guess.confidence)
            return await self._release()
        if self._spent():
            logger.info("dictation language: no confident answer in %d attempts, the automatic stream stays",
                        self._attempts)
            return await self._release()
        return None

    async def _release(self) -> str:
        self.locked = True
        await self.cancel()
        self._buffer.clear()
        return LOCK_RELEASED

    async def _restart(self, lang: str, confidence: float) -> str:
        self.locked = True
        await self.cancel()
        if not await self._provider.restart(lang):
            logger.warning("dictation stream restart in %s failed, reopening the automatic stream", lang)
            if not await self._provider.restart("multi"):
                raise RuntimeError("the speech-to-text stream could not be reopened")
            # The old stream's unfinished window died with it: the replay
            # into the reopened automatic stream recovers those words.
            lang = "multi"
        self.held.clear()
        audio = bytes(self._buffer)
        self._buffer.clear()
        piece = int(_LOCK_REPLAY_PIECE_S * self._rate) * 2
        for i in range(0, len(audio), piece):
            await self._provider.send_audio(audio[i:i + piece])
        self.replayed_at = time.monotonic()
        if lang == "multi":
            logger.info("dictation: the automatic stream reopened, %.1f s replayed",
                        len(audio) / (self._rate * 2))
        else:
            logger.info("dictation language locked: %s (%.2f), %.1f s replayed",
                        lang, confidence, len(audio) / (self._rate * 2))
        return LOCK_RESTARTED

    async def cancel(self) -> None:
        """The identifications still in flight once the lock has decided or
        the socket ends: nothing of their answers is wanted, and an error
        must not go unretrieved."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self) -> list[str]:
        """The socket's stop: a running identification is cancelled and the
        held finals go out; a replay a moment ago has its final awaited,
        since ``finish`` returns only what is already queued."""
        await self.cancel()
        out, self.held = list(self.held), []
        if self.replayed_at and time.monotonic() - self.replayed_at < _LOCK_REPLAY_FINAL_WAIT_S:
            with contextlib.suppress(Exception):
                await self._provider.force_endpoint()
                late = await self._provider.wait_for_transcript(1.0)
                if late:
                    out.append(late)
        return out


async def ws_audio_stt_handler(websocket: WebSocket):
    await websocket.accept()

    provider = None
    provider_name = ""
    user_sub = ""
    total_bytes = 0
    sample_rate = 16000
    sampwidth = 2  # 16-bit PCM
    lock: _LanguageLock | None = None

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

        # 2. Build + open the STT provider (the token pre-selected which one).
        try:
            provider, row = await asyncio.to_thread(
                audio_service.build_chat_provider, "stt", provider_id=provider_id,
            )
        except audio_service.AudioUnavailableError as e:
            await websocket.send_json({"type": "error", "message": str(e)})
            await websocket.close(code=_CLOSE_INTERNAL)
            return
        provider_name = row["provider_name"]
        # Configure the provider for the chat stream's actual rate (16 kHz; the
        # provider default is the 8 kHz telephony rate, which would make Deepgram
        # decode the PCM as garbage → empty transcripts) and request interim
        # results so the mic shows live text as the user speaks. Dictation gets
        # its own endpointing (advanced.chat_endpointing_ms) so low-latency call
        # tuning never tightens chat commits; unset → the provider's call value.
        chat_endpointing = (row.get("advanced") or {}).get("chat_endpointing_ms")
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
        except Exception as e:
            # A dead mic with no feedback is the worst outcome — tell the client
            # (bad credential / provider outage land here for Deepgram).
            logger.warning("chat STT provider %s failed to start: %s", provider_name, e)
            await websocket.send_json({"type": "error", "code": "provider",
                                       "message": "Speech-to-text could not start — check the provider configuration"})
            await websocket.close(code=_CLOSE_INTERNAL)
            return
        await websocket.send_json({"type": "ready"})
        # The Automatic pick on a provider whose automatic stream covers a
        # fixed set of languages: the first words identify the language and
        # a stream for it replaces the automatic one (_LanguageLock).
        covered = getattr(provider, "multi_languages", None)
        lock = _LanguageLock(provider, sample_rate, covered) if language == "multi" and covered else None

        # 3. Stream loop: binary PCM in, transcripts out.
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            chunk = msg.get("bytes")
            if chunk is not None:
                total_bytes += len(chunk)
                if total_bytes / (sample_rate * sampwidth) > max_seconds:
                    await websocket.send_json({"type": "error", "code": "max_seconds",
                                               "message": "Maximum recording length reached"})
                    await websocket.close(code=_CLOSE_INTERNAL)
                    break
                if lock is not None and not lock.locked:
                    lock.feed(chunk)
                    try:
                        outcome = await lock.settle()
                    except Exception as e:
                        # The words dictated before the lock go out before
                        # the failure; the client keeps what it has.
                        if lock.held:
                            await websocket.send_json({"type": "final", "text": " ".join(lock.held)})
                            lock.held.clear()
                        await websocket.send_json({"type": wire.ERROR, "code": "provider",
                                                   "message": str(e)})
                        await websocket.close(code=_CLOSE_INTERNAL)
                        break
                    if outcome == LOCK_RELEASED:
                        for held in lock.held:
                            await websocket.send_json({"type": "final", "text": held})
                        lock.held.clear()
                    elif outcome == LOCK_RESTARTED:
                        # The partial the automatic stream painted stays
                        # until the reopened stream's first partial replaces
                        # it (a blank between the two reads worse than a
                        # wrong reading for the moment). The replay carried
                        # this chunk.
                        continue
                await provider.send_audio(chunk)
                text = provider.drain_transcript()
                if text:
                    if lock is not None and not lock.locked:
                        lock.held.append(text)
                    else:
                        await websocket.send_json({"type": "final", "text": text})
                # Live partials → the client shows text as the user speaks and
                # commits it when a final arrives (the native-dictation feel).
                # They flow while the lock is open too: a partial is replaced
                # by the next one, so the automatic stream's reading of a
                # language it does not cover costs a flicker, never a word;
                # only the finals wait, since a committed word stays.
                interim = getattr(provider, "pop_interim", lambda: None)()
                if interim:
                    await websocket.send_json({"type": "interim", "text": interim})
                # A provider that died after connecting (auth/quota error frame)
                # would otherwise look like a mic that hears nothing — forward
                # the failure so the client can stop and show it.
                fatal = getattr(provider, "pop_fatal_error", lambda: None)()
                if fatal:
                    await websocket.send_json({"type": "error", "code": "provider",
                                               "message": fatal})
                    await websocket.close(code=_CLOSE_INTERNAL)
                    break
                continue

            # Control frame: {"type":"stop"} flushes the final transcript.
            raw = msg.get("text")
            if raw is not None:
                try:
                    ctrl = json.loads(raw)
                except (ValueError, TypeError):
                    ctrl = {}
                if ctrl.get("type") == "stop":
                    # One final frame: the client resolves on the first
                    # final after a stop and closes, so held finals and the
                    # stream's own tail travel together.
                    parts = await lock.stop() if lock is not None else []
                    final = await provider.finish()
                    if final:
                        parts.append(final)
                    if parts:
                        await websocket.send_json({"type": "final", "text": " ".join(parts)})
                    break

    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as e:  # never leak a stack trace to the socket
        logger.warning("audio STT ws error: %s", e)
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                await lock.cancel()
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
            if user_sub and total_bytes:
                seconds = total_bytes / (sample_rate * sampwidth)
                if lock is not None:
                    seconds += lock.identified_seconds
                try:
                    await asyncio.to_thread(
                        audio_service.record_audio_usage,
                        user_sub, "audio-stt-chat", provider_name, provider, seconds=seconds,
                    )
                except Exception as e:
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
    except Exception as e:  # never leak a stack trace to the socket
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
                except Exception as e:
                    logger.debug("audio TTS usage record failed: %s", e)
