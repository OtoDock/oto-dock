"""Image Generation MCP Server — AI image generation via Nano Banana Pro and GPT Image 1.5.

Stdio transport. Generates images from text prompts, saves full-res to disk,
pushes resized preview inline to dashboard. Cost is now declared in the
manifest's `costs` block and evaluated by the proxy at TOOL_RESULT time —
this server no longer reports cost.
"""

import base64
import contextlib
import io
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
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY", "")
OPENAI_IMAGE_API_KEY = os.environ.get("OPENAI_IMAGE_API_KEY", "")
IMAGE_SAVE_DIR = os.environ.get("IMAGE_SAVE_DIR", "")

MAX_PREVIEW_DIM = 1568  # Max dimension for inline preview (matches display-mcp)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("image-gen")

# Image generation is slow — allow a long read timeout.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

# Vendor REST bases (BYO mode). Hosted mode routes to {OTODOCK_RELAY_BASE}/{vendor}.
_VENDOR_BASE = {
    "google-ai": "https://generativelanguage.googleapis.com",
    "openai-image": "https://api.openai.com",
}


def _hosted() -> bool:
    """Hosted (OtoDock relay) mode: the framework injected the relay base + a
    per-user token instead of vendor keys. Read fresh for test-friendliness."""
    return bool(
        os.environ.get("OTODOCK_RELAY_BASE") and os.environ.get("OTODOCK_RELAY_TOKEN")
    )


def _relay_error() -> str:
    """When the platform routes this MCP through the relay but the relay isn't
    available, the framework injects OTODOCK_RELAY_ERROR instead of keys/token —
    surface it verbatim so the user sees a clear message."""
    return os.environ.get("OTODOCK_RELAY_ERROR", "")


def _vendor_available(byo_key: str) -> bool:
    """A provider is callable if we're hosted (the relay supplies the key) or a
    BYO key is configured for it."""
    return _hosted() or bool(byo_key)


def _vendor_request_args(vendor: str, path: str) -> tuple[str, dict, dict]:
    """Build (url, headers, params) for a POST to ``vendor`` + ``path``.

    Hosted: route through the OtoDock relay — ``{OTODOCK_RELAY_BASE}/{vendor}{path}``
    with a per-user Bearer token; the relay injects the real vendor key, meters the
    call, and proxies. BYO: hit the vendor directly with the configured key (Gemini
    via ``?key=``, OpenAI via ``Authorization: Bearer``). The hosted branch is
    identical for every vendor — only the BYO auth differs. ``OTODOCK_RELAY_BASE``
    already includes the manifest ``relay_path`` (``/v1/relay``)."""
    headers: dict = {}
    params: dict = {}
    if _hosted():
        base = os.environ["OTODOCK_RELAY_BASE"].rstrip("/")
        headers["Authorization"] = f"Bearer {os.environ['OTODOCK_RELAY_TOKEN']}"
        return f"{base}/{vendor}{path}", headers, params
    base = _VENDOR_BASE[vendor]
    if vendor == "google-ai":
        params["key"] = GOOGLE_AI_API_KEY
    elif vendor == "openai-image":
        headers["Authorization"] = f"Bearer {OPENAI_IMAGE_API_KEY}"
    return f"{base}{path}", headers, params


def _unavailable_msg(provider_label: str, key_name: str) -> str:
    err = _relay_error()
    if err:
        return f"Error: {err}"
    return (
        f"Error: {provider_label} not configured. Ask an admin to set {key_name} "
        f"in the image-gen-mcp admin page."
    )


def _extract_gemini_image(data: dict) -> bytes | None:
    """Pull the first inline image out of a Gemini generateContent response
    (REST JSON uses camelCase ``inlineData``; tolerate snake_case too)."""
    for cand in data.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts") or []):
            inline = part.get("inlineData") or part.get("inline_data") or {}
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            b64 = inline.get("data")
            if b64 and mime.startswith("image/"):
                return base64.b64decode(b64)
    return None


OPENAI_SIZE_MAP = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:3": "1536x1024",
    "3:4": "1024x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "21:9": "1536x1024",
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("image-gen")


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        Tool(
            name="generate_image",
            description=(
                "Generate an AI image from a text description. "
                "The image is saved to disk and displayed to the user in the chat.\n\n"
                "Default model: Nano Banana Pro (Google) — best for photorealism, "
                "product shots, text in images.\n"
                "Use model='gpt-image' for text-heavy images, iterative editing, or creative work.\n\n"
                "By default the image is saved under the workspace's `generated-assets/` "
                "subfolder with an auto-generated filename. Pass `save_path` (relative or "
                "absolute under the workspace) to save into a specific subfolder."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed text description of the image to generate.",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["nano-banana", "gpt-image"],
                        "default": "nano-banana",
                        "description": "nano-banana (default, photorealism) or gpt-image (text-heavy/creative).",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"],
                        "default": "1:1",
                        "description": "Aspect ratio. 16:9 for presentations, 9:16 for stories, 1:1 for avatars.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["standard", "high"],
                        "default": "standard",
                        "description": "standard or high (more detailed, costs more).",
                    },
                    "style": {
                        "type": "string",
                        "description": "Optional style (e.g., 'watercolor', 'photorealistic', 'flat illustration').",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Optional save location. Default: workspace's "
                            "`generated-assets/` subfolder with an auto-generated filename. "
                            "Pass a relative path (e.g. `\"projects/2026/icon.png\"`) "
                            "to save into a specific subfolder under the workspace — "
                            "the subfolder is created if missing. Absolute sandbox-style "
                            "paths under the workspace also work (e.g. "
                            "`\"/users/{u}/workspace/portraits/sarah.png\"`). Paths "
                            "outside the workspace are re-anchored to "
                            "`generated-assets/<basename>` for safety."
                        ),
                    },
                    "num_images": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "default": 1,
                        "description": "Number of images to generate (1-4).",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="edit_image_ai",
            description=(
                "Edit an existing image using AI. Provide the image and edit instructions. "
                "The edited image is saved and displayed to the user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the source image.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Edit instruction (e.g., 'make the background a beach sunset').",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["nano-banana", "gpt-image"],
                        "default": "nano-banana",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["standard", "high"],
                        "default": "standard",
                        "description": (
                            "GPT Image only: 'high' for sharper, more detailed edits "
                            "(higher cost); 'standard' (medium) is the default."
                        ),
                    },
                    "mask_path": {
                        "type": "string",
                        "description": "Optional mask image (white=edit, black=keep). GPT Image only.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Optional save location. Default: workspace's "
                            "`generated-assets/` subfolder. Same conventions as "
                            "`generate_image.save_path` — relative paths join under "
                            "the workspace; absolute sandbox paths under the workspace "
                            "are kept; out-of-workspace paths are re-anchored to "
                            "`generated-assets/<basename>`."
                        ),
                    },
                },
                "required": ["image_path", "prompt"],
            },
        ),
    ]
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "generate_image":
        return await _handle_generate(arguments)
    elif name == "edit_image_ai":
        return await _handle_edit(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Proxy hooks
# ---------------------------------------------------------------------------

async def _hook_post(endpoint: str, payload: dict, *, read_timeout: float = 30.0,
                     attempts: int = 1) -> bool:
    """POST to proxy hook endpoint. Returns True on success, False on every-attempt
    failure (also logs each failure loudly). Non-raising — callers must check the
    bool to react to failures (e.g. clear a stuck UI placeholder).
    """
    if not PROXY_URL or not SESSION_ID:
        return False
    last_err: Exception | None = None
    for i in range(max(1, attempts)):
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
            last_err = e
            logger.warning(
                "Hook %s attempt %d/%d failed: %s",
                endpoint, i + 1, attempts, e,
            )
    logger.error("Hook %s gave up after %d attempt(s): %s", endpoint, attempts, last_err)
    return False


async def _push_generating(prompt: str, model: str) -> bool:
    return await _hook_post("/v1/hooks/image-generating", {
        "prompt_preview": prompt[:100],
        "model": model,
    })


async def _push_image_preview(image_bytes: bytes, mime: str, caption: str) -> bool:
    """Push resized preview inline to dashboard. Returns False if the preview
    couldn't be encoded OR the POST failed after retries — caller MUST clear
    the `image_generating` placeholder in that case (otherwise it sticks
    forever and gets persisted to chat history).
    """
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) > MAX_PREVIEW_DIM:
            img.thumbnail((MAX_PREVIEW_DIM, MAX_PREVIEW_DIM), Image.LANCZOS)
        # JPEG can't encode an alpha channel; gpt-image-1 returns transparent PNGs.
        # Flatten onto white before encoding.
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.exception("Preview encode failed: %s", e)
        return False
    # Larger payloads (base64-encoded JPEGs at 1568px) can take longer than the
    # default 30s if the proxy is busy; retry once on failure before giving up.
    # Posts a 1-item gallery — the unified /v1/hooks/images endpoint renders
    # single images identically to the old /v1/hooks/image flow.
    return await _hook_post(
        "/v1/hooks/images",
        {"images": [{
            "image_data": b64,
            "mime_type": "image/jpeg",
            "caption": caption,
        }]},
        read_timeout=120.0,
        attempts=2,
    )


async def _push_file_download(file_path: str, filename: str):
    """Push a file download link to the dashboard."""
    await _hook_post("/v1/hooks/file", {
        "path": file_path,
        "filename": filename,
        "description": "Generated image (full resolution)",
    })


async def _push_failed():
    await _hook_post("/v1/hooks/image-gen-failed", {})


# ---------------------------------------------------------------------------
# Image saving
# ---------------------------------------------------------------------------

# Default subdirectory under workspace for auto-generated images. Keeps
# the workspace root tidy and gives the agent a predictable place to find
# AI-generated assets when the LLM didn't pick a save_path. Agents can
# override by passing any save_path under workspace.
DEFAULT_SUBDIR = "generated-assets"


def _get_save_path(save_path: str | None, ext: str = "png") -> str:
    """Determine save path, always anchored under IMAGE_SAVE_DIR (workspace).

    The LLM may pass:
      - ``None`` / empty → auto-generate
        ``IMAGE_SAVE_DIR/generated-assets/generated_<uuid>.<ext>`` (default
        subdir keeps workspace root tidy; agents can override).
      - a relative path → joined under ``IMAGE_SAVE_DIR`` (e.g.
        ``"projects/2026/icon.png"`` → ``<workspace>/projects/2026/icon.png``).
        Use this to save into existing or new subfolders the user already
        has organized.
      - an absolute path that's already under ``IMAGE_SAVE_DIR`` → used as-is
        (allows fully-qualified sandbox paths like
        ``"/users/{u}/workspace/foo.png"``).
      - an absolute path elsewhere → re-anchored to
        ``IMAGE_SAVE_DIR/<DEFAULT_SUBDIR>/<basename>`` so accidental escapes
        (e.g. the LLM trying to write to the parent user dir) still produce
        a usable file inside workspace.

    Cross-target consistency: ``IMAGE_SAVE_DIR`` is a sandbox-style virtual
    path on local (bwrap maps it to the host) and a satellite-absolute path
    on remote (the satellite's path_translator translates it before
    subprocess spawn). The MCP code is identical on both — file ops just
    use the resolved env value.

    ``IMAGE_SAVE_DIR`` is always injected by the platform via the manifest's
    ``path_env: {IMAGE_SAVE_DIR: workspace}`` declaration; if it's missing we
    fail loudly so the misconfiguration surfaces during dev.
    """
    if not IMAGE_SAVE_DIR:
        raise RuntimeError(
            "IMAGE_SAVE_DIR is not set. The image-gen-mcp manifest must "
            "declare `path_env: {\"IMAGE_SAVE_DIR\": {\"role\": \"workspace\"}}` "
            "and the platform must inject it. Check proxy/services/path_roles.py."
        )

    workspace = IMAGE_SAVE_DIR.rstrip("/")
    default_dir = os.path.join(workspace, DEFAULT_SUBDIR)

    if not save_path:
        # Default: auto-named file under generated-assets/ subdir.
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, f"generated_{uuid.uuid4().hex[:8]}.{ext}")

    # Normalize the LLM-supplied save_path.
    if os.path.isabs(save_path):
        # Absolute path — accept only if it's under IMAGE_SAVE_DIR. Otherwise
        # re-anchor under <workspace>/<DEFAULT_SUBDIR>/ using the basename.
        # This preserves the LLM's intent for the filename while keeping the
        # write inside workspace (prevents accidental writes to the parent
        # user dir, /config, /tmp, etc.).
        normalized = os.path.normpath(save_path)
        if normalized == workspace or normalized.startswith(workspace + os.sep):
            os.makedirs(os.path.dirname(normalized) or workspace, exist_ok=True)
            return normalized
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, os.path.basename(normalized))

    # Relative path — anchor under workspace. Strip parent-traversal segments
    # so a malicious or buggy "../foo.png" can't escape the workspace.
    normalized = os.path.normpath(save_path)
    if normalized.startswith(".." + os.sep) or normalized == "..":
        # Path tried to escape — drop into the default subdir using basename.
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(
            default_dir,
            os.path.basename(normalized) or f"image_{uuid.uuid4().hex[:8]}.{ext}",
        )

    # Bare filename (no directory component) → drop into default subdir to
    # keep workspace root tidy. The LLM's filename choice is preserved; only
    # the parent dir is supplied. To deliberately write at workspace root,
    # the LLM must pass an absolute sandbox path under workspace (form
    # `/users/{u}/workspace/<name>` — handled in the absolute-path branch
    # above) or a relative path with an explicit `./` prefix
    # (`./<name>` normalizes to `<name>`, so use the absolute form for that).
    if os.sep not in normalized and "/" not in save_path:
        os.makedirs(default_dir, exist_ok=True)
        return os.path.join(default_dir, normalized)

    # Relative path with explicit subdir component — honor verbatim.
    full = os.path.join(workspace, normalized)
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    return full


# ---------------------------------------------------------------------------
# Provider: Nano Banana Pro (Google AI)
# ---------------------------------------------------------------------------

_GEMINI_IMAGE_MODEL = "gemini-3-pro-image-preview"


async def _generate_nano_banana(prompt: str, aspect_ratio: str, quality: str, num_images: int) -> list[bytes]:
    """Generate images using Nano Banana Pro (Google Gemini image generation).

    Calls the Gemini REST ``generateContent`` endpoint directly via httpx so the
    request routes through the OtoDock relay in hosted mode (else BYO ``?key=``)."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }
    url, headers, params = _vendor_request_args(
        "google-ai", f"/v1beta/models/{_GEMINI_IMAGE_MODEL}:generateContent",
    )
    images: list[bytes] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for _ in range(num_images):
            resp = await client.post(url, params=params, headers=headers, json=body)
            resp.raise_for_status()
            img = _extract_gemini_image(resp.json())
            if img:
                images.append(img)
    if not images:
        raise RuntimeError("Nano Banana Pro returned no images")
    return images


async def _edit_nano_banana(image_path: str, prompt: str) -> bytes:
    """Edit an image using Nano Banana Pro (Gemini REST via httpx)."""
    with open(image_path, "rb") as f:
        image_data = f.read()

    mime = "image/png"
    if image_path.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif image_path.lower().endswith(".webp"):
        mime = "image/webp"

    body = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": mime,
                            "data": base64.b64encode(image_data).decode()}},
            {"text": prompt},
        ]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url, headers, params = _vendor_request_args(
        "google-ai", f"/v1beta/models/{_GEMINI_IMAGE_MODEL}:generateContent",
    )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, params=params, headers=headers, json=body)
        resp.raise_for_status()
        img = _extract_gemini_image(resp.json())
    if img is None:
        raise RuntimeError("Nano Banana Pro returned no edited image")
    return img


# ---------------------------------------------------------------------------
# Provider: GPT Image 1.5 (OpenAI)
# ---------------------------------------------------------------------------

async def _generate_openai(prompt: str, aspect_ratio: str, quality: str, num_images: int) -> list[bytes]:
    """Generate images using OpenAI GPT Image (REST /v1/images/generations via httpx)."""
    size = OPENAI_SIZE_MAP.get(aspect_ratio, "1024x1024")
    q = "high" if quality == "high" else "medium"
    body = {
        "model": "gpt-image-1", "prompt": prompt,
        "n": num_images, "size": size, "quality": q,
    }
    url, headers, params = _vendor_request_args("openai-image", "/v1/images/generations")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, params=params, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    images = [
        base64.b64decode(item["b64_json"])
        for item in (data.get("data") or []) if item.get("b64_json")
    ]
    if not images:
        raise RuntimeError("OpenAI returned no images")
    return images


async def _edit_openai(image_path: str, prompt: str, mask_path: str | None,
                       quality: str = "standard") -> bytes:
    """Edit an image using OpenAI GPT Image (REST /v1/images/edits multipart via httpx).

    Multipart passes through the relay intact (the relay forwards the raw body +
    Content-Type and injects the vendor key)."""
    url, headers, params = _vendor_request_args("openai-image", "/v1/images/edits")
    # Send an explicit quality + size so the hosted relay prices the edit
    # deterministically (its variant matrix) instead of the safe-high fallback.
    # 'high' → sharper/pricier; anything else → 'medium' (the default). Mirrors
    # _generate_openai's quality mapping.
    q = "high" if quality == "high" else "medium"
    form = {"model": "gpt-image-1", "prompt": prompt, "n": "1",
            "size": "1024x1024", "quality": q}
    # Handles enter the stack as they open, so a failure between open and
    # send can't leak them past the with.
    with contextlib.ExitStack() as stack:
        files = {
            "image": (os.path.basename(image_path),
                      stack.enter_context(open(image_path, "rb"))),
        }
        if mask_path and os.path.isfile(mask_path):
            files["mask"] = (os.path.basename(mask_path),
                             stack.enter_context(open(mask_path, "rb")))
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                url, params=params, headers=headers, data=form, files=files,
            )
            resp.raise_for_status()
            data = resp.json()
    items = data.get("data") or []
    if items and items[0].get("b64_json"):
        return base64.b64decode(items[0]["b64_json"])
    raise RuntimeError("OpenAI returned no edited image")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _handle_generate(args: dict) -> list[TextContent]:
    prompt = args.get("prompt", "").strip()
    if not prompt:
        return [TextContent(type="text", text="Error: prompt is required.")]

    model = args.get("model", "nano-banana")
    aspect_ratio = args.get("aspect_ratio", "1:1")
    quality = args.get("quality", "standard")
    style = args.get("style", "")
    save_path = args.get("save_path")
    num_images = min(max(int(args.get("num_images", 1)), 1), 4)

    if style:
        full_prompt = f"{prompt}. Style: {style}"
    else:
        full_prompt = prompt

    model_label = "Nano Banana Pro" if model == "nano-banana" else "GPT Image 1.5"

    # Check provider availability (hosted relay supplies the key, else BYO).
    if model == "nano-banana" and not _vendor_available(GOOGLE_AI_API_KEY):
        return [TextContent(type="text", text=_unavailable_msg("Google AI (Nano Banana)", "GOOGLE_AI_API_KEY"))]
    if model == "gpt-image" and not _vendor_available(OPENAI_IMAGE_API_KEY):
        return [TextContent(type="text", text=_unavailable_msg("OpenAI Image", "OPENAI_IMAGE_API_KEY"))]

    # Show skeleton placeholder
    await _push_generating(prompt, model)

    try:
        # Generate
        if model == "nano-banana":
            image_list = await _generate_nano_banana(full_prompt, aspect_ratio, quality, num_images)
        else:
            image_list = await _generate_openai(full_prompt, aspect_ratio, quality, num_images)

        # Save and display each image
        saved_paths = []
        for i, img_bytes in enumerate(image_list):
            suffix = f"_{i+1}" if len(image_list) > 1 else ""
            if save_path and len(image_list) == 1:
                path = _get_save_path(save_path)
            else:
                base = save_path or None
                if base and len(image_list) > 1:
                    name, ext = os.path.splitext(base)
                    path = _get_save_path(f"{name}{suffix}{ext}")
                else:
                    path = _get_save_path(None)

            with open(path, "wb") as f:
                f.write(img_bytes)
            saved_paths.append(path)

            caption = f"{model_label}: {prompt[:80]}"
            preview_ok = await _push_image_preview(img_bytes, "image/png", caption)
            if not preview_ok:
                # Clear the stuck `image_generating` placeholder so the chat
                # doesn't show a perpetual spinner. The full-res file is still
                # pushed below — user can click to view it.
                await _push_failed()
                logger.warning(
                    "Inline preview push failed for image %d (%s); "
                    "placeholder cleared, file still attached.", i + 1, path,
                )
            await _push_file_download(path, os.path.basename(path))

        paths_str = ", ".join(saved_paths)
        return [TextContent(
            type="text",
            text=(
                f"Generated {len(image_list)} image(s) with {model_label}. "
                f"Displayed to user. Saved to: {paths_str}"
            ),
        )]

    except Exception as e:
        await _push_failed()
        logger.exception(f"Image generation failed: {e}")
        return [TextContent(type="text", text=f"Error generating image: {e}")]


async def _handle_edit(args: dict) -> list[TextContent]:
    image_path = args.get("image_path", "").strip()
    prompt = args.get("prompt", "").strip()
    if not image_path or not prompt:
        return [TextContent(type="text", text="Error: image_path and prompt are required.")]
    if not os.path.isfile(image_path):
        return [TextContent(type="text", text=f"Error: file not found: {image_path}")]

    model = args.get("model", "nano-banana")
    mask_path = args.get("mask_path")
    save_path = args.get("save_path")
    quality = args.get("quality", "standard")

    model_label = "Nano Banana Pro" if model == "nano-banana" else "GPT Image 1.5"

    if model == "nano-banana" and not _vendor_available(GOOGLE_AI_API_KEY):
        return [TextContent(type="text", text=_unavailable_msg("Google AI (Nano Banana)", "GOOGLE_AI_API_KEY"))]
    if model == "gpt-image" and not _vendor_available(OPENAI_IMAGE_API_KEY):
        return [TextContent(type="text", text=_unavailable_msg("OpenAI Image", "OPENAI_IMAGE_API_KEY"))]

    await _push_generating(prompt, model)

    try:
        if model == "nano-banana":
            img_bytes = await _edit_nano_banana(image_path, prompt)
        else:
            img_bytes = await _edit_openai(image_path, prompt, mask_path, quality)

        path = _get_save_path(save_path)
        with open(path, "wb") as f:
            f.write(img_bytes)

        caption = f"{model_label} edit: {prompt[:80]}"
        preview_ok = await _push_image_preview(img_bytes, "image/png", caption)
        if not preview_ok:
            await _push_failed()
            logger.warning(
                "Inline preview push failed for edit (%s); placeholder cleared, "
                "file still attached.", path,
            )
        await _push_file_download(path, os.path.basename(path))

        return [TextContent(
            type="text",
            text=f"Edited image with {model_label}. Displayed to user. Saved to: {path}",
        )]

    except Exception as e:
        await _push_failed()
        logger.exception(f"Image edit failed: {e}")
        return [TextContent(type="text", text=f"Error editing image: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
