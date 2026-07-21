"""TTS MCP — text → voice-over WAV files via the platform's TTS providers.

Thin client: the actual synthesis runs on the platform. Every tool is a call
to the proxy's ``/v1/audio/tts/*`` endpoints — the proxy resolves the
configured TTS provider (Cartesia, ElevenLabs, …), holds the vendor
credentials, and records cost. The MCP itself never sees an API key, so new
engines added platform-side work here without a code change.

Unlike the phone/chat surfaces (static per-language voice map), every call may
mix and match: pick any ``voice_id``, ``model_id``, language, and delivery
settings — that's the point for video voice-over work, where the voice is a
creative choice per project.

Env (auto-injected by the platform): ``PROXY_URL`` + ``PROXY_API_KEY`` reach
the proxy; ``OTO_SESSION_ID`` keys the inline-player hook; ``AUDIO_SAVE_DIR``
is the agent's workspace dir; ``TTS_PROVIDER_ID`` is the instance's provider
binding (blank = platform default).
"""

from __future__ import annotations

import logging
import os
import uuid

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts-mcp")

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")
AUDIO_SAVE_DIR = os.environ.get("AUDIO_SAVE_DIR", "")
# Per-instance TTS provider binding (the instance's tts_provider_select field).
INSTANCE_PROVIDER_ID = os.environ.get("TTS_PROVIDER_ID", "").strip()

# Default subdirectory under workspace for generated audio — same convention
# as image-gen / music-gen, so all AI-generated assets land in one place.
DEFAULT_SUBDIR = "generated-assets"

# Generation is synthesis-time bound (a 5k-char narration takes a while).
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)

server = Server("tts")


class TtsError(Exception):
    """A user-facing failure (formatted as ``Error: ...``)."""


def _text(s: str) -> list[TextContent]:
    return [TextContent(type="text", text=s)]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


def _effective_provider_id(args: dict):
    """Which TTS provider to use: the tool's ``provider_id`` arg wins; else the
    instance binding (``TTS_PROVIDER_ID`` env); else ``None`` → the proxy picks
    the platform default."""
    pid = args.get("provider_id")
    if pid is not None:
        return pid
    if INSTANCE_PROVIDER_ID:
        try:
            return int(INSTANCE_PROVIDER_ID)
        except ValueError:
            return None
    return None


async def _proxy_request(method: str, path: str, **kwargs) -> httpx.Response:
    if not PROXY_URL or not PROXY_API_KEY:
        raise TtsError("PROXY_URL / PROXY_API_KEY not injected — tts-mcp is misconfigured.")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await client.request(
            method, f"{PROXY_URL.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
            **kwargs,
        )


def _proxy_error(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text[:300]
    return f"speech service returned {resp.status_code}: {detail}"


# Path contract: ``save_path`` is declared in the manifest's ``tool_arg_paths``,
# so the platform's stdio interceptor has already gated and rewritten it to a
# real absolute path when it's given. The anchoring below (same rules as
# music-gen/image-gen) is the fallback shaping for defaults/relative forms.

def _get_save_path(save_path: str | None) -> str:
    """Save location, always anchored under AUDIO_SAVE_DIR (workspace):
    default → ``generated-assets/voiceover_<uuid>.wav``; relative joins under
    the workspace (bare filenames drop into ``generated-assets/``); absolute
    outside the workspace re-anchors to ``generated-assets/<basename>``."""
    if not AUDIO_SAVE_DIR:
        raise TtsError(
            "AUDIO_SAVE_DIR is not set. The tts-mcp manifest must declare "
            "`path_env: {\"AUDIO_SAVE_DIR\": {\"role\": \"workspace\"}}`."
        )
    workspace = AUDIO_SAVE_DIR.rstrip("/")
    default_dir = os.path.join(workspace, DEFAULT_SUBDIR)

    if not save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, f"voiceover_{uuid.uuid4().hex[:8]}.wav")

    if not save_path.endswith(".wav"):
        save_path += ".wav"

    if os.path.isabs(save_path):
        normalized = os.path.normpath(save_path)
        if normalized == workspace or normalized.startswith(workspace + os.sep):
            os.makedirs(os.path.dirname(normalized) or workspace, exist_ok=True)
            return normalized
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, os.path.basename(normalized))

    normalized = os.path.normpath(save_path)
    if normalized.startswith(".." + os.sep) or normalized == "..":
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(
            default_dir, os.path.basename(normalized) or f"voiceover_{uuid.uuid4().hex[:8]}.wav",
        )

    if os.sep not in normalized and "/" not in save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, normalized)

    full = os.path.join(workspace, normalized)
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    return full


async def _push_audio_player(file_path: str, title: str, caption: str = "") -> bool:
    """Inline audio player in the chat (display-mcp media hook). Best-effort:
    tool results never depend on it."""
    if not PROXY_URL or not SESSION_ID:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=60, write=60, pool=5),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL.rstrip('/')}/v1/hooks/media",
                json={"session_id": SESSION_ID, "source": file_path,
                      "media_kind": "audio", "title": title, "caption": caption, "poster": ""},
                headers={"Authorization": f"Bearer {PROXY_API_KEY}",
                         "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("Media hook failed: %s", e)
        return False


def _format_voices(voices: list[dict], *, with_owner: bool = False) -> list[str]:
    lines = []
    for v in voices:
        langs = ",".join(v.get("languages") or []) or "?"
        parts = [f"- **{v.get('name') or v.get('id')}** — `{v.get('id')}`  ({langs}"]
        if v.get("category"):
            parts.append(f"; {v['category']}")
        parts.append(")")
        if v.get("description"):
            parts.append(f" — {v['description']}")
        if with_owner and v.get("owner_id"):
            parts.append(f"  [owner_id: `{v['owner_id']}`]")
        if v.get("preview_url"):
            parts.append(f"  [preview]({v['preview_url']})")
        lines.append("".join(parts))
    return lines


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    _provider = {
        "type": "integer",
        "description": "Optional TTS provider id to override the platform/instance default.",
    }
    _setting = {"type": "number", "minimum": 0, "maximum": 1}
    return [
        Tool(
            name="list_voices",
            description=(
                "List the voices available on the configured TTS provider "
                "(workspace + premade voices for engines with a voice catalog; "
                "the configured per-language map otherwise). Each entry shows "
                "the voice_id to pass to generate_speech and a preview link."
            ),
            inputSchema={
                "type": "object",
                "properties": {"provider_id": _provider},
            },
        ),
        Tool(
            name="search_voice_library",
            description=(
                "Search the provider's shared voice library (ElevenLabs: "
                "10,000+ community voices) by free text, language, gender, age "
                "or category — the way to FIND a voice matching a video's "
                "style. Library voices must be added to the vendor workspace "
                "before use: pass the result's owner_id + voice_id to "
                "add_library_voice (admin approval needed)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Free-text query (e.g. 'warm documentary narrator')."},
                    "language": {"type": "string", "description": "Language code filter (e.g. 'en', 'el')."},
                    "gender": {"type": "string", "enum": ["male", "female", "neutral"]},
                    "age": {"type": "string", "enum": ["young", "middle_aged", "old"]},
                    "category": {"type": "string", "enum": ["professional", "famous", "high_quality"]},
                    "page": {"type": "integer", "minimum": 0, "default": 0},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "provider_id": _provider,
                },
            },
        ),
        Tool(
            name="add_library_voice",
            description=(
                "Add a shared-library voice to the vendor workspace so "
                "generate_speech can use it. Admin-only (each add permanently "
                "consumes one of the account's limited voice slots) — "
                "non-admin sessions get a clear refusal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "public_owner_id": {"type": "string", "description": "The owner_id from search_voice_library results."},
                    "voice_id": {"type": "string", "description": "The voice_id from search_voice_library results."},
                    "name": {"type": "string", "description": "Optional display name for the added voice."},
                    "provider_id": _provider,
                },
                "required": ["public_owner_id", "voice_id"],
            },
        ),
        Tool(
            name="generate_speech",
            description=(
                "Synthesize text into a voice-over WAV saved in the workspace; "
                "an inline audio player is shown to the user. Mix and match "
                "voice_id / language / model / delivery settings per call — "
                "generate short samples with candidate voices to compare, then "
                "produce the final segments with the winner.\n\n"
                "Long narrations: generate one segment per scene/paragraph "
                "(each call is capped by the platform's per-request character "
                "limit) — video tools then place each segment on the timeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The narration text to speak."},
                    "voice_id": {
                        "type": "string",
                        "description": "Voice to use (from list_voices / search_voice_library). "
                                       "Omit to use the provider's configured per-language voice.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code (e.g. 'en', 'el'). Omit to auto-detect from the text.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Provider model override (e.g. ElevenLabs 'eleven_v3' for maximum "
                                       "expressiveness, 'eleven_multilingual_v2' for long-form consistency; "
                                       "Cartesia 'sonic-3.5'). Omit for the provider's configured model.",
                    },
                    "stability": {**_setting, "description": "Voice stability 0-1 (lower = more emotional range). Provider-specific."},
                    "similarity_boost": {**_setting, "description": "Voice similarity 0-1. Provider-specific."},
                    "style": {**_setting, "description": "Style exaggeration 0-1. Provider-specific."},
                    "speed": {"type": "number", "minimum": 0.7, "maximum": 1.2,
                              "description": "Speaking speed multiplier (1.0 = normal)."},
                    "sample_rate": {
                        "type": "integer", "enum": [8000, 16000, 22050, 24000, 44100], "default": 24000,
                        "description": "Output sample rate. 24000 is right for video voice-over; 44100 needs "
                                       "a higher provider tier on some vendors.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional save location (.wav). Default: workspace's `generated-assets/` "
                                       "with an auto-generated filename. Relative paths join under the workspace.",
                    },
                    "provider_id": _provider,
                },
                "required": ["text"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "list_voices":
            return await _handle_list_voices(arguments)
        if name == "search_voice_library":
            return await _handle_search(arguments)
        if name == "add_library_voice":
            return await _handle_add_voice(arguments)
        if name == "generate_speech":
            return await _handle_generate(arguments)
        return _err(f"unknown tool: {name}")
    except TtsError as e:
        return _err(str(e))
    except httpx.HTTPError as e:
        return _err(f"could not reach the speech service: {e}")
    except Exception as e:  # last-resort guard so the MCP never crashes the turn
        logger.exception("tts tool failed")
        return _err(f"unexpected failure: {e}")


async def _handle_list_voices(args: dict) -> list[TextContent]:
    params: dict = {}
    pid = _effective_provider_id(args)
    if pid is not None:
        params["provider_id"] = pid
    resp = await _proxy_request("GET", "/v1/audio/tts/voices", params=params)
    if resp.status_code != 200:
        return _err(_proxy_error(resp))
    body = resp.json()
    voices = body.get("voices") or []
    configured = body.get("configured") or {}
    lines = [f"## Voices — {body.get('provider_name', 'provider')} ({len(voices)})"]
    if configured:
        lines.append("Configured per-language defaults: "
                     + ", ".join(f"`{k}` → `{v}`" for k, v in configured.items()))
    lines.append("")
    lines.extend(_format_voices(voices) or ["(no voices in the catalog — pass voice_id explicitly)"])
    lines.append("")
    lines.append("Use a voice by passing its `voice_id` to `generate_speech`. "
                 "Need something different? Try `search_voice_library`.")
    return _text("\n".join(lines))


async def _handle_search(args: dict) -> list[TextContent]:
    params: dict = {}
    for key in ("search", "language", "gender", "age", "category", "page", "page_size"):
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    pid = _effective_provider_id(args)
    if pid is not None:
        params["provider_id"] = pid
    resp = await _proxy_request("GET", "/v1/audio/tts/voices/search", params=params)
    if resp.status_code != 200:
        return _err(_proxy_error(resp))
    voices = resp.json().get("voices") or []
    if not voices:
        return _text("No library voices matched — loosen the filters or try different search terms.")
    lines = [f"## Voice library results ({len(voices)})", ""]
    lines.extend(_format_voices(voices, with_owner=True))
    lines.append("")
    lines.append(
        "To USE a library voice it must first be added to the vendor workspace: "
        "`add_library_voice(public_owner_id=<owner_id>, voice_id=<voice_id>)` "
        "(admin-only). Already-added voices appear in `list_voices`."
    )
    return _text("\n".join(lines))


async def _handle_add_voice(args: dict) -> list[TextContent]:
    owner = (args.get("public_owner_id") or "").strip()
    vid = (args.get("voice_id") or "").strip()
    if not owner or not vid:
        return _err("public_owner_id and voice_id are required (from search_voice_library results)")
    payload: dict = {"public_owner_id": owner, "voice_id": vid}
    if args.get("name"):
        payload["name"] = args["name"]
    pid = _effective_provider_id(args)
    if pid is not None:
        payload["provider_id"] = pid
    resp = await _proxy_request("POST", "/v1/audio/tts/voices/add", json=payload)
    if resp.status_code == 403:
        return _err("adding library voices is admin-only (it permanently consumes a "
                    "vendor voice slot) — ask a platform admin to run this")
    if resp.status_code != 200:
        return _err(_proxy_error(resp))
    added = resp.json().get("voice_id", vid)
    return _text(f"Voice added to the workspace. Use it with `generate_speech(voice_id=\"{added}\")`.")


async def _handle_generate(args: dict) -> list[TextContent]:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("text is required")

    payload: dict = {"text": text}
    pid = _effective_provider_id(args)
    if pid is not None:
        payload["provider_id"] = pid
    for key in ("voice_id", "language", "model_id"):
        if args.get(key):
            payload[key] = args[key]
    settings = {k: args[k] for k in ("stability", "similarity_boost", "style", "speed")
                if args.get(k) is not None}
    if settings:
        payload["voice_settings"] = settings
    if args.get("sample_rate") is not None:
        payload["sample_rate"] = int(args["sample_rate"])

    resp = await _proxy_request("POST", "/v1/audio/tts/generate", json=payload)
    if resp.status_code != 200:
        return _err(_proxy_error(resp))
    if not resp.content:
        return _err("speech service returned no audio")

    path = _get_save_path(args.get("save_path"))
    with open(path, "wb") as f:
        f.write(resp.content)

    seconds = resp.headers.get("X-Audio-Seconds", "?")
    provider = resp.headers.get("X-Provider-Used", "unknown")
    voice = resp.headers.get("X-Voice-Used", "")
    # Title stays short (window chrome); the FULL narration rides as the
    # caption so the player never shows a mid-sentence cut.
    played = await _push_audio_player(path, "Voice-over", caption=text)
    suffix = " Audio player displayed to user." if played else ""
    return _text(
        f"Generated {seconds}s of speech ({provider}, voice `{voice}`). "
        f"Saved to: {path}.{suffix}"
    )


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
