"""A session on a paired machine across a restart and an outage: its turn ends on its
own, once, and nothing beside the machine continues it."""
from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid

from lib.chat import assistant_text, script
from lib.scenario import check, scenario
from run import ENGINES

from scenarios.s55_continuity import (
    CONTINUE_WAIT_S,
    SECOND_RESULT_S,
    _continued,
    _cut_cards,
    _held_call,
    _stop_and_start,
)
from scenarios.s55_frozen_machine import OFFLINE_WITHIN_S, _machine_process, _stopped

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
CODEX, CODEX_MODEL = ENGINES["codex"]
# The platform's reconnect window for a machine that went away
# (``satellite_connection._GRACE_WINDOW_S``): past it the waiting goes on and
# nothing is ended.
WINDOW_S = 90.0
# The model's answer comes this long after the call reached it: long enough for
# the restart or the freeze to land while the turn is in flight.
ANSWER_AFTER_S = 20
# The calls of one turn start within this of each other.
TURN_SPREAD_S = 5.0


def _turn_on_machine(ctx, *, engine: str, model: str, terminal: bool = False):
    """A chat on the machine with a turn whose call has reached the model server:
    (the chat, its tag, its answer). The socket is closed: the rows are read over HTTP."""
    tag = f"ms-{uuid.uuid4().hex[:8]}"
    answer = f"The machine's answer {tag}."
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, target=ctx.state["machine_id"],
                          terminal=terminal, text="Hold. " + script({"sleep": ANSWER_AFTER_S, "text": answer},
                                                                    tag=tag))
    if terminal:
        chat.attach_terminal()
    _held_call(ctx, tag)
    chat.close()
    return chat, tag, answer


def _end_the_engine(ctx, session_id: str) -> None:
    """The machine's engine of this session ends (its command line names the id)."""
    ctx.require_rig()
    subprocess.run(["pkill", "-f", session_id], check=False, timeout=30)


def _restart_the_machine(ctx) -> None:
    """The machine's satellite starts again, which ends every engine it ran."""
    ctx.require_rig()
    done = subprocess.run(["systemctl", "--user", "restart", "oto-dock-satellite"],
                          capture_output=True, text=True, timeout=120, check=False)
    check(done.returncode == 0, f"the machine did not start again: {done.stderr[-200:]}")


def _answered_once(ctx, chat, tag: str, answer: str) -> None:
    """The turn's own answer lands once, with no card and nothing continued beside it."""
    def answered():
        rows = ctx.owner.rows(chat.chat_id)
        return rows if answer in assistant_text(rows) else None
    ctx.owner.wait_until("the machine's answer", answered, timeout=CONTINUE_WAIT_S, every=3)
    time.sleep(SECOND_RESULT_S)
    rows = ctx.owner.rows(chat.chat_id)
    check(not _continued(rows), f"the platform continued a turn its machine held: {_continued(rows)}")
    check(not _cut_cards(rows), f"a card for a turn its machine held: {_cut_cards(rows)}")
    check(assistant_text(rows).count(answer) == 1, f"the answer once: {assistant_text(rows)[:200]!r}")
    # A terminal's turn opens with two calls at once (Claude Code's own side
    # call): one turn is calls that all start together, none after the cut.
    made = sorted(float(c.get("t") or 0) for c in ctx.model_calls() if c.get("tag") == tag)
    check(made and made[-1] - made[0] < TURN_SPREAD_S,
          f"one turn of model calls: {len(made)} spread over {made[-1] - made[0] if made else 0:.0f} s")


def _away_past_the_window(ctx, pid: int, *, during=None) -> None:
    """The machine's process stopped until the platform reads it away and the
    reconnect window has run out, then running again. ``during`` runs as soon as
    it is stopped, before the turn's answer comes."""
    owner = ctx.owner
    machine_id = ctx.state["machine_id"]

    def status() -> str:
        return owner.ok("GET", f"/v1/admin/remote-machines/{machine_id}").get("status", "")

    os.kill(pid, signal.SIGSTOP)
    try:
        time.sleep(1)
        check(_stopped(pid), "the machine's process is not stopped")
        if during is not None:
            during()
        owner.wait_until("the machine away", lambda: status() != "online", timeout=OFFLINE_WITHIN_S, every=2)
        time.sleep(WINDOW_S + 10)
    finally:
        os.kill(pid, signal.SIGCONT)
    owner.wait_until("the machine online again", lambda: status() == "online", timeout=120, every=2)


@scenario("machine-restart-codex", needs=("machine-turn-codex",), needs_rig=True, machine=True)
def machine_restart_codex(ctx, broken: bool = False):
    """A restart in mid-turn with a Codex turn on a paired machine: the machine keeps
    it, its one answer lands, and nothing continues it beside the machine. Broken: the
    machine starts again in the downtime, so its report lists nothing and the platform
    continues the chat, which the checks refuse."""
    chat, tag, answer = _turn_on_machine(ctx, engine=CODEX, model=CODEX_MODEL)
    _stop_and_start(ctx, downtime=(lambda: _restart_the_machine(ctx)) if broken else None)
    _answered_once(ctx, chat, tag, answer)
    return "the machine kept the Codex turn"


@scenario("machine-terminal-restart", needs=("machine-terminal-claude",), needs_rig=True, machine=True)
def machine_terminal_restart(ctx, broken: bool = False):
    """A restart in mid-turn with a terminal on a paired machine: the platform takes the
    terminal back, its one answer lands as a row, and nothing continues it beside the
    machine. Broken: the terminal's engine ends in the downtime, so no answer lands."""
    chat, tag, answer = _turn_on_machine(ctx, engine=CLAUDE, model=CLAUDE_MODEL, terminal=True)
    sid = chat.session_id
    _stop_and_start(ctx, downtime=(lambda: _end_the_engine(ctx, sid)) if broken else None)
    _answered_once(ctx, chat, tag, answer)
    return "the platform took the terminal back"


@scenario("machine-outage-headless", needs=("machine-turn-claude",), needs_rig=True, machine=True)
def machine_outage_headless(ctx, broken: bool = False):
    """The machine away past the reconnect window with a headless turn in flight, then
    back: the turn ends on its own with its one answer and no card. Broken: the engine
    ends while the machine is away, so no answer lands."""
    pid = _machine_process(ctx)
    chat, tag, answer = _turn_on_machine(ctx, engine=CLAUDE, model=CLAUDE_MODEL)
    sid = chat.session_id
    _away_past_the_window(ctx, pid, during=(lambda: _end_the_engine(ctx, sid)) if broken else None)
    _answered_once(ctx, chat, tag, answer)
    return "the turn outlived the window"


@scenario("machine-outage-terminal", needs=("machine-terminal-claude",), needs_rig=True, machine=True)
def machine_outage_terminal(ctx, broken: bool = False):
    """The machine away past the reconnect window with a terminal's turn in flight, then
    back: the terminal is not ended at the window, its one answer lands as a row and no
    card. Broken: the terminal's engine ends while the machine is away."""
    pid = _machine_process(ctx)
    chat, tag, answer = _turn_on_machine(ctx, engine=CLAUDE, model=CLAUDE_MODEL, terminal=True)
    sid = chat.session_id
    _away_past_the_window(ctx, pid, during=(lambda: _end_the_engine(ctx, sid)) if broken else None)
    _answered_once(ctx, chat, tag, answer)
    return "the terminal outlived the window"
