"""ElevenLabs Scribe STT provider.

Batch ``transcribe_file`` via ``POST /v1/speech-to-text`` (scribe_v2, word
timestamps) and streaming via the Scribe v2 Realtime WebSocket
(``wss://api.elevenlabs.io/v1/speech-to-text/realtime``). Wire protocol
(verified against the API reference 2026-07-12):

  Client frames: ``{"message_type": "input_audio_chunk",
  "audio_base_64": <b64>, "commit": bool, "sample_rate": int}``.
  Server frames by ``message_type``: ``session_started``,
  ``partial_transcript`` (interim), ``committed_transcript`` /
  ``committed_transcript_with_timestamps`` (final), plus error types.
  Endpointing runs server-side with ``commit_strategy=vad``; a manual
  ``commit: true`` chunk is the fast-finalize path (Deepgram-Finalize
  analogue). There is no explicit end-of-session frame — closing the socket
  ends it.

First edition is interface-correct + reconnect-safe (chat mic, batch files);
it is NOT call-tuned (echo hooks stay no-ops) — Deepgram remains the calls
default. Streaming accepts pcm_8000..48000 and ulaw_8000; we always send PCM.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import math
from urllib.parse import urlencode

import httpx
import websockets

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.stt.base import STTProvider, TranscriptResult, Word

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io"
_WS_BASE = "wss://api.elevenlabs.io"

# Scribe batch is $0.22/audio-hour at the API rate card (2026-07); realtime is
# $0.39/hr — the class rate reflects batch (the transcribe endpoint dominates
# usage today); docs note the realtime delta.
_COST_PER_SECOND = 0.0000611

_BATCH_MODEL = "scribe_v2"
_REALTIME_MODEL = "scribe_v2_realtime"

# Input rates the realtime endpoint accepts as pcm_<rate>.
_PCM_RATES = (8000, 16000, 22050, 24000, 44100, 48000)

# Below this int16 RMS a frame counts as silence and its bytes are ZEROED
# before hitting the wire (0 disables). Scribe is Whisper-family and
# hallucinates on low-level room noise/breath during an open mic (observed
# live 2026-07-13: trailing "Ναι, ναι, ναι…" the user never said) — zeroing
# keeps the audio timeline intact for server-side VAD/endpointing while
# removing the noise the model invents speech from. Normal speech RMS is
# ~2000-8000; phone-mic ambience with AGC ~100-600.
#
# Zeroing alone is NOT enough: Whisper-family models hallucinate on pure
# silence too, so committing a zeros-only buffer (the mic-stop force_endpoint
# after a pause) still invents text conditioned on the prior context (observed
# live 2026-07-15: trailing repeated Greek the user never said). Hence
# ``_voiced_since_commit``: when nothing voiced was sent since the last
# commit, commits are skipped and any partial/committed text the server
# produces anyway is discarded.
_SILENCE_GATE_RMS = 300

# Real audio keeps flowing this long (audio-time) after the last voiced frame
# so soft trailing syllables aren't zeroed mid-word.
_GATE_HANGOVER_S = 0.6


def _to_elevenlabs_lang(tag: str | None) -> str | None:
    """BCP-47 → ISO 639-1 (``el-GR`` → ``el``); ``multi``/empty → ``None``
    (omit the param — Scribe auto-detects)."""
    if not tag or tag == "multi":
        return None
    return tag.split("-", 1)[0].lower()


class ElevenLabsSTT(STTProvider):
    """Streaming + batch speech-to-text via ElevenLabs Scribe."""

    capabilities = STTCapabilities(
        supports_streaming=True,
        supports_transcribe_file=True,
        supports_endpointing=True,
        supports_word_timestamps=True,
        is_local=False,
    )

    def __init__(
        self,
        *,
        api_key: str,
        endpointing_ms: int = 500,
        sample_rate: int = 8000,
        vad_silence_offset_ms: int = 50,
        model: str = _REALTIME_MODEL,
        batch_model: str = _BATCH_MODEL,
        silence_gate_rms: int = _SILENCE_GATE_RMS,
    ):
        self._api_key = api_key
        self._model = model or _REALTIME_MODEL
        self._batch_model = batch_model or _BATCH_MODEL
        self._endpointing_ms = endpointing_ms
        self._sample_rate = sample_rate
        self._vad_silence_offset_ms = vad_silence_offset_ms
        self._silence_gate_rms = silence_gate_rms
        self._gate_hangover_left = 0.0
        self._voiced_since_commit = False
        self._ws: websockets.ClientConnection | None = None
        self._reader_task: asyncio.Task | None = None
        self._transcript_queue: asyncio.Queue[str] = asyncio.Queue()
        self._transcript_ready = asyncio.Event()
        self._is_open = False
        self._latest_interim = ""
        self._last_interim_sent = ""
        self._fatal_error: str | None = None
        # Last start() parameters, kept for recover_after_opening reconnects.
        self._active_rate = sample_rate
        self._interim_results = False
        self._endpointing_override: int | None = None

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "ElevenLabsSTT":
        advanced = row.get("advanced") or {}
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            endpointing_ms=int(advanced.get("call_endpointing_ms", 500)),
            vad_silence_offset_ms=int(advanced.get("vad_silence_offset_ms", 50)),
            model=advanced.get("model_id") or _REALTIME_MODEL,
            batch_model=advanced.get("batch_model_id") or _BATCH_MODEL,
            silence_gate_rms=int(advanced.get("silence_gate_rms", _SILENCE_GATE_RMS)),
        )

    @classmethod
    def cost_per_unit(cls) -> float:
        return _COST_PER_SECOND

    @classmethod
    def default_advanced_settings(cls) -> dict:
        # model ids surfaced so the admin pill SHOWS what runs when unset.
        return {
            "call_endpointing_ms": 500,
            "chat_endpointing_ms": 1500,
            "model_id": _REALTIME_MODEL,
            "batch_model_id": _BATCH_MODEL,
            "silence_gate_rms": _SILENCE_GATE_RMS,
        }

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        for key in ("call_endpointing_ms", "chat_endpointing_ms",
                    "vad_silence_offset_ms", "silence_gate_rms"):
            if key in settings:
                try:
                    if int(settings[key]) < 0:
                        errors[key] = "must be >= 0"
                except (TypeError, ValueError):
                    errors[key] = "must be an integer"
        return errors

    @property
    def endpointing_ms(self) -> int:
        return self._endpointing_ms

    # ── Batch / prerecorded transcription ──────────────────────────

    async def transcribe_file(self, audio: bytes, *, language: str | None = None) -> TranscriptResult:
        """Transcribe a complete file via ``POST /v1/speech-to-text``.

        ``tag_audio_events`` is off — (laughter)/(music) tags pollute text
        meant for reading and subtitles. Scribe returns no duration field, so
        the billing duration derives from the last word's end offset (trailing
        silence goes unbilled — acceptable, and the direction favours the
        user)."""
        form: dict = {
            "model_id": self._batch_model,
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
        }
        lang = _to_elevenlabs_lang(language)
        if lang:
            form["language_code"] = lang
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=600.0, write=120.0, pool=15.0),
        ) as client:
            resp = await client.post(
                f"{_API_BASE}/v1/speech-to-text",
                data=form,
                files={"file": ("audio", audio, "application/octet-stream")},
                headers={"xi-api-key": self._api_key},
            )
        if resp.status_code != 200:
            detail = resp.text[:300]
            if resp.status_code in (401, 403):
                detail = "ElevenLabs rejected the API key — check the provider credential in the Audio tab"
            raise RuntimeError(f"ElevenLabs STT error {resp.status_code}: {detail}")

        doc = resp.json()
        text = (doc.get("text") or "").strip()
        words: list[Word] = []
        duration = 0.0
        for w in doc.get("words") or []:
            if w.get("type") != "word":
                continue  # skip "spacing" / "audio_event" entries
            try:
                start, end = float(w.get("start", 0.0)), float(w.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            words.append(Word(word=(w.get("text") or "").strip(), start=start, end=end))
            duration = max(duration, end)

        self._log_transcript("Scribe file", text)
        return TranscriptResult(
            text=text,
            language=doc.get("language_code") or (lang or "en"),
            audio_seconds=duration,
            words=words,
            provider_used="elevenlabs",
        )

    # ── Streaming surface ──────────────────────────────────────────

    def _ws_url(self, *, rate: int, language: str | None, endpointing_ms: int) -> str:
        params: dict = {
            "model_id": self._model,
            "audio_format": f"pcm_{rate}",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": max(0.1, endpointing_ms / 1000.0),
        }
        if language:
            params["language_code"] = language
        else:
            params["include_language_detection"] = "true"
        return f"{_WS_BASE}/v1/speech-to-text/realtime?{urlencode(params)}"

    async def start(
        self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False,
        endpointing_ms: int | None = None,
    ) -> None:
        rate = sample_rate or self._sample_rate
        if rate not in _PCM_RATES:
            raise ValueError(
                f"ElevenLabs realtime STT has no pcm_{rate} input (choose from {_PCM_RATES})"
            )
        lang = _to_elevenlabs_lang(language)
        self._active_rate = rate
        self._interim_results = interim_results
        self._endpointing_override = endpointing_ms
        self._latest_interim = ""
        self._last_interim_sent = ""
        self._gate_hangover_left = 0.0
        self._voiced_since_commit = False
        self._fatal_error = None
        self._transcript_queue = asyncio.Queue()
        self._transcript_ready.clear()

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

        self._ws = await websockets.connect(
            self._ws_url(
                rate=rate, language=lang,
                endpointing_ms=endpointing_ms if endpointing_ms is not None else self._endpointing_ms,
            ),
            additional_headers={"xi-api-key": self._api_key},
            max_size=2 ** 23,
        )
        self._is_open = True
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop(self._ws))
        logger.info("ElevenLabs STT connection opened")

    async def _read_loop(self, ws: websockets.ClientConnection) -> None:
        """Push server frames into the transcript queue until the socket ends."""
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                mtype = msg.get("message_type", "")
                if mtype == "partial_transcript":
                    text = (msg.get("text") or "").strip()
                    # Text produced with zero voiced audio in the buffer is
                    # invented — never surface it (the chat composer previews
                    # partials live and keeps the last one if no final follows).
                    if text and self._voiced_since_commit:
                        self._latest_interim = text
                elif mtype in ("committed_transcript", "committed_transcript_with_timestamps"):
                    text = (msg.get("text") or "").strip()
                    voiced = self._voiced_since_commit
                    self._voiced_since_commit = False  # commit consumed the buffer
                    if text and not voiced:
                        logger.info("ElevenLabs STT dropped silence-only transcript "
                                    f"(hallucination guard): {text[:80]!r}")
                    elif text:
                        self._log_transcript("Scribe final", text)
                        self._latest_interim = ""
                        self._last_interim_sent = ""
                        self._transcript_queue.put_nowait(text)
                        self._transcript_ready.set()
                elif mtype == "session_started":
                    logger.debug("Scribe session started")
                elif mtype:
                    # Error frames (auth_error, quota_exceeded, rate_limited, …)
                    if "error" in mtype or msg.get("error"):
                        detail = str(msg.get("error") or msg)[:200]
                        logger.error(f"ElevenLabs STT {mtype}: {detail}")
                        self._fatal_error = (
                            f"ElevenLabs speech-to-text {mtype.replace('_', ' ')}: {detail}"
                        )
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"ElevenLabs STT reader error: {e}")
        finally:
            if ws is self._ws:
                self._is_open = False

    async def _send_chunk(self, audio_bytes: bytes, *, commit: bool) -> None:
        if not self._ws or not self._is_open:
            if self._ws:
                logger.warning("ElevenLabs STT send skipped — connection closed")
            return
        frame = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(audio_bytes).decode(),
            "commit": commit,
            "sample_rate": self._active_rate,
        }
        try:
            await self._ws.send(json.dumps(frame))
        except Exception as e:
            logger.error(f"ElevenLabs STT send error: {e}")

    def _gate_silence(self, chunk: bytes) -> bytes:
        """Zero sub-threshold frames (see ``_SILENCE_GATE_RMS``). Keeps every
        byte's place in the timeline — server VAD still sees the silence gap
        and commits — but the model never hears the noise it hallucinates
        from. A hangover window passes real audio briefly after voiced frames
        so soft trailing syllables survive.

        Also tracks ``_voiced_since_commit`` for the hallucination guard:
        pass-through frames (gate disabled, non-pcm16) count as voiced so the
        guard can never drop audio it didn't inspect; hangover frames don't
        set it on their own — they only ever follow a voiced frame within the
        same uncommitted segment."""
        if not chunk:
            return chunk
        if self._silence_gate_rms <= 0 or len(chunk) % 2:
            self._voiced_since_commit = True
            return chunk  # disabled / odd length (not clean pcm16) → pass through
        samples = array.array("h", chunk)  # native little-endian == pcm_s16le here
        duration_s = len(samples) / float(self._active_rate)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        if rms >= self._silence_gate_rms:
            self._gate_hangover_left = _GATE_HANGOVER_S
            self._voiced_since_commit = True
            return chunk
        if self._gate_hangover_left > 0:
            self._gate_hangover_left -= duration_s
            return chunk
        return b"\x00" * len(chunk)

    async def send_audio(self, audio_bytes: bytes) -> None:
        await self._send_chunk(self._gate_silence(audio_bytes), commit=False)

    async def force_endpoint(self) -> None:
        """Commit whatever Scribe has buffered — one 20 ms silence frame with
        ``commit: true`` (the schema requires audio bytes on every chunk).

        Skipped when nothing voiced was sent since the last commit: the
        buffer is gated zeros, and committing it makes the model transcribe
        silence — which Whisper-family models hallucinate on."""
        if self._ws and self._is_open and self._voiced_since_commit:
            silence = b"\x00" * int(self._active_rate * 0.02) * 2
            await self._send_chunk(silence, commit=True)

    def drain_transcript(self) -> str | None:
        parts = []
        while not self._transcript_queue.empty():
            try:
                text = self._transcript_queue.get_nowait()
                if text:
                    parts.append(text)
            except asyncio.QueueEmpty:
                break
        self._transcript_ready.clear()
        transcript = " ".join(parts).strip() if parts else None
        if transcript:
            self._log_transcript("STT drain", transcript)
        return transcript

    def pop_interim(self) -> str | None:
        """Latest live partial if it changed since the last call (the chat WS
        duck-types this for live dictation text)."""
        txt = self._latest_interim
        if txt and txt != self._last_interim_sent:
            self._last_interim_sent = txt
            return txt
        return None

    @property
    def latest_interim(self) -> str:
        return self._latest_interim

    def pop_fatal_error(self) -> str | None:
        err, self._fatal_error = self._fatal_error, None
        return err

    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        self._transcript_ready.clear()
        try:
            await asyncio.wait_for(self._transcript_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.drain_transcript()

    def clear_queue(self) -> None:
        discarded = 0
        while not self._transcript_queue.empty():
            try:
                self._transcript_queue.get_nowait()
                discarded += 1
            except asyncio.QueueEmpty:
                break
        self._transcript_ready.clear()
        if discarded:
            logger.info(f"STT queue cleared ({discarded} items discarded)")

    async def finish(self) -> str | None:
        """Commit pending audio, give the final a moment to arrive, close.

        With nothing voiced since the last commit there is nothing worth
        finalizing — skip the commit AND the wait, so stopping the mic after
        a pause is instant instead of a hallucination-prone silence commit."""
        if self._ws and self._is_open and self._voiced_since_commit:
            try:
                await self.force_endpoint()
                await asyncio.wait_for(self._transcript_ready.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"ElevenLabs STT finish error: {e}")
        await self.close()
        return self.drain_transcript()

    async def close(self) -> None:
        self._is_open = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Lifecycle hooks ────────────────────────────────────────────

    async def recover_after_opening(self, language: str) -> bool:
        """Reconnect if the socket died during a long opening TTS — the base
        no-op would report healthy over a dead mic."""
        if self._is_open:
            self.clear_queue()
            return True
        try:
            await self.close()
        except Exception:
            pass
        try:
            await self.start(
                language=language, sample_rate=self._active_rate,
                interim_results=self._interim_results,
                endpointing_ms=self._endpointing_override,
            )
            logger.info("ElevenLabs STT reconnected after opening TTS")
            return True
        except Exception as e:
            logger.warning(f"ElevenLabs STT reconnect after opening failed: {e}")
            return False

    @property
    def vad_silence_padding_ms(self) -> int:
        return self._endpointing_ms + self._vad_silence_offset_ms
