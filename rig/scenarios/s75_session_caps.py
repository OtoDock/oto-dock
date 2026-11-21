"""A command that grows without bound in one chat, on the compute service and on the
paired Linux computer: the machine's memory guard stops it before the machine's
reserve is eaten, the agent gets one sentence in the command's result, nothing of the
command is left, the chat's next command answers, and a second chat on the same
machine answers all through it."""
from __future__ import annotations

import json

from lib.chat import ChatSession, assistant_text, script, tool_rows
from lib.scenario import check, scenario
from lib.tools import host_scores, tool_scores
from run import ENGINES

from scenarios.s70_compute import _signed_in

ENGINE, MODEL = ENGINES["claude"]
SENTENCE = "That command used too much memory and was stopped."
# Its last argument names it in the machine's process list.
MARK = "session-caps-runaway"
# Bytes multiplied are written, so every page is resident: 64 MB a step, no end.
_RUNAWAY = f'python3 -c "b = []\nwhile True: b.append(b\'x\' * (64 << 20))" {MARK}'


def _results(chat: ChatSession) -> str:
    rows = chat.person.rows(chat.chat_id)
    return json.dumps(tool_rows(rows, "Bash"))


def _left(ctx, target: str) -> list[str]:
    """The runaway's processes still on the machine: the compute service's container,
    or this host, where the paired computer's sessions run."""
    found = tool_scores(ctx, "otodock-compute", MARK) if not target else host_scores(ctx, MARK)
    return [command for _, command in found]


def _runaway_case(ctx, target: str, broken: bool) -> str:
    other = ChatSession(ctx.owner, agent=ctx.agent)
    runaway = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        other.open(engine=ENGINE, model=MODEL, target=target, text="Say ready. " + script({"text": "Ready."}))
        other.wait_done()
        runaway.open(engine=ENGINE, model=MODEL, target=target, text="Grow. " + script(
            {"tool": "shell", "input": {"command": _RUNAWAY}}, {"text": "It was stopped."}))
        # The second chat answers while the command grows and is stopped.
        other.send("Say still here. " + script({"text": "Still here."}))
        other.wait_done(timeout=120)
        check("Still here." in assistant_text(other.person.rows(other.chat_id)),
              "the second chat did not answer while the command ran")
        runaway.wait_done(timeout=240)
        stopped = SENTENCE in _results(runaway)
        check(stopped != broken, f"the command's result: {_results(runaway)[-400:]}")
        left = _left(ctx, target)
        check(not left, f"the command's processes are still there: {left[:3]}")
        runaway.send("Again. " + script({"tool": "shell", "input": {"command": "echo after-the-stop"}},
                                        {"text": "Done."}))
        runaway.wait_done(timeout=120)
        check("after-the-stop" in _results(runaway), "the next command did not answer")
    finally:
        other.close()
        runaway.close()
    return "the guard stopped the runaway command; both chats answered"


@scenario("session-caps", needs=("turn-claude",), needs_rig=True)
def session_caps(ctx, broken: bool = False):
    """On the compute service the guard stops the runaway command alone: its result
    names the stop, nothing of it is left, the same chat's next command answers, a
    second chat answers while it runs. Broken: the result is expected to hold no such
    sentence, as on a machine with no guard."""
    _signed_in(ctx)
    return _runaway_case(ctx, "", broken)


@scenario("session-caps-machine", needs=("machine-turn-claude",), needs_rig=True, machine=True)
def session_caps_machine(ctx, broken: bool = False):
    """The same on the paired Linux computer, whose guard watches its whole memory.
    Broken: the result is expected to hold no such sentence."""
    return _runaway_case(ctx, ctx.state["machine_id"], broken)


# A folder no session has, inside its own working folder: the command runs, which a
# path outside the session's reach would not (the platform refuses that one before
# it runs), and fails on its own.
MISSING_FOLDER = "rig-no-such-folder"
NO_SUCH = "No such file"


def _failed_steps(ctx, target: str, broken: bool) -> str:
    """A Claude Code chat whose command and read both run and fail: each step's row holds
    the error the agent read. Claude Code reports a failed step on its own hook event,
    so a platform that listened to the other alone shows an empty result."""
    from scenarios.s50_machine import when_the_machine_takes_it
    missing = f"/users/{ctx.owner.username}/workspace/rig-no-such-file.txt"
    text = "Look. " + script({"tool": "shell", "input": {"command": f"ls {MISSING_FOLDER}"}},
                             {"tool": "Read", "input": {"file_path": missing}},
                             {"text": "Neither is there."})
    chat, waited = when_the_machine_takes_it(ctx, lambda: ctx.owner.chat(
        ctx.agent, engine=ENGINE, model=MODEL, target=target, text=text))
    try:
        chat.wait_done(timeout=240)
        rows = chat.rows()
    finally:
        chat.close()
    shell, read = tool_rows(rows, "Bash"), tool_rows(rows, "Read")
    check(shell and read, f"the chat holds {len(shell)} command row(s) and {len(read)} read row(s)")
    said = {"the command": str(shell[-1].get("tool_result") or ""), "the read": str(read[-1].get("tool_result") or "")}
    if broken:
        check(not any(said.values()), f"a failed step's result is expected empty: {said}")
        return "both results empty, as the broken form expects"
    check(NO_SUCH in said["the command"], f"the command's result: {said['the command'][:300]!r}")
    check(said["the read"].strip(), "the read's result is empty")
    check(shell[-1].get("is_error") and read[-1].get("is_error"),
          f"a failed step is not marked as one: {shell[-1].get('is_error')}, {read[-1].get('is_error')}")
    # Said only when the machine refused it first: a chat opened at once waits a moment too.
    took = f", after the machine took a chat again in {waited:.0f} s" if waited >= 10 else ""
    return (f"the command said {said['the command'].strip()[:80]!r}, the read said "
            f"{said['the read'].strip()[:80]!r}{took}")


@scenario("failed-step-result", needs=("turn-claude",), needs_rig=True)
def failed_step_result(ctx, broken: bool = False):
    """On the compute service a Claude Code chat's failed command and failed read each
    show the error in the chat. Broken: both results are expected empty."""
    return _failed_steps(ctx, "", broken)


@scenario("failed-step-result-machine", needs=("machine-turn-claude",), needs_rig=True, machine=True)
def failed_step_result_machine(ctx, broken: bool = False):
    """The same on the paired Linux computer. Broken: both results are expected empty."""
    return _failed_steps(ctx, ctx.state["machine_id"], broken)
