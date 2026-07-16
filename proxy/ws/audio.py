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

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from services.media import audio_service
from services.media import ws_audio_token

logger = logging.getLogger("claude-proxy")

# WS close codes.
_CLOSE_BAD_INIT = 4400
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_INTERNAL = 1011


async def ws_audio_stt_handler(websocket: WebSocket):
    await websocket.accept()

    provider = None
    provider_name = ""
    user_sub = ""
    total_bytes = 0
    sample_rate = 16000
    sampwidth = 2  # 16-bit PCM

    try:
        # 1. First frame: init + token (validated before any audio is accepted).
        init = await websocket.receive_json()
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
        language = init.get("language", "en")

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
                await provider.send_audio(chunk)
                text = provider.drain_transcript()
                if text:
                    await websocket.send_json({"type": "final", "text": text})
                # Live partials → the client shows text as the user speaks and
                # commits it when a final arrives (the native-dictation feel).
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
                    final = await provider.finish()
                    if final:
                        await websocket.send_json({"type": "final", "text": final})
                    break

    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as e:  # never leak a stack trace to the socket
        logger.warning("audio STT ws error: %s", e)
    finally:
        if provider is not None:
            try:
                await provider.close()
            except Exception:
                pass
            if user_sub and total_bytes:
                seconds = total_bytes / (sample_rate * sampwidth)
                try:
                    await asyncio.to_thread(
                        audio_service.record_audio_usage,
                        user_sub, "audio-stt-chat", provider_name, provider, seconds=seconds,
                    )
                except Exception:
                    pass


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
        init = await websocket.receive_json()
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
            try:
                await provider.close()
            except Exception:
                pass
            if user_sub and total_chars:
                try:
                    await asyncio.to_thread(
                        audio_service.record_audio_usage,
                        user_sub, "audio-tts-chat", provider_name, provider, chars=total_chars,
                    )
                except Exception:
                    pass
