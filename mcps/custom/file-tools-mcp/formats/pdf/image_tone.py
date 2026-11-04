"""edit_image's tone and colour operations: highlights and shadows by
frequency separation (darktable's shadhi), the black and white points,
clarity, dark-channel-prior dehaze (He et al. 2011), sigmoid split toning
(darktable's colorbalancergb), cosine-bell per-colour HSL, chroma-only
denoise (YCbCr). Each takes the edit job, the operation's context and its
keys, and replaces ``job.img``.
"""

from __future__ import annotations

import contextlib
from typing import Any

from formats.pdf.pages import whole
from results import OpCtx


def op_highlights(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_shadows(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_blacks(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_whites(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_clarity(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_dehaze(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_split_tone(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_hsl_adjust(job: Any, ctx: OpCtx, op: dict) -> None:
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


def op_denoise(job: Any, ctx: OpCtx, op: dict) -> None:
    from PIL import Image, ImageFilter

    strength = whole(ctx, op.get("strength", 3), "strength")
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
