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
import sys
from pathlib import Path

import pytest

# Make the MCP's own modules importable (geometry/environment/executor/screen/server).
_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import environment
import executor
import geometry
import server
import steps

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
    assert "macOS only" in perms["notes"]


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
# server — the gates, the save folder, the error mapping (the runner's own
# behaviour is test_computer_steps.py)
# ===========================================================================

def test_wayland_refuses_a_call_that_acts_before_anything_runs(monkeypatch):
    # Real gates, forced Wayland: a click is refused with the actionable text.
    monkeypatch.setattr(environment, "display_backend", lambda: "wayland")
    monkeypatch.setattr(environment, "check_session_active", lambda: None)
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "left_click", "coordinate": [1, 1]}]}))
    assert out[0].text.startswith("⚠️") and "Wayland" in out[0].text


def test_no_display_refuses_even_a_screenshot(monkeypatch):
    monkeypatch.setattr(environment, "display_backend", lambda: "none")
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}]}))
    assert "no active graphical display" in out[0].text


def test_alloc_save_path_uses_plain_screenshots_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    abs_path, rel = server._alloc_save_path(1)
    assert rel.startswith("screenshots/screen-1-")
    assert "/.screenshots" not in abs_path and not rel.startswith(".screenshots")
    assert (tmp_path / "screenshots").is_dir()  # folder created
    assert abs_path.endswith(".png")
    _abs, zoom_rel = server._alloc_save_path(2, "zoom")
    assert zoom_rel.startswith("screenshots/zoom-2-")


def test_alloc_save_path_none_without_workspace(monkeypatch):
    monkeypatch.delenv("OTO_WORKSPACE_DIR", raising=False)
    assert server._alloc_save_path(1) == (None, None)


def test_call_tool_maps_device_unavailable(monkeypatch):
    def boom(*a, **k):
        raise environment.DeviceUnavailable("no display here")
    monkeypatch.setattr(steps, "run", boom)
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}]}))
    assert out[0].type == "text" and "no display here" in out[0].text


def test_call_tool_maps_valueerror(monkeypatch):
    monkeypatch.setattr(steps, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad arg")))
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}]}))
    assert "bad arg" in out[0].text


def test_an_unexpected_error_names_only_its_type(monkeypatch, caplog):
    monkeypatch.setattr(steps, "run", lambda *a, **k: (_ for _ in ()).throw(KeyError("typed secret")))
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}]}))
    assert out[0].text == "⚠️ Unexpected error performing the action: KeyError."
    assert all("typed secret" not in r.getMessage() for r in caplog.records)


def test_a_bad_display_is_refused(monkeypatch):
    asyncio.run(server.list_tools())
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}], "display": "two"}))
    assert out.isError is True and "display" in out.content[0].text
    server.TOOL_ARGS._schemas = {}  # unlisted again: the runner's own refusal
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "screenshot"}], "display": "two"}))
    assert out[0].text == "⚠️ 'display' must be an integer monitor index (1 = primary)."


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
    assert computer.inputSchema["required"] == ["steps"]
    item = computer.inputSchema["properties"]["steps"]["items"]
    assert set(item["properties"]["action"]["enum"]) == set(server._ACTIONS)
    assert set(item["properties"]) == {
        "action", "coordinate", "start_coordinate", "text", "scroll_direction", "scroll_amount", "duration",
        "region", "save", "window",
    }


def test_the_docstring_says_a_granted_computer_shows_no_card():
    assert "every `mcp__computer__*`" not in server.__doc__
    assert "no approval card in every mode" in server.__doc__


# ===========================================================================
# macOS grants: refuse what macOS would silently drop, and say what to turn on
# ===========================================================================

@pytest.fixture
def mac(monkeypatch):
    """A Mac whose grants the test sets; records each read's prompt flag."""
    state = {"accessibility": True, "screen_recording": True, "prompts": []}

    def perms(prompt=False):
        state["prompts"].append(prompt)
        return {"accessibility": state["accessibility"], "screen_recording": state["screen_recording"], "notes": ""}

    monkeypatch.setattr(environment, "os_kind", lambda: "darwin")
    monkeypatch.setattr(environment, "check_macos_permissions", perms)
    monkeypatch.setattr(environment, "_asked", False)
    monkeypatch.setattr(environment, "_macos_major", lambda: 27)
    monkeypatch.setattr(environment, "responsible_app",
                        lambda: ("OtoDock Satellite", "/Users/p/.oto-dock/OtoDock Satellite.app"))
    return state


def test_the_first_action_asks_macos_once(mac):
    environment.check_macos_grants(input_needed=True)
    environment.check_macos_grants(input_needed=True)
    assert mac["prompts"] == [True, False]


def test_input_is_refused_without_accessibility_and_capture_still_runs(mac):
    mac["accessibility"] = False
    environment.check_macos_grants(input_needed=False)
    with pytest.raises(environment.DeviceUnavailable) as e:
        environment.check_macos_grants(input_needed=True)
    assert str(e.value) == (
        "macOS does not let OtoDock Satellite control the mouse and keyboard yet. Turn on OtoDock "
        "Satellite in System Settings, Privacy & Security, under Device Control and Data Access. "
        "Then start a new chat on this machine.")


def test_a_capture_is_refused_without_screen_recording(mac):
    mac["screen_recording"] = False
    mac["accessibility"] = False
    with pytest.raises(environment.DeviceUnavailable) as e:
        environment.check_macos_grants(input_needed=True)
    assert "see the screen or control the mouse and keyboard" in str(e.value)
    assert "under Screen & System Audio Recording and under Device Control and Data Access" in str(e.value)


def test_an_unreadable_grant_never_refuses(mac):
    mac["accessibility"] = mac["screen_recording"] = None
    environment.check_macos_grants(input_needed=True)


def test_older_macos_names_its_own_lists_and_another_app(monkeypatch, mac):
    monkeypatch.setattr(environment, "_macos_major", lambda: 14)
    monkeypatch.setattr(environment, "responsible_app",
                        lambda: ("Python", "/opt/homebrew/Cellar/python@3.13/3.13.18/Python.app"))
    msg = environment.grant_message(["screen", "accessibility"])
    assert "under Screen Recording and under Accessibility" in msg
    assert "Turn on Python (/opt/homebrew/Cellar/python@3.13/3.13.18/Python.app)" in msg
    assert ";" not in msg and "—" not in msg


@pytest.mark.parametrize("app", [("sshd-session", "/usr/libexec/sshd-session"), None])
def test_a_bare_program_is_never_named_as_the_one_to_turn_on(monkeypatch, mac, app):
    # Over SSH macOS credits sshd: its switch would open the screen to every
    # SSH sign-in. The person is pointed at the satellite's app instead.
    monkeypatch.setattr(environment, "responsible_app", lambda: app)
    msg = environment.grant_message(["accessibility"])
    assert "sshd" not in msg
    assert "Turn on the satellite's app in System Settings, Privacy & Security, under Device Control" in msg
    assert ("Start the satellite from its login item" in msg) is (app is not None)


def test_a_click_macos_would_drop_is_never_sent(monkeypatch, mac):
    mac["accessibility"] = False
    for name in ("check_capture_supported", "check_input_supported", "check_session_active"):
        monkeypatch.setattr(environment, name, lambda: None)
    sent = []
    monkeypatch.setattr(executor, "_gui", lambda: sent.append("gui") or None)
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "left_click", "coordinate": [1, 1]}]}))
    assert "control the mouse and keyboard" in out[0].text
    assert sent == []


def test_permissions_off_a_mac_say_so_plainly(monkeypatch):
    monkeypatch.setattr(environment, "os_kind", lambda: "linux")
    assert environment.check_macos_permissions()["notes"] == "Permission checks are macOS only. This is not macOS."
    assert environment.responsible_app() is None
