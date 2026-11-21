"""An app server on the compute machine: it runs there and never in the proxy, its
one way in is the compute plane's address, and a restart of the proxy starts it
again at its next request."""
from __future__ import annotations

import base64
import json
import time

from lib.chat import script
from lib.scenario import check, scenario
from run import ENGINES

from scenarios.s30_automation import APP_MANIFEST, _tool_rows
from scenarios.s70_compute import _signed_in

CLAUDE, CLAUDE_MODEL = ENGINES["claude"]
SERVER = 'Bun.serve({port: Number(process.env.PORT), hostname: "0.0.0.0", fetch() { return new Response("ok"); }});'
_BUN = ("for p in /proc/[0-9]*; do { tr '\\0' '\\n' < $p/cmdline; } 2>/dev/null | head -1 | tr '\\n' ' '; echo; done"
        " | grep -E '(^|/)bun( |$)' || true")
_LISTENING = "ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null || true"


def deploy_took(row: dict) -> bool:
    """The display tool answers a refused deploy as plain text, not as an error: the
    deploy took when it says so, or waits for an approval."""
    said = str(row.get("tool_result", ""))
    return "Deployed '" in said or "WAITING FOR APPROVAL" in said


def deploy_said(deployed: list[dict]) -> str:
    """A failed deploy's words, its own answer first: the chat rows after it are long."""
    if not deployed:
        return "the chat holds no deploy_app call"
    from lib.chat import tool_said
    return tool_said(deployed[-1], "the deploy")


def _bun_in(ctx, service: str) -> str:
    return ctx.compose("exec", "-T", service, "sh", "-c", _BUN, timeout=60).stdout


def _deploy_server_app(ctx, *, slug: str = "rig-server", server: str | None = SERVER, manifest: dict = APP_MANIFEST,
                       visibility: str = "user") -> str:
    """A folder app (with a server unless ``server`` is None), deployed by the owner's
    chat and approved: its id."""
    owner = ctx.owner
    # A shared app's folder is the agent's workspace, a person's their own.
    folder = f"/users/{owner.username}/workspace/apps/{slug}" if visibility == "user" else f"/workspace/apps/{slug}"
    files = (f"mkdir -p {folder}/client && "
             f"printf '%s' '{json.dumps(manifest)}' > {folder}/app.json && "
             f"printf '<p>Rig</p>' > {folder}/client/index.html")
    if server is not None:
        code = base64.b64encode(server.encode()).decode()
        files += f" && mkdir -p {folder}/server && echo {code} | base64 -d > {folder}/server/index.ts"
    prompt = "Deploy the app. " + script({"tool": "shell", "input": {"command": files}},
                                         {"tool": "deploy_app", "input": {"slug": slug, "visibility": visibility}},
                                         {"text": "Deployed."})
    chat = owner.chat(ctx.agent, engine=CLAUDE, model=CLAUDE_MODEL, text=prompt)
    try:
        chat.wait_done(timeout=240)
        rows = chat.rows()
    finally:
        chat.close()
    deployed = _tool_rows(rows, "deploy_app")
    check(deployed and not deployed[-1].get("is_error") and deploy_took(deployed[-1]), deploy_said(deployed))

    def listed():
        apps = owner.ok("GET", f"/v1/apps?agent={ctx.agent}")
        found = [a for a in (apps.get("apps", apps) if isinstance(apps, dict) else apps) if a.get("slug") == slug]
        return found[0] if found else None
    item = owner.wait_until("the app in the list", listed, timeout=60)
    state = owner.ok("GET", f"/v1/apps/{item['id']}/deploy/status")
    if state.get("pending_release"):
        owner.ok("POST", f"/v1/apps/{item['id']}/deploy/approve", {"release": state["pending_release"]})
    return str(item["id"])


def _wake(ctx, app_id: str) -> None:
    """A viewer's call that wakes the server and waits until it answers."""
    token = ctx.owner.ok("POST", f"/v1/apps/{app_id}/viewer-token")["token"]
    deadline = time.monotonic() + 120
    status = 0
    while time.monotonic() < deadline:
        status, _ = ctx.owner.call("GET", f"/v1/apps/{app_id}/api/x", headers={"Authorization": f"Bearer {token}"})
        if status == 200:
            return
        time.sleep(1)
    check(False, f"the app's server answered {status}")


@scenario("compute-app-server", needs=("turn-claude",), needs_rig=True)
def compute_app_server(ctx, broken: bool = False):
    """A server runs on the compute machine, its one way in is the compute plane's
    address, and a proxy restart starts it again at the next request. Broken: the
    server is expected in the proxy."""
    _signed_in(ctx)
    app_id = _deploy_server_app(ctx)
    ctx.state["server_app_id"] = app_id
    _wake(ctx, app_id)
    on_compute, on_proxy = _bun_in(ctx, "otodock-compute"), _bun_in(ctx, "otodock-proxy")
    if broken:
        on_compute, on_proxy = on_proxy, on_compute
    check(on_compute and not on_proxy, f"compute: {on_compute!r}, proxy: {on_proxy!r}")
    listening = ctx.compose("exec", "-T", "otodock-compute", "sh", "-c", _LISTENING, timeout=60).stdout
    address = ctx.compose("exec", "-T", "otodock-proxy", "printenv", "OTODOCK_COMPUTE_LINK_FROM", timeout=30).stdout.strip()
    check(address and f"{address}:" in listening, f"no listener on the compute plane: {listening[:400]}")
    ctx.compose("restart", "otodock-proxy", timeout=180)
    _signed_in(ctx)
    _wake(ctx, app_id)

    def _settled():
        on_c, on_p = _bun_in(ctx, "otodock-compute"), _bun_in(ctx, "otodock-proxy")
        return (on_c, on_p) if len(on_c.splitlines()) == 1 and not on_p else None
    ctx.owner.wait_until("one server, on the compute machine", _settled, timeout=20)


# The app's server asks the platform three things through the compute machine's app
# listener, the way an app's own code does: an event of its own app (refused for an app
# nobody shares), a vendor call to a host it never declared, and a route off the
# listener's list. It answers what each said.
PROBE_SERVER = """
const base = process.env.OTODOCK_PROXY_URL, id = process.env.OTODOCK_APP_ID;
const auth = {Authorization: `Bearer ${process.env.OTODOCK_APP_TOKEN}`, "Content-Type": "application/json"};
async function ask(path, init) {
  try {
    const r = await fetch(base + path, {headers: auth, ...init});
    return {status: r.status, body: (await r.text()).slice(0, 300)};
  } catch (e) {
    return {status: 0, body: String(e).slice(0, 300)};
  }
}
Bun.serve({port: Number(process.env.PORT), hostname: "0.0.0.0", async fetch(req) {
  if (new URL(req.url).pathname !== "/probe") return new Response("ok");
  return Response.json({
    events: await ask(`/v1/apps/${id}/events`, {method: "POST", body: JSON.stringify({name: "rig", payload: {}})}),
    // Built at run time: the deploy refuses a host it reads that the app does not declare.
    egress: await ask(`/v1/apps/${id}/egress/${["example", "com"].join(".")}/`, {method: "GET"}),
    off_list: await ask("/v1/agents", {method: "GET"}),
  });
}});
"""
# The route off the listener's list, and how each side logs a refused path
# (``satellite/transport/http_tunnel.py``, ``core/remote/satellite_http_tunnel.py``).
OFF_LIST = "/v1/agents"
OFF_LIST_LINE, PASSED_LINE = "tunnel: rejected app path: ", "tunnel: rejected non-allowlisted path: "
# What each answers (``api/apps/app_bindings.py``, ``app_egress.py``, the listener's list).
LISTENER_ANSWERS = {"events": (403, "only a shared app emits events"),
                    "egress": (404, "not an egress host of this app"),
                    "off_list": (403, "path-not-allowlisted")}
# One connection to the relay: how many bytes came back to a plain request.
RELAY_PROBE = ("import socket, sys\n"
               "s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)\n"
               "data = b''\n"
               "try:\n"
               "    s.sendall(b'GET / HTTP/1.0\\r\\nHost: rig\\r\\n\\r\\n')\n"
               "    while True:\n"
               "        chunk = s.recv(4096)\n"
               "        if not chunk:\n"
               "            break\n"
               "        data += chunk\n"
               "except OSError:\n"
               "    pass\n"
               "print(len(data))\n")


def _relay_ports(ctx, address: str) -> list[str]:
    """The ports the compute machine listens on at its link's address: its app servers' relays."""
    listening = ctx.compose("exec", "-T", "otodock-compute", "sh", "-c", _LISTENING, timeout=60).stdout
    return sorted({word.rsplit(":", 1)[1] for word in listening.split() if word.startswith(f"{address}:")})


@scenario("compute-app-listener", needs=("compute-app-server",), needs_rig=True)
def compute_app_listener(ctx, broken: bool = False):
    """An app server on the compute machine reaches its own app's routes through the
    machine's app listener and nothing else: an event is answered by the platform, a
    vendor call to an undeclared host is refused, a route off the list never leaves the
    machine. Its relay takes the platform's end of the link alone: a third container on
    the link's network gets no byte. Broken: the relay is asked from the platform's end,
    which it serves."""
    _signed_in(ctx)
    app_id = _deploy_server_app(ctx, slug="rig-listener", server=PROBE_SERVER)
    _wake(ctx, app_id)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    answers = _viewer_call(ctx, app_id, "probe")
    wrong = {what: answers.get(what) for what, (status, words) in LISTENER_ANSWERS.items()
             if (answers.get(what) or {}).get("status") != status or words not in str((answers.get(what) or {}).get("body"))}
    check(not wrong, f"the listener's answers: {wrong}")
    # The same refusal comes from the platform for a path the machine let through: the
    # logs say whose it was.
    machine_log = ctx.compose("logs", "--no-color", "--since", since, "otodock-compute", timeout=120).stdout
    proxy_log = ctx.compose("logs", "--no-color", "--since", since, "otodock-proxy", timeout=120).stdout
    check(f"{OFF_LIST_LINE}{OFF_LIST}" in machine_log, "the compute machine did not refuse the off-list route itself")
    check(f"{PASSED_LINE}{OFF_LIST}" not in proxy_log, "the off-list route reached the platform")
    address = ctx.compose("exec", "-T", "otodock-proxy", "printenv", "OTODOCK_COMPUTE_LINK_FROM", timeout=30).stdout.strip()
    ports = _relay_ports(ctx, address)
    check(ports, f"no relay listens at {address}")
    if broken:
        done = ctx.compose("exec", "-T", "otodock-proxy", "python3", "-c", RELAY_PROBE, address, ports[0], timeout=60)
    else:
        image = ctx.docker("inspect", "-f", "{{.Config.Image}}", ctx.compose("ps", "-q", "otodock-proxy").stdout.split()[0])
        done = ctx.docker("run", "--rm", "--network", "otodock_otodock-compute", "--entrypoint", "python3",
                          image.stdout.strip(), "-c", RELAY_PROBE, address, ports[0], timeout=120)
    check(done.returncode == 0, f"the relay's probe failed: {done.stderr[-300:]}")
    got = int((done.stdout.strip().splitlines() or ["-1"])[-1])
    check(got == 0, f"the relay answered a third container {got} bytes")
    logs = ctx.compose("logs", "--no-color", "--since", "10m", "otodock-compute", timeout=120).stdout
    check("refused" in logs and "a connection from" in logs, "the compute machine's log does not name the refused connection")
    return f"the listener answered {sorted(LISTENER_ANSWERS)} as the platform does; the relay gave a third container nothing"


# An app that keeps rows and may make a deletion final: its server seeds its database
# and asks the platform to remove the older copies the releases keep.
FORGET_MANIFEST = {**APP_MANIFEST, "actions": [
    {"id": "forget", "label": "Remove old data copies", "type": "platform", "method": "app.forget_copies"}]}
FORGET_SERVER = """
import { Database } from "bun:sqlite";
const base = process.env.OTODOCK_PROXY_URL, id = process.env.OTODOCK_APP_ID;
const auth = {Authorization: `Bearer ${process.env.OTODOCK_APP_TOKEN}`, "Content-Type": "application/json"};
Bun.serve({port: Number(process.env.PORT), hostname: "0.0.0.0", async fetch(req) {
  const path = new URL(req.url).pathname;
  if (path === "/seed") {
    const db = new Database(`${process.env.OTODOCK_DATA_DIR}/app.db`);
    db.run("CREATE TABLE IF NOT EXISTS rows (x TEXT)");
    db.run("INSERT INTO rows VALUES ('a row the app deletes')");
    db.close();
    return Response.json({seeded: true});
  }
  if (path === "/forget") {
    const r = await fetch(`${base}/v1/apps/${id}/platform/app.forget_copies`, {method: "POST", headers: auth, body: "{}"});
    return Response.json({status: r.status, body: await r.json().catch(() => null)});
  }
  return new Response("ok");
}});
"""
# The copy a release keeps of the database as it was before that release went live.
DB_BEFORE = "db-before.sqlite"


def _viewer_call(ctx, app_id: str, path: str):
    """One call of the app's server as its owner's page makes it; a viewer token per two seconds."""
    time.sleep(2.2)
    token = ctx.owner.ok("POST", f"/v1/apps/{app_id}/viewer-token")["token"]
    return ctx.owner.ok("GET", f"/v1/apps/{app_id}/api/{path}", headers={"Authorization": f"Bearer {token}"})


def _copies(ctx, app_id: str) -> list[str]:
    done = ctx.proxy_exec(f"find /opt/otodock/agents/{ctx.agent}/app-releases -path '*/{app_id}/*' "
                          f"-name '{DB_BEFORE}*' 2>/dev/null; true", timeout=60)
    return [line for line in done.stdout.splitlines() if line.strip()]


@scenario("compute-app-forgets-copies", needs=("compute-app-server",), needs_rig=True)
def compute_app_forgets_copies(ctx, broken: bool = False):
    """An app server on the compute machine makes a deletion final: after a second release
    over its database the platform keeps the copy from before it, and the server's own call
    removes it. Broken: the owner's page asks instead, which the platform refuses, so the
    copy stays."""
    _signed_in(ctx)
    app_id = _deploy_server_app(ctx, slug="rig-forget", server=FORGET_SERVER, manifest=FORGET_MANIFEST)
    _wake(ctx, app_id)
    check(_viewer_call(ctx, app_id, "seed").get("seeded"), "the app's server did not seed its database")
    again = _deploy_server_app(ctx, slug="rig-forget", server=FORGET_SERVER + "// the second release\n",
                               manifest=FORGET_MANIFEST)
    check(again == app_id, f"the second release made another app: {again}")
    ctx.owner.wait_until("the second release's copy of the database", lambda: _copies(ctx, app_id), timeout=120, every=3)
    _wake(ctx, app_id)
    if broken:
        said = ctx.owner.ok("POST", f"/v1/apps/{app_id}/catalog/app.forget_copies", {})
    else:
        said = _viewer_call(ctx, app_id, "forget")
        said = {"ok": (said.get("body") or {}).get("ok"), **(said.get("body") or {}), "status": said.get("status")}
    left = _copies(ctx, app_id)
    check(said.get("ok") is True and not left, f"the call answered {str(said)[:200]}, and {len(left)} copy(ies) are left")
    return f"the server's call removed {said.get('result', {}).get('removed')} file(s); no copy is left"
