"""Health per part, /metrics and the one busy-or-idle answer, read from a fresh install."""
from __future__ import annotations

import time

from lib.chat import ChatSession, script
from lib.person import Http
from lib.scenario import check, scenario
from run import ENGINES


@scenario("health-parts", needs=("sign-in",))
def health_parts(ctx, broken: bool = False):
    """/health names each part and the install takes work; /health/ready says 200.
    Broken: a state the install is not in is expected."""
    http = Http(ctx.base)
    status, body = http.call("GET", "/health", timeout=10)
    check(status == 200 and body.get("status") == "ok", f"/health answered {status} {body}")
    parts = body.get("parts") or {}
    check(set(parts) == {"database", "tools", "scheduler", "compute"}, f"the public parts: {parts}")
    check(all(parts.get(part) == "ok" for part in ("database", "tools", "scheduler", "compute")),
          f"a part is not working: {parts}")
    admin = ctx.owner.ok("GET", "/v1/admin/health", timeout=10).get("parts") or {}
    check(admin.get("phone") in ("ok", "off"), f"the admin's phone part: {admin}")
    want = "starting" if broken else "ready"
    check(body.get("state") == want and body.get("ready") is True, f"the state: {body}")
    ready, _ = http.call("GET", "/health/ready", timeout=10)
    check(ready == 200, f"/health/ready answered {ready}")
    check(not set(body) - {"status", "service", "build", "state", "ready", "parts"},
          f"the public answer grew a field: {sorted(body)}")
    return f"{body.get('state')}: {parts}"


@scenario("metrics-access", needs=("sign-in",))
def metrics_access(ctx, broken: bool = False):
    """/metrics answers an admin and refuses a stranger and a member; no person's data
    rides in it. Broken: a stranger is expected to read it."""
    status, _, _ = Http(ctx.base).raw("GET", "/metrics", timeout=10)
    check(status == (200 if broken else 401), f"a stranger got {status}")
    member, _, _ = ctx.person("member").raw("GET", "/metrics", timeout=10)
    check(member == 403, f"a member got {member}")
    owner, data, _ = ctx.owner.raw("GET", "/metrics", timeout=10)
    text = data.decode()
    check(owner == 200, f"the owner got {owner}")
    for name in ("otodock_up 1", 'otodock_part_ok{part="database"} 1', 'otodock_open_work{kind="turns"}',
                 "otodock_idle_seconds ", "otodock_sessions "):
        check(name in text, f"{name!r} is missing")
    for who in ("owner", "member"):
        check(ctx.email(who) not in text, f"the {who}'s address is in the counters")
    check("Rig Owner" not in text, "a person's name is in the counters")
    return f"{len(text.splitlines())} lines"


@scenario("busy-or-idle", needs=("engines",), needs_rig=True)
def busy_or_idle(ctx, broken: bool = False):
    """The admin command and /metrics read the same answer: busy while a turn runs,
    idle after it. Broken: the turn is expected to read as idle."""
    engine, model = ENGINES["claude"]
    before = ctx.admin("status")
    check(before.returncode == 0 and before.stdout.startswith("Idle"), f"status before: {before.stdout!r}")
    chat = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        chat.open(engine=engine, model=model, text="Wait a little. " + script(
            # Claude Code backgrounds a bare sleep: a call that holds the turn.
            {"tool": "shell", "input": {"command": 'python3 -c "import time; time.sleep(25)"'}},
            {"text": "Done waiting."}))
        busy = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            chat.next_frame(timeout=1)
            busy = ctx.admin("status").stdout
            if busy.startswith("Busy"):
                break
        check(busy.startswith("Idle" if broken else "Busy") and "turn" in busy,
              f"status during the turn: {busy!r}")
        _, data, _ = ctx.owner.raw("GET", "/metrics", timeout=10)
        text = data.decode()
        check('otodock_open_work{kind="turns"} 1' in text and "otodock_idle_seconds 0" in text,
              "the metrics do not read the turn: " + " ".join(
                  ln for ln in text.splitlines() if "open_work" in ln or "idle" in ln))
        chat.wait_done(timeout=180)
    finally:
        chat.close()
    after = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        after = ctx.admin("status").stdout
        if after.startswith("Idle"):
            break
        time.sleep(3)
    check(after.startswith("Idle"), f"status after the turn: {after!r}")
    return f"{busy.strip()} then {after.strip()}"
