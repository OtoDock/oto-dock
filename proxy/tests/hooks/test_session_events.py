"""core/session/session_events — the named session events every engine
reports to (docs/architecture/HOOKS.md): the turn-end verdict computed for
one source per placement, the round cap, the reason cap, the tool record,
the subagent stop, and ``drive_turns`` — the proxy-driven delivery of a
continue verdict as a follow-up turn."""

from __future__ import annotations

import pytest

from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
from core.session import session_events as se


@pytest.fixture(autouse=True)
def _clean():
    handlers = list(se._turn_end_handlers)
    yield
    for h in list(se._turn_end_handlers):
        if h not in handlers:
            se.unregister_turn_end_handler(h)
    for sid in ("s-hook", "s-loop", "s-obs", "s-rounds", "s-drive", "s-drive-stop",
                "s-rec", "s-cap", "s-cut"):
        se.cleanup_session(sid)


def _continue(reason: str):
    async def handler(ctx):
        return se.continue_with(reason)
    return handler


# --- turn_end: one verdict source per placement --------------------------

@pytest.mark.asyncio
async def test_default_verdict_is_stop_with_nothing_registered():
    v = await se.turn_end("s-hook", source="hook", engine="claude", driven_by="cli")
    assert v is se.STOP and not v.should_continue


@pytest.mark.asyncio
async def test_a_terminal_takes_its_verdict_from_the_hook_only():
    se.register_turn_end_handler(_continue("fix the tests"))
    # The transcript fold is an observation on a terminal.
    assert not (await se.turn_end("s-obs", source="transcript", engine="claude",
                                  driven_by="cli")).should_continue
    # A loop source never applies to a CLI-driven session either.
    assert not (await se.turn_end("s-obs", source="loop", engine="claude",
                                  driven_by="cli")).should_continue
    v = await se.turn_end("s-hook", source="hook", engine="codex", driven_by="cli")
    assert v.continue_reason == "fix the tests"


@pytest.mark.asyncio
async def test_a_proxy_driven_session_takes_its_verdict_from_the_loop_only():
    se.register_turn_end_handler(_continue("again"))
    # The Stop hook may still fire on a headless session: observation.
    assert not (await se.turn_end("s-loop", source="hook", engine="claude",
                                  driven_by="proxy")).should_continue
    assert (await se.turn_end("s-loop", source="loop", engine="claude",
                              driven_by="proxy")).should_continue


@pytest.mark.asyncio
async def test_rounds_are_capped_and_reset_when_the_turn_ends():
    se.register_turn_end_handler(_continue("more"))
    for n in range(se.MAX_ROUNDS):
        v = await se.turn_end("s-rounds", source="loop", driven_by="proxy")
        assert v.should_continue, n
        assert se.rounds("s-rounds") == n + 1
    # The cap: the turn ends whatever the handler says, and the count resets.
    v = await se.turn_end("s-rounds", source="loop", driven_by="proxy")
    assert not v.should_continue
    assert se.rounds("s-rounds") == 0


@pytest.mark.asyncio
async def test_the_last_fix_round_is_judged_but_never_continued():
    """A check is evaluated up to rounds + 1 times: the turn after the last
    continue reaches the handlers (a fix that worked must be seen passing),
    and a continue from it is refused."""
    seen: list[int] = []

    async def handler(ctx):
        seen.append(ctx.rounds)
        return se.continue_with("more")
    se.register_turn_end_handler(handler)
    for _ in range(se.MAX_ROUNDS + 1):
        await se.turn_end("s-rounds", source="loop", driven_by="proxy")
    assert seen == list(range(se.MAX_ROUNDS + 1))
    assert se.rounds("s-rounds") == 0


@pytest.mark.asyncio
async def test_stop_hook_active_counts_a_round_the_proxy_never_saw():
    """A hook reporting stop_hook_active after a proxy restart mid-loop: the
    round it carries counts toward the cap."""
    calls = []

    async def handler(ctx):
        calls.append(ctx.rounds)
        return se.continue_with("x")
    se.register_turn_end_handler(handler)
    await se.turn_end("s-rounds", source="hook", driven_by="cli", stop_hook_active=True)
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_failing_handler_lets_the_turn_end():
    async def boom(ctx):
        raise RuntimeError("judge down")
    se.register_turn_end_handler(boom)
    assert not (await se.turn_end("s-hook", source="hook", driven_by="cli")).should_continue


def test_the_reason_is_capped():
    v = se.continue_with("x" * (se.MAX_REASON_BYTES + 100))
    assert v.should_continue
    assert len(v.continue_reason.encode()) <= se.MAX_REASON_BYTES + 32
    assert v.continue_reason.endswith("(truncated)")
    assert se.continue_with("   ") is se.STOP


# --- post_tool: the turn's record ------------------------------------------

@pytest.mark.asyncio
async def test_tool_records_keep_paths_and_clear_when_the_turn_ends():
    se.post_tool("s-rec", "Write", tool_use_id="t1",
                 tool_input={"file_path": "/workspace/a.md", "content": "…"}, source="forwarder")
    se.post_tool("s-rec", "apply_patch", tool_use_id="t2",
                 tool_input={"_codex_paths": ["/w/b.py", "/w/c.py"]}, source="codex-stream")
    se.post_tool("s-rec", "mcp__x__y", tool_use_id="t3", is_error=True, source="direct")
    recs = se.tool_records("s-rec")
    assert [r.tool_name for r in recs] == ["Write", "apply_patch", "mcp__x__y"]
    assert recs[0].paths == ("/workspace/a.md",)
    assert recs[1].paths == ("/w/b.py", "/w/c.py")
    assert recs[2].is_error and recs[2].paths == ()
    # A stop verdict ends the turn: the record is cleared.
    await se.turn_end("s-rec", source="loop", driven_by="proxy")
    assert se.tool_records("s-rec") == []


def test_tool_records_are_a_bounded_ring():
    for i in range(se.MAX_TOOL_RECORDS + 20):
        se.post_tool("s-cap", "Read", tool_use_id=f"t{i}")
    recs = se.tool_records("s-cap")
    assert len(recs) == se.MAX_TOOL_RECORDS
    assert recs[0].tool_use_id == "t20"


# --- subagent_stop -----------------------------------------------------------

def test_subagent_stop_marks_the_registry_and_dedups():
    from core.session.session_state import get_subagent_registry, cleanup_session_permission_state
    sid = "s-sub"
    try:
        reg = get_subagent_registry(sid)
        reg.register_spawn("agent-1", "tuid-1")
        assert se.subagent_stop(sid, "agent-1", "researcher") is True
        assert se.subagent_stop(sid, "agent-1", "researcher") is False
        # A stop that raced its spawn is parked, not lost.
        assert se.subagent_stop(sid, "agent-2", "") is False
    finally:
        cleanup_session_permission_state(sid)


# --- drive_turns: the proxy-driven delivery -----------------------------------

async def _events(gen):
    return [ev async for ev in gen]


@pytest.mark.asyncio
async def test_drive_turns_runs_the_reason_as_a_follow_up_turn_and_hides_the_inner_done():
    prompts: list[str] = []
    verdicts = iter([se.continue_with("also add tests"), se.STOP])

    async def handler(ctx):
        return next(verdicts, se.STOP)
    se.register_turn_end_handler(handler)

    async def run_turn(text):
        prompts.append(text)
        yield CommonEvent(type=TEXT, data={"content": f"answer to {text}"})
        yield CommonEvent(type=DONE, data={})

    out = await _events(se.drive_turns("s-drive", "claude", "do it", run_turn))
    assert prompts == ["do it", "also add tests"]
    assert [e.type for e in out] == [TEXT, TEXT, DONE]
    assert se.rounds("s-drive") == 0


@pytest.mark.asyncio
async def test_drive_turns_without_a_verdict_is_one_turn():
    async def run_turn(text):
        yield CommonEvent(type=TEXT, data={"content": text})
        yield CommonEvent(type=DONE, data={})
    out = await _events(se.drive_turns("s-drive-stop", "codex", "hi", run_turn))
    assert [e.type for e in out] == [TEXT, DONE]


@pytest.mark.asyncio
async def test_drive_turns_respects_the_round_cap():
    se.register_turn_end_handler(_continue("again"))
    prompts: list[str] = []

    async def run_turn(text):
        prompts.append(text)
        yield CommonEvent(type=DONE, data={})
    out = await _events(se.drive_turns("s-drive", "direct", "go", run_turn))
    assert len(prompts) == se.MAX_ROUNDS + 1
    assert [e.type for e in out] == [DONE]


@pytest.mark.asyncio
async def test_drive_turns_starts_a_user_turn_at_round_zero():
    """A fix round that never reached its turn_end (a hard Stop) leaves a
    count behind; the next user turn does not inherit it."""
    seen: list[int] = []

    async def handler(ctx):
        seen.append(ctx.rounds)
        return se.STOP
    se.register_turn_end_handler(handler)
    se._rounds["s-drive"] = se.MAX_ROUNDS

    async def run_turn(text):
        yield CommonEvent(type=DONE, data={})
    await _events(se.drive_turns("s-drive", "claude", "go", run_turn))
    assert seen == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [
    [CommonEvent(type=TEXT, data={"content": "x"}),
     CommonEvent(type=ERROR, data={"message": "rate limited"}),
     CommonEvent(type=DONE, data={})],
    [CommonEvent(type=TEXT, data={"content": "x"})],
], ids=["engine-error", "no-done"])
async def test_a_failed_turn_is_neither_judged_nor_continued(turn):
    """The pump stops reading at an engine error: a fix turn after it would
    run where nobody sees it. A turn with no end is not judged either."""
    calls: list[int] = []

    async def handler(ctx):
        calls.append(ctx.rounds)
        return se.continue_with("fix it")
    se.register_turn_end_handler(handler)
    prompts: list[str] = []

    async def run_turn(text):
        prompts.append(text)
        for ev in turn:
            yield ev
    out = await _events(se.drive_turns("s-drive", "codex", "go", run_turn))
    assert prompts == ["go"] and calls == []
    assert [e.type for e in out] == [e.type for e in turn]
    assert se.rounds("s-drive") == 0


@pytest.mark.asyncio
async def test_a_turn_the_person_cut_is_not_judged_and_its_records_carry():
    """A message queued behind the turn, or a Stop: the turn is not judged
    nor continued, and what it changed is judged with the next turn."""
    seen: list[tuple[str, ...]] = []

    async def handler(ctx):
        seen.append(tuple(r.tool_name for r in se.tool_records(ctx.session_id)))
        return se.STOP
    se.register_turn_end_handler(handler)

    async def cut_turn(text):
        se.post_tool("s-cut", "Write", tool_input={"file_path": "/w/a.py"})
        se.note_user_message("s-cut")
        yield CommonEvent(type=DONE, data={})
    out = await _events(se.drive_turns("s-cut", "claude", "go", cut_turn))
    assert [e.type for e in out] == [DONE] and seen == []

    async def next_turn(text):
        se.post_tool("s-cut", "Edit", tool_input={"file_path": "/w/b.py"})
        yield CommonEvent(type=DONE, data={})
    await _events(se.drive_turns("s-cut", "claude", "and then", next_turn))
    assert seen == [("Write", "Edit")]
    assert se.tool_records("s-cut") == []


@pytest.mark.asyncio
async def test_a_person_cutting_in_during_the_evaluation_stops_the_fix_round():
    prompts: list[str] = []

    async def handler(ctx):
        se.note_user_message("s-cut")     # typed while the judge ran
        return se.continue_with("fix it")
    se.register_turn_end_handler(handler)

    async def run_turn(text):
        prompts.append(text)
        yield CommonEvent(type=DONE, data={})
    out = await _events(se.drive_turns("s-cut", "claude", "go", run_turn))
    assert prompts == ["go"] and [e.type for e in out] == [DONE]
    assert se.rounds("s-cut") == 0
