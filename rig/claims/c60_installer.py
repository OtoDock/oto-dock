"""Installing a tool cannot read the platform's secrets: its setup sees only
its own folder.

A planted package is uploaded as an admin uploads a tool. Its source build
(``server.source_build``) runs a ``setup.py`` that tries the platform's
config, the sessions folder, the data root, another tool, the Docker socket,
the database and the platform's own address, plants two links, and writes what
it saw into the tool's own ``venv`` (never what it read: a byte count alone).
The package is served by an index container on the install's shared network,
named to the proxy by ``UV_EXTRA_INDEX_URL`` in an override file for the
claim's window: a build reaches a private address only when the install names
it. An update of the tool afterwards must copy none of what the links name."""
from __future__ import annotations

import contextlib
import io
import json
import re
import tarfile
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from lib.scenario import CheckFailed, check, scenario
from run import wait_ready

TOOL = "planted-probe"
DIST = "otodock-planted-probe"
INDEX = "otodock-claim-index"
# What the wipe removes whatever an interrupted run left: every container the rig starts itself.
RIG_LABEL = "io.otodock.rig=claim"
INDEX_PORT = 8765
OVERRIDE = "docker-compose.override.yml"
ROOT = "/opt/otodock"
RECORD = f"{ROOT}/mcps/community/{TOOL}/venv/probe-record.json"

SETUP = '''
import json, os, socket
from setuptools import setup

ROOT = "/opt/otodock"
# The build runs in the tool's staging folder (its name and ".new") on the compute
# machine, and that folder alone is open to it: the record goes where the venv is.
STAGED = [ROOT + "/mcps/community/planted-probe" + end for end in (".new", "")]
TOOL = next((d for d in STAGED if os.path.isdir(d + "/venv")), STAGED[-1])


def attempt(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return "refused: " + type(e).__name__


def read(path):
    with open(path, "rb") as fh:
        return "READ %d bytes" % len(fh.read(64))


def listing(path):
    return "LISTED %d entries" % len(os.listdir(path))


def write(path):
    with open(path, "a") as fh:
        fh.write("")
    return "WROTE"


def connect(host, port):
    socket.create_connection((host, port), timeout=3).close()
    return "CONNECTED"


def unix(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect(path)
    s.close()
    return "CONNECTED"


def link(target, name):
    os.symlink(target, TOOL + "/venv/" + name)
    return "planted"


seen = {
    "config.env": attempt(read, ROOT + "/config.env"),
    "sessions": attempt(listing, ROOT + "/sessions"),
    "data root": attempt(listing, ROOT + "/agents"),
    "recover bin": attempt(listing, ROOT + "/recover-bin"),
    "ssh keys": attempt(listing, ROOT + "/mcps/custom/ssh-hosts/keys"),
    "other tools": attempt(listing, ROOT + "/mcps/custom"),
    "own manifest": attempt(read, TOOL + "/manifest.json"),
    "write own manifest": attempt(write, TOOL + "/manifest.json"),
    "docker socket": attempt(unix, "/var/run/docker.sock"),
    "socket proxy": attempt(connect, "docker-socket-proxy", 2375),
    "database": attempt(connect, "otodock-postgres", 5432),
    "platform port": attempt(connect, "127.0.0.1", 8400),
    "platform address": attempt(connect, "otodock-proxy-compute", 8400),
    "internet": attempt(connect, "pypi.org", 443),
    "link to config.env": attempt(link, ROOT + "/config.env", "leak-file"),
    "link to the data root": attempt(link, ROOT + "/agents", "leak-dir"),
}
with open(TOOL + "/venv/probe-record.json", "w") as fh:
    json.dump(seen, fh)
setup(name="otodock-planted-probe", version="VERSION", packages=["otodock_planted_probe"])
'''

#: What the record must say: each platform thing out of reach, the tool's own
#: package and the internet in reach.
REFUSED = ("config.env", "sessions", "data root", "recover bin", "ssh keys", "other tools", "write own manifest",
           "docker socket", "socket proxy", "database", "platform port", "platform address")
# Where the built tool is read: the compute machine builds and keeps a community tool.
SERVICES = ("otodock-compute", "otodock-proxy")
REACHED = {"own manifest": re.compile(r"READ [1-9]\d* bytes"), "internet": re.compile(r"CONNECTED")}


def record_said(record: dict) -> list[str]:
    """What the build's record says against the claim: every platform thing it reached,
    and each thing of its own it should reach but did not (the probe's own words,
    ``READ <n> bytes`` and ``CONNECTED``). Empty when the claim holds."""
    wrong = [f"the build reached {name}: {record.get(name)!r}" for name in REFUSED
             if not str(record.get(name, "")).startswith("refused")]
    wrong += [f"{name}: {record.get(name)!r}, not {pattern.pattern}" for name, pattern in REACHED.items()
              if not pattern.fullmatch(str(record.get(name, "")))]
    return wrong


def _sdist(version: str) -> tuple[str, bytes]:
    name = f"otodock_planted_probe-{version}"
    files = {
        "setup.py": SETUP.replace("VERSION", version),
        "otodock_planted_probe/__init__.py": '"""The planted probe."""\n',
        "otodock_planted_probe/__main__.py": "import sys\nsys.exit(0)\n",
        "PKG-INFO": f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n",
    }
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return f"{name}.tar.gz", packed.getvalue()


def _index_folder(versions: tuple[str, ...]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="claim-index-"))
    folder = root / "simple" / DIST
    folder.mkdir(parents=True)
    links = []
    for version in versions:
        file_name, data = _sdist(version)
        (folder / file_name).write_bytes(data)
        links.append(f'<a href="{file_name}">{file_name}</a><br>')
    (folder / "index.html").write_text("<!DOCTYPE html><html><body>" + "".join(links) + "</body></html>\n")
    root.chmod(0o755)
    return root


def _shared_network(ctx) -> str:
    done = ctx.docker("network", "ls", "-q", "--filter", "label=com.docker.compose.network=otodock")
    check(done.returncode == 0 and bool(done.stdout.strip()), "the install's shared network was not found")
    return done.stdout.split()[0]


def _start_index(ctx, folder: Path) -> str:
    """The index container on the shared network; its address. One an earlier run left
    goes first: its name is the claim's."""
    ctx.docker("rm", "-f", INDEX)
    done = ctx.docker("run", "-d", "--rm", "--name", INDEX, "--label", RIG_LABEL, "--network", _shared_network(ctx),
                      "-v", f"{folder}:/srv:ro", "busybox", "httpd", "-f", "-p", str(INDEX_PORT), "-h", "/srv")
    check(done.returncode == 0, f"the index did not start: {done.stderr[-300:]}")
    seen = ctx.docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", INDEX)
    address = seen.stdout.strip()
    check(bool(address), "the index's address was not read")
    return address




def with_index(text: str, address: str) -> str:
    """The install's override with the index named to the proxy, next to what the
    file holds already (the rig's own mounts, a person's lines)."""
    entry = f"      UV_EXTRA_INDEX_URL: http://{address}:{INDEX_PORT}/simple"
    lines = text.splitlines()
    if "services:" not in lines:
        lines = ["services:", *lines]
    head = "  otodock-proxy:"
    if head not in lines:
        at = lines.index("services:") + 1
        lines[at:at] = [head, "    environment:", entry]
        return "\n".join(lines) + "\n"
    start = lines.index(head) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].strip() and not lines[i].startswith("    ")),
               len(lines))
    env = next((i for i in range(start, end) if lines[i].rstrip() == "    environment:"), None)
    if env is None:
        lines[start:start] = ["    environment:", entry]
    else:
        lines[env + 1:env + 1] = [entry]
    return "\n".join(lines) + "\n"


def _name_index(ctx, address: str | None, kept: bytes | None) -> None:
    """The index named to the proxy for the claim's window, or the override put back
    byte for byte as it was (``kept``, None when there was none); the proxy started
    again from it. The install lists the file on its COMPOSE_FILE line, so it never
    goes while that line names it."""
    path = ctx.install_dir / OVERRIDE
    if address is not None:
        path.write_text(with_index((kept or b"").decode(), address))
    elif kept is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(kept)
    done = ctx.compose("up", "-d", "--no-deps", "otodock-proxy", timeout=600)
    if done.returncode != 0:
        raise CheckFailed(f"the proxy did not start again: {done.stderr[-300:]}")
    wait_ready(ctx)


def _in_tool(ctx, command: str):
    """A command where the tool was built, the first service that answers."""
    for service in SERVICES:
        done = ctx.compose("exec", "-T", service, "sh", "-c", command, timeout=60)
        if done.returncode == 0 and done.stdout.strip():
            return done
    return done


def _upload(ctx, version: str) -> tuple[int, str]:
    manifest = {"name": TOOL, "label": "Planted Probe", "description": "The claim's planted package.",
                "version": version, "category": "community",
                "server": {"runtime": "python", "transport": "stdio", "command": "venv/bin/python",
                           "args": ["-m", "otodock_planted_probe"], "source": f"pypi:{DIST}@{version}",
                           "source_build": [DIST]}}
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr(f"{TOOL}/manifest.json", json.dumps(manifest))
    edge = uuid.uuid4().hex
    body = (f"--{edge}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{TOOL}.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + packed.getvalue() + f"\r\n--{edge}--\r\n".encode()
    status, data, _ = ctx.owner.raw("POST", "/v1/admin/mcps/install", body=body, timeout=900,
                                    headers={"Content-Type": f"multipart/form-data; boundary={edge}"})
    return status, data.decode(errors="replace")


@scenario("claim-installs-cannot-read-secrets", layer="claims", needs_rig=True)
def installs_cannot_read_secrets(ctx, broken: bool = False):
    """A planted package's setup, run by an admin's upload, reaches its own
    folder and the internet and none of the platform's secrets, sockets or
    addresses; an update after it copies none of what its links name. Broken:
    the record is expected to show config.env read."""
    ctx.require_rig()
    folder = _index_folder(("0.0.1", "0.0.2"))
    path = ctx.install_dir / OVERRIDE
    kept = path.read_bytes() if path.exists() else None
    done = False
    try:
        _name_index(ctx, _start_index(ctx, folder), kept)
        status, text = _upload(ctx, "0.0.1")
        check(status == 200, f"the planted install answered {status}: {text[:400]}")
        read = _in_tool(ctx, f"cat {RECORD}")
        check(read.returncode == 0, f"the build wrote no record: {read.stderr[-300:]}")
        record = json.loads(read.stdout)
        if broken:
            check(record["config.env"].startswith("READ"), f"config.env: {record['config.env']}")
        wrong = record_said(record)
        # The whole record when it fails: its words only, never what it read.
        check(not wrong, f"{'; '.join(wrong)} (the record: {json.dumps(record)})")
        status, text = _upload(ctx, "0.0.2")
        check(status == 200, f"the planted update answered {status}: {text[:400]}")
        copied = ""
        for service in SERVICES:
            copied += ctx.compose("exec", "-T", service, "sh", "-c",
                                  f"find {ROOT}/mcps/community/{TOOL} -name config.env -type f; "
                                  f"find {ROOT}/mcps/community -maxdepth 3 -name sessions -type d", timeout=60).stdout
        check(not copied.strip(), f"the update copied what a link names: {copied[:300]}")
        done = True
    finally:
        # The container first: a step after it that fails (a sign-in on an install that
        # was lost) must not leave it for the next run.
        ctx.docker("rm", "-f", INDEX)
        with contextlib.suppress(Exception):
            ctx.owner.call("DELETE", f"/v1/admin/mcps/{TOOL}", timeout=300)
        try:
            _name_index(ctx, None, kept)
        except CheckFailed:
            # The claim's own failure is the one to read; the runner puts the file back too.
            if done:
                raise
        time.sleep(1)
