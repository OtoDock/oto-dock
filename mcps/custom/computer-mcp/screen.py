"""Screen capture for the computer-control MCP.

Wraps ``mss`` (capture) + ``Pillow`` (downscale + PNG encode) and exposes the
per-monitor geometry the executor needs to translate model clicks back to OS
input coordinates. Both heavy deps are imported **lazily inside functions** so
the module imports on a headless box (and in the test suite) without a display
or the capture deps installed; failures become actionable
:class:`environment.DeviceUnavailable` errors rather than import-time crashes.

A call grabs through one :class:`Grabber` (one ``mss`` instance, so one display
connection of its own on X11, never pyautogui's) and keeps :class:`Frame`
objects: the raw pixels of a monitor, compared as bytes by the still wait and
the spot check, and encoded into the picture the model sees only once.
"""

from __future__ import annotations

import base64
import contextlib
import io
from dataclasses import dataclass, field
from typing import Any

import geometry
from environment import DeviceUnavailable, os_kind


@dataclass
class Frame:
    """One grab of a monitor at its physical pixels: the raw BGRA bytes and the
    monitor measured from them. Compared as bytes. Turned into a Pillow image
    once, when something needs its pixels."""

    monitor: geometry.Monitor
    raw: bytes
    width: int
    height: int
    _image: Any = field(default=None, repr=False, compare=False)

    def image(self):
        if self._image is None:
            from PIL import Image

            self._image = Image.frombuffer("RGB", (self.width, self.height), self.raw, "raw", "BGRX", 0, 1)
        return self._image


@dataclass
class Capture:
    """The picture handed to the model plus everything needed to map a click on
    it back to a physical/OS coordinate."""

    png_b64: str                 # base64 PNG, downscaled to model space
    model_width: int             # width of the image actually sent
    model_height: int
    scale: float                 # model_px = physical_px * scale
    monitor: geometry.Monitor    # the captured monitor's geometry
    monitor_count: int           # total individual monitors detected
    saved_path: str | None = None  # abs path of the full-res PNG, if save was requested
    frame: Frame | None = None   # the grab it was made from


def ensure_dpi_aware() -> None:
    """Make the process per-monitor-DPI-aware on Windows so ``mss`` and
    ``pyautogui`` report the SAME (physical) coordinate space. Without this,
    Windows virtualizes coordinates inconsistently between capture and input on
    scaled displays. It must run before pyautogui's import, which sets a
    process-wide awareness that cannot be changed afterwards
    (``executor._gui`` calls it first). No-op off Windows and idempotent/safe
    to call repeatedly.
    """
    if os_kind() != "windows":
        return
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+). Falls back to the
        # process-wide SetProcessDPIAware (Vista+) if shcore is unavailable.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001  # ctypes calls into win32 raise whatever the bridge does
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001  # ctypes calls into win32 raise whatever the bridge does
        # Best-effort only; the DPR math still self-corrects from the
        # capture/monitor size ratio even if this fails.
        pass


def list_monitors() -> list[geometry.Monitor]:
    """Enumerate individual monitors (mss index 1..N; index 0 — the
    all-monitors virtual screen — is skipped). ``phys_*`` come from a real grab
    so the DPR is measured, not assumed. Raises
    :class:`environment.DeviceUnavailable` if the screen subsystem can't init.
    A call reads it once and passes the list on.
    """
    ensure_dpi_aware()
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


def select_monitor(display: int | None, monitors: list[geometry.Monitor] | None = None) -> tuple[geometry.Monitor, int]:
    """Resolve a 1-based ``display`` index (``None`` => primary) to a
    :class:`geometry.Monitor`, returning ``(monitor, total_count)``. An
    out-of-range index falls back to the primary so a bad arg never crashes."""
    monitors = monitors if monitors is not None else list_monitors()
    count = len(monitors)
    if display is None or display < 1 or display > count:
        return monitors[0], count
    return monitors[display - 1], count


def _rect(monitor: geometry.Monitor) -> dict:
    return {"left": monitor.left, "top": monitor.top, "width": monitor.width, "height": monitor.height}


class Grabber:
    """One ``mss`` instance for one call."""

    def __init__(self) -> None:
        try:
            import mss
        except Exception as e:  # pragma: no cover - import guard
            raise DeviceUnavailable(f"Screen-capture dependencies are not available: {e}") from e
        try:
            self._sct = mss.mss()
        except Exception as e:
            raise DeviceUnavailable(
                "Could not read the display configuration. The session may be "
                f"locked, asleep, or headless. ({e})"
            ) from e

    def grab(self, monitor: geometry.Monitor) -> Frame:
        try:
            shot = self._sct.grab(_rect(monitor))
        except Exception as e:
            raise DeviceUnavailable(
                "Screen capture failed. On Wayland this is expected (capture is "
                "blocked); otherwise the session may be locked or asleep. "
                f"({e})"
            ) from e
        measured = geometry.Monitor(
            index=monitor.index, left=monitor.left, top=monitor.top,
            width=monitor.width, height=monitor.height,
            phys_width=int(shot.width), phys_height=int(shot.height),
            is_primary=monitor.is_primary,
        )
        return Frame(measured, bytes(shot.raw), int(shot.width), int(shot.height))

    def region(self, rect: dict):
        """A region of the desktop (in the OS input space, as a monitor's
        bounds are) at the screen's full sharpness, as a Pillow image. On
        macOS ``mss`` grabs one pixel per point unless its options lose the
        nominal-resolution flag, which this does for the one grab (mss's own
        note on ``IMAGE_OPTIONS``, its issue 257)."""
        from PIL import Image

        try:
            if os_kind() == "darwin":
                import mss.darwin as mss_darwin

                saved = mss_darwin.IMAGE_OPTIONS
                mss_darwin.IMAGE_OPTIONS = saved & ~mss_darwin.kCGWindowImageNominalResolution
                try:
                    shot = self._sct.grab(rect)
                finally:
                    mss_darwin.IMAGE_OPTIONS = saved
            else:
                shot = self._sct.grab(rect)
        except Exception as e:
            raise DeviceUnavailable(f"Screen capture failed. ({e})") from e
        return Image.frombuffer("RGB", (int(shot.width), int(shot.height)), bytes(shot.raw), "raw", "BGRX", 0, 1)

    def close(self) -> None:
        with contextlib.suppress(Exception):  # closing a display connection must never fail a call
            self._sct.close()


def open_grabber() -> Grabber:
    return Grabber()


def png_b64(img, coord_mode: str) -> tuple[str, int, int, float]:
    """``(base64 PNG, width, height, scale)`` of ``img`` shrunk to the mode's
    screenshot limit, never enlarged: the one encoder of every picture."""
    from PIL import Image

    scale = geometry.compute_scale(img.width, img.height, coord_mode)
    w, h = geometry.scaled_size(img.width, img.height, scale)
    small = img.resize((w, h), Image.LANCZOS) if (w, h) != (img.width, img.height) else img
    buf = io.BytesIO()
    small.save(buf, format="PNG", optimize=False, compress_level=6)
    return base64.b64encode(buf.getvalue()).decode("ascii"), w, h, scale


def save_png(img, path: str) -> str | None:
    """Write ``img`` at full sharpness. A failure is non-fatal: ``None``."""
    try:
        img.save(path, format="PNG", optimize=False, compress_level=6)
        return path
    except (OSError, ValueError):
        return None


def encode(frame: Frame, coord_mode: str, *, monitor_count: int, save_path: str | None = None) -> Capture:
    """The picture of ``frame`` the model sees. ``save_path`` (a DELIBERATE
    save only) writes the FULL-resolution frame there for dashboard viewing,
    never into a ``.screenshots`` tree (the caller picks a normal workspace
    path; see ``server._alloc_save_path``)."""
    img = frame.image()
    saved_path = save_png(img, save_path) if save_path else None
    data, w, h, scale = png_b64(img, coord_mode)
    return Capture(
        png_b64=data, model_width=w, model_height=h, scale=scale,
        monitor=frame.monitor, monitor_count=monitor_count,
        saved_path=saved_path, frame=frame,
    )
