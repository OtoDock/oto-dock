"""The checks the computer tool runs before each step that acts.

Three reads come from the system: the cursor (pyautogui, on the runner's own
thread, since pyautogui's display connection is not thread safe), the window
in front and, per system, the rights of a window's program or Secure Input
(``environment.desktop``'s module, each in a helper thread bounded to
:data:`READ_TIMEOUT_S`). The spot under a click is pixels only.

A read that raises, whose library is missing or that does not answer in time
turns its check off for the call and the call says so in one line. A read that
did not answer is not started again while its thread still hangs, so a stuck
read costs one timeout; once that thread ends it is tried again. A check never
makes a call fail or hang.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import executor
import geometry
import still
from environment import Window

logger = logging.getLogger("computer-mcp")

READ_TIMEOUT_S = 0.5
MOUSE_TOLERANCE_PX = 3

# The names of the checks, as the result's lines say them.
MOUSE = "mouse"
WINDOW = "window in front"
RIGHTS = "window's rights"
SECURE = "Secure Input"

_OFF_LINE = {
    MOUSE: "The check of the mouse was off for this call: the cursor could not be read.",
    WINDOW: "The check of the window in front was off for this call: it could not be read.",
    RIGHTS: "The check of a window's rights was off for this call: they could not be read.",
    SECURE: "The check of Secure Input was off for this call: it could not be read.",
}

# Reads that did not answer in time, with the event their thread sets when it
# ends: never started again while that thread still hangs.
_STUCK: dict[str, threading.Event] = {}


class _Off(Exception):
    pass


def bounded(kind: str, fn: Callable[[], object], timeout: float | None = None):
    """``fn()`` in a helper thread, waited for at most ``timeout``. Raises
    :class:`_Off` when it raised, or did not answer (then the kind is stuck)."""
    stuck = _STUCK.get(kind)
    if stuck is not None:
        if not stuck.is_set():
            raise _Off(kind)
        del _STUCK[kind]   # it answered after all: try again
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001  # any failure of a system read turns its check off
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name=f"computer-read-{kind}").start()
    timeout = READ_TIMEOUT_S if timeout is None else timeout
    if not done.wait(timeout):
        _STUCK[kind] = done
        logger.warning("computer: the %s read did not answer in %.1fs; it stays off", kind, timeout)
        raise _Off(kind)
    if "error" in box:
        logger.warning("computer: the %s read failed: %s", kind, type(box["error"]).__name__)
        raise _Off(kind)
    return box.get("value")


class Checks:
    """The reads of one call. A check that went off stays off for the call."""

    def __init__(self, desk, *, cursor: Callable[[], tuple[int, int]] = executor.cursor_position) -> None:
        self.desk = desk
        self._cursor = cursor
        self.off: list[str] = []

    def _turn_off(self, kind: str) -> None:
        if kind not in self.off:
            self.off.append(kind)

    def is_off(self, kind: str) -> bool:
        return kind in self.off

    def off_lines(self) -> list[str]:
        return [_OFF_LINE[k] for k in self.off]

    def cursor(self) -> tuple[int, int] | None:
        if self.is_off(MOUSE):
            return None
        try:
            x, y = self._cursor()
            return int(x), int(y)
        except Exception as e:  # noqa: BLE001  # an unreadable cursor turns the check off
            logger.warning("computer: the cursor read failed: %s", type(e).__name__)
            self._turn_off(MOUSE)
            return None

    def _read(self, kind: str, name: str, *args):
        fn = getattr(self.desk, name, None) if self.desk is not None else None
        if fn is None or self.is_off(kind):
            if fn is None and self.desk is None:
                self._turn_off(kind)
            return None
        try:
            return bounded(name, lambda: fn(*args))
        except _Off:
            self._turn_off(kind)
            return None

    def front(self) -> Window | None:
        return self._read(WINDOW, "front_window")

    def window_at(self, x: int, y: int) -> Window | None:
        """The window under a point where the system tells it (Windows),
        else ``None`` and the caller judges by the window in front."""
        if self.desk is None or not hasattr(self.desk, "window_at"):
            return None
        return self._read(RIGHTS, "window_at", x, y)

    def blocks(self, window: Window) -> bool | None:
        if self.desk is None or not hasattr(self.desk, "blocks_input"):
            return None
        return self._read(RIGHTS, "blocks_input", window)

    def secure_input(self) -> tuple[bool, int | None] | None:
        if self.desk is None or not hasattr(self.desk, "secure_input"):
            return None
        return self._read(SECURE, "secure_input")

    def program_of(self, pid: int) -> str:
        if self.desk is None or not hasattr(self.desk, "program_of"):
            return ""
        try:
            return str(bounded("program_of", lambda: self.desk.program_of(pid)) or "")
        except _Off:
            return ""


def mouse_moved(now: tuple[int, int] | None, left: tuple[int, int] | None) -> bool:
    """The cursor is away from where this tool last left it."""
    if now is None or left is None:
        return False
    return max(abs(now[0] - left[0]), abs(now[1] - left[1])) > MOUSE_TOLERANCE_PX


def matches(window: Window | None, name: str) -> bool:
    """``name`` is part of the window's title or its program's name."""
    if window is None:
        return False
    want = name.casefold().strip()
    return bool(want) and (want in window.title.casefold() or want in window.program.casefold())


# --- the spot under a click -------------------------------------------------

def patch_rect(point: tuple[float, float], scale: float) -> tuple[int, int, int, int, int]:
    """``(left, top, right, bottom, block)`` of the square checked under a
    model point, in its monitor's frame."""
    left, top, block = geometry.patch_box(point[0], point[1], scale=scale)
    side = block * geometry.PATCH_CELLS
    return left, top, left + side, top + side, block


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _masked(rect: tuple[int, int, int, int], mask: bytearray | None, cols: int) -> bool:
    if not mask:
        return False
    c0, r0 = max(0, rect[0] // still.CELL), max(0, rect[1] // still.CELL)
    c1, r1 = (rect[2] - 1) // still.CELL, (rect[3] - 1) // still.CELL
    rows = len(mask) // cols if cols else 0
    for r in range(r0, min(r1, rows - 1) + 1):
        for c in range(c0, min(c1, cols - 1) + 1):
            if mask[r * cols + c]:
                return True
    return False


def spot_changed(seen, current, point: tuple[float, float], scale: float, *,
                 earlier: list[tuple[int, int, int, int]], mask: bytearray | None) -> bool:
    """The screen under ``point`` is not what the model saw in ``seen``.

    The square around the point is compared exactly in
    ``PATCH_CELLS`` x ``PATCH_CELLS`` blocks. A block does not count when it
    lies off the frame, inside a square an earlier step of the call aimed at
    (that step was meant to change it) or on a cell that was already moving.
    A change counts when its blocks span more than two in both directions (a
    caret is a thin line) and it touches the centre or covers a quarter of the
    blocks that count."""
    from PIL import Image

    left, top, right, bottom, block = patch_rect(point, scale)
    cells = geometry.PATCH_CELLS
    fw, fh = seen.width, seen.height
    box = (max(0, left), max(0, top), min(fw, right), min(fh, bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        return False
    canvas_a = Image.new("RGB", (right - left, bottom - top))
    canvas_b = Image.new("RGB", (right - left, bottom - top))
    offset = (box[0] - left, box[1] - top)
    canvas_a.paste(seen.image().crop(box), offset)
    canvas_b.paste(current.image().crop(box), offset)
    grid, _cols, _rows = still.changed_grid(canvas_a, canvas_b, block)
    mask_cols = still.grid_size(fw, fh)[0]
    centre = range(cells // 2 - 2, cells // 2 + 2)
    counted, changed = 0, []
    for r in range(cells):
        for c in range(cells):
            rect = (left + c * block, top + r * block, left + (c + 1) * block, top + (r + 1) * block)
            if not _overlaps(rect, box):
                continue
            if any(_overlaps(rect, e) for e in earlier) or _masked(rect, mask, mask_cols):
                continue
            counted += 1
            if grid[r * cells + c]:
                changed.append((r, c))
    if not changed:
        return False
    rows = [r for r, _ in changed]
    cols = [c for _, c in changed]
    if max(rows) - min(rows) < 2 or max(cols) - min(cols) < 2:
        return False  # a thin line: a caret, an underline
    if any(r in centre and c in centre for r, c in changed):
        return True
    return len(changed) * 4 >= counted
