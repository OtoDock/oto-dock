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

``exec`` and ``run`` run the command the script asked for, with every path
under a mount rewritten to the host path standing in for it, so the tree
programs the scripts stream in run for real against test directories.
"""

from __future__ import annotations

from pathlib import Path

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
    real = [mapped(a, mounts) for a in cmd]
    if real[0] == "python3":
        real[0] = shutil.which("python3") or sys.executable
    return subprocess.call(real)


if args[:1] == ["ps"]:
    ps(args[1:])
    sys.exit(0)
if args[:1] == ["exec"]:
    rest = [a for a in args[1:] if a != "-i"]
    c = by_id.get(rest[0], {})
    sys.exit(run_cmd(rest[1:], c.get("mounts", {})))
if args[:1] == ["inspect"]:
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    c = by_id.get(args[-1])
    if c is None:
        sys.exit(1)
    status = c.get("status") or ("running" if c.get("running", True) else "exited")
    if ".Id" in fmt and ".State.Status" in fmt:
        print(f"{c['id']} {status}")
    elif ".State.Status" in fmt:
        print(status)
    elif ".Image" in fmt:
        print(c.get("image", "fakeimage"))
    elif ".Mounts" in fmt:
        m = re.search(r'eq \.Destination "([^"]+)"', fmt)
        mount = c.get("mounts", {}).get(m.group(1) if m else "", {})
        print(mount.get("name", "") if ".Name" in fmt else mount.get("source", ""))
    sys.exit(0)
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
