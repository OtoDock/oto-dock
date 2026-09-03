"""Windows system-tray icon for the satellite daemon.

Windows-only, headless-safe. The satellite now runs as a per-user logon
task in the user's interactive session (not a Session-0 SYSTEM service), so
it can draw a tray icon. asyncio owns the main thread; the tray runs on a
daemon thread calling ``icon.run()`` (the Win32 backend doesn't need the
main thread).

Threading model:
  * tray thread → asyncio loop: menu callbacks enqueue a typed command via
    ``loop.call_soon_threadsafe(cmd_queue.put_nowait, cmd)``. A consumer
    task in ``__main__`` dispatches them (PAUSE / RESUME / QUIT / UNINSTALL).
    Open-dashboard / view-logs are handled inline on the tray thread (no
    loop hop — just ``webbrowser`` / ``os.startfile``).
  * asyncio loop → tray: ``TrayController.set_status(state)`` is called from
    the loop thread (via ws_client's status callback) and mutates the icon /
    tooltip; pystray tolerates these cross-thread mutations.

Everything is guarded: ``maybe_start_tray`` returns ``None`` off-Windows or
if pystray/Pillow can't import or the backend can't start, and the daemon
runs fully headless. No code path may assume the tray exists.

A future macOS menu-bar backend would slot in behind ``maybe_start_tray``;
the only constraint to preserve is that asyncio keeps the main thread (see
``__main__.main``), since a Cocoa backend would then need its own approach.
"""

import contextlib
import logging
import threading
from pathlib import Path

logger = logging.getLogger("satellite")

# Command strings enqueued to the asyncio loop (dispatched in __main__).
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_QUIT = "quit"
CMD_UNINSTALL = "uninstall"

# Status strings pushed from ws_client via set_status.
STATUS_STARTING = "starting"
STATUS_CONNECTED = "connected"
STATUS_RECONNECTING = "reconnecting"
STATUS_PAUSED = "paused"

_STATUS_LABEL = {
    STATUS_STARTING: "Starting…",
    STATUS_CONNECTED: "Connected",
    STATUS_RECONNECTING: "Reconnecting…",
    STATUS_PAUSED: "Paused",
}

# Status dot colors (RGBA) composited onto the base logo.
_STATUS_DOT = {
    STATUS_STARTING: (148, 163, 184, 255),    # slate
    STATUS_CONNECTED: (34, 197, 94, 255),     # green
    STATUS_RECONNECTING: (245, 158, 11, 255),  # amber
    STATUS_PAUSED: (148, 163, 184, 255),      # slate
}


def _dashboard_url(platform_url: str) -> str:
    """Derive the dashboard https origin from the WS platform_url.

    Mirrors the transform uninstall.sh/.ps1 use: ``wss://host/v1/satellite``
    → ``https://host``.
    """
    url = platform_url.strip()
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    # Strip the /v1/satellite path (and anything after it).
    for marker in ("/v1/satellite",):
        idx = url.find(marker)
        if idx != -1:
            url = url[:idx]
            break
    return url.rstrip("/")


class TrayController:
    """Owns the pystray Icon and its daemon thread. Construct via
    ``maybe_start_tray`` — never directly (it does the platform/import guard).
    """

    def __init__(self, icon, image_base, status: str):
        self._icon = icon
        self._image_base = image_base  # PIL.Image | None (base logo)
        self._status = status
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            # Backend failed to start (no desktop session, etc.). Degrade to
            # headless — the daemon keeps running without a tray.
            logger.warning("tray icon backend exited", exc_info=True)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="tray", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Hide + stop the icon (best-effort). Safe to call from any thread."""
        with contextlib.suppress(Exception):
            self._icon.visible = False
        with contextlib.suppress(Exception):
            self._icon.stop()

    # ---- status (called from the asyncio loop thread) ----

    def set_status(self, status: str) -> None:
        """Update the icon + tooltip to reflect a new connection state."""
        if status == self._status:
            return
        self._status = status
        try:
            self._icon.icon = self._compose_image(status)
            self._icon.title = f"OtoDock — {_STATUS_LABEL.get(status, status)}"
            # Re-render the menu so the Pause/Resume label + status line track.
            self._icon.update_menu()
        except Exception:
            logger.debug("tray set_status failed", exc_info=True)

    @property
    def paused(self) -> bool:
        return self._status == STATUS_PAUSED

    # ---- image composition ----

    def _compose_image(self, status: str):
        """Base logo + a status dot in the lower-right corner."""
        from PIL import Image, ImageDraw

        base = self._image_base
        if base is None:
            # Fallback placeholder so the tray never crashes on a missing icon.
            base = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
        img = base.convert("RGBA").resize((64, 64))
        draw = ImageDraw.Draw(img)
        color = _STATUS_DOT.get(status, _STATUS_DOT[STATUS_STARTING])
        # Dot occupies the lower-right ~40%, with a white ring for contrast.
        x0, y0, x1, y1 = 34, 34, 62, 62
        draw.ellipse((x0 - 2, y0 - 2, x1 + 2, y1 + 2), fill=(255, 255, 255, 255))
        draw.ellipse((x0, y0, x1, y1), fill=color)
        if status == STATUS_PAUSED:
            # Two pause bars in white.
            draw.rectangle((43, 41, 47, 55), fill=(255, 255, 255, 255))
            draw.rectangle((49, 41, 53, 55), fill=(255, 255, 255, 255))
        return img


def _load_base_image():
    """Load the bundled OtoDock logo (bin/otodock.ico). Returns a PIL.Image
    or None — composition falls back to a solid placeholder on None.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    # The tarball ships the package under a `satellite/` arcname but ships
    # `bin/otodock.ico` at the tarball ROOT; the installer extracts BOTH into
    # <OtoDir>/satellite/. So on a real install this module is at
    # <OtoDir>/satellite/satellite/host/tray.py while the icon is at
    # <OtoDir>/satellite/bin/otodock.ico — one level ABOVE __file__'s package
    # dir. Check both layouts (nested install + flat dev checkout) so the real
    # logo loads instead of the solid-blue placeholder.
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "bin" / "otodock.ico",         # flat dev checkout: satellite/bin/
        here.parent.parent / "bin" / "otodock.ico",  # nested install: <OtoDir>/satellite/bin/
    ]
    ico = next((c for c in candidates if c.is_file()), None)
    if ico is None:
        logger.debug("tray icon not found in: %s", [str(c) for c in candidates])
        return None
    try:
        img = Image.open(ico)
        img.load()
        return img.convert("RGBA")
    except Exception:
        logger.debug("failed to load tray base image", exc_info=True)
        return None


def maybe_start_tray(loop, cmd_queue, config, status: str = STATUS_STARTING):
    """Start the Windows tray icon if possible; return a TrayController or None.

    Returns None (headless) when:
      * not on Windows,
      * pystray / Pillow can't be imported, or
      * the icon can't be constructed.

    ``loop`` is the asyncio event loop, ``cmd_queue`` an ``asyncio.Queue`` the
    consumer task in __main__ drains, ``config`` the SatelliteConfig (for the
    dashboard URL + log path).
    """
    import sys

    if sys.platform != "win32":
        return None

    try:
        import pystray
    except Exception:
        logger.info("pystray not available — running without a tray icon")
        return None

    def _enqueue(cmd: str) -> None:
        # Hop to the loop thread; the consumer task dispatches the command.
        try:
            loop.call_soon_threadsafe(cmd_queue.put_nowait, cmd)
        except Exception:
            logger.debug("tray enqueue failed", exc_info=True)

    # ---- menu actions ----

    def _on_open_dashboard(icon, item):
        import webbrowser
        try:
            webbrowser.open(_dashboard_url(config.platform_url))
        except Exception:
            logger.debug("open dashboard failed", exc_info=True)

    def _on_view_logs(icon, item):
        import os
        log_path = otodir_log_path()
        try:
            os.startfile(str(log_path))  # type: ignore[attr-defined]
        except Exception:
            logger.debug("view logs failed", exc_info=True)

    def _on_pause_resume(icon, item):
        # controller.paused reflects the live status pushed from ws_client.
        _enqueue(CMD_RESUME if controller.paused else CMD_PAUSE)

    def _on_remove(icon, item):
        # Native confirm (no Tk dep) before the irreversible self-uninstall.
        if _confirm_remove():
            _enqueue(CMD_UNINSTALL)

    def _on_quit(icon, item):
        _enqueue(CMD_QUIT)

    def _status_text(item):
        return f"Status: {_STATUS_LABEL.get(controller._status, controller._status)}"

    def _pause_text(item):
        return "Resume" if controller.paused else "Pause"

    menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Open dashboard", _on_open_dashboard, default=True
        ),
        pystray.MenuItem(_pause_text, _on_pause_resume),
        pystray.MenuItem("View logs", _on_view_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Remove this machine…", _on_remove),
        pystray.MenuItem("Quit", _on_quit),
    )

    base_image = _load_base_image()

    # Build a temporary controller-less image first, then wrap in controller.
    try:
        icon = pystray.Icon(
            "otodock-satellite",
            title=f"OtoDock — {_STATUS_LABEL.get(status, status)}",
            menu=menu,
        )
    except Exception:
        logger.info("could not construct tray icon — running headless",
                    exc_info=True)
        return None

    controller = TrayController(icon, base_image, status)
    try:
        icon.icon = controller._compose_image(status)
    except Exception:
        logger.debug("tray initial image failed", exc_info=True)

    controller.start()
    logger.info("Windows tray icon started")
    return controller


def otodir_log_path() -> Path:
    """satellite.log path under the OtoDock root."""
    from ..config import otodock_dir
    return otodock_dir() / "satellite.log"


def _confirm_remove() -> bool:
    """Native Yes/No confirm via user32.MessageBoxW. True = user chose Yes.

    Falls back to True if the API is unavailable (the menu item is explicit
    enough that proceeding is acceptable), but logs it.
    """
    try:
        import ctypes
        MB_YESNO = 0x4
        MB_ICONWARNING = 0x30
        MB_DEFBUTTON2 = 0x100  # default to "No"
        IDYES = 6
        res = ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            0,
            "Remove this machine from OtoDock?\n\n"
            "This uninstalls the satellite from this computer and deletes it "
            "from the dashboard. This cannot be undone.",
            "OtoDock Satellite",
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
        )
        return res == IDYES
    except Exception:
        logger.debug("confirm dialog unavailable", exc_info=True)
        return True
