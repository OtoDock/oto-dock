"""Unit tests for display-mcp's `_resize_and_encode` + `_is_svg` helpers.

The MCP code lives outside the proxy import path; we add it to sys.path and
import the module directly. These are pure functions (bytes in, (base64, mime)
out) so no MCP runtime is needed.

HEIC tests skip automatically when pillow-heif isn't installed in the running
venv — the proxy test venv doesn't carry it, the display-mcp venv does.
"""

from __future__ import annotations

import base64
import io
import sys

import pytest
from PIL import Image

from tests._paths import CUSTOM_MCPS
MCP_DIR = CUSTOM_MCPS / "display-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import display_server as d  # noqa: E402


# ───────────────────────────── helpers ──────────────────────────────────────


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _encode(mode: str, size: tuple[int, int], color, fmt: str, **save) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt, **save)
    return buf.getvalue()


def _anim(fmt: str, n: int = 3, size: tuple[int, int] = (10, 10)) -> bytes:
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)][:n]
    frames = [Image.new("RGB", size, c) for c in colors]
    buf = io.BytesIO()
    frames[0].save(
        buf, format=fmt, save_all=True, append_images=frames[1:], duration=80, loop=0
    )
    return buf.getvalue()


# ───────────────────────────── _is_svg ──────────────────────────────────────


@pytest.mark.parametrize(
    "data,expected",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', True),
        (b"  \n  <svg></svg>", True),  # leading whitespace
        (b'<?xml version="1.0"?>\n<svg></svg>', True),  # XML prolog
        (b"\xef\xbb\xbf<svg></svg>", True),  # UTF-8 BOM
        (b"<SVG></SVG>", True),  # case-insensitive
        (b'<?xml version="1.0"?><rss></rss>', False),  # XML but not SVG
        (b"\x89PNG\r\n\x1a\n", False),  # PNG magic bytes
        (b"\xff\xd8\xff", False),  # JPEG magic bytes
        (b"", False),
    ],
)
def test_is_svg(data, expected):
    assert d._is_svg(data) is expected


# ─────────────────────────── SVG passthrough ────────────────────────────────


def test_svg_passthrough_byte_for_byte():
    svg = (
        b'<?xml version="1.0"?>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    )
    b64, mime = d._resize_and_encode(svg)
    assert mime == "image/svg+xml"
    assert base64.b64decode(b64) == svg  # no decode/re-encode


def test_svg_oversized_rejected(monkeypatch):
    monkeypatch.setattr(d, "MAX_PASSTHROUGH_BYTES", 8)
    with pytest.raises(ValueError, match="SVG too large"):
        d._resize_and_encode(b"<svg> some padding bytes </svg>")


# ───────────────────────── animated passthrough ─────────────────────────────


def test_animated_gif_passthrough():
    raw = _anim("GIF")
    b64, mime = d._resize_and_encode(raw)
    assert mime == "image/gif"
    assert base64.b64decode(b64) == raw
    assert _decode(b64).n_frames == 3  # animation preserved


def test_animated_webp_passthrough():
    raw = _anim("WEBP")
    b64, mime = d._resize_and_encode(raw)
    assert mime == "image/webp"
    assert base64.b64decode(b64) == raw


def test_oversized_animation_falls_back_to_static(monkeypatch):
    raw = _anim("GIF")
    monkeypatch.setattr(d, "MAX_PASSTHROUGH_BYTES", 4)  # force oversize
    b64, mime = d._resize_and_encode(raw)
    assert mime == "image/jpeg"
    assert getattr(_decode(b64), "n_frames", 1) == 1  # flattened to first frame


def test_single_frame_gif_is_not_passthrough():
    raw = _encode("RGB", (10, 10), (1, 2, 3), "GIF")
    _, mime = d._resize_and_encode(raw)
    assert mime == "image/jpeg"  # static GIF re-encoded, not passed through


# ──────────────────────────── alpha / opaque ────────────────────────────────


def test_png_with_alpha_kept_as_png():
    raw = _encode("RGBA", (20, 20), (0, 0, 0, 0), "PNG")
    b64, mime = d._resize_and_encode(raw)
    assert mime == "image/png"


def test_palette_transparency_kept_as_png():
    raw = _encode("P", (10, 10), 0, "GIF", transparency=0)
    _, mime = d._resize_and_encode(raw)
    assert mime == "image/png"


def test_opaque_image_becomes_jpeg():
    raw = _encode("RGB", (20, 20), (10, 20, 30), "PNG")
    _, mime = d._resize_and_encode(raw)
    assert mime == "image/jpeg"


# ────────────────────────── EXIF orientation ────────────────────────────────


def test_jpeg_exif_orientation_applied():
    im = Image.new("RGB", (40, 20), (10, 20, 30))
    exif = im.getexif()
    exif[274] = 6  # Orientation: rotate 90° CW
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    b64, _ = d._resize_and_encode(buf.getvalue())
    assert _decode(b64).size == (20, 40)  # dimensions swapped by transpose


# ───────────────────────────── downscale ────────────────────────────────────


def test_large_image_downscaled_to_max_dim():
    raw = _encode("RGB", (4000, 2000), (5, 5, 5), "PNG")
    b64, _ = d._resize_and_encode(raw)
    assert max(_decode(b64).size) == d.MAX_IMAGE_DIM


# ─────────────────────── HEIC (skips without dep) ────────────────────────────


@pytest.mark.skipif(not d._HEIC_AVAILABLE, reason="pillow-heif not installed")
def test_heic_decodes_to_jpeg():
    raw = _encode("RGB", (30, 24), (200, 100, 50), "HEIF")
    b64, mime = d._resize_and_encode(raw)
    assert mime == "image/jpeg"
    assert _decode(b64).size == (30, 24)


# ─────────────────────── display_ui handler validation ──────────────────────


@pytest.mark.asyncio
async def test_display_ui_rejects_empty_and_oversized_html():
    # No html AND no save_path: nothing to display, nothing to re-read.
    out = await d._handle_display_ui({"html": "   "})
    assert "Error" in out[0].text and "save_path" in out[0].text
    out = await d._handle_display_ui({"html": "x" * (d.MAX_UI_HTML_BYTES + 1)})
    assert "Error" in out[0].text and "2MB" in out[0].text


@pytest.mark.asyncio
async def test_display_ui_rejects_non_integer_height():
    out = await d._handle_display_ui({"html": "<p>x</p>", "height": "tall"})
    assert "Error" in out[0].text and "height" in out[0].text


@pytest.mark.asyncio
async def test_display_ui_posts_hook_payload_and_acks_path(monkeypatch):
    posted = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok", "path": "/users/a/workspace/generated-ui/x.html",
                    "ui_url": "/v1/ui/tok"}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["payload"] = json
            return _Resp()

    monkeypatch.setattr(d.httpx, "AsyncClient", _Client)
    out = await d._handle_display_ui({
        "html": "<p>hi</p>", "title": "Hi", "height": 300,
        "save_path": "reports/x.html",
    })
    assert posted["url"].endswith("/v1/hooks/ui")
    assert posted["payload"]["html"] == "<p>hi</p>"
    assert posted["payload"]["height"] == 300
    assert posted["payload"]["save_path"] == "reports/x.html"
    assert posted["payload"]["display"] is True
    # The ack teaches the iterate loop: saved path + re-display hint.
    assert "/users/a/workspace/generated-ui/x.html" in out[0].text
    assert "save_path" in out[0].text

    out = await d._handle_display_ui({"html": "<p>q</p>", "display": False})
    assert posted["payload"]["display"] is False
    assert out[0].text.startswith("Saved UI artifact")


@pytest.mark.asyncio
async def test_display_ui_htmlless_redisplay_reads_file(monkeypatch, tmp_path):
    """html omitted + save_path → the MCP forwards the file's CURRENT content
    (the cheap Edit-then-re-display iteration loop)."""
    ws = tmp_path / "workspace"
    (ws / "games").mkdir(parents=True)
    (ws / "games" / "board.html").write_text("<b>edited</b>", "utf-8")
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("OTO_USERNAME", "")

    posted = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok", "path": "/workspace/games/board.html",
                    "ui_url": "/v1/ui/tok2"}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["payload"] = json
            return _Resp()

    monkeypatch.setattr(d.httpx, "AsyncClient", _Client)
    out = await d._handle_display_ui({"save_path": "/workspace/games/board.html"})
    assert posted["payload"]["html"] == "<b>edited</b>"
    assert posted["payload"]["save_path"] == "/workspace/games/board.html"
    assert "Displayed UI artifact" in out[0].text

    # Missing file → guidance, no POST.
    posted.clear()
    out = await d._handle_display_ui({"save_path": "games/missing.html"})
    assert "no artifact file found" in out[0].text
    assert not posted
