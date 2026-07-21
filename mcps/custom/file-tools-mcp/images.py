"""Image editing and analysis handlers for the file-tools MCP.

Lightroom-style editing with research-backed algorithms:
- Frequency-separation highlights/shadows (darktable shadhi)
- Dark channel prior dehaze (He et al. 2011)
- Chroma-only denoise (YCbCr)
- Sigmoid split toning (darktable colorbalancergb)
- Cosine-bell HSL per-color adjustment
"""

import contextlib
import io
from pathlib import Path

import httpx

from shared import (
    HOOK_TIMEOUT,
    PROXY_URL,
    _current_session,
    _dropped_note,
    _normalize_operations,
    _op_type,
    _notify_file_written,
    _push_image_preview,
    _resolve_path,
    _to_agents_relative,
    logger,
)


# rembg background-removal model. We use ``isnet-general-use`` (newer than
# rembg's default ``u2net`` — noticeably cleaner edges/hair at the same ~176MB
# size + CPU-speed class). The model is pre-baked into the Docker image (see the
# Dockerfile: ``U2NET_HOME`` + ``new_session('isnet-general-use')``), so it never
# downloads at runtime. The session is created lazily once and reused across
# calls — loading the model is expensive, so recreating it per call would reload
# ~176MB every time.
_REMBG_MODEL = "isnet-general-use"
_rembg_session = None


def _get_rembg_session():
    """Lazily create and cache the rembg background-removal session."""
    global _rembg_session
    if _rembg_session is None:
        from rembg import new_session
        _rembg_session = new_session(_REMBG_MODEL)
    return _rembg_session


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


async def handle_edit_image(args: dict) -> str:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

    output_path = args.get("output_path")
    # No output_path = in-place edit: the input IS the write target, so it
    # must resolve as a write for the proxy's write-RBAC to fire (an editor
    # could otherwise edit a /knowledge image in place through the
    # read-resolved path). With an output_path the input stays read-only.
    path = _resolve_path(args["path"], writing=not output_path)
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

    if output_path:
        out = _resolve_path(output_path, writing=True)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
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
    before_stats = _tone_stats(img) if has_tonal_op else None

    for op in ops:
        ot = _op_type(op)
        try:
            if ot == "resize":
                w = int(op.get("width", img.width))
                h = int(op.get("height", img.height))
                if op.get("maintain_aspect", True):
                    img.thumbnail((w, h), Image.LANCZOS)
                else:
                    img = img.resize((w, h), Image.LANCZOS)

            elif ot == "crop":
                left = int(op.get("left", 0))
                top = int(op.get("top", 0))
                right = int(op.get("right", img.width))
                bottom = int(op.get("bottom", img.height))
                img = img.crop((left, top, right, bottom))

            elif ot == "rotate":
                degrees = float(op.get("degrees", 0))
                expand = op.get("expand", True)
                img = img.rotate(
                    -degrees, expand=expand, resample=Image.BICUBIC
                )

            elif ot == "flip":
                direction = op.get("direction", "horizontal")
                if direction == "horizontal":
                    img = img.transpose(Image.FLIP_LEFT_RIGHT)
                else:
                    img = img.transpose(Image.FLIP_TOP_BOTTOM)

            elif ot == "brightness":
                img = ImageEnhance.Brightness(img).enhance(
                    float(op.get("factor", 1.0))
                )

            elif ot == "contrast":
                img = ImageEnhance.Contrast(img).enhance(
                    float(op.get("factor", 1.0))
                )

            elif ot == "saturation":
                img = ImageEnhance.Color(img).enhance(
                    float(op.get("factor", 1.0))
                )

            elif ot == "sharpness":
                img = ImageEnhance.Sharpness(img).enhance(
                    float(op.get("factor", 1.0))
                )

            elif ot == "grayscale":
                img = ImageOps.grayscale(img)
                img = img.convert("RGB")

            elif ot == "sepia":
                gray = ImageOps.grayscale(img)
                sepia = Image.merge(
                    "RGB",
                    (
                        gray.point(lambda x: min(255, int(x * 1.2))),
                        gray.point(lambda x: min(255, int(x * 1.0))),
                        gray.point(lambda x: min(255, int(x * 0.8))),
                    ),
                )
                img = sepia

            elif ot == "invert":
                if img.mode == "RGBA":
                    r, g, b, a = img.split()
                    rgb = Image.merge("RGB", (r, g, b))
                    rgb = ImageOps.invert(rgb)
                    img = Image.merge("RGBA", (*rgb.split(), a))
                else:
                    img = ImageOps.invert(img.convert("RGB"))

            elif ot == "blur":
                radius = float(op.get("radius", 2))
                img = img.filter(ImageFilter.GaussianBlur(radius=radius))

            elif ot == "sharpen":
                img = img.filter(ImageFilter.SHARPEN)

            elif ot == "edge_enhance":
                img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)

            elif ot == "emboss":
                img = img.filter(ImageFilter.EMBOSS)

            elif ot == "auto_enhance":
                img = ImageOps.autocontrast(img)
                img = ImageEnhance.Color(img).enhance(1.1)
                img = ImageEnhance.Sharpness(img).enhance(1.1)

            elif ot == "color_temperature":
                temp = float(op.get("temperature", 5500))
                factor = (temp - 5500) / 5500
                r, g, b = img.split()[:3]
                r = r.point(
                    lambda x: min(255, max(0, int(x * (1 + factor * 0.1))))
                )
                b = b.point(
                    lambda x: min(255, max(0, int(x * (1 - factor * 0.1))))
                )
                if img.mode == "RGBA":
                    img = Image.merge("RGBA", (r, g, b, img.split()[3]))
                else:
                    img = Image.merge("RGB", (r, g, b))

            elif ot == "exposure":
                gamma = float(op.get("gamma", 1.0))
                inv_gamma = 1.0 / gamma
                lut = [
                    int(((i / 255.0) ** inv_gamma) * 255) for i in range(256)
                ]
                if img.mode == "RGBA":
                    r, g, b, a = img.split()
                    r = r.point(lut)
                    g = g.point(lut)
                    b = b.point(lut)
                    img = Image.merge("RGBA", (r, g, b, a))
                else:
                    channels = img.split()
                    channels = tuple(ch.point(lut) for ch in channels)
                    img = Image.merge(img.mode, channels)

            elif ot == "add_text":
                draw = ImageDraw.Draw(img)
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
                except Exception:
                    font = ImageFont.load_default()
                draw.text((x, y), text, fill=color, font=font)

            elif ot == "add_border":
                border_w = int(op.get("width", 5))
                color = op.get("color", "#000000")
                img = ImageOps.expand(img, border=border_w, fill=color)

            elif ot == "paste_image":
                overlay_path = _resolve_path(op["image_path"])
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
                    alpha = alpha.point(lambda a: int(a * opacity))
                    overlay.putalpha(alpha)
                if overlay.mode == "RGBA":
                    img.paste(overlay, (x, y), overlay)
                else:
                    img.paste(overlay, (x, y))

            elif ot == "add_watermark":
                txt = op.get("text", "WATERMARK")
                opacity = float(op.get("opacity", 0.3))
                watermark = Image.new("RGBA", img.size, (0, 0, 0, 0))
                draw = ImageDraw.Draw(watermark)
                font_size = max(img.width // len(txt), 20)
                try:
                    font = ImageFont.truetype(
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                        font_size,
                    )
                except Exception:
                    font = ImageFont.load_default()
                bbox = draw.textbbox((0, 0), txt, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                x = (img.width - tw) // 2
                y = (img.height - th) // 2
                alpha_val = int(255 * opacity)
                draw.text(
                    (x, y), txt, fill=(128, 128, 128, alpha_val), font=font
                )
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                img = Image.alpha_composite(img, watermark)
                img = img.convert("RGB")

            elif ot == "highlights":
                import numpy as np

                amount = float(op.get("amount", 0))
                if amount != 0:
                    arr = np.array(img, dtype=np.float32) / 255.0
                    lum = (
                        0.299 * arr[:, :, 0]
                        + 0.587 * arr[:, :, 1]
                        + 0.114 * arr[:, :, 2]
                    )
                    blur_r = max(3, int(img.width * 0.025))
                    blur_img = img.filter(
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
                    img = Image.fromarray(
                        np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                    )

            elif ot == "shadows":
                import numpy as np

                amount = float(op.get("amount", 0))
                if amount != 0:
                    arr = np.array(img, dtype=np.float32) / 255.0
                    lum = (
                        0.299 * arr[:, :, 0]
                        + 0.587 * arr[:, :, 1]
                        + 0.114 * arr[:, :, 2]
                    )
                    blur_r = max(3, int(img.width * 0.025))
                    blur_img = img.filter(
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
                    img = Image.fromarray(
                        np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                    )

            elif ot == "blacks":
                import numpy as np

                level = float(op.get("level", 0))
                arr = np.array(img, dtype=np.float32) / 255.0
                if level > 0:
                    lift = level / 100.0 * 0.08
                    arr = lift + arr * (1.0 - lift)
                elif level < 0:
                    power = 1.0 + abs(level) / 100.0 * 0.5
                    arr = arr**power
                img = Image.fromarray(
                    np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                )

            elif ot == "whites":
                import numpy as np

                level = float(op.get("level", 0))
                arr = np.array(img, dtype=np.float32) / 255.0
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
                img = Image.fromarray(
                    np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                )

            elif ot == "clarity":
                import numpy as np

                amount = float(op.get("amount", 20))
                radius = max(3, int(img.width * 0.02))
                scale = amount / 100.0 * 0.4
                arr = np.array(img, dtype=np.float32)
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
                img = Image.fromarray(
                    np.clip(arr, 0, 255).astype(np.uint8), img.mode
                )

            elif ot == "dehaze":
                import numpy as np
                from scipy.ndimage import minimum_filter, uniform_filter

                strength = float(op.get("strength", 0.3))
                arr = np.array(img, dtype=np.float32) / 255.0
                dark = np.min(arr, axis=2)
                win = max(5, int(min(img.width, img.height) * 0.01))
                try:
                    dark_channel = minimum_filter(dark, size=win)
                except Exception:
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
                img = Image.fromarray(
                    np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                )

            elif ot == "split_tone":
                import colorsys

                import numpy as np

                arr = np.array(img, dtype=np.float32) / 255.0
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

                img = Image.fromarray(
                    np.clip(arr * 255, 0, 255).astype(np.uint8), img.mode
                )

            elif ot == "hsl_adjust":
                import numpy as np

                arr = np.array(img, dtype=np.float32) / 255.0
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
                img = Image.fromarray(
                    np.clip(arr_out * 255, 0, 255).astype(np.uint8), "RGB"
                )

            elif ot == "denoise":
                import numpy as np

                strength = int(op.get("strength", 3))
                ycbcr = img.convert("YCbCr")
                y, cb, cr = ycbcr.split()
                radius = max(1, strength * 0.8)
                cb = cb.filter(ImageFilter.GaussianBlur(radius=radius))
                cr = cr.filter(ImageFilter.GaussianBlur(radius=radius))
                if strength >= 4:
                    y = y.filter(ImageFilter.GaussianBlur(radius=0.3))
                ycbcr_out = Image.merge("YCbCr", (y, cb, cr))
                img = ycbcr_out.convert("RGB")

            # -------------------------------------------------------
            # NEW: Background removal, blur region, compress, convert
            # -------------------------------------------------------

            elif ot == "remove_background":
                from rembg import remove

                img = remove(img, session=_get_rembg_session())
                # rembg returns RGBA with transparent background
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                # If user wants a specific background color instead of transparent
                bg_color = op.get("background_color")
                if bg_color:
                    c = str(bg_color).lstrip("#")
                    r, g, b = int(c[:2], 16), int(c[2:4], 16), int(c[4:6], 16)
                    bg = Image.new("RGB", img.size, (r, g, b))
                    bg.paste(img, mask=img.split()[3])
                    img = bg

            elif ot == "blur_region":
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
                left = max(0, min(left, img.width))
                top_coord = max(0, min(top_coord, img.height))
                right = max(left + 1, min(right, img.width))
                bottom = max(top_coord + 1, min(bottom, img.height))
                radius = float(op.get("radius") or op.get("intensity") or op.get("strength") or 15)
                # Crop region, blur, paste back
                region = img.crop((left, top_coord, right, bottom))
                region = region.filter(
                    ImageFilter.GaussianBlur(radius=radius)
                )
                img.paste(region, (left, top_coord))

            elif ot == "compress":
                # Set compression for the final save
                quality = int(op.get("quality", 75))
                target_format = op.get("format", "").upper()
                if target_format == "JPG":
                    target_format = "JPEG"
                # Store for save step (handled below)
                img._compress_quality = quality
                if target_format:
                    img._compress_format = target_format

            elif ot == "convert_format":
                # Convert to a different format by changing output path
                target_format = op.get("format", "").lower()
                if target_format:
                    if target_format == "jpg":
                        target_format = "jpeg"
                    new_ext = target_format if target_format != "jpeg" else "jpg"
                    out = str(Path(out).with_suffix(f".{new_ext}"))
                    Path(out).parent.mkdir(parents=True, exist_ok=True)

            else:
                logger.warning(
                    f"edit_image: unknown operation '{ot}', skipping"
                )

        except Exception as exc:
            return f"Error in operation '{ot}': {exc}"

    # Save
    # Check for compress operation overrides
    compress_quality = getattr(img, "_compress_quality", None)
    compress_format = getattr(img, "_compress_format", None)

    save_format = Path(out).suffix.lstrip(".").upper()
    if save_format == "JPG":
        save_format = "JPEG"
    if compress_format:
        save_format = compress_format
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
    img.save(
        out,
        format=(
            save_format
            if save_format in ("JPEG", "PNG", "WEBP", "TIFF", "BMP")
            else None
        ),
        **save_kwargs,
    )

    # Flush platform-cache write to remote satellite (no-op for local).
    await _notify_file_written(out)

    # Push inline image preview
    buf = io.BytesIO()
    preview_img = img.copy()
    preview_img.thumbnail((1568, 1568), Image.LANCZOS)
    if preview_img.mode == "RGBA":
        preview_img = preview_img.convert("RGB")
    preview_img.save(buf, format="JPEG", quality=85)
    await _push_image_preview(
        buf.getvalue(), "image/jpeg", f"Edited: {Path(out).name}"
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
                        "description": f"Full resolution ({img.width}x{img.height})",
                    },
                    headers={"Authorization": auth},
                )
        except Exception:
            pass

    result = (f"Image saved: {_to_agents_relative(out)} "
              f"({img.width}x{img.height}, {len(ops)} operations applied)")
    result += _dropped_note(dropped)
    if before_stats is not None:
        result += "\n" + _tone_check_line(before_stats, _tone_stats(img))
    return result


# ---------------------------------------------------------------------------
# analyze_image
# ---------------------------------------------------------------------------


async def handle_analyze_image(args: dict) -> str:
    """Analyze an image — file info, EXIF, histogram, exposure, suggestions."""
    import os

    from PIL import Image, ImageStat
    from PIL.ExifTags import TAGS

    import numpy as np

    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

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
                except Exception:
                    exif_info.append("GPS: present")
    except Exception:
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

    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: File not found: {args['path']}"

    language = args.get("language", "eng")

    try:
        result = subprocess.run(
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
