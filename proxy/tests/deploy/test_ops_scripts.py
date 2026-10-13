"""The operator scripts, run against a fake ``docker`` on PATH.

backup.sh and restore.sh pick the platform's own Postgres and proxy
containers (compose labels, else the bare-metal container_name matched
whole) and refuse to guess between two; a community MCP container whose free
name merely contains the text is never chosen. restore.sh refuses while the
platform answers on the port and address .env publishes it on. backup.sh's
apps copy opens every app database read-only whatever its file name.
install.sh and compose.sh create the secrets file private from its first
byte, and install.sh checks the free disk where Docker keeps its images. dev-setup.sh stops before its first step while the otodock-proxy unit
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

from tests.deploy.fake_docker import FAKE_DOCKER

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
    (tmp_path / "config.env").write_text("JWT_SECRET=placeholder\n")
    (tmp_path / "agents").mkdir(exist_ok=True)
    env.update({
        "PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(containers),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
        "OTODOCK_BACKUP_DIR": str(tmp_path / "backups"),
        "OTODOCK_ENV_FILE": str(tmp_path / "config.env"),
        "OTODOCK_AGENTS_DIR": str(tmp_path / "agents"),
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


def _calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _exec_ids(tmp_path) -> list[str]:
    """The container of every `docker exec` the script ran."""
    return [a[2] if a[1] == "-i" else a[1] for a in _calls(tmp_path) if a[:1] == ["exec"]]


def _fed(tmp_path) -> bool:
    """Whether a dump was fed to psql (a `psql -v ON_ERROR_STOP=1` exec on
    stdin; the schema recreation is a `-c` statement, not a feed)."""
    return any(a[:1] == ["exec"] and "psql" in a and "-v" in a and "-c" not in a for a in _calls(tmp_path))


# ── container selection ────────────────────────────────────────────────

@pytest.mark.parametrize("containers, chosen", [
    ([EVIL_PG, PG_T2], "pg-t2"),        # the newest match is a community MCP
    ([EVIL_PG, PG_T1], "pg-t1"),        # bare metal: the whole container_name
    ([PG_T2], "pg-t2"),
])
def test_backup_dumps_the_platform_postgres(tmp_path, fakebin, containers, chosen):
    r = _run("backup.sh", tmp_path, fakebin, containers)
    assert r.returncode == 0, r.stderr
    assert set(_exec_ids(tmp_path)) == {chosen}


def test_backup_refuses_a_lookalike_alone(tmp_path, fakebin):
    r = _run("backup.sh", tmp_path, fakebin, [EVIL_PG])
    assert r.returncode != 0
    assert _exec_ids(tmp_path) == []
    assert not any((tmp_path / "backups").glob("*"))


def test_backup_refuses_two_platform_postgres(tmp_path, fakebin):
    twin = {**PG_T2, "id": "pg-t2b", "name": "otodock-otodock-postgres-2"}
    r = _run("backup.sh", tmp_path, fakebin, [PG_T2, twin])
    assert r.returncode != 0
    assert "more than one" in r.stderr
    assert _exec_ids(tmp_path) == []


def _proxy_with_mounts(tmp_path, **extra) -> dict:
    return {**PROXY_T2, **extra, "mounts": {
        "/opt/otodock/config.env": {"source": str(tmp_path / "config.env")},
        "/opt/otodock/agents": {"name": "vol-agents", "source": str(tmp_path / "agents")},
    }}


def test_backup_reads_the_tree_inside_the_platform_proxy(tmp_path, fakebin):
    r = _run("backup.sh", tmp_path, fakebin, [EVIL_PROXY, _proxy_with_mounts(tmp_path), PG_T2],
             OTODOCK_AGENTS_DIR="")
    assert r.returncode == 0, r.stderr
    assert set(_exec_ids(tmp_path)) == {"pg-t2", "proxy-t2"}


def test_backup_apps_refuses_two_platform_proxies(tmp_path, fakebin):
    twin = {**PROXY_T2, "id": "proxy-t2b", "name": "otodock-otodock-proxy-2"}
    r = _run("backup.sh", tmp_path, fakebin, [PROXY_T2, twin, PG_T2], OTODOCK_AGENTS_DIR="")
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
    return r, probed, _fed(tmp_path)


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
    r = _run("backup.sh", tmp_path, fakebin, [PG_T2], OTODOCK_AGENTS_DIR=str(agents))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in data.iterdir()) == before  # nothing created
    (run_dir,) = (tmp_path / "backups").glob("otodock-*")
    archive = run_dir / "agents.tar.gz"
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


# ── install.sh: free disk where Docker keeps its images ────────────────

# docker answers the context endpoint, the OS and the root dir from FAKE_*,
# and lists the proxy image when FAKE_PROXY_IMAGE is set; df reports
# FAKE_DF_TOTAL_K and FAKE_DF_FREE_K; uname says FAKE_UNAME.
_DISK_DOCKER = r'''#!/bin/sh
case "$1 $2" in
  "context inspect") echo "${FAKE_ENDPOINT-unix:///var/run/docker.sock}" ;;
  "info -f") case "$3" in *OperatingSystem*) echo "${FAKE_OS:-Ubuntu 24.04 LTS}" ;;
                          *) echo "${FAKE_ROOT-/var/lib/docker}" ;; esac ;;
  "image ls") [ -n "${FAKE_PROXY_IMAGE:-}" ] && echo 0123456789ab ;;
esac
exit 0
'''
_DISK_DF = r'''#!/bin/sh
[ -n "${FAKE_DF_FAIL:-}" ] && exit 1
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/sda1 ${FAKE_DF_TOTAL_K} 1 ${FAKE_DF_FREE_K} 50% /"
'''
_GB = 1024 * 1024


def _disk_preflight(tmp_path, **env_extra):
    b = tmp_path / "diskbin"
    b.mkdir(exist_ok=True)
    for name, body in (("docker", _DISK_DOCKER), ("df", _DISK_DF),
                       ("uname", '#!/bin/sh\necho "${FAKE_UNAME:-Linux}"\n')):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    script = ('say() { echo "say: $*"; }\nfail() { echo "fail: $*" >&2; exit 1; }\n'
              + _install_function("disk_preflight") + 'disk_preflight\necho "went on"\n')
    env = {"PATH": f"{b}:{os.environ['PATH']}", "FAKE_DF_TOTAL_K": str(100 * _GB),
           "FAKE_DF_FREE_K": str(50 * _GB), **env_extra}
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


def test_disk_preflight_stops_below_the_floor(tmp_path):
    r = _disk_preflight(tmp_path, FAKE_DF_FREE_K=str(9 * _GB))
    assert r.returncode == 1 and "went on" not in r.stdout
    assert "only 9 GB free" in r.stderr and "OTODOCK_MIN_FREE_GB=0" in r.stderr


def test_disk_preflight_goes_on_when_the_images_are_here(tmp_path):
    r = _disk_preflight(tmp_path, FAKE_DF_FREE_K=str(9 * _GB), FAKE_PROXY_IMAGE="1")
    assert r.returncode == 0 and "went on" in r.stdout
    assert "already on this host" in r.stdout


def test_disk_preflight_warns_below_the_upload_floor(tmp_path):
    # 500 GB disk: the server keeps 25 GB free, so 30 GB free warns
    r = _disk_preflight(tmp_path, FAKE_DF_TOTAL_K=str(500 * _GB), FAKE_DF_FREE_K=str(30 * _GB))
    assert r.returncode == 0 and "went on" in r.stdout
    assert "refuses uploads" in r.stdout and "less than 25 GB" in r.stdout
    # a small disk: the 5 GB minimum applies
    r = _disk_preflight(tmp_path, FAKE_DF_TOTAL_K=str(40 * _GB), FAKE_DF_FREE_K=str(15 * _GB))
    assert "less than 5 GB" in r.stdout


def test_disk_preflight_passes_quietly_with_room(tmp_path):
    r = _disk_preflight(tmp_path)
    assert r.returncode == 0 and r.stdout == "went on\n"


@pytest.mark.parametrize("extra", [
    {"FAKE_UNAME": "Darwin"},
    {"FAKE_ENDPOINT": "tcp://10.9.9.9:2375"},
    {"FAKE_ENDPOINT": ""},
    {"FAKE_OS": "Docker Desktop"},
    {"FAKE_ROOT": ""},
    {"FAKE_DF_FAIL": "1"},
    {"OTODOCK_MIN_FREE_GB": "0"},
], ids=["not-linux", "remote-daemon", "no-context", "docker-desktop", "no-root-dir", "df-fails", "floor-0"])
def test_disk_preflight_skips(tmp_path, extra):
    r = _disk_preflight(tmp_path, FAKE_DF_FREE_K="1", **extra)
    assert r.returncode == 0 and r.stdout.endswith("went on\n"), r.stderr


def test_disk_preflight_floor_must_be_a_whole_number(tmp_path):
    r = _disk_preflight(tmp_path, OTODOCK_MIN_FREE_GB="lots", FAKE_DF_FREE_K=str(12 * _GB))
    assert "not a whole number" in r.stdout and r.returncode == 1
    r = _disk_preflight(tmp_path, OTODOCK_MIN_FREE_GB="20", FAKE_DF_FREE_K=str(15 * _GB))
    assert r.returncode == 1 and "needs about 20 GB" in r.stderr


# ── restore.sh on a run directory ───────────────────────────────────────────

import gzip  # noqa: E402
import io  # noqa: E402

KEY_A = "a" * 32
KEY_B = "b" * 32


TOKEN_FILE = "./github-tokens/dev-admin/walk-a1.json"


def _archive(path: Path, members: dict) -> None:
    """A tree archive from ``members`` ({name: text | ("link", target) |
    ("hardlink", target)}); ``addfile`` keeps a name verbatim (``add`` would
    normalise it)."""
    with tarfile.open(path, "w:gz") as tf:
        for name, value in members.items():
            if isinstance(value, tuple):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE if value[0] == "link" else tarfile.LNKTYPE
                info.linkname = value[1]
                tf.addfile(info)
            else:
                data = value.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))


def _run_dir(tmp_path, *, version="1.8.0", keys=None, floor=None, tree=True, db_name="db.sql.gz",
             members=None, errors=0, sessions="present") -> Path:
    """A backup.sh run directory: the dump, the agents archive built from
    ``members``, the sessions archive (``sessions``: "present" writes it,
    "pruned" leaves the manifest row alone, None leaves the row out, as a run
    from before the sessions tree was archived) and the manifest."""
    d = tmp_path / f"otodock-{version}-20261030-120000"
    d.mkdir()
    with gzip.open(d / db_name, "wb") as fh:
        fh.write(b"SELECT 1;\n")
    lines = [f"otodock_version={version}", "topology=docker", "project=otodock", f"db={db_name}", "env=1",
             f"agents={'present' if tree else 'pruned'}", "ssh_keys=skipped", f"errors={errors}"]
    if sessions is not None:
        lines.append(f"sessions={sessions if tree else 'pruned'}")
    if keys is not None:
        lines.append("credential_keys=" + json.dumps(keys))
    if floor is not None:
        lines.append("credential_keys_floor=" + json.dumps(floor))
    lines.append("complete=1")
    (d / "manifest").write_text("\n".join(lines) + "\n")
    if tree:
        _archive(d / "agents.tar.gz", members or {"./helper/workspace/notes.md": "restored",
                                                  "./helper/py": ("link", "/usr/bin/python3")})
        if sessions == "present":
            _archive(d / "sessions.tar.gz", {TOKEN_FILE: '{"access_token": "restored"}', "./index.json": "{}"})
    return d


def _stack(tmp_path, *, running=False, status=None, sessions_volume=True):
    """A containerised install whose proxy is stopped: its agents and sessions
    volumes stand in by ``tmp_path/vol-agents`` and ``tmp_path/vol-sessions``
    (each holding one file a restore must replace)."""
    vol = tmp_path / "vol-agents"
    vol.mkdir(exist_ok=True)
    (vol / "stale.txt").write_text("old")
    sessions = tmp_path / "vol-sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / "stale-index.json").write_text("old")
    mounts = {
        "/opt/otodock/config.env": {"source": str(tmp_path / "config.env")},
        "/opt/otodock/agents": {"name": "vol-agents", "source": str(vol)},
    }
    if sessions_volume:
        mounts["/opt/otodock/sessions"] = {"name": "vol-sessions", "source": str(sessions)}
    proxy = {**PROXY_T2, "running": running, "status": status, "image": "ghcr.io/x/proxy:1.8.0", "mounts": mounts}
    return [PG_T2, proxy], vol


def _restore(tmp_path, fakebin, containers, target, *args, **extra):
    extra.setdefault("FAKE_VOLUMES", json.dumps({"vol-agents": str(tmp_path / "vol-agents"),
                                                 "vol-sessions": str(tmp_path / "vol-sessions")}))
    extra.setdefault("FAKE_IMAGE_VERSION", "1.8.0")
    extra.setdefault("FAKE_KEYS", json.dumps({KEY_A: "JWT_SECRET"}))
    # A set OTODOCK_AGENTS_DIR means the host, so the stack's restores unset it.
    extra.setdefault("OTODOCK_AGENTS_DIR", "")
    return _run("restore.sh", tmp_path, fakebin, containers, str(target), *args, stdin="yes\n", **extra)


def _one_offs(tmp_path) -> list[list[str]]:
    return [a for a in _calls(tmp_path) if a[:1] == ["run"]]


def test_a_run_directory_restores_the_database_and_the_tree_into_the_volume(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "every key the backup needs is held here" in r.stdout
    assert _fed(tmp_path)
    schema = [a for a in _calls(tmp_path) if a[:1] == ["exec"] and "DROP SCHEMA IF EXISTS public CASCADE" in a]
    assert schema, "the public schema is recreated before the dump"
    runs = _one_offs(tmp_path)
    checks = [a for a in runs if "-c" in a and "--network" in a and "vol-agents:/opt/otodock/agents:ro" in a]
    assert checks, "the archive is checked by a one-off with the archive on stdin and the volume read-only"
    assert not any(".tar.gz" in " ".join(a) for a in runs), "no archive path is mounted into a container"
    calls = _calls(tmp_path)
    first_check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "-c" in a)
    first_psql = next(i for i, a in enumerate(calls) if a[:1] == ["exec"] and "psql" in a and "-v" in a)
    assert first_check < first_psql, "the archives are checked before the database is touched"
    assert any(a[:1] == ["exec"] and "DELETE FROM platform_settings WHERE key = 'credential_key_fingerprints'" in " ".join(a)
               for a in calls), "the restored record of the source's keys is cleared"
    assert any("vol-agents:/opt/otodock/agents" in a for a in runs), "the tree goes into the volume"
    assert (vol / "helper" / "workspace" / "notes.md").read_text() == "restored"
    assert os.readlink(vol / "helper" / "py") == "/usr/bin/python3"
    assert not (vol / "stale.txt").exists()
    assert not list(vol.glob(".pre-restore-*"))
    # The sessions tree, after the agents tree: the connected account's tokens are back.
    sessions = tmp_path / "vol-sessions"
    assert any("vol-sessions:/opt/otodock/sessions" in a for a in runs), "the sessions tree goes into its volume"
    assert (sessions / TOKEN_FILE[2:]).read_text() == '{"access_token": "restored"}'
    assert not (sessions / "stale-index.json").exists() and not list(sessions.glob(".pre-restore-*"))
    order = [a for a in runs if "-v" in a and ":ro" not in " ".join(a)]
    assert "vol-agents:/opt/otodock/agents" in order[0] and "vol-sessions:/opt/otodock/sessions" in order[-1]
    assert "the sessions tree in vol-sessions" in r.stdout


@pytest.mark.parametrize("sessions, said", [
    (None, "no sessions archive"),
    ("pruned", "sessions archive was pruned"),
    ("skipped", "backup skipped the sessions tree"),
])
def test_a_run_without_a_sessions_archive_restores_the_rest_and_says_so(tmp_path, fakebin, sessions, said):
    """A 1.8.0 run from before the sessions tree was archived, or one whose
    trees retention pruned: the agents tree comes back, the connected
    accounts' tokens do not, and the operator is told to connect them again."""
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, sessions=sessions)
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert said in r.stderr and "connect the accounts again" in r.stderr
    assert (vol / "helper" / "workspace" / "notes.md").read_text() == "restored"
    assert (tmp_path / "vol-sessions" / "stale-index.json").exists()
    assert not any("vol-sessions" in " ".join(a) for a in _one_offs(tmp_path))


def test_a_proxy_without_a_sessions_volume_restores_the_rest_and_says_so(tmp_path, fakebin):
    containers, vol = _stack(tmp_path, sessions_volume=False)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "mounts no sessions volume" in r.stderr
    assert (vol / "helper" / "workspace" / "notes.md").read_text() == "restored"
    assert (tmp_path / "vol-sessions" / "stale-index.json").exists()


def test_a_sessions_archive_that_fails_the_check_changes_nothing(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, sessions=None)
    _archive(d / "sessions.tar.gz", {TOKEN_FILE: "x", "./a/../../evil": "x"})
    (d / "manifest").write_text((d / "manifest").read_text() + "sessions=present\n")
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert "sessions.tar.gz did not pass the check" in r.stderr
    assert not _fed(tmp_path) and (vol / "stale.txt").exists()
    assert (tmp_path / "vol-sessions" / "stale-index.json").exists()


def test_db_only_leaves_the_tree_alone(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d, "--db-only")
    assert r.returncode == 0, r.stderr
    assert _fed(tmp_path)
    assert not any("python3" in a for a in _one_offs(tmp_path))
    assert (vol / "stale.txt").exists()


def test_a_backup_from_a_newer_release_is_refused_unless_allowed(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, version="1.9.0", keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert "taken on OtoDock 1.9.0 and this install runs 1.8.0" in r.stderr
    assert "OTODOCK_ALLOW_NEWER_DATABASE=true" in r.stderr
    assert not _fed(tmp_path) and (vol / "stale.txt").exists()
    r = _restore(tmp_path, fakebin, containers, d, "--allow-newer")
    assert r.returncode == 0, r.stderr
    assert "WARNING: this backup was taken on OtoDock 1.9.0" in r.stderr
    assert _fed(tmp_path)


def test_a_backup_whose_keys_this_install_lacks_is_refused_naming_them(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_B: "CREDENTIAL_ENCRYPTION_KEY entry 1", KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert "keys this install cannot read" in r.stderr
    assert f"CREDENTIAL_ENCRYPTION_KEY entry 1 {KEY_B[:8]}" in r.stderr
    assert "JWT_SECRET " + KEY_A[:8] not in r.stderr
    assert "totp_enabled = false" in r.stderr
    assert not _fed(tmp_path) and (vol / "stale.txt").exists()
    r = _restore(tmp_path, fakebin, containers, d, "--ignore-credential-key")
    assert r.returncode == 0, r.stderr
    assert "WARNING: the credentials in this backup" in r.stderr and _fed(tmp_path)


def test_a_1_7_1_backup_is_checked_by_the_secret_floor_record(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, version="1.7.1", floor={"JWT_SECRET": [KEY_B]})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert f"JWT_SECRET {KEY_B[:8]}" in r.stderr
    d2 = _run_dir(tmp_path / "ok", version="1.7.1", floor={"JWT_SECRET": [KEY_A]}) if (tmp_path / "ok").mkdir() is None else None
    r = _restore(tmp_path, fakebin, containers, d2)
    assert r.returncode == 0, r.stderr


def test_a_target_that_never_recorded_its_keys_or_a_bare_dump_is_not_checked(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_B: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d, FAKE_KEYS="")
    assert r.returncode == 0, r.stderr
    assert "has not recorded its credential keys yet" in r.stderr
    dump = tmp_path / "old.sql.gz"
    with gzip.open(dump, "wb") as fh:
        fh.write(b"SELECT 1;\n")
    (tmp_path / "docker.log").unlink()
    r = _restore(tmp_path, fakebin, containers, dump)
    assert r.returncode == 0, r.stderr
    assert "a dump alone" in r.stderr and _fed(tmp_path)
    assert not any("python3" in a for a in _one_offs(tmp_path))


def test_a_pruned_tree_restores_the_database_and_says_so(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, tree=False)
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "pruned by the retention" in r.stderr and (vol / "stale.txt").exists()


def test_a_skipped_tree_restores_the_database_and_says_why(tmp_path, fakebin):
    """A run whose agents directory was not readable when backup.sh ran
    (``agents=skipped``) restores the database and names the reason."""
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, tree=False)
    (d / "manifest").write_text((d / "manifest").read_text().replace("agents=pruned", "agents=skipped"))
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "backup skipped the trees" in r.stderr and (vol / "stale.txt").exists()


def test_a_set_agents_dir_restores_onto_this_host_beside_a_stack(tmp_path, fakebin):
    """As for backup.sh, OTODOCK_AGENTS_DIR names this host: the trees land
    in that directory and no one-off container runs on the proxy's image,
    even with the project's proxy container present (stopped)."""
    containers, vol = _stack(tmp_path)
    # On this host the running version is the checkout's VERSIONS.md.
    d = _run_dir(tmp_path, version="1.7.0", keys={KEY_A: "JWT_SECRET"})
    host_dir = tmp_path / "agents"
    r = _restore(tmp_path, fakebin, containers, d, OTODOCK_AGENTS_DIR=str(host_dir))
    assert r.returncode == 0, r.stderr
    assert "host)" in r.stdout
    assert (host_dir / "helper" / "workspace" / "notes.md").read_text() == "restored"
    assert (vol / "stale.txt").exists() and not _one_offs(tmp_path)


@pytest.mark.parametrize("status", ["running", "restarting", "paused"])
def test_a_running_restarting_or_paused_proxy_refuses_the_restore_on_docker(tmp_path, fakebin, status):
    """A crash-looping proxy comes back mid-restore and migrates a half-restored
    database; a paused one resumes: neither is stopped."""
    containers, _ = _stack(tmp_path, running=True, status=status)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert f"the platform is running (container proxy-t2, state {status})" in r.stderr
    assert "docker compose stop otodock-proxy" in r.stderr
    assert not _fed(tmp_path)


def test_an_archive_that_fails_the_check_changes_nothing(tmp_path, fakebin):
    """A member that would land outside the root (..) fails the read-only
    check before anything is touched; an absolute name is relocated under
    the root by the filter, never written where it says."""
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, members={"./ok.txt": "y", "./a/../../evil": "x"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1
    assert "did not pass the check" in r.stderr
    assert not _fed(tmp_path), "the database is untouched when an archive fails its check"
    assert (vol / "stale.txt").exists() and not (vol / "ok.txt").exists()
    (tmp_path / "two").mkdir()
    d = _run_dir(tmp_path / "two", keys={KEY_A: "JWT_SECRET"}, members={"/etc/evil": "x"})
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert (vol / "etc" / "evil").read_text() == "x" and not (vol / "stale.txt").exists()


def _through_a_link(outside: Path, shape: str) -> dict:
    """The three archives tar_filter's realpath check cannot see before the
    extraction: a link, then a member written through it."""
    if shape == "absolute":
        return {"./a/esc": ("link", str(outside)), "./a/esc/pwned.txt": "x"}
    if shape == "relative":
        return {"./a/esc": ("link", "../../outside"), "./a/esc/pwned.txt": "x"}
    return {"./esc": ("link", str(outside)), "./outside-ok.txt": "fine", "./h": ("hardlink", "esc/secret.txt")}


@pytest.mark.parametrize("shape", ["absolute", "relative", "hardlink"])
def test_a_member_through_an_archived_link_fails_the_check_before_the_database(tmp_path, fakebin, shape):
    containers, vol = _stack(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, members=_through_a_link(outside, shape))
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 1, r.stderr
    assert "did not pass the check" in r.stderr and "archived link" in r.stderr
    assert not _fed(tmp_path)
    assert (vol / "stale.txt").exists() and not list(vol.glob(".pre-restore-*"))
    assert list(outside.iterdir()) == []


# Loaded by the check's interpreter at start (PYTHONPATH): the place the tree
# goes reports almost no free space.
_NO_SPACE = """\
import shutil
shutil.disk_usage = lambda path: shutil._ntuple_diskusage(1, 1, 1024)
"""


def test_an_archive_that_does_not_fit_the_volume_is_refused_before_the_database(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_NO_SPACE)
    r = _restore(tmp_path, fakebin, containers, d, PYTHONPATH=str(site))
    assert r.returncode == 1, r.stderr
    assert "MB free" in r.stderr and "did not pass the check" in r.stderr
    assert not _fed(tmp_path) and (vol / "stale.txt").exists()
    checks = [a for a in _one_offs(tmp_path) if "-c" in a]
    assert checks and any(a.startswith("vol-agents:/opt/otodock/agents:ro") for a in checks[0]), \
        "the check mounts the volume read-only to measure its free space"


def test_a_failed_schema_recreation_stops_the_restore_before_the_dump(tmp_path, fakebin):
    containers, vol = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"})
    r = _restore(tmp_path, fakebin, containers, d, FAKE_PSQL_FAIL="DROP SCHEMA")
    assert r.returncode == 1
    assert "could not be recreated" in r.stderr and "PARTIAL" in r.stderr
    assert not _fed(tmp_path), "the dump is never fed into the old schema"
    assert (vol / "stale.txt").exists()


def test_the_postgres_container_can_be_named_outright(tmp_path, fakebin):
    """The bare-metal checkout beside a compose stack: its project has no proxy
    (host topology, the checkout's VERSIONS.md) and its Postgres is named."""
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, version="1.7.0", keys={KEY_A: "JWT_SECRET"}, tree=False)
    r = _restore(tmp_path, fakebin, containers + [PG_T1], d,
                 OTODOCK_COMPOSE_PROJECT="oto-dock", OTODOCK_POSTGRES_CONTAINER="otodock-postgres")
    assert r.returncode == 0, r.stderr
    assert _fed(tmp_path) and set(_exec_ids(tmp_path)) == {"pg-t1"}
    r = _restore(tmp_path, fakebin, containers + [PG_T1], d, OTODOCK_POSTGRES_CONTAINER="nope")
    assert r.returncode == 1 and "OTODOCK_POSTGRES_CONTAINER=nope is not a container" in r.stderr


def test_the_manifest_names_the_dump(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, tree=False, db_name="otodock-otodock-20261030-120000.sql.gz")
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert _fed(tmp_path)


def test_a_named_project_restores_only_its_own_containers(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, tree=False)
    r = _restore(tmp_path, fakebin, containers + [PG_T1], d, OTODOCK_COMPOSE_PROJECT="laneu-b")
    assert r.returncode == 1
    assert "project laneu-b" in r.stderr and not _fed(tmp_path)


def test_a_missing_decrypt_only_key_warns_and_a_missing_first_key_refuses(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    k_old = "c" * 32
    d = _run_dir(tmp_path, keys={KEY_B: "CREDENTIAL_ENCRYPTION_KEY entry 1", k_old: "CREDENTIAL_ENCRYPTION_KEY entry 2",
                                 KEY_A: "JWT_SECRET"}, tree=False)
    r = _restore(tmp_path, fakebin, containers, d, FAKE_KEYS=json.dumps({KEY_A: "JWT_SECRET", KEY_B: "CREDENTIAL_ENCRYPTION_KEY entry 1"}))
    assert r.returncode == 0, r.stderr
    assert "older decrypt-only keys" in r.stderr and f"entry 2 {k_old[:8]}" in r.stderr
    assert _fed(tmp_path)
    (tmp_path / "two").mkdir()
    d = _run_dir(tmp_path / "two", floor={"JWT_SECRET": [k_old, KEY_A]}, tree=False)
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "older decrypt-only keys" in r.stderr


def test_an_incomplete_tree_is_named_before_the_confirmation(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _run_dir(tmp_path, keys={KEY_A: "JWT_SECRET"}, errors=2)
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "errors=2" in r.stderr and "incomplete" in r.stderr
    assert r.stderr.index("incomplete") < len(r.stderr) and "Type 'yes'" in r.stdout
