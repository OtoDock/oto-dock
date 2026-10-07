"""F64: a satellite-origin ``file_changed`` is bound to the machine the
remote layer holds the session on; a session that layer does not hold and
whose context names no machine is a mismatch for a frame from a machine (an
empty target no longer passes the guard). A task placed on a machine carries
that machine on its context, so its write-back keeps working.
"""

from types import SimpleNamespace
from unittest.mock import patch

from core.remote import satellite_file_transfer as sft


def _sec(agent="pa", machine_id=""):
    return SimpleNamespace(agent=agent, placement=SimpleNamespace(machine_id=machine_id))


def _frame(agent="pa", path="users/u/workspace/x.txt", action="write", session_id="s-1"):
    return {"agent_slug": agent, "path": path, "action": action, "session_id": session_id}


def _machine_held(machine_id):
    layer = SimpleNamespace(_sessions={"s-1": SimpleNamespace(machine_id=machine_id)})
    return patch("core.session.session_manager.find_layer_for_session", return_value=layer)


def test_a_frame_from_the_machine_the_layer_holds_the_session_on_is_admitted():
    with patch("core.session.session_state.get_session_security", return_value=_sec()), \
         _machine_held("m-1"):
        frame = sft._admit_file_changed("m-1", _frame())
    assert frame is not None and frame.agent_slug == "pa"


def test_a_frame_from_another_machine_than_the_layer_holds_is_a_mismatch():
    with patch("core.session.session_state.get_session_security", return_value=_sec()), \
         _machine_held("m-1"):
        assert sft._admit_file_changed("m-9", _frame()) is None


def test_a_session_the_layer_does_not_hold_with_no_machine_on_its_context_is_a_mismatch():
    # The empty-target case the finding names: a context that names no
    # machine and a remote layer that holds no such session must not pass a
    # frame that arrived from a machine.
    with patch("core.session.session_state.get_session_security", return_value=_sec(machine_id="")), \
         patch("core.session.session_manager.find_layer_for_session", return_value=None):
        assert sft._admit_file_changed("m-1", _frame()) is None


def test_a_task_context_that_names_its_placement_machine_is_admitted():
    # A task placed on a machine carries placement.machine_id on its context
    # even when the remote layer record is not found by this path.
    with patch("core.session.session_state.get_session_security",
               return_value=_sec(machine_id="m-1")), \
         patch("core.session.session_manager.find_layer_for_session", return_value=None):
        frame = sft._admit_file_changed("m-1", _frame())
    assert frame is not None


def test_the_agent_must_still_match_after_the_machine_bind():
    with patch("core.session.session_state.get_session_security",
               return_value=_sec(agent="other", machine_id="m-1")), \
         patch("core.session.session_manager.find_layer_for_session", return_value=None):
        assert sft._admit_file_changed("m-1", _frame(agent="pa")) is None
