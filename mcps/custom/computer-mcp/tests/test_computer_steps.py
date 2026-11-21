"""The computer tool's one runner: a call is a list of steps, checked before
anything runs, run in order with the checks before each step that acts, and
reported in one picture and one line per step. Every stop is proved to send
no input event after it, and typed text never reaches a line or a log."""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import fakes
import pytest

import environment
import executor
import geometry
import server
import steps
from environment import Window

FORM = Window(id=1, pid=10, program="form.exe", title="Order form")


def call(rig, step_list, *, display=None, think=3.0, **extra):
    """One call through the server, as an engine sends it, ``think`` seconds
    after the previous answer (the model's look; a call of the same turn
    comes at once)."""
    rig.clock.now += think
    args = {"steps": step_list, **extra}
    if display is not None:
        args["display"] = display
    server._hooks = lambda: rig.hooks(server)
    return asyncio.run(server.call_tool("computer", args))


def text(blocks) -> str:
    return blocks[-1].text


def images(blocks) -> list:
    return [b for b in blocks if b.type == "image"]


def look(rig):
    """The model takes a screenshot: the tool now holds its last picture."""
    out = call(rig, [{"action": "screenshot"}])
    assert "1 screenshot: picture 1" in text(out)
    rig.gui.events.clear()
    return out


@pytest.fixture(autouse=True)
def _hooks_back(monkeypatch):
    monkeypatch.setattr(server, "_hooks", server._hooks)


# ===========================================================================
# the schema and the door
# ===========================================================================

def test_the_schema_lists_only_steps_and_display():
    tools = asyncio.run(server.list_tools())
    computer = next(t for t in tools if t.name == "computer")
    schema = computer.inputSchema
    assert set(schema["properties"]) == {"steps", "display"}
    assert schema["required"] == ["steps"]
    assert schema["properties"]["steps"]["maxItems"] == 12
    item = schema["properties"]["steps"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["properties"]["action"]["enum"]) == set(server._ACTIONS)
    assert "left_mouse_down" not in server._ACTIONS and "left_mouse_up" not in server._ACTIONS
    assert "anyOf" not in schema and "oneOf" not in schema
    assert "steps" in computer.description and "also for one action" in computer.description


STEPS_ONLY = "Nothing ran. This tool takes `steps`, a list of 1 to 12 actions. Send this action as one step."


@pytest.mark.parametrize("args", [
    {"action": "left_click", "coordinate": [300, 200]},
    {"action": "screenshot", "display": 1},
    {"action": "type", "text": "Maria", "steps": [{"action": "type", "text": "Maria"}]},
])
def test_a_top_level_action_runs_nothing_and_asks_for_steps(rig, args):
    look(rig)
    grabs = rig.grabber.grabs
    server._hooks = lambda: rig.hooks(server)
    rig.clock.now += 3
    out = asyncio.run(server.call_tool("computer", args))
    assert [b.type for b in out] == ["text"]
    assert out[0].text == f"⚠️ {STEPS_ONLY}"
    assert rig.gui.events == []
    assert rig.grabber.grabs == grabs


def test_the_action_sent_again_as_one_step_runs_at_once(rig):
    look(rig)
    server._hooks = lambda: rig.hooks(server)
    rig.clock.now += 3
    asyncio.run(server.call_tool("computer", {"action": "left_click", "coordinate": [300, 200]}))
    out = call(rig, [{"action": "left_click", "coordinate": [300, 200]}], think=0.2)
    assert text(out).startswith("Ran 1 of 1 step.")
    assert any(e[0] == "click" for e in rig.gui.events)


@pytest.mark.parametrize("args, needle", [
    ({"steps": "click"}, "'steps' must be a list of steps."),
    ({"steps": []}, "1 to 12 steps (this call has 0)"),
    ({"steps": [{"action": "wait"}] * 13}, "1 to 12 steps (this call has 13)"),
    ({"steps": [{"action": "wait"}, "type"]}, "Step 2 must be an object"),
])
def test_the_door_refuses_a_malformed_list_with_counts_only(rig, args, needle):
    server._hooks = lambda: rig.hooks(server)
    out = asyncio.run(server.call_tool("computer", args))
    assert needle in out[0].text
    assert rig.gui.events == []


def test_thirteen_steps_holding_a_secret_never_echo_it(rig):
    secret = "S3cret-pass!"
    many = [{"action": "left_click", "coordinate": [10, 10]}, {"action": "type", "text": secret}] * 7
    server._hooks = lambda: rig.hooks(server)
    out = asyncio.run(server.call_tool("computer", {"steps": many}))
    assert all(secret not in getattr(b, "text", "") for b in out)
    assert "this call has 14" in out[0].text


def test_a_field_on_the_wrong_level_is_named_not_echoed(rig):
    server._hooks = lambda: rig.hooks(server)
    out = asyncio.run(server.call_tool("computer", {"steps": [
        {"action": "type", "text": "hunter2", "display": 2}]}))
    assert out.isError is True
    assert "'display' was unexpected" in out.content[0].text
    assert "hunter2" not in out.content[0].text


def test_a_call_without_steps_asks_for_them(rig):
    server._hooks = lambda: rig.hooks(server)
    out = asyncio.run(server.call_tool("computer", {}))
    assert "'steps' is a required property" in out.content[0].text


# ===========================================================================
# the plan check: refused before anything runs
# ===========================================================================

@pytest.mark.parametrize("plan, needle", [
    ([{"action": "key", "text": "Return"}, {"action": "type", "text": "x"}],
     "Step 1 presses Enter, so it must be the last step that acts."),
    ([{"action": "key", "text": "ctrl+KP_Enter"}, {"action": "key", "text": "Tab"}], "Step 1 presses Enter"),
    ([{"action": "hold_key", "text": "Return", "duration": 1}, {"action": "type", "text": "x"}],
     "Step 1 presses Enter"),
    ([{"action": "left_click", "text": "Return"}, {"action": "type", "text": "x"}], "Step 1 presses Enter"),
    ([{"action": "zoom", "region": [float("nan"), 0, 100, 100]}], "'region' must hold finite numbers"),
    ([{"action": "left_click", "coordinate": [float("inf"), 5]}], "'coordinate' must hold finite numbers"),
    ([{"action": "zoom", "region": [0, 0, 50, 50]}, {"action": "key", "text": "Tab"}],
     "Step 2: put screenshot, zoom and cursor_position after the other steps"),
    ([{"action": "wait", "duration": 6}], "Step 1: 'duration' can be 0 to 5 seconds"),
    ([{"action": "hold_key", "text": "shift", "duration": 30}], "'duration' can be 0 to 5 seconds"),
    ([{"action": "scroll", "scroll_amount": 500}], "'scroll_amount' can be 1 to 50"),
    ([{"action": "type", "text": "a" * 201}], "at most 200 characters here (this one has 201)"),
    ([{"action": "type", "text": "α" * 2001}], "at most 2000 characters here (this one has 2001)"),
    ([{"action": "zoom", "region": [0, 0, 4, 50]}], "at least 8 x 8 pixels"),
    ([{"action": "zoom", "region": [0, 0, 900, 50]}], "must lie inside the screenshot (800x600)"),
    ([{"action": "zoom"}], "a zoom needs 'region'"),
    ([{"action": "zoom", "region": [0, 0, 50, 50]}] * 4, "at most 3 zoom steps"),
    ([{"action": "screenshot"}, {"action": "screenshot"}], "one screenshot step"),
    ([{"action": "left_click", "coordinate": [1, 1], "save": True}], "only a screenshot or a zoom takes 'save'"),
    ([{"action": "screenshot", "window": "x"}], "only a step that acts takes 'window'"),
    ([{"action": "type", "text": "x", "region": [0, 0, 9, 9]}], "only a zoom takes 'region'"),
    ([{"action": "mouse_move"}], "'mouse_move' needs 'coordinate'"),
    ([{"action": "key"}], "'key' needs 'text'"),
    ([{"action": "left_click", "coordinate": [800, 10]}], "(800, 10) lies outside your last screenshot (800x600)"),
])
def test_the_plan_is_refused_before_anything_runs(rig, plan, needle):
    look(rig)
    grabs = rig.grabber.grabs
    out = call(rig, plan)
    assert out[0].text.startswith("⚠️")
    assert needle in out[0].text, out[0].text
    assert rig.gui.events == []
    assert rig.grabber.grabs == grabs


def test_enter_may_close_the_list_with_a_pause_and_reads_after_it(rig):
    look(rig)
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "key", "text": "Return"},
                     {"action": "wait", "duration": 1}, {"action": "zoom", "region": [0, 0, 100, 100]}])
    assert text(out).startswith("Ran 4 of 4 steps.")
    assert ("keyDown", "enter") in rig.gui.events


# ===========================================================================
# a full call and its answer
# ===========================================================================

def test_a_form_in_one_call_and_its_answer(rig):
    look(rig)
    out = call(rig, [
        {"action": "left_click", "coordinate": [412, 230]},
        {"action": "type", "text": "Maria"},
        {"action": "left_click", "coordinate": [412, 290]},
        {"action": "type", "text": "Papadopoulou"},
        {"action": "zoom", "region": [380, 200, 700, 320]},
    ])
    assert [b.type for b in out] == ["image", "image", "text"]
    assert text(out) == (
        "Ran 5 of 5 steps.\n"
        "1 left_click (412, 230): done\n"
        "2 type 5 characters: done\n"
        "3 left_click (412, 290): done\n"
        "4 type 12 characters: done\n"
        "5 zoom (380, 200)-(700, 320): picture 2, 320x120 px, at 1.00x your screenshot's scale. "
        "Click coordinates stay in your screenshot's space.\n"
        "Screenshot of display 1 of 1 (800x600 px). Click coordinates are in this image's pixel space: "
        "(0,0) top-left, max (799,599).")
    writes = [e for e in rig.gui.events if e[0] == "write"]
    assert sum(e[1] for e in writes) == len("Maria") + len("Papadopoulou")


def test_a_screenshot_alone_saves_when_asked(rig, monkeypatch, tmp_path):
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    out = call(rig, [{"action": "screenshot", "save": True}])
    assert "Saved full-resolution screenshot to 'screenshots/screen-1-" in text(out)
    saved = list((tmp_path / "screenshots").glob("screen-1-*.png"))
    assert len(saved) == 1 and not (tmp_path / ".screenshots").exists()


def test_a_zoom_alone_needs_no_picture_and_returns_only_its_own(rig, monkeypatch, tmp_path):
    monkeypatch.setenv("OTO_WORKSPACE_DIR", str(tmp_path))
    out = call(rig, [{"action": "zoom", "region": [10, 10, 110, 60], "save": True}])
    assert [b.type for b in out] == ["image", "text"]
    assert "1 zoom (10, 10)-(110, 60): picture 1, 100x50 px" in text(out)
    assert "Saved to 'screenshots/zoom-1-" in text(out)
    assert f"On this computer: {tmp_path}/screenshots/zoom-1-" in text(out)
    assert list((tmp_path / "screenshots").glob("zoom-1-*.png"))
    assert rig.grabber.regions == [{"left": 10, "top": 10, "width": 100, "height": 50}]


def test_cursor_position_alone_is_a_line(rig):
    rig.gui.pos = (40, 30)
    out = call(rig, [{"action": "cursor_position"}])
    assert [b.type for b in out] == ["text"]
    assert "1 cursor_position: (40, 30) in display 1's screenshot space" in text(out)


def test_a_wait_alone_returns_a_fresh_picture(rig):
    out = call(rig, [{"action": "wait", "duration": 2}])
    assert [b.type for b in out] == ["image", "text"]
    assert "1 wait 2s: done" in text(out)
    assert 2 in rig.clock.slept


def test_the_layout_is_read_once_and_a_single_click_has_no_interval(rig):
    look(rig)
    rig.layout_reads = 0
    call(rig, [{"action": "left_click", "coordinate": [50, 50]}, {"action": "double_click", "coordinate": [60, 60]}])
    assert rig.layout_reads == 1
    clicks = [e for e in rig.gui.events if e[0] == "click"]
    assert clicks[0][3:] == (1, "left", 0.0)
    assert clicks[1][3:] == (2, "left", 0.05)


# ===========================================================================
# the last picture
# ===========================================================================

def test_a_new_process_has_no_picture_so_a_click_returns_one_and_does_nothing(rig):
    out = call(rig, [{"action": "left_click", "coordinate": [10, 10]}])
    assert images(out)
    assert text(out).startswith("Ran 0 of 1 step. Stopped before step 1.\n1 left_click (10, 10): stopped. Nothing ran.")
    assert rig.gui.inputs() == []
    again = call(rig, [{"action": "left_click", "coordinate": [10, 10]}])
    assert "1 left_click (10, 10): done" in text(again)


def test_keys_need_no_picture(rig):
    out = call(rig, [{"action": "key", "text": "ctrl+l"}])
    assert "1 key ctrl+l: done" in text(out)


def test_a_screen_of_another_size_stops_a_click(rig, monkeypatch):
    look(rig)
    rig.monitors[0] = geometry.Monitor(1, 0, 0, 1024, 768, 1024, 768, True)
    rig.screen_img = fakes.desk_image(1024, 768)
    out = call(rig, [{"action": "left_click", "coordinate": [10, 10]}])
    assert "stopped. The screen changed size since your last screenshot." in text(out)
    assert rig.gui.inputs() == []


# ===========================================================================
# the stops, each with no input after it
# ===========================================================================

def test_a_dialog_over_the_next_click_stops_the_list_before_it(rig):
    look(rig)

    def dialog_after_first_click(gui, ev):
        if ev[0] == "click" and ev[1] == 100:
            rig.screen_img = fakes.desk_image(boxes=[(250, 150, 550, 450, (30, 30, 30))])
    rig.gui.on_event = dialog_after_first_click
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "x"},
                     {"action": "left_click", "coordinate": [400, 300]}, {"action": "type", "text": "y"}])
    assert text(out).startswith("Ran 2 of 4 steps. Stopped before step 3.")
    assert "3 left_click (400, 300): stopped. The screen under (400, 300) changed since your last screenshot." in text(out)
    assert "4: not run." in text(out)
    assert [e for e in rig.gui.events if e[0] == "click"] == [("click", 100, 100, 1, "left", 0.0)]
    assert [e for e in rig.gui.events if e[0] == "write"] == [("write", 1)]


def test_a_screen_that_changed_while_the_model_thought_stops_the_first_click(rig):
    look(rig)
    rig.screen_img = fakes.desk_image(boxes=[(250, 150, 550, 450, (30, 30, 30))])
    out = call(rig, [{"action": "left_click", "coordinate": [400, 300]}])
    assert "1 left_click (400, 300): stopped. The screen under (400, 300)" in text(out)
    assert rig.gui.inputs() == []
    again = call(rig, [{"action": "left_click", "coordinate": [400, 300]}])
    assert "1 left_click (400, 300): done" in text(again)


def test_a_person_moving_the_mouse_stops_the_list(rig):
    look(rig)

    def person(gui, ev):
        if ev[0] == "write":
            gui.pos = (700, 500)
    rig.gui.on_event = person
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "abc"},
                     {"action": "key", "text": "Tab"}])
    assert "3 key Tab: stopped. Someone moved the mouse. A person may be using this computer." in text(out)
    assert not any(e[0] == "keyDown" for e in rig.gui.events)


def test_a_mouse_moved_between_calls_stops_the_first_step_then_is_known(rig):
    look(rig)
    rig.gui.pos = (5, 5)
    out = call(rig, [{"action": "key", "text": "Tab"}])
    assert "1 key Tab: stopped. Someone moved the mouse." in text(out)
    assert rig.gui.inputs() == []
    again = call(rig, [{"action": "key", "text": "Tab"}])
    assert "1 key Tab: done" in text(again)


def test_a_person_typing_into_their_mail_stops_a_long_type_between_chunks(rig):
    look(rig)

    def person(gui, ev):
        if ev == ("write", 3) and len([e for e in gui.events if e[0] == "write"]) == 4:
            gui.pos = (650, 50)
    rig.gui.on_event = person
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "x" * 57}])
    assert text(out).startswith("Ran 1 of 2 steps. Stopped in step 2.")
    assert "2 type 57 characters: stopped after 12. Someone moved the mouse." in text(out)
    assert [e for e in rig.gui.events if e[0] == "write"] == [("write", 3)] * 4


def test_a_window_that_takes_the_keys_mid_word_stops_the_typing(rig):
    look(rig)

    def dialog(gui, ev):
        if ev[0] == "write" and len([e for e in gui.events if e[0] == "write"]) == 2:
            rig.desk.window = Window(id=7, pid=10, program="form.exe", title="Session expired")
    rig.gui.on_event = dialog
    out = call(rig, [{"action": "type", "text": "maria@example.com"}])
    assert "1 type 17 characters: stopped after 6. Another window came to the front (form.exe)." in text(out)
    assert [e for e in rig.gui.events if e[0] == "write"] == [("write", 3), ("write", 3)]


def test_a_cancel_during_the_checks_stops_before_the_dispatch(rig):
    look(rig)
    cancel = threading.Event()

    class CancellingDesk(fakes.Desk):
        def front_window(self):
            cancel.set()
            return super().front_window()
    rig.desk.__class__ = CancellingDesk
    rig.clock.now += 3
    result = steps.run([{"action": "key", "text": "a"}], None, arrived=rig.clock.clock(),                        cancel=cancel, hooks=rig.hooks(server))
    assert result.cancelled and rig.gui.inputs() == []


def test_another_window_in_front_stops_the_list(rig):
    look(rig)

    def popup(gui, ev):
        if ev[0] == "click":
            rig.desk.window = Window(id=7, pid=70, program="notify.exe", title="You won a prize")
    rig.gui.on_event = popup
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "secret"}])
    assert ("2 type 6 characters: stopped. Another window came to the front (notify.exe). If you meant it, "
            "send the step again with window set to part of its name.") in text(out)
    assert "prize" not in text(out)
    assert not any(e[0] == "write" for e in rig.gui.events)


def test_a_named_window_is_honoured(rig):
    look(rig)

    def save_dialog(gui, ev):
        if ev == ("keyUp", "ctrl"):
            rig.desk.window = Window(id=8, pid=10, program="form.exe", title="Save As")
    rig.gui.on_event = save_dialog
    out = call(rig, [{"action": "key", "text": "ctrl+s"}, {"action": "type", "text": "order", "window": "save"}])
    assert "2 type 5 characters: done" in text(out)


def test_a_named_window_that_never_comes_forward_stops_the_step(rig):
    look(rig)
    out = call(rig, [{"action": "key", "text": "ctrl+s"}, {"action": "type", "text": "order", "window": "Save"}])
    assert '2 type 5 characters: stopped. The window in front is form.exe, not "Save".' in text(out)
    assert not any(e[0] == "write" for e in rig.gui.events)


def test_the_window_in_front_changed_between_calls_stops_the_first_step(rig):
    look(rig)
    rig.desk.window = Window(id=9, pid=90, program="mail.exe", title="Inbox")
    out = call(rig, [{"action": "type", "text": "hello"}])
    assert "1 type 5 characters: stopped. Another window came to the front (mail.exe)." in text(out)
    assert rig.gui.inputs() == []


def test_no_step_starts_after_thirty_seconds(rig):
    look(rig)
    out = call(rig, [{"action": "wait", "duration": 5}] * 6 + [{"action": "key", "text": "a"}] * 2)
    assert "7 key a: stopped. This call has run for 30 seconds. Send the rest in a new call." in text(out)
    assert not any(e[0] == "keyDown" for e in rig.gui.events)


def test_a_screen_that_cannot_be_read_stops_every_later_step_that_acts(rig):
    look(rig)
    rig.grabber.fail_after = rig.grabber.grabs + 2
    out = call(rig, [{"action": "key", "text": "a"}, {"action": "key", "text": "b"}])
    assert "2 key b: stopped. The screen could not be read, so nothing more ran." in text(out)
    assert ("keyDown", "b") not in rig.gui.events


def test_a_locked_screen_fails_the_next_step(rig, monkeypatch):
    look(rig)
    calls = {"n": 0}

    def session():
        calls["n"] += 1
        if calls["n"] > 2:
            raise environment.DeviceUnavailable("No interactive desktop session is on the console.")
    monkeypatch.setattr(environment, "check_session_active", session)
    out = call(rig, [{"action": "key", "text": "a"}, {"action": "key", "text": "b"}])
    assert text(out).startswith("Ran 1 of 2 steps. Step 2 failed.")
    assert "2 key b: failed. No interactive desktop session is on the console." in text(out)
    assert ("keyDown", "b") not in rig.gui.events


def test_a_program_with_more_rights_fails_a_click_by_the_window_under_its_point(rig):
    look(rig)
    rig.desk.under = Window(id=5, pid=50, program="taskmgr.exe", title="Task Manager")
    rig.desk.blocked = {5}
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}])
    assert ("1 left_click (100, 100): failed. That window belongs to a program with more rights than this "
            "computer's OtoDock (an administrator program or the system), so this tool cannot reach it.") in text(out)
    assert rig.gui.inputs() == []


def test_keys_are_judged_by_the_window_in_front(rig):
    look(rig)
    rig.desk.under = Window(id=5, pid=50, program="taskmgr.exe", title="Task Manager")
    rig.desk.blocked = {5}
    out = call(rig, [{"action": "key", "text": "a"}])
    assert "1 key a: done" in text(out)
    rig.desk.blocked = {1}
    out = call(rig, [{"action": "key", "text": "b"}])
    assert "1 key b: failed. That window belongs to a program with more rights" in text(out)


def test_secure_input_in_the_window_in_front_fails_typing(rig):
    look(rig)
    rig.desk.secure = (True, 10)
    out = call(rig, [{"action": "type", "text": "hello"}, {"action": "key", "text": "Tab"}])
    assert text(out).startswith("Ran 0 of 2 steps. Step 1 failed.")
    assert "1 type 5 characters: failed. macOS Secure Input is on in the window in front" in text(out)
    assert rig.gui.inputs() == []


def test_secure_input_held_by_another_program_is_a_note(rig):
    look(rig)
    rig.desk.secure = (True, 99)
    rig.desk.programs = {99: "1Password"}
    out = call(rig, [{"action": "type", "text": "hello"}])
    assert "1 type 5 characters: done" in text(out)
    assert "Step 1: Secure Input is on in 1Password, so typed keys may be lost." in text(out)


def test_an_error_from_the_input_library_fails_the_step_and_names_only_its_type(rig, caplog):
    look(rig)
    rig.gui.fail_on = ("write",)
    with caplog.at_level(logging.DEBUG):
        out = call(rig, [{"action": "type", "text": "hunter2"}, {"action": "key", "text": "Tab"}])
    assert "1 type 7 characters: failed. Unexpected error (OSError)." in text(out)
    assert "2: not run." in text(out)
    assert all("hunter2" not in r.getMessage() for r in caplog.records)
    assert ("keyUp", "shift") in rig.gui.events


def test_a_cancel_between_steps_sends_nothing_more(rig):
    look(rig)
    cancel = threading.Event()

    def engine_gives_up(gui, ev):
        if ev[0] == "click":
            cancel.set()
    rig.gui.on_event = engine_gives_up
    rig.clock.now += 3
    result = steps.run([{"action": "left_click", "coordinate": [10, 10]}, {"action": "key", "text": "a"}], None,
                       arrived=rig.clock.clock(), cancel=cancel, hooks=rig.hooks(server))
    assert result.cancelled
    assert not any(e[0] == "keyDown" for e in rig.gui.events)
    assert steps._SEEN[1].frame is not None  # the earlier look stays the baseline


def test_a_cancelled_wait_ends_at_once(rig):
    cancel = threading.Event()
    cancel.set()
    result = steps.run([{"action": "wait", "duration": 5}], None, arrived=rig.clock.clock(),                        cancel=cancel, hooks=rig.hooks(server))
    assert result.cancelled and 5 not in rig.clock.slept


def test_an_engine_that_cancels_the_call_stops_the_thread_before_its_next_step(rig):
    look(rig)
    gate = threading.Event()

    def slow_click(gui, ev):
        if ev[0] == "click":
            gate.wait(5)
    rig.gui.on_event = slow_click
    server._hooks = lambda: rig.hooks(server)
    rig.clock.now += 3

    async def scenario():
        task = asyncio.ensure_future(server.call_tool("computer", {"steps": [
            {"action": "left_click", "coordinate": [10, 10]}, {"action": "key", "text": "a"}]}))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if any(e[0] == "click" for e in rig.gui.events):
                break                       # the thread is inside the click
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert steps._LOCK.locked()        # the thread still holds the screen
        gate.set()
        for _ in range(200):
            if not steps._LOCK.locked():
                break
            await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert not steps._LOCK.locked()
    assert [e[0] for e in rig.gui.events] == ["click"]   # the click ran, the key never did


def test_a_call_that_cannot_get_the_screen_in_thirty_seconds_runs_nothing(rig):
    look(rig)
    assert steps._LOCK.acquire()
    try:
        result = steps.run([{"action": "key", "text": "a"}], None, arrived=rig.clock.clock() - 29.95,
                           cancel=threading.Event(), hooks=rig.hooks(server))
    finally:
        steps._LOCK.release()
    assert result.lines[:2] == ["Ran 0 of 1 step. Stopped before step 1.", "1: stopped. Another call was using the screen."]
    assert rig.gui.inputs() == []


def test_another_process_on_the_desktop_holds_its_lock(rig):
    look(rig)
    other = environment.desktop_lock()
    assert other.acquire(1)
    try:
        result = steps.run([{"action": "key", "text": "a"}], None, arrived=rig.clock.clock() - 29.9,
                           cancel=threading.Event(), hooks=rig.hooks(server))
    finally:
        other.release()
    assert "Another call was using the screen." in result.lines[1]
    assert rig.gui.inputs() == []


# ===========================================================================
# nothing held after a stop or an exception
# ===========================================================================

def test_a_click_with_keys_held_releases_them_when_it_fails(rig):
    look(rig)
    rig.gui.fail_on = ("click",)
    out = call(rig, [{"action": "left_click", "coordinate": [10, 10], "text": "ctrl+shift"}])
    assert "failed" in text(out)
    downs = [e[1] for e in rig.gui.events if e[0] == "keyDown"]
    ups = [e[1] for e in rig.gui.events if e[0] == "keyUp"]
    assert downs == ["ctrl", "shift"] and ups == ["shift", "ctrl"]
    assert executor.held() == ([], [])


def test_a_combo_that_fails_on_its_second_key_releases_the_first(rig):
    look(rig)
    rig.gui.fail_on = ("keyDown", "s")
    out = call(rig, [{"action": "key", "text": "ctrl+s"}])
    assert "failed" in text(out)
    assert ("keyUp", "ctrl") in rig.gui.events
    assert executor.held() == ([], [])


def test_a_drag_that_fails_releases_the_button(rig):
    look(rig)
    rig.gui.fail_on = ("dragTo",)
    out = call(rig, [{"action": "left_click_drag", "coordinate": [50, 50], "start_coordinate": [10, 10]}])
    assert "failed" in text(out)
    assert ("mouseUp", "left") in rig.gui.events
    assert executor.held() == ([], [])


def test_a_held_key_released_when_the_call_is_cancelled_during_it(rig):
    look(rig)
    cancel = threading.Event()
    hooks = rig.hooks(server)

    def cancel_while_holding(event, seconds):
        cancel.set()
        return True
    hooks.wait = cancel_while_holding
    steps.run([{"action": "hold_key", "text": "shift", "duration": 3}], None, arrived=rig.clock.clock(),
              cancel=cancel, hooks=hooks)
    assert rig.gui.events[-1] == ("keyUp", "shift")
    assert executor.held() == ([], [])


def test_the_runner_releases_what_a_step_left_recorded(rig, monkeypatch):
    look(rig)
    real = server._do_step

    def leaves_a_key(args, ctx):
        executor._held_keys.append("alt")
        return real(args, ctx)
    monkeypatch.setattr(server, "_do_step", leaves_a_key)
    call(rig, [{"action": "key", "text": "a"}])
    assert rig.gui.events[-1] == ("keyUp", "alt")
    assert executor.held() == ([], [])


def test_nothing_is_released_when_nothing_is_held(rig):
    look(rig)
    rig.desk.window = Window(id=9, pid=90, program="mail.exe", title="Inbox")
    call(rig, [{"action": "key", "text": "a"}])
    assert rig.gui.inputs() == []


def test_a_signal_cancels_the_running_call_and_exits(monkeypatch):
    flag = threading.Event()
    steps._CANCELS.add(flag)
    exits = []
    monkeypatch.setattr(server.os, "_exit", exits.append)
    try:
        server._on_signal(15, None)
    finally:
        steps._CANCELS.discard(flag)
    assert flag.is_set() and exits == [143]


# ===========================================================================
# the continuation: two calls planned on one picture
# ===========================================================================

def _dialog_on_first_click(rig):
    def dialog(gui, ev):
        if ev[0] == "click" and ev[1] == 100:
            rig.screen_img = fakes.desk_image(boxes=[(450, 350, 750, 550, (30, 30, 30))])
    rig.gui.on_event = dialog


def test_a_second_call_sent_in_the_same_turn_is_judged_on_the_first_calls_picture(rig):
    look(rig)
    _dialog_on_first_click(rig)
    call(rig, [{"action": "left_click", "coordinate": [100, 100]}])
    out = call(rig, [{"action": "left_click", "coordinate": [600, 450]}], think=0.2)
    assert "1 left_click (600, 450): stopped. The screen under (600, 450) changed" in text(out)
    assert len([e for e in rig.gui.events if e[0] == "click"]) == 1


def test_a_call_after_the_model_looked_is_judged_on_the_last_picture(rig):
    look(rig)
    _dialog_on_first_click(rig)
    call(rig, [{"action": "left_click", "coordinate": [100, 100]}])
    out = call(rig, [{"action": "left_click", "coordinate": [600, 450]}], think=4.0)
    assert "1 left_click (600, 450): done" in text(out)


def test_a_same_turn_call_after_a_stop_stops_once_then_the_chain_ends(rig):
    look(rig)

    def dialog(gui, ev):
        if ev[0] == "click" and ev[1] == 100:
            rig.desk.window = Window(id=7, pid=70, program="bank.exe", title="Confirm transfer")
            rig.screen_img = fakes.desk_image(boxes=[(450, 350, 750, 550, (30, 30, 30))])
    rig.gui.on_event = dialog
    first = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "x"}])
    assert "Another window came to the front (bank.exe)" in text(first)
    second = call(rig, [{"action": "left_click", "coordinate": [600, 450]}], think=0.05)
    assert "1 left_click (600, 450): stopped. Nothing ran. An earlier call of this turn stopped" in text(second)
    assert images(second)
    assert ("click", 600, 450, 1, "left", 0.0) not in rig.gui.events
    third = call(rig, [{"action": "key", "text": "Escape"}], think=0.05)
    assert "1 key Escape: done" in text(third)   # never two chain stops in a row


def test_a_same_turn_call_after_no_picture_stops_too(rig):
    first = call(rig, [{"action": "left_click", "coordinate": [300, 300]}])
    assert "Nothing ran. This tool has no screenshot" in text(first)
    second = call(rig, [{"action": "left_click", "coordinate": [500, 400]}], think=0.05)
    assert "An earlier call of this turn stopped" in text(second)
    assert rig.gui.inputs() == []


def test_a_zoom_alone_between_two_calls_of_a_turn_keeps_the_chain(rig):
    look(rig)
    _dialog_on_first_click(rig)
    call(rig, [{"action": "left_click", "coordinate": [100, 100]}])
    call(rig, [{"action": "zoom", "region": [0, 0, 100, 100]}], think=0.05)
    out = call(rig, [{"action": "left_click", "coordinate": [600, 450]}], think=0.05)
    assert "1 left_click (600, 450): stopped. The screen under (600, 450) changed" in text(out)


def test_a_reads_only_call_after_a_stop_is_never_stopped(rig):
    look(rig)
    rig.gui.pos = (1, 1)
    assert "Someone moved the mouse" in text(call(rig, [{"action": "key", "text": "a"}]))
    out = call(rig, [{"action": "zoom", "region": [0, 0, 100, 100]}], think=0.05)
    assert "1 zoom (0, 0)-(100, 100): picture 1" in text(out)


# ===========================================================================
# the still wait in a call, the mask, the clipboard
# ===========================================================================

def test_a_step_with_no_visible_effect_waits_the_settle_time(rig):
    look(rig)
    before = rig.clock.now
    call(rig, [{"action": "key", "text": "a"}], think=0)
    assert 0.7 <= rig.clock.now - before < 1.0


def _moving_box(tick):
    """A video in the corner: every pixel of its box changes on every frame."""
    def source(mon):
        tick["n"] += 1
        return fakes.frame_of(fakes.desk_image(boxes=[(10, 10, 90, 60, (tick["n"] * 37 % 256, 0, 0))]), mon)
    return source


def test_a_spinner_the_step_started_waits_the_ceiling_and_says_so(rig):
    look(rig)
    tick = {"n": 0}

    def start(gui, ev):
        if ev[0] == "keyUp":
            rig.grabber.source = _moving_box(tick)
    rig.gui.on_event = start
    before = rig.clock.now
    out = call(rig, [{"action": "key", "text": "a"}], think=0)
    assert "1 key a: done (the screen was still changing after 2 s)" in text(out)
    assert 2.0 <= rig.clock.now - before < 2.3


def test_a_video_that_was_already_playing_holds_no_wait(rig):
    look(rig)
    tick = {"n": 0}
    rig.grabber.source = _moving_box(tick)
    before = rig.clock.now
    out = call(rig, [{"action": "key", "text": "a"}], think=0)
    assert "1 key a: done" in text(out) and "still changing" not in text(out)
    assert rig.clock.now - before < 1.0


def test_a_click_on_a_video_that_was_already_playing_is_not_stopped(rig):
    tick = {"n": 0}
    rig.grabber.source = _moving_box(tick)
    look(rig)
    out = call(rig, [{"action": "left_click", "coordinate": [40, 30]}])
    assert "1 left_click (40, 30): done" in text(out)


def test_settle_zero_waits_nothing(rig):
    look(rig)
    rig.settle_s = 0
    before = rig.clock.now
    call(rig, [{"action": "key", "text": "a"}], think=0)
    assert rig.clock.now - before < 0.1


def test_a_paste_puts_the_clipboard_back_once_it_landed(rig, monkeypatch):
    look(rig)
    board = {"v": "the person's copy"}
    monkeypatch.setattr(executor, "clipboard_text", lambda: board["v"])
    monkeypatch.setattr(executor, "set_clipboard_text", lambda v: board.__setitem__("v", v))
    import types
    monkeypatch.setitem(__import__("sys").modules, "pyperclip",
                        types.SimpleNamespace(copy=lambda v: board.__setitem__("v", v), paste=lambda: board["v"]))

    def paste_lands(gui, ev):
        if ev == ("keyUp", "ctrl"):
            rig.screen_img = fakes.desk_image(boxes=[(10, 10, 90, 30, (0, 0, 0))])
    rig.gui.on_event = paste_lands
    out = call(rig, [{"action": "type", "text": "Καλημέρα"}])
    assert "1 type 8 characters: done" in text(out)
    assert board["v"] == "the person's copy"


def test_a_paste_with_no_visible_effect_empties_the_clipboard(rig, monkeypatch):
    look(rig)
    board = {"v": "the person's copy"}
    monkeypatch.setattr(executor, "clipboard_text", lambda: board["v"])
    monkeypatch.setattr(executor, "set_clipboard_text", lambda v: board.__setitem__("v", v))
    import types
    monkeypatch.setitem(__import__("sys").modules, "pyperclip",
                        types.SimpleNamespace(copy=lambda v: board.__setitem__("v", v), paste=lambda: board["v"]))
    call(rig, [{"action": "type", "text": "Καλημέρα"}])
    assert board["v"] == ""


def test_a_clipboard_that_hangs_costs_one_timeout_and_a_line(rig, monkeypatch):
    look(rig)
    monkeypatch.setattr(steps, "CLIPBOARD_LAG_S", 0.05)
    monkeypatch.setattr(executor, "clipboard_text", lambda: threading.Event().wait(2))
    import types
    monkeypatch.setitem(__import__("sys").modules, "pyperclip", types.SimpleNamespace(copy=lambda v: None))
    out = call(rig, [{"action": "type", "text": "Καλημέρα"}])
    assert "Step 1: the clipboard could not be read, so it now holds the typed text." in text(out)


# ===========================================================================
# the checks that cannot read
# ===========================================================================

def test_a_window_read_that_fails_turns_its_check_off_with_one_line(rig):
    look(rig)
    rig.desk.fail = {"front_window"}
    out = call(rig, [{"action": "key", "text": "a"}, {"action": "key", "text": "b"}])
    assert "2 key b: done" in text(out)
    assert text(out).count("The check of the window in front was off for this call") == 1


def test_a_window_read_that_hangs_costs_one_timeout_and_never_runs_again(rig, monkeypatch):
    import guards

    monkeypatch.setattr(guards, "READ_TIMEOUT_S", 0.05)
    look(rig)
    rig.desk.hang = {"front_window"}
    out = call(rig, [{"action": "key", "text": "a"}])
    assert "1 key a: done" in text(out)
    assert "The check of the window in front was off" in text(out)
    hung = rig.desk.calls.count("front_window")
    call(rig, [{"action": "key", "text": "b"}])
    assert rig.desk.calls.count("front_window") == hung


def test_a_cursor_that_cannot_be_read_turns_the_mouse_check_off(rig, monkeypatch):
    look(rig)

    def broken():
        raise OSError("no pointer")
    monkeypatch.setattr(rig.gui, "position", broken)
    out = call(rig, [{"action": "left_click", "coordinate": [10, 10]}])
    assert "1 left_click (10, 10): done" in text(out)
    assert "The check of the mouse was off for this call: the cursor could not be read." in text(out)


def test_no_window_reads_on_this_system_turns_the_check_off(rig, monkeypatch):
    look(rig)
    monkeypatch.setattr(environment, "desktop", lambda: None)
    out = call(rig, [{"action": "key", "text": "a"}])
    assert "1 key a: done" in text(out)
    assert "The check of the window in front was off" in text(out)


# ===========================================================================
# typed text never in a line or a log
# ===========================================================================

def test_typed_text_reaches_no_line_and_no_log(rig, caplog):
    look(rig)
    secret = "correct horse battery"
    with caplog.at_level(logging.DEBUG):
        out = call(rig, [{"action": "left_click", "coordinate": [10, 10]}, {"action": "type", "text": secret}])
        rig.gui.pos = (1, 1)
        stopped = call(rig, [{"action": "type", "text": secret}])
    for blocks in (out, stopped):
        assert all(secret not in getattr(b, "text", "") for b in blocks)
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert json.dumps([getattr(b, "text", "") for b in out]).count("characters") == 1


# ===========================================================================
# the review's findings, each held by a test
# ===========================================================================

def test_a_window_that_comes_forward_during_the_wait_is_in_the_spot_frame(rig):
    look(rig)
    reads = {"n": 0, "armed": False}

    class LateDialog(fakes.Desk):
        def front_window(self):
            if reads["armed"]:
                reads["n"] += 1
                if reads["n"] == 3:   # the Save dialog comes forward during the window wait
                    self.window = Window(id=8, pid=10, program="form.exe", title="Save As")
                    rig.screen_img = fakes.desk_image(boxes=[(250, 150, 550, 450, (30, 30, 30))])
            return super().front_window()
    rig.desk.__class__ = LateDialog

    def arm(gui, ev):
        if ev == ("keyUp", "ctrl"):
            reads["armed"] = True
    rig.gui.on_event = arm
    out = call(rig, [{"action": "key", "text": "ctrl+s"},
                     {"action": "left_click", "coordinate": [400, 300], "window": "Save"}])
    assert "2 left_click (400, 300): stopped. The screen under (400, 300) changed" in text(out)
    assert not any(e[0] == "click" for e in rig.gui.events)


def test_the_window_wait_is_one_second_of_time(rig):
    look(rig)
    reads = []

    class SlowDesk(fakes.Desk):
        def front_window(self):
            reads.append(1)
            rig.clock.now += 0.2      # each read takes 0.2 s
            return super().front_window()
    rig.desk.__class__ = SlowDesk
    reads.clear()
    out = call(rig, [{"action": "type", "text": "order", "window": "Save"}])
    assert 'not "Save"' in text(out)
    assert len(reads) <= 6


def test_the_picture_after_a_stop_inside_a_type_is_fresh(rig):
    look(rig)

    def dialog(gui, ev):
        if ev[0] == "write" and len([e for e in gui.events if e[0] == "write"]) == 2:
            rig.desk.window = Window(id=7, pid=10, program="form.exe", title="Session expired")
            rig.screen_img = fakes.desk_image(boxes=[(250, 150, 550, 450, (30, 30, 30))])
    rig.gui.on_event = dialog
    out = call(rig, [{"action": "type", "text": "maria@example.com"}])
    assert "stopped after 6" in text(out)
    assert steps._SEEN[1].frame.raw == fakes.frame_of(rig.screen_img).raw   # the dialog is in it


def test_the_picture_after_a_failed_drag_is_fresh(rig):
    look(rig)

    def moved_then_fails(gui, ev):
        if ev[0] == "moveTo":
            rig.screen_img = fakes.desk_image(boxes=[(0, 0, 400, 300, (0, 0, 0))])
    rig.gui.on_event = moved_then_fails
    rig.gui.fail_on = ("dragTo",)
    out = call(rig, [{"action": "left_click_drag", "coordinate": [50, 50], "start_coordinate": [10, 10]}])
    assert "failed" in text(out)
    assert steps._SEEN[1].frame.raw == fakes.frame_of(rig.screen_img).raw


def test_a_target_that_keeps_changing_stops_once_then_runs_with_a_note(rig):
    look(rig)
    rig.screen_img = fakes.desk_image(boxes=[(380, 280, 420, 320, (200, 0, 0))])   # a carousel under the target
    first = call(rig, [{"action": "left_click", "coordinate": [400, 300]}])
    assert "stopped. The screen under (400, 300) changed" in text(first)
    rig.screen_img = fakes.desk_image(boxes=[(380, 280, 420, 320, (0, 0, 200))])   # it turned again
    second = call(rig, [{"action": "left_click", "coordinate": [402, 301]}])
    assert "1 left_click (402, 301): done" in text(second)
    assert "Step 1: the screen under (402, 301) keeps changing, so it was not checked." in text(second)


def test_a_mouse_moved_during_the_last_still_wait_is_seen_next_call(rig):
    look(rig)

    def person(gui, ev):
        if ev[0] == "keyUp":
            gui.pos = (700, 500)
    rig.gui.on_event = person
    call(rig, [{"action": "key", "text": "a"}])
    rig.gui.on_event = None
    nxt = call(rig, [{"action": "key", "text": "b"}])
    assert "1 key b: stopped. Someone moved the mouse." in text(nxt)


def _board(monkeypatch, start="the person's copy"):
    import types

    board = {"v": start}
    monkeypatch.setattr(executor, "clipboard_text", lambda: board["v"])
    monkeypatch.setattr(executor, "set_clipboard_text", lambda v: board.__setitem__("v", v))
    monkeypatch.setitem(__import__("sys").modules, "pyperclip",
                        types.SimpleNamespace(copy=lambda v: board.__setitem__("v", v), paste=lambda: board["v"]))
    return board


def test_a_short_paste_puts_the_clipboard_back(rig, monkeypatch):
    look(rig)
    board = _board(monkeypatch)

    def paste_lands(gui, ev):
        if ev == ("keyUp", "ctrl"):
            rig.screen_img = fakes.desk_image(boxes=[(100, 98, 118, 110, (0, 0, 0))])   # "José", a cell or two
    rig.gui.on_event = paste_lands
    call(rig, [{"action": "type", "text": "José"}])
    assert board["v"] == "the person's copy"


def test_a_paste_that_fails_puts_the_clipboard_back(rig, monkeypatch):
    look(rig)
    board = _board(monkeypatch)
    rig.gui.fail_on = ("keyDown", "v")
    out = call(rig, [{"action": "type", "text": "Καλημέρα"}])
    assert "failed" in text(out)
    assert board["v"] == "the person's copy"


def test_settle_zero_leaves_the_clipboard_alone(rig, monkeypatch):
    look(rig)
    board = _board(monkeypatch)
    rig.settle_s = 0
    call(rig, [{"action": "type", "text": "Καλημέρα"}])
    assert board["v"] == "Καλημέρα"


def test_a_line_break_in_typed_text_is_pasted_and_never_presses_enter(rig, monkeypatch):
    look(rig)
    board = _board(monkeypatch)
    out = call(rig, [{"action": "type", "text": "Dear Maria,\nthank you."}, {"action": "key", "text": "Tab"}])
    assert "1 type 22 characters: done" in text(out)
    assert ("keyDown", "enter") not in rig.gui.events and not any(e[0] == "write" for e in rig.gui.events)
    assert ("keyDown", "v") in rig.gui.events
    assert board["v"] == ""   # nothing visibly changed in this fake, so the text is not left behind


def test_an_exception_after_a_step_ran_keeps_its_lines(rig):
    look(rig)

    def broken(rect):
        raise RuntimeError("a picture library failure")
    rig.grabber.region_image = broken
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "zoom", "region": [0, 0, 100, 100]}])
    assert text(out).startswith("Ran 1 of 2 steps. Step 2 failed.")
    assert "1 left_click (100, 100): done" in text(out)
    assert "2 zoom (0, 0)-(100, 100): failed. Unexpected error (RuntimeError)." in text(out)
    assert images(out)


def test_the_head_says_stopped_in_when_a_type_stopped_with_steps_after(rig):
    look(rig)

    def person(gui, ev):
        if ev == ("write", 3) and len([e for e in gui.events if e[0] == "write"]) == 4:
            gui.pos = (650, 50)
    rig.gui.on_event = person
    out = call(rig, [{"action": "left_click", "coordinate": [100, 100]}, {"action": "type", "text": "x" * 57},
                     {"action": "key", "text": "Tab"}])
    assert text(out).startswith("Ran 1 of 3 steps. Stopped in step 2.")
    assert "3: not run." in text(out)


def test_a_numeric_text_is_refused_without_its_value(rig):
    server._hooks = lambda: rig.hooks(server)
    asyncio.run(server.list_tools())
    out = asyncio.run(server.call_tool("computer", {"steps": [{"action": "type", "text": 482913}]}))
    assert out[0].text == "⚠️ Step 1: 'text' must be a string."
    assert "482913" not in str(out)


def test_a_screen_that_changed_whole_stops_typing(rig):
    look(rig)
    rig.screen_img = fakes.desk_image(colour=(20, 20, 60))     # a locker or a new page over everything
    out = call(rig, [{"action": "type", "text": "hunter2"}])
    assert "1 type 7 characters: stopped. Most of the screen changed since your last screenshot." in text(out)
    assert rig.gui.inputs() == []
    again = call(rig, [{"action": "type", "text": "hunter2"}])
    assert "1 type 7 characters: done" in text(again)
