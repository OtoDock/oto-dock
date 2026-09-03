"""Screen capture for the computer-control MCP.

Wraps ``mss`` (capture) + ``Pillow`` (downscale + PNG encode) and exposes the
per-monitor geometry the executor needs to translate model clicks back to OS
input coordinates. Both heavy deps are imported **lazily inside functions** so
the module imports on a headless box (and in the test suite) without a display
or the capture deps installed; failures become actionable
:class:`environment.DeviceUnavailable` errors rather than import-time crashes.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import geometry
from environment import DeviceUnavailable, os_kind


@dataclass
class Capture:
    """The result of a screenshot: the PNG to hand the model plus everything
    needed to map a click on it back to a physical/OS coordinate."""

    png_b64: str                 # base64 PNG, downscaled to model space
    model_width: int             # width of the image actually sent
    model_height: int
    scale: float                 # model_px = physical_px * scale
    monitor: geometry.Monitor    # the captured monitor's geometry
    monitor_count: int           # total individual monitors detected
    saved_path: str | None = None  # abs path of the full-res PNG, if save was requested


def _ensure_windows_dpi_aware() -> None:
    """Make the process per-monitor-DPI-aware on Windows so ``mss`` and
    ``pyautogui`` report the SAME (physical) coordinate space. Without this,
    Windows virtualizes coordinates inconsistently between capture and input on
    scaled displays. No-op off Windows and idempotent/safe to call repeatedly.
    """
    if os_kind() != "windows":
        return
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+). Falls back to the
        # process-wide SetProcessDPIAware (Vista+) if shcore is unavailable.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        # Best-effort only; the DPR math still self-corrects from the
        # capture/monitor size ratio even if this fails.
        pass


def list_monitors() -> list[geometry.Monitor]:
    """Enumerate individual monitors (mss index 1..N; index 0 — the
    all-monitors virtual screen — is skipped). ``phys_*`` come from a real grab
    so the DPR is measured, not assumed. Raises
    :class:`environment.DeviceUnavailable` if the screen subsystem can't init.
    """
    _ensure_windows_dpi_aware()
    try:
        import mss
    except Exception as e:  # pragma: no cover - import guard
        raise DeviceUnavailable(
            f"Screen-capture backend (mss) is not available: {e}"
        ) from e

    monitors: list[geometry.Monitor] = []
    try:
        with mss.mss() as sct:
            raw = sct.monitors  # [0]=all, [1..]=individual
            individual = raw[1:] if len(raw) > 1 else raw
            for i, mon in enumerate(individual, start=1):
                shot = sct.grab(mon)
                monitors.append(
                    geometry.Monitor(
                        index=i,
                        left=int(mon["left"]),
                        top=int(mon["top"]),
                        width=int(mon["width"]),
                        height=int(mon["height"]),
                        phys_width=int(shot.width),
                        phys_height=int(shot.height),
                        is_primary=(i == 1),
                    )
                )
    except DeviceUnavailable:
        raise
    except Exception as e:
        raise DeviceUnavailable(
            "Could not read the display configuration. The session may be "
            f"locked, asleep, or headless. ({e})"
        ) from e
    if not monitors:
        raise DeviceUnavailable("No monitors were detected on this machine.")
    return monitors


def select_monitor(display: int | None) -> tuple[geometry.Monitor, int]:
    """Resolve a 1-based ``display`` index (``None`` => primary) to a
    :class:`geometry.Monitor`, returning ``(monitor, total_count)``. An
    out-of-range index falls back to the primary so a bad arg never crashes."""
    monitors = list_monitors()
    count = len(monitors)
    if display is None or display < 1 or display > count:
        return monitors[0], count
    return monitors[display - 1], count


def resolve_target(display: int | None, coord_mode: str) -> tuple[geometry.Monitor, float]:
    """Return ``(monitor, scale)`` for ``display`` WITHOUT keeping the image —
    used by input actions to convert a model-space click into an OS coordinate.
    The selected monitor's ``phys_*`` are measured from a real grab (so the DPR
    is correct on Retina/HiDPI), and ``scale`` matches exactly what a
    screenshot of the same display+mode would have used (capture is
    deterministic), so cached state is unnecessary."""
    monitor, _count = select_monitor(display)
    scale = geometry.compute_scale(monitor.phys_width, monitor.phys_height, coord_mode)
    return monitor, scale


def capture(display: int | None, coord_mode: str, *, save_path: str | None = None) -> Capture:
    """Grab ``display`` (1-based; ``None`` => primary), downscale per
    ``coord_mode``, and return a :class:`Capture`. Raises
    :class:`environment.DeviceUnavailable` on any capture failure (callers turn
    it into an actionable error result).

    ``save_path`` (set only for a DELIBERATE ``screenshot(save=true)``) writes
    the FULL-resolution PNG to that path for dashboard viewing — never the
    downscaled model frame, and never into a ``.screenshots`` tree (the caller
    picks a normal workspace path; see ``server._alloc_save_path``). A save
    failure is non-fatal: ``saved_path`` stays ``None`` and the inline image is
    still returned."""
    try:
        import mss
        from PIL import Image
    except Exception as e:  # pragma: no cover - import guard
        raise DeviceUnavailable(
            f"Screen-capture dependencies are not available: {e}"
        ) from e

    monitor, count = select_monitor(display)
    mon_dict = {
        "left": monitor.left,
        "top": monitor.top,
        "width": monitor.width,
        "height": monitor.height,
    }
    try:
        with mss.mss() as sct:
            shot = sct.grab(mon_dict)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception as e:
        raise DeviceUnavailable(
            "Screen capture failed. On Wayland this is expected (capture is "
            "blocked); otherwise the session may be locked or asleep. "
            f"({e})"
        ) from e

    phys_w, phys_h = img.width, img.height

    # Deliberate save: persist the FULL-resolution frame before downscaling.
    saved_path = None
    if save_path:
        try:
            img.save(save_path, format="PNG", optimize=False, compress_level=6)
            saved_path = save_path
        except Exception:
            saved_path = None  # non-fatal — inline image is still returned

    scale = geometry.compute_scale(phys_w, phys_h, coord_mode)
    model_w, model_h = geometry.scaled_size(phys_w, phys_h, scale)
    model_img = img.resize((model_w, model_h), Image.LANCZOS) if (model_w, model_h) != (phys_w, phys_h) else img

    buf = io.BytesIO()
    model_img.save(buf, format="PNG", optimize=False, compress_level=6)
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    # Refresh the captured monitor's measured physical size from the actual
    # grab (handles the Retina/HiDPI case where the monitor dict is logical).
    measured = geometry.Monitor(
        index=monitor.index,
        left=monitor.left,
        top=monitor.top,
        width=monitor.width,
        height=monitor.height,
        phys_width=phys_w,
        phys_height=phys_h,
        is_primary=monitor.is_primary,
    )
    return Capture(
        png_b64=png_b64,
        model_width=model_w,
        model_height=model_h,
        scale=scale,
        monitor=measured,
        monitor_count=count,
        saved_path=saved_path,
    )
