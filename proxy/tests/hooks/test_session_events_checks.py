"""core/session/session_events — what lane 3 added for the checks: the
headless forwarder's paths (the ``_paths`` key), the shell command on the
record, the result text a proxy-driven loop hands turn_end, the person's
message flag, and the hook interval (HOOKS.md)."""

import pytest

from core.events.common_events import DONE, TEXT, CommonEvent
from core.session import session_events as se


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    handlers = list(se._turn_end_handlers)
    yield
    for h in list(se._turn_end_handlers):
        if h not in handlers:
            se.unregister_turn_end_handler(h)
    for sid in ("c-paths", "c-cmd", "c-text", "c-msg", "c-hook"):
        se.cleanup_session(sid)


def test_the_forwarders_paths_land_on_the_record():
    # The receiver posts {"_paths": [...]}: the record must read it.
    se.post_tool("c-paths", "Write", tool_input={"_paths": ["/users/u/workspace/a.py"]},
                 source="forwarder")
    assert se.tool_records("c-paths")[0].paths == ("/users/u/workspace/a.py",)


def test_a_shell_command_is_kept_and_capped_and_only_for_shell_tools():
    se.post_tool("c-cmd", "Bash", tool_input={"command": "git commit -m x", "description": "d"},
                 source="tailer")
    se.post_tool("c-cmd", "Bash", command="npm run build", source="forwarder")
    se.post_tool("c-cmd", "Bash", tool_input={"cmd": "pytest -q"}, source="tailer")
    se.post_tool("c-cmd", "Write", tool_input={"command": "not a shell", "file_path": "/w/a"},
                 source="direct")
    se.post_tool("c-cmd", "Bash", tool_input={"command": "x" * (se.MAX_COMMAND_BYTES + 50)},
                 source="tailer")
    recs = se.tool_records("c-cmd")
    assert [r.command for r in recs[:3]] == ["git commit -m x", "npm run build", "pytest -q"]
    assert recs[3].command == "" and recs[3].paths == ("/w/a",)
    assert len(recs[4].command.encode()) == se.MAX_COMMAND_BYTES


@pytest.mark.asyncio
async def test_drive_turns_hands_the_turns_text_to_turn_end():
    seen: list[str] = []

    async def handler(ctx):
        seen.append(ctx.last_message)
        return se.STOP
    se.register_turn_end_handler(handler)

    async def run_turn(text):
        yield CommonEvent(type=TEXT, data={"content": "first "})
        yield CommonEvent(type=TEXT, data={"content": "second"})
        yield CommonEvent(type=DONE, data={})
    async for _ in se.drive_turns("c-text", "claude", "go", run_turn):
        pass
    assert seen == ["first second"]


def test_the_result_tail_is_kept_when_long():
    tail = se._TextTail()
    tail.add("a" * se.MAX_LAST_MESSAGE_BYTES)
    tail.add("END")
    out = tail.text()
    assert out.startswith("… ") and out.endswith("END")
    assert len(out.encode()) <= se.MAX_LAST_MESSAGE_BYTES + 4


def test_a_persons_message_is_noted_per_session():
    import time
    before = time.monotonic()
    assert not se.user_message_since("c-msg", before)
    se.note_user_message("c-msg")
    assert se.user_message_since("c-msg", before)
    assert not se.user_message_since("c-msg", time.monotonic() + 1)


@pytest.mark.asyncio
async def test_a_second_hook_evaluation_within_the_interval_is_an_observation():
    calls = []

    async def handler(ctx):
        calls.append(1)
        return se.continue_with("again")
    se.register_turn_end_handler(handler)
    v1 = await se.turn_end("c-hook", source="hook", engine="claude", driven_by="cli")
    v2 = await se.turn_end("c-hook", source="hook", engine="claude", driven_by="cli")
    assert v1.should_continue and not v2.should_continue
    assert calls == [1]
    assert se.rounds("c-hook") == 1
