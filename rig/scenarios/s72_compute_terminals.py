"""A terminal chat on the compute service across a restart of the proxy alone: the
platform takes the terminal back, its turn's one answer lands, and nothing continues it
beside the compute machine."""
from __future__ import annotations

import uuid

from lib.chat import script
from lib.scenario import check, scenario
from run import ENGINES

from scenarios.s55_continuity import _held_call, _stop_and_start
from scenarios.s56_machine_sessions import ANSWER_AFTER_S, _answered_once
from scenarios.s70_compute import _signed_in

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
CODEX, CODEX_MODEL = ENGINES["codex"]

# Every process inside the compute container whose command line names the session
# ends (the image has no pkill): the broken form's engine gone in the downtime.
_END = ("for p in /proc/[0-9]*; do if tr '\\0' ' ' < $p/cmdline 2>/dev/null | grep -q -- \"$0\"; "
        "then kill ${p#/proc/} 2>/dev/null; fi; done; true")


def _terminal_turn(ctx, *, engine: str, model: str):
    """A terminal chat on the compute service (the platform's own place) whose
    call has reached the model server: (the chat, its tag, its answer)."""
    tag = f"ct-{uuid.uuid4().hex[:8]}"
    answer = f"The compute terminal's answer {tag}."
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, terminal=True,
                          text="Hold. " + script({"sleep": ANSWER_AFTER_S, "text": answer}, tag=tag))
    chat.attach_terminal()
    _held_call(ctx, tag)
    chat.close()
    return chat, tag, answer


def _end_in_compute(ctx, session_id: str) -> None:
    ctx.require_rig()
    done = ctx.compose("exec", "-T", "otodock-compute", "sh", "-c", _END, session_id, timeout=60)
    check(done.returncode == 0, f"the engine did not end: {done.stderr[-200:]}")


def _restart_keeps_the_terminal(ctx, *, engine: str, model: str, broken: bool) -> None:
    _signed_in(ctx)
    started = ctx.compose("ps", "-q", "otodock-compute").stdout.strip()
    chat, tag, answer = _terminal_turn(ctx, engine=engine, model=model)
    sid = chat.session_id
    _stop_and_start(ctx, downtime=(lambda: _end_in_compute(ctx, sid)) if broken else None)
    _answered_once(ctx, chat, tag, answer)
    check(ctx.compose("ps", "-q", "otodock-compute").stdout.strip() == started,
          "the compute service was restarted")


@scenario("compute-terminal-restart", needs=("turn-claude",), needs_rig=True)
def compute_terminal_restart(ctx, broken: bool = False):
    """A restart of the proxy in mid-turn with a Claude Code terminal on the compute
    service: its one answer lands as a row, no card, nothing continued. Broken: the
    terminal's engine ends in the downtime, so no answer lands."""
    _restart_keeps_the_terminal(ctx, engine=CLAUDE, model=CLAUDE_MODEL, broken=broken)
    return "the platform took the compute terminal back"


@scenario("compute-terminal-restart-codex", needs=("turn-codex",), needs_rig=True)
def compute_terminal_restart_codex(ctx, broken: bool = False):
    """The same with a Codex terminal."""
    _restart_keeps_the_terminal(ctx, engine=CODEX, model=CODEX_MODEL, broken=broken)
    return "the platform took the compute Codex terminal back"
