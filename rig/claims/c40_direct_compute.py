"""Direct LLM's tool processes: the model loop stays in the proxy, each stdio tool
runs on the install's compute machine, a call's and a chat's alike."""
from __future__ import annotations

import contextlib
import time

from lib.chat import ChatSession
from lib.scenario import check, scenario
from lib.tools import READ_MEMORY, tool_procs
from run import ENGINES, engine_of

from claims.c10_claims import _ask, _Call, _outside_picks

#: A stdio tool an outside caller may be given, uploaded for the call's claim: the
#: memory tool, like every tool that manages the platform, never reaches a caller.
CALL_PROBE = "rig-call-probe"
CALL_PROBE_MANIFEST = {
    "name": CALL_PROBE, "label": "Rig call probe", "version": "1", "category": "community",
    "description": "The rig's probe of a call's stdio tool: answers one call.",
    "server": {"runtime": "python", "transport": "stdio", "source": "pypi:six@1.17.0",
               "command": "venv/bin/python", "args": ["server.py"], "tool_timeout_sec": 60},
    "credentials": {"type": "none"},
}
CALL_PROBE_TOOL = f"mcp__{CALL_PROBE}__probe"
CALL_PROBE_STEP = {"tool": "probe", "input": {}}


def _memory_procs(ctx, service: str) -> str:
    return "\n".join(tool_procs(ctx, service, "memory-mcp"))


@scenario("claim-direct-tools-on-compute", layer="claims", needs=("turn-direct",), needs_rig=True)
def direct_tools_on_compute(ctx, broken: bool = False):
    """A Direct LLM chat's stdio tool runs in the compute service's container and in no
    other, and answers its call there. Broken: the tool is expected in the proxy's."""
    ctx.require_rig()
    engine, model = engine_of(ctx, "direct")
    chat = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        chat.open(engine=engine, model=model, text=_ask(ctx, "Read your memory with your memory tool, then reply with one word.",
                                                        READ_MEMORY, {"text": "Read."}))
        chat.wait_done()
        expected, other = ("otodock-proxy", "otodock-compute") if broken else ("otodock-compute", "otodock-proxy")
        check(bool(_memory_procs(ctx, expected)), f"no stdio tool process in {expected}")
        check(not _memory_procs(ctx, other), f"a stdio tool process runs in {other}")
    finally:
        chat.close()


def call_probe_server() -> str:
    """The exit probe's server with its one tool answering at once (a call's turn is
    timed), named for this claim."""
    from claims.c30_exit import PROBE_SERVER
    body = 'def probe() -> dict:\n    return {"answered": True}\n\n\n'
    server = PROBE_SERVER[:PROBE_SERVER.index("def probe()")] + body + PROBE_SERVER[PROBE_SERVER.index("def answer("):]
    return server.replace("rig-exit-probe", CALL_PROBE)


@scenario("claim-direct-call-tools-on-compute", layer="claims", needs=("turn-direct",), needs_rig=True)
def direct_call_tools_on_compute(ctx, broken: bool = False):
    """A call on Direct LLM runs its stdio tool on the compute service, and a turn with
    a tool answers within a bound a person on the line can bear. The line's engine is
    the agent's own, so it is Direct LLM for the call, and the tool is one a manager
    may pick for a caller. Broken: a bound of nothing."""
    ctx.require_rig()
    from claims.c30_exit import _enabled_mcps, _install_probe
    owner = ctx.owner
    engine, model = engine_of(ctx, "direct")
    row = owner.ok("GET", f"/v1/agents/{ctx.agent}/info")
    before = {"execution_paths": row.get("execution_paths") or [row.get("execution_path") or ENGINES["claude"][0]],
              "default_model": row.get("default_model") or ""}
    enabled_before = None
    try:
        owner.ok("PATCH", f"/v1/agents/{ctx.agent}", {"execution_paths": [engine], "default_model": model})
        _install_probe(ctx, CALL_PROBE, CALL_PROBE_MANIFEST, call_probe_server())
        enabled_before = _enabled_mcps(ctx)
        owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [*enabled_before, CALL_PROBE]})
        _outside_picks(ctx, [{"mcp": CALL_PROBE, "access": "read_write", "functions": []}])
        call = _Call(ctx)
        try:
            call.warm()
            started = time.monotonic()
            _text, tools = call.say("Call the probe tool once, then say one word.", CALL_PROBE_STEP, {"text": "Done."})
            taken = time.monotonic() - started
            check(tools == [CALL_PROBE_TOOL], f"the call's turn used {tools}")
            check(bool(tool_procs(ctx, "otodock-compute", CALL_PROBE)), "no stdio tool process in the compute service")
            check(not tool_procs(ctx, "otodock-proxy", CALL_PROBE), "a stdio tool process runs in the proxy's container")
            check(taken < (0 if broken else 30), f"the turn took {taken:.1f} s")
        finally:
            call.close()
    finally:
        with contextlib.suppress(Exception):
            _outside_picks(ctx, [])
        with contextlib.suppress(Exception):
            if enabled_before is not None:
                owner.call("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled_before})
            owner.call("DELETE", f"/v1/admin/mcps/{CALL_PROBE}", timeout=120)
        owner.ok("PATCH", f"/v1/agents/{ctx.agent}", before)
    return f"the call's {CALL_PROBE} ran on the compute service, the turn took {taken:.1f} s"


# The shape every session starts in, as ``scripts/image-boot-check.sh`` asks for it: the
# network launcher wrapping one bwrap level with a fresh /proc.
SANDBOX_SHAPE = ("/opt/otodock/proxy/scripts/oto-sandbox-net --block-private -- bwrap --unshare-pid --unshare-ipc "
                 "--unshare-uts --die-with-parent --share-net --unshare-user --uid 1000 --gid 1000 --ro-bind / / "
                 "--dev /dev --proc /proc -- true")


@scenario("claim-proxy-starts-no-sandbox", layer="claims", needs_rig=True)
def proxy_starts_no_sandbox(ctx, broken: bool = False):
    """The proxy's container holds none of the sandbox's privileges: no security option,
    no device, no capability. The session's shape does not start there, and it starts in
    the compute service's container. Of every container on the host, the compute
    service's alone holds the sandbox's relaxations and its network device. Broken: the
    two containers are swapped."""
    ctx.require_rig()
    refused, runs = ("otodock-compute", "otodock-proxy") if broken else ("otodock-proxy", "otodock-compute")
    no = ctx.compose("exec", "-T", refused, "sh", "-c", SANDBOX_SHAPE, timeout=90)
    check(no.returncode != 0, f"the sandbox's shape started in {refused}")
    yes = ctx.compose("exec", "-T", runs, "sh", "-c", SANDBOX_SHAPE, timeout=90)
    check(yes.returncode == 0, f"the sandbox's shape did not start in {runs}: {(yes.stderr or yes.stdout)[-300:]}")
    container = ctx.compose("ps", "-q", "otodock-proxy", timeout=60).stdout.split()
    check(len(container) == 1, f"no single otodock-proxy container: {container}")
    held = ctx.docker("inspect", "-f", "{{.HostConfig.SecurityOpt}}|{{.HostConfig.Devices}}|{{.HostConfig.CapAdd}}|"
                                       "{{.HostConfig.Privileged}}", container[0])
    check(held.returncode == 0, f"the proxy's container was not read: {held.stderr[-200:]}")
    options, devices, caps, privileged = held.stdout.strip().split("|")
    none = ("[]", "<no value>")
    check(options in none and devices in none and caps in none and privileged == "false",
          f"the proxy's container holds a security option, a device or a capability: {held.stdout.strip()}")
    holders = _sandbox_holders(ctx)
    compute = ctx.compose("ps", "-q", "otodock-compute", timeout=60).stdout.split()
    check(len(compute) == 1, f"no single otodock-compute container: {compute}")
    expected = {container[0][:12]} if broken else {compute[0][:12]}
    check(set(holders) == expected,
          f"the sandbox's privileges are held by {sorted(holders.values()) or 'no container'}, not the compute service alone")
    return f"refused in {refused}, runs in {runs}, its privileges on {', '.join(sorted(holders.values()))} alone"


# What the compute service's container is given for the sandbox and no other container
# needs (docker-compose.yml, the compute service): the three relaxations and the device.
SANDBOX_PRIVILEGES = ("seccomp=unconfined", "apparmor=unconfined", "apparmor=otodock", "systempaths=unconfined",
                      "/dev/net/tun")


def _sandbox_holders(ctx) -> dict[str, str]:
    """Each running container on this host that holds one of the sandbox's privileges:
    its short id and its name. A tool's container, Collabora and the one-shots hold
    none. A compute machine added on another server (``claim-compute-on-another-host``
    starts one on this host) is a compute machine too, and is left out."""
    ids = ctx.docker("ps", "-q", timeout=60).stdout.split()
    check(ids, "no container runs on this host")
    holders = {}
    for cid in ids:
        seen = ctx.docker("inspect", "-f", "{{.Name}}|{{.HostConfig.SecurityOpt}}|{{.HostConfig.Devices}}|"
                                           '{{index .Config.Labels "io.otodock.remote-compute"}}', cid, timeout=60)
        check(seen.returncode == 0, f"the container {cid} was not read: {seen.stderr[-200:]}")
        name, _, held = seen.stdout.strip().partition("|")
        held, _, remote = held.rpartition("|")
        if remote.strip() not in ("", "<no value>"):
            continue
        if any(word in held for word in SANDBOX_PRIVILEGES):
            holders[cid[:12]] = name.lstrip("/")
    return holders
