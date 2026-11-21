"""Waiting until the screen is still, and the cells of two frames that differ.

After a step that acts the tool watches the screen until it stops changing, in
place of a fixed pause: a probe (a grab of the call's monitor) every
:data:`PROBE_S`, compared as bytes and, only when the bytes differ, in square
cells of :data:`CELL` screen pixels. Cells that were already changing before
the step (a video, an animation) form the motion mask and never hold the wait.

The loop takes its probe, clock and sleep as arguments, so it runs on made-up
frame series in the tests. The pixel work is Pillow's, in C (about 10 ms for
a changed 4K frame); the cell grid is read in Python only as a few thousand
bytes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

PROBE_S = 0.05        # a probe costs about 6 ms at 1080p and 26 ms at 4K (measured)
# Still: nothing changed outside the mask for this long, so never sooner. Past a
# 300 ms debounce: a suggestion list that opened 300 ms after the last key
# raced a 300 ms quiet on the virtual screen and was missed.
QUIET_S = 0.45
CEILING_S = 2.0       # both vendors' fixed waits are 2 s
CELL = 16             # screen pixels per cell side
TOLERANCE = 4         # a caret or a clock's seconds change a cell or two
_EPS = 1e-6           # sums of probe intervals are not exact in floating point


def _marked(img_a, img_b):
    """A binary image (255 where any band of a pixel differs), or ``None``
    when nothing differs."""
    from PIL import ImageChops

    diff = ImageChops.difference(img_a, img_b)
    marked = diff.point(lambda v: 255 if v else 0).convert("L").point(lambda v: 255 if v else 0)
    return None if marked.getbbox() is None else marked


def changed_grid(img_a, img_b, block: int) -> tuple[bytes, int, int]:
    """``(cells, columns, rows)``: one byte per square of ``block`` pixels a
    side over two images of one size (row by row, the last row and column
    partial), non-zero where any pixel of the square differs.

    The binary picture is reduced as floats, so a lone changed pixel never
    averages away whatever the block's size, then scaled past 255."""
    cols, rows = grid_size(img_a.width, img_a.height, block)
    marked = _marked(img_a, img_b)
    if marked is None:
        return bytes(cols * rows), cols, rows
    small = marked.convert("F").reduce(block).point(lambda v: v * 1_000_000).convert("L")
    return small.tobytes(), small.width, small.height


def grid_size(width: int, height: int, block: int = CELL) -> tuple[int, int]:
    return max(1, math.ceil(width / block)), max(1, math.ceil(height / block))


def frame_cells(a, b) -> bytes | None:
    """The changed cells of two frames of one monitor at :data:`CELL` pixels
    a side (an all-zero grid when they are the same), ``None`` when their
    sizes differ."""
    if (a.width, a.height) != (b.width, b.height):
        return None
    if a.raw == b.raw:
        cols, rows = grid_size(a.width, a.height)
        return bytes(cols * rows)
    return changed_grid(a.image(), b.image(), CELL)[0]


def count_outside(cells: bytes, mask: bytearray | None) -> int:
    if mask is None:
        return sum(1 for c in cells if c)
    return sum(1 for i, c in enumerate(cells) if c and not mask[i])


def merge_into(mask: bytearray, cells: bytes) -> None:
    for i, c in enumerate(cells):
        if c:
            mask[i] = 1


def new_mask(cells: bytes | None, cols: int) -> bytearray:
    """The motion mask from one pair of probes, grown by one cell each way: a
    moving thing reaches past where two frames caught it."""
    size = len(cells) if cells else 0
    mask = bytearray(size)
    if not cells or cols <= 0:
        return mask
    rows = size // cols
    for i, c in enumerate(cells):
        if not c:
            continue
        r, k = divmod(i, cols)
        for rr in range(max(0, r - 1), min(rows, r + 2)):
            for kk in range(max(0, k - 1), min(cols, k + 2)):
                mask[rr * cols + kk] = 1
    return mask


@dataclass
class Settled:
    """What a wait saw: the last frame, whether the screen changed, whether it
    was still changing at the ceiling, and the cells of the last pair."""

    frame: object
    changed: bool
    slow: bool
    last_cells: bytes | None
    seconds: float
    touched: bool = False     # any cell outside the mask changed at all (a short paste)


def wait_until_still(
    probe: Callable[[], object],
    base,
    *,
    mask: bytearray | None,
    settle_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], object],
    quiet_s: float = QUIET_S,
    ceiling_s: float = CEILING_S,
    probe_s: float = PROBE_S,
    tolerance: int = TOLERANCE,
) -> Settled:
    """Probe until the screen is still after a step.

    It ends when a change was seen and nothing outside ``mask`` changed for
    ``quiet_s`` (so never sooner than that), when nothing changed by
    ``settle_s``, or at ``ceiling_s`` (``slow``). A probe that raises ends the
    wait: the caller decides what an unreadable screen means."""
    start = clock()
    prev = base
    changed = touched = False
    last_change = 0.0
    last_cells: bytes | None = None
    ceiling_s = max(ceiling_s, settle_s)
    while True:
        sleep(probe_s)
        frame = probe()
        now = clock() - start
        cells = frame_cells(prev, frame) if prev is not None else None
        moved = count_outside(cells, mask) if cells is not None else None
        if moved is None:
            changed, last_change = True, now   # the size changed: count it all
        elif moved > tolerance:
            changed, last_change = True, now
        touched = touched or moved is None or moved > 0
        last_cells = cells
        prev = frame
        if changed and now - last_change + _EPS >= quiet_s:
            return Settled(frame, True, False, last_cells, now, touched)
        if not changed and now + _EPS >= settle_s:
            return Settled(frame, False, False, last_cells, now, touched)
        if now + _EPS >= ceiling_s:
            return Settled(frame, changed, True, last_cells, now, touched)
