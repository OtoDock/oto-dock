"""The answer to the dialog that parked a terminal's turn reopens it, and no
other line does.

A question (AskUserQuestion) or a plan approval (ExitPlanMode) parks the turn
while the CLI shows its dialog. The person's answer is journaled as the
dialog call's ``tool_result``: that result, at its own time, is the batch's
``tool_use`` signal (the model answers it next), so the page shows the turn
running again within a tail of the answer instead of at the model's next stop
reason. A sibling tool's result, an error answer (Esc), or one of two
questions answered leave the turn as it is. Each ``tail_lines`` call below is
one batch, the satellite's forwarded lines (the local file path folds the
same lines through the same function).
"""
import json

import pytest

from core.session import transcript_tailer as T, transcript_tool_events as _TE

CHAT = "chat-dialog"


def _at(second: int) -> str:
    return f"2026-10-18T04:50:{second:02d}.000Z"


def _line(obj: dict) -> str:
    return json.dumps(obj)


def _prompt(text: str, second: int) -> str:
    return _line({"type": "user", "uuid": f"u{second}", "timestamp": _at(second),
                  "message": {"role": "user", "content": text}})


def _calls(blocks: list[tuple[str, str]], second: int) -> str:
    return _line({"type": "assistant", "uuid": f"a{second}", "timestamp": _at(second),
                  "message": {"role": "assistant", "stop_reason": "tool_use",
                              "content": [{"type": "tool_use", "id": tuid, "name": name,
                                           "input": {}} for tuid, name in blocks]}})


def _result(tuid: str, second: int, *, is_error: bool = False) -> str:
    block = {"type": "tool_result", "tool_use_id": tuid, "content": "answered"}
    if is_error:
        block["is_error"] = True
    return _line({"type": "user", "uuid": f"r{tuid}{second}", "timestamp": _at(second),
                  "message": {"role": "user", "content": [block]}})


def _seconds(second: int) -> float:
    return T.line_time({"timestamp": _at(second)})


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    import storage.database as db
    monkeypatch.setattr(db, "add_chat_message", lambda *a, **kw: None)
    monkeypatch.setattr(db, "rebuild_chat_search", lambda cid: None)
    monkeypatch.setattr(db, "get_chat", lambda cid: {"user_sub": "u", "title": "t"})
    monkeypatch.setattr(db, "update_chat", lambda cid, **kw: None)
    T._offsets.clear()
    T._tool_events.clear()
    T._attach_ts.clear()
    _TE._sent_prompts.clear()


def _tail(sid: str, *lines: str) -> dict:
    return T.tail_lines(sid, CHAT, list(lines))


def test_the_answer_alone_reopens_the_parked_turn_at_its_own_time():
    first = _tail("s1", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    assert first["question_pending"] is True and first["last_signal"] == "end_turn"
    answer = _tail("s1", _result("q1", 9))
    assert answer["last_signal"] == "tool_use"
    assert answer["last_signal_at"] == _seconds(9)
    assert answer["question_pending"] is False


def test_a_plan_approval_is_a_dialog_too():
    _tail("s2", _prompt("plan", 1), _calls([("p1", "ExitPlanMode")], 2))
    assert _tail("s2", _result("p1", 7))["last_signal"] == "tool_use"


def test_a_sibling_tools_result_after_the_park_does_not_reopen_it():
    # AskUserQuestion runs beside other tools of one message: the Read's
    # result can be journaled after the hook parked the turn.
    _tail("s3", _prompt("go", 1), _calls([("r1", "Read"), ("q1", "AskUserQuestion")], 2))
    assert _tail("s3", _result("r1", 4))["last_signal"] is None
    assert _tail("s3", _result("q1", 8))["last_signal"] == "tool_use"


def test_two_questions_reopen_on_the_second_answer_only():
    _tail("s4", _prompt("go", 1),
          _calls([("q1", "AskUserQuestion"), ("q2", "AskUserQuestion")], 2))
    assert _tail("s4", _result("q1", 5))["last_signal"] is None
    assert _tail("s4", _result("q2", 9))["last_signal"] == "tool_use"


def test_an_error_answer_is_esc_and_the_marker_ends_the_turn():
    _tail("s5", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    assert _tail("s5", _result("q1", 6, is_error=True))["last_signal"] is None
    marker = _tail("s5", _prompt("[Request interrupted by user for tool use]", 6))
    assert marker["last_signal"] == "end_turn"


def test_a_question_journaled_late_with_its_answer_reopens_at_the_answers_time():
    # The CLI wrote the question's own line only with the answer, so the
    # batch's assistant line predates the park: the answer's time decides.
    out = _tail("s6", _calls([("q1", "AskUserQuestion")], 2), _result("q1", 11))
    assert out["last_signal"] == "tool_use"
    assert out["last_signal_at"] == _seconds(11)
    assert out["question_pending"] is False


def test_a_carried_question_never_folds_a_later_batch():
    _tail("s7", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    later = _tail("s7", _calls([("b1", "Bash")], 3))
    assert later["question_pending"] is False and later["last_signal"] == "tool_use"


def test_a_new_prompt_or_the_marker_drops_an_unanswered_question():
    _tail("s8", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    _tail("s8", _prompt("never mind, do this instead", 3))
    assert T._tool_events["s8"].dialogs == set()
    # A stale id would otherwise keep any later question's answer from
    # reopening its turn.
    _tail("s8", _calls([("q2", "AskUserQuestion")], 4))
    assert _tail("s8", _result("q2", 6))["last_signal"] == "tool_use"
    _tail("s8", _calls([("q3", "AskUserQuestion")], 7))
    _tail("s8", _prompt("[Request interrupted by user]", 8))
    assert T._tool_events["s8"].dialogs == set()


def test_forget_and_the_attach_seek_drop_the_open_questions(tmp_path, monkeypatch):
    _tail("s9", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    assert T._tool_events["s9"].dialogs == {"q1"}
    T.forget("s9")
    assert "s9" not in T._tool_events
    _tail("s10", _prompt("go", 1), _calls([("q1", "AskUserQuestion")], 2))
    monkeypatch.setattr(T, "resolve_transcript_path", lambda sid: None)
    T.seek_past_existing("s10", CHAT)
    assert T._tool_events["s10"].dialogs == set()


def test_any_other_result_stays_signal_less():
    _tail("s11", _prompt("go", 1), _calls([("b1", "Bash")], 2))
    assert _tail("s11", _result("b1", 3))["last_signal"] is None
