"""A paired Linux machine on this host."""
from __future__ import annotations

import os
import subprocess
import time

from lib.chat import TurnFailed
from lib.scenario import CheckFailed, check, scenario
from run import rig_home

from scenarios.s20_turns import _headless_turn, _terminal_turn


def run_detached_install(command: str, log: str) -> None:
    """Run the command the dashboard shows, as a person pastes it, off the runner's own session."""
    with open(log, "w") as out:
        done = subprocess.run(["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                              timeout=900, check=False)
    if done.returncode != 0:
        raise OSError(f"the pairing command exited {done.returncode}: see {log}")


def pair_linux(ctx, *, rewrite_for_plain_http: bool = False) -> str:
    ctx.require_rig()
    owner = ctx.owner
    made = owner.ok("POST", "/v1/admin/remote-machines/pair", {"name": "rig-linux", "allow_full_fs": False})
    ctx.state["machine_id"] = made["machine_id"]
    command = made["install_commands"]["linux"]
    log = str(ctx.run_dir / "pair-linux.log")
    if rewrite_for_plain_http:
        # 1.9.1 always prints https and wss, whatever the public address says. The
        # bootstrap carries the machine's secret: it goes from the pipe to bash and
        # never lands in a file.
        check(command.endswith(")"), f"the printed command has the shape it had: {command[:80]}")
        command = command.replace("https://", "http://", 1)[:-1] + " | sed 's#wss://#ws://#')"
    run_detached_install(command, log)

    def online():
        row = owner.ok("GET", f"/v1/admin/remote-machines/{made['machine_id']}")
        return row if row.get("status") == "online" else None
    row = owner.wait_until("the machine online", online, timeout=120, every=3)
    # What the wipe reads before it touches a satellite: this one is the rig's.
    (rig_home() / ".oto-dock" / ".otodock-rig-satellite").write_text(made["machine_id"] + "\n")
    return f"satellite {row.get('satellite_version')}"


UNIT = "oto-dock-satellite"
# A running machine reconnects by itself after the platform restarts, its tries up to
# 30 s apart: a row on it waits this long for it, a bound a person would accept.
BACK_BY_ITSELF_S = 120


def _status(ctx, machine_id: str) -> str:
    status, row = ctx.owner.call("GET", f"/v1/admin/remote-machines/{machine_id}", timeout=30)
    if status == 404:
        return "gone"
    return str((row or {}).get("status", "")) if status == 200 else f"answer {status}"


def _installed() -> bool:
    """The rig's satellite is on this host: a machine the platform removed uninstalls
    itself, its folder and its unit with it."""
    home = rig_home()
    return ((home / ".oto-dock" / "satellite").is_dir()
            and (home / ".config" / "systemd" / "user" / f"{UNIT}.service").is_file())


def _unit_active() -> bool:
    done = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True, timeout=30,
                          check=False)
    return done.stdout.strip() == "active"


def machine_ready(ctx) -> str:
    """The paired machine online before a row that runs on it; what the runner did.

    A machine whose program runs is waited for, and one that does not come back by
    itself within ``BACK_BY_ITSELF_S`` fails the row: that is the product's to fix. One
    the rig took down (a stop, or a restore onto a new host's empty database, which
    makes a machine uninstall itself) is started again or paired again, and the runner
    log says so. In the upgrade layer a machine is never paired again: its rows are
    about the machine paired under the previous release. With the injection that stops
    it on purpose, nothing is started."""
    ctx.require_rig()
    machine_id = ctx.state.get("machine_id", "")
    check(bool(machine_id), "no machine was paired on this install")
    if _status(ctx, machine_id) == "online":
        return ""
    if os.environ.get("RIG_SATELLITE_DOWN"):
        raise CheckFailed(f"{UNIT} is stopped on purpose for this run: the machine is offline")
    did = ""
    if not _installed():
        check(ctx.layer != "upgrade", "the machine paired under the previous release uninstalled itself")
        if _status(ctx, machine_id) != "gone":
            ctx.owner.ok("DELETE", f"/v1/admin/remote-machines/{machine_id}", timeout=60)
        did = f"the machine had uninstalled itself: paired again ({pair_linux(ctx)})"
    elif not _unit_active():
        subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True, text=True,
                       timeout=ctx.machinery(60), check=False)
        did = f"{UNIT} was stopped: started again"
    try:
        ctx.owner.wait_until("the machine online before the row", lambda: _status(ctx, ctx.state["machine_id"]) == "online",
                             timeout=BACK_BY_ITSELF_S, every=3)
    except TimeoutError as exc:
        raise CheckFailed(f"{exc}: the machine did not come back online by itself"
                          + (f" after the rig's own repair ({did})" if did else "")) from exc
    if did:
        print(f"    {did}", flush=True)
    return did


# What a machine short of memory answers a new chat (``concurrency_paired``): a row
# after one that filled it waits for its next report.
LOW_MEMORY = "This computer is low on memory. Close something on it or try again."
TAKES_A_CHAT_S = 180


def when_the_machine_takes_it(ctx, open_chat):
    """``open_chat()`` once the machine takes a chat: a refusal for its memory is tried
    again, every 10 s within ``TAKES_A_CHAT_S`` (the rig's own bound), since the row
    before may have filled it and its last memory report is older than the chat. The
    chat and how long it waited; any other refusal at once."""
    started = time.monotonic()
    while True:
        try:
            return open_chat(), time.monotonic() - started
        except TurnFailed as refused:
            if LOW_MEMORY not in str(refused.frame.get("error", "")):
                raise
            if time.monotonic() - started >= ctx.machinery(TAKES_A_CHAT_S):
                raise CheckFailed(f"the machine refused a chat for its memory for {time.monotonic() - started:.0f} s: "
                                  f"{LOW_MEMORY}") from refused
            time.sleep(10)


@scenario("machine-pair", needs=("engines",), needs_rig=True)
def machine_pair(ctx):
    """The command the dashboard shows pairs this host, as printed, over plain HTTP on a LAN."""
    return pair_linux(ctx)


@scenario("machine-turn-claude", needs=("machine-pair",), needs_rig=True, machine=True)
def machine_turn_claude(ctx):
    """A headless turn on Claude Code on the machine."""
    _headless_turn(ctx, "claude", target=ctx.state["machine_id"])


@scenario("machine-turn-codex", needs=("machine-pair",), needs_rig=True, machine=True)
def machine_turn_codex(ctx):
    """A headless turn on Codex on the machine."""
    _headless_turn(ctx, "codex", target=ctx.state["machine_id"])


@scenario("machine-terminal-claude", needs=("machine-pair",), needs_rig=True, machine=True)
def machine_terminal_claude(ctx):
    """A terminal chat on Claude Code on the machine."""
    _terminal_turn(ctx, "claude", target=ctx.state["machine_id"])


@scenario("machine-terminal-codex", needs=("machine-pair",), needs_rig=True, machine=True)
def machine_terminal_codex(ctx):
    """A terminal chat on Codex on the machine."""
    _terminal_turn(ctx, "codex", target=ctx.state["machine_id"])
