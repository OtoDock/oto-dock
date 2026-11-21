"""Fixtures for the computer tool's tests. No test reaches a real display: the
display variables are cleared, and the input library, the screen and the
system's window reads fail the test unless it hands in its own stand-ins."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).resolve().parent.parent
for p in (str(_MCP_DIR), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import fakes

import environment
import executor
import guards
import screen
import steps


def _refuse(what):
    def refuse(*_a, **_k):
        raise AssertionError(f"a test reached the real {what}")
    return refuse


@pytest.fixture(autouse=True)
def _no_real_display(monkeypatch, tmp_path):
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(executor, "_pyautogui", None)
    monkeypatch.setattr(executor, "_gui", _refuse("pyautogui"))
    monkeypatch.setattr(screen, "list_monitors", _refuse("monitor list"))
    monkeypatch.setattr(screen, "open_grabber", _refuse("screen grabber"))
    monkeypatch.setattr(environment, "desktop", lambda: None)
    monkeypatch.setattr(environment, "desktop_lock",
                        lambda: environment.DesktopLock(str(tmp_path / "desktop.lock")))
    steps.forget()
    guards._STUCK.clear()
    executor._held_keys.clear()
    executor._held_buttons.clear()
    yield
    steps.forget()
    executor._held_keys.clear()
    executor._held_buttons.clear()


@pytest.fixture
def open_gates(monkeypatch):
    """Every device check passes, on a Linux desktop."""
    for name in ("check_capture_supported", "check_input_supported", "check_session_active"):
        monkeypatch.setattr(environment, name, lambda: None)
    monkeypatch.setattr(environment, "check_macos_grants", lambda input_needed: None)
    monkeypatch.setattr(environment, "os_kind", lambda: "linux")
    monkeypatch.setattr(executor, "os_kind", lambda: "linux")
    monkeypatch.setattr(screen, "os_kind", lambda: "linux")


class Rig:
    """A desktop the runner drives: one monitor, scripted frames, a recording
    input library, the window reads, a fake clock."""

    def __init__(self, monkeypatch, *, size=(800, 600), monitors=None) -> None:
        self.clock = fakes.Clock()
        self.gui = fakes.Gui(self.clock)
        self.desk = fakes.Desk()
        w, h = size
        self.monitor = monitors[0] if monitors else environment_monitor(w, h)
        self.monitors = monitors or [self.monitor]
        self.base_img = fakes.desk_image(w, h)
        self.screen_img = self.base_img
        self.grabber = fakes.Grabber(source=lambda mon: fakes.frame_of(self.screen_img, mon))
        self.layout_reads = 0
        self.saved: list[str] = []
        monkeypatch.setattr(executor, "_gui", lambda: self.gui)
        monkeypatch.setattr(executor, "_pyautogui", self.gui)

        def layout():
            self.layout_reads += 1
            return self.monitors
        monkeypatch.setattr(screen, "list_monitors", layout)
        monkeypatch.setattr(screen, "open_grabber", lambda: self.grabber)
        monkeypatch.setattr(environment, "desktop", lambda: self.desk)
        self.settle_s = 0.7

    def hooks(self, server_module):
        return steps.Hooks(dispatch=server_module._do_step, coord_mode="rescale", settle_s=self.settle_s,
                           alloc_save_path=server_module._alloc_save_path, clock=self.clock.clock,
                           sleep=self.clock.sleep, wait=self.clock.wait)


def environment_monitor(w, h):
    import geometry

    return geometry.Monitor(1, 0, 0, w, h, w, h, True)


@pytest.fixture
def rig(monkeypatch, open_gates):
    return Rig(monkeypatch)
