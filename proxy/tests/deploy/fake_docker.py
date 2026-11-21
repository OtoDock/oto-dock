"""A stand-in for the docker CLI, shared by the operator-script tests
(backup.sh, restore.sh). Written to a bin directory by ``write_fake``; the
scripts run with that directory first on PATH, no daemon.

The fake reads its world from the environment:

* ``FAKE_CONTAINERS``: a JSON list of ``{id, name, labels, running, status,
  image, mounts}``; ``status`` is what ``inspect`` reports for
  ``.State.Status`` (default ``running`` or ``exited`` from ``running``);
  ``mounts`` maps a destination inside the container to ``{name, source}``
  (a named volume's name, the host path that stands in for it in the test).
  ``ps`` applies the daemon's filter semantics (every label filter must
  match; any name filter, a regex searched in the name with and without its
  leading slash; ``-a`` includes a stopped one). ``inspect`` takes an id or
  a name.
* ``FAKE_LOG``: every argv appended as one JSON line.
* ``FAKE_DB_VERSION``, ``FAKE_KEYS``, ``FAKE_FLOOR``: what ``psql -tAc``
  answers for the schema_meta row and the two fingerprint settings.
* ``FAKE_PSQL_FAIL``: a ``psql`` call whose argv carries this text fails
  (exit 1, one line on stderr); ``FAKE_PG_DUMP_RC``: pg_dump's exit status.
* ``FAKE_IMAGE_VERSION``: the ``OTODOCK_VERSION`` the image's VERSIONS.md
  carries (the ``sed`` read inside the image).
* ``FAKE_DU_K``: what ``du -sk`` inside the image answers, when set.
* ``FAKE_VOLUMES``: a JSON map of volume name to the host directory that
  stands in for it in a one-off ``docker run -v name:path``.
* ``FAKE_API_VERSION`` and ``FAKE_COMPOSE_VERSION``: what ``docker version``
  and ``docker compose version --short`` answer (1.51 and 2.35.1).
  ``docker compose ps -q <service>`` lists the running containers of that
  service; ``inspect`` of a health format answers a container's ``health``
  (healthy).

``exec`` and ``run`` run the command the script asked for, with every path
under a mount rewritten to the host path standing in for it, so the tree
programs the scripts stream in run for real against test directories;
``exec -e K=V`` sets the variable for that command; the admin command line
(``admin_cli.py <verb>``) prints ``FAKE_ADMIN_<VERB>`` and exits
``FAKE_ADMIN_<VERB>_RC`` when ``FAKE_ADMIN_<VERB>`` is set (else it runs);
``FAKE_ADMIN_<VERB>_SIGNAL`` (a signal name) first sends that signal to the
script that called docker, as a Ctrl+C or a ``timeout`` in that verb would. ``stop`` and ``start`` exit ``FAKE_STOP_RC`` and
``FAKE_START_RC``. ``inspect -f`` answers a label with
``index .Config.Labels "<key>"``.

``write_host_fakes`` writes a refusing fake of every host command
(``HOST_COMMANDS``) and ``host_commands_named`` reads which of them a script
names: a suite that runs scripts with the host's PATH behind its fakes holds
the two to each other in a guard test of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

# The commands that act on the host itself (its services, its sessions, its
# root account). A script a test runs may name one only where the test's PATH
# holds a fake of it: the real one does not read HOME or a scratch folder.
HOST_COMMANDS = ("systemctl", "loginctl", "sudo")
# A host command no test here means to reach: the call is logged beside the
# fake and nothing runs. A read of a unit's state (systemctl is-active) acts
# on nothing: it answers "inactive" and is not logged.
HOST_CALLS_LOG = "host-calls.log"
FAKE_HOST_COMMAND = f'''#!/bin/sh
[ "${{0##*/}}" = systemctl ] && [ "$1" = is-active ] && exit 3
echo "${{0##*/}} $*" >> "${{0%/*}}/{HOST_CALLS_LOG}"
exit 97
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, shutil, subprocess, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\n")
cs = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
by_id = {c["id"]: c for c in cs}
by_id.update({c["name"]: c for c in cs if c.get("name")})
volumes = json.loads(os.environ.get("FAKE_VOLUMES", "{}"))


def ps(rest):
    want_all = "-a" in rest
    labels, names = [], []
    it = iter(rest)
    for a in it:
        if a == "--filter":
            k, _, v = next(it).partition("=")
            (labels if k == "label" else names).append(v)
    for c in cs:
        if not want_all and not c.get("running", True):
            continue
        ok = all(c["labels"].get(l.partition("=")[0]) == l.partition("=")[2] for l in labels)
        if names:
            ok = ok and any(re.search(n, c["name"]) or re.search(n, "/" + c["name"]) for n in names)
        if ok:
            print(c["id"])


def mapped(path, mounts):
    for dest, m in sorted(mounts.items(), key=lambda kv: -len(kv[0])):
        src = m.get("source", "")
        if src and (path == dest or path.startswith(dest + "/")):
            return src + path[len(dest):]
    return path


def run_cmd(cmd, mounts):
    if not cmd:
        return 0
    if cmd[0] == "pg_dump":
        print("-- dump")
        return int(os.environ.get("FAKE_PG_DUMP_RC", "0"))
    if cmd[0] == "psql":
        bad = os.environ.get("FAKE_PSQL_FAIL")
        if bad and any(bad in a for a in cmd):
            print("ERROR:  the fake refused this statement", file=sys.stderr)
            return 1
        sql = cmd[cmd.index("-tAc") + 1] if "-tAc" in cmd else ""
        if "schema_meta" in sql:
            print(os.environ.get("FAKE_DB_VERSION", ""))
        elif "credential_key_fingerprints" in sql:
            print(os.environ.get("FAKE_KEYS", ""))
        elif "secret_floor" in sql:
            print(os.environ.get("FAKE_FLOOR", ""))
        elif "pg_get_userbyid" in sql:
            print("otodock")
        elif "rolname" in sql:
            print("")
        elif sql:
            print("1")
        elif "-c" not in cmd:
            sys.stdin.read()
        return 0
    if cmd[0] == "sed" and any(a.endswith("VERSIONS.md") for a in cmd):
        v = os.environ.get("FAKE_IMAGE_VERSION", "")
        if v:
            print(v)
        return 0
    if cmd[0] == "du" and os.environ.get("FAKE_DU_K"):
        print(os.environ["FAKE_DU_K"] + "\t/x")
        return 0
    at = [i for i, a in enumerate(cmd) if str(a).endswith("/admin_cli.py")]
    verb = str(cmd[at[0] + 1]) if at and at[0] + 1 < len(cmd) else ""
    if verb and f"FAKE_ADMIN_{verb.upper()}" in os.environ:
        sig = os.environ.get(f"FAKE_ADMIN_{verb.upper()}_SIGNAL")
        if sig:
            import signal
            os.kill(os.getppid(), getattr(signal, f"SIG{sig}"))
        print(os.environ[f"FAKE_ADMIN_{verb.upper()}"])
        return int(os.environ.get(f"FAKE_ADMIN_{verb.upper()}_RC", "0"))
    real = [mapped(a, mounts) for a in cmd]
    if real[0] == "python3":
        real[0] = shutil.which("python3") or sys.executable
    return subprocess.call(real)


if args[:1] == ["version"]:
    print(os.environ.get("FAKE_API_VERSION", "1.51"))
    sys.exit(0)
if args[:2] == ["compose", "version"] and "--short" in args:
    print(os.environ.get("FAKE_COMPOSE_VERSION", "2.35.1"))
    sys.exit(0)
if args[:1] == ["ps"]:
    ps(args[1:])
    sys.exit(0)
if args[:3] == ["compose", "ps", "-q"]:
    for c in cs:
        if c.get("running", True) and c["labels"].get("com.docker.compose.service") == args[3]:
            print(c["id"])
    sys.exit(0)
if args[:1] == ["exec"]:
    rest, env = [], {}
    it = iter(args[1:])
    for a in it:
        if a == "-i":
            continue
        if a == "-e":
            k, _, v = next(it).partition("=")
            env[k] = v
            continue
        rest.append(a)
    c = by_id.get(rest[0], {})
    os.environ.update(env)
    sys.exit(run_cmd(rest[1:], c.get("mounts", {})))
if args[:1] == ["inspect"]:
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    c = by_id.get(args[-1])
    if c is None:
        sys.exit(1)
    status = c.get("status") or ("running" if c.get("running", True) else "exited")
    if ".Id" in fmt and ".State.Status" in fmt:
        print(f"{c['id']} {status}")
    elif "Health" in fmt:
        print(c.get("health", "healthy"))
    elif ".State.Status" in fmt:
        print(status)
    elif ".Config.Labels" in fmt:
        m = re.search(r'index \.Config\.Labels "([^"]+)"', fmt)
        print(c["labels"].get(m.group(1), "") if m else "")
    elif ".Image" in fmt:
        print(c.get("image", "fakeimage"))
    elif ".Mounts" in fmt:
        m = re.search(r'eq \.Destination "([^"]+)"', fmt)
        mount = c.get("mounts", {}).get(m.group(1) if m else "", {})
        print(mount.get("name", "") if ".Name" in fmt else mount.get("source", ""))
    sys.exit(0)
if args[:1] in (["stop"], ["start"]):
    sys.exit(int(os.environ.get(f"FAKE_{args[0].upper()}_RC", "0")))
if args[:1] == ["run"]:
    rest, mounts, entry, i = args[1:], {}, None, 0
    while i < len(rest):
        a = rest[i]
        if a in ("--rm", "-i"):
            i += 1
        elif a == "--network":
            i += 2
        elif a == "-v":
            name, dest = rest[i + 1].split(":")[:2]
            mounts[dest] = {"source": volumes.get(name, name)}
            i += 2
        elif a == "--entrypoint":
            entry = rest[i + 1]
            i += 2
        else:
            break
    cmd = ([entry] if entry else []) + rest[i + 1:]
    sys.exit(run_cmd(cmd, mounts))
sys.exit(0)
'''


def write_fake(bindir: Path) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / "docker"
    path.write_text(FAKE_DOCKER)
    path.chmod(0o755)
    return path


def write_host_fakes(bindir: Path) -> None:
    """A refusing fake of every host command in ``bindir`` (first on the
    PATH a script runs with), where a test's own fake is not already."""
    bindir.mkdir(parents=True, exist_ok=True)
    for name in HOST_COMMANDS:
        path = bindir / name
        if not path.exists():
            path.write_text(FAKE_HOST_COMMAND)
            path.chmod(0o755)


def host_commands_named(script: Path) -> set[str]:
    """The host commands a script names outside its comments, as a command
    or in a message: either way the suite that runs it holds a fake."""
    code = "\n".join(line for line in script.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    return {name for name in HOST_COMMANDS if re.search(rf"(?<![\w-]){name}(?![\w-])", code)}


def host_calls(bindir: Path) -> list[str]:
    """Every call a refusing fake of ``bindir`` logged."""
    log = bindir / HOST_CALLS_LOG
    return log.read_text().splitlines() if log.exists() else []
