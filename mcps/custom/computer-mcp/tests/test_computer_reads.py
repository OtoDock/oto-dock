"""The computer tool's reads: the still wait on made-up frame series, the cells
of two frames, the spot under a click, the mapping of a patch and a zoom, the
bounded reads, and each system's window reads with its library faked (Windows,
macOS and X11 code cannot run on this box; the operator's test and the rig
prove them on real desktops)."""

from __future__ import annotations

import ctypes
import random
import sys
import threading
import types

import fakes
import pytest
from PIL import Image

import desktop_macos
import desktop_windows
import desktop_x11
import environment
import executor
import geometry
import guards
import screen
import still
from environment import Window

# ===========================================================================
# cells
# ===========================================================================

@pytest.mark.parametrize("block", [1, 3, 5, 11, 16, 17, 25, 40])
def test_a_lone_changed_pixel_marks_exactly_its_own_cell(block):
    rnd = random.Random(block)
    for _ in range(40):
        w, h = rnd.randint(1, 300), rnd.randint(1, 200)
        a = Image.new("RGB", (w, h), (10, 20, 30))
        b = a.copy()
        x, y = rnd.randrange(w), rnd.randrange(h)
        b.putpixel((x, y), (10, 20, 31))   # one unit, one band
        grid, cols, rows = still.changed_grid(a, b, block)
        assert (cols, rows) == still.grid_size(w, h, block)
        assert [i for i, v in enumerate(grid) if v] == [(y // block) * cols + x // block]


def test_identical_frames_have_no_changed_cell():
    f = fakes.frame_of(fakes.desk_image())
    assert set(still.frame_cells(f, f)) == {0}


def test_frames_of_another_size_are_not_compared():
    assert still.frame_cells(fakes.frame_of(fakes.desk_image(100, 100)), fakes.frame_of(fakes.desk_image(90, 100))) is None


def test_the_mask_grows_by_one_cell():
    cells = bytearray(25)
    cells[12] = 1                     # the middle of a 5 x 5 grid
    mask = still.new_mask(bytes(cells), 5)
    assert [i for i, v in enumerate(mask) if v] == [6, 7, 8, 11, 12, 13, 16, 17, 18]


# ===========================================================================
# the still wait on made-up series (each probe is 50 ms)
# ===========================================================================

def _series(*imgs):
    frames = [fakes.frame_of(i) for i in imgs]
    state = {"i": 0}

    def probe():
        f = frames[min(state["i"], len(frames) - 1)]
        state["i"] += 1
        return f
    return probe


BASE = fakes.desk_image(320, 240)
CHANGED = fakes.desk_image(320, 240, boxes=[(10, 10, 200, 120, (0, 0, 0))])
CHANGED_MORE = fakes.desk_image(320, 240, boxes=[(10, 10, 200, 220, (0, 0, 0))])
CARET = fakes.desk_image(320, 240, boxes=[(100, 100, 101, 115, (0, 0, 0))])


def _wait(probe, *, mask=None, settle=0.7):
    clock = fakes.Clock()
    out = still.wait_until_still(probe, fakes.frame_of(BASE), mask=mask, settle_s=settle,
                                 clock=clock.clock, sleep=clock.sleep)
    return out, round(out.seconds, 2)


def test_a_reaction_ends_the_wait_after_the_floor_and_the_quiet():
    out, t = _wait(_series(CHANGED))
    assert out.changed and not out.slow
    assert t == 0.5        # changed at 0.05, quiet for 0.45


def test_the_wait_never_ends_before_the_quiet_time():
    out, t = _wait(_series(CHANGED))
    assert t >= still.QUIET_S


def test_a_second_reaction_restarts_the_quiet():
    # the focus ring at once, a suggestion list 300 ms after the typing
    probe = _series(*([CHANGED] * 6 + [CHANGED_MORE]))
    out, t = _wait(probe)
    assert out.changed and t == 0.8
    assert out.frame.raw == fakes.frame_of(CHANGED_MORE).raw


def test_a_late_reaction_inside_the_settle_time_is_caught():
    probe = _series(*([BASE] * 9 + [CHANGED]))
    out, t = _wait(probe)
    assert out.changed and t == 0.95


def test_no_visible_effect_waits_the_settle_time():
    out, t = _wait(_series(BASE))
    assert not out.changed and t == 0.7


def test_a_blinking_caret_is_tolerated():
    probe = _series(*([CARET, BASE] * 10))
    out, t = _wait(probe)
    assert not out.changed and t == 0.7


def test_a_change_that_never_stops_waits_the_ceiling():
    imgs = [fakes.desk_image(320, 240, boxes=[(10, 10, 200, 120, (i * 9 % 256, 0, 0))]) for i in range(60)]
    out, t = _wait(_series(*imgs))
    assert out.slow and t == 2.0


def test_a_masked_video_holds_nothing():
    imgs = [fakes.desk_image(320, 240, boxes=[(0, 0, 63, 63, (i * 9 % 256, 0, 0))]) for i in range(60)]
    cells = still.frame_cells(fakes.frame_of(imgs[0]), fakes.frame_of(imgs[1]))
    mask = still.new_mask(cells, still.grid_size(320, 240)[0])
    out, t = _wait(_series(*imgs), mask=mask)
    assert not out.slow and t == 0.7


def test_the_last_probe_is_the_frame_returned():
    out, _t = _wait(_series(CHANGED))
    assert out.frame.raw == fakes.frame_of(CHANGED).raw


def test_a_probe_that_raises_ends_the_wait_with_it():
    def probe():
        raise environment.DeviceUnavailable("gone")
    with pytest.raises(environment.DeviceUnavailable):
        _wait(probe)


# ===========================================================================
# the spot under a click
# ===========================================================================

def _spot(seen_img, now_img, point, *, scale=1.0, earlier=(), mask=None):
    return guards.spot_changed(fakes.frame_of(seen_img), fakes.frame_of(now_img), point, scale,
                               earlier=list(earlier), mask=mask)


def test_a_dialog_over_the_point_counts():
    now = fakes.desk_image(boxes=[(300, 200, 500, 400, (20, 20, 20))])
    assert _spot(fakes.desk_image(), now, (400, 300))


def test_a_caret_at_the_point_does_not_count():
    now = fakes.desk_image(boxes=[(400, 290, 401, 310, (0, 0, 0))])
    assert not _spot(fakes.desk_image(), now, (400, 300))


def test_a_small_change_beside_the_point_does_not_count():
    now = fakes.desk_image(boxes=[(380, 280, 388, 290, (0, 0, 0))])   # a clock digit off the centre
    assert not _spot(fakes.desk_image(), now, (400, 300))


def test_a_small_change_at_the_point_counts():
    now = fakes.desk_image(boxes=[(395, 295, 406, 306, (0, 0, 0))])   # an icon on the target itself
    assert _spot(fakes.desk_image(), now, (400, 300))


def test_a_change_over_a_quarter_of_the_square_counts():
    now = fakes.desk_image(boxes=[(376, 276, 424, 290, (0, 0, 0))])   # the top quarter of the square
    assert _spot(fakes.desk_image(), now, (400, 300))


def test_an_earlier_steps_square_is_left_out_but_the_centre_still_counts():
    seen = fakes.desk_image()
    # field one at y 100, field two at y 132: a list opened over field two
    first = guards.patch_rect((200, 100), 1.0)[:4]
    focus_ring = fakes.desk_image(boxes=[(150, 80, 250, 122, (0, 120, 255))])
    assert _spot(seen, focus_ring, (200, 132))            # without the earlier square it would stop
    assert not _spot(seen, focus_ring, (200, 132), earlier=[first])
    suggestion = fakes.desk_image(boxes=[(150, 120, 260, 220, (255, 255, 200))])
    assert _spot(seen, suggestion, (200, 132), earlier=[first])


def test_a_moving_cell_is_left_out():
    seen = fakes.desk_image()
    now = fakes.desk_image(boxes=[(384, 284, 416, 316, (0, 0, 0))])
    cols = still.grid_size(800, 600)[0]
    mask = bytearray(cols * still.grid_size(800, 600)[1])
    for r in range(17, 20):
        for c in range(23, 27):
            mask[r * cols + c] = 1
    assert not _spot(seen, now, (400, 300), mask=mask)


def test_a_square_at_the_frame_edge_is_clipped():
    now = fakes.desk_image(boxes=[(0, 0, 30, 30, (0, 0, 0))])
    assert _spot(fakes.desk_image(), now, (2, 2))


@pytest.mark.parametrize("scale", [1.0, 0.745, 0.372])
def test_the_square_is_about_48_model_pixels_on_a_scaled_screen(scale):
    left, top, right, bottom, block = guards.patch_rect((400, 300), scale)
    side_model = (right - left) * scale
    assert 40 <= side_model <= 56
    assert (right - left) == block * geometry.PATCH_CELLS
    cx, cy = geometry.model_to_physical(400, 300, scale=scale)
    assert left <= cx <= right and top <= cy <= bottom


# ===========================================================================
# the mapping of a click, a patch and a zoom
# ===========================================================================

def test_a_click_and_its_patch_meet_on_one_physical_pixel_on_a_second_monitor_left_of_the_primary():
    mon = geometry.Monitor(2, -1920, -200, 1920, 1080, 1920, 1080)
    scale = geometry.compute_scale(1920, 1080, "rescale")
    gx, gy = geometry.model_to_input_coords(700, 400, monitor=mon, scale=scale)
    px, py = geometry.model_to_physical(700, 400, scale=scale)
    assert (gx - mon.left) * mon.dpr_x == pytest.approx(px)
    assert (gy - mon.top) * mon.dpr_y == pytest.approx(py)
    left, top, right, bottom, _block = guards.patch_rect((700, 400), scale)
    assert left <= px <= right and top <= py <= bottom
    assert abs((left + right) / 2 - px) <= 1 and abs((top + bottom) / 2 - py) <= 1


def test_a_retina_factor_of_two_maps_the_click_to_points_and_the_patch_to_pixels():
    mon = geometry.Monitor(1, 0, 0, 1440, 900, 2880, 1800)
    scale = geometry.compute_scale(2880, 1800, "rescale")
    gx, gy = geometry.model_to_input_coords(100, 50, monitor=mon, scale=scale)
    assert gx == pytest.approx(100 / scale / 2) and gy == pytest.approx(50 / scale / 2)
    left, _top, right, _bottom, block = guards.patch_rect((100, 50), scale)
    assert block == round(4 / scale) and (left + right) / 2 == pytest.approx(100 / scale, abs=1)


def test_a_zoom_at_the_bottom_right_corner_stays_on_the_monitor():
    import steps

    for w, h in ((1920, 1080), (2560, 1440), (3840, 2160)):
        mon = geometry.Monitor(1, 0, 0, w, h, w, h, True)
        scale = geometry.compute_scale(w, h, "rescale")
        mw, mh = geometry.scaled_size(w, h, scale)
        rect = steps._zoom_rect([mw - 200, mh - 100, mw, mh], mon, scale)
        assert rect["left"] + rect["width"] == w and rect["top"] + rect["height"] == h


def test_a_zoom_on_a_monitor_left_of_the_primary_is_clipped_to_it():
    import steps

    mon = geometry.Monitor(2, -1920, 0, 1920, 1080, 1920, 1080)
    scale = geometry.compute_scale(1920, 1080, "rescale")
    rect = steps._zoom_rect([0, 0, 1430, 804], mon, scale)
    assert rect == {"left": -1920, "top": 0, "width": 1920, "height": 1080}


def test_a_zoom_on_a_second_monitor_grabs_its_global_rectangle(monkeypatch):
    import steps

    mon = geometry.Monitor(2, -1920, 0, 1920, 1080, 1920, 1080)
    grabber = fakes.Grabber(frames=[fakes.frame_of(fakes.desk_image(1920, 1080), mon)])
    run = types.SimpleNamespace(monitor=mon, scale=geometry.compute_scale(1920, 1080, "rescale"),
                                grabber=grabber, hooks=types.SimpleNamespace(coord_mode="rescale"), zooms=[],
                                shows=False, lines=[], display=2)
    step = steps.Step(1, {"action": "zoom", "region": [0, 0, 200, 100]}, "zoom", steps.READ)
    steps._read(step, run)
    rect = grabber.regions[0]
    assert rect["left"] == -1920 and rect["top"] == 0
    assert rect["width"] == round(200 / run.scale) or abs(rect["width"] - 200 / run.scale) <= 1
    assert "1 zoom (0, 0)-(200, 100): picture 1" in run.lines[0]


def test_a_zoom_is_never_enlarged_and_a_large_one_is_shrunk():
    small = Image.new("RGB", (120, 80))
    _data, w, h, _s = screen.png_b64(small, "rescale")
    assert (w, h) == (120, 80)
    big = Image.new("RGB", (3000, 2000))
    _data, w, h, _s = screen.png_b64(big, "rescale")
    assert max(w, h) <= 1568 and w * h <= 1_150_000 + 3000


def test_a_zoom_past_the_answers_budget_is_left_out(monkeypatch):
    import steps

    monkeypatch.setattr(steps, "ZOOM_BUDGET", 10)
    mon = geometry.Monitor(1, 0, 0, 800, 600, 800, 600, True)
    run = types.SimpleNamespace(monitor=mon, scale=1.0, grabber=fakes.Grabber(frames=[None]),
                                hooks=types.SimpleNamespace(coord_mode="rescale"), zooms=[], shows=True,
                                lines=[], display=None)
    steps._read(steps.Step(2, {"action": "zoom", "region": [0, 0, 100, 100]}, "zoom", steps.READ), run)
    assert run.zooms == [] and "left out, the answer would be too large" in run.lines[0]


def test_a_zoom_on_macos_grabs_at_the_backing_scale_and_puts_the_option_back(monkeypatch):
    seen = []
    darwin = types.ModuleType("mss.darwin")
    darwin.kCGWindowImageNominalResolution = 1 << 4
    darwin.IMAGE_OPTIONS = 1 | 2 | (1 << 4)
    monkeypatch.setitem(sys.modules, "mss.darwin", darwin)
    mss_pkg = types.ModuleType("mss")
    mss_pkg.darwin = darwin

    class Sct:
        def grab(self, rect):
            seen.append(darwin.IMAGE_OPTIONS)
            shot = types.SimpleNamespace(width=rect["width"] * 2, height=rect["height"] * 2)
            shot.raw = bytes(shot.width * shot.height * 4)
            return shot
    mss_pkg.mss = Sct
    monkeypatch.setitem(sys.modules, "mss", mss_pkg)
    monkeypatch.setattr(screen, "os_kind", lambda: "darwin")
    g = screen.Grabber()
    img = g.region({"left": 10, "top": 10, "width": 50, "height": 20})
    assert img.size == (100, 40)
    assert seen == [1 | 2] and darwin.IMAGE_OPTIONS == 1 | 2 | (1 << 4)


# ===========================================================================
# the executor
# ===========================================================================

def test_dpi_awareness_comes_before_pyautoguis_import(monkeypatch):
    order = []
    monkeypatch.setattr(screen, "ensure_dpi_aware", lambda: order.append("dpi"))

    class Pyautogui(types.ModuleType):
        def __setattr__(self, name, value):
            if name == "FAILSAFE":
                order.append("pyautogui")
            super().__setattr__(name, value)
    monkeypatch.setitem(sys.modules, "pyautogui", Pyautogui("pyautogui"))
    monkeypatch.setattr(executor, "_pyautogui", None)
    fakes.REAL_GUI()
    assert order == ["dpi", "pyautogui"]


def test_type_goes_in_chunks_and_asks_between_them(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)
    asked = []
    out = executor.type_text("a" * 25, between=lambda: asked.append(1))
    assert [e[1] for e in gui.events] == [3] * 8 + [1] and len(asked) == 8
    assert out.summary == "typed 25 characters" and out.stopped is None


def test_type_stops_where_it_was_told(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)
    out = executor.type_text("a" * 25, between=lambda: "stop here")
    assert out.typed == 3 and out.stopped == "stop here" and out.summary == "typed 3 of 25 characters"


def test_a_paste_names_no_text(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)
    monkeypatch.setattr(executor, "os_kind", lambda: "linux")
    monkeypatch.setitem(sys.modules, "pyperclip", types.SimpleNamespace(copy=lambda v: None))
    out = executor.type_text("Καλημέρα")
    assert out.summary == "pasted 8 characters via clipboard" and "Καλημέρα" not in out.summary
    assert gui.events == [("keyDown", "ctrl"), ("keyDown", "v"), ("keyUp", "v"), ("keyUp", "ctrl")]


def test_a_click_that_fails_releases_its_keys_itself(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)
    gui.fail_on = ("click",)
    m = geometry.Monitor(1, 0, 0, 100, 100, 100, 100)
    with pytest.raises(OSError):
        executor.click(5, 5, m, 1.0, modifiers=["ctrl", "alt"])
    assert gui.events == [("keyDown", "ctrl"), ("keyDown", "alt"), ("keyUp", "alt"), ("keyUp", "ctrl")]
    assert executor.held() == ([], [])


def test_a_held_combo_released_when_its_wait_raises(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)

    def cancelled(seconds):
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError):
        executor.hold_keys("ctrl+shift", 3, wait=cancelled)
    assert gui.events == [("keyDown", "ctrl"), ("keyDown", "shift"), ("keyUp", "shift"), ("keyUp", "ctrl")]
    assert executor.held() == ([], [])


def test_release_held_sends_nothing_when_nothing_is_held(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_pyautogui", gui)
    executor.release_held()
    assert gui.events == []
    executor._held_keys.extend(["ctrl", "shift"])
    executor._held_buttons.append("left")
    gui.fail_on = ("keyUp", "shift")
    executor.release_held()
    assert gui.events == [("keyUp", "ctrl"), ("mouseUp", "left")]
    assert executor.held() == ([], [])


# ===========================================================================
# the bounded reads and the desktop lock
# ===========================================================================

def test_a_read_that_does_not_answer_is_off_for_the_process():
    gate = threading.Event()
    with pytest.raises(guards._Off):
        guards.bounded("slow-thing", lambda: gate.wait(2), timeout=0.05)
    calls = []
    with pytest.raises(guards._Off):
        guards.bounded("slow-thing", lambda: calls.append(1))
    assert calls == []
    gate.set()


def test_a_read_that_raises_is_off():
    with pytest.raises(guards._Off):
        guards.bounded("broken", lambda: 1 / 0)


def test_matches_reads_the_title_and_the_program():
    w = Window(id=1, pid=2, program="notepad.exe", title="Untitled - Notepad")
    assert guards.matches(w, "notepad") and guards.matches(w, "UNTITLED") and not guards.matches(w, "Save")
    assert not guards.matches(w, "  ") and not guards.matches(None, "x")


def test_the_desktop_lock_is_held_across_processes(tmp_path):
    a = environment.DesktopLock(str(tmp_path / "d.lock"))
    b = environment.DesktopLock(str(tmp_path / "d.lock"))
    assert a.acquire(0.5)
    try:
        assert not b.acquire(0.1)   # flock: another open file description of the same file
    finally:
        a.release()
    assert b.acquire(0.5)
    b.release()


def test_desktop_picks_the_systems_module(monkeypatch):
    real = environment.__dict__["desktop"]
    for kind, backend, module in (("windows", "windows", "desktop_windows"), ("darwin", "quartz", "desktop_macos"),
                                  ("linux", "x11", "desktop_x11"), ("linux", "wayland", None),
                                  ("linux", "none", None)):
        monkeypatch.setattr(environment, "os_kind", lambda k=kind: k)
        monkeypatch.setattr(environment, "display_backend", lambda b=backend: b)
        got = fakes_real_desktop(real)
        assert (got.__name__ if got else None) == module


def fakes_real_desktop(_patched):
    import importlib

    import environment as env_mod
    source = importlib.util.find_spec("environment")
    mod = importlib.util.module_from_spec(source)
    source.loader.exec_module(mod)
    mod.os_kind = env_mod.os_kind
    mod.display_backend = env_mod.display_backend
    return mod.desktop()


# ===========================================================================
# Windows, with its libraries faked
# ===========================================================================

class _Fn:
    def __init__(self, impl):
        self.impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.impl(*args)


class _Dll:
    def __init__(self, **fns):
        for name, impl in fns.items():
            setattr(self, name, _Fn(impl))


def _windows(monkeypatch, *, front=0x10, under=0x20, pids=None, levels=None, denied=(), names=None, own=0x2000):
    """Fake user32, kernel32 and advapi32: windows by handle, a pid per window,
    an integrity level per pid, pids whose token or process is refused."""
    pids = pids or {0x10: 100, 0x20: 200}
    levels = levels or {100: 0x2000, 200: 0x2000}
    names = names or {100: r"C:\\Windows\\notepad.exe", 200: r"C:\\Windows\\System32\\Taskmgr.exe"}
    state = {"err": 0, "tokens": {}, "next": 1000}
    me = -1

    def open_process(access, inherit, pid):
        if pid in denied:
            state["err"] = desktop_windows.ERROR_ACCESS_DENIED
            return None
        state["next"] += 1
        state["tokens"][state["next"]] = pid
        return state["next"]

    def open_token(process, access, ref):
        process = getattr(process, "value", process)
        pid = me if process == me else state["tokens"].get(process)
        if pid in denied:
            state["err"] = desktop_windows.ERROR_ACCESS_DENIED
            return 0
        state["next"] += 1
        state["tokens"][state["next"]] = pid
        ref._obj.value = state["next"]
        return 1

    def token_info(token, cls, buf, size, ref):
        token = getattr(token, "value", token)
        if buf is None:
            ref._obj.value = ctypes.sizeof(desktop_windows.TOKEN_MANDATORY_LABEL) + 16
            return 0
        label = desktop_windows.TOKEN_MANDATORY_LABEL.from_buffer(buf)
        label.Label.Sid = 0x7000 + int(token)
        return 1

    def level_of(sid):
        token = sid - 0x7000
        pid = state["tokens"][token]
        return own if pid == me else levels[pid]

    def thread_pid(hwnd, ref):
        ref._obj.value = pids.get(hwnd, 0)
        return 1

    def text(hwnd, buf, n):
        buf.value = {0x10: "notes.txt - Notepad", 0x20: "Task Manager"}.get(hwnd, "")
        return len(buf.value)

    def image_name(process, flags, buf, ref):
        buf.value = names.get(state["tokens"][process], "")
        return 1

    user32 = _Dll(GetForegroundWindow=lambda: front, WindowFromPoint=lambda pt: under,
                  GetAncestor=lambda hwnd, flag: hwnd, GetWindowThreadProcessId=thread_pid, GetWindowTextW=text)
    kernel32 = _Dll(OpenProcess=open_process, CloseHandle=lambda h: 1, GetCurrentProcess=lambda: me,
                    QueryFullProcessImageNameW=image_name)
    advapi32 = _Dll(OpenProcessToken=open_token, GetTokenInformation=token_info,
                    GetSidSubAuthorityCount=lambda sid: ctypes.pointer(ctypes.c_ubyte(1)),
                    GetSidSubAuthority=lambda sid, i: ctypes.pointer(ctypes.c_uint32(level_of(sid))))
    dlls = {"user32": user32, "kernel32": kernel32, "advapi32": advapi32}
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: dlls[name], raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: state["err"], raising=False)
    monkeypatch.setattr(desktop_windows, "_LIBS", None)
    monkeypatch.setattr(desktop_windows, "_OWN_LEVEL", None)
    return dlls


def test_windows_declares_every_function_it_calls(monkeypatch):
    dlls = _windows(monkeypatch)
    desktop_windows.front_window()
    for dll in dlls.values():
        for fn in vars(dll).values():
            assert fn.restype is not None, "a function without a declared result type"
            assert fn.argtypes is not None


def test_windows_reads_the_window_in_front_and_its_program(monkeypatch):
    _windows(monkeypatch)
    w = desktop_windows.front_window()
    assert w == Window(id=0x10, pid=100, program="notepad.exe", title="notes.txt - Notepad")


def test_windows_reads_the_window_under_a_point(monkeypatch):
    _windows(monkeypatch)
    assert desktop_windows.window_at(10, 10).program == "Taskmgr.exe"


def test_windows_a_higher_integrity_program_blocks_input(monkeypatch):
    _windows(monkeypatch, levels={100: 0x2000, 200: 0x3000})
    assert desktop_windows.blocks_input(desktop_windows.window_at(1, 1)) is True
    assert desktop_windows.blocks_input(desktop_windows.front_window()) is False


def test_windows_a_satellite_that_runs_elevated_drives_an_elevated_program(monkeypatch):
    _windows(monkeypatch, levels={100: 0x2000, 200: 0x3000}, own=0x3000)
    assert desktop_windows.blocks_input(desktop_windows.window_at(1, 1)) is False


def test_windows_a_refused_process_counts_as_higher_below_high(monkeypatch):
    _windows(monkeypatch, denied={200})
    w = Window(id=0x20, pid=200, program="", title="")
    assert desktop_windows.blocks_input(w) is True
    _windows(monkeypatch, denied={200}, own=0x3000)
    assert desktop_windows.blocks_input(w) is None


def test_windows_no_window_in_front_is_a_window_of_its_own(monkeypatch):
    _windows(monkeypatch, front=0)
    assert desktop_windows.front_window() == Window(id="none", pid=None, program="", title="")


# ===========================================================================
# macOS, with its libraries faked
# ===========================================================================

def _macos(monkeypatch, *, focused=(42, "Order — Edited"), rows=None, secure=False, holder=None, ax_fails=False):
    ax = types.ModuleType("ApplicationServices")
    ax.kAXFocusedApplicationAttribute, ax.kAXFocusedWindowAttribute, ax.kAXTitleAttribute = "app", "win", "title"
    ax.AXUIElementCreateSystemWide = lambda: "system"

    def copy(element, attr, _none):
        if ax_fails:
            return -25204, None
        return 0, {("system", "app"): "APP", ("APP", "win"): "WIN", ("WIN", "title"): focused[1]}.get((element, attr))
    ax.AXUIElementCopyAttributeValue = copy
    ax.AXUIElementGetPid = lambda app, _none: (0, focused[0])
    quartz = types.ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGWindowListExcludeDesktopElements = 1, 16
    quartz.kCGNullWindowID = 0
    quartz.CGWindowListCopyWindowInfo = lambda opts, wid: rows if rows is not None else [
        {"kCGWindowLayer": 25, "kCGWindowOwnerPID": 1, "kCGWindowNumber": 1, "kCGWindowOwnerName": "Spotlight"},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 42, "kCGWindowNumber": 77, "kCGWindowOwnerName": "TextEdit",
         "kCGWindowName": "Order"},
        {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 50, "kCGWindowNumber": 80, "kCGWindowOwnerName": "Terminal"},
    ]
    quartz.CGSessionCopyCurrentDictionary = lambda: {"kCGSSessionSecureInputPID": holder} if holder else {}
    monkeypatch.setitem(sys.modules, "ApplicationServices", ax)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    class Carbon:
        def __init__(self, path):
            assert path == desktop_macos._CARBON
            self.IsSecureEventInputEnabled = _Fn(lambda: secure)
    monkeypatch.setattr(desktop_macos.ctypes, "CDLL", Carbon)
    monkeypatch.setattr(desktop_macos, "_secure_fn", None)


def test_macos_reads_the_focused_window_with_its_window_number(monkeypatch):
    _macos(monkeypatch)
    w = desktop_macos.front_window()
    assert w == Window(id=(42, 77), pid=42, program="TextEdit", title="Order — Edited")


def test_macos_falls_back_to_the_front_layer_zero_window(monkeypatch):
    _macos(monkeypatch, ax_fails=True)
    w = desktop_macos.front_window()
    assert w.id == (42, 77) and w.program == "TextEdit" and w.title == "Order"


def test_macos_secure_input_and_its_holder(monkeypatch):
    _macos(monkeypatch, secure=True, holder=50)
    assert desktop_macos.secure_input() == (True, 50)
    _macos(monkeypatch, secure=False, holder=50)
    assert desktop_macos.secure_input() == (False, None)
    _macos(monkeypatch, secure=True, holder=None)
    assert desktop_macos.secure_input() == (True, None)


def test_macos_carbon_is_declared_as_a_bool(monkeypatch):
    _macos(monkeypatch, secure=True)
    desktop_macos.secure_input()
    assert desktop_macos._secure_fn.restype is ctypes.c_bool and desktop_macos._secure_fn.argtypes == []


# ===========================================================================
# X11, with python-xlib faked
# ===========================================================================

class _Prop:
    def __init__(self, value):
        self.value = value


class _XWin:
    def __init__(self, wid, parent=None, props=None, name=None, wm_class=None):
        self.id, self.parent, self.props, self.name, self.wm_class = wid, parent, props or {}, name, wm_class

    def query_tree(self):
        return types.SimpleNamespace(parent=self.parent)

    def get_full_property(self, atom, kind):
        v = self.props.get(atom)
        return _Prop(v) if v is not None else None

    def get_wm_name(self):
        return self.name

    def get_wm_class(self):
        return self.wm_class


def _x11(monkeypatch, *, active=None, focus=None, windows=None):
    root = _XWin(1, props={"_NET_ACTIVE_WINDOW": [active] if active else None})
    windows = windows or {}
    d = types.SimpleNamespace(
        screen=lambda: types.SimpleNamespace(root=root),
        intern_atom=lambda name: name,
        create_resource_object=lambda kind, wid: windows[wid],
        get_input_focus=lambda: types.SimpleNamespace(focus=focus),
    )
    monkeypatch.setattr(desktop_x11, "_display", d)
    return root


def test_x11_reads_the_active_window(monkeypatch):
    app = _XWin(0x400001, props={"_NET_WM_NAME": b"Order form", "_NET_WM_PID": [321]}, wm_class=("form", "Form"))
    _x11(monkeypatch, active=0x400001, windows={0x400001: app})
    assert desktop_x11.front_window() == Window(id=0x400001, pid=321, program="Form", title="Order form")


def test_x11_without_a_window_manager_raises_the_focus_to_its_top_level(monkeypatch):
    root = _x11(monkeypatch)
    top = _XWin(0x500001, parent=root, name="tk", wm_class=("tk", "Tk"))
    child = _XWin(0x500009, parent=top)
    monkeypatch.setattr(desktop_x11, "_display", types.SimpleNamespace(
        screen=lambda: types.SimpleNamespace(root=root), intern_atom=lambda n: n,
        get_input_focus=lambda: types.SimpleNamespace(focus=child)))
    w = desktop_x11.front_window()
    assert w.id == 0x500001 and w.program == "Tk" and w.title == "tk"


@pytest.mark.parametrize("focus", [0, 1])
def test_x11_pointer_root_and_none_are_focus_values_of_their_own(monkeypatch, focus):
    _x11(monkeypatch, focus=focus)
    assert desktop_x11.front_window().id == f"focus:{focus}"


def test_a_stuck_read_is_tried_again_once_it_answered():
    gate = threading.Event()
    with pytest.raises(guards._Off):
        guards.bounded("late-thing", lambda: gate.wait(2), timeout=0.05)
    gate.set()
    for _ in range(100):
        if guards._STUCK["late-thing"].is_set():
            break
        threading.Event().wait(0.01)
    assert guards.bounded("late-thing", lambda: "answered") == "answered"


def test_x11_a_window_destroyed_between_the_reads_keeps_its_id(monkeypatch):
    class BadWindow(Exception):
        pass

    class Gone(_XWin):
        def get_full_property(self, atom, kind):
            raise BadWindow
    _x11(monkeypatch, active=0x400002, windows={0x400002: Gone(0x400002)})
    monkeypatch.setattr(desktop_x11, "_x_errors", lambda: BadWindow)
    assert desktop_x11.front_window() == Window(id=0x400002, pid=None, program="", title="")


def test_the_desktop_lock_names_one_desktop_once(monkeypatch, tmp_path):
    real = fakes_real_module("environment")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":0")
    a = real.desktop_lock().path
    monkeypatch.setenv("DISPLAY", ":0.0")
    assert real.desktop_lock().path == a and a.startswith(str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":1")
    assert real.desktop_lock().path != a


def test_a_lock_file_that_cannot_be_opened_is_skipped(tmp_path):
    lock = environment.DesktopLock(str(tmp_path / "missing-folder" / "d.lock"))
    assert lock.acquire(0.1) is True
    lock.release()


def test_a_release_that_fails_stays_recorded(monkeypatch):
    gui = fakes.Gui()
    monkeypatch.setattr(executor, "_gui", lambda: gui)
    monkeypatch.setattr(executor, "_pyautogui", gui)
    gui.fail_on = ("keyUp", "ctrl")
    with pytest.raises(OSError):
        executor.press_keys("ctrl+a")
    assert executor.held() == (["ctrl"], [])
    gui.fail_on = None
    executor.release_held()
    assert executor.held() == ([], []) and gui.events[-1] == ("keyUp", "ctrl")


def fakes_real_module(name):
    import importlib

    spec = importlib.util.find_spec(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
