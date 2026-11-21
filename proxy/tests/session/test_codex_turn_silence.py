"""A Codex turn that fails ends with the ``error`` ending: the engine
answered, so its process stays for the next message. The silence of a turn
on a machine is ``tests/remote/test_remote_codex_bg.py``'s."""

from core.events import turn_ending
from core.events.common_events import ERROR
from core.layers.codex.events import CodexEvent
from core.layers.codex.translator import CodexEventTranslator


def test_a_failed_turn_is_the_error_ending_and_keeps_the_process():
    tr = CodexEventTranslator(model="gpt-5.4", session_id="s1")
    failed = tr.translate(CodexEvent(type="turn/completed", data={
        "threadId": "thr-1", "turn": {"status": "failed", "error": {"message": "model 400"}}}))
    err = [e for e in failed if e.type == ERROR][0]
    ending = turn_ending.from_dict(err.data["ending"])
    assert ending.reason == turn_ending.ERROR and ending.detail == "model 400"
    assert ending.reason not in turn_ending.KILLS_PROCESS
