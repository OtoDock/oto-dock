"""One call of the computer tool: its steps checked, run in order and reported.

Every call is a list of 1 to :data:`MAX_STEPS` steps and runs here, one at a
time on a desktop. Before each step that acts the runner checks the call's
time, a cancel, the screen, the mouse, the window in front, the spot under the
next click (or the whole screen, for a step with no point of its own) and the
step's own target, and stops before the step when one of them fails: a stop
never acts. After each step that acts it waits until the screen is still
(``still.py``). One picture comes back at the end or at the stop, then one
line per step: done, failed, stopped with the reason, not run.

The baseline of the checks is the last picture the tool DELIVERED (a cancelled
call delivers none), kept in this process for the session's life: the
platform's stand-in keeps a tool it started for a call running until the
session ends, so a new session starts with none. Calls of one turn (one that
arrives while another runs, or within :data:`TURN_GAP_S` of its answer) were
planned on one picture and are judged as one chain.

Typed text never reaches a line, a refusal or a log record: counts only.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import environment
import executor
import geometry
import guards
import screen
import still
from environment import DeviceUnavailable, Window

logger = logging.getLogger("computer-mcp")

MAX_STEPS = 12
MAX_ZOOMS = 3
MAX_SCREENSHOTS = 1
START_LIMIT_S = 30.0          # no step starts later: Codex stops an MCP call at 60 s
TYPE_STOP_S = 40.0            # a long type stops between chunks past this
DELIVERY_LIMIT_S = 55.0       # an answer later than this may never have reached the model
MAX_PAUSE_S = 5.0             # a wait or a held key
MAX_SCROLL = 50
MAX_KEYED_CHARS = 200         # about 10 s on macOS, which sleeps 10 ms per key event
MAX_PASTED_CHARS = 2000
MIN_ZOOM_PX = 8
TURN_GAP_S = 1.0              # a call this soon after another was planned on the same picture
WINDOW_WAIT_S = 1.0           # a named window may take this long to come forward
CLIPBOARD_LAG_S = 1.0         # a pasted clipboard is put back no sooner
ZOOM_BUDGET = 6_000_000       # base64 characters of zooms in one answer (the screenshot adds at most 2 MB)
WHOLE_SCREEN_SHARE = 0.5      # a step with no point stops when this much of the screen changed
SAME_SPOT_PX = 8              # a second spot stop this near the first one runs the step

# The kinds of step.
READ = "read"
PAUSE = "pause"
ACT = "act"

_READS = frozenset({"screenshot", "zoom", "cursor_position"})
_PAUSES = frozenset({"wait"})
_SCREENSHOT = frozenset({"screenshot"})
_ZOOM = frozenset({"zoom"})
_CURSOR = frozenset({"cursor_position"})
_TYPING = frozenset({"type"})
_KEYS = frozenset({"key", "hold_key"})
_HELD = frozenset({"hold_key"})
_KEYBOARD = _TYPING | _KEYS
_CLICKS = frozenset({"left_click", "right_click", "middle_click", "double_click", "triple_click"})
_DRAG = frozenset({"left_click_drag"})
_SCROLL = frozenset({"scroll"})
_POINTED = frozenset({"mouse_move", "left_click_drag"}) | _CLICKS | _SCROLL
_NEEDS_POINT = frozenset({"mouse_move", "left_click_drag"})
_SAVES = _SCREENSHOT | _ZOOM

# A step's outcome.
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

# How a call ended, for the next call of its turn.
COMPLETE = "complete"
BROKEN = "broken"          # it stopped or failed
CHAIN_STOP = "chain stop"  # it was stopped because an earlier call of its turn was

ENTER = "enter"

NO_PICTURE = ("Nothing ran. This tool has no screenshot of this screen from this session yet. "
              "Look at this one, then send the steps again.")
EARLIER_STOPPED = ("Nothing ran. An earlier call of this turn stopped or failed, so this one was planned on a "
                   "screen that is gone. Look at this screenshot, then send the steps again.")
RESIZED = "The screen changed size since your last screenshot."
BUSY = "Another call was using the screen."
TIME_UP = "This call has run for 30 seconds. Send the rest in a new call."
UNREADABLE = "The screen could not be read, so nothing more ran."
MOUSE_MOVED = "Someone moved the mouse. A person may be using this computer."
CANCELLED = "The call was cancelled."
WHOLE_SCREEN = "Most of the screen changed since your last screenshot."


@dataclass
class Step:
    n: int
    args: dict
    action: str
    kind: str
    point: tuple[float, float] | None = None
    start: tuple[float, float] | None = None
    enter: bool = False


@dataclass
class Seen:
    """The last picture delivered for one monitor."""

    frame: screen.Frame
    model_width: int
    model_height: int
    scale: float


@dataclass
class Baseline:
    """What a call's checks compare against."""

    seen: dict[int, Seen]
    window: Window | None
    squares: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class LastCall:
    ended: float
    outcome: str
    display: int
    baseline: Baseline
    spot_stop: tuple[int, tuple[float, float]] | None = None   # (monitor, point) of a spot stop


# Process state, guarded by _LOCK.
_LOCK = threading.Lock()
_SEEN: dict[int, Seen] = {}
_WINDOW_SEEN: Window | None = None
_CURSOR_LEFT: tuple[int, int] | None = None
_LAST: LastCall | None = None
_RUNNING = threading.Event()
_CANCELS: set[threading.Event] = set()
_CANCELS_LOCK = threading.Lock()


def busy() -> bool:
    return _RUNNING.is_set()


def cancel_all() -> None:
    """Set every running call's cancel flag (a signal to stop the process)."""
    with _CANCELS_LOCK:
        for flag in _CANCELS:
            flag.set()


def forget() -> None:
    """Drop the process state (the tests' fresh process)."""
    global _WINDOW_SEEN, _CURSOR_LEFT, _LAST
    _SEEN.clear()
    _WINDOW_SEEN = _CURSOR_LEFT = _LAST = None


# --- the plan --------------------------------------------------------------

def _number(value, what: str, n: int) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Step {n}: '{what}' must hold numbers.") from exc
    if not math.isfinite(out):
        raise ValueError(f"Step {n}: '{what}' must hold finite numbers.")
    return out


def _num_pair(value, what: str, n: int) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"Step {n}: '{what}' must be [x, y] in screenshot pixels.")
    return _number(value[0], what, n), _number(value[1], what, n)


def _seconds(args: dict, n: int) -> float:
    value = _number(args.get("duration", 1.0), "duration", n)
    if not 0 <= value <= MAX_PAUSE_S:
        raise ValueError(f"Step {n}: 'duration' can be 0 to {MAX_PAUSE_S:g} seconds. "
                         "Use several wait steps for a longer pause.")
    return value


def _presses_enter(action: str, args: dict) -> bool:
    """A key, a held key or a click's held keys include Enter. Typed text never
    presses it: a line break goes in one paste (``executor.type_text``)."""
    text = args.get("text")
    if not isinstance(text, str) or not text or not (action in _KEYS or action in _CLICKS):
        return False
    try:
        return ENTER in executor.parse_key_combo(text)
    except ValueError:
        return False


def kind_of(action: str) -> str:
    if action in _READS:
        return READ
    if action in _PAUSES:
        return PAUSE
    return ACT


def check_plan(raw: list[dict], *, model_size: tuple[int, int], seen: Seen | None) -> list[Step]:
    """The steps, or ``ValueError`` with the first problem, its step named.
    Nothing has run when this refuses."""
    steps: list[Step] = []
    zooms = screenshots = 0
    read_seen = False
    model_w, model_h = model_size
    for n, args in enumerate(raw, start=1):
        action = str(args.get("action"))
        kind = kind_of(action)
        step = Step(n, args, action, kind)
        if kind == READ:
            read_seen = True
        elif read_seen:
            raise ValueError(f"Step {n}: put screenshot, zoom and cursor_position after the other steps, "
                             "so that they show the screen at the end.")
        if args.get("region") is not None and action not in _ZOOM:
            raise ValueError(f"Step {n}: only a zoom takes 'region'.")
        if args.get("save") and action not in _SAVES:
            raise ValueError(f"Step {n}: only a screenshot or a zoom takes 'save'.")
        if args.get("window") is not None and kind != ACT:
            raise ValueError(f"Step {n}: only a step that acts takes 'window'.")
        if action in _ZOOM:
            zooms += 1
            region = args.get("region")
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise ValueError(f"Step {n}: a zoom needs 'region': [x0, y0, x1, y1] in screenshot pixels.")
            x0, y0, x1, y1 = (_number(v, "region", n) for v in region)
            if x1 - x0 < MIN_ZOOM_PX or y1 - y0 < MIN_ZOOM_PX:
                raise ValueError(f"Step {n}: 'region' must be at least {MIN_ZOOM_PX} x {MIN_ZOOM_PX} pixels, "
                                 "with x0 < x1 and y0 < y1.")
            if x0 < 0 or y0 < 0 or x1 > model_w or y1 > model_h:
                raise ValueError(f"Step {n}: 'region' must lie inside the screenshot ({model_w}x{model_h}).")
        if action in _SCREENSHOT:
            screenshots += 1
        if action in _PAUSES or action in _HELD:
            _seconds(args, n)
        if action in _KEYS:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Step {n}: '{action}' needs 'text', a key or a combo such as 'ctrl+s' or 'Return'.")
        if action in _TYPING:
            text = args.get("text")
            if not isinstance(text, str):
                raise ValueError(f"Step {n}: 'type' needs 'text', the characters to type.")
            limit = MAX_PASTED_CHARS if executor.types_through_clipboard(text) else MAX_KEYED_CHARS
            if len(text) > limit:
                raise ValueError(f"Step {n}: one type step takes at most {limit} characters here "
                                 f"(this one has {len(text)}). Split the text into several type steps.")
        if action in _SCROLL:
            try:
                amount = int(args.get("scroll_amount", 3))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Step {n}: 'scroll_amount' must be a whole number.") from exc
            if not 1 <= amount <= MAX_SCROLL:
                raise ValueError(f"Step {n}: 'scroll_amount' can be 1 to {MAX_SCROLL}.")
        if action in _POINTED:
            if args.get("coordinate") is not None:
                step.point = _num_pair(args.get("coordinate"), "coordinate", n)
            elif action in _NEEDS_POINT:
                raise ValueError(f"Step {n}: '{action}' needs 'coordinate': [x, y] in screenshot pixels.")
            if action in _DRAG and args.get("start_coordinate") is not None:
                step.start = _num_pair(args.get("start_coordinate"), "start_coordinate", n)
            if seen is not None:
                for p in (step.point, step.start):
                    if p is not None and not (0 <= p[0] < seen.model_width and 0 <= p[1] < seen.model_height):
                        raise ValueError(
                            f"Step {n}: ({p[0]:.0f}, {p[1]:.0f}) lies outside your last screenshot "
                            f"({seen.model_width}x{seen.model_height}).")
        step.enter = _presses_enter(action, args)
        steps.append(step)
    if zooms > MAX_ZOOMS:
        raise ValueError(f"A call can hold at most {MAX_ZOOMS} zoom steps.")
    if screenshots > MAX_SCREENSHOTS:
        raise ValueError("A call can hold one screenshot step.")
    acting = [s for s in steps if s.kind == ACT]
    for s in acting[:-1]:
        if s.enter:
            raise ValueError(f"Step {s.n} presses Enter, so it must be the last step that acts. "
                             "Send what comes after it in a new call, once you have seen the screen.")
    return steps


# --- describing steps --------------------------------------------------------

def _pt(p: tuple[float, float]) -> str:
    return f"({p[0]:.0f}, {p[1]:.0f})"


def describe(step: Step) -> str:
    a, args = step.action, step.args
    if a in _TYPING:
        text = args.get("text")
        return f"type {len(text) if isinstance(text, str) else 0} characters"
    if a in _KEYS:
        out = f"{a} {args.get('text')}"
        return out + (f" for {float(args.get('duration', 1.0)):g}s" if a in _HELD else "")
    if a in _PAUSES:
        return f"{a} {float(args.get('duration', 1.0)):g}s"
    if a in _ZOOM:
        x0, y0, x1, y1 = (float(v) for v in args.get("region") or (0, 0, 0, 0))
        return f"zoom {_pt((x0, y0))}-{_pt((x1, y1))}"
    if a in _SCROLL:
        out = f"scroll {args.get('scroll_direction') or 'down'} {int(args.get('scroll_amount', 3))}"
        return out + (f" at {_pt(step.point)}" if step.point else "")
    if a in _DRAG:
        out = f"{a} to {_pt(step.point)}" if step.point else a
        return out + (f" from {_pt(step.start)}" if step.start else " from the cursor")
    if step.point is not None:
        out = f"{a} {_pt(step.point)}"
    elif a in _CLICKS:
        out = f"{a} at the cursor"
    else:
        out = a
    if a in _CLICKS and args.get("text"):
        out += f" with {args.get('text')}"
    return out


# --- running ---------------------------------------------------------------

@dataclass
class Hooks:
    """What the runner needs from the server: the dispatch of one step and the
    settings. ``dispatch(step_args, ctx)`` returns the cursor's new point (OS
    input space) or None, and an ``executor.Typed`` for a type step."""

    dispatch: Callable[[dict, Context], tuple[tuple[int, int] | None, executor.Typed | None]]
    coord_mode: str
    settle_s: float
    alloc_save_path: Callable[[int | None, str], tuple[str | None, str | None]]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep
    wait: Callable[[threading.Event, float], object] = threading.Event.wait   # a pause a cancel ends


@dataclass
class Context:
    """What a dispatched step reads: its monitor and scale, the cancel-aware
    pause, the check between typed chunks and the bounded clipboard write."""

    monitor: geometry.Monitor
    scale: float
    wait: Callable[[float], object]
    between: Callable[[], str | None] | None = None
    copy: Callable[[str], object] | None = None


@dataclass
class Result:
    lines: list[str]
    pictures: list[str]       # base64 PNGs: the screenshot first, then the zooms
    note: str | None          # the screenshot's own text
    cancelled: bool = False
    refusal: str | None = None


def _not_run(lines: list[str], first: int, last: int) -> None:
    if first > last:
        return
    lines.append(f"{first}: not run." if first == last else f"{first} to {last}: not run.")


class _Stop(Exception):
    def __init__(self, outcome: str, reason: str, *, written: bool = False, sent: bool = False,
                 inside: bool = False) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.written = written   # the step's line is in the result already
        self.sent = sent         # input went out before it: the screen is read again
        self.inside = inside     # it stopped part of the way through its step


def _same_turn(arrived: float) -> LastCall | None:
    """The previous call when this one belongs to its turn: it arrived while
    that one ran (so before it ended) or within :data:`TURN_GAP_S` after."""
    last = _LAST
    if last is not None and arrived - last.ended <= TURN_GAP_S:
        return last
    return None


def run(raw_steps: list[dict], display: int | None, *, arrived: float, cancel: threading.Event,
        hooks: Hooks) -> Result:
    """Run one call. ``arrived`` is when the call reached the tool: the 30 s
    count starts there, before the locks."""
    with _CANCELS_LOCK:
        _CANCELS.add(cancel)
    try:
        left = START_LIMIT_S - (hooks.clock() - arrived)
        if left <= 0 or not _LOCK.acquire(timeout=left):
            return _busy_result(raw_steps)
        try:
            desk_lock = environment.desktop_lock()
            if not desk_lock.acquire(START_LIMIT_S - (hooks.clock() - arrived)):
                return _busy_result(raw_steps)
            _RUNNING.set()
            try:
                return _run_locked(raw_steps, display, arrived=arrived, cancel=cancel, hooks=hooks)
            finally:
                executor.release_held()
                _RUNNING.clear()
                desk_lock.release()
        finally:
            _LOCK.release()
    finally:
        with _CANCELS_LOCK:
            _CANCELS.discard(cancel)


def _busy_result(raw_steps: list[dict]) -> Result:
    total = len(raw_steps)
    lines = [f"Ran 0 of {total} step{'s' if total != 1 else ''}. Stopped before step 1.", f"1: stopped. {BUSY}"]
    _not_run(lines, 2, total)
    return Result(lines, [], None)


def _gates(acts: bool) -> None:
    """The device checks of today, once per call (a step that acts repeats the
    session ones, ``_check_before``)."""
    environment.check_capture_supported()
    environment.check_session_active()
    environment.check_macos_grants(input_needed=acts)
    if acts:
        environment.check_input_supported()


def _run_locked(raw_steps: list[dict], display: int | None, *, arrived: float, cancel: threading.Event,
                hooks: Hooks) -> Result:
    global _WINDOW_SEEN, _CURSOR_LEFT, _LAST
    if cancel.is_set():
        return Result([], [], None, cancelled=True)
    kinds = [kind_of(str(a.get("action"))) for a in raw_steps]
    acts = ACT in kinds
    _gates(acts)

    monitors = screen.list_monitors()
    monitor, count = screen.select_monitor(display, monitors)
    index = monitor.index
    scale = geometry.compute_scale(monitor.phys_width, monitor.phys_height, hooks.coord_mode)
    model_size = geometry.scaled_size(monitor.phys_width, monitor.phys_height, scale)

    prev = _same_turn(arrived)
    follow = prev if prev is not None and prev.outcome == COMPLETE else None
    base = follow.baseline if follow is not None else Baseline(dict(_SEEN), _WINDOW_SEEN)
    seen = base.seen.get(index)
    try:
        steps = check_plan(raw_steps, model_size=model_size, seen=seen)
    except ValueError as e:
        return Result([], [], None, refusal=str(e))

    squares = list(base.squares) if follow is not None and follow.display == index else []
    run_state = _Run(steps, monitor=monitor, monitors=monitors, scale=scale, hooks=hooks, cancel=cancel,
                     arrived=arrived, display=display, checks=guards.Checks(environment.desktop()),
                     squares=squares, expected=base.window, seen=seen,
                     repeat_spot=_LAST.spot_stop if _LAST is not None and prev is None else None,
                     index=index)
    if prev is not None and prev.outcome == BROKEN and run_state.acts:
        run_state.chain_stop = True
    try:
        run_state.go()
        delivered, cap = run_state.picture(count)
    finally:
        run_state.close()

    if delivered and cap is not None:
        _SEEN[index] = Seen(cap.frame, cap.model_width, cap.model_height, cap.scale)
        _WINDOW_SEEN = run_state.checks.front()
        if _CURSOR_LEFT is None or run_state.mouse_stopped:
            _CURSOR_LEFT = run_state.checks.cursor()   # the model sees where the person left it
    if cap is None and not run_state.acts and _LAST is not None and run_state.stop is None:
        _LAST.ended = hooks.clock()   # a zoom alone keeps its turn's chain
    else:
        outcome = (CHAIN_STOP if run_state.chain_stop else
                   COMPLETE if run_state.stop is None and delivered and not cancel.is_set() else BROKEN)
        _LAST = LastCall(hooks.clock(), outcome, index, Baseline(base.seen, run_state.expected, run_state.squares),
                         spot_stop=run_state.spot_stop)
    return Result(run_state.text_lines(), run_state.pictures, run_state.note, cancelled=cancel.is_set())


class _Run:
    """The state of one call's run, step by step."""

    def __init__(self, steps: list[Step], *, monitor, monitors, scale, hooks: Hooks, cancel, arrived,
                 display, checks: guards.Checks, squares, expected, seen: Seen | None,
                 repeat_spot, index: int) -> None:
        self.steps, self.monitor, self.monitors, self.scale = steps, monitor, monitors, scale
        self.hooks, self.cancel, self.arrived, self.display = hooks, cancel, arrived, display
        self.checks, self.squares, self.expected, self.seen = checks, squares, expected, seen
        self.repeat_spot, self.index = repeat_spot, index
        self.settle = max(0.0, hooks.settle_s)
        self.grabber = screen.open_grabber()
        self.current: screen.Frame | None = None
        self.unreadable = False
        self.mask: bytearray | None = None
        self.ran = 0
        self.stop: tuple[int, str, str] | None = None
        self.stop_inside = False
        self.chain_stop = False
        self.mouse_stopped = False
        self.spot_stop: tuple[int, tuple[float, float]] | None = None
        self.lines: list[str] = []
        self.notes: list[str] = []
        self.zooms: list[str] = []
        self.pictures: list[str] = []
        self.note: str | None = None
        self.acts = any(st.kind == ACT for st in steps)
        self.shows = self.acts or any(st.kind == PAUSE or st.action in _SCREENSHOT for st in steps)

    def close(self) -> None:
        self.grabber.close()

    def elapsed(self) -> float:
        return self.hooks.clock() - self.arrived

    def grab(self) -> screen.Frame | None:
        try:
            return self.grabber.grab(self.monitor)
        except DeviceUnavailable:
            self.unreadable = True
            return None

    def pause(self, seconds: float) -> None:
        if seconds > 0:
            self.hooks.wait(self.cancel, seconds)
        if self.cancel.is_set():
            raise _Stop(STOPPED, CANCELLED)

    def gate_time(self) -> None:
        if self.elapsed() > START_LIMIT_S:
            raise _Stop(STOPPED, TIME_UP)
        if self.cancel.is_set():
            raise _Stop(STOPPED, CANCELLED)

    def go(self) -> None:
        try:
            if self.chain_stop:
                raise _Stop(STOPPED, EARLIER_STOPPED)
            pointed = any(st.point is not None or st.start is not None for st in self.steps)
            if pointed and self.seen is None:
                raise _Stop(STOPPED, NO_PICTURE)
            if self.acts:
                first = self.grab()
                if self.settle > 0:
                    self.hooks.sleep(still.PROBE_S)
                    self.current = self.grab()
                    cells = still.frame_cells(first, self.current) if first and self.current else None
                    cols = still.grid_size(self.monitor.phys_width, self.monitor.phys_height)[0]
                    self.mask = still.new_mask(cells, cols) if cells is not None else None
                else:
                    self.current = first
                if self.expected is None:
                    self.expected = self.checks.front()
            for step in self.steps:
                self.gate_time()
                if step.kind == ACT:
                    clip = _save_clipboard(step, self)
                    self.gate_time()
                    self.expected = _check_before(step, self.checks, run=self)
                    self.gate_time()
                    _act(step, self, clip)
                elif step.kind == PAUSE:
                    self.pause(_seconds(step.args, step.n))
                    self.current = self.grab()
                    self.lines.append(f"{step.n} {describe(step)}: {DONE}")
                else:
                    _read(step, self)
                self.ran += 1
        except _Stop as s:
            self._stopped(s)
        except Exception as e:  # noqa: BLE001  # any failure is the current step's line, never a lost answer
            logger.warning("computer step %d failed outside its action: %s", self.ran + 1, type(e).__name__)
            self._stopped(_Stop(FAILED, f"Unexpected error ({type(e).__name__}).", sent=True))

    def _stopped(self, s: _Stop) -> None:
        n = self.steps[self.ran].n if self.ran < len(self.steps) else len(self.steps)
        self.stop = (n, s.outcome, s.reason)
        self.stop_inside = s.inside
        self.mouse_stopped = s.reason == MOUSE_MOVED
        if not s.written:
            self.lines.append(f"{n} {describe(self.steps[n - 1])}: {s.outcome}. {s.reason}")
        _not_run(self.lines, n + 1, len(self.steps))
        if s.sent:
            # Input went out: show the screen as it is now, not as it was.
            self.hooks.sleep(still.QUIET_S)
            self.current = None

    def picture(self, count: int) -> tuple[bool, screen.Capture | None]:
        """Encode the final picture; ``(delivered, capture)``."""
        if not (self.shows or self.stop is not None):
            self.pictures = list(self.zooms)
            return False, None
        frame = self.current or self.grab()
        if frame is None:
            self.note = "The screen could not be captured, so no screenshot came back."
            self.pictures = list(self.zooms)
            return False, None
        save = any(st.action in _SCREENSHOT and st.args.get("save") for st in self.steps[:self.ran])
        save_abs, save_rel = self.hooks.alloc_save_path(self.display, "screen") if save else (None, None)
        cap = screen.encode(frame, self.hooks.coord_mode, monitor_count=count, save_path=save_abs)
        self.note = (f"Screenshot of display {cap.monitor.index} of {cap.monitor_count} "
                     f"({cap.model_width}x{cap.model_height} px). Click coordinates are in this image's "
                     f"pixel space: (0,0) top-left, max ({cap.model_width - 1},{cap.model_height - 1}).")
        if save:
            if cap.saved_path and save_rel:
                self.note += (f"\nSaved full-resolution screenshot to '{save_rel}' "
                              f"(viewable in the dashboard file preview). On this computer: {save_abs}")
            else:
                self.note += "\n(Could not save to the workspace, returned inline only.)"
        self.pictures = [cap.png_b64, *self.zooms]
        delivered = not self.cancel.is_set() and self.elapsed() <= DELIVERY_LIMIT_S
        return delivered, cap

    def text_lines(self) -> list[str]:
        total = len(self.steps)
        head = f"Ran {self.ran} of {total} step{'s' if total != 1 else ''}."
        if self.stop is not None:
            n, outcome, _reason = self.stop
            if outcome == FAILED:
                head += f" Step {n} failed."
            elif self.stop_inside:
                head += f" Stopped in step {n}."
            else:
                head += f" Stopped before step {n}."
        return [head, *self.lines, *self.notes, *(self.checks.off_lines() if self.acts else [])]


def _check_before(step: Step, checks: guards.Checks, *, run: _Run) -> Window | None:
    """The checks before a step that acts, in their order. Returns the window
    expected in front from here on, or raises :class:`_Stop`."""
    if run.unreadable:
        raise _Stop(STOPPED, UNREADABLE)
    try:
        environment.check_session_active()
        environment.check_macos_grants(input_needed=True)
    except DeviceUnavailable as e:
        raise _Stop(FAILED, str(e)) from e
    if guards.mouse_moved(checks.cursor(), _CURSOR_LEFT):
        raise _Stop(STOPPED, MOUSE_MOVED)
    expected = run.expected
    front = checks.front()
    name = step.args.get("window")
    if isinstance(name, str) and name.strip() and front is not None:
        deadline = run.hooks.clock() + WINDOW_WAIT_S
        waited = False
        while front is not None and not guards.matches(front, name) and run.hooks.clock() < deadline:
            run.hooks.sleep(still.PROBE_S)
            waited = True
            front = checks.front()
        if front is not None and not guards.matches(front, name):
            raise _Stop(STOPPED, f"The window in front is {_program(front)}, not \"{name.strip()}\".")
        expected = front if front is not None else expected
        if waited:
            run.current = run.grab()   # what came forward during the wait is on the screen now
    elif front is not None:
        if expected is not None and front.id != expected.id:
            raise _Stop(STOPPED, f"Another window came to the front ({_program(front)}). If you meant it, "
                                 "send the step again with window set to part of its name.")
        expected = front
    _check_screen(step, run)
    _own_target(step, checks, front=front, run=run)
    return expected


def _check_screen(step: Step, run: _Run) -> None:
    """The spot under the step's point, or the whole screen for a step with
    none (a key, a type, a click at the cursor), against the model's picture."""
    seen, current = run.seen, run.current
    if seen is None or current is None:
        return
    if (current.width, current.height) != (seen.frame.width, seen.frame.height):
        raise _Stop(STOPPED, RESIZED)
    points = [p for p in (step.start, step.point) if p is not None]
    if not points:
        cells = still.frame_cells(seen.frame, current)
        if cells and still.count_outside(cells, run.mask) >= WHOLE_SCREEN_SHARE * len(cells):
            raise _Stop(STOPPED, WHOLE_SCREEN)
        return
    for p in points:
        if not guards.spot_changed(seen.frame, current, p, run.scale, earlier=run.squares, mask=run.mask):
            continue
        again = run.repeat_spot
        if again is not None and again[0] == run.index and max(
                abs(again[1][0] - p[0]), abs(again[1][1] - p[1])) <= SAME_SPOT_PX:
            run.notes.append(f"Step {step.n}: the screen under {_pt(p)} keeps changing, so it was not checked.")
            continue
        run.spot_stop = (run.index, p)
        raise _Stop(STOPPED, f"The screen under {_pt(p)} changed since your last screenshot.")


def _program(window: Window | None) -> str:
    if window is None:
        return "a window this tool cannot name"
    return window.program or "a window with no program name"


def _own_target(step: Step, checks: guards.Checks, *, front: Window | None, run: _Run) -> None:
    """Windows: the program the input goes to has no more rights than this
    one. macOS: Secure Input is not held by the window that takes the keys."""
    target = front
    if step.point is not None and step.action not in _KEYBOARD:
        gx, gy = geometry.model_to_input_coords(step.point[0], step.point[1], monitor=run.monitor, scale=run.scale)
        under = checks.window_at(int(round(gx)), int(round(gy)))
        target = under if under is not None else front
    if target is not None and checks.blocks(target) is True:
        raise _Stop(FAILED, "That window belongs to a program with more rights than this computer's OtoDock "
                            "(an administrator program or the system), so this tool cannot reach it.")
    if step.action in _KEYBOARD:
        secure = checks.secure_input()
        if secure is not None and secure[0]:
            holder = secure[1]
            if holder is not None and front is not None and front.pid == holder:
                raise _Stop(FAILED, "macOS Secure Input is on in the window in front (a password field), "
                                    "so typed keys would be lost.")
            who = checks.program_of(holder) if holder else ""
            run.notes.append(f"Step {step.n}: Secure Input is on in {who or 'another program'}, "
                             "so typed keys may be lost.")


@dataclass
class _Clip:
    saved: str


def _save_clipboard(step: Step, run: _Run) -> _Clip | None:
    """What the clipboard held before a paste, read before the step's checks
    (so a cancel during the read is seen), or None when nothing is pasted."""
    if step.action not in _TYPING or not executor.types_through_clipboard(str(step.args.get("text") or "")):
        return None
    try:
        return _Clip(str(guards.bounded("clipboard", executor.clipboard_text, CLIPBOARD_LAG_S)))
    except guards._Off:
        run.notes.append(f"Step {step.n}: the clipboard could not be read, so it now holds the typed text.")
        return None


def _act(step: Step, run: _Run, clip: _Clip | None) -> None:
    """Dispatch one step that acts, then wait until the screen is still."""
    global _CURSOR_LEFT
    checks, hooks = run.checks, run.hooks

    def between() -> str | None:
        if run.cancel.is_set():
            return CANCELLED
        if run.elapsed() > TYPE_STOP_S:
            return "This call ran too long."
        if guards.mouse_moved(checks.cursor(), _CURSOR_LEFT):
            return MOUSE_MOVED
        front = checks.front()
        if front is not None and run.expected is not None and front.id != run.expected.id:
            return f"Another window came to the front ({_program(front)})."
        return None

    def copy(value: str) -> None:
        guards.bounded("clipboard", lambda: executor.set_clipboard_text(value), CLIPBOARD_LAG_S)

    ctx = Context(run.monitor, run.scale, wait=run.pause, between=between, copy=copy)
    started = hooks.clock()
    try:
        moved, typed = hooks.dispatch(step.args, ctx)
    except _Stop:
        raise
    except Exception as e:  # a library failure fails the step, never the call
        logger.warning("computer step %d (%s) failed: %s", step.n, step.action, type(e).__name__)
        if clip is not None:
            _put_back_clipboard(step, clip, landed=True, since=started, run=run)
        if isinstance(e, (DeviceUnavailable, ValueError)):
            reason = str(e) if step.action not in _TYPING or isinstance(e, DeviceUnavailable) else \
                "The text could not be typed."
        else:
            reason = f"Unexpected error ({type(e).__name__})."
        raise _Stop(FAILED, reason, sent=True) from e
    if moved is not None:
        _CURSOR_LEFT = (int(moved[0]), int(moved[1]))
    elif _CURSOR_LEFT is None:
        _CURSOR_LEFT = checks.cursor()
    for p in (step.point, step.start):
        if p is not None:
            run.squares.append(guards.patch_rect(p, run.scale)[:4])
    if typed is not None and typed.stopped:
        run.lines.append(f"{step.n} {describe(step)}: {STOPPED} after {typed.typed}. {typed.stopped}")
        raise _Stop(STOPPED, typed.stopped, written=True, sent=True, inside=True)
    slow = landed = False
    if run.settle > 0 and run.current is not None:
        try:
            settled = still.wait_until_still(lambda: _probe(run), run.current, mask=run.mask,
                                             settle_s=run.settle, clock=hooks.clock, sleep=hooks.sleep)
            run.current, slow, landed = settled.frame, settled.slow, settled.touched
            if run.mask is not None and settled.last_cells is not None and slow:
                still.merge_into(run.mask, settled.last_cells)
        except DeviceUnavailable:
            run.current = None
            hooks.sleep(run.settle)
    else:
        if run.settle > 0:
            hooks.sleep(run.settle)
        run.current = run.grab()
    if clip is not None and run.settle > 0:
        _put_back_clipboard(step, clip, landed=landed, since=started, run=run)
    run.lines.append(f"{step.n} {describe(step)}: {DONE}" + (" (the screen was still changing after 2 s)" if slow else ""))


def _probe(run: _Run) -> screen.Frame:
    frame = run.grab()
    if frame is None:
        raise DeviceUnavailable("Screen capture failed.")
    return frame


def _put_back_clipboard(step: Step, clip: _Clip, *, landed: bool, since: float, run: _Run) -> None:
    """Put back the clipboard a paste replaced, once the paste landed (any
    pixel changed during the wait) and no sooner than :data:`CLIPBOARD_LAG_S`
    after it; when nothing changed, empty it, so a late paste gets nothing."""
    text = str(step.args.get("text") or "")
    wait = CLIPBOARD_LAG_S - (run.hooks.clock() - since)
    if wait > 0:
        run.hooks.sleep(wait)
    try:
        if guards.bounded("clipboard", executor.clipboard_text, CLIPBOARD_LAG_S) != text:
            return
        value = clip.saved if landed else ""
        guards.bounded("clipboard", lambda: executor.set_clipboard_text(value), CLIPBOARD_LAG_S)
    except guards._Off:
        run.notes.append(f"Step {step.n}: the clipboard could not be put back, so it holds the typed text.")


def _zoom_rect(region, monitor: geometry.Monitor, scale: float) -> dict:
    """The zoom's region in the OS input space (where the click goes), rounded
    outward and clipped to its monitor: a box at the picture's right edge
    maps a fraction past the monitor."""
    x0, y0, x1, y1 = (float(v) for v in region)
    gl, gt = geometry.model_to_input_coords(x0, y0, monitor=monitor, scale=scale)
    gr, gb = geometry.model_to_input_coords(x1, y1, monitor=monitor, scale=scale)
    left, top = max(monitor.left, math.floor(gl)), max(monitor.top, math.floor(gt))
    right = min(monitor.left + monitor.width, math.ceil(gr))
    bottom = min(monitor.top + monitor.height, math.ceil(gb))
    return {"left": left, "top": top, "width": max(1, right - left), "height": max(1, bottom - top)}


def _read(step: Step, run: _Run) -> None:
    if step.action in _SCREENSHOT:
        run.lines.append(f"{step.n} {describe(step)}: picture 1")
        if run.current is None:
            run.current = run.grab()
        return
    if step.action in _CURSOR:
        try:
            gx, gy = executor.cursor_position()
        except Exception as e:  # noqa: BLE001  # a read that fails is a line, never a failed call
            run.lines.append(f"{step.n} {describe(step)}: the cursor could not be read ({type(e).__name__}).")
            return
        under = next((m for m in run.monitors
                      if m.left <= gx < m.left + m.width and m.top <= gy < m.top + m.height), run.monitor)
        s = geometry.compute_scale(under.phys_width, under.phys_height, run.hooks.coord_mode)
        mx, my = geometry.input_to_model_coords(gx, gy, monitor=under, scale=s)
        run.lines.append(f"{step.n} {describe(step)}: ({mx:.0f}, {my:.0f}) in display {under.index}'s "
                         "screenshot space")
        return
    x0, _y0, x1, _y1 = (float(v) for v in step.args["region"])
    try:
        img = run.grabber.region(_zoom_rect(step.args["region"], run.monitor, run.scale))
    except DeviceUnavailable:
        run.lines.append(f"{step.n} {describe(step)}: the region could not be captured.")
        return
    saved = ""
    if step.args.get("save"):
        save_abs, save_rel = run.hooks.alloc_save_path(run.display, "zoom")
        # The full path too: a file saved this turn is on this computer before the
        # platform has its copy, and the image tool reads it here at once.
        saved = f" Saved to '{save_rel}'. On this computer: {save_abs}" if save_abs and screen.save_png(
            img, save_abs) else \
            " It could not be saved to the workspace."
    data, w, h, _s = screen.png_b64(img, run.hooks.coord_mode)
    if sum(len(z) for z in run.zooms) + len(data) > ZOOM_BUDGET:
        run.lines.append(f"{step.n} {describe(step)}: left out, the answer would be too large.{saved}")
        return
    run.zooms.append(data)
    number = len(run.zooms) + (1 if run.shows else 0)
    factor = w / max(1.0, x1 - x0)
    run.lines.append(f"{step.n} {describe(step)}: picture {number}, {w}x{h} px, at {factor:.2f}x your "
                     f"screenshot's scale. Click coordinates stay in your screenshot's space.{saved}")
