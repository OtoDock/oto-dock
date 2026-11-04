"""Image editing and analysis handlers for the file-tools MCP.

Lightroom-style editing with research-backed algorithms:
- Frequency-separation highlights/shadows (darktable shadhi)
- Dark channel prior dehaze (He et al. 2011)
- Chroma-only denoise (YCbCr)
- Sigmoid split toning (darktable colorbalancergb)
- Cosine-bell HSL per-color adjustment
"""

import asyncio
import contextvars
import functools
import io
import os
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from batch import run_batch
from catalog import MISSING, Catalog, KeySpec, OpSpec
from formats.pdf import image_tone
from formats.pdf.pages import whole
from results import Code, NoOp, OpCtx, OpError
from shared import (
    HOOK_TIMEOUT,
    PROXY_URL,
    _current_session,
    _notify_file_written,
    _push_image_preview,
    _resolve_path,
    _to_agents_relative,
    safe_open_write,
)

_EXAMPLE_PNG = "logo.png"

# rembg background-removal model. We use ``isnet-general-use`` (noticeably
# cleaner edges/hair than ``u2net`` at the same ~176MB size + CPU-speed class;
# rembg's own default is ``bria-rmbg``, so the name is always passed). The model
# is pre-baked into the Docker image (see the Dockerfile: ``U2NET_HOME`` +
# ``new_session('isnet-general-use')``), so it never downloads at runtime. The
# session is created lazily once and reused across calls — loading the model is
# expensive, so recreating it per call would reload ~176MB every time.
_REMBG_MODEL = "isnet-general-use"
_rembg_session = None
# Image jobs run in worker threads: the first two to want the session must
# not both build it.
_rembg_lock = threading.Lock()


def _get_rembg_session():
    """Lazily create and cache the rembg background-removal session."""
    global _rembg_session
    with _rembg_lock:
        if _rembg_session is None:
            from rembg import new_session
            _rembg_session = new_session(_REMBG_MODEL)
    return _rembg_session


# ---------------------------------------------------------------------------
# One image job at a time, off the event loop
# ---------------------------------------------------------------------------
#
# The shared server serves every session, so a decode or an edit of a large
# photo must not hold its loop. It runs in a worker thread instead, and only
# one image job runs at a time: two edits of one file apply one after the
# other, the working sets of two large edits never add up in the server's
# memory, and the default executor keeps its threads for the path lookups.

_image_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _image_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _image_sems.get(loop)
    if sem is None:
        sem = _image_sems[loop] = asyncio.Semaphore(1)
    return sem


async def _image_job(fn, *args):
    """``fn(*args)`` in a worker thread under the one-at-a-time hold. The
    hold is released when the thread ends, not when the caller stops
    waiting: a cancelled call leaves its thread running to the end, and the
    next job waits for it."""
    sem = _image_sem()
    await sem.acquire()
    try:
        call = functools.partial(contextvars.copy_context().run, fn, *args)
        fut = asyncio.get_running_loop().run_in_executor(None, call)
    except BaseException:
        sem.release()
        raise
    fut.add_done_callback(lambda _f: sem.release())
    return await asyncio.shield(fut)


@dataclass
class _ImageEdit:
    """The image one edit_image call works on and the path it saves to; an
    operation replaces either (most return a new image, convert_format a
    new extension). The save settings a compress asked for live here, not
    on the image, so an operation that returns a new image keeps them."""
    img: Any
    out: str
    compress_quality: int | None = None
    compress_format: str | None = None
    targets: dict[str, str] = field(default_factory=dict)   # convert_format's resolved outputs
    segment: Any = None                                      # remove_background's model, set lazily


# ---------------------------------------------------------------------------
# edit_image
# ---------------------------------------------------------------------------

# Ops that move tone/color — these trigger the numeric "Tone check" line in
# the result so the model sees exactly what its edit did (mean/median/zones/
# clipping, before → after) instead of judging brightness off the inline
# preview. This closes the calibration loop that made tonal edits hit-or-miss.
_TONAL_OPS = {
    "brightness", "contrast", "exposure", "highlights", "shadows",
    "whites", "blacks", "clarity", "dehaze", "auto_enhance",
    "color_temperature", "saturation", "grayscale", "sepia", "invert",
    "split_tone", "hsl_adjust",
}


def _tone_stats(img) -> dict:
    """Compact luminance stats on a downsampled copy — cheap enough to run
    before AND after every edit."""
    import numpy as np

    small = img.convert("RGB").copy()
    small.thumbnail((256, 256))
    arr = np.asarray(small, dtype=np.float32)
    lum = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    flat = lum.ravel()
    return {
        "mean": float(flat.mean()),
        "median": float(np.median(flat)),
        "clip_black": float((flat <= 5).mean() * 100),
        "clip_white": float((flat >= 250).mean() * 100),
        "shadows": float((flat < 64).mean() * 100),
        "highlights": float((flat >= 192).mean() * 100),
    }


def _tone_check_line(before: dict, after: dict) -> str:
    return (
        "Tone check (before → after): "
        f"mean {before['mean']:.0f} → {after['mean']:.0f} "
        f"({after['mean'] - before['mean']:+.0f}), "
        f"median {before['median']:.0f} → {after['median']:.0f} | "
        f"shadows<64 {before['shadows']:.0f}% → {after['shadows']:.0f}%, "
        f"highlights>192 {before['highlights']:.0f}% → {after['highlights']:.0f}% | "
        f"clipped blacks {before['clip_black']:.1f}% → {after['clip_black']:.1f}%, "
        f"whites {before['clip_white']:.1f}% → {after['clip_white']:.1f}%"
    )


def _op_resize(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    w = int(op.get("width", job.img.width))
    h = int(op.get("height", job.img.height))
    if op.get("maintain_aspect", True):
        job.img.thumbnail((w, h), Image.LANCZOS)
    else:
        job.img = job.img.resize((w, h), Image.LANCZOS)


def _op_crop(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    left = int(op.get("left", 0))
    top = int(op.get("top", 0))
    right = int(op.get("right", job.img.width))
    bottom = int(op.get("bottom", job.img.height))
    job.img = job.img.crop((left, top, right, bottom))


def _op_rotate(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    degrees = float(op.get("degrees", 0))
    expand = op.get("expand", True)
    job.img = job.img.rotate(
        -degrees, expand=expand, resample=Image.BICUBIC
    )


def _op_flip(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    direction = op.get("direction", "horizontal")
    if direction == "horizontal":
        job.img = job.img.transpose(Image.FLIP_LEFT_RIGHT)
    else:
        job.img = job.img.transpose(Image.FLIP_TOP_BOTTOM)


def _op_brightness(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Brightness(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_contrast(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Contrast(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_saturation(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Color(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_sharpness(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Sharpness(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_grayscale(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageOps

    job.img = ImageOps.grayscale(job.img)
    job.img = job.img.convert("RGB")


def _op_sepia(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image, ImageOps

    gray = ImageOps.grayscale(job.img)
    sepia = Image.merge(
        "RGB",
        (
            gray.point(lambda x: min(255, int(x * 1.2))),
            gray.point(lambda x: min(255, int(x * 1.0))),
            gray.point(lambda x: min(255, int(x * 0.8))),
        ),
    )
    job.img = sepia


def _op_invert(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image, ImageOps

    if job.img.mode == "RGBA":
        r, g, b, a = job.img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageOps.invert(rgb)
        job.img = Image.merge("RGBA", (*rgb.split(), a))
    else:
        job.img = ImageOps.invert(job.img.convert("RGB"))


def _op_blur(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageFilter

    radius = float(op.get("radius", 2))
    job.img = job.img.filter(ImageFilter.GaussianBlur(radius=radius))


def _op_sharpen(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.SHARPEN)


def _op_edge_enhance(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.EDGE_ENHANCE_MORE)


def _op_emboss(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.EMBOSS)


def _op_auto_enhance(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageEnhance, ImageOps

    job.img = ImageOps.autocontrast(job.img)
    job.img = ImageEnhance.Color(job.img).enhance(1.1)
    job.img = ImageEnhance.Sharpness(job.img).enhance(1.1)


def _op_color_temperature(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    temp = float(op.get("temperature", 5500))
    factor = (temp - 5500) / 5500
    r, g, b = job.img.split()[:3]
    r = r.point(
        lambda x, factor=factor: min(255, max(0, int(x * (1 + factor * 0.1))))
    )
    b = b.point(
        lambda x, factor=factor: min(255, max(0, int(x * (1 - factor * 0.1))))
    )
    if job.img.mode == "RGBA":
        job.img = Image.merge("RGBA", (r, g, b, job.img.split()[3]))
    else:
        job.img = Image.merge("RGB", (r, g, b))


def _op_exposure(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    gamma = float(op.get("gamma", 1.0))
    inv_gamma = 1.0 / gamma
    lut = [
        int(((i / 255.0) ** inv_gamma) * 255) for i in range(256)
    ]
    if job.img.mode == "RGBA":
        r, g, b, a = job.img.split()
        r = r.point(lut)
        g = g.point(lut)
        b = b.point(lut)
        job.img = Image.merge("RGBA", (r, g, b, a))
    else:
        channels = job.img.split()
        channels = tuple(ch.point(lut) for ch in channels)
        job.img = Image.merge(job.img.mode, channels)


def _op_add_text(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(job.img)
    text = op.get("text", "")
    x = int(op.get("x", 10))
    y = int(op.get("y", 10))
    font_size = int(op.get("font_size", 24))
    color = _rgb(op.get("color", "#FFFFFF"))
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            font_size,
        )
    except OSError:
        font = ImageFont.load_default()
    draw.text((x, y), text, fill=color, font=font)


def _op_add_border(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageOps

    border_w = int(op.get("width", 5))
    color = _rgb(op.get("color", "#000000"))
    job.img = ImageOps.expand(job.img, border=border_w, fill=color)


def _op_paste_image(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    overlay_path = op["image_path"]
    if not os.path.isfile(overlay_path):
        raise OpError(Code.NOT_FOUND, f"the image {Path(overlay_path).name} does not exist", location="image_path",
                      hint="Check the path.")
    overlay = Image.open(overlay_path)
    x = int(op.get("x", 0))
    y = int(op.get("y", 0))
    if op.get("width"):
        ow = int(op["width"])
        ratio = ow / overlay.width
        oh = int(overlay.height * ratio)
        overlay = overlay.resize((ow, oh), Image.LANCZOS)
    opacity = float(op.get("opacity", 1.0))
    if opacity < 1.0:
        overlay = overlay.convert("RGBA")
        alpha = overlay.split()[3]
        alpha = alpha.point(lambda a, opacity=opacity: int(a * opacity))
        overlay.putalpha(alpha)
    if overlay.mode == "RGBA":
        job.img.paste(overlay, (x, y), overlay)
    else:
        job.img.paste(overlay, (x, y))


def _op_add_watermark(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image, ImageDraw, ImageFont

    txt = op.get("text", "WATERMARK")
    opacity = float(op.get("opacity", 0.3))
    watermark = Image.new("RGBA", job.img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(watermark)
    font_size = max(job.img.width // len(txt), 20)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            font_size,
        )
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), txt, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (job.img.width - tw) // 2
    y = (job.img.height - th) // 2
    alpha_val = int(255 * opacity)
    draw.text(
        (x, y), txt, fill=(128, 128, 128, alpha_val), font=font
    )
    if job.img.mode != "RGBA":
        job.img = job.img.convert("RGBA")
    job.img = Image.alpha_composite(job.img, watermark)
    job.img = job.img.convert("RGB")


def _op_remove_background(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import Image

    job.img = (job.segment or _segment)(job.img)
    # rembg returns RGBA with transparent background
    if job.img.mode != "RGBA":
        job.img = job.img.convert("RGBA")
    # If user wants a specific background color instead of transparent
    bg_color = op.get("background_color")
    if bg_color:
        bg = Image.new("RGB", job.img.size, _rgb(bg_color))
        bg.paste(job.img, mask=job.img.split()[3])
        job.img = bg


def _op_blur_region(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    from PIL import ImageFilter

    # Blur a rectangular region (privacy: faces, plates, addresses)
    # Accept both (left,top,right,bottom) and (x,y,width,height) formats
    if op.get("x") is not None or op.get("width") is not None:
        x = int(op.get("x", 0))
        y = int(op.get("y", 0))
        w = int(op.get("width", 100))
        h = int(op.get("height", 100))
        left, top_coord, right, bottom = x, y, x + w, y + h
    else:
        left = int(op.get("left", 0))
        top_coord = int(op.get("top", 0))
        right = int(op.get("right", left + 100))
        bottom = int(op.get("bottom", top_coord + 100))
    # Clamp to image bounds
    left = max(0, min(left, job.img.width))
    top_coord = max(0, min(top_coord, job.img.height))
    right = max(left + 1, min(right, job.img.width))
    bottom = max(top_coord + 1, min(bottom, job.img.height))
    radius = float(op.get("radius", 15))
    # Crop region, blur, paste back
    region = job.img.crop((left, top_coord, right, bottom))
    region = region.filter(
        ImageFilter.GaussianBlur(radius=radius)
    )
    job.img.paste(region, (left, top_coord))


def _op_compress(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    # Set compression for the final save
    quality = whole(ctx, op.get("quality", 75), "quality")
    target_format = op.get("format", "").upper()
    if target_format == "JPG":
        target_format = "JPEG"
    # Store for save step (handled below)
    job.compress_quality = quality
    if target_format:
        job.compress_format = target_format


def _op_convert_format(job: _ImageEdit, ctx: OpCtx, op: dict) -> None:
    # Convert to a different format by changing output path
    target_format = _format_name(op.get("format", ""))
    if not target_format:
        raise NoOp("no format was given")
    job.out = job.targets.get(target_format) or job.out


_FORMATS = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "tif": "tiff", "tiff": "tiff",
            "bmp": "bmp", "gif": "gif"}
_SUFFIX = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "tiff": ".tiff", "bmp": ".bmp", "gif": ".gif"}


def _format_name(value: Any) -> str:
    """A format the save takes (jpeg, png, webp, ...), "" for none."""
    if not value:
        return ""
    name = _FORMATS.get(str(value).strip().lstrip(".").casefold())
    if name is None:
        raise OpError(Code.BAD_VALUE, f"the format '{value}' is not one edit_image writes", location="format",
                      hint="Give png, jpeg, webp, tiff, bmp or gif.")
    return name


def _rgb(value: Any) -> tuple[int, ...]:
    from colors import parse_color

    color = parse_color(value, allow_theme=False)
    rgb = tuple(color.rgb)
    return rgb if color.alpha in (None, 1, 1.0) else (*rgb, round(color.alpha * 255))


def _segment(img: Any) -> Any:
    from rembg import remove

    return remove(img, session=_get_rembg_session())


_PX = ("int", "float")
_COLOR_KEY = ("color", "list")


def _k(name: str, types: tuple[str, ...], default: Any = MISSING, help: str = "", **kw: Any) -> KeySpec:
    return KeySpec(name, types, default=default, help=help, **kw)


def _factor(note: str) -> tuple[KeySpec, ...]:
    return (_k("factor", ("float",), 1.0, note),)


_TONE_ZONE = (_k("hue", ("float",), 0, "0-360"), _k("saturation", ("float",), 50, "0-100"),
              _k("strength", ("float",), 0.15, "0-1"))
_HSL = (_k("hue", ("float",), 0, "degrees"), _k("saturation", ("float",), 0, "-100 to 100"),
        _k("lightness", ("float",), 0, "-100 to 100"))
_HSL_COLORS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")

CATALOG = Catalog("edit_image", [
    OpSpec("resize", _op_resize, (_k("width", _PX, help="px; the image's own when left out"),
                                  _k("height", _PX, help="px"),
                                  _k("maintain_aspect", ("bool",), True, "fit inside width x height")),
           "BASIC", examples=({"op": "resize", "width": 20},)),
    OpSpec("crop", _op_crop, tuple(_k(n, _PX, help="px") for n in ("left", "top", "right", "bottom")), "BASIC",
           examples=({"op": "crop", "left": 2, "top": 2, "right": 30, "bottom": 18},)),
    OpSpec("rotate", _op_rotate, (_k("degrees", ("float",), 0, "clockwise"),
                                  _k("expand", ("bool",), True, "grow the canvas to fit")),
           "BASIC", examples=({"op": "rotate", "degrees": 90},)),
    OpSpec("flip", _op_flip, (_k("direction", ("str",), "horizontal", "horizontal | vertical"),), "BASIC",
           examples=({"op": "flip", "direction": "vertical"},)),
    OpSpec("convert_format", _op_convert_format, (_k("format", ("str",), "", "png | jpeg | webp | tiff | bmp | gif"),),
           "BASIC", note="Saves in another format: the output takes that extension.",
           examples=({"op": "convert_format", "format": "webp"},)),
    OpSpec("compress", _op_compress, (_k("quality", ("int", "float"), 75, "1-100 (JPEG, WebP)"),
                                      _k("format", ("str",), "", "jpeg | webp | png")),
           "BASIC", examples=({"op": "compress", "quality": 70},)),
    OpSpec("exposure", _op_exposure, (_k("gamma", ("float",), 1.0, ">1 brightens, <1 darkens; midtone-weighted"),),
           "TONE", examples=({"op": "exposure", "gamma": 1.2},)),
    OpSpec("highlights", image_tone.op_highlights, (_k("amount", ("float",), 0, "-100 to 100"),), "TONE",
           examples=({"op": "highlights", "amount": -30},)),
    OpSpec("shadows", image_tone.op_shadows, (_k("amount", ("float",), 0, "-100 to 100"),), "TONE",
           examples=({"op": "shadows", "amount": 30},)),
    OpSpec("whites", image_tone.op_whites, (_k("level", ("float",), 0, "relative"),), "TONE",
           examples=({"op": "whites", "level": 10},)),
    OpSpec("blacks", image_tone.op_blacks, (_k("level", ("float",), 0, "relative: >0 lifts, <0 deepens"),),
           "TONE", examples=({"op": "blacks", "level": -10},)),
    OpSpec("brightness", _op_brightness, _factor("1 unchanged; linear, clips sooner than exposure"), "TONE",
           examples=({"op": "brightness", "factor": 1.1},)),
    OpSpec("contrast", _op_contrast, _factor("1 unchanged"), "TONE", examples=({"op": "contrast", "factor": 1.2},)),
    OpSpec("clarity", image_tone.op_clarity, (_k("amount", ("float",), 20, "midtone local contrast"),), "TONE",
           examples=({"op": "clarity", "amount": 20},)),
    OpSpec("dehaze", image_tone.op_dehaze, (_k("strength", ("float",), 0.3, "0-1; darkens overall"),), "TONE",
           examples=({"op": "dehaze", "strength": 0.3},)),
    OpSpec("saturation", _op_saturation, _factor("1 unchanged"), "COLOR",
           examples=({"op": "saturation", "factor": 1.2},)),
    OpSpec("color_temperature", _op_color_temperature,
           (_k("temperature", ("float",), 5500, "kelvin; above 5500 is warmer, below cooler"),),
           "COLOR", examples=({"op": "color_temperature", "temperature": 4500},)),
    OpSpec("grayscale", _op_grayscale, (), "COLOR", examples=({"op": "grayscale"},)),
    OpSpec("sepia", _op_sepia, (), "COLOR", examples=({"op": "sepia"},)),
    OpSpec("invert", _op_invert, (), "COLOR", examples=({"op": "invert"},)),
    OpSpec("split_tone", image_tone.op_split_tone, tuple(
        _k(zone, ("dict",), fields=_TONE_ZONE) for zone in ("shadows", "midtones", "highlights")), "COLOR",
        examples=({"op": "split_tone", "shadows": {"hue": 210, "saturation": 40}},)),
    OpSpec("hsl_adjust", image_tone.op_hsl_adjust, tuple(_k(c, ("dict",), fields=_HSL) for c in _HSL_COLORS),
           "COLOR", examples=({"op": "hsl_adjust", "blue": {"saturation": 20}},)),
    OpSpec("sharpness", _op_sharpness, _factor("1 unchanged"), "DETAIL", examples=({"op": "sharpness", "factor": 1.5},)),
    OpSpec("denoise", image_tone.op_denoise, (_k("strength", ("int", "float"), 3, "1-5"),), "DETAIL",
           examples=({"op": "denoise", "strength": 2},)),
    OpSpec("blur", _op_blur, (_k("radius", ("float",), 2, "px"),), "DETAIL", examples=({"op": "blur", "radius": 1},)),
    OpSpec("sharpen", _op_sharpen, (), "DETAIL", examples=({"op": "sharpen"},)),
    OpSpec("edge_enhance", _op_edge_enhance, (), "DETAIL", examples=({"op": "edge_enhance"},)),
    OpSpec("emboss", _op_emboss, (), "DETAIL", examples=({"op": "emboss"},)),
    OpSpec("auto_enhance", _op_auto_enhance, (), "EFFECTS", examples=({"op": "auto_enhance"},)),
    OpSpec("add_text", _op_add_text, (_k("text", ("str",), ""), _k("x", _PX, 10, "px"), _k("y", _PX, 10, "px"),
                                      _k("font_size", _PX, 24, "px"), _k("color", _COLOR_KEY, "#FFFFFF")),
           "EFFECTS", examples=({"op": "add_text", "text": "Αθήνα 2026", "x": 2, "y": 2, "font_size": 8},)),
    OpSpec("add_border", _op_add_border, (_k("width", _PX, 5, "px"), _k("color", _COLOR_KEY, "#000000")),
           "EFFECTS", examples=({"op": "add_border", "width": 2},)),
    OpSpec("paste_image", _op_paste_image, (KeySpec("image_path", ("path",), required=True),
                                            _k("x", _PX, 0, "px"), _k("y", _PX, 0, "px"),
                                            _k("width", _PX, help="px; keeps the proportions"),
                                            _k("opacity", ("float",), 1.0, "0-1")),
           "EFFECTS", examples=({"op": "paste_image", "image_path": _EXAMPLE_PNG, "x": 1, "y": 1},)),
    OpSpec("add_watermark", _op_add_watermark, (_k("text", ("str",), "WATERMARK"), _k("opacity", ("float",), 0.3, "0-1")),
           "EFFECTS", examples=({"op": "add_watermark", "text": "ΔΕΙΓΜΑ"},)),
    OpSpec("remove_background", _op_remove_background, (
        _k("background_color", _COLOR_KEY, help="a colour behind the subject; transparent PNG when left out"),),
        "EFFECTS", note="AI segmentation; the result has a transparent background unless background_color is given.",
        examples=({"op": "remove_background", "background_color": "white"},)),
    OpSpec("blur_region", _op_blur_region, (
        *(_k(n, _PX, help="px") for n in ("x", "y", "width", "height", "left", "top", "right", "bottom")),
        _k("radius", ("float",), 15, "blur strength", aliases=("intensity", "strength"))),
        "EFFECTS", note="Blurs a rectangle (faces, plates, addresses): x, y, width, height or left, top, right, "
                        "bottom.",
        examples=({"op": "blur_region", "x": 2, "y": 2, "width": 10, "height": 8},)),
], color_theme=False, notes="Pixel values count from the top left. Operations apply in order.")


def contract_job(tmp_dir: str) -> _ImageEdit:
    from PIL import Image

    return _ImageEdit(Image.new("RGB", (40, 24), (120, 140, 160)), os.path.join(tmp_dir, "contract.png"),
                      segment=lambda img: img.convert("RGBA"))


def contract_fixtures(tmp_dir: str) -> dict[str, str]:
    from PIL import Image

    image = os.path.join(tmp_dir, _EXAMPLE_PNG)
    Image.new("RGB", (8, 6), (31, 78, 121)).save(image)
    return {_EXAMPLE_PNG: image}


@dataclass
class _EditedImage:
    """What the edit's thread hands back to the loop."""
    text: str
    saved: bool
    out: str = ""
    width: int = 0
    height: int = 0
    preview: bytes = b""


def _open_image(path: str) -> Any:
    from PIL import Image, ImageOps

    img = Image.open(path)
    # EXIF orientation applied, so the pixels match what a viewer shows and
    # a manual rotation does not fight the tag.
    img = ImageOps.exif_transpose(img) or img
    if img.mode in ("RGBA", "P"):
        has_alpha = img.mode == "RGBA" or (img.mode == "P" and "transparency" in img.info)
        if not has_alpha:
            img = img.convert("RGB")
    return img


def _save_format(job: _ImageEdit) -> tuple[str, dict]:
    from PIL import Image

    save_format = Path(job.out).suffix.lstrip(".").upper()
    if save_format == "JPG":
        save_format = "JPEG"
    if job.compress_format:
        save_format = job.compress_format
    if save_format not in ("JPEG", "PNG", "WEBP", "TIFF", "BMP"):
        # A file object needs the format spelled out: the one Pillow
        # registers for the suffix, else the output is refused.
        save_format = Image.registered_extensions().get(Path(job.out).suffix.lower()) or ""
    kwargs: dict[str, Any] = {}
    if save_format == "JPEG":
        kwargs = {"quality": job.compress_quality or 95, "subsampling": 0}
    elif save_format == "PNG":
        kwargs = {"compress_level": 6}
    elif save_format == "WEBP":
        kwargs = {"quality": job.compress_quality or 95}
    return save_format, kwargs


def _edit_image_core(path: str, out: str, ops: list, *, best_effort: bool, new_file: bool, total: int,
                     rejected: list, pre_notes: dict, targets: dict[str, str]) -> _EditedImage:
    """In a worker thread: the image opened, the batch run, and when it
    commits the save beneath the mount and the chat preview."""

    from PIL import Image

    from results import render_outcome

    img = _open_image(path)
    has_tonal_op = any(op.get("op") in _TONAL_OPS for _, op in ops)
    # The tonal operations rebuild the image from 8-bit data in its own mode,
    # which a 1-bit image cannot take.
    if has_tonal_op and img.mode == "1":
        img = img.convert("L")
    before_stats = _tone_stats(img) if has_tonal_op else None
    job = _ImageEdit(img, out, targets=targets)
    outcome = run_batch(ops, CATALOG, job, best_effort=best_effort, create_new=new_file, rejected=rejected,
                        pre_notes=pre_notes, total=total)
    existed = os.path.exists(out)
    if not outcome.should_commit:
        return _EditedImage(render_outcome(outcome, kind="Image", path=out, saved=False, existed=existed), False)
    img, out = job.img, job.out
    save_format, save_kwargs = _save_format(job)
    if not save_format:
        return _EditedImage(f"Nothing saved: {_to_agents_relative(out)} was not written. The output format "
                            f"'{Path(out).suffix}' is not one edit_image writes: use png, jpg, webp, tiff or bmp.",
                            False)
    if save_format == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    # Beneath the mount, no link followed: a link at the output name is
    # replaced, a link on the way refuses the write.
    with safe_open_write(out) as fh:
        img.save(fh, format=save_format, **save_kwargs)
    buf = io.BytesIO()
    preview_img = img.copy()
    preview_img.thumbnail((1568, 1568), Image.LANCZOS)
    if preview_img.mode == "RGBA":
        preview_img = preview_img.convert("RGB")
    preview_img.save(buf, format="JPEG", quality=85)
    text = render_outcome(outcome, kind="Image", path=out, saved=True, existed=existed)
    first, _, rest = text.partition("\n")
    text = f"{first}\nSize: {img.width}x{img.height} px" + (f"\n{rest}" if rest else "")
    if before_stats is not None:
        text += "\n" + _tone_check_line(before_stats, _tone_stats(img))
    return _EditedImage(text, True, out, img.width, img.height, buf.getvalue())


async def _targets(ops: list, base: str) -> dict[str, str]:
    """Each convert_format's output, resolved as a write: the extension it
    gives the output is a path the proxy judges, like any other."""
    found: dict[str, str] = {}
    for _, op in ops:
        if op.get("op") != "convert_format" or not op.get("format"):
            continue
        name = _FORMATS.get(str(op["format"]).strip().lstrip(".").casefold())
        if name and name not in found:
            found[name] = await _resolve_path(str(Path(base).with_suffix(_SUFFIX[name])), writing=True)
    return found


async def handle_edit_image(args: dict) -> str:
    from batch import prepare_batch
    from results import OpResult, render_rejected

    best_effort = bool(args.get("best_effort", False))
    prepared = prepare_batch(args.get("operations"), CATALOG)
    answer = CATALOG.answer_help(prepared.help_ops)
    if prepared.help_ops and not prepared.ops and not prepared.problems:
        return answer
    ops, problems, notes = CATALOG.resolve_batch(prepared.ops)
    problems = [*prepared.problems, *problems]
    if problems and not best_effort:
        return _joined(render_rejected(problems, kind="Image", path=args["path"], total=prepared.total), answer)
    output_path = args.get("output_path")
    # No output_path: an in-place edit, so the input resolves as a write for
    # the proxy's write rules to apply; with one the input stays a read.
    path = await _resolve_path(args["path"], writing=not output_path)
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"
    out = await _resolve_path(output_path, writing=True) if output_path else path
    ops, path_problems = await CATALOG.preresolve(ops, main_path=out)
    if path_problems and not best_effort:
        return _joined(render_rejected([*problems, *path_problems], kind="Image", path=out, total=prepared.total,
                                       existed=await asyncio.to_thread(os.path.exists, out)), answer)
    try:
        targets = await _targets(ops, out)
    except Exception as exc:  # noqa: BLE001  # the proxy's refusal, whatever its type, answers the call
        return f"Nothing saved: {_to_agents_relative(out)} was not written. The converted file's path was refused: {exc}"
    rejected = [OpResult.from_error(p) for p in [*problems, *path_problems]]
    edited = await _image_job(functools.partial(
        _edit_image_core, path, out, ops, best_effort=best_effort, new_file=out != path,
        total=prepared.total, rejected=rejected, pre_notes=notes, targets=targets))
    if not edited.saved:
        return _joined(edited.text, answer)
    await _notify_file_written(edited.out)
    await _push_image_preview(edited.preview, "image/jpeg", f"Edited: {Path(edited.out).name}")
    # A download link to the full resolution.
    session_id, auth = _current_session()
    if PROXY_URL and session_id and auth:
        try:
            async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
                await client.post(
                    f"{PROXY_URL}/v1/hooks/file",
                    json={
                        "session_id": session_id,
                        "path": _to_agents_relative(edited.out),
                        "filename": Path(edited.out).name,
                        "description": f"Full resolution ({edited.width}x{edited.height})",
                    },
                    headers={"Authorization": auth},
                )
        except httpx.HTTPError:
            pass
    return _joined(edited.text, answer)


def _joined(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# analyze_image
# ---------------------------------------------------------------------------


async def handle_analyze_image(args: dict) -> str:
    """Analyze an image — file info, EXIF, histogram, exposure, suggestions."""
    path = await _resolve_path(args["path"])
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"
    return await _image_job(_analyze_image_core, path)


def _analyze_image_core(path: str) -> str:
    """In a worker thread: the analysis of the image at ``path``."""
    import numpy as np
    from PIL import Image, ImageStat
    from PIL.ExifTags import TAGS

    raw_img = Image.open(path)
    img = raw_img.convert("RGB")
    arr = np.array(img, dtype=np.float32)
    stat = ImageStat.Stat(img)

    # --- File info ---
    file_size = os.path.getsize(path)
    if file_size > 1_000_000:
        size_str = f"{file_size / 1_000_000:.1f} MB"
    else:
        size_str = f"{file_size / 1_000:.0f} KB"

    result = [
        f"**Image Analysis**: {Path(path).name}",
        f"**Format**: {raw_img.format or Path(path).suffix.upper().lstrip('.')} | "
        f"**Mode**: {raw_img.mode} | **Size**: {raw_img.width}x{raw_img.height} | "
        f"**File**: {size_str}",
    ]

    # --- EXIF data ---
    exif_info = []
    try:
        exif_data = raw_img._getexif()
        if exif_data:
            tag_names = {}
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                tag_names[tag_name] = value

            if "Make" in tag_names or "Model" in tag_names:
                camera = f"{tag_names.get('Make', '')} {tag_names.get('Model', '')}".strip()
                exif_info.append(f"Camera: {camera}")
            if "DateTime" in tag_names:
                exif_info.append(f"Date: {tag_names['DateTime']}")
            if "ExposureTime" in tag_names:
                et = tag_names["ExposureTime"]
                if hasattr(et, "numerator"):
                    exif_info.append(f"Exposure: {et.numerator}/{et.denominator}s")
            if "FNumber" in tag_names:
                fn = tag_names["FNumber"]
                if hasattr(fn, "numerator"):
                    exif_info.append(f"f/{fn.numerator / fn.denominator:.1f}")
            if "ISOSpeedRatings" in tag_names:
                exif_info.append(f"ISO {tag_names['ISOSpeedRatings']}")
            if "FocalLength" in tag_names:
                fl = tag_names["FocalLength"]
                if hasattr(fl, "numerator"):
                    exif_info.append(f"{fl.numerator / fl.denominator:.0f}mm")
            # GPS
            if "GPSInfo" in tag_names:
                gps = tag_names["GPSInfo"]
                try:
                    def _to_degrees(v):
                        d, m, s = v
                        if hasattr(d, "numerator"):
                            d = d.numerator / d.denominator
                        if hasattr(m, "numerator"):
                            m = m.numerator / m.denominator
                        if hasattr(s, "numerator"):
                            s = s.numerator / s.denominator
                        return d + m / 60 + s / 3600
                    lat = _to_degrees(gps[2])
                    if gps[1] == "S":
                        lat = -lat
                    lon = _to_degrees(gps[4])
                    if gps[3] == "W":
                        lon = -lon
                    exif_info.append(f"GPS: {lat:.6f}, {lon:.6f}")
                except Exception:  # noqa: BLE001  # EXIF values come in whatever shape a camera wrote them
                    exif_info.append("GPS: present")
    except Exception:  # noqa: BLE001  # Pillow on the tool's image raises anything from a decoder
        pass

    if exif_info:
        result.append(f"**EXIF**: {' | '.join(exif_info)}")

    # --- Luminance analysis ---
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    lum_flat = lum.flatten()
    p = np.percentile(lum_flat, [1, 5, 25, 50, 75, 95, 99])

    shadows_pct = (lum_flat < 64).sum() / lum_flat.size * 100
    midtones_pct = (
        ((lum_flat >= 64) & (lum_flat < 192)).sum() / lum_flat.size * 100
    )
    highlights_pct = (lum_flat >= 192).sum() / lum_flat.size * 100

    r_mean, g_mean, b_mean = stat.mean
    dr = float(p[6] - p[0])
    clip_black_pct = (lum_flat <= 5).sum() / lum_flat.size * 100
    clip_white_pct = (lum_flat >= 250).sum() / lum_flat.size * 100

    result.extend([
        "",
        "**Luminance Distribution**:",
        f"  Darkest 1%: {p[0]:.0f}  |  5%: {p[1]:.0f}  |  25%: {p[2]:.0f}",
        f"  Median: {p[3]:.0f}  |  Mean: {lum_flat.mean():.0f}",
        f"  75%: {p[4]:.0f}  |  95%: {p[5]:.0f}  |  Brightest 1%: {p[6]:.0f}",
        f"  Dynamic range: {dr:.0f} (ideal: 200+)",
        "",
        "**Zone Balance**:",
        f"  Shadows (<64): {shadows_pct:.1f}%",
        f"  Midtones (64-192): {midtones_pct:.1f}%",
        f"  Highlights (>192): {highlights_pct:.1f}%",
        f"  Clipped: blacks ≤5: {clip_black_pct:.1f}%"
        f"  |  whites ≥250: {clip_white_pct:.1f}%",
        "",
        f"**Color**: R={r_mean:.0f} G={g_mean:.0f} B={b_mean:.0f}",
    ])

    temp_hint = "neutral"
    if r_mean > b_mean + 15:
        temp_hint = "warm (reddish)"
    elif b_mean > r_mean + 15:
        temp_hint = "cool (bluish)"
    result.append(f"**Temperature**: {temp_hint}")
    result.append(
        f"**Overall contrast** (stddev): "
        f"{float(stat.stddev[0] + stat.stddev[1] + stat.stddev[2]) / 3:.1f}"
    )

    # --- Suggestions ---
    issues = []
    if p[0] > 30:
        issues.append(f"Blacks are lifted/faded (black point at {p[0]:.0f})")
    if p[6] < 220:
        issues.append(f"Highlights are compressed (white point at {p[6]:.0f})")
    if float(stat.stddev[0] + stat.stddev[1] + stat.stddev[2]) / 3 < 40:
        issues.append("Low contrast (flat image)")
    if shadows_pct > 50:
        issues.append(f"Underexposed ({shadows_pct:.0f}% shadows)")
    if highlights_pct > 40:
        issues.append(f"Overexposed ({highlights_pct:.0f}% highlights)")
    if clip_white_pct > 3:
        issues.append(
            f"Blown highlights ({clip_white_pct:.0f}% clipped at white) — "
            "detail there is unrecoverable; darken mids, don't chase recovery"
        )
    if clip_black_pct > 3:
        issues.append(
            f"Crushed blacks ({clip_black_pct:.0f}% clipped at black) — "
            "lift with exposure/shadows; the deepest detail may be gone"
        )
    if issues:
        result.append("")
        result.append(f"**Suggested fixes**: {'; '.join(issues)}")

    return "\n".join(result)
