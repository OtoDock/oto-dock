"""The fresh install, the wizard, the people, the scripted engines."""
from __future__ import annotations

import time

from lib.person import Http, Person
from lib.scenario import check, scenario
from run import IDLE_TIMEOUT_S

#: How long a fresh install may take to build its tools before it takes work
#: (over ten minutes at the rig's write limit).
READY_WAIT_S = 1800.0


@scenario("install", needs_rig=True)
def install(ctx):
    """The install answers, its containers are healthy and the public address is set."""
    status, body = Http(ctx.base).call("GET", "/health", timeout=10)
    check(status == 200 and body.get("status") == "ok", f"/health answered {status} {body}")
    check(ctx.env_value("DASHBOARD_PUBLIC_URL") == ctx.base, "the public address is in .env")
    rows = ctx.compose("ps", "--format", "{{.Service}} {{.Health}}").stdout.split("\n")
    states = dict(r.split(" ", 1) for r in rows if " " in r)
    check(states.get("otodock-proxy") == "healthy", f"the proxy is {states.get('otodock-proxy')}")
    waited = wait_ready(ctx)
    return f"build {body.get('build')}, ready after {waited:.0f} s"


def wait_ready(ctx, *, timeout: float = READY_WAIT_S) -> float:
    """A first boot listens before it builds the tools' environments and holds
    new work meanwhile: the scenarios start once the install takes work."""
    started = time.monotonic()

    def ready():
        try:
            status, _body = Http(ctx.base).call("GET", "/health/ready", timeout=5)
        except OSError:
            return None
        return status == 200 or None
    Person(ctx.base, "", "").wait_until("the install to take work", ready, timeout=timeout, every=10)
    return time.monotonic() - started


# What a start that found a bundled tool's environment to build logs: the proxy names the
# tools and the command, and builds nothing (``mcp_venv_bootstrap.build_notice``). The image
# carries those environments, so a start of it never says so. The compute machine beside
# the platform runs no reconcile of its own, so its log has no such line to read.
BUILD_LINES = {"otodock-proxy": ("need their environment built",)}


def build_lines(ctx) -> dict[str, list[str]]:
    """The lines of a tools' build in the proxy's log as it runs now."""
    built = {}
    for service, lines in BUILD_LINES.items():
        logs = ctx.compose("logs", "--no-color", service, timeout=120)
        check(logs.returncode == 0 and logs.stdout, f"{service}'s log was not read: {logs.stderr[-200:]}")
        built[service] = [line for line in lines if line in logs.stdout]
    return built


def builds_nothing(ctx, broken: bool, *, built: dict | None = None) -> str:
    """No line of a tools' build in those logs (or in ``built``, read earlier), and
    /health's tools ready."""
    built = dict(built if built is not None else build_lines(ctx))
    if broken:
        built["otodock-proxy"] = [] if built.get("otodock-proxy") else ["none of the lines a build logs"]
    check(not any(built.values()), f"the start built tools: {built}")
    _, body = Http(ctx.base).call("GET", "/health", timeout=10)
    check((body.get("parts") or {}).get("tools") == "ok", f"/health's tools: {body}")
    return "the proxy prepared nothing, the tools are ready"


@scenario("fresh-start-builds-nothing", needs=("install",), needs_rig=True)
def fresh_start_builds_nothing(ctx, broken: bool = False):
    """A fresh install's start builds no tool: the image carries the bundled tools'
    environments, so the proxy names nothing to prepare and holds no work for it, and
    /health says the tools are ready. The compute machine beside the platform runs no
    reconcile of its own: its log has nothing to read here. Broken: the proxy's line is
    expected."""
    return builds_nothing(ctx, broken)


@scenario("wizard", needs=("install",), needs_rig=True)
def wizard(ctx):
    """The wizard makes the first owner and refuses a second."""
    http = Http(ctx.base)
    config = http.ok("GET", "/auth/config")
    check(config.get("setup_required"), f"the install says it is set up: {config}")
    made = http.ok("POST", "/auth/setup", {"email": ctx.email("owner"), "password": ctx.password("owner"),
                                           "display_name": "Rig Owner", "origin": ctx.base}, timeout=300)
    check(made, "the wizard answered")
    status, _ = Http(ctx.base).call("POST", "/auth/setup", {"email": "second@rig.invalid", "password": ctx.password("owner"),
                                                            "display_name": "Second"})
    check(status == 403, f"a second owner answered {status}, not 403")
    agents = ctx.owner.ok("GET", "/v1/agents")["agents"]
    check(ctx.agent in [a["name"] for a in agents], f"the Personal Assistant is installed: {[a['name'] for a in agents]}")


@scenario("sign-in", needs=("wizard",))
def sign_in(ctx):
    """A wrong password is refused; a creator and a member are made and sign in."""
    status, _ = Http(ctx.base).call("POST", "/auth/login/local", {"email": ctx.email("owner"), "password": "wrong-password"})
    check(status in (401, 403), f"a wrong password answered {status}")
    me = ctx.owner.ok("GET", "/auth/me")["user"]
    check(me.get("email") == ctx.email("owner") and me.get("role") == "admin", f"the owner's own view: {me}")
    for who, role in (("creator", "creator"), ("member", "member")):
        first = ctx.password(f"{who}-first")
        ctx.owner.ok("POST", "/v1/admin/users", {"email": ctx.email(who), "display_name": f"Rig {who}", "role": role, "password": first})
        person = Person(ctx.base, ctx.email(who), first).sign_in()
        check(person.me.get("must_change_password"), f"a password an admin set is a first one: {person.me}")
        person.ok("PUT", "/v1/users/me/password", {"current_password": first, "new_password": ctx.password(who)})
        person = Person(ctx.base, ctx.email(who), ctx.password(who)).sign_in()
        ctx.state[f"{who}_sub"] = person.sub
        ctx.people[who] = person
        check(person.me.get("role") == role, f"{who} signed in as {person.me.get('role')}")


@scenario("engines", needs=("wizard",))
def engines(ctx):
    """The scripted model server connects through the admin pages' own routes."""
    from run import SCRIPTED_KEY, SCRIPTED_MODEL
    owner = ctx.owner
    # The admin's own knob: a warm chat holds a CLI and every tool's program, about
    # 300 MB, and the scenarios leave dozens of them behind on a 15 GB machine.
    owner.ok("PUT", "/v1/admin/platform-settings", {"session_idle_timeout": str(IDLE_TIMEOUT_S)})

    def made(status: int, body, what: str) -> None:
        # 409: an iteration on the install that is up finds it already there.
        check(status < 300 or status == 409, f"{what} answered {status}: {str(body)[:300]}")

    made(*owner.call("POST", "/v1/admin/execution-layers/claude-code-cli/subscriptions",
                     {"provider": "anthropic", "auth_type": "api_key", "api_key": SCRIPTED_KEY, "label": "Scripted"}), "the key")
    made(*owner.call("POST", "/v1/admin/execution-layers/local-endpoints",
                     {"provider": "openai_compatible", "endpoint_url": f"{ctx.models_url}/v1", "label": "Scripted",
                      "layers": ["codex-cli", "direct-llm"]}), "the local endpoint")
    for layer in ("codex-cli", "direct-llm"):
        made(*owner.call("POST", f"/v1/admin/execution-layers/{layer}/models",
                         {"model_id": SCRIPTED_MODEL, "display_name": "Scripted", "provider": "openai_compatible",
                          "context_window": 100000}), f"{layer}'s model")
    layers = owner.ok("GET", "/v1/admin/execution-layers")["layers"]
    ctx.state["capabilities"] = {row["name"]: row["capabilities"] for row in layers}
    for layer in ("codex-cli", "direct-llm"):
        rows = owner.ok("GET", f"/v1/admin/execution-layers/{layer}/models")
        names = [m.get("model_id") for m in (rows.get("models", rows) if isinstance(rows, dict) else rows)]
        check(SCRIPTED_MODEL in names, f"{layer} lists the scripted model: {names}")
