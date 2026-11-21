"""A tool's program starts at the agent's first call to it: a later chat runs only the
tool's stand-in until then, on the compute machine and on a paired machine, on every
engine, and a start the platform cannot make says so in one sentence."""
from __future__ import annotations

import time

from lib.chat import script, tool_rows
from lib.scenario import check, scenario
from lib.tools import (
    MACHINE_FOLDER,
    READ_MEMORY,
    host_scores,
    programs_of,
    stand_ins_of,
    tool_procs,
)
from run import ENGINES

# A bundled stdio tool that starts at its first call (no ``server.starts``) and reads
# without an account.
TOOL = "memory-mcp"
# A session's programs end at its idle bound (the rig sets 60 s) and the reaper's tick.
QUIET_WAIT_S = 240.0


def _lines(ctx, target: str) -> list[str]:
    """The tool's command lines where the chat runs: the compute service, or this host
    (the paired machine, whose sessions are this account's processes)."""
    if not target:
        return tool_procs(ctx, "otodock-compute", TOOL)
    return [command for _, command in host_scores(ctx, TOOL) if MACHINE_FOLDER in command]


def _quiet(ctx, target: str) -> None:
    """No program of the tool runs there: the sessions before this one have ended."""
    ctx.owner.wait_until(f"no {TOOL} program from an earlier session", lambda: not programs_of(_lines(ctx, target), TOOL),
                         timeout=QUIET_WAIT_S, every=5)


def _chat(ctx, word: str, target: str, text: str, *, chat_id: str = ""):
    engine, model = ENGINES[word]
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, target=target, text=text, chat_id=chat_id)
    chat.wait_done()
    return chat


def _first_use(ctx, word: str, *, target: str = "", broken: bool = False) -> str:
    """A chat that saves the tool's answers, then the chat the claim is about: after a
    turn with no call, the tool's stand-in runs and its program does not; the call
    starts the program and answers. Direct LLM starts the tool in its own client, so
    nothing of it runs before the call. Broken: the second chat calls the tool in its
    first turn, as a session that started every program with the chat would show."""
    first = _chat(ctx, word, target, "Read your memory. " + script(READ_MEMORY, {"text": "Read."}))
    first.close()
    _quiet(ctx, target)
    opening = ("Read your memory. " + script(READ_MEMORY, {"text": "Read."})) if broken else \
        ("Say hello. " + script({"text": "Hello."}))
    chat = _chat(ctx, word, target, opening)
    try:
        before = _lines(ctx, target)
        check(not programs_of(before, TOOL), f"{word}: the tool's program runs before the agent called it: {before[:3]}")
        if word != "direct":
            check(stand_ins_of(before), f"{word}: no stand-in of {TOOL} in the session: {before[:3]}")
        chat.send("Read your memory. " + script(READ_MEMORY, {"text": "Read."}))
        chat.wait_done()
        steps = tool_rows(chat.rows(), "memory")
        check(steps and not steps[-1].get("is_error"), f"{word}: the first call did not answer: {str(steps)[-300:]}")
        after = programs_of(_lines(ctx, target), TOOL)
        check(after, f"{word}: the call answered but no program of {TOOL} runs")
    finally:
        chat.close()
    return f"{word}: {len(stand_ins_of(before))} stand-in(s) at rest, {len(after)} program(s) after the call"


@scenario("tools-first-call-claude", needs=("turn-claude",), needs_rig=True)
def tools_first_call_claude(ctx, broken: bool = False):
    """A later Claude Code chat on the compute machine runs no tool program until its
    first call, which starts the program and answers. Broken: the chat calls in its first turn."""
    return _first_use(ctx, "claude", broken=broken)


@scenario("tools-first-call-codex", needs=("turn-codex",), needs_rig=True)
def tools_first_call_codex(ctx, broken: bool = False):
    """The same on Codex. Broken: the chat calls in its first turn."""
    return _first_use(ctx, "codex", broken=broken)


@scenario("tools-first-call-direct", needs=("turn-direct",), needs_rig=True)
def tools_first_call_direct(ctx, broken: bool = False):
    """A Direct LLM chat starts its relayed tool on the compute machine at the first call
    alone. Broken: the chat calls in its first turn."""
    return _first_use(ctx, "direct", broken=broken)


@scenario("tools-first-call-machine", needs=("machine-turn-claude",), needs_rig=True, machine=True)
def tools_first_call_machine(ctx, broken: bool = False):
    """The same for a Claude Code chat on the paired Linux machine. Broken: the chat
    calls in its first turn."""
    return _first_use(ctx, "claude", target=ctx.state["machine_id"], broken=broken)


# What a call answers when its tool's late start finds no room (``concurrency.grow``).
NO_ROOM = "There is not enough free memory to start this tool right now."


def _no_sessions(ctx) -> None:
    """The install's own compute machine runs no session: none is taken back after the
    restart, so the next chat is the empty box's last resort."""
    def empty():
        machines = ctx.owner.ok("GET", "/v1/admin/remote-machines", timeout=10)["machines"]
        own = [m for m in machines if (m.get("compute") or {}).get("own")]
        return bool(own) and own[0]["compute"].get("sessions") == 0
    ctx.owner.wait_until("no session on the compute machine", empty, timeout=QUIET_WAIT_S, every=5)


@scenario("tools-start-refused-full-box", needs=("tools-first-call-claude",), needs_rig=True)
def tools_start_refused_full_box(ctx, broken: bool = False):
    """On a box short of memory, a chat admitted as the empty box's last resort calls a tool
    whose answers are saved: the late start is refused with one sentence, its program does
    not start, and the chat goes on. Broken: the box has room, so the call starts the tool."""
    from scenarios.s55_continuity import _healthy
    from scenarios.s70_compute import _set_env, _signed_in
    saved = _chat(ctx, "claude", "", "Read your memory. " + script(READ_MEMORY, {"text": "Read."}))
    saved.close()
    _no_sessions(ctx)
    if not broken:
        _set_env(ctx, "SESSION_RESERVE_FLOOR_MB", "100000000")
    chat = None
    try:
        done = ctx.compose("restart", "otodock-proxy", timeout=240)
        check(done.returncode == 0, f"the restart failed: {done.stderr[-300:]}")
        _healthy(ctx)
        _signed_in(ctx)
        chat = _chat(ctx, "claude", "", "Read your memory. " + script(READ_MEMORY, {"text": "Done."}))
        steps = tool_rows(chat.rows(), "memory")
        check(steps, "the call left no row")
        said = str(steps[-1].get("tool_result", ""))
        check(NO_ROOM in said, f"the call on a full box answered: {said[:200]!r}")
        check(not programs_of(_lines(ctx, ""), TOOL), f"{TOOL}'s program started on a full box")
    finally:
        if chat is not None:
            chat.close()
        _set_env(ctx, "SESSION_RESERVE_FLOOR_MB", None)
        ctx.compose("restart", "otodock-proxy", timeout=240)
        _healthy(ctx)
    return f"the late start was refused: {NO_ROOM}"


# How long a relayed tool may outlive what ended its session: the satellite closes it
# when the link drops, with a kill 5 s after the stop.
ORPHAN_WAIT_S = 30.0
# Past the platform's rule for a silent link (75 s) and its 15 s tick.
PAUSED_S = 100


def _direct_tool(ctx, chat_id: str = ""):
    """A Direct LLM chat (a new one, or ``chat_id`` reopened) whose relayed tool has
    answered a call on the compute machine."""
    chat = _chat(ctx, "direct", "", "Read your memory. " + script(READ_MEMORY, {"text": "Read."}), chat_id=chat_id)
    check(programs_of(_lines(ctx, ""), TOOL), "the Direct chat's relayed tool does not run on the compute machine")
    return chat


def _no_orphan(ctx, what: str) -> None:
    ctx.owner.wait_until(f"no relayed tool left after {what}", lambda: not programs_of(_lines(ctx, ""), TOOL),
                         timeout=ORPHAN_WAIT_S, every=2)


def _wait_reading(chat, seconds: float) -> None:
    """Wait, reading the chat's socket meanwhile, so the server's pings are answered."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        chat.next_frame(timeout=1)


@scenario("direct-tools-leave-no-orphan", needs=("tools-first-call-direct",), needs_rig=True)
def direct_tools_leave_no_orphan(ctx, broken: bool = False):
    """A Direct LLM chat's relayed tool on the compute machine ends with what ends its
    session: a proxy restart, a link that falls silent (the compute container paused past
    the platform's rule) and a restart of the compute service each leave no tool process,
    and the chat's next call starts one again. A restarted container ends every process of
    it, so that leg proves the next call more than the end. Broken: the proxy is not
    restarted, so its session keeps the tool."""
    from scenarios.s55_continuity import _healthy, _stop_and_start
    from scenarios.s70_compute import _signed_in
    _signed_in(ctx)
    _quiet(ctx, "")
    chat = _direct_tool(ctx)
    try:
        chat.close()
        if not broken:
            _stop_and_start(ctx)
        _no_orphan(ctx, "a proxy restart")
        _signed_in(ctx)
        chat = _direct_tool(ctx)
        paused = ctx.compose("pause", "otodock-compute", timeout=60)
        check(paused.returncode == 0, f"the compute service was not paused: {paused.stderr[-200:]}")
        try:
            _wait_reading(chat, PAUSED_S)
        finally:
            ctx.compose("unpause", "otodock-compute", timeout=60)
        _no_orphan(ctx, "a silent link")
        _signed_in(ctx)
        chat.close()
        chat = _direct_tool(ctx, chat.chat_id)
        chat.close()
        done = ctx.compose("restart", "otodock-compute", timeout=240)
        check(done.returncode == 0, f"the compute service did not restart: {done.stderr[-200:]}")
        _signed_in(ctx)
        _no_orphan(ctx, "a compute restart")
        chat = _direct_tool(ctx, chat.chat_id)
    finally:
        chat.close()
        _healthy(ctx)
    return "no relayed tool outlived a proxy restart, a silent link or a compute restart; each next call started one"
