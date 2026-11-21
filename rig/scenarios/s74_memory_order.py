"""Who the kernel ends first when memory runs out, read from a running install: the
proxy and Postgres keep the default, the compute service sits above them, and what
can be started again (a session's processes, the document services, an app server, a
relayed tool) above that, and a paired Linux computer's session above its owner's
other programs."""
from __future__ import annotations

from lib.chat import ChatSession, script
from lib.scenario import check, scenario
from lib.tools import (
    MACHINE_FOLDER,
    READ_MEMORY,
    host_scores,
    programs_of,
    runs,
    tool_scores,
)
from run import ENGINES

from scenarios.s70_compute import _signed_in
from scenarios.s71_compute_apps import _deploy_server_app, _wake

# Each container's score, as compose sets it.
CONTAINERS = {"otodock-proxy": 0, "otodock-postgres": 0, "docker-socket-proxy": 0, "otodock-compute": 250,
              "file-tools": 500, "collabora": 500}
# A process a compute session, an app server or a relayed tool runs: at least this.
EXPENDABLE = 500
def _container_score(ctx, service: str) -> int:
    ids = ctx.compose("ps", "-q", service, timeout=60).stdout.split()
    check(ids, f"no {service} container runs")
    done = ctx.docker("inspect", "-f", "{{.HostConfig.OomScoreAdj}}", ids[0])
    check(done.returncode == 0, f"{service} was not read: {done.stderr[-200:]}")
    return int(done.stdout.strip() or 0)


@scenario("memory-order", needs=("turn-claude", "turn-direct", "compute-app-server", "machine-turn-claude"),
          needs_rig=True, machine=True)
def memory_order(ctx, broken: bool = False):
    """The proxy and Postgres score 0, the compute service 250, file-tools and Collabora
    500; on the compute machine a held Claude Code turn's engine, a Direct LLM chat's
    relayed tool and an app server score 500 or more, and so does a held turn's engine on
    the paired Linux computer. Broken: the proxy is expected to score 500, as an install
    that let the kernel end it before a session would."""
    _signed_in(ctx)
    wanted = {**CONTAINERS, **({"otodock-proxy": EXPENDABLE} if broken else {})}
    seen = {service: _container_score(ctx, service) for service in wanted}
    wrong = {s: f"{seen[s]}, not {want}" for s, want in wanted.items() if seen[s] != want}
    check(not wrong, f"the containers' scores: {wrong}")
    engine, model = ENGINES["claude"]
    held = ChatSession(ctx.owner, agent=ctx.agent)
    direct_engine, direct_model = ENGINES["direct"]
    direct = ChatSession(ctx.owner, agent=ctx.agent)
    paired = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        held.open(engine=engine, model=model, text="Wait a little. " + script(
            {"tool": "shell", "input": {"command": 'python3 -c "import time; time.sleep(30)"'}}, {"text": "Done."}))
        engines = ctx.owner.wait_until("the held turn's engine on the compute machine", lambda: [
            (score, command) for score, command in tool_scores(ctx, "otodock-compute", "claude") if runs("claude", command)],
            timeout=90, every=2)
        # Read to its end now: an unread socket is closed by the server's ping.
        held.wait_done(timeout=120)
        direct.open(engine=direct_engine, model=direct_model,
                    text="Read your memory. " + script(READ_MEMORY, {"text": "Read."}))
        direct.wait_done()
        relayed = programs_of([command for _, command in tool_scores(ctx, "otodock-compute", "memory-mcp")], "memory-mcp")
        tools = [(score, command) for score, command in tool_scores(ctx, "otodock-compute", "memory-mcp")
                 if command in relayed]
        app_id = ctx.state.get("server_app_id") or _deploy_server_app(ctx)
        _wake(ctx, app_id)
        servers = [(score, command) for score, command in tool_scores(ctx, "otodock-compute", "bun") if runs("bun", command)]
        paired.open(engine=engine, model=model, target=ctx.state["machine_id"], text="Wait a little. " + script(
            {"tool": "shell", "input": {"command": 'python3 -c "import time; time.sleep(30)"'}}, {"text": "Done."}))
        # The paired computer's sessions are this host's processes, under its folder.
        computer = ctx.owner.wait_until("the held turn's engine on the paired computer", lambda: [
            (score, command) for score, command in host_scores(ctx, "claude")
            if MACHINE_FOLDER in command and runs("claude", command)], timeout=90, every=2)
        paired.wait_done(timeout=120)
    finally:
        held.close()
        direct.close()
        paired.close()
    for what, found in (("the session's engine", engines), ("the relayed tool", tools), ("the app server", servers),
                        ("the paired computer's engine", computer)):
        check(found, f"no process of {what}")
        low = [f"{score} {command[:80]}" for score, command in found if score < EXPENDABLE]
        check(not low, f"{what} scores under {EXPENDABLE}: {low}")
    return (f"containers {seen}; on compute the engine {sorted({s for s, _ in engines})}, "
            f"the tool {sorted({s for s, _ in tools})}, the app server {sorted({s for s, _ in servers})}; "
            f"on the paired computer the engine {sorted({s for s, _ in computer})}")
