"""Video Generation MCP Server — AI video generation, transitions, and editing.

Stdio transport. Three tools on three providers, all BYO-key (instance config
on the admin page):
  - generate_video     — Veo 3.1 Fast (default), Veo 3.1, or Gemini Omni Flash
  - generate_transition — Seedance 2.0 reference-to-video via fal.ai (default,
    creative: the model sees both source videos, not just endpoint frames), or
    Veo 3.1 / Fast first/last-frame on the extracted junction frames (seamless
    by construction; fal-routed when FAL_API_KEY is set, Google direct as the
    region-gated fallback)
  - edit_video         — Runway Aleph 2.0 (text-instruction video-to-video)

Generated MP4s land in the agent workspace and an inline video player is
pushed to the dashboard via the media hook. Per-call cost is declared in the
manifest's `costs` block and evaluated by the proxy at TOOL_RESULT time.
Frame extraction uses imageio-ffmpeg's bundled static ffmpeg binary — no
system ffmpeg needed on any execution target.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
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
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY", "")
RUNWAY_API_KEY = os.environ.get("RUNWAY_API_KEY", "")
FAL_API_KEY = os.environ.get("FAL_API_KEY", "")
VIDEO_SAVE_DIR = os.environ.get("VIDEO_SAVE_DIR", "")

GOOGLE_BASE = "https://generativelanguage.googleapis.com"
RUNWAY_BASE = "https://api.dev.runwayml.com"
RUNWAY_VERSION = "2024-11-06"
FAL_REST_BASE = "https://rest.fal.ai"
FAL_QUEUE_URL = "https://queue.fal.run/bytedance/seedance-2.0/reference-to-video"
# Veo first/last-frame routed through fal: Google gates FLF by region ("Your
# use case is currently not supported" for EEA keys — live 2026-07-19 — while
# t2v/i2v work), but fal's US-side endpoints run the same models for everyone,
# billed on the FAL key. Input is (frame URLs, prompt); output {video: {url}}.
FAL_VEO_FLF_URLS = {
    "veo-3.1": "https://queue.fal.run/fal-ai/veo3.1/first-last-frame-to-video",
    "veo-3.1-fast": "https://queue.fal.run/fal-ai/veo3.1/fast/first-last-frame-to-video",
}

# Preview-labeled model IDs (2026-07) — isolate here so a GA rename is a
# one-line fix.
OMNI_MODEL = "gemini-omni-flash-preview"
VEO_MODELS = {
    "veo-3.1": "veo-3.1-generate-preview",
    "veo-3.1-fast": "veo-3.1-fast-generate-preview",
}

MODEL_LABELS = {
    "omni-flash": "Gemini Omni Flash",
    "veo-3.1": "Veo 3.1",
    "veo-3.1-fast": "Veo 3.1 Fast",
    "seedance": "Seedance 2.0",
    "aleph2": "Runway Aleph 2.0",
}

VEO_DURATIONS = (4, 6, 8)
SEEDANCE_MIN_SECONDS = 4
SEEDANCE_MAX_SECONDS = 15
# Documented hard input caps — checked locally to save a doomed upload.
SEEDANCE_MAX_COMBINED_BYTES = 50 * 1024 * 1024
RUNWAY_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Submit-then-poll on every provider; generations run 11 s to a few minutes.
POLL_INTERVAL_SECONDS = 10.0
POLL_BUDGET_SECONDS = 600.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("video-gen")

# API submits/polls are quick; the Omni synchronous call holds the connection
# for the whole generation, so its read timeout matches the poll budget.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
OMNI_SYNC_TIMEOUT = httpx.Timeout(connect=10.0, read=POLL_BUDGET_SECONDS, write=60.0, pool=10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


def _unavailable_msg(provider_label: str, key_name: str) -> str:
    return (
        f"Error: {provider_label} not configured. Ask an admin to set "
        f"{key_name} in the video-gen-mcp admin page."
    )


def _api_error_message(provider_label: str, key_name: str, e: httpx.HTTPStatusError) -> str:
    """Map a provider error response to an agent-readable message."""
    status = e.response.status_code
    try:
        detail = json.dumps(e.response.json())[:300]
    except Exception:
        detail = e.response.text[:300]
    if status in (401, 403):
        # Not always a bad key: Google returns 403 for project-level denials
        # (e.g. "Your project has been denied access") on a perfectly valid
        # key — always surface the provider's own words alongside the hint.
        return (
            f"{provider_label} rejected the request ({status}). Check "
            f"{key_name} in the video-gen-mcp admin page — and that the "
            f"account/project behind it has access. Provider detail: {detail}"
        )
    return f"{provider_label} API error {status}: {detail}"


def _image_mime(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/png"


def _coerce_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got: {value!r}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("video-gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_video",
            description=(
                "Generate an AI video from a text description (optionally "
                "animating a source image). The MP4 is saved to the workspace "
                "and an inline video player is shown to the user.\n\n"
                "Models: veo-3.1-fast (default) / veo-3.1 — exact 4, 6, or 8 "
                "second clips, native audio, 1080p/4k and 9:16; consistently "
                "the best visual quality in our testing. omni-flash — 720p "
                "alternative that picks its own length (duration_seconds is a "
                "target, ~10s max); noticeably weaker visuals.\n\n"
                "Pass `image_path` to animate an existing image "
                "(image-to-video). By default the video is saved under the "
                "workspace's `generated-assets/` subfolder; pass `save_path` "
                "to save elsewhere."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the video: scene, subject, motion, camera work, mood, audio.",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["veo-3.1-fast", "veo-3.1", "omni-flash"],
                        "default": "veo-3.1-fast",
                        "description": "veo-3.1-fast (default — best quality per cost) | veo-3.1 (top fidelity, pricier) | omni-flash (flexible length, weaker visuals).",
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Clip length in seconds. Veo models: exactly 4, 6, or 8. omni-flash: a target — the model picks the final length (max ~10).",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16"],
                        "default": "16:9",
                        "description": "16:9 (default) or 9:16 vertical.",
                    },
                    "resolution": {
                        "type": "string",
                        "enum": ["720p", "1080p", "4k"],
                        "default": "720p",
                        "description": "Veo models only — omni-flash is always 720p. Higher resolutions cost more.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "Optional source image to animate (image-to-video).",
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
                "required": ["prompt", "duration_seconds"],
            },
        ),
        Tool(
            name="generate_transition",
            description=(
                "Generate an AI transition clip that bridges two existing "
                "videos: it starts where the first clip ends and flows into "
                "the start of the second. Returns the BRIDGE CLIP ONLY — "
                "assemble first clip + bridge + second clip with the "
                "video-tools MCP (or ship the bridge standalone).\n\n"
                "Models: seedance (default, premium creative) — the model "
                "watches BOTH source clips (motion, not just endpoint frames) "
                "and invents a transition (4-15 seconds); source clips are "
                "limited to 15s/50MB combined (trim junction segments first "
                "if longer). veo-3.1 / veo-3.1-fast — seamless: the bridge is "
                "generated between the first clip's last frame and the second "
                "clip's first frame (4, 6, or 8 seconds), so both joins are "
                "guaranteed to match; runs via fal.ai when FAL_API_KEY is set "
                "(works everywhere), else via Google direct (region-gated — "
                "'use case not supported' for EEA keys)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "video_a_path": {
                        "type": "string",
                        "description": "The clip the transition starts from (its ending is the entry point).",
                    },
                    "video_b_path": {
                        "type": "string",
                        "description": "The clip the transition leads into (its beginning is the exit point).",
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Bridge length in seconds. seedance: 4-15. Veo models: exactly 4, 6, or 8.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional style direction for the transition (e.g. 'morph through swirling ink', 'match-cut on the circular shape').",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["seedance", "veo-3.1", "veo-3.1-fast"],
                        "default": "seedance",
                        "description": "seedance (default, creative, needs FAL_API_KEY) | veo-3.1 / veo-3.1-fast (seamless first/last-frame; via fal.ai with FAL_API_KEY, else Google direct which is region-gated).",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16"],
                        "default": "16:9",
                        "description": "Match the source clips' orientation — 9:16 for vertical footage.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional save location. Same conventions as `generate_video.save_path`.",
                    },
                },
                "required": ["video_a_path", "video_b_path", "duration_seconds"],
            },
        ),
        Tool(
            name="edit_video",
            description=(
                "Edit an existing video with a text instruction (Runway "
                "Aleph 2.0): add/remove objects, restyle, relight, change "
                "wardrobe or environment, adjust camera. The edit propagates "
                "across cuts. Input: 2-30 seconds, up to 1080p. The edited "
                "MP4 is saved to the workspace and shown to the user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "video_path": {
                        "type": "string",
                        "description": "The video to edit.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Edit instruction (e.g. 'remove the traffic cones from the street', 'make it golden hour').",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional save location. Same conventions as `generate_video.save_path`.",
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "minimum": 2,
                        "maximum": 30,
                        "description": "Length of the input video in seconds (2-30). The edit always preserves the input duration; pass this so the platform's cost tracking prices the edit correctly.",
                    },
                },
                "required": ["video_path", "prompt"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "generate_video":
        return await _handle_generate(arguments)
    elif name == "generate_transition":
        return await _handle_transition(arguments)
    elif name == "edit_video":
        return await _handle_edit(arguments)
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


async def _push_video_player(file_path: str, title: str) -> bool:
    """Render an inline video player in the chat for the saved file. The proxy
    resolves the path and serves it with Range support (same flow as
    display-mcp's display_video)."""
    return await _hook_post("/v1/hooks/media", {
        "source": file_path,
        "media_kind": "video",
        "title": title,
        "caption": "",
        "poster": "",
    })


# ---------------------------------------------------------------------------
# Video saving
# ---------------------------------------------------------------------------

# Default subdirectory under workspace for auto-generated videos — same
# convention as image-gen/music-gen, so all AI-generated assets land in one
# predictable place.
DEFAULT_SUBDIR = "generated-assets"


def _get_save_path(save_path: str | None, prefix: str) -> str:
    """Determine save path, always anchored under VIDEO_SAVE_DIR (workspace).

    Same anchoring rules as image-gen's ``_get_save_path``:
      - ``None`` / empty → ``<workspace>/generated-assets/<prefix>_<uuid>.mp4``
      - relative path → joined under the workspace (bare filenames drop into
        ``generated-assets/``; parent-traversal escapes are re-anchored there)
      - absolute path under the workspace → used as-is
      - absolute path elsewhere → re-anchored to
        ``generated-assets/<basename>``

    ``VIDEO_SAVE_DIR`` is injected by the platform via the manifest's
    ``path_env`` declaration; if it's missing we fail loudly so the
    misconfiguration surfaces during dev.
    """
    if not VIDEO_SAVE_DIR:
        raise RuntimeError(
            "VIDEO_SAVE_DIR is not set. The video-gen-mcp manifest must "
            "declare `path_env: {\"VIDEO_SAVE_DIR\": {\"role\": \"workspace\"}}` "
            "and the platform must inject it. Check proxy/services/path_roles.py."
        )

    workspace = VIDEO_SAVE_DIR.rstrip("/")
    default_dir = os.path.join(workspace, DEFAULT_SUBDIR)

    if not save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, f"{prefix}_{uuid.uuid4().hex[:8]}.mp4")

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
            os.path.basename(normalized) or f"{prefix}_{uuid.uuid4().hex[:8]}.mp4",
        )

    if os.sep not in normalized and "/" not in save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, normalized)

    full = os.path.join(workspace, normalized)
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    return full


# ---------------------------------------------------------------------------
# Shared plumbing: polling + streamed download
# ---------------------------------------------------------------------------

async def _poll_until(check, desc: str):
    """Call ``await check()`` every POLL_INTERVAL_SECONDS until it returns a
    non-None result, or POLL_BUDGET_SECONDS elapses (RuntimeError)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + POLL_BUDGET_SECONDS
    while True:
        result = await check()
        if result is not None:
            return result
        if loop.time() >= deadline:
            raise RuntimeError(
                f"{desc} did not finish within {POLL_BUDGET_SECONDS:.0f}s — "
                "the provider may be overloaded; try again."
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _download_to(url: str, path: str, *, headers: dict | None = None,
                       follow_redirects: bool = False) -> None:
    """Stream a (potentially large) video to ``path``. Writes to a ``.part``
    sibling and renames on completion, so an interrupted download never leaves
    a truncated file at the final path."""
    part = path + ".part"
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, follow_redirects=follow_redirects,
        ) as client:
            async with client.stream("GET", url, headers=headers or {}) as resp:
                resp.raise_for_status()
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        if os.path.getsize(part) == 0:
            raise RuntimeError("provider returned an empty video file")
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.remove(part)


# ---------------------------------------------------------------------------
# Provider: Veo 3.1 (Google predictLongRunning)
# ---------------------------------------------------------------------------

def _google_headers() -> dict:
    return {"x-goog-api-key": GOOGLE_AI_API_KEY}


async def _veo_generate(model_key: str, prompt: str, duration: int, aspect_ratio: str,
                        resolution: str, target_path: str,
                        first_frame: tuple[str, bytes] | None = None,
                        last_frame: tuple[str, bytes] | None = None) -> None:
    """Submit a Veo generation (text-to-video, image-to-video, or first/last
    frame), poll the long-running operation, and download the result.
    ``first_frame`` / ``last_frame`` are (mime, bytes) tuples."""
    # predictLongRunning instances take predict-style image parts
    # (bytesBase64Encoded) — the generateContent inlineData shape is rejected
    # with "`inlineData` isn't supported by this model" (verified live 2026-07-19).
    instance: dict = {"prompt": prompt}
    if first_frame:
        instance["image"] = {
            "mimeType": first_frame[0],
            "bytesBase64Encoded": base64.b64encode(first_frame[1]).decode(),
        }
    if last_frame:
        instance["lastFrame"] = {
            "mimeType": last_frame[0],
            "bytesBase64Encoded": base64.b64encode(last_frame[1]).decode(),
        }
    body = {
        "instances": [instance],
        "parameters": {
            "aspectRatio": aspect_ratio,
            "resolution": resolution,
            "durationSeconds": duration,
        },
    }
    model_id = VEO_MODELS[model_key]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{GOOGLE_BASE}/v1beta/models/{model_id}:predictLongRunning",
            headers=_google_headers(), json=body,
        )
        resp.raise_for_status()
        op_name = resp.json().get("name", "")
    if not op_name:
        raise RuntimeError("Veo did not return an operation name")

    async def check():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{GOOGLE_BASE}/v1beta/{op_name}", headers=_google_headers())
            r.raise_for_status()
            data = r.json()
        if not data.get("done"):
            return None
        if data.get("error"):
            raise RuntimeError(f"Veo generation failed: {json.dumps(data['error'])[:300]}")
        return data

    data = await _poll_until(check, "Veo generation")
    samples = (
        (data.get("response") or {}).get("generateVideoResponse") or {}
    ).get("generatedSamples") or []
    uri = ((samples[0].get("video") or {}).get("uri") or "") if samples else ""
    if not uri:
        # Typically responsible-AI filtering — surface whatever the response says.
        raise RuntimeError(
            f"Veo returned no video (possibly filtered): {json.dumps(data.get('response') or {})[:300]}"
        )
    # The download URI redirects and requires the API key header on each hop.
    await _download_to(uri, target_path, headers=_google_headers(), follow_redirects=True)


# ---------------------------------------------------------------------------
# Provider: Gemini Omni Flash (Interactions API)
# ---------------------------------------------------------------------------

def _omni_extract_video(data: dict) -> tuple[bytes | None, str]:
    """Pull the generated video out of an interaction response: inline base64
    (documented ≤4 MB quirk) or a file URI. Returns (bytes, "") or (None, uri)."""
    for step in data.get("steps") or []:
        for part in step.get("content") or []:
            mime = part.get("mime_type") or part.get("mimeType") or ""
            if part.get("data") and mime.startswith("video/"):
                return base64.b64decode(part["data"]), ""
            if part.get("uri") and (mime.startswith("video/") or "files/" in part["uri"]):
                return None, part["uri"]
    return None, ""


async def _omni_generate(prompt: str, aspect_ratio: str, target_path: str,
                         image: tuple[str, bytes] | None = None) -> None:
    """Generate a video with Gemini Omni Flash via a synchronous interaction
    (the background-polling contract is still partially documented), then
    download the result from inline data or the files API."""
    input_parts: list[dict] = [{"type": "text", "text": prompt}]
    task = "text_to_video"
    if image:
        input_parts.append({
            "type": "image",
            "data": base64.b64encode(image[1]).decode(),
            "mime_type": image[0],
        })
        task = "image_to_video"
    body = {
        "model": OMNI_MODEL,
        "input": input_parts,
        "response_format": {"type": "video", "aspect_ratio": aspect_ratio, "delivery": "uri"},
        "generation_config": {"video_config": {"task": task}},
        "background": False,
        # URI delivery REQUIRES the interaction to be stored ("store=true is
        # required when response format has video delivery set to URI" —
        # verified live 2026-07-19); inline delivery caps at ~4 MB, too small
        # for real clips.
        "store": True,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=OMNI_SYNC_TIMEOUT) as client:
        resp = await client.post(
            f"{GOOGLE_BASE}/v1beta/interactions", headers=_google_headers(), json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    video_bytes, uri = _omni_extract_video(data)
    if video_bytes:
        with open(target_path, "wb") as f:
            f.write(video_bytes)
        return
    if not uri:
        raise RuntimeError(
            f"Omni Flash returned no video: {json.dumps(data)[:300]}"
        )

    match = re.search(r"files/([^:/?]+)", uri)
    if match:
        file_name = f"files/{match.group(1)}"

        async def check():
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(f"{GOOGLE_BASE}/v1beta/{file_name}", headers=_google_headers())
                r.raise_for_status()
                state = r.json().get("state", "")
            if state == "FAILED":
                raise RuntimeError("Omni Flash video file processing failed")
            return True if state == "ACTIVE" else None

        await _poll_until(check, "Omni Flash video file")
        if ":download" not in uri:
            uri = f"{GOOGLE_BASE}/v1beta/{file_name}:download?alt=media"
    await _download_to(uri, target_path, headers=_google_headers(), follow_redirects=True)


# ---------------------------------------------------------------------------
# Provider: Runway Aleph 2.0 (video-to-video editing)
# ---------------------------------------------------------------------------

def _runway_headers() -> dict:
    return {
        "Authorization": f"Bearer {RUNWAY_API_KEY}",
        "X-Runway-Version": RUNWAY_VERSION,
    }


async def _runway_upload(video_path: str) -> str:
    """Upload a local video via Runway's ephemeral-upload flow; returns the
    ``runway://`` URI (valid 24 h)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{RUNWAY_BASE}/v1/uploads", headers=_runway_headers(),
            json={"filename": os.path.basename(video_path), "type": "ephemeral"},
        )
        resp.raise_for_status()
        data = resp.json()
    upload_url = data.get("uploadUrl") or data.get("upload_url") or ""
    runway_uri = data.get("runwayUri") or data.get("runway_uri") or ""
    fields = data.get("fields") or {}
    if not upload_url or not runway_uri:
        raise RuntimeError(f"Runway upload initiation returned an unexpected shape: {json.dumps(data)[:200]}")
    with open(video_path, "rb") as f:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
            resp = await client.post(
                upload_url, data=fields,
                files={"file": (os.path.basename(video_path), f, "video/mp4")},
            )
            resp.raise_for_status()
    return runway_uri


async def _runway_edit(video_path: str, prompt: str, target_path: str) -> None:
    """Upload, submit an aleph2 video_to_video task, poll, download."""
    video_uri = await _runway_upload(video_path)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{RUNWAY_BASE}/v1/video_to_video", headers=_runway_headers(),
            json={"model": "aleph2", "videoUri": video_uri, "promptText": prompt},
        )
        resp.raise_for_status()
        task_id = resp.json().get("id", "")
    if not task_id:
        raise RuntimeError("Runway did not return a task id")

    async def check():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{RUNWAY_BASE}/v1/tasks/{task_id}", headers=_runway_headers())
            r.raise_for_status()
            data = r.json()
        status = data.get("status", "")
        if status == "SUCCEEDED":
            return data
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(
                f"Runway edit {status.lower()}: "
                f"{data.get('failure') or data.get('failureCode') or 'no reason given'}"
            )
        return None

    data = await _poll_until(check, "Runway edit")
    outputs = data.get("output") or []
    if not outputs:
        raise RuntimeError("Runway task succeeded but returned no output")
    # Output URLs expire within 24-48h — download immediately.
    await _download_to(outputs[0], target_path)


# ---------------------------------------------------------------------------
# Provider: Seedance 2.0 reference-to-video (fal.ai queue)
# ---------------------------------------------------------------------------

def _fal_headers() -> dict:
    return {"Authorization": f"Key {FAL_API_KEY}"}


async def _fal_upload(data: bytes, name: str, mime: str) -> str:
    """Upload bytes to the fal CDN; returns the access URL. Uploads are
    publicly readable, so a 24 h object lifecycle is always attached."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{FAL_REST_BASE}/storage/auth/token?storage_type=fal-cdn-v3",
            headers={**_fal_headers(), "Content-Type": "application/json"},
            json={},
        )
        resp.raise_for_status()
        tok = resp.json()
    base_url = tok.get("base_url", "")
    token = tok.get("token", "")
    token_type = tok.get("token_type", "Bearer")
    if not base_url or not token:
        raise RuntimeError(f"fal storage token response was unexpected: {json.dumps(tok)[:200]}")
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/files/upload",
            headers={
                "Authorization": f"{token_type} {token}",
                "Content-Type": mime,
                "X-Fal-File-Name": name,
                "X-Fal-Object-Lifecycle": json.dumps({"expiration_duration_seconds": 86400}),
            },
            content=data,
        )
        resp.raise_for_status()
        access_url = resp.json().get("access_url", "")
    if not access_url:
        raise RuntimeError("fal upload returned no access_url")
    return access_url


async def _fal_queue_run(queue_url: str, body: dict, what: str) -> dict:
    """Submit a request to a fal queue endpoint, poll it to completion, and
    return the response JSON. Shared by Seedance and the fal-routed Veo FLF."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(queue_url, headers=_fal_headers(), json=body)
        resp.raise_for_status()
        submit = resp.json()
    status_url = submit.get("status_url", "")
    response_url = submit.get("response_url", "")
    if not status_url or not response_url:
        raise RuntimeError(f"fal queue submit was unexpected: {json.dumps(submit)[:200]}")

    async def check():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(status_url, headers=_fal_headers())
            r.raise_for_status()
            status = r.json().get("status", "")
        if status == "COMPLETED":
            return True
        if status in ("FAILED", "ERROR"):
            raise RuntimeError(f"{what} failed on fal.ai")
        return None

    await _poll_until(check, what)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(response_url, headers=_fal_headers())
        r.raise_for_status()
        return r.json()


async def _seedance_transition(video_a: str, video_b: str, style: str,
                               duration: int, aspect_ratio: str, target_path: str) -> None:
    """Upload both clips PLUS their junction frames and submit a
    reference-to-video request whose prompt anchors the transition's endpoints
    on those frames. Seedance output is not pixel-continuous — without the
    explicit frame anchors the bridge tends to end on drifted framing (live
    finding 2026-07-19: a bridge exited zoomed-in relative to clip B, making
    the assembly cut visibly jump)."""
    last_a = await _extract_frame(video_a, "last")
    first_b = await _extract_frame(video_b, "first")
    with open(video_a, "rb") as f:
        bytes_a = f.read()
    with open(video_b, "rb") as f:
        bytes_b = f.read()
    url_a = await _fal_upload(bytes_a, os.path.basename(video_a), "video/mp4")
    url_b = await _fal_upload(bytes_b, os.path.basename(video_b), "video/mp4")
    url_last_a = await _fal_upload(last_a, "a_last.png", "image/png")
    url_first_b = await _fal_upload(first_b, "b_first.png", "image/png")
    prompt = SEEDANCE_TRANSITION_TEMPLATE + (f" {style}" if style else "")
    body = {
        "prompt": prompt,
        "image_urls": [url_last_a, url_first_b],
        "video_urls": [url_a, url_b],
        "resolution": "720p",
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "generate_audio": True,
    }
    data = await _fal_queue_run(FAL_QUEUE_URL, body, "Seedance transition")
    video_url = (data.get("video") or {}).get("url", "")
    if not video_url:
        raise RuntimeError("Seedance completed but returned no video URL")
    await _download_to(video_url, target_path)


async def _fal_veo_flf_transition(model: str, video_a: str, video_b: str, style: str,
                                  duration: int, aspect_ratio: str, target_path: str) -> None:
    """Veo first/last-frame bridge via fal's queue: extract the junction frames
    (clip A's last, clip B's first), upload them, and animate between them.
    Frame-anchored by construction, so the joins are guaranteed to match —
    the seamless counterpart to Seedance's creative (sometimes-cutting) mode."""
    last_a = await _extract_frame(video_a, "last")
    first_b = await _extract_frame(video_b, "first")
    url_last_a = await _fal_upload(last_a, "a_last.png", "image/png")
    url_first_b = await _fal_upload(first_b, "b_first.png", "image/png")
    prompt = TRANSITION_BASE_PROMPT + (f" {style}" if style else "")
    body = {
        "prompt": prompt,
        "first_frame_url": url_last_a,
        "last_frame_url": url_first_b,
        "duration": f"{duration}s",
        # 1080p costs the same as 720p on fal's Veo endpoints, and the extra
        # sharpness is what keeps a hard cut into native footage from popping
        # (live finding 2026-07-20: a 720p bridge upscaled into a 1080p
        # timeline reads as a visible quality jump at the join).
        "resolution": "1080p",
        "aspect_ratio": aspect_ratio,
        "generate_audio": True,
    }
    data = await _fal_queue_run(FAL_VEO_FLF_URLS[model], body, "Veo transition (fal)")
    video_url = (data.get("video") or {}).get("url", "")
    if not video_url:
        raise RuntimeError("fal Veo transition completed but returned no video URL")
    await _download_to(video_url, target_path)


# ---------------------------------------------------------------------------
# Frame extraction (bundled static ffmpeg — no system install)
# ---------------------------------------------------------------------------

async def _run_ffmpeg(args: list[str]) -> int:
    """Run the bundled ffmpeg with the given args; returns the exit code.
    imageio-ffmpeg ships the binary inside its wheel, so this works on local
    sandboxes and remote satellites alike (and there is deliberately no
    ffprobe — it isn't bundled)."""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    proc = await asyncio.create_subprocess_exec(
        exe, "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg timed out extracting a frame")


async def _extract_frame(video_path: str, position: str) -> bytes:
    """Extract the first or last frame of a video as PNG bytes. Frames go to
    an OS temp dir — never the workspace, temp files must not hit file sync."""
    tmpdir = tempfile.mkdtemp(prefix="video-gen-frame-")
    out = os.path.join(tmpdir, "frame.png")
    try:
        if position == "first":
            await _run_ffmpeg(["-i", video_path, "-frames:v", "1", "-y", out])
        else:
            # Decode only the final window and keep overwriting one image —
            # the survivor is the last frame. Sub-second clips can yield
            # nothing in the window; fall back to a full decode.
            await _run_ffmpeg(["-sseof", "-0.5", "-i", video_path, "-update", "1", "-y", out])
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                await _run_ffmpeg(["-i", video_path, "-update", "1", "-y", out])
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError(
                f"could not extract the {position} frame of {os.path.basename(video_path)} — "
                "is it a valid video file?"
            )
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

TRANSITION_BASE_PROMPT = (
    "A seamless, impressive cinematic transition from the first scene to the "
    "second scene, with smooth continuous motion."
)
# Seedance is natively multi-shot and will happily insert a hard cut unless
# the prompt forbids it outright (live finding 2026-07-19: an anchored-but-
# unconstrained brief produced "2s of scene A, CUT, 3s of scene B").
SEEDANCE_TRANSITION_TEMPLATE = (
    "One single continuous camera shot — absolutely no cuts, no editing, no "
    "scene jumps anywhere in the clip. The shot starts exactly on the framing "
    "of @Image1 (where @Video1 ends) and, in one unbroken cinematic camera "
    "move, travels and morphs until it ends exactly on the framing of "
    "@Image2 (where @Video2 begins), matching its zoom level and composition "
    "precisely."
)


async def _handle_generate(args: dict) -> list[TextContent]:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return [TextContent(type="text", text="Error: prompt is required.")]

    model = args.get("model", "veo-3.1-fast")
    if model not in ("omni-flash", "veo-3.1", "veo-3.1-fast"):
        return [TextContent(type="text", text=f"Error: unknown model: {model}")]
    try:
        duration = _coerce_int(args.get("duration_seconds"), "duration_seconds")
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    aspect_ratio = args.get("aspect_ratio", "16:9")
    resolution = args.get("resolution", "720p")
    image_path = (args.get("image_path") or "").strip()

    # Explicit range errors instead of silent clamping — a paid call should
    # produce what the agent asked for or fail loudly.
    if model.startswith("veo") and duration not in VEO_DURATIONS:
        return [TextContent(type="text", text=(
            f"Error: duration_seconds must be 4, 6, or 8 for Veo models (got {duration})."
        ))]
    if model == "omni-flash" and resolution != "720p":
        return [TextContent(type="text", text=(
            "Error: omni-flash generates 720p only — use veo-3.1 or "
            "veo-3.1-fast for 1080p/4k."
        ))]
    if image_path and not os.path.isfile(image_path):
        return [TextContent(type="text", text=f"Error: file not found: {image_path}")]
    if not GOOGLE_AI_API_KEY:
        return [TextContent(type="text", text=_unavailable_msg("Google AI", "GOOGLE_AI_API_KEY"))]

    image = None
    if image_path:
        with open(image_path, "rb") as f:
            image = (_image_mime(image_path), f.read())
    label = MODEL_LABELS[model]
    path = _get_save_path(args.get("save_path"), "video")
    try:
        if model == "omni-flash":
            # Omni has no duration parameter — steer via the prompt.
            omni_prompt = f"{prompt} Aim for a duration of about {duration} seconds."
            await _omni_generate(omni_prompt, aspect_ratio, path, image=image)
        else:
            await _veo_generate(model, prompt, duration, aspect_ratio, resolution,
                                path, first_frame=image)
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=(
            f"Error: {_api_error_message('Google AI', 'GOOGLE_AI_API_KEY', e)}"
        ))]
    except Exception as e:
        logger.exception("Video generation failed: %s", e)
        return [TextContent(type="text", text=f"Error generating video: {e}")]

    played = await _push_video_player(path, f"{label}: {prompt[:80]}")
    suffix = " Video player displayed to user." if played else ""
    duration_note = f"{duration}s" if model.startswith("veo") else f"~{duration}s (model-chosen)"
    return [TextContent(type="text", text=(
        f"Generated {duration_note} video with {label} ({resolution}, {aspect_ratio}"
        f"{', from image' if image else ''}). Saved to: {path}.{suffix}"
    ))]


async def _handle_transition(args: dict) -> list[TextContent]:
    video_a = (args.get("video_a_path") or "").strip()
    video_b = (args.get("video_b_path") or "").strip()
    if not video_a or not video_b:
        return [TextContent(type="text", text="Error: video_a_path and video_b_path are required.")]
    for p in (video_a, video_b):
        if not os.path.isfile(p):
            return [TextContent(type="text", text=f"Error: file not found: {p}")]

    model = args.get("model", "seedance")
    if model not in ("veo-3.1", "veo-3.1-fast", "seedance"):
        return [TextContent(type="text", text=f"Error: unknown model: {model}")]
    try:
        duration = _coerce_int(args.get("duration_seconds"), "duration_seconds")
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    style = (args.get("prompt") or "").strip()
    aspect_ratio = args.get("aspect_ratio", "16:9")

    if model == "seedance":
        if duration < SEEDANCE_MIN_SECONDS or duration > SEEDANCE_MAX_SECONDS:
            return [TextContent(type="text", text=(
                f"Error: duration_seconds must be between {SEEDANCE_MIN_SECONDS} and "
                f"{SEEDANCE_MAX_SECONDS} for seedance (got {duration})."
            ))]
        if not FAL_API_KEY:
            return [TextContent(type="text", text=_unavailable_msg(
                "Seedance premium transitions (fal.ai)", "FAL_API_KEY"))]
        combined = os.path.getsize(video_a) + os.path.getsize(video_b)
        if combined > SEEDANCE_MAX_COMBINED_BYTES:
            return [TextContent(type="text", text=(
                f"Error: seedance accepts at most 50 MB of input video combined "
                f"(got {combined / 1024 / 1024:.0f} MB). Trim or compress the clips first "
                "(video-tools can do this)."
            ))]
    else:
        if duration not in VEO_DURATIONS:
            return [TextContent(type="text", text=(
                f"Error: duration_seconds must be 4, 6, or 8 for Veo models (got {duration})."
            ))]
        if not FAL_API_KEY and not GOOGLE_AI_API_KEY:
            return [TextContent(type="text", text=_unavailable_msg(
                "Veo transitions (fal.ai or Google AI)", "FAL_API_KEY or GOOGLE_AI_API_KEY"))]

    # Veo transitions prefer the fal route: same models, no regional FLF gate,
    # billed on the FAL key. Google direct is the keyless-fal fallback (works
    # only where Google allows FLF — not for EEA keys, live 2026-07-19).
    via_fal = model == "seedance" or bool(FAL_API_KEY)
    label = MODEL_LABELS[model]
    path = _get_save_path(args.get("save_path"), "transition")
    try:
        if model == "seedance":
            await _seedance_transition(video_a, video_b, style, duration, aspect_ratio, path)
        elif via_fal:
            await _fal_veo_flf_transition(
                model, video_a, video_b, style, duration, aspect_ratio, path,
            )
        else:
            last_a = await _extract_frame(video_a, "last")
            first_b = await _extract_frame(video_b, "first")
            prompt = TRANSITION_BASE_PROMPT + (f" {style}" if style else "")
            await _veo_generate(
                model, prompt, duration, aspect_ratio, "720p", path,
                first_frame=("image/png", last_a), last_frame=("image/png", first_b),
            )
    except httpx.HTTPStatusError as e:
        provider = ("fal.ai", "FAL_API_KEY") if via_fal else ("Google AI", "GOOGLE_AI_API_KEY")
        return [TextContent(type="text", text=f"Error: {_api_error_message(*provider, e)}")]
    except Exception as e:
        logger.exception("Transition generation failed: %s", e)
        return [TextContent(type="text", text=f"Error generating transition: {e}")]

    route = " via fal.ai" if via_fal and model != "seedance" else ""
    played = await _push_video_player(path, f"Transition ({label})")
    suffix = " Video player displayed to user." if played else ""
    return [TextContent(type="text", text=(
        f"Generated {duration}s transition with {label}{route}. Saved to: {path}.{suffix} "
        "This is the bridge clip only — assemble first clip + bridge + second "
        "clip with video-tools for the final sequence."
    ))]


async def _handle_edit(args: dict) -> list[TextContent]:
    video_path = (args.get("video_path") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not video_path or not prompt:
        return [TextContent(type="text", text="Error: video_path and prompt are required.")]
    if not os.path.isfile(video_path):
        return [TextContent(type="text", text=f"Error: file not found: {video_path}")]
    if not RUNWAY_API_KEY:
        return [TextContent(type="text", text=_unavailable_msg("Runway", "RUNWAY_API_KEY"))]
    size = os.path.getsize(video_path)
    if size > RUNWAY_MAX_UPLOAD_BYTES:
        return [TextContent(type="text", text=(
            f"Error: Runway accepts uploads up to 200 MB (got {size / 1024 / 1024:.0f} MB). "
            "Trim or compress the clip first (video-tools can do this)."
        ))]

    path = _get_save_path(args.get("save_path"), "edit")
    try:
        await _runway_edit(video_path, prompt, path)
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=(
            f"Error: {_api_error_message('Runway', 'RUNWAY_API_KEY', e)}"
        ))]
    except Exception as e:
        logger.exception("Video edit failed: %s", e)
        return [TextContent(type="text", text=f"Error editing video: {e}")]

    played = await _push_video_player(path, f"Aleph 2.0 edit: {prompt[:80]}")
    suffix = " Video player displayed to user." if played else ""
    return [TextContent(type="text", text=(
        f"Edited video with Runway Aleph 2.0. Saved to: {path}.{suffix}"
    ))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
