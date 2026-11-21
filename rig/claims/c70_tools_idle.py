"""A Docker tool's container sleeps when nobody uses it and starts at its first
call: the catalog's video tools on the rig's install, the quiet spell set to one
minute for the claims' window (``OTODOCK_TOOLS_IDLE_MINUTES`` in ``.env``, the
proxy started again), and put back after. Collabora and the phone service are
never stopped, also across a restart of Docker on the rig's VM."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid

from lib.chat import tool_rows, tool_said
from lib.scenario import CheckFailed, Inconclusive, check, scenario
from run import ENGINES, wait_ready

TOOL = "video-tools"
SERVICE = "video-tools"
_CARD = "<html><body style='background:#123;color:#fff;font:120px Inter'>OtoDock</body></html>"
# A function of the tool that answers in a few seconds: a half-second card.
QUICK = {"tool": "render_motion_clip", "input": {"html": _CARD, "duration": 0.5, "width": 320, "height": 180,
                                                 "output_path": "workspace/rig-tools-idle-quick.mp4"}}
# A render that outlives the spell: a small card, its length sized on this host
# (``_sized_long``) so the call runs about twice the spell and ends well before
# Claude Code's own limit on a tool that sends nothing (300 s).
LONG = {"tool": "render_motion_clip", "input": {"html": _CARD, "duration": 120, "fps": 30, "width": 320,
                                                "height": 180, "output_path": "workspace/rig-tools-idle-long.mp4"}}
SPELL_S = 60
LONG_TARGET_S = 2 * SPELL_S
# Before the long render: a small one that wakes the tool (a sleeping tool's wake would
# land in the measure), then one measured render. Its time over its frames is the cost
# of a frame with the call's own start in it, so a little above the true cost: the plan
# errs short. A cost below the floor is not believed, and no plan runs past the ceiling,
# well under Claude Code's 300 s on a silent tool.
WAKE_FRAMES, MEASURE_FRAMES = 15, 300
# The floor keeps a plan safe on the rig (about 30 ms a frame) whatever the measure says:
# 120 s over 20 ms is 6000 frames, about 200 s at the rig's cost.
FRAME_FLOOR_S = 0.02
PLAN_CEILING_S = 150
SLEEP_WAIT_S = SPELL_S + 3 * 60
KEPT = ("collabora", "otodock-phone")
# After the install answers again from a restart of Docker: long enough for its boot to
# start any tool it would start.
AFTER_BOOT_S = 30


def _env(ctx, line: str | None) -> None:
    """Set or drop the spell in the install's ``.env`` and start the proxy again."""
    env = ctx.install_dir / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith("OTODOCK_TOOLS_IDLE_MINUTES=")]
    if line:
        lines.append(line)
    env.write_text("\n".join(lines) + "\n")
    done = ctx.compose("up", "-d", "--no-deps", "--force-recreate", "otodock-proxy", timeout=300)
    if done.returncode != 0:
        raise CheckFailed(f"the proxy did not start again: {done.stderr[-300:]}")
    wait_ready(ctx)


def _running(ctx, project_filter: str) -> bool:
    done = ctx.docker("ps", "--filter", f"name={project_filter}", "--format", "{{.Names}}")
    return bool(done.stdout.strip())


def _tool_running(ctx) -> bool:
    return _running(ctx, f"mcp-{TOOL}")


def _put_back_keeping_logs(ctx, row: str, started: float, failed: bool) -> None:
    """The spell taken out and the server started again, as every claim here ends. A
    failed claim's logs are kept first: the new container's log would hold none of its
    calls."""
    if failed and getattr(ctx, "is_rig", False):
        from run import save_evidence
        save_evidence(ctx, f"{row}-before-the-restart", started)
    _env(ctx, None)


def _installed(ctx) -> None:
    """The catalog's video tools installed and enabled for the agent, as the admin's
    pages do it (the agent's whole list, the tool added). A tool the install already
    lists is not installed again: that makes its container again, and the claims after
    the first read the one they found."""
    from claims.c30_exit import _enabled_mcps
    have = [m.get("name") for m in ctx.owner.ok("GET", "/v1/admin/mcps")["mcps"]]
    if TOOL not in have:
        status, data, _ = ctx.owner.raw("POST", f"/v1/admin/community/mcps/{TOOL}/install", timeout=900)
        check(status in (200, 202), f"the video tools' install answered {status}: {data[:300]!r}")

    def listed() -> bool:
        # The install runs in the background: its job ends, and the agent's list names the tool.
        jobs = ctx.owner.ok("GET", "/v1/admin/community/mcps/installs").get("installs") or []
        job = next((j for j in jobs if j.get("name") == TOOL), None)
        if job and job.get("status") == "failed":
            raise CheckFailed(f"the video tools' install failed: {job.get('error') or job.get('message')}")
        names = [m.get("name") for m in ctx.owner.ok("GET", f"/v1/agents/{ctx.agent}/mcps")["mcps"]]
        return TOOL in names and (job is None or job.get("status") == "done")
    ctx.owner.wait_until("the video tools installed", listed, timeout=1800, every=10)
    enabled = _enabled_mcps(ctx)
    if TOOL not in enabled:
        ctx.owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [*enabled, TOOL]})


def _call(ctx, step: dict, text: str = "Use the video tools.", *, timeout: float = 180.0):
    """One Claude Code turn that calls the tool once. A real model (the live layer) is
    asked in words, with the call's arguments."""
    from claims.c10_claims import _ask
    engine, model = ENGINES["claude"]
    prompt = _ask(ctx, f"{text} Call the video tools' {step['tool']} tool exactly once with these arguments: "
                       f"{json.dumps(step['input'])}. Then reply with one word.", step, {"text": "Done."})
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt)
    try:
        chat.wait_done(timeout=timeout)
    finally:
        chat.close()
    return chat


def _timed_call(ctx, step: dict, text: str = "Use the video tools.", *, timeout: float = 180.0):
    """``_call``, and how long the tool itself took: between the model's call and its next
    one, by the scripted server's log. None with a real model, whose calls are not logged."""
    if ctx.state.get("real_models"):
        return _call(ctx, step, text, timeout=timeout), None
    from lib.chat import script
    tag = f"idle-{uuid.uuid4().hex[:8]}"
    engine, model = ENGINES["claude"]
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model,
                          text=f"{text} " + script(step, {"text": "Done."}, tag=tag))
    try:
        chat.wait_done(timeout=timeout)
    finally:
        chat.close()
    # The tool's call is the last of step 0 (a tool search before it answers on the same
    # step), its answer reaches the model with the first of step 1.
    calls = [(c.get("step"), float(c.get("t") or 0)) for c in ctx.model_calls() if c.get("tag") == tag]
    asked = [t for step_no, t in calls if step_no == 0]
    answered = [t for step_no, t in calls if step_no == 1]
    return chat, (min(answered) - max(asked) if asked and answered else None)


def _sized_long(ctx, broken: bool = False) -> tuple[dict, str]:
    """The long render's call, sized on this host to run about ``LONG_TARGET_S`` and
    never planned past ``PLAN_CEILING_S``: a small render wakes the tool, a measured one
    gives the cost of a frame, bounded below by ``FRAME_FLOOR_S``. With a real model,
    or when the measured render was not timed, the default. What it says names the
    measure, and a measure below the floor. Broken: a render shorter than the spell."""
    if broken:
        return {"tool": LONG["tool"], "input": {**LONG["input"], "duration": 0.5}}, "a half-second card"
    took = None
    for frames in (WAKE_FRAMES, MEASURE_FRAMES):
        step = {"tool": LONG["tool"], "input": {**LONG["input"], "duration": frames / 30,
                                                "output_path": f"workspace/rig-tools-idle-{frames}.mp4"}}
        chat, took = _timed_call(ctx, step, "Render a short card.")
        row = _answered(ctx, chat, LONG["tool"])
        check(not row.get("is_error"), tool_said(row, f"the render of {frames} frames"))
    if took is None:
        return LONG, "the default length: the measured render was not timed"
    measured = took / MEASURE_FRAMES
    per_frame = max(measured, FRAME_FLOOR_S)
    frames = int(min(LONG_TARGET_S, PLAN_CEILING_S) / per_frame)
    fps = 30 if frames <= 120 * 30 else 60
    duration = round(min(120.0, max(1.0, frames / fps)), 1)
    said = f"{duration} s at {fps} fps, {measured * 1000:.0f} ms a frame measured here"
    if measured < FRAME_FLOOR_S:
        said += f", below the {FRAME_FLOOR_S * 1000:.0f} ms floor, so planned on the floor"
    planned = duration * fps * per_frame
    return ({"tool": LONG["tool"], "input": {**LONG["input"], "fps": fps, "duration": duration}},
            f"{said}, {planned:.0f} s planned")


def _long_call(ctx, broken: bool = False) -> str:
    """The long render, sized on this host: it answers, it outlasted the spell, and the
    tool was not put to sleep under it. What it took."""
    step, sized = _sized_long(ctx, broken)
    chat, took = _timed_call(ctx, step, "Render the test pattern.", timeout=600)
    row = _answered(ctx, chat, LONG["tool"])
    # The call's own answer first: one refused ends at once and says why.
    check(not row.get("is_error"), tool_said(row, "a call longer than the spell"))
    check(took is None or took >= SPELL_S,
          f"the render took {took or 0:.0f} s, not past the {SPELL_S} s spell ({sized})")
    check(_tool_running(ctx), "the video tools were put to sleep under the call")
    return (f"the long call took {'an unmeasured time' if took is None else f'{took:.0f} s'} "
            f"past the {SPELL_S} s spell ({sized})")


def _answered(ctx, chat, name: str) -> dict:
    rows = tool_rows(chat.rows(), name)
    if not rows and ctx.state.get("real_models"):
        raise Inconclusive(f"the model did not call {name}")
    check(rows, f"no {name} call in the chat")
    return rows[-1]


@scenario("claim-tools-idle", layer="claims", needs_rig=True)
def tools_idle(ctx, broken: bool = False):
    """The tool sleeps after the spell, a call wakes it and answers, two calls at once
    start it once, a call longer than the spell is not stopped, Collabora and the phone
    service keep running. Broken: the spell is left at zero, so the tool never sleeps."""
    _installed(ctx)
    kept = [name for name in KEPT if _running(ctx, name)]
    started, failed = time.time(), True
    try:
        _env(ctx, None if broken else f"OTODOCK_TOOLS_IDLE_MINUTES={SPELL_S // 60}")
        ctx.owner.wait_until("the video tools asleep", lambda: not _tool_running(ctx),
                             timeout=SLEEP_WAIT_S, every=10)
        check(all(_running(ctx, name) for name in kept), "Collabora or the phone service stopped")
        # The memory order's row for a community Docker tool: its container, asleep
        # (kept by the sleep), scores as every tool does.
        name = ctx.docker("ps", "-a", "--filter", f"name=mcp-{TOOL}", "--format", "{{.Names}}").stdout.split()
        check(name, "no video tools container is kept while it sleeps")
        score = ctx.docker("inspect", "-f", "{{.HostConfig.OomScoreAdj}}", name[0]).stdout.strip()
        check(score == "500", f"the video tools' container scores {score!r}")
        chat = _call(ctx, QUICK)
        row = _answered(ctx, chat, QUICK["tool"])
        check(not row.get("is_error"), tool_said(row, "the first call after the sleep"))
        check(_tool_running(ctx), "the call did not start the video tools")
        ctx.owner.wait_until("the video tools asleep again", lambda: not _tool_running(ctx),
                             timeout=SLEEP_WAIT_S, every=10)
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
        chats: list = []
        threads = [threading.Thread(target=lambda: chats.append(_call(ctx, QUICK))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(300)
        woke = ctx.compose("logs", "otodock-proxy", "--since", since).stdout.count("video-tools: woke in")
        check(woke == 1, f"two calls at once started the video tools {woke} times")
        said = _long_call(ctx)
        failed = False
    finally:
        _put_back_keeping_logs(ctx, "claim-tools-idle", started, failed)
    return f"asleep after the spell, woken by a call, started once by two; {said}"


@scenario("claim-tools-idle-long-call", layer="claims", needs_rig=True)
def tools_idle_long_call(ctx, broken: bool = False):
    """A call from the compute machine that runs past the tools' idle spell is neither
    cut nor put to sleep under: the door's keepalives carry it. A call past the link's
    own idle reap (16 minutes) waits for Claude Code's limit on a silent tool (300 s).
    Broken: the render is shorter than the spell, so it proves nothing and fails."""
    _installed(ctx)
    started, failed = time.time(), True
    try:
        _env(ctx, f"OTODOCK_TOOLS_IDLE_MINUTES={SPELL_S // 60}")
        said = _long_call(ctx, broken)
        failed = False
    finally:
        _put_back_keeping_logs(ctx, "claim-tools-idle-long-call", started, failed)
    return said


def restart_docker(ctx) -> None:
    """Docker itself started again on the rig's VM, as a host's reboot or an engine update
    does: every container stops, and those whose policy says so start again. The model
    server and the paired machine are host processes and stay. The write cap sits on the
    service slice, so it stays too. Waited for: the install answers, the compute machine
    signs in."""
    ctx.require_rig()
    live = ctx.docker("info", "-f", "{{.LiveRestoreEnabled}}").stdout.strip()
    check(live == "false", f"Docker keeps containers running across its restart here ({live!r}): nothing would be proven")
    done = subprocess.run(["sudo", "-n", "systemctl", "restart", "docker"], capture_output=True, text=True,
                          timeout=ctx.machinery(300), check=False)
    check(done.returncode == 0, f"Docker did not start again: {done.stderr[-300:]}")
    from scenarios.s70_compute import _signed_in
    wait_ready(ctx)
    _signed_in(ctx)


@scenario("claim-tools-idle-docker-restart", layer="claims", needs_rig=True)
def tools_idle_docker_restart(ctx, broken: bool = False):
    """A tool asleep stays asleep across a restart of Docker (its own service is
    ``unless-stopped``), and a boot starts only the tools that stay warm or have no
    record: Collabora and the phone service run again, the video tools do not. Broken:
    the tool's restart policy is put back to ``always``, so Docker starts it."""
    _installed(ctx)
    kept = [name for name in KEPT if _running(ctx, name)]
    started, failed = time.time(), True
    try:
        _env(ctx, f"OTODOCK_TOOLS_IDLE_MINUTES={SPELL_S // 60}")
        ctx.owner.wait_until("the video tools asleep", lambda: not _tool_running(ctx),
                             timeout=SLEEP_WAIT_S, every=10)
        name = ctx.docker("ps", "-a", "--filter", f"name=mcp-{TOOL}", "--format", "{{.Names}}").stdout.split()
        check(name, "no video tools container")
        if broken:
            ctx.docker("update", "--restart", "always", name[0])
        restart_docker(ctx)
        ctx.owner.wait_until("Collabora and the phone service back", lambda: all(_running(ctx, n) for n in kept),
                             timeout=ctx.machinery(300), every=10)
        time.sleep(AFTER_BOOT_S)
        check(not _tool_running(ctx), "the video tools started with Docker while nobody called them")
        done = ctx.docker("inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", name[0])
        check(done.stdout.strip() == "unless-stopped", f"the restart policy is {done.stdout.strip()!r}")
        failed = False
    finally:
        _put_back_keeping_logs(ctx, "claim-tools-idle-docker-restart", started, failed)
    return f"asleep across a restart of Docker; {', '.join(kept) or 'no kept service'} running again"
