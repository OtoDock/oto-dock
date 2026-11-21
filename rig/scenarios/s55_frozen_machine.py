"""A paired machine stops answering: the platform's own work keeps its times, and the
machine catches up when it runs again."""
from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from lib.chat import script
from lib.scenario import check, scenario
from run import ENGINES, rig_home

DIRECT, DIRECT_MODEL = ENGINES["direct"]
CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
# What a person may wait beside a machine that answers nothing. Much longer than
# these runs take here (under a second), far below what a wait on the machine
# costs (about 29 s for this step).
TURN_BOUND_S = 10.0
SAVE_BOUND_S = 5.0
# A silent machine reads offline within 90 s (``_SILENT_DROP_S``, 75 s, and
# ``_MONITOR_TICK_S``, 15 s); this leaves room.
OFFLINE_WITHIN_S = 120.0


def _machine_process(ctx) -> int:
    """The process of this host's paired machine, read from its own unit, and only the
    machine this run paired."""
    ctx.require_rig()
    marker = rig_home() / ".oto-dock" / ".otodock-rig-satellite"
    check(marker.read_text().strip() == ctx.state["machine_id"], "the marker names another machine")
    shown = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "oto-dock-satellite"],
                           capture_output=True, text=True, timeout=20, check=False)
    pid = int(shown.stdout.strip() or 0)
    check(pid > 0, f"the machine's unit names no process: {shown.stderr[-200:]}")
    return pid


def _stopped(pid: int) -> bool:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0] == "T"


def _on_machine(ctx, username: str, rel: str) -> Path:
    return rig_home() / ".oto-dock" / "agents" / ctx.agent / "users" / username / rel


@scenario("frozen-machine", needs=("machine-pair", "turn-direct"), needs_rig=True, machine=True)
def frozen_machine(ctx, broken: bool = False):
    """While the paired machine is stopped: a local chat's turn with tool calls and a
    workspace save keep their times, also once it reads offline. When it runs again it
    is online, holds what it missed, and its chat answers. Broken: the machine is never
    stopped, as a rig that froze nothing."""
    owner = ctx.owner
    machine_id = ctx.state["machine_id"]
    pid = _machine_process(ctx)
    username = owner.username
    run = uuid.uuid4().hex[:8]

    def status() -> str:
        return owner.ok("GET", f"/v1/admin/remote-machines/{machine_id}").get("status", "")

    # A scenario before this one may have restarted the platform.
    owner.wait_until("the machine online first", lambda: status() == "online", timeout=60, every=2)
    there = ctx.owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text="Say hello.", target=machine_id)
    here = ctx.owner.chat(ctx.agent, engine=DIRECT, model=DIRECT_MODEL, text="Say hello.")
    frozen = False
    try:
        there.wait_done()
        here.wait_done()

        def turn(name: str) -> tuple[str, float]:
            rel = f"workspace/{run}-{name}.txt"
            started = time.monotonic()
            here.send("Write a file. " + script(
                {"tool": "Write", "input": {"file_path": f"/users/{username}/{rel}", "content": name}},
                {"tool": "Read", "input": {"file_path": f"/users/{username}/{rel}"}},
                {"text": "Written."}))
            here.wait_done(timeout=TURN_BOUND_S * 4)
            return rel, time.monotonic() - started

        def save(name: str) -> tuple[str, float]:
            rel = f"workspace/{run}-{name}.txt"
            started = time.monotonic()
            owner.ok("PUT", f"/v1/agents/{ctx.agent}/files/users/{username}/{rel}", {"content": name},
                     timeout=SAVE_BOUND_S * 30)
            return rel, time.monotonic() - started

        if not broken:
            os.kill(pid, signal.SIGSTOP)
            frozen = True
        time.sleep(1)
        check(_stopped(pid), "the machine's process is not stopped")

        missed, took = [], {}
        for name, act in (("turn", turn), ("save", save)):
            rel, took[name] = act(name)
            missed.append(rel)

        def away() -> bool:
            # A tab answers the server's pings all through this wait, longer than
            # their timeout: these two sockets must too, or the server closes them.
            here.next_frame(timeout=0.5)
            there.next_frame(timeout=0.5)
            return status() != "online"

        owner.wait_until("the machine away", away, timeout=OFFLINE_WITHIN_S, every=2)
        for name, act in (("turn-offline", turn), ("save-offline", save)):
            rel, took[name] = act(name)
            missed.append(rel)
        for name, seconds in took.items():
            bound = SAVE_BOUND_S if name.startswith("save") else TURN_BOUND_S
            check(seconds < bound, f"{name} took {seconds:.1f} s beside a stopped machine, the bound is {bound:.0f} s")

        os.kill(pid, signal.SIGCONT)
        frozen = False
        owner.wait_until("the machine online again", lambda: status() == "online", timeout=120, every=2)
        owner.wait_until(
            "the machine holds what it missed",
            lambda: all(_on_machine(ctx, username, rel).is_file() for rel in missed), timeout=120, every=2)
        # Nothing read this socket through the machine's return, so the server's ping
        # may have closed it: the machine's chat is judged on a socket of its own.
        there.close()
        there = ctx.owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, target=machine_id, chat_id=there.chat_id)
        there.send("Say hello again.")
        there.wait_done()
        return ", ".join(f"{name} {seconds:.1f} s" for name, seconds in took.items())
    finally:
        if frozen:
            os.kill(pid, signal.SIGCONT)
        there.close()
        here.close()
