"""MCP server for displaying images, media, links, files, and UI artifacts
in the user's chat.

Tools:
  - display_images: Renders 1-N images inline in chat as a gallery. Each
    image can be an external URL (passed through — browser fetches from CDN)
    or a local file path (loaded, resized, base64-encoded by this server).
    Per-image caption, attribution, optional clickable link_url for source/
    product pages. Dashboard renders 1 as a single card, 2-3 as a row, 4+
    as a horizontal scroll-snap carousel.
  - display_video / display_audio: Inline players; the proxy resolves the
    source, serves it with Range support, and transcodes as needed.
  - display_ui: Renders an agent-authored HTML artifact (chart/table/
    calculator/animation) in a sandboxed, theme-matched, auto-sized frame.
  - send_url: Sends a clickable link card to the chat.
  - send_file: Sends a file as a downloadable link in the chat.
  - the app and Dock tools (pin_app, app_push, app_state, open_app,
    rollback_app, pin_file, …) live in app_tools.py and dispatch through
    its HANDLERS table.

The proxy routes display events to the appropriate client adapter (dashboard, phone, etc.).
"""

import base64
import io
import logging
import os

import httpx
from PIL import Image, ImageOps
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("display-mcp")

# HEIC/HEIF decode (iPhone photos default to HEIC). Registering the opener
# teaches Pillow's format registry to recognize HEIC — the existing decode
# path then handles it like any other raster format. Import is graceful so the
# server still starts (without HEIC) if the wheel is missing from the venv, and
# so the proxy test suite can import this module without the dep.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIC_AVAILABLE = True
except ImportError:
    _HEIC_AVAILABLE = False
    logger.warning("pillow-heif not installed; HEIC/HEIF images won't decode")

PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SESSION_ID = os.environ.get("OTO_SESSION_ID", "")

from app_tools import (  # noqa: E402 — the app and Dock tools
    MAX_UI_HTML_BYTES,
    _artifact_read_candidates,  # noqa: F401 — the tests reach it through this module
    _read_artifact_file,
)
import app_tools  # noqa: E402


# Max image dimension (matches Claude's limit)
MAX_IMAGE_DIM = 1568

# Cap on raw bytes we'll base64-passthrough without decoding (SVG, animated
# GIF/WebP). ~10MB raw → ~13MB base64. Oversized animations fall back to a
# static first frame via Pillow; oversized SVGs are rejected.
MAX_PASSTHROUGH_BYTES = 10 * 1024 * 1024

# Hard cap on a local file we'll read into memory at all. Matches the
# framework's single-blob file-sync limit. Above this we refuse rather than
# risk OOMing the subprocess on a multi-GB file before Pillow's decompression
# guard ever sees it.
MAX_SOURCE_BYTES = 50 * 1024 * 1024

server = Server("display")


def _is_svg(data: bytes) -> bool:
    """True if the bytes look like an SVG document.

    SVG is text/XML and renders natively in an <img> data URL, so it should
    pass through untouched — rasterizing via Pillow would throw away the
    vector's scalability. Sniffs the leading non-whitespace bytes for an XML
    prolog or a literal <svg root element (a UTF-8 BOM, if present, is
    skipped first).
    """
    head = data[:1024]
    if head[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM
        head = head[3:]
    head = head.lstrip()
    if not head:
        return False
    if head[:5] == b"<?xml":
        # The root <svg> element follows the XML prolog (possibly after a
        # DOCTYPE / comment) — scan the rest of the head window for it.
        return b"<svg" in head.lower()
    return head[:4].lower() == b"<svg"


def _resize_and_encode(img_bytes: bytes) -> tuple[str, str]:
    """Return (base64_data, mime_type) for an image to render inline in chat.

    Three strategies, in priority order:

    1. **SVG passthrough** — base64 the raw bytes as ``image/svg+xml``. No
       decode; the browser renders the vector directly.
    2. **Animated GIF/WebP passthrough** — base64 the raw bytes with the
       original mime so ``<img>`` auto-plays the animation. Capped at
       ``MAX_PASSTHROUGH_BYTES``; oversized inputs fall through to (3), which
       flattens them to a static first frame.
    3. **Pillow decode + re-encode** — everything else (JPEG, PNG, HEIC, BMP,
       TIFF, ICO, static/oversized WebP, multi-page TIFF, oversized GIF). EXIF
       orientation is normalized, the image is downscaled to ``MAX_IMAGE_DIM``,
       and saved as PNG when an alpha channel / palette transparency is present
       (else JPEG for size). Without the alpha branch a transparent-bg logo
       gets a black or white plate baked in by JPEG's RGB-only color model.
    """
    if _is_svg(img_bytes):
        if len(img_bytes) > MAX_PASSTHROUGH_BYTES:
            raise ValueError(
                f"SVG too large to display "
                f"({len(img_bytes) // (1024 * 1024)}MB; "
                f"limit {MAX_PASSTHROUGH_BYTES // (1024 * 1024)}MB)"
            )
        return base64.b64encode(img_bytes).decode(), "image/svg+xml"

    with Image.open(io.BytesIO(img_bytes)) as img:
        fmt = (img.format or "").upper()

        # Animated GIF / WebP: pass the original bytes through so the browser
        # plays the animation. The resize + single-frame save below would
        # otherwise flatten it. Oversized animations fall through to that
        # static path rather than blowing up the chat payload.
        if (
            getattr(img, "is_animated", False)
            and fmt in ("GIF", "WEBP")
            and len(img_bytes) <= MAX_PASSTHROUGH_BYTES
        ):
            mime = "image/gif" if fmt == "GIF" else "image/webp"
            return base64.b64encode(img_bytes).decode(), mime

        # Apply EXIF orientation BEFORE measuring/resizing. iPhone photos
        # (JPEG + HEIC) record rotation as a tag instead of rotating pixels;
        # without this they render sideways. In-place so the opened image is
        # the one the `with` block closes (no orphan copy).
        ImageOps.exif_transpose(img, in_place=True)

        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIM:
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
            logger.info(f"Resized image from {w}x{h} to {img.size[0]}x{img.size[1]}")

        has_alpha = (
            img.mode in ("RGBA", "LA")
            or img.info.get("transparency") is not None
        )

        buf = io.BytesIO()
        if has_alpha:
            if img.mode == "P":
                img = img.convert("RGBA")
            img.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode(), "image/png"

        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"



@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="display_images",
            description=(
                "Render 1-N images inline in the user's chat as a gallery. "
                "Each image can be an external URL (passed straight through — the "
                "browser fetches it from the source CDN) or a local file path "
                "(this server loads, resizes, and base64-encodes it). "
                "The dashboard auto-adapts the layout: 1 image renders as a single "
                "card, 2-3 as a row, 4+ as a horizontal swipe-able carousel. "
                "Pass `link_url` on a gallery item to make the card clickable — "
                "ideal for image-search-mcp reverse-search results where each "
                "card should open the source / retailer page in a new tab."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "images": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "List of images to render together as one gallery "
                            "block. 1 item = single image, 2-3 items = side-by-side "
                            "row, 4+ items = swipe-able carousel."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {
                                    "type": "string",
                                    "description": (
                                        "Image URL OR local file path. URLs are "
                                        "passed through and fetched by the USER'S "
                                        "browser — never pass authenticated or "
                                        "private/LAN URLs (192.168.*, camera "
                                        "snapshots): download those to the "
                                        "workspace first and pass the local path. "
                                        "Local paths are loaded + resized + "
                                        "base64-encoded by this server."
                                    ),
                                },
                                "caption": {
                                    "type": "string",
                                    "description": "Optional caption shown under this card.",
                                },
                                "attribution": {
                                    "type": "string",
                                    "description": (
                                        "Optional smaller line below caption — "
                                        "e.g. 'Photo by Jane Doe (Unsplash)' or "
                                        "'$45 — H&M'."
                                    ),
                                },
                                "link_url": {
                                    "type": "string",
                                    "description": (
                                        "Optional. If set, an external-link icon "
                                        "appears on the card; clicking opens this "
                                        "URL in a new tab. Use for reverse-search "
                                        "product pages or find_images source pages."
                                    ),
                                },
                                "download_url": {
                                    "type": "string",
                                    "description": (
                                        "Optional. Overrides what the per-image "
                                        "download button fetches. Defaults to the "
                                        "image URL itself."
                                    ),
                                },
                            },
                            "required": ["source"],
                        },
                    },
                },
                "required": ["images"],
            },
        ),
        Tool(
            name="send_file",
            description=(
                "Send a file to the user as a downloadable link in the chat. "
                "Use this to share reports, data exports, generated files, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file on disk.",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Display name for the download (e.g. 'report.md'). "
                            "If omitted, uses the file's basename."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description shown alongside the download link.",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="send_url",
            description=(
                "Send a clickable link to the user's chat. "
                "Use this to share URLs that the user should open in their browser "
                "(e.g. Nextcloud share links, web pages, dashboards)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to display as a clickable link.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Link title displayed prominently.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description shown below the title.",
                    },
                },
                "required": ["url", "title"],
            },
        ),
        Tool(
            name="display_video",
            description=(
                "Play a video inline in the user's chat with a full player "
                "(play/pause, scrub, volume, fullscreen). `source` can be: a "
                "YouTube or Vimeo link (embedded inline — ALWAYS use this, NOT "
                "send_url, for video links), a direct web video URL (streamed "
                "from the origin), OR a local/remote file path (served with "
                "seeking; iPhone/HEVC and other non-web-native formats are "
                "converted automatically). One video per call. Prefer this over "
                "send_url/send_file for any video so the user can watch inline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Video to play: a YouTube/Vimeo link, a direct web "
                            "video URL, or a local/remote file path."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption shown under the player.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title shown above the player.",
                    },
                    "poster": {
                        "type": "string",
                        "description": (
                            "Optional poster image URL shown before playback "
                            "begins. Must be a web URL."
                        ),
                    },
                },
                "required": ["source"],
            },
        ),
        Tool(
            name="display_ui",
            description=(
                "Render an interactive HTML artifact inline in the user's chat — "
                "a live chart, rich data table, stat dashboard, calculator, "
                "timeline, animation, or any custom visual. The content renders "
                "borderless in a sandboxed frame, auto-sized and theme-matched "
                "(light/dark) to the dashboard. Write a BODY FRAGMENT (no "
                "<html>/<head> wrapper — a full document keeps its own head and "
                "loses only the token stylesheet; the platform runtime is "
                "injected either way). A design-token stylesheet is auto-loaded: CSS vars "
                "(--p-text, --p-primary, --p-surface, …) plus native-looking "
                "primitives — .card, styled <table> (th/td, .num for right-aligned "
                "numbers), .btn/.btn.primary, .stat (.value/.label), .badge "
                "(.success/.warn/.error), .row, .grid, .muted. Self-hosted "
                "libraries, added per artifact via script tag: "
                '<script src="/ui-kit/echarts.min.js"></script> (charts), '
                '<script src="/ui-kit/anime.min.js"></script> (animation — global '
                "`anime`, v4 API: anime.animate(...)), "
                '<script src="/ui-kit/tailwind.js"></script> (Tailwind v4 '
                "utilities, runtime-compiled — RECOMMENDED for any rich/custom "
                "layout; combine with the token vars via arbitrary values like "
                "bg-[var(--p-surface)]; dark: variants follow the theme), "
                '<script src="/ui-kit/three.min.js"></script> (3D — global '
                "`THREE` incl. addons: OrbitControls/MapControls, "
                "EffectComposer/RenderPass/UnrealBloomPass/OutputPass/"
                "ShaderPass (bloom glow — the signature look for living "
                "dashboards), Line2/LineGeometry/LineMaterial fat lines, "
                "RoundedBoxGeometry. Sandbox limits: NO asset loaders "
                "(GLTFLoader etc. — fetch is blocked), no workers/WASM "
                "decoders, no WebXR; use procedural geometry + data:/blob:/"
                "canvas textures, plain WebGL works fine — recipes in the "
                "app-authoring skill). "
                "Inline <script> is allowed and runs sandboxed: client-side "
                "interactivity (inputs, tabs, sliders, sorting, local "
                "calculations) fully works, but the artifact CANNOT reach "
                "external networks or platform APIs. Buttons can send an "
                "interaction back to YOU via window.otodock.send(payload) — it "
                "arrives as a framed chat input (user-gesture only, small JSON; "
                "see the skill for the ack contract). Plain <a href=\"https://…\"> "
                "links work: absolute http(s) links open in a new tab behind a "
                "one-time consent (relative/platform URLs never open — see the "
                "skill). Inline <svg> (including "
                "CSS/SMIL animation) works too. The HTML is saved as a "
                "workspace file (path returned in the ack). ITERATING is "
                "cheap: Edit that file directly, then call display_ui again "
                "with ONLY save_path (html omitted = the file's current "
                "content is re-displayed) — full html is only for the first "
                "creation or a rewrite. Same save_path + display=true "
                "re-shows the artifact at the newest chat position (the older "
                "copy collapses); display=false updates it silently in place "
                "(no new chat block) — how ONE standing artifact (a booking "
                "board, a running comparison) carries through a whole "
                "conversation. For a chart to embed in a DOCUMENT "
                "(docx/pdf/xlsx), use create_chart (file-tools) instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": (
                            "The artifact content — an HTML body fragment "
                            "(markup + optional <style>/<script>). Max 2MB. "
                            "OMIT after editing the saved file directly: the "
                            "file's current content at save_path is "
                            "re-displayed (the cheap update path)."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Short title — used for the accessible label, the "
                            "window title, and the saved filename slug."
                        ),
                    },
                    "height": {
                        "type": "integer",
                        "description": (
                            "Fixed height in px. Omit for automatic height "
                            "(recommended — the artifact resizes to fit)."
                        ),
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Where to save the artifact file: workspace-relative "
                            "('reports/chart.html') or sandbox-virtual "
                            "('/workspace/…', '/users/{u}/workspace/…'). Default: "
                            "generated-ui/<title-slug>-<uniq>.html in your "
                            "workspace. Reuse the path an earlier ack "
                            "returned (verbatim) to update that artifact; "
                            "with html omitted it must name the existing "
                            "file to re-display."
                        ),
                    },
                    "display": {
                        "type": "boolean",
                        "description": (
                            "false = save/update the file only, no new chat "
                            "block (already-rendered instances still "
                            "live-reload). Default true."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="display_audio",
            description=(
                "Play an audio file inline in the user's chat with a compact "
                "player (play/pause, scrub, volume). `source` can be a web URL OR "
                "a local/remote file path — served with seeking support; non-web "
                "formats are converted automatically. One audio file per call. "
                "Prefer this over send_file for audio so the user can listen inline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Audio URL or local/remote file path.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption shown under the player.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title (e.g. track name).",
                    },
                },
                "required": ["source"],
            },
        ),
        *app_tools.APP_TOOLS,
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
    if name == "display_images":
        return await _handle_display_images(arguments)
    elif name == "send_file":
        return await _handle_send_file(arguments)
    elif name == "send_url":
        return await _handle_send_url(arguments)
    elif name == "display_video":
        return await _handle_display_media(arguments, "video")
    elif name == "display_audio":
        return await _handle_display_media(arguments, "audio")
    elif name == "display_ui":
        return await _handle_display_ui(arguments)
    handler = app_tools.HANDLERS.get(name)
    if handler is not None:
        return await handler(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _prepare_image_item(item: dict) -> dict:
    """Convert one input item into a wire-format dict for /v1/hooks/images.

    URL items pass straight through (browser fetches from the CDN — no
    base64 round-trip through the proxy). Local file paths are read,
    resized, and base64-encoded here so the dashboard can render them
    without server access.
    """
    source = (item.get("source") or "").strip()
    if not source:
        raise ValueError("each image must have a non-empty 'source'")

    base = {
        "caption": item.get("caption") or "",
        "attribution": item.get("attribution") or "",
        "link_url": item.get("link_url") or "",
        "download_url": item.get("download_url") or "",
    }

    if source.startswith(("http://", "https://")):
        # URL passes through unchanged. Default download_url to the source
        # URL so the per-card download button has something to fetch.
        base["url"] = source
        base["mime_type"] = ""
        base["image_data"] = ""
        if not base["download_url"]:
            base["download_url"] = source
        return base

    # Local file — load + resize + base64-encode. Path is already in
    # satellite-host form (on remote sessions the stdio interceptor
    # translates `tool_arg_paths` declarations) or host form (on local
    # sessions bwrap maps the sandbox transparently).
    if not os.path.isfile(source):
        raise ValueError(f"file not found: {source}")
    size = os.path.getsize(source)
    if size > MAX_SOURCE_BYTES:
        raise ValueError(
            f"file too large to display "
            f"({size // (1024 * 1024)}MB; "
            f"limit {MAX_SOURCE_BYTES // (1024 * 1024)}MB)"
        )
    with open(source, "rb") as f:
        img_bytes = f.read()
    b64_data, mime_type = _resize_and_encode(img_bytes)
    base["url"] = ""
    base["image_data"] = b64_data
    base["mime_type"] = mime_type
    return base


async def _handle_display_images(arguments: dict) -> list[TextContent]:
    """Render 1-N images inline in the chat as a single gallery block."""
    items = arguments.get("images") or []
    if not isinstance(items, list) or not items:
        return [TextContent(type="text", text="Error: 'images' must be a non-empty list.")]

    prepared: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return [TextContent(type="text", text=f"Error: images[{idx}] must be an object.")]
        try:
            # URL → passed through (browser fetches); local file → read +
            # resized + base64-inlined so the proxy serves it. display-mcp is a
            # generic display tool — it carries NO vendor credentials. To show
            # an image that needs auth or lives on a private/local network
            # (e.g. a Home Assistant camera), the agent downloads it to its
            # workspace first and passes the local file path (see the owning
            # MCP's skill).
            prepared.append(await _prepare_image_item(item))
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error preparing images[{idx}]: {e}")]

    payload = {"session_id": SESSION_ID, "images": prepared}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/images",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return [TextContent(
            type="text",
            text=f"Error posting gallery: HTTP {e.response.status_code} — {e.response.text[:200]}",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error posting gallery: {e}")]

    return [TextContent(
        type="text",
        text=f"Displayed {len(prepared)} image{'s' if len(prepared) != 1 else ''} to user.",
    )]


async def _handle_send_file(arguments: dict) -> list[TextContent]:
    """Push a file download link to the chat via proxy."""
    path = arguments.get("path", "").strip()
    filename = arguments.get("filename", "").strip()
    description = arguments.get("description", "")

    if not path:
        return [TextContent(type="text", text="Error: path is required.")]

    # The framework's tool_arg_paths interceptor + path_policy_v2
    # already gate this path against the session's
    # allow_full_fs / home-dir / agent-tree policy BEFORE we see it.
    # Don't re-gate — a previous OTO_ALLOWED_ROOTS check was both
    # redundant (the framework already validated scope) AND broken on
    # Windows satellites (os.path.normpath flips `/` to `\` while the
    # ALLOWED_ROOTS env var stays forward-slash, so `startswith` always
    # missed). Just verify the file exists on disk and pass through.
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"Error: file not found: {path}")]

    if not filename:
        filename = os.path.basename(path)

    try:
        payload = {
            "session_id": SESSION_ID,
            "path": path,
            "filename": filename,
            "description": description,
        }

        async with httpx.AsyncClient(
            # /v1/hooks/file does a FULL synchronous satellite pull for
            # remote sessions — budget must cover a multi-MB WAN transfer
            # (proxy pull budget is 180 s).
            timeout=httpx.Timeout(connect=5.0, read=150.0, write=30.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/file",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()

        return [TextContent(
            type="text",
            text=f"File sent to user: {filename}",
        )]

    except httpx.HTTPStatusError as e:
        return [TextContent(
            type="text",
            text=f"Error sending file: HTTP {e.response.status_code} — {e.response.text[:200]}",
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Error sending file: {e}",
        )]


async def _handle_send_url(arguments: dict) -> list[TextContent]:
    """Push a clickable link to the chat."""
    url = arguments.get("url", "").strip()
    title = arguments.get("title", "").strip()
    description = arguments.get("description", "")

    if not url or not title:
        return [TextContent(type="text", text="Error: url and title are required.")]

    try:
        payload = {
            "session_id": SESSION_ID,
            "url": url,
            "title": title,
            "description": description,
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/url",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()

        return [TextContent(
            type="text",
            text=f"Link sent to user: [{title}]({url})",
        )]

    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Error sending URL: {e}",
        )]


async def _handle_display_media(arguments: dict, media_kind: str) -> list[TextContent]:
    """Render an audio/video player in the chat.

    Thin by design: unlike images (read + resized + base64-encoded here), media
    is handed to the proxy as a source reference. The proxy resolves it (web
    URL / local / agent-tree / satellite-host path — pulling remote files as
    needed), serves it with HTTP Range support via a capability token, and
    transcodes non-web-native codecs. This tool just forwards the request.
    """
    source = (arguments.get("source") or "").strip()
    if not source:
        return [TextContent(type="text", text="Error: 'source' is required.")]

    payload = {
        "session_id": SESSION_ID,
        "source": source,
        "media_kind": media_kind,
        "caption": arguments.get("caption") or "",
        "title": arguments.get("title") or "",
        "poster": arguments.get("poster") or "",
    }
    try:
        # Generous read timeout: a remote source may be pulled from a satellite
        # (and, post-transcode, converted) before the hook returns.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/media",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return [TextContent(
            type="text",
            text=f"Error displaying {media_kind}: HTTP {e.response.status_code} — {e.response.text[:200]}",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error displaying {media_kind}: {e}")]

    return [TextContent(type="text", text=f"Displayed {media_kind} to user.")]


async def _handle_display_ui(arguments: dict) -> list[TextContent]:
    """Render an HTML artifact in the chat (or save it without displaying).

    Thin by design: the proxy hook owns save-path resolution (scope defaults,
    RBAC re-gating, satellite push) and serving happens through the sandboxed
    /v1/ui route at display time — this tool just validates and forwards. The
    saved path comes back in the ack so the agent can Edit + re-display to
    iterate on one artifact: with ``html`` omitted, the file's CURRENT
    content is read here (same filesystem the CLI's Edit just wrote) and
    forwarded, so an update costs diff-sized tokens instead of a full
    re-send.
    """
    html_content = arguments.get("html") or ""
    save_path_arg = (arguments.get("save_path") or "").strip()
    if not html_content.strip():
        if not save_path_arg:
            return [TextContent(type="text", text=(
                "Error: provide 'html', or 'save_path' of an existing "
                "artifact to re-display it from its file."
            ))]
        html_content, err = _read_artifact_file(save_path_arg)
        if err:
            return [TextContent(type="text", text=err)]
    if len(html_content.encode("utf-8")) > MAX_UI_HTML_BYTES:
        return [TextContent(
            type="text",
            text=(
                "Error: html exceeds the 2MB artifact cap — aggregate the data "
                "or trim embedded assets instead of inlining them."
            ),
        )]
    height = arguments.get("height")
    if height is not None:
        try:
            height = int(height)
        except (TypeError, ValueError):
            return [TextContent(type="text", text="Error: 'height' must be an integer.")]
    display = bool(arguments.get("display", True))

    payload = {
        "session_id": SESSION_ID,
        "html": html_content,
        "title": (arguments.get("title") or "").strip(),
        "height": height,
        "save_path": (arguments.get("save_path") or "").strip(),
        "display": display,
    }
    try:
        # Generous read timeout: the hook pushes the file to active remote
        # sessions before returning.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/ui",
                json=payload,
                headers={
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            saved_path = (resp.json() or {}).get("path", "")
    except httpx.HTTPStatusError as e:
        return [TextContent(
            type="text",
            text=f"Error displaying UI artifact: HTTP {e.response.status_code} — {e.response.text[:200]}",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error displaying UI artifact: {e}")]

    if not display:
        return [TextContent(
            type="text",
            text=(
                f"Saved UI artifact to {saved_path} (no new chat block; any "
                f"already-displayed artifact at this path live-reloaded)."
            ),
        )]
    return [TextContent(
        type="text",
        text=(
            f"Displayed UI artifact to user (saved at {saved_path}). To "
            f"iterate cheaply: Edit that file, then call display_ui with "
            f"ONLY save_path='{saved_path}' (no html) — display=true "
            f"re-shows it at the newest chat position (the older copy "
            f"collapses), display=false refreshes it silently in place."
        ),
    )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
