"""The operator scripts, run against a fake ``docker`` on PATH.

backup.sh and restore.sh pick the platform's own Postgres and proxy
containers (compose labels, else the bare-metal container_name matched
whole) and refuse to guess between two; a community MCP container whose free
name merely contains the text is never chosen. restore.sh refuses while the
platform answers on the port and address .env publishes it on. backup.sh's
apps copy opens every app database read-only whatever its file name.
install.sh and compose.sh create the secrets file private from its first
byte. dev-setup.sh stops before its first step while the otodock-proxy unit
is active (otodock-phone too with --phone).
"""
from __future__ import annotations

import json
import os
import re
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


# A stand-in for curl: logs the URL it was given and answers only the one
# FAKE_CURL_UP names (a refused connection otherwise).
FAKE_CURL = r'''#!/bin/sh
for a; do url="$a"; done
echo "$url" >> "$FAKE_CURL_LOG"
[ "$url" = "${FAKE_CURL_UP:-}" ] || exit 7
'''


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("docker", FAKE_DOCKER),
                       ("curl", FAKE_CURL)):
        p = b / name
        p.write_text(body)
        p.chmod(0o755)
    return b


def _env(tmp_path, fakebin, containers, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "OTODOCK_", "PLATFORM_", "PROXY_"))}
    env.update({
        "PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(containers),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
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


# ── restore.sh refuses while the platform answers ──────────────────────

def _put(folder: Path, files: dict) -> None:
    """Write ``{name: text}`` into ``folder``; a text of ``...`` makes a directory."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        if text is ...:
            (folder / name).mkdir()
        else:
            (folder / name).write_text(text)


def _restore_against(tmp_path, fakebin, up_url, cwd_files=None, install_files=None, **shell):
    """Run a copy of restore.sh from an install folder, in a separate working
    folder, with curl answering only ``up_url``; returns the run, the URLs
    probed and whether the dump was fed to psql. ``PORT`` is empty unless
    ``shell`` sets it (the harness sets it to 1)."""
    install, work = tmp_path / "install", tmp_path / "work"
    (install / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "restore.sh", install / "scripts" / "restore.sh")
    _put(install, install_files or {})
    _put(work, cwd_files or {})
    dump = tmp_path / "d.sql"
    dump.write_text("SELECT 1;\n")
    r = subprocess.run(
        ["bash", str(install / "scripts" / "restore.sh"), str(dump)], cwd=work, input="yes\n",
        env=_env(tmp_path, fakebin, [PG_T2], FAKE_CURL_UP=up_url, **{"PORT": "", **shell}),
        capture_output=True, text=True, timeout=60,
    )
    log = tmp_path / "curl.log"
    probed = log.read_text().split() if log.exists() else []
    docker_log = tmp_path / "docker.log"
    fed = docker_log.exists() and "psql -v" in docker_log.read_text()
    return r, probed, fed


@pytest.mark.parametrize("cwd_files, install_files, port", [
    ({".env": "PROXY_PORT=8410\n"}, {}, "8410"),
    ({".env": 'PROXY_PORT="8411"  # moved\n'}, {}, "8411"),
    ({".env": "PROXY_PORT=8400\nexport PROXY_PORT='8412'\n"}, {}, "8412"),
    ({".env": "# PROXY_PORT=8413\nPOSTGRES_DB=otodock\n"}, {}, "8400"),
    ({".env": ...}, {}, "8400"),
    ({}, {".env": "PROXY_PORT=8414\n"}, "8414"),
    ({".env": "PROXY_PORT=8415\n"}, {".env": "PROXY_PORT=8416\n"}, "8416"),
    ({".env": ...}, {".env": "PROXY_PORT=8417\n"}, "8417"),
    ({"config.env": "PROXY_PORT=8418\n"}, {}, "8418"),
    ({".env": "POSTGRES_DB=otodock\n"}, {"config.env": "PROXY_PORT=8419\n"}, "8419"),
    ({".env": "PROXY_PORT=8422\n"}, {"config.env": "PROXY_PORT=8423\n"}, "8423"),
    ({"config.env": "PROXY_PORT=8424\n"}, {".env": "POSTGRES_DB=otodock\n"}, "8424"),
])
def test_restore_probes_the_port_the_env_files_publish(
        tmp_path, fakebin, cwd_files, install_files, port):
    url = f"http://127.0.0.1:{port}/health"
    r, probed, fed = _restore_against(tmp_path, fakebin, url, cwd_files, install_files)
    assert r.returncode == 1, r.stderr
    assert "stop the proxy first" in r.stderr and port in r.stderr
    assert probed == [url]
    assert not fed


@pytest.mark.parametrize("shell, port", [
    ({"PROXY_PORT": "8420", "PORT": "8421"}, "8420"),
    ({"PORT": "8421"}, "8421"),
])
def test_restore_prefers_the_shell_port_to_the_env_files(tmp_path, fakebin, shell, port):
    url = f"http://127.0.0.1:{port}/health"
    r, probed, fed = _restore_against(
        tmp_path, fakebin, url, {".env": "PROXY_PORT=8410\n"}, **shell)
    assert r.returncode == 1, r.stderr
    assert probed == [url]
    assert not fed


@pytest.mark.parametrize("env_file, shell, hosts", [
    ("PROXY_PORT=8410\nPROXY_BIND_IP=192.0.2.5\n", {}, ["127.0.0.1", "192.0.2.5"]),
    ("PROXY_PORT=8410\n", {"PROXY_BIND_IP": "192.0.2.6"}, ["127.0.0.1", "192.0.2.6"]),
    ("PROXY_PORT=8410\nPROXY_BIND_IP=fd00::5\n", {}, ["127.0.0.1", "[fd00::5]"]),
    ("PROXY_PORT=8410\nPROXY_BIND_IP=[fd00::6]\n", {}, ["127.0.0.1", "[fd00::6]"]),
    ("PROXY_PORT=8410\nPROXY_BIND_IP=::\n", {}, ["127.0.0.1", "[::1]"]),
    ("PROXY_PORT=8410\nPROXY_BIND_IP=127.0.0.1\n", {}, ["127.0.0.1"]),
    ("PROXY_PORT=8410\nPROXY_BIND_IP=0.0.0.0\n", {}, ["127.0.0.1"]),
])
def test_restore_probes_the_bind_address_too(tmp_path, fakebin, env_file, shell, hosts):
    urls = ([f"http://{hosts[0]}:8410/health", "http://127.0.0.1:8400/health"]
            + [f"http://{h}:8410/health" for h in hosts[1:]])
    r, probed, fed = _restore_against(tmp_path, fakebin, urls[-1], {".env": env_file}, **shell)
    assert r.returncode == 1, r.stderr
    assert probed == urls
    assert not fed


def test_restore_probes_8400_besides_the_configured_port(tmp_path, fakebin):
    # A compose .env without PROXY_PORT publishes 8400 whatever config.env says.
    r, probed, fed = _restore_against(
        tmp_path, fakebin, "http://127.0.0.1:8400/health", {},
        {".env": "POSTGRES_DB=otodock\n", "config.env": "PROXY_PORT=8410\n"})
    assert r.returncode == 1, r.stderr
    assert "port 8400" in r.stderr
    assert probed == ["http://127.0.0.1:8410/health", "http://127.0.0.1:8400/health"]
    assert not fed


@pytest.mark.parametrize("env_file, probes", [
    ("PROXY_PORT=8410\nPROXY_BIND_IP=192.0.2.5\n",
     ["127.0.0.1:8410", "127.0.0.1:8400", "192.0.2.5:8410"]),
    ("PROXY_PORT=8400\n", ["127.0.0.1:8400"]),
])
def test_restore_runs_when_nothing_answers(tmp_path, fakebin, env_file, probes):
    r, probed, fed = _restore_against(tmp_path, fakebin, "", {".env": env_file})
    assert r.returncode == 0, r.stderr
    assert probed == [f"http://{p}/health" for p in probes]
    assert fed


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


def _install_function(name: str) -> str:
    text = (SCRIPTS / "install.sh").read_text()
    m = re.search(rf"^{name}\(\) \{{.*?^\}}\n", text, re.S | re.M)
    assert m, name
    return m.group(0)


@pytest.mark.parametrize("env_lines,shell,hinted", [
    ("DASHBOARD_PUBLIC_URL=https://otodock.example.com\n", {}, True),
    ('DASHBOARD_PUBLIC_URL="https://otodock.example.com"\nTRUSTED_PROXY=\n', {}, True),
    ("DASHBOARD_PUBLIC_URL=https://otodock.example.com\nTRUSTED_PROXY=10.200.0.1\n", {}, False),
    ("DASHBOARD_PUBLIC_URL=http://192.168.1.10:8400\n", {}, False),
    ("#DASHBOARD_PUBLIC_URL=https://x\n", {}, False),
    ("", {"DASHBOARD_PUBLIC_URL": "https://otodock.example.com"}, True),
    ("DASHBOARD_PUBLIC_URL=https://otodock.example.com\n", {"TRUSTED_PROXY": "10.0.0.2"}, False),
])
def test_install_names_trusted_proxy_for_an_https_url(tmp_path, env_lines, shell, hinted):
    (tmp_path / ".env").write_text(env_lines)
    script = ('say() { echo "$*"; }\n' + _install_function("env_value")
              + _install_function("trusted_proxy_hint") + "trusted_proxy_hint\n")
    env = {"PATH": os.environ["PATH"], **shell}
    r = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert ("TRUSTED_PROXY is empty" in r.stdout) is hinted
    if hinted:
        assert "PROXY_BIND_IP=127.0.0.1" in r.stdout and "set TRUSTED_PROXY=" not in r.stdout


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


# ── dev-setup.sh stops while the platform's units run ──────────────────

# A stand-in for systemctl: `is-active --quiet <unit>` answers 0 for a unit
# FAKE_ACTIVE names and 3 (inactive) otherwise; every call is logged.
FAKE_SYSTEMCTL = r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_CALLS"
[ "$1" = is-active ] || exit 1
for unit; do :; done
case " ${FAKE_ACTIVE:-} " in *" $unit "*) exit 0 ;; esac
exit 3
'''


def _dev_setup(tmp_path, *flags, active="", systemctl=True):
    """Run a copy of dev-setup.sh from a scratch platform root whose
    installer is a stub that logs and exits 7, so a run past the guard stops
    at its second step. PATH holds only logging fakes of node (answering the
    pinned major), uv and docker, systemctl unless ``systemctl`` is False,
    and the system tools the script runs before that step. Returns the run
    and the calls logged, systemctl's apart."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    for name in ("dev-setup.sh", "versions.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    shutil.copy2(REPO / "VERSIONS.md", root / "VERSIONS.md")
    (root / "scripts" / "install-baseline-tools.sh").write_text(
        'echo installer >> "$FAKE_CALLS"\nexit 7\n')
    node = re.search(r"^NODE_VERSION=(\S+)", (REPO / "VERSIONS.md").read_text(), re.M)
    assert node, "VERSIONS.md has no NODE_VERSION"
    bodies = {name: f'#!/bin/sh\necho "{name} $*" >> "$FAKE_CALLS"\n' for name in ("uv", "docker")}
    bodies["node"] = f'#!/bin/sh\necho "node $*" >> "$FAKE_CALLS"\necho v{node.group(1)}\n'
    if systemctl:
        bodies["systemctl"] = FAKE_SYSTEMCTL
    fakes, tools, home = tmp_path / "devbin", tmp_path / "tools", tmp_path / "home"
    for d in (fakes, tools, home):
        d.mkdir()
    for name, body in bodies.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    # No /usr/bin on PATH: the host's own systemctl, node or sudo is never run.
    for name in ("bash", "dirname", "head", "id", "sed"):
        (tools / name).symlink_to(shutil.which(name))
    calls = tmp_path / "calls.log"
    calls.touch()
    r = subprocess.run(
        [str(tools / "bash"), str(root / "scripts" / "dev-setup.sh"), *flags],
        cwd=tmp_path, stdin=subprocess.DEVNULL,
        env={"PATH": f"{fakes}:{tools}", "HOME": str(home), "USER": "tester",
             "FAKE_CALLS": str(calls), "FAKE_ACTIVE": active},
        capture_output=True, text=True, timeout=60,
    )
    lines = calls.read_text().splitlines()
    return (r, [c for c in lines if c.startswith("systemctl ")],
            [c for c in lines if not c.startswith("systemctl ")])


@pytest.mark.parametrize("flags, active, unit", [
    ((), "otodock-proxy", "otodock-proxy"),
    (("--phone",), "otodock-phone", "otodock-phone"),
    (("--phone",), "otodock-proxy otodock-phone", "otodock-proxy"),
])
def test_dev_setup_stops_while_a_unit_runs(tmp_path, flags, active, unit):
    r, asked, calls = _dev_setup(tmp_path, *flags, active=active)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"{unit} is running" in r.stderr
    assert f"sudo systemctl stop {unit}" in r.stderr
    assert f"systemctl is-active --quiet {unit}" in asked
    assert calls == []  # no node, uv, docker or installer


@pytest.mark.parametrize("active, systemctl", [
    ("otodock-phone", True),  # the phone unit counts only with --phone
    ("", True),
    ("", False),              # a host without systemd skips the check
])
def test_dev_setup_goes_on_past_the_guard(tmp_path, active, systemctl):
    r, asked, calls = _dev_setup(tmp_path, active=active, systemctl=systemctl)
    assert r.returncode == 7, r.stdout + r.stderr
    assert "is running" not in r.stderr
    assert asked == (["systemctl is-active --quiet otodock-proxy"] if systemctl else [])
    # The Node step ran, then the stub installer ended the run.
    assert calls[-1] == "installer" and calls[:-1] and set(calls[:-1]) == {"node -v"}
