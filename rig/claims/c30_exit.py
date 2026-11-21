"""The credential exit: a session holds no value of a
covered credential, read through the running install with a probe tool of the
rig's own.

The probe is a community stdio tool packed inline and installed by upload. The
installer takes a community Python tool only from a package source and drops an
uploaded ``requirements.txt``, so the probe names one small pinned package it never
imports and ships a server on the standard library alone (``PROBE_SERVER``). Its
manifest declares an env-delivered secret instance field and an ``exit`` entry for
it toward one host; its one tool answers what its environment holds and how an
HTTPS request carrying that value in its URL toward the host is answered.
"""
from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import re
import secrets
import subprocess
import time
import uuid
import zipfile

from lib import secret_scan
from lib.chat import script, tool_rows
from lib.scenario import CheckFailed, Inconclusive, check, scenario
from run import ENGINES, PROXY_PY, engine_of, wait_ready

PROBE = "rig-exit-probe"
PROBE_HOST = "api.example.com"
PROBE_KEY = "PROBE_API_KEY"
#: What the exit hands a session in place of a value, and what it answers a request
#: that carries the placeholder outside the credential's header
#: (``proxy/core/credentials/credential_exit.py``: ``PLACEHOLDER_PREFIX``, ``REFUSAL_PLACE``).
PLACEHOLDER = re.compile(r"oto-exit-[0-9a-f]{32}")
REFUSAL_PLACE = "The credential may be sent only in its header"
#: The broken form's edit of the server's own code: ``_exit_placeholders`` gives no
#: placeholder, so the bundle is delivered as it was and the tool gets the value.
_EXIT_DOCSTRING_END = '        routes through the exit."""'
_EXIT_NONE = _EXIT_DOCSTRING_END + "\n        return (), False"
#: Where a value could sit inside the proxy's container: the sessions' config
#: dirs, the agents' folders, the installed community tools, the proxy's own
#: log file and the temporary folder.
INSIDE_ROOTS = ("/opt/otodock/sessions", "/opt/otodock/agents", "/opt/otodock/mcps/community",
                "/opt/otodock/proxy/proxy.log*", "/tmp")

PROBE_MANIFEST = {
    "name": PROBE, "label": "Rig exit probe", "version": "1", "category": "community",
    "description": "The rig's probe of the credential exit: reports its environment and one request.",
    "server": {"runtime": "python", "transport": "stdio", "source": "pypi:six@1.17.0",
               "command": "venv/bin/python", "args": ["server.py"], "tool_timeout_sec": 60},
    "credentials": {"type": "none",
                    "exit": [{"key": PROBE_KEY, "header": "Authorization", "prefix": "Bearer ",
                              "hosts": [PROBE_HOST]}]},
    "instances": {"delivery": "env", "max_instances": 0,
                  "fields": [{"key": PROBE_KEY, "label": "Probe API key", "input_type": "password",
                              "secret": True, "required": True}]},
    "assignment_mode": "explicit",
    "permissions": {"default_tier": "standard"},
}

PROBE_SERVER = '''\
"""The rig's probe: one tool that reports what its environment holds and how one
HTTPS request through the environment's proxy settings is answered."""
import json
import os
import sys
import urllib.error
import urllib.request

HOST = "api.example.com"
TOOL = {"name": "probe",
        "description": "Report the probe's environment and the answer to one HTTPS request.",
        "inputSchema": {"type": "object", "properties": {}}}


def probe() -> dict:
    value = os.environ.get("PROBE_API_KEY", "")
    out = {"env_value": value,
           "https_proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))}
    try:
        with urllib.request.urlopen(f"https://{HOST}/?k={value}", timeout=15) as reply:
            out["status"], out["body"] = reply.status, reply.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        out["status"], out["body"] = error.code, error.read(2000).decode("utf-8", "replace")
    except (OSError, ValueError) as error:
        out["status"], out["body"] = 0, f"{type(error).__name__}: {error}"
    return out


def answer(request: dict):
    method = request.get("method", "")
    params = request.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "rig-exit-probe", "version": "1"}}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        if params.get("name") != "probe":
            return {"content": [{"type": "text", "text": f"no tool {params.get('name')}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(probe())}], "isError": False}
    if method == "ping":
        return {}
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        result = answer(request)
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": f"no method {request.get('method')}"}
        else:
            reply["result"] = result
        sys.stdout.write(json.dumps(reply) + "\\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''

# Run in the proxy's container with the value on stdin: the processes whose command
# line or environment carry it (this probe's own left out) and the files under the
# given roots that do. Paths and process ids are printed, the value never.
INSIDE = (
    "import glob, json, os, sys\n"
    "value = sys.stdin.read().strip().encode()\n"
    "me = str(os.getpid())\n"
    "found = {'processes': [], 'files': []}\n"
    "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
    "    if pid == me:\n"
    "        continue\n"
    "    for part in ('cmdline', 'environ'):\n"
    "        try:\n"
    "            data = open(f'/proc/{pid}/{part}', 'rb').read()\n"
    "        except OSError:\n"
    "            continue\n"
    "        if value in data:\n"
    "            found['processes'].append(f'{pid} {part}')\n"
    "def look(path):\n"
    "    try:\n"
    "        if os.path.islink(path) or os.path.getsize(path) > 64 * 1024 * 1024:\n"
    "            return\n"
    "        with open(path, 'rb') as fh:\n"
    "            data = fh.read()\n"
    "    except OSError:\n"
    "        return\n"
    "    if value in data:\n"
    "        found['files'].append(path)\n"
    "for root in (p for pattern in sys.argv[1:] for p in glob.glob(pattern)):\n"
    "    if os.path.isfile(root):\n"
    "        look(root)\n"
    "        continue\n"
    "    for folder, _dirs, names in os.walk(root):\n"
    "        for name in names:\n"
    "            look(os.path.join(folder, name))\n"
    "print(json.dumps(found))\n"
)




def _restart(ctx) -> None:
    done = ctx.compose("restart", "otodock-proxy", timeout=300)
    if done.returncode != 0:
        raise CheckFailed(f"the proxy did not restart: {done.stderr[-300:]}")
    wait_ready(ctx)


def _install_probe(ctx, name: str = PROBE, manifest: dict = PROBE_MANIFEST, server: str = PROBE_SERVER) -> None:
    """Upload a probe as an admin uploads a tool."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr(f"{name}/manifest.json", json.dumps(manifest))
        z.writestr(f"{name}/server.py", server)
    edge = uuid.uuid4().hex
    body = (f"--{edge}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + packed.getvalue() + f"\r\n--{edge}--\r\n".encode()
    status, data, _ = ctx.owner.raw("POST", "/v1/admin/mcps/install", body=body, timeout=600,
                                    headers={"Content-Type": f"multipart/form-data; boundary={edge}"})
    text = data.decode(errors="replace")
    check(status == 200, f"the probe's install answered {status}: {text[:400]}")
    answer = json.loads(text)
    check(answer.get("status") in ("installed", "updated"), f"the probe's install: {text[:400]}")
    # An uploaded tool's sign-in hosts wait for an admin's Allow, as the card asks.
    waiting = answer.get("hosts_to_confirm")
    if waiting:
        ctx.owner.ok("POST", "/v1/admin/oauth-bearer-allowlist/allow",
                     {key: waiting[key] for key in ("mcp_name", "provider", "hosts", "digest")})


def _make_instance(ctx, value: str) -> int:
    made = ctx.owner.ok("POST", f"/v1/admin/mcps/{PROBE}/instances",
                        {"instance_name": "rig", "field_values": {PROBE_KEY: value}, "agents": [ctx.agent]})
    check(isinstance(made, dict) and made.get("id") is not None, f"the instance: {str(made)[:200]}")
    return int(made["id"])


def _enabled_mcps(ctx) -> list[str]:
    return [m["name"] for m in ctx.owner.ok("GET", f"/v1/agents/{ctx.agent}/mcps")["mcps"] if m.get("enabled")]


def _inside(ctx, value: str, service: str = "otodock-proxy", roots: tuple[str, ...] = INSIDE_ROOTS) -> dict:
    """Where the value sits inside a service's container (``INSIDE``): the
    proxy's, or the compute service's. It goes in on stdin, so the look's own
    command line never carries it."""
    ctx.require_rig()
    done = subprocess.run(["docker", "compose", "exec", "-T", service, "python3", "-c", INSIDE, *roots],
                          cwd=ctx.install_dir, input=value, capture_output=True, text=True, timeout=300, check=False)
    check(done.returncode == 0, f"the look inside {service} failed: {done.stderr[-300:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _hide(text: str, value: str) -> str:
    """A text for the report, the value taken out of it."""
    return str(text).replace(value, "<the value>")


def _probe_answer(ctx, rows: list[dict], value: str) -> dict:
    steps = tool_rows(rows, "__probe")
    if not steps:
        if ctx.state.get("real_models"):
            raise Inconclusive("the model did not call the probe")
        raise CheckFailed("the probe's call left no row")
    text = str(steps[0].get("tool_result", ""))
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        raise CheckFailed(f"the probe answered no JSON: {_hide(text, value)[:300]!r}") from None


@scenario("claim-exit-no-value-in-the-session", layer="claims", needs=("sign-in", "turn-claude"), needs_rig=True)
def exit_no_value_in_the_session(ctx, broken: bool = False):
    """A tool whose manifest declares an exit entry gets a placeholder
    in place of the value an admin typed into its instance; the value is in no process,
    file or log line of the install, and the tool's request carrying the placeholder in
    its URL toward the declared host is refused at the exit before any dial. Broken: the
    session's config hands the tool the value itself, as a product that skipped the exit
    would."""
    ctx.require_rig()
    engine, model = ENGINES["claude"]
    planted = "rig-exit-" + secrets.token_hex(16)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    instance_id = None
    enabled_before: list[str] | None = None
    installed = False
    try:
        if broken:
            ctx.break_product("services/mcp/mcp_session_config.py", _EXIT_DOCSTRING_END, _EXIT_NONE)
            _restart(ctx)
        _install_probe(ctx)
        installed = True
        instance_id = _make_instance(ctx, planted)
        enabled_before = _enabled_mcps(ctx)
        ctx.owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [*enabled_before, PROBE]})
        words = "Call the probe tool once, then reply with one word."
        prompt = words if ctx.state.get("real_models") else f"{words} {script({'tool': 'probe', 'input': {}}, {'text': 'Probed.'})}"
        chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt, mode="dontAsk")
        try:
            chat.wait_done(timeout=240)
            rows = chat.rows()
            # The session is still warm: its tool process is among the container's.
            inside = _inside(ctx, planted)
        finally:
            chat.close()
        answer = _probe_answer(ctx, rows, planted)
        value = str(answer.get("env_value", ""))
        check(answer.get("https_proxy") is True, "the tool's environment names the exit as its HTTPS proxy")
        check(value != planted, "the tool's environment holds the value the admin typed")
        check(PLACEHOLDER.fullmatch(value), f"the tool's environment holds {value[:9]!r}... ({len(value)} characters), no placeholder")
        status, body = answer.get("status"), str(answer.get("body", ""))
        check(status == 400 and REFUSAL_PLACE in body,
              f"the GET with the placeholder in its URL answered {status}: {_hide(body, planted)[:200]!r}")
        check(not inside["processes"], f"the value is in the process list: {inside['processes']}")
        check(not inside["files"], f"the value is in a file of the install: {inside['files']}")
        for folder in (ctx.install_dir, ctx.run_dir):
            hits = [h for h in secret_scan.scan(folder, {"the planted value": planted}) if h.endswith("the planted value")]
            check(not hits, f"the value is under {folder}: {hits}")
        logs = ctx.compose("logs", "--no-color", "--since", since, "otodock-proxy", timeout=120)
        check(logs.returncode == 0, f"the proxy's log could not be read: {logs.stderr[-300:]}")
        check(planted not in logs.stdout, "the value is in the proxy's log")
        check(not PLACEHOLDER.search(logs.stdout), "the placeholder is in the proxy's log")
        # The exit changes nothing for the session's own tools: none of them
        # reaches it (a Docker tool's compose name stays direct), and every
        # one connects as it would without it.
        check("a plain request refused" not in logs.stdout,
              "a client of the session sent a plain request through the exit")
        failed = [ln for ln in logs.stdout.splitlines() if "MCP init" in ln and "failed=[]" not in ln]
        check(not failed, f"a tool of the session failed to connect with the exit on: {failed[-1][-160:] if failed else ''}")
    finally:
        with contextlib.suppress(Exception):
            if enabled_before is not None:
                ctx.owner.call("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled_before})
            if instance_id is not None:
                ctx.owner.call("DELETE", f"/v1/admin/mcps/{PROBE}/instances/{instance_id}")
            if installed:
                ctx.owner.call("DELETE", f"/v1/admin/mcps/{PROBE}", timeout=120)
        if broken:
            ctx.mend_product()
            wait_ready(ctx)
    return (f"a placeholder in the tool's environment, the GET toward {PROBE_HOST} refused 400 at the exit, "
            "the value in no process, file or log line")


TOKEN_PROBE = "rig-token-probe"
TOKEN_PROBE_PROVIDER = "rig-probe"
TOKEN_PROBE_ENV = "RIG_PROBE_TOKEN"
TOKEN_PROBE_MANIFEST = {
    "name": TOKEN_PROBE, "label": "Rig token probe", "version": "1", "category": "community",
    "description": "The rig's probe of a tool's account token: reports its environment and one renewal.",
    "server": {"runtime": "python", "transport": "stdio", "source": "pypi:six@1.17.0",
               "command": "venv/bin/python", "args": ["server.py"], "tool_timeout_sec": 60},
    "credentials": {"type": "per_user", "label": "Rig token probe", "oauth": {
        "provider_id": TOKEN_PROBE_PROVIDER, "flows": ["authorization_code"],
        "authorization_url": "https://auth.example.com/authorize", "token_url": "https://auth.example.com/token",
        "mcp_env_injection": [TOKEN_PROBE_ENV]}},
    "assignment_mode": "auto",
    "permissions": {"default_tier": "standard"},
}
#: The probe's one tool for this claim: what its environment holds and what the
#: bundle's route answers its renewal.
_TOKEN_PROBE_BODY = '''def probe() -> dict:
    out = {"token": os.environ.get("RIG_PROBE_TOKEN", ""),
           "renewal": bool(os.environ.get("OTO_TOKEN_RENEWAL")),
           "expires": os.environ.get("OTO_TOKEN_EXPIRES_AT", "")}
    request = urllib.request.Request(
        os.environ["PROXY_URL"].rstrip("/") + "/v1/hooks/mcp-credentials", data=b"", method="POST",
        headers={"Authorization": "Bearer " + os.environ.get("OTO_TOKEN_RENEWAL", "")})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as reply:
            out["status"], out["names"] = reply.status, sorted(json.loads(reply.read()).get("env", {}))
    except urllib.error.HTTPError as error:
        out["status"], out["names"] = error.code, []
    return out


'''
TOKEN_PROBE_SERVER = (PROBE_SERVER[:PROBE_SERVER.index("def probe()")] + _TOKEN_PROBE_BODY
                      + PROBE_SERVER[PROBE_SERVER.index("def answer("):]).replace("rig-exit-probe", TOKEN_PROBE)
# Run in the proxy's container with the planted account on stdin: the file and the
# rows a sign-in leaves behind, so no vendor is needed.
PLANT_ACCOUNT = (
    "import json, sys\n"
    "sys.path.insert(0, '/opt/otodock/proxy')\n"
    "from services.oauth import oauth_account_store as s\n"
    "from storage.identity import credential_store as c\n"
    "a = json.load(sys.stdin)\n"
    "d = s.get_token_dir(a['username'], provider_id=a['provider'])\n"
    "(d / (a['label'] + '.json')).write_text(json.dumps(a['file']))\n"
    "c.set_user_credentials(a['sub'], a['mcp'], {}, account_label=a['label'])\n"
    "c.add_account_to_agent(a['sub'], a['mcp'], a['agent'], a['label'])\n"
)
#: The broken form's edit: the reader of a token file answers the refresh token,
#: as a product that handed the tool the file's secret would.
_TOKEN_READER = '    return raw.get("access_token") or ""'
_TOKEN_READER_BROKEN = '    return raw.get("refresh_token") or ""'


def _token_probe_claim(ctx, word: str, broken: bool) -> str:
    """The token probe on one engine's session (on the compute service, where the
    install runs Claude Code and Codex): the access token and a renewal of its names
    alone in the tool, the refresh token in no process, file or log line outside the
    central store, in the proxy's container or the compute service's."""
    ctx.require_rig()
    engine, model = engine_of(ctx, word)
    refresh, access = "rig-refresh-" + secrets.token_hex(16), "rig-access-" + secrets.token_hex(16)
    label = "rig@example.test"
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    enabled_before: list[str] | None = None
    installed = False
    try:
        if broken:
            ctx.break_product("services/oauth/oauth_account_store.py", _TOKEN_READER, _TOKEN_READER_BROKEN)
            wait_ready(ctx)
        _install_probe(ctx, TOKEN_PROBE, TOKEN_PROBE_MANIFEST, TOKEN_PROBE_SERVER)
        installed = True
        account = {"username": ctx.owner.username, "sub": ctx.owner.sub, "provider": TOKEN_PROBE_PROVIDER,
                   "mcp": TOKEN_PROBE, "agent": ctx.agent, "label": label,
                   "file": {"access_token": access, "refresh_token": refresh, "expires_at": expires,
                            "client_id": "rig", "client_secret": "rig-secret", "scopes": ["rig.read"]}}
        done = subprocess.run(["docker", "compose", "exec", "-T", "otodock-proxy", PROXY_PY, "-c", PLANT_ACCOUNT],
                              cwd=ctx.install_dir, input=json.dumps(account), capture_output=True, text=True,
                              timeout=ctx.machinery(120), check=False)
        check(done.returncode == 0, f"the account could not be planted: {done.stderr[-300:]}")
        enabled_before = _enabled_mcps(ctx)
        ctx.owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [*enabled_before, TOKEN_PROBE]})
        words = "Call the probe tool once, then reply with one word."
        prompt = words if ctx.state.get("real_models") else f"{words} {script({'tool': 'probe', 'input': {}}, {'text': 'Probed.'})}"
        chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt, mode="dontAsk")
        try:
            chat.wait_done(timeout=240)
            rows = chat.rows()
            inside = _inside(ctx, refresh)
            on_compute = _inside(ctx, refresh, "otodock-compute", COMPUTE_ROOTS)
        finally:
            chat.close()
        answer = _probe_answer(ctx, rows, refresh)
        check(answer.get("token") == access, f"the tool's token is {str(answer.get('token'))[:12]!r}..., not the account's access token")
        check(answer.get("renewal") is True, "the tool holds no renewal for a token that expires")
        check(answer.get("expires") == expires, "the tool was not told when its token expires")
        check(answer.get("status") == 200, f"the renewal answered {answer.get('status')}")
        names = set(answer.get("names") or [])
        check(TOKEN_PROBE_ENV in names and "OTO_TOKEN_RENEWAL" not in names,
              f"the renewal answered the names {sorted(names)}")
        check(not inside["processes"], f"the refresh token is in the process list: {inside['processes']}")
        leaked = [f for f in inside["files"] if f"{TOKEN_PROBE_PROVIDER}-tokens/" not in f]
        check(not leaked, f"the refresh token is in a file outside the central store: {leaked}")
        check(not on_compute["processes"], f"the refresh token is in a process of the compute service: {on_compute['processes']}")
        leaked = [f for f in on_compute["files"] if f"{TOKEN_PROBE_PROVIDER}-tokens/" not in f]
        check(not leaked, f"the refresh token is in a file of the compute service outside the central store: {leaked}")
        logs = ctx.compose("logs", "--no-color", "--since", since, "otodock-proxy", timeout=120)
        check(logs.returncode == 0, f"the proxy's log could not be read: {logs.stderr[-300:]}")
        check(refresh not in logs.stdout and access not in logs.stdout, "a token is in the proxy's log")
    finally:
        with contextlib.suppress(Exception):
            if enabled_before is not None:
                ctx.owner.call("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled_before})
            if installed:
                ctx.owner.call("DELETE", f"/v1/admin/mcps/{TOKEN_PROBE}", timeout=120)
        if broken:
            ctx.mend_product()
            wait_ready(ctx)
    return (f"{word}: the tool holds the access token and a renewal of its names alone; the refresh token is in "
            "no process, file or log line")


@scenario("claim-google-no-refresh-token-in-the-session", layer="claims", needs=("sign-in", "turn-claude"), needs_rig=True)
def no_refresh_token_in_the_session(ctx, broken: bool = False):
    """A stdio tool on an OAuth account (the Google tools' shape) holds a short-lived
    access token and a renewal that reads only that token; the account's refresh token
    is in no process, file or log line a session reads. Broken: the reader of the token
    file answers the refresh token, as a product that handed it over would."""
    return _token_probe_claim(ctx, "claude", broken)


@scenario("claim-google-no-refresh-token-in-the-session-codex", layer="claims", needs=("sign-in", "turn-codex"),
          needs_rig=True)
def no_refresh_token_in_the_session_codex(ctx, broken: bool = False):
    """The same in a Codex session. Broken: the reader of the token file answers the
    refresh token."""
    return _token_probe_claim(ctx, "codex", broken)


# ---------------------------------------------------------------------------
# the exit on the machines: a paired computer and the compute service
# ---------------------------------------------------------------------------

#: What the exit answers a session's request toward an address the sandbox does
#: not reach (``exit_relay.REFUSAL_WHERE``).
REFUSAL_WHERE = "The exit does not reach that address"
#: Where a value could sit inside the compute service's container: the sessions'
#: config dirs, the agents' folders, the tools and the temporary folder.
COMPUTE_ROOTS = ("/opt/otodock/sessions", "/opt/otodock/agents", "/opt/otodock/mcps/community", "/tmp")
#: Three addresses a compute machine's exit never dials for a session, whatever the
#: request carries: the machine's own loopback, the cloud metadata address and a
#: private address outside the session's carves.
#: The private one is the top of 10/8, built from its first octet so the
#: public tree names no internal address.
DIAL_TARGETS = ("127.0.0.1", "169.254.169.254", str(ipaddress.IPv4Network((10 << 24, 8))[-2]))
DIALS = "rig-exit-dials"
DIALS_MANIFEST = {**PROBE_MANIFEST, "name": DIALS, "label": "Rig exit dials",
                  "description": "The rig's probe of a compute machine's exit: its environment and three addresses."}
_PROBE_REQUEST = '''    try:
        with urllib.request.urlopen(f"https://{HOST}/?k={value}", timeout=15) as reply:
            out["status"], out["body"] = reply.status, reply.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        out["status"], out["body"] = error.code, error.read(2000).decode("utf-8", "replace")
    except (OSError, ValueError) as error:
        out["status"], out["body"] = 0, f"{type(error).__name__}: {error}"
    return out
'''
_DIALS_REQUESTS = '''    out["dials"] = {}
    # The session's environment sends its own loopback past the exit (NO_PROXY): the
    # dial is the exit's to refuse, so every target goes through it.
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    for target in TARGETS:
        try:
            with urllib.request.urlopen(f"https://{target}/", timeout=15) as reply:
                out["dials"][target] = f"{reply.status} {reply.read(200).decode('utf-8', 'replace')}"
        except urllib.error.HTTPError as error:
            out["dials"][target] = f"{error.code} {error.read(200).decode('utf-8', 'replace')}"
        except (OSError, ValueError) as error:
            out["dials"][target] = f"0 {type(error).__name__}: {error}"
    return out
'''
#: The dials probe: the probe's server with its one request replaced by one
#: CONNECT through the environment's proxy toward each target. A refused CONNECT
#: surfaces as the tunnel's failure carrying the exit's status.
DIALS_SERVER = PROBE_SERVER.replace(
    'HOST = "api.example.com"', 'HOST = "api.example.com"\nTARGETS = ' + repr(DIAL_TARGETS),
).replace(_PROBE_REQUEST, _DIALS_REQUESTS)
assert _PROBE_REQUEST in PROBE_SERVER and "TARGETS" in DIALS_SERVER


def _host_search(value: str, roots: list[str], since: str) -> dict:
    """Where the value sits on this host (the rig's own, paired as the Linux
    machine): the processes whose command line or environment carry it, the
    files under the roots (the satellite's own folder) and the account's
    journal since the start."""
    done = subprocess.run(["python3", "-c", INSIDE, *roots], input=value, capture_output=True, text=True,
                          timeout=300, check=False)
    check(done.returncode == 0, f"the look on the host failed: {done.stderr[-300:]}")
    found = json.loads(done.stdout.strip().splitlines()[-1])
    journal = subprocess.run(["journalctl", "--user", "--since", since, "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=120, check=False)
    found["journal"] = journal.returncode == 0 and value in journal.stdout
    return found


def _claim_setup(ctx, broken: bool, name: str, manifest: dict, server: str, planted: str,
                 before_probe=None) -> tuple[list[str], int]:
    """The broken form's edit loaded by a restart, ``before_probe`` run while no tool of
    the rig declares anything for the exit, then the probe installed with its
    planted instance and enabled for the agent; what the teardown needs."""
    if broken:
        ctx.break_product("services/mcp/mcp_session_config.py", _EXIT_DOCSTRING_END, _EXIT_NONE)
        _restart(ctx)
    if before_probe is not None:
        before_probe()
    _install_probe(ctx, name, manifest, server)
    made = ctx.owner.ok("POST", f"/v1/admin/mcps/{name}/instances",
                        {"instance_name": "rig", "field_values": {PROBE_KEY: planted}, "agents": [ctx.agent]})
    check(isinstance(made, dict) and made.get("id") is not None, f"the instance: {str(made)[:200]}")
    enabled_before = _enabled_mcps(ctx)
    ctx.owner.ok("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": [*enabled_before, name]})
    return enabled_before, int(made["id"])


def _claim_teardown(ctx, broken: bool, name: str, enabled_before: list[str] | None,
                    instance_id: int | None) -> None:
    with contextlib.suppress(Exception):
        if enabled_before is not None:
            ctx.owner.call("PUT", f"/v1/agents/{ctx.agent}/mcps", {"mcps": enabled_before})
        if instance_id is not None:
            ctx.owner.call("DELETE", f"/v1/admin/mcps/{name}/instances/{instance_id}")
        ctx.owner.call("DELETE", f"/v1/admin/mcps/{name}", timeout=120)
    if broken:
        ctx.mend_product()
        wait_ready(ctx)


def _probe_turn(ctx, target: str, planted: str) -> dict:
    """One Claude Code turn that calls the probe, on the machine ``target``
    (the compute service when none is named), and its answer."""
    engine, model = ENGINES["claude"]
    words = "Call the probe tool once, then reply with one word."
    prompt = words if ctx.state.get("real_models") else f"{words} {script({'tool': 'probe', 'input': {}}, {'text': 'Probed.'})}"
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt, mode="dontAsk", target=target)
    try:
        chat.wait_done(timeout=240)
        rows = chat.rows()
        if target:
            # A chat's row names its machine as its execution target (it has no machine id).
            from scenarios.s20_turns import _ran_on
            _ran_on(ctx, chat.chat_id, target)
        return _probe_answer(ctx, rows, planted)
    finally:
        chat.close()


@scenario("claim-exit-no-value-on-a-paired-machine", layer="claims", needs=("machine-pair", "turn-claude"),
          needs_rig=True, machine=True)
def exit_no_value_on_a_paired_machine(ctx, broken: bool = False):
    """A session on the paired Linux machine (this host) gets a
    placeholder in place of the value an admin typed, and the tool's process the
    variables naming the machine's own exit; the value is in no process, file or
    journal line of the machine, and the tool's request carrying the placeholder in
    its URL toward the declared host is refused at the machine's exit before any
    dial. Broken: the session's config hands the tool the value itself."""
    from run import rig_home
    ctx.require_rig()
    planted = "rig-exit-" + secrets.token_hex(16)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
    enabled_before: list[str] | None = None
    instance_id: int | None = None
    try:
        enabled_before, instance_id = _claim_setup(ctx, broken, PROBE, PROBE_MANIFEST, PROBE_SERVER, planted)
        # The broken form's restart of the server sends the machine into its reconnect wait.
        from scenarios.s50_machine import machine_ready
        machine_ready(ctx)
        answer = _probe_turn(ctx, ctx.state["machine_id"], planted)
        # The session is still warm when the probe answers: the search runs on
        # its machine while the tool's process lives.
        found = _host_search(planted, [str(rig_home() / ".oto-dock")], since)
        value = str(answer.get("env_value", ""))
        check(value != planted, "the tool's environment on the machine holds the value the admin typed")
        check(PLACEHOLDER.fullmatch(value), f"the tool's environment holds {value[:9]!r}... ({len(value)} characters), no placeholder")
        check(answer.get("https_proxy") is True, "the tool's environment names no exit on the machine")
        status, body = answer.get("status"), str(answer.get("body", ""))
        check(status == 400 and REFUSAL_PLACE in body,
              f"the GET with the placeholder in its URL answered {status}: {_hide(body, planted)[:200]!r}")
        check(not found["processes"], f"the value is in a process of the machine: {found['processes']}")
        check(not found["files"], f"the value is in a file of the satellite's folder: {found['files']}")
        check(not found["journal"], "the value is in the machine's journal")
        inside = _inside(ctx, planted)
        check(not inside["files"], f"the value is in a file of the install: {inside['files']}")
    finally:
        _claim_teardown(ctx, broken, PROBE, enabled_before, instance_id)
    return (f"on the paired machine a placeholder in the tool's environment, the machine's exit named, "
            f"the GET toward {PROBE_HOST} refused 400 there, the value in no process, file or journal line")


#: An HTTPS host no tool declares: a session's request to it goes through the
#: machine's exit as a plain tunnel and answers from the far side.
PLAIN_HTTPS = "https://example.com/"


def _plain_https_answers(ctx) -> None:
    """A compute session whose tools declare nothing for the exit still
    reaches an HTTPS host: its door has no entry, a plain tunnel, and no
    request is refused 407 for want of one."""
    engine, model = ENGINES["claude"]
    command = f"curl -sS -o /dev/null -w 'status=%{{http_code}}' {PLAIN_HTTPS} 2>&1 || true"
    words = "Run one shell command, then reply with one word."
    prompt = (f"Run exactly this shell command, then reply with one word: {command}" if ctx.state.get("real_models")
              else f"{words} {script({'tool': 'Bash', 'input': {'command': command, 'description': 'A request'}}, {'text': 'Done.'})}")
    chat = ctx.owner.chat(ctx.agent, engine=engine, model=model, text=prompt, mode="dontAsk")
    try:
        chat.wait_done(timeout=240)
        steps = tool_rows(chat.rows(), "Bash")
    finally:
        chat.close()
    if not steps:
        if ctx.state.get("real_models"):
            raise Inconclusive("the model did not run the request")
        raise CheckFailed("the request left no row")
    said = str(steps[0].get("tool_result", ""))
    if "status=" not in said and ctx.state.get("real_models"):
        raise Inconclusive(f"the model ran another command: {said[:120]!r}")
    check("status=" in said and "status=000" not in said and "407" not in said,
          f"a session with no declared tool could not reach {PLAIN_HTTPS} through the exit: {said[:200]!r}")


@scenario("claim-exit-no-value-on-compute", layer="claims", needs=("compute-sessions", "turn-claude"), needs_rig=True)
def exit_no_value_on_compute(ctx, broken: bool = False):
    """A session on the compute service whose tools declare
    nothing for the exit still reaches an HTTPS host through it; a session with the
    probe gets a placeholder and the variables naming the compute machine's exit;
    the value is in no process or file of the compute service's container, and the
    exit dials nothing the session's sandbox does not reach: the machine's loopback,
    the metadata address and a private address outside the carves are each
    refused. Broken: the session's config hands the tool the value itself."""
    ctx.require_rig()
    planted = "rig-exit-" + secrets.token_hex(16)
    enabled_before: list[str] | None = None
    instance_id: int | None = None
    try:
        enabled_before, instance_id = _claim_setup(ctx, broken, DIALS, DIALS_MANIFEST, DIALS_SERVER, planted,
                                                   before_probe=lambda: _plain_https_answers(ctx))
        answer = _probe_turn(ctx, "", planted)
        inside = _inside(ctx, planted, "otodock-compute", COMPUTE_ROOTS)
        value = str(answer.get("env_value", ""))
        check(value != planted, "the tool's environment on compute holds the value the admin typed")
        check(PLACEHOLDER.fullmatch(value), f"the tool's environment holds {value[:9]!r}... ({len(value)} characters), no placeholder")
        check(answer.get("https_proxy") is True, "the tool's environment names no exit on compute")
        dials = answer.get("dials") or {}
        for target in DIAL_TARGETS:
            said = str(dials.get(target, ""))
            check("403" in said, f"the exit answered the CONNECT toward {target} with {said[:160]!r}, no refusal")
        check(not inside["processes"], f"the value is in a process of the compute service: {inside['processes']}")
        check(not inside["files"], f"the value is in a file of the compute service: {inside['files']}")
        logs = ctx.compose("logs", "--no-color", "otodock-compute", timeout=120)
        check(logs.returncode == 0, f"the compute service's log could not be read: {logs.stderr[-300:]}")
        check(planted not in logs.stdout, "the value is in the compute service's log")
    finally:
        _claim_teardown(ctx, broken, DIALS, enabled_before, instance_id)
    return ("on compute a placeholder in the tool's environment, the machine's exit named, three addresses "
            "refused at it, the value in no process, file or log line of the compute service")
