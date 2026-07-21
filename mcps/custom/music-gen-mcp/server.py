"""Music Generation MCP Server — AI music and sound effects via ElevenLabs.

Stdio transport. Composes music tracks (ElevenLabs Music API) and generates
sound effects (ElevenLabs SFX API) from text prompts, saves MP3s to the agent
workspace, and pushes an inline audio player to the dashboard. BYO key only:
``ELEVENLABS_API_KEY`` comes from the MCP instance config (admin page).
"""

import json
import logging
import os
import uuid

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
AUDIO_SAVE_DIR = os.environ.get("AUDIO_SAVE_DIR", "")

ELEVENLABS_BASE = "https://api.elevenlabs.io"

# MP3 44.1kHz/128kbps: web-safe everywhere (display_audio plays it without
# transcoding, video-tools muxes it directly).
OUTPUT_FORMAT = "mp3_44100_128"

# The API accepts 3s–600s; cap at 5 minutes to bound per-call credit burn.
MUSIC_MIN_SECONDS = 3
MUSIC_MAX_SECONDS = 300
DEFAULT_MUSIC_SECONDS = 30
# SFX range is the API's own.
SFX_MIN_SECONDS = 0.5
SFX_MAX_SECONDS = 30.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("music-gen")

# Music generation is slow (a 5-minute track can take a couple of minutes) —
# allow a long read timeout.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


def _missing_key_msg() -> str:
    return (
        "Error: ElevenLabs not configured. Ask an admin to set "
        "ELEVENLABS_API_KEY in the music-gen-mcp admin page."
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("music-gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="compose_music",
            description=(
                "Compose an original music track from a text description "
                "(ElevenLabs Music). The MP3 is saved to the workspace and an "
                "inline audio player is shown to the user.\n\n"
                "Describe genre, mood, instrumentation, and tempo in the prompt "
                "(e.g. 'uplifting electronic track with driving synths and a "
                "punchy four-on-the-floor beat, modern tech-launch energy'). "
                "Set `instrumental: true` for background/soundtrack use — "
                "otherwise the model may add vocals.\n\n"
                "By default the track is saved under the workspace's "
                "`generated-assets/` subfolder with an auto-generated filename. "
                "Pass `save_path` (relative or absolute under the workspace) to "
                "save elsewhere."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the music to compose (genre, mood, instruments, tempo).",
                    },
                    "duration_seconds": {
                        "type": "number",
                        "minimum": MUSIC_MIN_SECONDS,
                        "maximum": MUSIC_MAX_SECONDS,
                        "default": DEFAULT_MUSIC_SECONDS,
                        "description": f"Track length in seconds ({MUSIC_MIN_SECONDS}-{MUSIC_MAX_SECONDS}).",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["music_v1", "music_v2"],
                        "default": "music_v1",
                        "description": "ElevenLabs music model.",
                    },
                    "instrumental": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force an instrumental track (no vocals). Recommended for video soundtracks.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Optional save location. Default: workspace's "
                            "`generated-assets/` subfolder with an auto-generated "
                            "filename. Relative paths join under the workspace; "
                            "paths outside the workspace are re-anchored to "
                            "`generated-assets/<basename>` for safety."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="sound_effect",
            description=(
                "Generate a sound effect from a text description (ElevenLabs "
                "SFX). The MP3 is saved to the workspace and an inline audio "
                "player is shown to the user.\n\n"
                "Describe the sound concretely (e.g. 'soft UI confirmation "
                "chime, glassy, short decay'). Omit `duration_seconds` to let "
                "the model pick a natural length."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the sound effect to generate.",
                    },
                    "duration_seconds": {
                        "type": "number",
                        "minimum": SFX_MIN_SECONDS,
                        "maximum": SFX_MAX_SECONDS,
                        "description": f"Optional length in seconds ({SFX_MIN_SECONDS}-{SFX_MAX_SECONDS}). Omit for automatic.",
                    },
                    "loop": {
                        "type": "boolean",
                        "default": False,
                        "description": "Generate a seamlessly looping sound.",
                    },
                    "prompt_influence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "0-1: higher sticks closer to the prompt, lower is more creative. Omit for the provider default.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Optional save location. Same conventions as "
                            "`compose_music.save_path`."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "compose_music":
        return await _handle_compose(arguments)
    elif name == "sound_effect":
        return await _handle_sfx(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Proxy hooks
# ---------------------------------------------------------------------------

async def _hook_post(endpoint: str, payload: dict, *, read_timeout: float = 120.0) -> bool:
    """POST to a proxy hook endpoint. Returns True on success, False on failure
    (logged) — hook delivery is best-effort, tool results never depend on it."""
    if not PROXY_URL or not SESSION_ID:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=read_timeout, write=read_timeout, pool=5),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}{endpoint}",
                json={"session_id": SESSION_ID, **payload},
                headers={"Authorization": f"Bearer {PROXY_API_KEY}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("Hook %s failed: %s", endpoint, e)
        return False


async def _push_audio_player(file_path: str, title: str) -> bool:
    """Render an inline audio player in the chat for the saved file. The proxy
    resolves the path and serves it with Range support (same flow as
    display-mcp's display_audio)."""
    return await _hook_post("/v1/hooks/media", {
        "source": file_path,
        "media_kind": "audio",
        "title": title,
        "caption": "",
        "poster": "",
    })


# ---------------------------------------------------------------------------
# Audio saving
# ---------------------------------------------------------------------------

# Default subdirectory under workspace for auto-generated audio — same
# convention as image-gen's generated images, so all AI-generated assets
# land in one predictable place.
DEFAULT_SUBDIR = "generated-assets"


def _get_save_path(save_path: str | None, prefix: str) -> str:
    """Determine save path, always anchored under AUDIO_SAVE_DIR (workspace).

    Same anchoring rules as image-gen's ``_get_save_path``:
      - ``None`` / empty → ``<workspace>/generated-assets/<prefix>_<uuid>.mp3``
      - relative path → joined under the workspace (bare filenames drop into
        ``generated-assets/``; parent-traversal escapes are re-anchored there)
      - absolute path under the workspace → used as-is
      - absolute path elsewhere → re-anchored to
        ``generated-assets/<basename>``

    ``AUDIO_SAVE_DIR`` is injected by the platform via the manifest's
    ``path_env`` declaration; if it's missing we fail loudly so the
    misconfiguration surfaces during dev.
    """
    if not AUDIO_SAVE_DIR:
        raise RuntimeError(
            "AUDIO_SAVE_DIR is not set. The music-gen-mcp manifest must "
            "declare `path_env: {\"AUDIO_SAVE_DIR\": {\"role\": \"workspace\"}}` "
            "and the platform must inject it. Check proxy/services/path_roles.py."
        )

    workspace = AUDIO_SAVE_DIR.rstrip("/")
    default_dir = os.path.join(workspace, DEFAULT_SUBDIR)

    if not save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, f"{prefix}_{uuid.uuid4().hex[:8]}.mp3")

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
            default_dir,
            os.path.basename(normalized) or f"{prefix}_{uuid.uuid4().hex[:8]}.mp3",
        )

    if os.sep not in normalized and "/" not in save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, normalized)

    full = os.path.join(workspace, normalized)
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    return full


# ---------------------------------------------------------------------------
# ElevenLabs API
# ---------------------------------------------------------------------------

def _api_error_message(e: httpx.HTTPStatusError) -> str:
    """Map an ElevenLabs error response to an agent-readable message."""
    status = e.response.status_code
    if status == 401:
        return "ElevenLabs rejected the API key (401). Check ELEVENLABS_API_KEY in the music-gen-mcp admin page."
    detail = ""
    try:
        detail = json.dumps(e.response.json().get("detail", ""))[:300]
    except Exception:
        detail = e.response.text[:300]
    return f"ElevenLabs API error {status}: {detail}"


async def _eleven_post(path: str, body: dict) -> bytes:
    """POST to the ElevenLabs API, returning the binary audio response."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{ELEVENLABS_BASE}{path}",
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            json=body,
        )
        resp.raise_for_status()
    if not resp.content:
        raise RuntimeError("ElevenLabs returned no audio")
    return resp.content


def _coerce_number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got: {value!r}")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _handle_compose(args: dict) -> list[TextContent]:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return [TextContent(type="text", text="Error: prompt is required.")]
    if not ELEVENLABS_API_KEY:
        return [TextContent(type="text", text=_missing_key_msg())]

    try:
        duration = _coerce_number(args.get("duration_seconds", DEFAULT_MUSIC_SECONDS), "duration_seconds")
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    # Explicit range error instead of silent clamping — a paid call should
    # produce what the agent asked for or fail loudly.
    if not MUSIC_MIN_SECONDS <= duration <= MUSIC_MAX_SECONDS:
        return [TextContent(type="text", text=(
            f"Error: duration_seconds must be between {MUSIC_MIN_SECONDS} and "
            f"{MUSIC_MAX_SECONDS} (got {duration:g})."
        ))]

    model = args.get("model", "music_v1")
    if model not in ("music_v1", "music_v2"):
        return [TextContent(type="text", text=f"Error: unknown model: {model}")]
    instrumental = bool(args.get("instrumental", False))

    body = {
        "prompt": prompt,
        "music_length_ms": int(duration * 1000),
        "model_id": model,
        "force_instrumental": instrumental,
    }
    try:
        audio = await _eleven_post("/v1/music", body)
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"Error: {_api_error_message(e)}")]
    except Exception as e:
        logger.exception("Music composition failed: %s", e)
        return [TextContent(type="text", text=f"Error composing music: {e}")]

    path = _get_save_path(args.get("save_path"), "music")
    with open(path, "wb") as f:
        f.write(audio)

    played = await _push_audio_player(path, f"Music: {prompt[:80]}")
    suffix = " Audio player displayed to user." if played else ""
    return [TextContent(type="text", text=(
        f"Composed {duration:g}s track with ElevenLabs Music ({model}"
        f"{', instrumental' if instrumental else ''}). Saved to: {path}.{suffix}"
    ))]


async def _handle_sfx(args: dict) -> list[TextContent]:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return [TextContent(type="text", text="Error: prompt is required.")]
    if not ELEVENLABS_API_KEY:
        return [TextContent(type="text", text=_missing_key_msg())]

    body: dict = {"text": prompt, "loop": bool(args.get("loop", False))}

    if args.get("duration_seconds") is not None:
        try:
            duration = _coerce_number(args["duration_seconds"], "duration_seconds")
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        if not SFX_MIN_SECONDS <= duration <= SFX_MAX_SECONDS:
            return [TextContent(type="text", text=(
                f"Error: duration_seconds must be between {SFX_MIN_SECONDS:g} and "
                f"{SFX_MAX_SECONDS:g} (got {duration:g})."
            ))]
        body["duration_seconds"] = duration

    if args.get("prompt_influence") is not None:
        try:
            influence = _coerce_number(args["prompt_influence"], "prompt_influence")
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        if not 0 <= influence <= 1:
            return [TextContent(type="text", text=(
                f"Error: prompt_influence must be between 0 and 1 (got {influence:g})."
            ))]
        body["prompt_influence"] = influence

    try:
        audio = await _eleven_post("/v1/sound-generation", body)
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"Error: {_api_error_message(e)}")]
    except Exception as e:
        logger.exception("Sound effect generation failed: %s", e)
        return [TextContent(type="text", text=f"Error generating sound effect: {e}")]

    path = _get_save_path(args.get("save_path"), "sfx")
    with open(path, "wb") as f:
        f.write(audio)

    played = await _push_audio_player(path, f"SFX: {prompt[:80]}")
    suffix = " Audio player displayed to user." if played else ""
    return [TextContent(type="text", text=(
        f"Generated sound effect. Saved to: {path}.{suffix}"
    ))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
