"""ElevenLabs TTS provider (multi-context WebSocket streaming + REST one-shot).

Wire protocol (verified against the API reference 2026-07-12):

  wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input
      ?model_id=…&output_format=pcm_<rate>&inactivity_timeout=180[&language_code=…]

Client frames are snake_case: the first frame of a context carries
``{"text": " ", "voice_settings": …, "generation_config": …, "context_id": …}``;
text frames ``{"text": "chunk ", "context_id": …}`` (text must end with a
space); ``{"context_id": …, "flush": true}`` forces the buffer out;
``{"context_id": …, "close_context": true}`` abandons a context. Server frames
are camelCase: ``{"audio": <b64>, "contextId": …}`` and
``{"isFinal": true, "contextId": …}``.

The socket URL binds ONE (voice, model, output_format, language) tuple — a
context needing a different binding transparently reconnects (single-flight
via an asyncio.Lock). ``eleven_v3`` has no WebSocket support at all, so those
model ids run each streaming context over the HTTP streaming endpoint instead
(same provider interface, higher latency — a voice-over model, not a phone
model).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from urllib.parse import urlencode

import httpx
import websockets

from audio.capabilities import TTSCapabilities
from audio.constants import SAMPLE_RATE
from audio.log_policy import log_transcript
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.tts.base import TTSProvider, UnsupportedProviderOperation, VoiceInfo

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io"
_WS_BASE = "wss://api.elevenlabs.io"

# Default model. eleven_flash_v2_5 is the realtime tier (~75 ms model TTFB,
# 32 languages, accepts language_code). Admin-overridable per provider via
# ``advanced.model_id`` (e.g. eleven_multilingual_v2 / eleven_v3 for maximum
# voice-over quality — v3 falls back to HTTP streaming, see module docstring).
_MODEL_ID = "eleven_flash_v2_5"

# Flash/Turbo bill 0.5 credits/char ≈ $0.05/1k chars at the API rate card
# (2026-07). Multilingual v2 / v3 are 2× — the class rate reflects the default
# model; docs note the premium-model delta.
_COST_PER_CHAR = 0.00005

# The API allows at most 180 s of socket inactivity — ask for the maximum and
# reconnect transparently past it.
_INACTIVITY_TIMEOUT_S = 180

# After the input is flushed, continuous generation never goes silent this
# long — treat it as end-of-utterance so a lost isFinal degrades to a stall
# guard instead of a hung turn.
_POST_FLUSH_STALL_S = 10.0

# Observed live (2026-07): the multi-context server streams the flushed audio
# but sends NO isFinal — the context stays open for more text — so every
# utterance ended via the 10 s guard above (a phone farewell then sits ~10 s
# before the hang-up). Once audio HAS flowed for a flushed context, a short
# silent gap is conclusive: post-flush generation streams continuously, and
# the receive loop only waits on the socket between frames (local playback
# pacing suspends it), so 2 s of true recv-silence means the utterance is
# done. The 10 s guard still covers the pre-audio phase (generation latency,
# or a lost flush with text still buffered).
_POST_FLUSH_TAIL_STALL_S = 2.0

# PCM output rates the API offers (raw s16le mono — our provider contract).
_PCM_RATES = (8000, 16000, 22050, 24000, 44100)

# First-generation buffer thresholds (chars). The API default [120,160,250,290]
# waits for 120 chars before the first audio — too slow for the phone bridge,
# which pushes ~20-char chunks. Overridable via advanced.chunk_length_schedule.
_CHUNK_SCHEDULE = [50, 120, 160, 250]

_VOICE_SETTING_KEYS = ("stability", "similarity_boost", "style", "use_speaker_boost", "speed")


def _to_elevenlabs_lang(tag: str | None) -> str | None:
    """BCP-47 tag → ISO 639-1 for the ``language_code`` param (``el-GR`` → ``el``).
    ``multi``/empty → ``None`` (omit the param; the model auto-detects)."""
    if not tag or tag == "multi":
        return None
    return tag.split("-", 1)[0].lower()


def _is_ws_model(model_id: str) -> bool:
    """eleven_v3-family models have no WebSocket endpoint (HTTP only)."""
    return not model_id.startswith("eleven_v3")


def _accepts_language_code(model_id: str) -> bool:
    """Only the Flash/Turbo families enforce ``language_code``; multilingual_v2
    and v3 reject/ignore it — omit it there and let the text decide."""
    return "flash" in model_id or "turbo" in model_id


class _Context:
    """One streaming utterance. ``start_streaming_context`` (sync, per the ABC)
    only records parameters; the InitialiseContext frame goes out lazily on the
    first text chunk so a slow LLM doesn't burn the context inactivity window."""

    __slots__ = ("id", "language", "sample_rate", "init_gen", "input_done", "buffer", "http_done")

    def __init__(self, *, language: str | None, sample_rate: int):
        self.id = uuid.uuid4().hex[:12]
        self.language = language
        self.sample_rate = sample_rate
        self.init_gen = -1        # ws generation the init frame went out on
        self.input_done = False
        self.buffer: list[str] = []          # HTTP-fallback text accumulator
        self.http_done = asyncio.Event()     # HTTP fallback: set at is_last


class ElevenLabsTTS(TTSProvider):
    """Streaming text-to-speech via the ElevenLabs multi-context WebSocket."""

    capabilities = TTSCapabilities(supports_streaming=True, supports_endpointing=False, is_local=False)

    # PREMADE voice (account-independent — workspace/library-added ids would
    # 404 on other accounts): Matilda, production-verified through this
    # platform. All ElevenLabs models are multilingual, so the ``en`` entry
    # covers every language. Admin-configured voices always win.
    default_voices = {"en": "XrExE9yKIg1WjnnlVkGX"}

    def __init__(
        self,
        *,
        api_key: str = "",
        voice_id: str = "",
        voices: dict[str, str] | None = None,
        advanced: dict | None = None,
    ):
        self._api_key = api_key
        self.voices: dict[str, str] = voices or {}
        self.voice_id: str = voice_id or ""
        self._advanced = advanced or {}
        self._model_id = self._advanced.get("model_id") or _MODEL_ID
        self._voice_settings = {
            k: self._advanced[k] for k in _VOICE_SETTING_KEYS if k in self._advanced
        }
        self._chunk_schedule = self._advanced.get("chunk_length_schedule") or _CHUNK_SCHEDULE
        self._ws: websockets.ClientConnection | None = None
        self._ws_bound: tuple[str, str, int, str | None] | None = None  # (voice, model, rate, lang)
        self._ws_gen = 0                 # bumped per (re)connect — contexts re-init after one
        self._ws_lock = asyncio.Lock()   # single-flight connect/reconnect
        self._ctx: _Context | None = None
        self._cancelled = False
        self._prewarm_task: asyncio.Task | None = None

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "ElevenLabsTTS":
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            voices=row.get("voices") or {},
            advanced=row.get("advanced") or {},
        )

    @classmethod
    def cost_per_unit(cls) -> float:
        return _COST_PER_CHAR

    @classmethod
    def default_advanced_settings(cls) -> dict:
        return {"model_id": _MODEL_ID}

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        errors: dict[str, str] = {}
        if "model_id" in settings and not str(settings["model_id"]).startswith("eleven_"):
            errors["model_id"] = "must be an ElevenLabs model id (eleven_…)"
        for key, lo, hi in (
            ("stability", 0.0, 1.0), ("similarity_boost", 0.0, 1.0),
            ("style", 0.0, 1.0), ("speed", 0.7, 1.2),
        ):
            if key in settings:
                try:
                    if not lo <= float(settings[key]) <= hi:
                        errors[key] = f"must be between {lo:g} and {hi:g}"
                except (TypeError, ValueError):
                    errors[key] = "must be a number"
        if "chunk_length_schedule" in settings:
            sched = settings["chunk_length_schedule"]
            ok = isinstance(sched, list) and 1 <= len(sched) <= 4 and all(
                isinstance(v, int) and 50 <= v <= 500 for v in sched
            )
            if not ok:
                errors["chunk_length_schedule"] = "must be a list of 1-4 integers between 50 and 500"
        return errors

    # ── HTTP helpers ───────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        # xi-api-key goes in a header, never a URL query param (URLs leak into logs).
        return {"xi-api-key": self._api_key}

    @staticmethod
    def _http_error_detail(resp: httpx.Response) -> str:
        if resp.status_code in (401, 403):
            return "ElevenLabs rejected the API key — check the provider credential in the Audio tab"
        try:
            return json.dumps(resp.json().get("detail", ""))[:300]
        except Exception:
            return resp.text[:300]

    # ── Connection lifecycle ───────────────────────────────────────

    def _ws_url(self, *, sample_rate: int, language: str | None) -> str:
        params = {
            "model_id": self._model_id,
            "output_format": f"pcm_{sample_rate}",
            "inactivity_timeout": _INACTIVITY_TIMEOUT_S,
        }
        if language and _accepts_language_code(self._model_id):
            params["language_code"] = language
        return f"{_WS_BASE}/v1/text-to-speech/{self.voice_id}/multi-stream-input?{urlencode(params)}"

    async def _ensure_ws(self, ctx: _Context) -> websockets.ClientConnection:
        """Open (or transparently re-open) the socket for ``ctx``'s binding.

        The URL binds (voice, model, output rate, language) — reconnect when the
        context needs a different tuple, or when the previous socket died from
        the 180 s inactivity timeout. The lock makes reconnection single-flight
        (the chat WS reader and receive pump race on this)."""
        lang = ctx.language if _accepts_language_code(self._model_id) else None
        want = (self.voice_id, self._model_id, ctx.sample_rate, lang)
        async with self._ws_lock:
            if self._ws is not None and self._ws_bound == want and self._ws.close_code is None:
                return self._ws
            await self._close_ws_locked()
            if not self.voice_id:
                raise RuntimeError("No ElevenLabs voice selected (empty voice_id)")
            self._ws = await websockets.connect(
                self._ws_url(sample_rate=ctx.sample_rate, language=lang),
                additional_headers=self._headers(),
                max_size=2 ** 23,
            )
            self._ws_bound = want
            self._ws_gen += 1
            logger.info("ElevenLabs TTS WebSocket connected (rate=%s)", ctx.sample_rate)
            return self._ws

    async def _close_ws_locked(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
            self._ws_bound = None

    async def connect(self) -> None:
        """Eagerly open the socket at the telephony default (8 kHz, current
        voice) — call sites connect at call/session setup precisely to keep the
        first utterance fast. A context needing a different binding (chat's
        24 kHz) reconnects once, prewarmed from ``start_streaming_context``."""
        self._cancelled = False
        if self.voice_id and _is_ws_model(self._model_id):
            await self._ensure_ws(_Context(language=None, sample_rate=SAMPLE_RATE))

    async def close(self) -> None:
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()
            self._prewarm_task = None
        async with self._ws_lock:
            await self._close_ws_locked()
        self._ctx = None

    # ── One-shot synthesis (greetings / fillers) ───────────────────

    async def synthesize(self, text: str, *, language: str | None = None) -> bytes:
        """One-shot REST synthesis to raw 8 kHz PCM (the ABC's telephony
        contract). Uses the plain HTTP endpoint — works for every model,
        including eleven_v3."""
        lang = _to_elevenlabs_lang(language)
        params: dict = {"output_format": f"pcm_{SAMPLE_RATE}"}
        if lang and _accepts_language_code(self._model_id):
            params["language_code"] = lang
        body: dict = {"text": text, "model_id": self._model_id}
        if self._voice_settings:
            body["voice_settings"] = self._voice_settings
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        ) as client:
            resp = await client.post(
                f"{_API_BASE}/v1/text-to-speech/{self.voice_id}",
                params=params, headers=self._headers(), json=body,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs TTS error {resp.status_code}: {self._http_error_detail(resp)}")
        log_transcript(logger, "TTS synthesize", text)
        logger.info(f"TTS synthesized {len(resp.content)} bytes")
        return resp.content

    # ── Streaming context (LLM → TTS bridge) ───────────────────────

    def start_streaming_context(
        self, *, output_sample_rate: int | None = None, language: str | None = None,
    ) -> None:
        rate = output_sample_rate or SAMPLE_RATE
        if rate not in _PCM_RATES:
            raise ValueError(f"ElevenLabs has no pcm_{rate} output (choose from {_PCM_RATES})")
        self._ctx = _Context(language=_to_elevenlabs_lang(language), sample_rate=rate)
        self._cancelled = False
        # Prewarm the socket for this binding concurrently with LLM thinking
        # time (this method is sync per the ABC — the real open happens under
        # the lock, so the first send_text_chunk never double-connects).
        if _is_ws_model(self._model_id) and self.voice_id:
            # no running loop (sync tests) — first chunk connects
            with contextlib.suppress(RuntimeError):
                self._prewarm_task = asyncio.get_running_loop().create_task(
                    self._prewarm(self._ctx)
                )
        logger.debug("TTS streaming context started")

    async def _prewarm(self, ctx: _Context) -> None:
        try:
            await self._ensure_ws(ctx)
        except Exception as e:
            logger.debug(f"TTS prewarm failed (first chunk will retry): {e}")

    def _init_frame(self, ctx: _Context) -> dict:
        frame: dict = {
            "text": " ",
            "context_id": ctx.id,
            "generation_config": {"chunk_length_schedule": self._chunk_schedule},
        }
        if self._voice_settings:
            frame["voice_settings"] = self._voice_settings
        return frame

    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        ctx = self._ctx
        if ctx is None or self._cancelled:
            return
        if not _is_ws_model(self._model_id):
            # HTTP fallback (eleven_v3): accumulate; generation runs at flush.
            if text and text.strip():
                ctx.buffer.append(text)
            if is_last:
                ctx.input_done = True
                ctx.http_done.set()
            return
        if is_last:
            # Semantic end-of-input regardless of delivery — receive_audio's
            # stall guard then bounds the utterance even if the flush is lost.
            ctx.input_done = True
        for attempt in (0, 1):
            try:
                ws = await self._ensure_ws(ctx)
                if ctx.init_gen != self._ws_gen:
                    # First frame of the context — or a reconnect happened since
                    # the init went out (the server-side context died with it).
                    await ws.send(json.dumps(self._init_frame(ctx)))
                    ctx.init_gen = self._ws_gen
                if text and text.strip():
                    # Frames must end with whitespace or the server buffers wrong.
                    payload = text if text.endswith((" ", "\n")) else text + " "
                    await ws.send(json.dumps({"text": payload, "context_id": ctx.id}))
                if is_last:
                    await ws.send(json.dumps({"context_id": ctx.id, "flush": True}))
                return
            except Exception as e:
                if self._cancelled:
                    return
                if attempt == 0:
                    # One transparent reconnect (the 180 s inactivity timeout is
                    # the common cause); the context re-inits on the new socket.
                    logger.warning(f"TTS send failed, reconnecting: {e}")
                    continue
                logger.error(f"TTS send error: {e}")

    async def receive_audio(self):
        ctx = self._ctx
        if ctx is None:
            return
        if not _is_ws_model(self._model_id):
            async for chunk in self._receive_http(ctx):
                yield chunk
            return
        try:
            ws = await self._ensure_ws(ctx)
        except Exception as e:
            if not self._cancelled:
                logger.error(f"TTS receive connect error: {e}")
            return
        stalled_s = 0.0
        yielded = 0
        while not self._cancelled and self._ctx is ctx:
            try:
                # Short poll so cancel()/input_done state changes are noticed
                # even while the socket is silent (a plain recv() could park
                # here forever if the final frame is lost).
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                stalled_s = 0.0
            except asyncio.TimeoutError:
                if ctx.input_done:
                    # Input flushed → generation streams continuously; a silent
                    # gap means the utterance is over (the server keeps the
                    # context open and sends no isFinal — see the tail guard
                    # note above) or isFinal/flush was lost — end, don't hang.
                    stalled_s += 1.0
                    limit = (_POST_FLUSH_TAIL_STALL_S if yielded
                             else _POST_FLUSH_STALL_S)
                    if stalled_s >= limit:
                        if yielded:
                            logger.info(
                                "ElevenLabs TTS: flushed audio drained "
                                "(no isFinal) — ending utterance"
                            )
                        else:
                            logger.warning("ElevenLabs TTS: no frames after flush — ending utterance")
                        break
                continue
            except websockets.ConnectionClosed:
                if not self._cancelled and not ctx.input_done:
                    logger.warning("ElevenLabs TTS socket closed mid-utterance")
                break
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if msg.get("contextId") not in (None, ctx.id):
                continue  # stale frame from a cancelled/previous context
            audio_b64 = msg.get("audio")
            if audio_b64:
                try:
                    chunk = base64.b64decode(audio_b64)
                except Exception:
                    continue
                if chunk:
                    yielded += 1
                    yield chunk
            if msg.get("isFinal"):
                logger.debug("ElevenLabs TTS: isFinal received")
                break
            if msg.get("error"):
                logger.error(f"ElevenLabs TTS error frame: {str(msg)[:200]}")
                break

    async def _receive_http(self, ctx: _Context):
        """eleven_v3 streaming-context fallback: wait for the flush, then run
        the whole utterance through the HTTP streaming endpoint."""
        await ctx.http_done.wait()
        if self._cancelled or self._ctx is not ctx:
            return
        text = "".join(ctx.buffer).strip()
        if not text:
            return
        body: dict = {"text": text, "model_id": self._model_id}
        if self._voice_settings:
            body["voice_settings"] = self._voice_settings
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            ) as client:
                async with client.stream(
                    "POST", f"{_API_BASE}/v1/text-to-speech/{self.voice_id}/stream",
                    params={"output_format": f"pcm_{ctx.sample_rate}"},
                    headers=self._headers(), json=body,
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        logger.error(
                            f"ElevenLabs TTS error {resp.status_code}: {self._http_error_detail(resp)}"
                        )
                        return
                    async for chunk in resp.aiter_bytes():
                        if self._cancelled or self._ctx is not ctx:
                            break
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            if not self._cancelled:
                logger.error(f"TTS receive error: {e}")

    def cancel(self) -> None:
        """Cancel the current streaming context (barge-in). Sync per the ABC:
        drop local state immediately (unblocks ``receive_audio``), then fire the
        close_context frame best-effort — tolerant of ``close()`` racing it."""
        ctx, ws = self._ctx, self._ws
        self._cancelled = True
        self._ctx = None
        if ctx is not None:
            ctx.http_done.set()
        # Close the context whenever it was initialised — generation may still
        # be running server-side after the flush, and an already-finished
        # context tolerates the close.
        if ws is not None and ctx is not None and ctx.init_gen >= 0:
            frame = json.dumps({"context_id": ctx.id, "close_context": True})

            async def _send_close() -> None:
                # socket already gone — the reconnect path cleans up
                with contextlib.suppress(Exception):
                    await ws.send(frame)

            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(_send_close())
        logger.debug("TTS streaming cancelled (barge-in)")

    # ── Voice discovery ────────────────────────────────────────────

    async def list_voices(self) -> list[VoiceInfo]:
        """Workspace + premade voices via ``GET /v1/voices``."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{_API_BASE}/v1/voices", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs voices error {resp.status_code}: {self._http_error_detail(resp)}")
        out: list[VoiceInfo] = []
        for v in resp.json().get("voices", []):
            labels = v.get("labels") or {}
            langs = [vl.get("language", "") for vl in (v.get("verified_languages") or []) if vl.get("language")]
            out.append(VoiceInfo(
                id=v.get("voice_id", ""),
                name=v.get("name", ""),
                languages=langs or ([labels["language"]] if labels.get("language") else []),
                category=v.get("category", ""),
                preview_url=v.get("preview_url", ""),
                description=" ".join(
                    s for s in (labels.get("gender"), labels.get("age"), labels.get("accent"),
                                labels.get("descriptive") or labels.get("description"))
                    if s
                ),
            ))
        return out

    async def search_voice_library(
        self, *, search: str | None = None, language: str | None = None,
        gender: str | None = None, age: str | None = None,
        category: str | None = None, page: int = 0, page_size: int = 20,
    ) -> list[VoiceInfo]:
        """Search the shared voice library (``GET /v1/shared-voices``). Results
        must be added to the workspace (``add_library_voice``) before use."""
        params: dict = {"page_size": max(1, min(int(page_size), 100)), "page": max(0, int(page))}
        if search:
            params["search"] = search
        if language:
            params["language"] = _to_elevenlabs_lang(language) or language
        if gender:
            params["gender"] = gender
        if age:
            params["age"] = age
        if category:
            params["category"] = category
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/v1/shared-voices", params=params, headers=self._headers(),
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs library error {resp.status_code}: {self._http_error_detail(resp)}")
        out: list[VoiceInfo] = []
        for v in resp.json().get("voices", []):
            langs = [vl.get("language", "") for vl in (v.get("verified_languages") or []) if vl.get("language")]
            out.append(VoiceInfo(
                id=v.get("voice_id", ""),
                name=v.get("name", ""),
                languages=langs or ([v["language"]] if v.get("language") else []),
                category=v.get("category", ""),
                preview_url=v.get("preview_url", ""),
                description=(v.get("description") or "")[:200],
                owner_id=v.get("public_owner_id", ""),
            ))
        return out

    async def add_library_voice(
        self, public_owner_id: str, voice_id: str, name: str | None = None,
    ) -> str:
        """Add a shared voice to the vendor workspace (required before TTS can
        use it). Permanently consumes a workspace voice slot — the proxy gates
        this behind admin."""
        if not public_owner_id or not voice_id:
            raise UnsupportedProviderOperation("public_owner_id and voice_id are required")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/v1/voices/add/{public_owner_id}/{voice_id}",
                headers=self._headers(), json={"new_name": name or voice_id},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs add-voice error {resp.status_code}: {self._http_error_detail(resp)}")
        return resp.json().get("voice_id", voice_id)
