"""Image editing and analysis handlers for the file-tools MCP.

Lightroom-style editing with research-backed algorithms:
- Frequency-separation highlights/shadows (darktable shadhi)
- Dark channel prior dehaze (He et al. 2011)
- Chroma-only denoise (YCbCr)
- Sigmoid split toning (darktable colorbalancergb)
- Cosine-bell HSL per-color adjustment
"""

import asyncio
import contextlib
import contextvars
import functools
import io
import os
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from shared import (
    _RESOLVE_ERR_PREFIX,
    HOOK_TIMEOUT,
    PROXY_URL,
    _checked_resolved,
    _current_session,
    _dropped_note,
    _normalize_operations,
    _notify_file_written,
    _op_type,
    _push_image_preview,
    _resolve_path,
    _to_agents_relative,
    logger,
    safe_open_write,
)

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


def _op_resize(job: _ImageEdit, op: dict) -> None:
    from PIL import Image

    w = int(op.get("width", job.img.width))
    h = int(op.get("height", job.img.height))
    if op.get("maintain_aspect", True):
        job.img.thumbnail((w, h), Image.LANCZOS)
    else:
        job.img = job.img.resize((w, h), Image.LANCZOS)


def _op_crop(job: _ImageEdit, op: dict) -> None:
    left = int(op.get("left", 0))
    top = int(op.get("top", 0))
    right = int(op.get("right", job.img.width))
    bottom = int(op.get("bottom", job.img.height))
    job.img = job.img.crop((left, top, right, bottom))


def _op_rotate(job: _ImageEdit, op: dict) -> None:
    from PIL import Image

    degrees = float(op.get("degrees", 0))
    expand = op.get("expand", True)
    job.img = job.img.rotate(
        -degrees, expand=expand, resample=Image.BICUBIC
    )


def _op_flip(job: _ImageEdit, op: dict) -> None:
    from PIL import Image

    direction = op.get("direction", "horizontal")
    if direction == "horizontal":
        job.img = job.img.transpose(Image.FLIP_LEFT_RIGHT)
    else:
        job.img = job.img.transpose(Image.FLIP_TOP_BOTTOM)


def _op_brightness(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Brightness(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_contrast(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Contrast(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_saturation(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Color(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_sharpness(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageEnhance

    job.img = ImageEnhance.Sharpness(job.img).enhance(
        float(op.get("factor", 1.0))
    )


def _op_grayscale(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageOps

    job.img = ImageOps.grayscale(job.img)
    job.img = job.img.convert("RGB")


def _op_sepia(job: _ImageEdit, op: dict) -> None:
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


def _op_invert(job: _ImageEdit, op: dict) -> None:
    from PIL import Image, ImageOps

    if job.img.mode == "RGBA":
        r, g, b, a = job.img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageOps.invert(rgb)
        job.img = Image.merge("RGBA", (*rgb.split(), a))
    else:
        job.img = ImageOps.invert(job.img.convert("RGB"))


def _op_blur(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageFilter

    radius = float(op.get("radius", 2))
    job.img = job.img.filter(ImageFilter.GaussianBlur(radius=radius))


def _op_sharpen(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.SHARPEN)


def _op_edge_enhance(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.EDGE_ENHANCE_MORE)


def _op_emboss(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageFilter

    job.img = job.img.filter(ImageFilter.EMBOSS)


def _op_auto_enhance(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageEnhance, ImageOps

    job.img = ImageOps.autocontrast(job.img)
    job.img = ImageEnhance.Color(job.img).enhance(1.1)
    job.img = ImageEnhance.Sharpness(job.img).enhance(1.1)


def _op_color_temperature(job: _ImageEdit, op: dict) -> None:
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


def _op_exposure(job: _ImageEdit, op: dict) -> None:
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


def _op_add_text(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(job.img)
    text = op.get("text", "")
    x = int(op.get("x", 10))
    y = int(op.get("y", 10))
    font_size = int(op.get("font_size", 24))
    color = op.get("color", "#FFFFFF")
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            font_size,
        )
    except OSError:
        font = ImageFont.load_default()
    draw.text((x, y), text, fill=color, font=font)


def _op_add_border(job: _ImageEdit, op: dict) -> None:
    from PIL import ImageOps

    border_w = int(op.get("width", 5))
    color = op.get("color", "#000000")
    job.img = ImageOps.expand(job.img, border=border_w, fill=color)


def _op_paste_image(job: _ImageEdit, op: dict) -> None:
    from PIL import Image

    overlay_path = _checked_resolved(op["image_path"])
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


def _op_add_watermark(job: _ImageEdit, op: dict) -> None:
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


def _op_highlights(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image, ImageFilter

    amount = float(op.get("amount", 0))
    if amount != 0:
        arr = np.array(job.img, dtype=np.float32) / 255.0
        lum = (
            0.299 * arr[:, :, 0]
            + 0.587 * arr[:, :, 1]
            + 0.114 * arr[:, :, 2]
        )
        blur_r = max(3, int(job.img.width * 0.025))
        blur_img = job.img.filter(
            ImageFilter.GaussianBlur(radius=blur_r)
        )
        arr_blur = np.array(blur_img, dtype=np.float32) / 255.0
        inv_blur = 1.0 - arr_blur
        overlay = np.where(
            arr > 0.5,
            1.0 - (1.0 - 2 * (arr - 0.5)) * (1.0 - inv_blur),
            2.0 * arr * inv_blur,
        )
        compress = 0.5
        lum_norm = lum / (1.0 - compress + 1e-7)
        mask = np.clip(1.0 - (1.0 - lum_norm), 0, 1) ** 2
        mask = mask[:, :, np.newaxis]
        strength = (
            (abs(amount) / 100.0) ** 1.5
            * np.sign(amount)
            * 0.6
        )
        if amount < 0:
            arr = arr * (1 - mask * abs(strength)) + overlay * mask * abs(strength)
        else:
            arr = arr + mask * strength * (1.0 - arr) * 0.5
        job.img = Image.fromarray(
            np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
        )


def _op_shadows(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image, ImageFilter

    amount = float(op.get("amount", 0))
    if amount != 0:
        arr = np.array(job.img, dtype=np.float32) / 255.0
        lum = (
            0.299 * arr[:, :, 0]
            + 0.587 * arr[:, :, 1]
            + 0.114 * arr[:, :, 2]
        )
        blur_r = max(3, int(job.img.width * 0.025))
        blur_img = job.img.filter(
            ImageFilter.GaussianBlur(radius=blur_r)
        )
        arr_blur = np.array(blur_img, dtype=np.float32) / 255.0
        inv_blur = 1.0 - arr_blur
        overlay = np.where(
            arr > 0.5,
            1.0 - (1.0 - 2 * (arr - 0.5)) * (1.0 - inv_blur),
            2.0 * arr * inv_blur,
        )
        compress = 0.5
        mask = (
            np.clip(1.0 - lum / (compress + 1e-7), 0, 1) ** 2
        )
        mask = mask[:, :, np.newaxis]
        strength = (abs(amount) / 100.0) ** 1.5 * 0.6
        if amount > 0:
            arr = arr * (1 - mask * strength) + overlay * mask * strength
        else:
            arr = (
                arr * (1 - mask * strength * 0.5)
                + (arr * arr) * mask * strength * 0.5
            )
        job.img = Image.fromarray(
            np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
        )


def _op_blacks(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image

    level = float(op.get("level", 0))
    arr = np.array(job.img, dtype=np.float32) / 255.0
    if level > 0:
        lift = level / 100.0 * 0.08
        arr = lift + arr * (1.0 - lift)
    elif level < 0:
        power = 1.0 + abs(level) / 100.0 * 0.5
        arr = arr**power
    job.img = Image.fromarray(
        np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
    )


def _op_whites(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image

    level = float(op.get("level", 0))
    arr = np.array(job.img, dtype=np.float32) / 255.0
    if level > 0:
        power = 1.0 - level / 100.0 * 0.15
        mask = np.clip((arr - 0.6) / 0.4, 0, 1)
        arr = arr * (1 - mask) + (arr**power) * mask
    elif level < 0:
        compress = 1.0 - abs(level) / 100.0 * 0.15
        arr = np.where(
            arr > compress,
            compress + (arr - compress) * 0.3,
            arr,
        )
    job.img = Image.fromarray(
        np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
    )


def _op_clarity(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image, ImageFilter

    amount = float(op.get("amount", 20))
    radius = max(3, int(job.img.width * 0.02))
    scale = amount / 100.0 * 0.4
    arr = np.array(job.img, dtype=np.float32)
    lum = (
        0.299 * arr[:, :, 0]
        + 0.587 * arr[:, :, 1]
        + 0.114 * arr[:, :, 2]
    )
    lum_img = Image.fromarray(lum.astype(np.uint8), "L")
    lum_blur = np.array(
        lum_img.filter(ImageFilter.GaussianBlur(radius=radius)),
        dtype=np.float32,
    )
    detail = lum - lum_blur
    midtone_w = np.exp(
        -((lum / 255.0 - 0.5) ** 2) / (2 * 0.2**2)
    )
    lum_boost = detail * scale * midtone_w
    lum_safe = np.maximum(lum, 1.0)
    ratio = ((lum + lum_boost) / lum_safe)[:, :, np.newaxis]
    arr = arr * ratio
    job.img = Image.fromarray(
        np.clip(arr, 0, 255).astype(np.uint8), job.img.mode
    )


def _op_dehaze(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image
    from scipy.ndimage import minimum_filter, uniform_filter

    strength = float(op.get("strength", 0.3))
    arr = np.array(job.img, dtype=np.float32) / 255.0
    dark = np.min(arr, axis=2)
    win = max(5, int(min(job.img.width, job.img.height) * 0.01))
    try:
        dark_channel = minimum_filter(dark, size=win)
    except Exception:  # noqa: BLE001  # scipy's filter on an odd window raises anything, the raw channel stands
        dark_channel = dark
    n_pixels = max(1, int(dark_channel.size * 0.001))
    flat_dark = dark_channel.flatten()
    threshold = np.partition(flat_dark, -n_pixels)[-n_pixels]
    bright_mask = dark_channel >= threshold
    A = np.array(
        [arr[:, :, c][bright_mask].mean() for c in range(3)]
    )
    A = np.clip(A, 0.5, 1.0)
    norm = arr / (A[np.newaxis, np.newaxis, :] + 1e-7)
    t_map = 1.0 - strength * np.min(norm, axis=2)
    t_map = np.clip(t_map, 0.1, 1.0)
    with contextlib.suppress(Exception):
        t_map = uniform_filter(t_map, size=win * 4)
    t_3d = t_map[:, :, np.newaxis]
    arr = (arr - A[np.newaxis, np.newaxis, :]) / np.maximum(
        t_3d, 0.1
    ) + A[np.newaxis, np.newaxis, :]
    job.img = Image.fromarray(
        np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
    )


def _op_split_tone(job: _ImageEdit, op: dict) -> None:
    import colorsys

    import numpy as np
    from PIL import Image

    arr = np.array(job.img, dtype=np.float32) / 255.0
    lum = (
        0.299 * arr[:, :, 0]
        + 0.587 * arr[:, :, 1]
        + 0.114 * arr[:, :, 2]
    )
    fulcrum = 0.1845
    x_offset = lum - fulcrum
    x_norm = x_offset / (fulcrum + 1e-7)
    weight = 4.0
    with np.errstate(over="ignore"):
        shadow_mask = 1.0 / (
            1.0
            + np.exp(np.clip(x_norm * weight, -20, 20))
        )
        highlight_mask = 1.0 / (
            1.0
            + np.exp(np.clip(-x_norm * weight, -20, 20))
        )
    s_comp = 1.0 - shadow_mask
    h_comp = 1.0 - highlight_mask
    midtone_mask = (
        np.exp(-(x_offset**2) * weight / 4.0)
        * s_comp**2
        * h_comp**2
        * 8.0
    )

    for zone_name, mask in [
        ("shadows", shadow_mask),
        ("midtones", midtone_mask),
        ("highlights", highlight_mask),
    ]:
        zone = op.get(zone_name)
        if not zone:
            continue
        hue = float(zone.get("hue", 0)) / 360.0
        sat = float(zone.get("saturation", 50)) / 100.0
        strength_z = float(zone.get("strength", 0.15))
        tr, tg, tb = colorsys.hls_to_rgb(hue, 0.5, sat)
        tint = (
            np.array([tr, tg, tb], dtype=np.float32) - 0.5
        )
        zone_mask = mask[:, :, np.newaxis] * strength_z
        arr = arr + tint[np.newaxis, np.newaxis, :] * zone_mask

    job.img = Image.fromarray(
        np.clip(arr * 255, 0, 255).astype(np.uint8), job.img.mode
    )


def _op_hsl_adjust(job: _ImageEdit, op: dict) -> None:
    import numpy as np
    from PIL import Image

    arr = np.array(job.img, dtype=np.float32) / 255.0
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin
    light = (cmax + cmin) / 2
    sat = np.where(
        delta < 1e-7,
        0,
        delta / (1 - np.abs(2 * light - 1) + 1e-7),
    )
    hue = np.zeros_like(delta)
    mask_r = (cmax == r) & (delta > 1e-7)
    mask_g = (cmax == g) & (delta > 1e-7)
    mask_b = (cmax == b) & (delta > 1e-7)
    hue[mask_r] = (
        60
        * (
            (g[mask_r] - b[mask_r])
            / (delta[mask_r] + 1e-7)
        )
    ) % 360
    hue[mask_g] = (
        60
        * (
            (b[mask_g] - r[mask_g])
            / (delta[mask_g] + 1e-7)
        )
        + 120
    )
    hue[mask_b] = (
        60
        * (
            (r[mask_b] - g[mask_b])
            / (delta[mask_b] + 1e-7)
        )
        + 240
    )
    hue = hue % 360

    color_ranges = {
        "red": (0, 45),
        "orange": (30, 40),
        "yellow": (60, 40),
        "green": (120, 50),
        "aqua": (180, 45),
        "blue": (240, 45),
        "purple": (270, 35),
        "magenta": (330, 40),
    }
    sat_gate = np.clip(sat * 5, 0, 1)

    for color_name, (center, width) in color_ranges.items():
        adj = op.get(color_name)
        if not adj:
            continue
        h_shift = float(adj.get("hue", 0))
        s_shift = float(adj.get("saturation", 0)) / 100.0
        l_shift = float(adj.get("lightness", 0)) / 100.0
        diff = np.minimum(
            np.abs(hue - center),
            360 - np.abs(hue - center),
        )
        w = (
            np.where(
                diff < width,
                0.5 * (1 + np.cos(np.pi * diff / width)),
                0.0,
            )
            * sat_gate
        )
        if h_shift:
            hue = hue + h_shift * w
            hue = hue % 360
        if s_shift:
            sat = sat + s_shift * 0.5 * w
            sat = np.clip(sat, 0, 1)
        if l_shift:
            light = light + l_shift * 0.3 * w
            light = np.clip(light, 0, 1)

    c = (1 - np.abs(2 * light - 1)) * sat
    x = c * (1 - np.abs((hue / 60) % 2 - 1))
    m = light - c / 2
    h_sector = (hue / 60).astype(int) % 6
    r_out = np.where(
        h_sector == 0, c,
        np.where(h_sector == 1, x,
        np.where(h_sector == 4, x,
        np.where(h_sector == 5, c, 0))),
    )
    g_out = np.where(
        h_sector == 0, x,
        np.where(h_sector == 1, c,
        np.where(h_sector == 2, c,
        np.where(h_sector == 3, x, 0))),
    )
    b_out = np.where(
        h_sector == 2, x,
        np.where(h_sector == 3, c,
        np.where(h_sector == 4, c,
        np.where(h_sector == 5, x, 0))),
    )
    arr_out = np.stack(
        [(r_out + m), (g_out + m), (b_out + m)], axis=2
    )
    job.img = Image.fromarray(
        np.clip(arr_out * 255, 0, 255).astype(np.uint8), "RGB"
    )


def _op_denoise(job: _ImageEdit, op: dict) -> None:
    from PIL import Image, ImageFilter

    strength = int(op.get("strength", 3))
    ycbcr = job.img.convert("YCbCr")
    y, cb, cr = ycbcr.split()
    radius = max(1, strength * 0.8)
    cb = cb.filter(ImageFilter.GaussianBlur(radius=radius))
    cr = cr.filter(ImageFilter.GaussianBlur(radius=radius))
    if strength >= 4:
        y = y.filter(ImageFilter.GaussianBlur(radius=0.3))
    ycbcr_out = Image.merge("YCbCr", (y, cb, cr))
    job.img = ycbcr_out.convert("RGB")


# ---------------------------------------------------------------------------
# Edit — background removal, blur region, compress, convert
# ---------------------------------------------------------------------------


def _op_remove_background(job: _ImageEdit, op: dict) -> None:
    from PIL import Image
    from rembg import remove

    job.img = remove(job.img, session=_get_rembg_session())
    # rembg returns RGBA with transparent background
    if job.img.mode != "RGBA":
        job.img = job.img.convert("RGBA")
    # If user wants a specific background color instead of transparent
    bg_color = op.get("background_color")
    if bg_color:
        c = str(bg_color).lstrip("#")
        r, g, b = int(c[:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        bg = Image.new("RGB", job.img.size, (r, g, b))
        bg.paste(job.img, mask=job.img.split()[3])
        job.img = bg


def _op_blur_region(job: _ImageEdit, op: dict) -> None:
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
    radius = float(op.get("radius") or op.get("intensity") or op.get("strength") or 15)
    # Crop region, blur, paste back
    region = job.img.crop((left, top_coord, right, bottom))
    region = region.filter(
        ImageFilter.GaussianBlur(radius=radius)
    )
    job.img.paste(region, (left, top_coord))


def _op_compress(job: _ImageEdit, op: dict) -> None:
    # Set compression for the final save
    quality = int(op.get("quality", 75))
    target_format = op.get("format", "").upper()
    if target_format == "JPG":
        target_format = "JPEG"
    # Store for save step (handled below)
    job.compress_quality = quality
    if target_format:
        job.compress_format = target_format


def _op_convert_format(job: _ImageEdit, op: dict) -> None:
    # Convert to a different format by changing output path
    target_format = op.get("format", "").lower()
    if target_format:
        if target_format == "jpg":
            target_format = "jpeg"
        new_ext = target_format if target_format != "jpeg" else "jpg"
        job.out = str(Path(job.out).with_suffix(f".{new_ext}"))


_HANDLERS = {
    "resize": _op_resize,
    "crop": _op_crop,
    "rotate": _op_rotate,
    "flip": _op_flip,
    "brightness": _op_brightness,
    "contrast": _op_contrast,
    "saturation": _op_saturation,
    "sharpness": _op_sharpness,
    "grayscale": _op_grayscale,
    "sepia": _op_sepia,
    "invert": _op_invert,
    "blur": _op_blur,
    "sharpen": _op_sharpen,
    "edge_enhance": _op_edge_enhance,
    "emboss": _op_emboss,
    "auto_enhance": _op_auto_enhance,
    "color_temperature": _op_color_temperature,
    "exposure": _op_exposure,
    "add_text": _op_add_text,
    "add_border": _op_add_border,
    "paste_image": _op_paste_image,
    "add_watermark": _op_add_watermark,
    "highlights": _op_highlights,
    "shadows": _op_shadows,
    "blacks": _op_blacks,
    "whites": _op_whites,
    "clarity": _op_clarity,
    "dehaze": _op_dehaze,
    "split_tone": _op_split_tone,
    "hsl_adjust": _op_hsl_adjust,
    "denoise": _op_denoise,
    "remove_background": _op_remove_background,
    "blur_region": _op_blur_region,
    "compress": _op_compress,
    "convert_format": _op_convert_format,
}


async def handle_edit_image(args: dict) -> str:
    output_path = args.get("output_path")
    # No output_path = in-place edit: the input IS the write target, so it
    # must resolve as a write for the proxy's write-RBAC to fire (an editor
    # could otherwise edit a /knowledge image in place through the
    # read-resolved path). With an output_path the input stays read-only.
    path = await _resolve_path(args["path"], writing=not output_path)
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"

    if output_path:
        out = await _resolve_path(output_path, writing=True)
    else:
        out = path

    ops, dropped = _normalize_operations(args.get("operations"))
    if args.get("operations") and not ops:
        # A non-empty operations arg that normalizes to nothing means the
        # caller's shape was unusable (double-encoded string, bare strings,
        # …). Saving an unchanged copy here would read as success — fail
        # loud instead.
        return (
            "Error: 'operations' could not be parsed into any operation "
            "(expected a list of objects like {\"type\": \"resize\", ...}); "
            "no edit was applied"
        )
    await _preresolve_paste_ops(ops)
    edited = await _image_job(_edit_image_core, path, out, ops)
    if isinstance(edited, str):
        return edited
    out = edited.out

    # Flush platform-cache write to remote satellite (no-op for local).
    await _notify_file_written(out)

    # Push inline image preview
    await _push_image_preview(
        edited.preview, "image/jpeg", f"Edited: {Path(out).name}"
    )

    # Push file download link for full resolution
    session_id, auth = _current_session()
    if PROXY_URL and session_id and auth:
        agents_rel = _to_agents_relative(out)
        try:
            async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
                await client.post(
                    f"{PROXY_URL}/v1/hooks/file",
                    json={
                        "session_id": session_id,
                        "path": agents_rel,
                        "filename": Path(out).name,
                        "description": f"Full resolution ({edited.width}x{edited.height})",
                    },
                    headers={"Authorization": auth},
                )
        except httpx.HTTPError:
            pass

    result = (f"Image saved: {_to_agents_relative(out)} "
              f"({edited.width}x{edited.height}, {len(ops)} operations applied)")
    result += _dropped_note(dropped)
    if edited.tone_check is not None:
        result += "\n" + edited.tone_check
    return result


async def _preresolve_paste_ops(ops: list) -> None:
    """Every paste_image op's ``image_path`` resolved in op order before the
    thread starts (the lookup is the proxy's, per session, async). A lookup
    that fails becomes the marker ``_checked_resolved`` raises at that op's
    turn, so the failure is reported for that op with the lookup's own text;
    an op without the key keeps raising for it there."""
    for op in ops:
        if _op_type(op) == "paste_image" and "image_path" in op:
            try:
                op["image_path"] = await _resolve_path(op["image_path"])
            except Exception as exc:  # noqa: BLE001  # the op reports it at its turn, as when it resolved there
                op["image_path"] = _RESOLVE_ERR_PREFIX + str(exc)


@dataclass
class _EditedImage:
    """What the edit's thread hands back to the loop."""
    out: str
    width: int
    height: int
    preview: bytes
    tone_check: str | None


def _edit_image_core(path: str, out: str, ops: list) -> "_EditedImage | str":
    """In a worker thread: open the image, apply every operation, save it
    beneath the mount and encode the chat preview. An operation that fails
    or an output format nothing writes is the answer's text instead."""
    from PIL import Image, ImageOps

    img = Image.open(path)
    # Apply EXIF orientation so pixel data matches display orientation.
    # Without this, manual rotations conflict with the EXIF orientation tag.
    img = ImageOps.exif_transpose(img) or img

    # Ensure RGB for most operations
    if img.mode in ("RGBA", "P"):
        has_alpha = img.mode == "RGBA" or (
            img.mode == "P" and "transparency" in img.info
        )
        if not has_alpha:
            img = img.convert("RGB")

    has_tonal_op = any(_op_type(op) in _TONAL_OPS for op in ops)
    # The tonal operations rebuild the image from 8-bit data in its own mode,
    # which a 1-bit image cannot take.
    if has_tonal_op and img.mode == "1":
        img = img.convert("L")
    before_stats = _tone_stats(img) if has_tonal_op else None

    job = _ImageEdit(img, out)
    for op in ops:
        ot = _op_type(op)
        try:
            handler = _HANDLERS.get(ot) if isinstance(ot, str) else None
            if handler is not None:
                handler(job, op)
            else:
                logger.warning(
                    f"edit_image: unknown operation '{ot}', skipping"
                )
        except Exception as exc:  # noqa: BLE001  # an op's failure becomes its error line, the other ops still run
            return f"Error in operation '{ot}': {exc}"
    img, out = job.img, job.out

    # Save
    # Check for compress operation overrides
    compress_quality = job.compress_quality
    compress_format = job.compress_format

    save_format = Path(out).suffix.lstrip(".").upper()
    if save_format == "JPG":
        save_format = "JPEG"
    if compress_format:
        save_format = compress_format
    if save_format not in ("JPEG", "PNG", "WEBP", "TIFF", "BMP"):
        # A file object needs the format spelled out: the one Pillow
        # registers for the suffix, else the output is refused.
        save_format = Image.registered_extensions().get(Path(out).suffix.lower())
        if not save_format:
            return f"Error: unsupported output format '{Path(out).suffix}'"
    if save_format == "JPEG" and img.mode == "RGBA":
        img = img.convert("RGB")
    save_kwargs = {}
    if save_format == "JPEG":
        save_kwargs["quality"] = compress_quality or 95
        save_kwargs["subsampling"] = 0
    elif save_format == "PNG":
        save_kwargs["compress_level"] = 6
    elif save_format == "WEBP":
        save_kwargs["quality"] = compress_quality or 95
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
    tone_check = (_tone_check_line(before_stats, _tone_stats(img))
                  if before_stats is not None else None)
    return _EditedImage(out, img.width, img.height, buf.getvalue(), tone_check)


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


# ---------------------------------------------------------------------------
# OCR on images
# ---------------------------------------------------------------------------


async def handle_ocr_image(args: dict) -> str:
    """Extract text from an image using tesseract OCR."""
    import subprocess

    path = await _resolve_path(args["path"])
    if not await asyncio.to_thread(os.path.exists, path):
        return f"Error: File not found: {args['path']}"

    language = args.get("language", "eng")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["tesseract", path, "stdout", "-l", language],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return f"OCR error: {result.stderr.strip()}"

        text = result.stdout.strip()
        if not text:
            return f"No text detected in {_to_agents_relative(path)}. The image may not contain text or may need preprocessing."

        return (
            f"**OCR Result** ({Path(path).name}, language: {language}):\n\n{text}"
        )
    except subprocess.TimeoutExpired:
        return "OCR timed out (60s limit)"
    except FileNotFoundError:
        return "Error: tesseract-ocr is not installed in this environment"
