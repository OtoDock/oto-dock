"""Unit tests for the computer-control MCP.

Covers the pure, display-free surface — the coordinate math (the load-bearing
correctness piece, incl. negative multi-monitor origins + HiDPI), the key-name
table, the environment/edge-case gates, and the server's action dispatch (with
``screen`` / ``executor`` mocked so no real display, mss, or pyautogui is
needed). Run with the proxy venv (it has ``mcp`` + ``pytest`` + ``PIL``):

    cd proxy && PYTHONPATH=../mcps/custom/computer-mcp venv/bin/python -m pytest \
        ../mcps/custom/computer-mcp/tests/test_computer_mcp.py -q
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Make the MCP's own modules importable (geometry/environment/executor/screen/server).
_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import environment  # noqa: E402
import executor  # noqa: E402
import geometry  # noqa: E402
import screen  # noqa: E402
import server  # noqa: E402


# ===========================================================================
# geometry — scaling
# ===========================================================================

def test_compute_scale_rescale_downscales_4k():
    s = geometry.compute_scale(3840, 2160, "rescale")
    # area-capped: sqrt(1.15M / (3840*2160)) ~= 0.372
    assert 0.36 < s < 0.38
    assert max(round(3840 * s), round(2160 * s)) <= 1568 + 1


def test_compute_scale_highres_uses_2576_edge():
    s = geometry.compute_scale(3840, 2160, "highres")
    assert s == pytest.approx(2576 / 3840, rel=1e-6)


def test_compute_scale_small_screen_is_unity():
    assert geometry.compute_scale(1280, 800, "rescale") == 1.0
    assert geometry.compute_scale(1280, 800, "highres") == 1.0


def test_compute_scale_zero_dims_safe():
    assert geometry.compute_scale(0, 0, "rescale") == 1.0


def test_normalize_coord_mode_aliases():
    assert geometry.normalize_coord_mode("highres") == geometry.COORD_MODE_HIGHRES
    assert geometry.normalize_coord_mode("opus") == geometry.COORD_MODE_HIGHRES
    assert geometry.normalize_coord_mode("1to1") == geometry.COORD_MODE_HIGHRES
    assert geometry.normalize_coord_mode("rescale") == geometry.COORD_MODE_RESCALE
    assert geometry.normalize_coord_mode("garbage") == geometry.COORD_MODE_RESCALE
    assert geometry.normalize_coord_mode(None) == geometry.COORD_MODE_RESCALE


def test_scaled_size_floors_at_one():
    assert geometry.scaled_size(10, 10, 0.0001) == (1, 1)


# ===========================================================================
# geometry — per-monitor DPR + coordinate conversion
# ===========================================================================

def test_dpr_x11_is_unity():
    m = geometry.Monitor(1, 0, 0, 1920, 1080, 1920, 1080)
    assert m.dpr_x == 1.0 and m.dpr_y == 1.0


def test_dpr_retina_is_two():
    m = geometry.Monitor(1, 0, 0, 1440, 900, 2880, 1800)
    assert m.dpr_x == 2.0 and m.dpr_y == 2.0


def test_negative_origin_monitor_offset():
    """A monitor to the LEFT of primary has left = -1920; a model click must
    land in that monitor's negative coordinate range — the easy-to-miss bug."""
    m = geometry.Monitor(2, -1920, 0, 1920, 1080, 1920, 1080)  # x11, dpr 1, no downscale
    scale = geometry.compute_scale(1920, 1080, "highres")  # 1.0
    gx, gy = geometry.model_to_input_coords(100, 50, monitor=m, scale=scale)
    assert (gx, gy) == (-1820.0, 50.0)


def test_negative_origin_top_offset():
    m = geometry.Monitor(2, 0, -1080, 1920, 1080, 1920, 1080)
    gx, gy = geometry.model_to_input_coords(0, 0, monitor=m, scale=1.0)
    assert (gx, gy) == (0.0, -1080.0)


def test_retina_bottom_right_maps_to_logical_extent():
    """On a 2880x1800 Retina capture downscaled to 2576 long-edge, the model's
    bottom-right click recovers the logical (point) extent 1440x900."""
    m = geometry.Monitor(1, 0, 0, 1440, 900, 2880, 1800)
    scale = geometry.compute_scale(2880, 1800, "highres")  # 2576/2880
    mx, my = 2880 * scale, 1800 * scale
    gx, gy = geometry.model_to_input_coords(mx, my, monitor=m, scale=scale)
    assert gx == pytest.approx(1440.0, abs=0.5)
    assert gy == pytest.approx(900.0, abs=0.5)


def test_model_input_round_trip():
    m = geometry.Monitor(2, -1920, -200, 1920, 1080, 3840, 2160)  # negative origin + 2x dpr
    scale = geometry.compute_scale(3840, 2160, "rescale")
    for (mx, my) in [(0, 0), (10, 10), (500, 300), (1000, 600)]:
        gx, gy = geometry.model_to_input_coords(mx, my, monitor=m, scale=scale)
        bx, by = geometry.input_to_model_coords(gx, gy, monitor=m, scale=scale)
        assert bx == pytest.approx(mx, abs=0.01)
        assert by == pytest.approx(my, abs=0.01)


def test_clamp_to_monitor_inclusive_and_negative():
    m = geometry.Monitor(2, -1920, 0, 1920, 1080, 1920, 1080)
    assert geometry.clamp_to_monitor(-5000, 99999, m) == (-1920, 1079)
    assert geometry.clamp_to_monitor(-1000, 500, m) == (-1000, 500)  # already inside
    assert geometry.clamp_to_monitor(0, 0, m) == (-1, 0)  # x just past right edge clamps to last px


# ===========================================================================
# executor — xdotool key-name parsing/mapping (pure; no pyautogui)
# ===========================================================================

def test_parse_key_combo_basic():
    assert executor.parse_key_combo("ctrl+s") == ["ctrl", "s"]
    assert executor.parse_key_combo("ctrl+shift+t") == ["ctrl", "shift", "t"]


def test_parse_key_combo_named_keys():
    assert executor.parse_key_combo("Return") == ["enter"]
    assert executor.parse_key_combo("Page_Down") == ["pagedown"]
    assert executor.parse_key_combo("Escape") == ["esc"]
    assert executor.parse_key_combo("alt+Tab") == ["alt", "tab"]
    assert executor.parse_key_combo("Control_L") == ["ctrl"]
    assert executor.parse_key_combo("F5") == ["f5"]


def test_parse_key_combo_plus_key():
    assert executor.parse_key_combo("+") == ["+"]


def test_parse_key_combo_empty_raises():
    with pytest.raises(ValueError):
        executor.parse_key_combo("")
    with pytest.raises(ValueError):
        executor.parse_key_combo("   ")


def test_super_key_is_os_specific(monkeypatch):
    monkeypatch.setattr(executor, "os_kind", lambda: "darwin")
    assert executor._map_key("super") == "command"
    monkeypatch.setattr(executor, "os_kind", lambda: "linux")
    assert executor._map_key("super") == "winleft"


def test_key_map_has_function_keys():
    for i in range(1, 25):
        assert executor.KEY_MAP[f"f{i}"] == f"f{i}"


# ===========================================================================
# environment — backend + gates
# ===========================================================================

def test_os_kind(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "linux")
    assert environment.os_kind() == "linux"
    monkeypatch.setattr(environment.sys, "platform", "darwin")
    assert environment.os_kind() == "darwin"
    monkeypatch.setattr(environment.sys, "platform", "win32")
    assert environment.os_kind() == "windows"


def test_display_backend_linux(monkeypatch):
    monkeypatch.setattr(environment, "os_kind", lambda: "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert environment.display_backend() == "wayland"
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert environment.display_backend() == "x11"
    monkeypatch.delenv("DISPLAY", raising=False)
    assert environment.display_backend() == "none"


def test_display_backend_mac_win(monkeypatch):
    monkeypatch.setattr(environment, "os_kind", lambda: "darwin")
    assert environment.display_backend() == "quartz"
    monkeypatch.setattr(environment, "os_kind", lambda: "windows")
    assert environment.display_backend() == "windows"


def test_check_input_supported_refuses_wayland(monkeypatch):
    monkeypatch.setattr(environment, "display_backend", lambda: "wayland")
    with pytest.raises(environment.DeviceUnavailable) as ei:
        environment.check_input_supported()
    assert "wayland" in str(ei.value).lower() or "x11" in str(ei.value).lower()


def test_check_input_supported_refuses_none(monkeypatch):
    monkeypatch.setattr(environment, "display_backend", lambda: "none")
    with pytest.raises(environment.DeviceUnavailable):
        environment.check_input_supported()


def test_check_input_supported_allows_x11(monkeypatch):
    monkeypatch.setattr(environment, "display_backend", lambda: "x11")
    environment.check_input_supported()  # no raise


def test_check_capture_allows_wayland_but_not_none(monkeypatch):
    # Capture is attempted under Wayland (XWayland may work); only hard no-display refuses.
    monkeypatch.setattr(environment, "display_backend", lambda: "wayland")
    environment.check_capture_supported()  # no raise
    monkeypatch.setattr(environment, "display_backend", lambda: "none")
    with pytest.raises(environment.DeviceUnavailable):
        environment.check_capture_supported()


def test_check_session_active_blocks_only_on_definite_false(monkeypatch):
    monkeypatch.setattr(environment, "session_active_unlocked", lambda: False)
    with pytest.raises(environment.DeviceUnavailable):
        environment.check_session_active()
    monkeypatch.setattr(environment, "session_active_unlocked", lambda: None)
    environment.check_session_active()  # no raise when unknown
    monkeypatch.setattr(environment, "session_active_unlocked", lambda: True)
    environment.check_session_active()  # no raise


def test_macos_permissions_non_mac(monkeypatch):
    monkeypatch.setattr(environment, "os_kind", lambda: "linux")
    perms = environment.check_macos_permissions()
    assert perms["accessibility"] is None and perms["screen_recording"] is None
    assert "macOS-only" in perms["notes"]


def test_session_active_unknown_on_linux(monkeypatch):
    monkeypatch.setattr(environment, "os_kind", lambda: "linux")
    assert environment.session_active_unlocked() is None


# ===========================================================================
# manifest — device-local fields are wired correctly
# ===========================================================================

def test_manifest_device_fields():
    data = json.loads((_MCP_DIR / "manifest.json").read_text())
    assert data["name"] == "computer-control"
    assert data["server_name"] == "computer"  # tools are mcp__computer__*
    assert data["placement"] == "satellite_only"
    assert data["requires_display"] is True
    assert data["device_capability"] == "computer"
    assert "phone" not in data["exclude_from"]  # allowed on calls; gated by device placement + grants
    # auto: visible in every agent's MCP tab (manager opts in/out per agent); the
    # real gates are placement (satellite-only) + the owner's device_grants
    # consent, not an admin instance-authorization.
    assert data["assignment_mode"] == "auto"
    assert data["server"]["command"] == "venv/bin/python"
    assert data["server"]["args"] == ["server.py"]


# ===========================================================================
# server — action dispatch (screen/executor/gates mocked, no display)
# ===========================================================================

class _FakeCapture:
    def __init__(self, saved_path=None):
        self.png_b64 = "QUJD"  # "ABC"
        self.model_width = 1280
        self.model_height = 720
        self.scale = 0.5
        self.monitor = geometry.Monitor(1, 0, 0, 2560, 1440, 2560, 1440)
        self.monitor_count = 1
        self.saved_path = saved_path


@pytest.fixture
def open_gates(monkeypatch):
    """Make all environment gates pass so dispatch reaches the action."""
    monkeypatch.setattr(server, "check_capture_supported", lambda: None)
    monkeypatch.setattr(server, "check_input_supported", lambda: None)
    monkeypatch.setattr(server, "check_session_active", lambda: None)
    monkeypatch.setattr(server, "os_kind", lambda: "linux")  # no Windows elevation probe


def test_screenshot_action_returns_image_and_text(monkeypatch, open_gates):
    monkeypatch.setattr(screen, "capture", lambda d, m, save_path=None: _FakeCapture())
    blocks = server._run_action({"action": "screenshot"})
    assert blocks[0].type == "image" and blocks[0].mimeType == "image/png"
    assert blocks[1].type == "text" and "1280x720" in blocks[1].text


def test_unknown_action_raises_valueerror(open_gates):
    with pytest.raises(ValueError):
        server._run_action({"action": "frobnicate"})


def test_key_without_text_raises(open_gates):
    with pytest.raises(ValueError):
        server._run_action({"action": "key"})


def test_left_click_converts_and_calls_executor(monkeypatch, open_gates):
    calls = {}
    mon = geometry.Monitor(1, 0, 0, 2560, 1440, 2560, 1440)
    monkeypatch.setattr(screen, "resolve_target", lambda d, m: (mon, 0.5))
    monkeypatch.setattr(screen, "capture", lambda d, m, save_path=None: _FakeCapture())

    def fake_click(mx, my, monitor, scale, **kw):
        calls["args"] = (mx, my, scale, kw)
        return 0, 0
    monkeypatch.setattr(executor, "click", fake_click)

    server._run_action({"action": "left_click", "coordinate": [100, 200]})
    assert calls["args"][0] == 100.0 and calls["args"][1] == 200.0
    assert calls["args"][3]["button"] == "left" and calls["args"][3]["clicks"] == 1


def test_double_click_uses_two_clicks(monkeypatch, open_gates):
    calls = {}
    monkeypatch.setattr(screen, "resolve_target", lambda d, m: (geometry.Monitor(1, 0, 0, 100, 100, 100, 100), 1.0))
    monkeypatch.setattr(screen, "capture", lambda d, m, save_path=None: _FakeCapture())
    monkeypatch.setattr(executor, "click", lambda *a, **k: calls.update(k) or (0, 0))
    server._run_action({"action": "double_click", "coordinate": [5, 5]})
    assert calls["clicks"] == 2 and calls["button"] == "left"


def test_click_with_modifier_text(monkeypatch, open_gates):
    calls = {}
    monkeypatch.setattr(screen, "resolve_target", lambda d, m: (geometry.Monitor(1, 0, 0, 100, 100, 100, 100), 1.0))
    monkeypatch.setattr(screen, "capture", lambda d, m, save_path=None: _FakeCapture())
    monkeypatch.setattr(executor, "click", lambda *a, **k: calls.update(k) or (0, 0))
    server._run_action({"action": "left_click", "coordinate": [5, 5], "text": "ctrl"})
    assert calls["modifiers"] == ["ctrl"]


def test_type_action(monkeypatch, open_gates):
    seen = {}
    monkeypatch.setattr(executor, "type_text", lambda t: seen.setdefault("t", t) or "typed 5 characters")
    monkeypatch.setattr(screen, "capture", lambda d, m, save_path=None: _FakeCapture())
    server._run_action({"action": "type", "text": "hello"})
    assert seen["t"] == "hello"


def test_wayland_input_refusal_bubbles(monkeypatch):
    # Real gate, forced Wayland: an input action must raise DeviceUnavailable.
    monkeypatch.setattr(environment, "display_backend", lambda: "wayland")
    monkeypatch.setattr(server, "check_session_active", lambda: None)
    with pytest.raises(environment.DeviceUnavailable):
        server._run_action({"action": "left_click", "coordinate": [1, 1]})


# ===========================================================================
# server — deliberate save goes to a NORMAL folder, never .screenshots
# ===========================================================================

def test_alloc_save_path_uses_plain_screenshots_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    abs_path, rel = server._alloc_save_path(1)
    assert rel.startswith("screenshots/")
    assert "/.screenshots" not in abs_path and not rel.startswith(".screenshots")
    assert (tmp_path / "screenshots").is_dir()  # folder created
    assert abs_path.endswith(".png")


def test_alloc_save_path_none_without_workspace(monkeypatch):
    monkeypatch.delenv("OTO_WORKSPACE_DIR", raising=False)
    assert server._alloc_save_path(1) == (None, None)


def test_screenshot_save_passes_path_to_capture(monkeypatch, open_gates, tmp_path):
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    seen = {}

    def fake_capture(d, m, save_path=None):
        seen["save_path"] = save_path
        return _FakeCapture(saved_path=save_path)
    monkeypatch.setattr(screen, "capture", fake_capture)

    blocks = server._run_action({"action": "screenshot", "save": True})
    assert seen["save_path"] is not None
    assert os.sep + "screenshots" + os.sep in seen["save_path"]
    assert ".screenshots" not in seen["save_path"]
    assert "screenshots/" in blocks[1].text  # path surfaced to the user


def test_reactive_frame_is_not_saved(monkeypatch, open_gates, tmp_path):
    """A result-screenshot after a click must NOT save (save flag only on the
    explicit screenshot action)."""
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    seen = {}
    monkeypatch.setattr(screen, "resolve_target", lambda d, m: (geometry.Monitor(1, 0, 0, 100, 100, 100, 100), 1.0))
    monkeypatch.setattr(executor, "click", lambda *a, **k: (0, 0))

    def fake_capture(d, m, save_path=None):
        seen["save_path"] = save_path
        return _FakeCapture()
    monkeypatch.setattr(screen, "capture", fake_capture)
    server._run_action({"action": "left_click", "coordinate": [1, 1]})
    assert seen["save_path"] is None


# ===========================================================================
# server — call_tool error mapping + check_permissions (async)
# ===========================================================================

def test_call_tool_maps_device_unavailable(monkeypatch):
    def boom(args):
        raise environment.DeviceUnavailable("no display here")
    monkeypatch.setattr(server, "_run_action", boom)
    out = asyncio.run(server.call_tool("computer", {"action": "screenshot"}))
    assert out[0].type == "text" and "no display here" in out[0].text


def test_call_tool_maps_valueerror(monkeypatch):
    monkeypatch.setattr(server, "_run_action", lambda a: (_ for _ in ()).throw(ValueError("bad arg")))
    out = asyncio.run(server.call_tool("computer", {"action": "x"}))
    assert "bad arg" in out[0].text


def test_call_tool_unknown_tool():
    out = asyncio.run(server.call_tool("nope", {}))
    assert "Unknown tool" in out[0].text


def test_check_permissions_reports_backend(monkeypatch):
    monkeypatch.setattr(server, "display_backend", lambda: "wayland")
    monkeypatch.setattr(server, "os_kind", lambda: "linux")
    out = asyncio.run(server.call_tool("check_permissions", {}))
    assert out[0].type == "text"
    assert "wayland" in out[0].text.lower()


def test_list_tools_shape():
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"computer", "check_permissions"}
    computer = next(t for t in tools if t.name == "computer")
    assert "action" in computer.inputSchema["required"]
    assert set(computer.inputSchema["properties"]["action"]["enum"]) == server._ALL_ACTIONS
