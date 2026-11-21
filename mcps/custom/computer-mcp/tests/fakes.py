"""Stand-ins for the computer tool's tests: an input library that records every
event, a screen that hands out scripted frames, the system's window reads and
a clock that only moves when the code sleeps."""

from __future__ import annotations

import threading

from PIL import Image, ImageDraw

import executor
import geometry
import screen
from environment import Window

REAL_GUI = executor._gui


def frame_of(img: Image.Image, monitor: geometry.Monitor | None = None) -> screen.Frame:
    """A :class:`screen.Frame` with ``img``'s pixels as mss hands them (BGRA)."""
    mon = monitor or geometry.Monitor(1, 0, 0, img.width, img.height, img.width, img.height, True)
    measured = geometry.Monitor(mon.index, mon.left, mon.top, mon.width, mon.height, img.width, img.height,
                                mon.is_primary)
    return screen.Frame(measured, img.convert("RGB").tobytes("raw", "BGRX"), img.width, img.height)


def desk_image(w: int = 800, h: int = 600, *, boxes=(), colour=(240, 240, 240)) -> Image.Image:
    """A plain desktop with filled boxes ``(x0, y0, x1, y1, (r, g, b))``."""
    img = Image.new("RGB", (w, h), colour)
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1, c in boxes:
        draw.rectangle((x0, y0, x1, y1), fill=c)
    return img


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, seconds)

    def wait(self, event: threading.Event, seconds: float) -> bool:
        if not event.is_set():
            self.sleep(seconds)
        return event.is_set()


class Gui:
    """pyautogui as the executor uses it: every event is recorded, the cursor
    follows the moves, and ``fail_on`` makes one kind of event raise."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.events: list[tuple] = []
        self.pos = (100, 100)
        self.fail_on: str | None = None
        self.on_event = None          # called after each event (a person moving the mouse)
        self.clock = clock
        self.hscroll = self._hscroll

    def _ev(self, *ev) -> None:
        if self.fail_on is not None and tuple(ev[:len(self.fail_on)]) == tuple(self.fail_on):
            raise OSError(f"{ev[0]} failed")
        self.events.append(ev)
        if self.on_event is not None:
            self.on_event(self, ev)

    def position(self):
        return self.pos

    def moveTo(self, x, y, duration=0.0):
        self._ev("moveTo", x, y)
        self.pos = (x, y)

    def click(self, x=None, y=None, clicks=1, button="left", interval=0.0):
        self._ev("click", x, y, clicks, button, interval)
        if x is not None:
            self.pos = (x, y)

    def dragTo(self, x, y, duration=0.0, button="left"):
        self._ev("dragTo", x, y, button)
        self.pos = (x, y)

    def mouseUp(self, button="left"):
        self._ev("mouseUp", button)

    def keyDown(self, key):
        self._ev("keyDown", key)

    def keyUp(self, key):
        self._ev("keyUp", key)

    def write(self, text, interval=0.0):
        self._ev("write", len(text))
        if self.clock is not None:
            self.clock.sleep(0.011 * len(text))

    def scroll(self, amount):
        self._ev("scroll", amount)

    def _hscroll(self, amount):
        self._ev("hscroll", amount)

    def inputs(self) -> list[tuple]:
        return [e for e in self.events if e[0] != "position"]


class Grabber:
    """A screen: each grab hands out the next frame from ``frames`` (the last
    one again when they run out), or what ``source()`` returns."""

    def __init__(self, frames=None, *, source=None, region_image=None) -> None:
        self.frames = list(frames or [])
        self.source = source
        self.grabs = 0
        self.regions: list[dict] = []
        self.region_image = region_image
        self.fail_after: int | None = None
        self.closed = False

    def grab(self, monitor):
        self.grabs += 1
        if self.fail_after is not None and self.grabs > self.fail_after:
            from environment import DeviceUnavailable

            raise DeviceUnavailable("Screen capture failed.")
        if self.source is not None:
            return self.source(monitor)
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame

    def region(self, rect):
        self.regions.append(dict(rect))
        if self.region_image is not None:
            return self.region_image(rect)
        return Image.new("RGB", (rect["width"], rect["height"]), (10, 120, 200))

    def close(self):
        self.closed = True


class Desk:
    """The system's window reads."""

    def __init__(self) -> None:
        self.window = Window(id=1, pid=10, program="form.exe", title="Order form")
        self.under = None
        self.blocked: set = set()
        self.secure = (False, None)
        self.fail: set[str] = set()
        self.hang: set[str] = set()
        self.calls: list[str] = []
        self.programs = {}

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise OSError(f"{name} failed")
        if name in self.hang:
            threading.Event().wait(5)

    def front_window(self):
        self._call("front_window")
        return self.window

    def window_at(self, x, y):
        self._call("window_at")
        return self.under or self.window

    def blocks_input(self, window):
        self._call("blocks_input")
        return window.id in self.blocked

    def secure_input(self):
        self._call("secure_input")
        return self.secure

    def program_of(self, pid):
        return self.programs.get(pid, "")
