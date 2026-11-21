"""A compute machine on another server: added by an admin in person with one
command, paired and started by that command, taking new chats when the
install's own compute machine is full, keeping a chat where it started,
syncing its files back, and removing itself from its server when removed.

The rig's Linux VM stands for both hosts: the add's command runs on the VM
with sudo, as an admin pastes it on a server, and starts the platform's image
(the install's own, already on the VM under its public name) under the unit
otodock-remote-compute.service. The install's own compute service is capped
low for the claim, the documented way (OTODOCK_COMPUTE_MEM_LIMIT and a new
container: a live container shrunk in place writes its memory out through the
disk), so the balancer gives the new chat away.

The chat runs on Codex: its model is the install's local endpoint, which the
platform hands every session wherever it runs. The rig's Claude Code reaches the
scripted server only through the wrapper the install's own compose file mounts,
and the add's command starts its container as a person gets it, without one."""
from __future__ import annotations

import contextlib
import json
import subprocess
import time

from lib.chat import ChatSession, tool_rows, tool_said
from lib.scenario import CheckFailed, check, scenario
from run import engine_of

from claims.c10_claims import _ask

HOST = "otodock-remote-compute"
TOOL = f"/opt/{HOST}/{HOST}"
# The claim's file, as the session names it and where each copy keeps it: the
# platform's agents folder, and the other host's own copy (its container's
# ``OTODOCK_COMPUTE_AGENTS_DIR``).
HOST_AGENTS = "/opt/otodock/compute/agents"


def claim_file(username: str) -> str:
    """The claim's file below the agent's folder: the person's own workspace. The
    install's first agent is Personal only, and a session of such an agent has no
    shared ``/workspace`` on any host (``SandboxConfig.mount_shared``)."""
    return f"users/{username}/workspace/remote-claim.txt"


def write_step(username: str) -> dict:
    """The command that writes it, by the path the session sees."""
    return {"tool": "shell",
            "input": {"command": f"printf 'from the other host' > /{claim_file(username)}"}}


def _on_vm(ctx, command: list[str], timeout: float = 900.0) -> subprocess.CompletedProcess:
    ctx.require_rig()
    return subprocess.run(command, capture_output=True, text=True, timeout=ctx.machinery(timeout), check=False)


CAP_KEY = "OTODOCK_COMPUTE_MEM_LIMIT"


def _cap(ctx, value: str | None) -> None:
    """The install's own compute machine started again under ``value`` (1g), or as its
    compose file says with None, and signed in."""
    from scenarios.s70_compute import _set_env, _signed_in
    _set_env(ctx, CAP_KEY, value)
    done = ctx.compose("up", "-d", "--no-deps", "--force-recreate", "otodock-compute", timeout=600)
    check(done.returncode == 0, f"the compute container did not start again: {done.stderr[-200:]}")
    _signed_in(ctx)


def _on_the_host(ctx) -> str:
    """Whether the other host's own copy holds the claim's file: what a session there
    wrote, before the platform got it."""
    seen = ctx.docker("exec", HOST, "sh", "-c", f"cat {HOST_AGENTS}/{ctx.agent}/{claim_file(ctx.owner.username)}")
    if seen.returncode == 0:
        return f"its copy holds the file: {seen.stdout.strip()[:80]!r}"
    return f"its copy has no such file: {(seen.stderr or seen.stdout).strip()[-200:]!r}"


def _keep_the_hosts_evidence(ctx, started: float, chat) -> None:
    """A failed claim's own pieces, kept before its cleanup removes the other host: the
    chat's rows, and the other host's container log and unit journal."""
    if not getattr(ctx, "is_rig", False):
        return
    from run import write_evidence
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started - 5))
    with contextlib.suppress(Exception):
        if chat is not None and chat.chat_id:
            write_evidence(ctx, NAME, "rows", json.dumps(chat.rows(), indent=1))
    with contextlib.suppress(Exception):
        done = ctx.docker("logs", "--timestamps", "--since", stamp, HOST, timeout=120)
        write_evidence(ctx, NAME, "host-container", done.stdout + done.stderr)
    with contextlib.suppress(Exception):
        done = _on_vm(ctx, ["sudo", "-n", "journalctl", "-u", f"{HOST}.service", "--since", f"@{int(started - 5)}",
                            "--no-pager", "-o", "short-iso"], timeout=120)
        write_evidence(ctx, NAME, "host-unit", done.stdout + done.stderr)


def _left_on_the_server(ctx) -> list[str]:
    """What of the machine is still on the VM: its unit, folders, container,
    network and volumes."""
    left = [path for path in (f"/etc/systemd/system/{HOST}.service", f"/var/lib/{HOST}", f"/opt/{HOST}")
            if _on_vm(ctx, ["sudo", "-n", "test", "-e", path]).returncode == 0]
    for kind in ("ps -aq", "network ls -q", "volume ls -q"):
        found = ctx.docker(*kind.split(), "--filter", "label=io.otodock.remote-compute")
        left += [f"{kind.split()[0]} {x}" for x in found.stdout.split()]
    return left


NAME = "claim-compute-on-another-host"


@scenario(NAME, layer="claims", needs=("turn-codex",), needs_rig=True)
def compute_on_another_host(ctx, broken: bool = False):
    """A new chat lands on the compute machine on another server while the install's
    own is full, a file it writes comes back, its second message stays there, and its
    removal sends the chat on with the digest and leaves nothing of it on its server.
    Broken: the chat is expected on the install's own machine."""
    ctx.require_rig()
    engine, model = engine_of(ctx, "codex")
    # Before the add: a failed check after it would leave the other host's unit there.
    check(not ctx.env_value(CAP_KEY), f"the install already sets {CAP_KEY}")
    chat = None
    capped = False
    machine = ""
    started = time.time()
    try:
        added = ctx.owner.ok("POST", "/v1/admin/remote-machines/compute", {"name": "Claim rack"})
        machine = added["id"]
        check("machine_secret" not in json.dumps(added), "the add's answer carries a secret")
        paired = _on_vm(ctx, ["bash", "-c", added["install_command"]])
        check(paired.returncode == 0, f"the command failed: {(paired.stderr or paired.stdout)[-400:]}")
        ctx.owner.wait_until("the machine on another server signs in", lambda: any(
            m["id"] == machine and m["status"] == "online"
            for m in ctx.owner.ok("GET", "/v1/admin/remote-machines")["machines"]), timeout=600)
        capped = True
        _cap(ctx, "1g")
        chat = ChatSession(ctx.owner, agent=ctx.agent)
        chat.open(engine=engine, model=model, text=_ask(
            ctx, f"Write the words 'from the other host' to the file /{claim_file(ctx.owner.username)}, "
            "then reply with one word.", write_step(ctx.owner.username), {"text": "Written."}))
        chat.wait_done(timeout=900)
        placed = ctx.sql(f"SELECT compute_machine FROM chats WHERE id = '{chat.chat_id}'")
        expected = "" if broken else machine
        check(placed == expected, f"the chat started on {placed or 'the install'}, not {expected or 'the install'}")
        steps = tool_rows(chat.rows(), "")
        check(steps and not steps[-1].get("is_error"),
              tool_said(steps[-1], "the command on the other host") if steps else "the chat holds no command")
        try:
            ctx.owner.wait_until("the file comes back", lambda: "from the other host" in (
                ctx.proxy_exec(f"cat /opt/otodock/agents/{ctx.agent}/{claim_file(ctx.owner.username)}").stdout),
                timeout=120)
        except TimeoutError as late:
            raise CheckFailed(f"{late}. On the other host {_on_the_host(ctx)}. "
                              f"{tool_said(steps[-1], 'The command')}") from late
        chat.send("Again.")
        chat.wait_done()
        check(ctx.sql(f"SELECT compute_machine FROM chats WHERE id = '{chat.chat_id}'") == machine,
              "the second message left the machine")
        ctx.owner.ok("DELETE", f"/v1/admin/remote-machines/{machine}")
        seed = ctx.sql(f"SELECT pending_history_seed FROM chats WHERE id = '{chat.chat_id}'")
        check(seed.startswith("machine_removed:"), f"the removed machine's chat carries {seed!r}")
        ctx.owner.wait_until("the server holds nothing of the machine", lambda: not _left_on_the_server(ctx),
                             timeout=300)
    except BaseException:
        # Before the other host goes: its container leaves with its log.
        _keep_the_hosts_evidence(ctx, started, chat)
        raise
    finally:
        if chat is not None:
            chat.close()
        # The platform's row first, whatever happened: a compute machine it still
        # lists holds every later start of the platform until it signs in.
        if machine:
            with contextlib.suppress(Exception):
                ctx.owner.call("DELETE", f"/v1/admin/remote-machines/{machine}", timeout=60)
        if _on_vm(ctx, ["sudo", "-n", "test", "-e", TOOL]).returncode == 0:
            _on_vm(ctx, ["sudo", "-n", TOOL, "remove", "--yes"], timeout=300)
        if capped:
            _cap(ctx, None)
        time.sleep(1)
    return (f"a Codex chat ran on the other host, its file in the person's workspace (/{claim_file(ctx.owner.username)}) "
            "came back, its second message stayed there, and its removal left nothing on the host")
