"""The operator scripts, run against a fake ``docker`` on PATH.

backup.sh and restore.sh pick the platform's own Postgres and proxy
containers (compose labels, else the bare-metal container_name matched
whole) and refuse to guess between two; a community MCP container whose free
name merely contains the text is never chosen. backup.sh's apps copy opens
every app database read-only whatever its file name. install.sh and
compose.sh create the secrets file private from its first byte.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"

# A stand-in for the docker CLI: `ps` applies the daemon's filter semantics
# (every label filter must match; any name filter, a regex searched in the
# name with and without its leading slash); `exec` logs the container id and
# answers like pg_dump, psql or the apps copy would.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
cs = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
log = os.environ["FAKE_LOG"]
if args[:1] == ["ps"]:
    labels, names = [], []
    it = iter(args[1:])
    for a in it:
        if a == "--filter":
            f = next(it)
            k, _, v = f.partition("=")
            (labels if k == "label" else names).append(v)
    for c in cs:
        ok = all(c["labels"].get(l.partition("=")[0]) == l.partition("=")[2] for l in labels)
        if names:
            ok = ok and any(re.search(n, c["name"]) or re.search(n, "/" + c["name"]) for n in names)
        if ok:
            print(c["id"])
    sys.exit(0)
if args[:1] == ["exec"]:
    rest = [a for a in args[1:] if a != "-i"]
    with open(log, "a") as fh:
        fh.write(rest[0] + " " + " ".join(rest[1:3]) + "\n")
    if "pg_dump" in rest:
        print("-- dump")
    elif "-i" in args:
        sys.stdin.read()
    sys.exit(0)
if args[:2] in (["compose", "version"], ["info"]):
    sys.exit(0)
sys.exit(0)
'''

PG_T2 = {"id": "pg-t2", "name": "otodock-otodock-postgres-1",
         "labels": {"com.docker.compose.project": "otodock",
                    "com.docker.compose.service": "otodock-postgres"}}
PG_T1 = {"id": "pg-t1", "name": "otodock-postgres",
         "labels": {"com.docker.compose.project": "oto-dock",
                    "com.docker.compose.service": "postgres"}}
PROXY_T2 = {"id": "proxy-t2", "name": "otodock-otodock-proxy-1",
            "labels": {"com.docker.compose.project": "otodock",
                       "com.docker.compose.service": "otodock-proxy"}}
# community MCP containers are named otodock-<install_id>-mcp-<manifest name>
EVIL_PG = {"id": "evil-pg", "name": "otodock-abcd1234-mcp-otodock-postgres-x",
           "labels": {"com.docker.compose.project": "otodock-abcd1234-mcp-otodock-postgres-x",
                      "com.docker.compose.service": "otodock-postgres"}}
EVIL_PROXY = {"id": "evil-proxy", "name": "otodock-abcd1234-mcp-otodock-proxy-x",
              "labels": {"com.docker.compose.project": "otodock-abcd1234-mcp-otodock-proxy-x",
                         "com.docker.compose.service": "otodock-proxy"}}


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("docker", FAKE_DOCKER),
                       ("curl", "#!/bin/sh\nexit 7\n")):
        p = b / name
        p.write_text(body)
        p.chmod(0o755)
    return b


def _env(tmp_path, fakebin, containers, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "OTODOCK_", "PLATFORM_"))}
    env.update({
        "PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(containers),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "OTODOCK_BACKUP_DIR": str(tmp_path / "backups"),
        "PORT": "1",
    })
    env.update(extra)
    return env


def _run(script, tmp_path, fakebin, containers, *args, stdin="", **extra):
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *args], cwd=tmp_path, input=stdin,
        env=_env(tmp_path, fakebin, containers, **extra),
        capture_output=True, text=True, timeout=60,
    )


def _exec_ids(tmp_path) -> list[str]:
    log = tmp_path / "docker.log"
    return [line.split()[0] for line in log.read_text().splitlines()] if log.exists() else []


# ── container selection ────────────────────────────────────────────────

@pytest.mark.parametrize("containers, chosen", [
    ([EVIL_PG, PG_T2], "pg-t2"),        # the newest match is a community MCP
    ([EVIL_PG, PG_T1], "pg-t1"),        # bare metal: the whole container_name
    ([PG_T2], "pg-t2"),
])
def test_backup_dumps_the_platform_postgres(tmp_path, fakebin, containers, chosen):
    r = _run("backup.sh", tmp_path, fakebin, containers)
    assert r.returncode == 0, r.stderr
    assert _exec_ids(tmp_path) == [chosen]


def test_backup_refuses_a_lookalike_alone(tmp_path, fakebin):
    r = _run("backup.sh", tmp_path, fakebin, [EVIL_PG])
    assert r.returncode != 0
    assert _exec_ids(tmp_path) == []
    assert not any((tmp_path / "backups").glob("*.sql.gz"))


def test_backup_refuses_two_platform_postgres(tmp_path, fakebin):
    twin = {**PG_T2, "id": "pg-t2b", "name": "otodock-otodock-postgres-2"}
    r = _run("backup.sh", tmp_path, fakebin, [PG_T2, twin])
    assert r.returncode != 0
    assert "more than one" in r.stderr
    assert _exec_ids(tmp_path) == []


def test_backup_apps_reads_the_platform_proxy(tmp_path, fakebin):
    r = _run("backup.sh", tmp_path, fakebin, [EVIL_PROXY, PROXY_T2, PG_T2], "--apps")
    assert r.returncode == 0, r.stderr
    assert _exec_ids(tmp_path) == ["pg-t2", "proxy-t2"]


def test_backup_apps_refuses_two_platform_proxies(tmp_path, fakebin):
    twin = {**PROXY_T2, "id": "proxy-t2b", "name": "otodock-otodock-proxy-2"}
    r = _run("backup.sh", tmp_path, fakebin, [PROXY_T2, twin, PG_T2], "--apps")
    assert r.returncode != 0
    assert "more than one" in r.stderr
    assert "proxy-t2" not in _exec_ids(tmp_path)


@pytest.mark.parametrize("containers, chosen", [
    ([EVIL_PG, PG_T2], "pg-t2"),
    ([EVIL_PG, PG_T1], "pg-t1"),
])
def test_restore_feeds_the_platform_postgres(tmp_path, fakebin, containers, chosen):
    dump = tmp_path / "d.sql"
    dump.write_text("SELECT 1;\n")
    r = _run("restore.sh", tmp_path, fakebin, containers, str(dump), stdin="yes\n")
    assert r.returncode == 0, r.stderr
    assert set(_exec_ids(tmp_path)) == {chosen}


def test_restore_refuses_a_lookalike_or_two(tmp_path, fakebin):
    dump = tmp_path / "d.sql"
    dump.write_text("SELECT 1;\n")
    for containers in ([EVIL_PG], [PG_T2, {**PG_T2, "id": "pg-t2b"}]):
        r = _run("restore.sh", tmp_path, fakebin, containers, str(dump), stdin="yes\n")
        assert r.returncode != 0
        assert _exec_ids(tmp_path) == []


# ── the apps copy opens every database read-only ───────────────────────

@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 not on PATH")
def test_apps_copy_opens_odd_file_names_read_only(tmp_path, fakebin):
    agents = tmp_path / "agents"
    data = agents / "helper" / "app-data"
    data.mkdir(parents=True)
    other = agents / "other" / "app-data"
    other.mkdir(parents=True)
    names = ["x#.db", "y?mode=rwc.db", "z%2F..%2F..%2Fother%2Fapp-data%2Fsecret.db"]
    for n in names:
        con = sqlite3.connect(str(data / n))
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES (?)", (n,))
        con.commit()
        con.close()
    con = sqlite3.connect(str(other / "secret.db"))
    con.execute("CREATE TABLE s (v TEXT)")
    con.execute("INSERT INTO s VALUES ('other agent')")
    con.commit()
    con.close()
    before = sorted(p.name for p in data.iterdir())
    r = _run("backup.sh", tmp_path, fakebin, [PG_T2], "--apps",
             OTODOCK_AGENTS_DIR=str(agents))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in data.iterdir()) == before  # nothing created
    (archive,) = (tmp_path / "backups").glob("otodock-apps-*.tar.gz")
    out = tmp_path / "out"
    with tarfile.open(archive) as tf:
        tf.extractall(out, filter="data")
    for n in names:
        con = sqlite3.connect(str(out / "helper" / "app-data" / n))
        assert con.execute("SELECT v FROM t").fetchall() == [(n,)]
        con.close()


# ── the secrets file is private from its first byte ────────────────────

def _no_chmod_bin(fakebin: Path) -> Path:
    # chmod after the write would mask a file created world-readable
    p = fakebin / "chmod"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return fakebin


def test_install_writes_env_private(tmp_path, fakebin):
    _no_chmod_bin(fakebin)
    inst = tmp_path / "inst"
    inst.mkdir()
    env = _env(tmp_path, fakebin, [], HOME=str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    r = subprocess.run(
        ["bash", "-c", f"umask 022; exec bash {SCRIPTS / 'install.sh'}"],
        cwd=inst, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert (inst / ".env").is_file(), r.stdout + r.stderr
    assert stat.S_IMODE((inst / ".env").stat().st_mode) == 0o600
    assert "POSTGRES_PASSWORD=" in (inst / ".env").read_text()


def test_compose_hint_creates_config_env_private(tmp_path, fakebin):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    for name in ("compose.sh", "versions.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    r = subprocess.run(["bash", str(root / "scripts" / "compose.sh"), "ps"],
                       cwd=root, env=_env(tmp_path, fakebin, []),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    hint = next(line.strip() for line in r.stderr.splitlines() if "> config.env" in line)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["bash", "-c", f"umask 022; {hint}"], cwd=work, check=True,
                   env=_env(tmp_path, fakebin, []), timeout=60)
    assert stat.S_IMODE((work / "config.env").stat().st_mode) == 0o600
    assert (work / "config.env").read_text().startswith("POSTGRES_PASSWORD=")
