"""Pure coordinate + scaling math for the computer-control MCP.

This module has **no heavy imports** (no ``mss`` / ``pyautogui`` / ``PIL``) so
it imports cleanly in headless test environments and is unit-testable without a
display. The geometry here is the load-bearing correctness surface of the whole
MCP: the round-trip between

    model-image pixels  <->  physical capture pixels  <->  OS input coordinates

done **per monitor**, accounting for HiDPI scaling (Retina / Windows display
scaling) and — the easy-to-get-wrong part — **negative multi-monitor origins**
(a monitor positioned to the left of / above the primary has a negative
``left`` / ``top``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# --- Coordinate modes ------------------------------------------------------
#
# The model is sent a (possibly downscaled) screenshot and replies with clicks
# in THAT image's pixel space. We always recover physical pixels with
# ``px = model_x / scale``, so injection is correct in BOTH modes — the mode
# only decides how aggressively we downscale before sending. Defaulting to
# "rescale" is therefore universally safe (just lower-resolution for a model
# that could handle more); "highres" is an opt-in detail bump for Opus 4.x.
COORD_MODE_RESCALE = "rescale"
COORD_MODE_HIGHRES = "highres"
DEFAULT_COORD_MODE = COORD_MODE_RESCALE

# Anthropic computer-use downscale targets.
_RESCALE_MAX_EDGE = 1568          # long-edge cap (px)
_RESCALE_MAX_AREA = 1_150_000     # ~WXGA area cap (px^2); the sqrt term
_HIGHRES_MAX_EDGE = 2576          # long-edge cap for Opus 4.x-class models


def normalize_coord_mode(raw: str | None) -> str:
    """Map a config string (possibly None / odd casing / aliases) to a canonical
    mode. Anything unrecognised falls back to the safe default ``rescale``."""
    m = (raw or "").strip().lower()
    if m in (COORD_MODE_HIGHRES, "highres_1to1", "1to1", "high", "opus"):
        return COORD_MODE_HIGHRES
    return COORD_MODE_RESCALE


def compute_scale(width: int, height: int, coord_mode: str = DEFAULT_COORD_MODE) -> float:
    """Return the downscale factor (``0 < scale <= 1.0``) to apply to a physical
    ``width`` x ``height`` capture before sending it to the model.

    ``rescale`` (default): ``min(1.0, 1568/longest, sqrt(1.15M/area))`` — the
    WXGA sweet spot that keeps older vision models accurate.
    ``highres``: ``min(1.0, 2576/longest)`` — bigger images for Opus 4.x.
    """
    if width <= 0 or height <= 0:
        return 1.0
    longest = max(width, height)
    if normalize_coord_mode(coord_mode) == COORD_MODE_HIGHRES:
        return min(1.0, _HIGHRES_MAX_EDGE / longest)
    return min(
        1.0,
        _RESCALE_MAX_EDGE / longest,
        math.sqrt(_RESCALE_MAX_AREA / (width * height)),
    )


def scaled_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """Pixel dimensions of the downscaled image actually sent to the model.
    Never returns 0 in either axis (a 1px floor keeps PIL/encoders happy)."""
    return max(1, round(width * scale)), max(1, round(height * scale))


@dataclass(frozen=True)
class Monitor:
    """One physical monitor, in the two coordinate spaces we care about.

    ``left`` / ``top`` / ``width`` / ``height`` are in the OS GLOBAL coordinate
    space that the **input API** (pyautogui) uses — which is exactly what
    ``mss``'s monitor dict reports: CoreGraphics *points* on macOS, *physical
    pixels* on X11 and DPI-aware Windows. ``left`` / ``top`` CAN BE NEGATIVE.

    ``phys_width`` / ``phys_height`` are the dimensions of the actual grabbed
    pixel buffer for this monitor (Retina => 2x the logical width/height). The
    per-axis device-pixel-ratio is derived from the two, so we never assume 1:1.
    """

    index: int                 # mss monitor index (1-based; 0 = all-monitors)
    left: int
    top: int
    width: int
    height: int
    phys_width: int
    phys_height: int
    is_primary: bool = False

    @property
    def dpr_x(self) -> float:
        return (self.phys_width / self.width) if self.width else 1.0

    @property
    def dpr_y(self) -> float:
        return (self.phys_height / self.height) if self.height else 1.0


def model_to_input_coords(
    model_x: float,
    model_y: float,
    *,
    monitor: Monitor,
    scale: float,
) -> tuple[float, float]:
    """Convert a model-image click ``(model_x, model_y)`` to a GLOBAL OS input
    coordinate ``(inject_x, inject_y)`` suitable for ``pyautogui.moveTo`` etc.

    Three steps:

      1. model -> physical pixel within this monitor:  ``px = model_x / scale``
      2. physical -> logical (divide by the monitor's DPR):  ``lx = px / dpr_x``
      3. logical -> global input space (add the origin, which may be NEGATIVE):
         ``gx = monitor.left + lx``

    Returns floats; the caller rounds and clamps.
    """
    if scale <= 0:
        scale = 1.0
    px = model_x / scale
    py = model_y / scale
    lx = px / monitor.dpr_x
    ly = py / monitor.dpr_y
    return monitor.left + lx, monitor.top + ly


def input_to_model_coords(
    gx: float,
    gy: float,
    *,
    monitor: Monitor,
    scale: float,
) -> tuple[float, float]:
    """Inverse of :func:`model_to_input_coords` — used by ``cursor_position`` to
    report the live cursor location back in the model's image space."""
    lx = gx - monitor.left
    ly = gy - monitor.top
    px = lx * monitor.dpr_x
    py = ly * monitor.dpr_y
    return px * scale, py * scale


def clamp_to_monitor(gx: float, gy: float, monitor: Monitor) -> tuple[float, float]:
    """Clamp a global input coordinate to this monitor's logical bounds so a
    model click a few pixels outside the image can't fling the cursor onto a
    different monitor or off-screen. Inclusive of the monitor's last pixel."""
    min_x, max_x = monitor.left, monitor.left + monitor.width - 1
    min_y, max_y = monitor.top, monitor.top + monitor.height - 1
    return min(max(gx, min_x), max_x), min(max(gy, min_y), max_y)
