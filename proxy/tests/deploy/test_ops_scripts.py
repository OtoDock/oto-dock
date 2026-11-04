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

import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tarfile
import time
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


# ── install-baseline-tools.sh: where pasta comes from ──────────────────

_BASELINE = (SCRIPTS / "install-baseline-tools.sh").read_text()
_PASST_PIN = re.search(r'local passt_ver="\$\{PASST_VERSION:-([^}]+)\}"', _BASELINE).group(1)
_PASST_COMMIT = re.search(r'local passt_commit="([0-9a-f]{40})"', _BASELINE).group(1)
_DEBIAN_12 = "0.0~git20230309.7c7625d-1"

# sudo logs and runs nothing: an apt-get of passt leaves a distro pasta (or
# fails with FAKE_APT_FAIL), the install of passt.new leaves the built one,
# tee swallows its input. The host's /usr/local/bin is never touched.
_PASST_FAKES = {
    "sudo": r'''#!/bin/sh
echo "sudo $*" >> "$FAKE_CALLS"
for a; do last="$a"; done
case "$1" in
  apt-get) if [ "$last" = passt ]; then
             [ -n "${FAKE_APT_FAIL:-}" ] && exit 100
             printf '#!/bin/sh\necho "pasta unknown version"\n' > "$FAKEBIN/pasta"; chmod +x "$FAKEBIN/pasta"
           fi ;;
  install) if [ "$last" = /usr/local/bin/passt.new ]; then
             printf '#!/bin/sh\necho "pasta %s"\n' "$FAKE_BUILT" > "$FAKEBIN/pasta"; chmod +x "$FAKEBIN/pasta"
           fi ;;
  tee) cat > /dev/null ;;
esac
exit 0
''',
    "apt-cache": '#!/bin/sh\necho "  Candidate: ${FAKE_CANDIDATE:-(none)}"\n',
    "git": r'''#!/bin/sh
echo "git $*" >> "$FAKE_CALLS"
case " $* " in
  *" clone "*) [ -n "${FAKE_CLONE_FAIL:-}" ] && exit 128
               for a; do dir="$a"; done
               mkdir -p "$dir/LICENSES" && touch "$dir/LICENSES/GPL-2.0-or-later.txt" ;;
  *" rev-parse "*) echo "$FAKE_HEAD" ;;
  *" archive "*) prev=""; for a; do [ "$prev" = -o ] && touch "$a"; prev="$a"; done ;;
esac
''',
    "make": r'''#!/bin/sh
echo "make $*" >> "$FAKE_CALLS"
[ -n "${FAKE_MAKE_FAIL:-}" ] && exit 2
dir=""; prev=""
for a; do [ "$prev" = -C ] && dir="$a"; prev="$a"; done
for a; do case "$a" in passt|passt.avx2) touch "$dir/$a" ;; esac; done
''',
    "curl": '#!/bin/sh\necho "curl $*" >> "$FAKE_CALLS"\nexit 7\n',
    "uname": '#!/bin/sh\necho "${FAKE_ARCH:-x86_64}"\n',
}


def _baseline_function(name: str) -> str:
    """A function of the installer as text: one line, or up to its closing brace."""
    m = (re.search(rf"^{name}\(\) \{{[^\n]*\}}\n", _BASELINE, re.M)
         or re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _BASELINE, re.S | re.M))
    assert m, name
    return m.group(0)


def _netns(tmp_path, mode, *, pasta=None, candidate=_DEBIAN_12, head=_PASST_COMMIT,
           arch="x86_64", **fail):
    """install_netns_tools from the real installer, with fakes for every tool
    it runs and a PATH holding nothing else: the host's own pasta, apt and git
    are never reached. ``pasta`` is the version an existing pasta prints;
    ``fail`` sets FAKE_CLONE_FAIL / FAKE_MAKE_FAIL / FAKE_APT_FAIL. Returns the
    run and the calls logged."""
    fakes, tools = tmp_path / "netnsbin", tmp_path / "netnstools"
    for d in (fakes, tools):
        d.mkdir()
    bodies = dict(_PASST_FAKES)
    if pasta:
        bodies["pasta"] = f'#!/bin/sh\necho "pasta {pasta}"\n'
    for name, body in bodies.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    for name in ("bash", "cat", "chmod", "head", "mkdir", "mktemp", "rm", "sed", "sort", "touch"):
        (tools / name).symlink_to(shutil.which(name))
    calls = tmp_path / "netns.log"
    calls.touch()
    script = ('set -euo pipefail\n'
              'info() { echo "[INFO] $*"; }; ok() { echo "[OK] $*"; }\n'
              'warn() { echo "[WARN] $*"; }; err() { echo "[ERROR] $*" >&2; }\n'
              f'SUDO=sudo\nOTODOCK_PASST_BUILD={mode}\n'
              + "".join(_baseline_function(n) for n in
                        ("_older", "_passt_date", "_install_passt", "_build_pinned_passt", "install_netns_tools"))
              + "install_netns_tools\n")
    env = {"PATH": f"{fakes}:{tools}", "FAKE_CALLS": str(calls), "FAKEBIN": str(fakes),
           "FAKE_CANDIDATE": candidate, "FAKE_HEAD": head, "FAKE_BUILT": _PASST_PIN,
           "FAKE_ARCH": arch, "TMPDIR": str(tmp_path)}
    env.update({f"FAKE_{k.upper()}_FAIL": "1" for k, v in fail.items() if v})
    r = subprocess.run([str(tools / "bash"), "-c", script], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    return r, calls.read_text().splitlines()


_APT_PASST = "sudo apt-get install -y --no-install-recommends passt"


def test_passt_dates_read_every_version_form(tmp_path):
    script = _baseline_function("_passt_date") + "".join(
        f'echo "[$(_passt_date "{v}")]"\n' for v in (
            "pasta 2026_10_02.cba3570", "pasta 2026_06_11.a9c61ff-31-gbf21038",
            _DEBIAN_12, "pasta unknown version", ""))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.stdout.split() == ["[2026_10_02]", "[2026_06_11]", "[2023_03_09]", "[]", "[]"], r.stderr


def test_passt_builds_the_pin_and_keeps_its_source(tmp_path):
    r, calls = _netns(tmp_path, "required")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"[OK] pasta installed — pasta {_PASST_PIN}" in r.stdout
    clone = next(c for c in calls if " clone " in c)
    assert f"--branch {_PASST_PIN} https://passt.top/passt" in clone
    order = [c.split()[0] + " " + c.split()[1] for c in calls if not c.startswith("sudo apt-get")]
    assert order[:4] == ["git -c", "git -C", "make -s", "sudo install"], calls
    assert any(" archive " in c and f"passt-{_PASST_PIN}.tar.gz" in c for c in calls)
    assert any(c.startswith("sudo install -m 0644") and f"passt-{_PASST_PIN}.tar.gz" in c
               and c.endswith("/usr/local/share/doc/passt/") for c in calls), calls
    # Both builds made; the AVX2 binary lands first, then passt is copied
    # beside, the old layout replaced, then the link.
    assert any(c.startswith("make -s -C") and c.endswith(" passt passt.avx2") for c in calls), calls
    swap = [c for c in calls if "/usr/local/bin/" in c]
    assert [c.split()[1] for c in swap] == ["install", "mv", "install", "rm", "mv", "ln"], swap
    assert swap[1].endswith("/usr/local/bin/passt.avx2")
    assert _APT_PASST not in calls


def test_passt_on_arm_builds_no_avx2_binary_and_drops_a_stale_one(tmp_path):
    r, calls = _netns(tmp_path, "required", arch="aarch64")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("make -s -C") and c.endswith(" passt") for c in calls), calls
    assert "sudo rm -f /usr/local/bin/passt.avx2" in calls
    assert not [c for c in calls if "passt.avx2.new" in c]


@pytest.mark.parametrize("head", [
    _PASST_COMMIT[:7] + "0" * 33,        # the tag's short sha, another commit
    "0123456789" * 4,                    # nothing of the tag's
])
def test_passt_refuses_a_clone_that_is_not_the_pinned_commit(tmp_path, head):
    r, calls = _netns(tmp_path, "required", head=head)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"resolves to commit {head}, not the pinned one" in r.stderr
    assert not [c for c in calls if c.startswith(("make", "sudo install", "sudo mv"))], calls


def test_passt_required_fails_on_a_failed_clone_and_takes_no_other(tmp_path):
    r, calls = _netns(tmp_path, "required", clone=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "did not build, and this install takes no other" in r.stderr
    assert _APT_PASST not in calls and not [c for c in calls if c.startswith("curl")]


def test_passt_auto_falls_back_to_the_distro_on_a_failed_build(tmp_path):
    r, calls = _netns(tmp_path, "auto", make=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.index(_APT_PASST) > next(i for i, c in enumerate(calls) if c.startswith("make"))
    assert "[WARN] passt installed from the distro" in r.stdout


def test_passt_auto_keeps_a_pasta_below_the_floor_when_the_build_fails(tmp_path):
    r, calls = _netns(tmp_path, "auto", pasta="0.0~git20230309.7c7625d-1", make=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stays, below the floor" in r.stdout and _APT_PASST not in calls


def test_passt_distro_mode_never_clones(tmp_path):
    r, calls = _netns(tmp_path, "distro")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if c.startswith(("git", "make"))], calls
    assert _APT_PASST in calls


def test_passt_distro_mode_without_a_distro_passt_warns_and_goes_on(tmp_path):
    """A paired machine on a release with no passt package (Ubuntu 22.04):
    no build, no upstream download, no failure of the pairing."""
    r, calls = _netns(tmp_path, "distro", candidate="(none)", apt=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if c.startswith(("git", "make", "curl"))], calls
    assert "[WARN] no pasta on this host (OTODOCK_PASST_BUILD=distro)" in r.stdout


@pytest.mark.parametrize("mode, pasta, candidate, apt", [
    ("auto", "2026_06_11.a9c61ff-31-gbf21038", _DEBIAN_12, False),   # at or above the floor: stays
    ("required", "2026_10_02.cba3570", _DEBIAN_12, False),
    ("auto", None, "0.0~git20240220.1e6f92b-1", True),               # Ubuntu 24.04's candidate
])
def test_passt_takes_nothing_new_when_the_host_or_distro_reaches_the_floor(tmp_path, mode, pasta, candidate, apt):
    r, calls = _netns(tmp_path, mode, pasta=pasta, candidate=candidate)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if c.startswith(("git", "make"))], calls
    assert (_APT_PASST in calls) is apt


def test_passt_required_never_takes_the_distro_candidate(tmp_path):
    # The image builds the pin even where the distro offers one above the floor.
    r, calls = _netns(tmp_path, "required", candidate="0.0~git20240220.1e6f92b-1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _APT_PASST not in calls and any(" clone " in c for c in calls)


# ── install-baseline-tools.sh on macOS ─────────────────────────────────

# A fake Homebrew: `shellenv` puts its own bin on PATH, every other call is
# logged. Homebrew's installer (what the fake curl downloads) logs how it ran
# and what it read on stdin, then lays the fake brew down at FAKE_BREW_AT.
_FAKE_BREW = r"""#!/bin/sh
if [ "$1" = shellenv ]; then echo "export PATH=\"${0%/*}:\$PATH\""; exit 0; fi
echo "brew $*" >> "$FAKE_CALLS"
"""
_MACOS_FAKES = {
    "sudo": r"""#!/bin/sh
echo "sudo $*" >> "$FAKE_CALLS"
[ -n "${FAKE_SUDO_FAIL:-}" ] && exit 1
case "$1" in -n|-v) exit 0 ;; esac
exec "$@"
""",
    "curl": r"""#!/bin/sh
echo "curl $*" >> "$FAKE_CALLS"
[ -n "${FAKE_CURL_FAIL:-}" ] && exit 22
prev=""; for a; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
cat > "$out" <<'EOF'
echo "installer NONINTERACTIVE=${NONINTERACTIVE:-} stdin=$(cat | wc -c | tr -d ' ')" >> "$FAKE_CALLS"
[ -n "${FAKE_INSTALLER_FAIL:-}" ] && exit 1
[ -n "${FAKE_INSTALLER_WAIT:-}" ] && { echo "installer waiting" >> "$FAKE_CALLS"; exec "$REAL_SLEEP" 30; }
mkdir -p "${FAKE_BREW_AT%/*}" && cp "$FAKEBIN/brew.fake" "$FAKE_BREW_AT" && chmod +x "$FAKE_BREW_AT"
EOF
""",
    "xcode-select": '#!/bin/sh\n[ -n "${FAKE_NO_CLT:-}" ] && exit 2\necho /Library/Developer/CommandLineTools\n',
    "id": '#!/bin/sh\necho "${FAKE_UID:-501}"\n',
}


def _macos_setup(tmp_path, *, brew_on_path=False, brew_at_candidate=False, fast_sleep=False, **env_extra):
    """install_macos_all from the real installer on a fake Mac: PATH holds
    the fakes and the system tools it runs, never the host's own brew or
    curl. The standard prefixes are two folders under ``tmp_path``;
    ``fast_sleep`` turns every ``sleep`` into a twentieth of a second.
    Returns the command, its environment, the calls log and the candidate
    the installer fills."""
    fakes, tools = tmp_path / "macbin", tmp_path / "mactools"
    for d in (fakes, tools):
        d.mkdir()
    for name, body in _MACOS_FAKES.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    (fakes / "brew.fake").write_text(_FAKE_BREW)
    if fast_sleep:
        (fakes / "sleep").write_text('#!/bin/sh\nexec "$REAL_SLEEP" 0.05\n')
        (fakes / "sleep").chmod(0o755)
    for name in ("bash", "cat", "chmod", "cp", "mkdir", "mktemp", "rm", "sleep", "tr", "wc"):
        (tools / name).symlink_to(shutil.which(name))
    first, second = tmp_path / "opt" / "bin" / "brew", tmp_path / "usrlocal" / "bin" / "brew"
    path = f"{fakes}:{tools}"
    if brew_on_path:
        onpath = tmp_path / "onpath"
        onpath.mkdir()
        (onpath / "brew").write_text(_FAKE_BREW)
        (onpath / "brew").chmod(0o755)
        path = f"{onpath}:{path}"
    if brew_at_candidate:
        second.parent.mkdir(parents=True)
        second.write_text(_FAKE_BREW)
        second.chmod(0o755)
    calls = tmp_path / "macos.log"
    calls.touch()
    script = ('set -euo pipefail\n'
              'info() { echo "[INFO] $*"; }; ok() { echo "[OK] $*"; }\n'
              'warn() { echo "[WARN] $*"; }; err() { echo "[ERROR] $*" >&2; }\n'
              'SUDO=sudo\nSKIP_TIER_2=false\nOTODOCK_PYTHON_MINOR=3.13\n'
              f'BREW_CANDIDATES="{first} {second}"\nHOMEBREW_INSTALL_URL=https://brew.example/install.sh\n'
              + "".join(_baseline_function(n) for n in ("install_macos_all", "_find_brew", "_install_homebrew", "_brew_failed"))
              + 'install_macos_all\necho "PATH-AFTER=$PATH"\n')
    env = {"PATH": path, "FAKE_CALLS": str(calls), "FAKEBIN": str(fakes), "REAL_SLEEP": shutil.which("sleep"),
           "FAKE_BREW_AT": str(first), "TMPDIR": str(tmp_path), **env_extra}
    return [str(tools / "bash"), "-c", script], env, calls, first


def _macos(tmp_path, *, stdin="", **kw):
    """``_macos_setup``'s run to its end: the run, the calls logged and the
    candidate the installer fills."""
    argv, env, calls, first = _macos_setup(tmp_path, **kw)
    r = subprocess.run(argv, env=env, cwd=tmp_path, input=stdin, capture_output=True, text=True, timeout=60)
    return r, calls.read_text().splitlines(), first


def test_macos_installs_homebrew_when_missing_unattended_and_off_stdin(tmp_path):
    # `curl … | bash`: the pipe carries the rest of the installer, which
    # Homebrew's own installer must never read.
    r, calls, brew = _macos(tmp_path, stdin="the rest of the piped installer\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sudo -n true" in calls
    assert any(c.startswith("curl -fsSL -o ") and c.endswith(" https://brew.example/install.sh") for c in calls), calls
    assert "installer NONINTERACTIVE=1 stdin=0" in calls
    assert brew.is_file()
    assert f"PATH-AFTER={brew.parent}:" in r.stdout
    installs = [c for c in calls if c.startswith("brew install ")]
    assert installs and "python@3.13" in installs[0].split() and "python" in installs[0].split()


def test_macos_finds_homebrew_off_path_at_a_standard_prefix(tmp_path):
    # An SSH command runs no login shell: brew exists but is on no PATH.
    r, calls, _ = _macos(tmp_path, brew_at_candidate=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if c.startswith(("curl", "installer", "sudo"))], calls
    assert any(c.startswith("brew install ") for c in calls)
    assert "/usrlocal/bin:" in r.stdout.split("PATH-AFTER=")[1]


def test_macos_with_homebrew_on_path_needs_no_sudo(tmp_path):
    r, calls, _ = _macos(tmp_path, brew_on_path=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if c.startswith(("curl", "installer", "sudo"))], calls


@pytest.mark.parametrize("fail, said", [
    ({"FAKE_SUDO_FAIL": "1"}, "Installing Homebrew needs an administrator account and its password"),
    ({"FAKE_CURL_FAIL": "1"}, "Could not download Homebrew's installer"),
    ({"FAKE_INSTALLER_FAIL": "1"}, "Homebrew did not install"),
])
def test_macos_stops_when_homebrew_cannot_install(tmp_path, fail, said):
    r, calls, _ = _macos(tmp_path, **fail)
    assert r.returncode == 1
    assert said in r.stderr
    assert not [c for c in calls if c.startswith("brew ")], calls


def test_macos_sudo_refresh_ends_with_an_interrupted_run(tmp_path):
    """Ctrl-C or a kill during Homebrew's installer: the loop that keeps sudo
    fresh must not outlive the run, or the person's terminal keeps a sudo
    ticket for good."""
    argv, env, calls, _ = _macos_setup(tmp_path, fast_sleep=True, FAKE_INSTALLER_WAIT="1")
    proc = subprocess.Popen(argv, env=env, cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)

    def refreshes():
        return calls.read_text().count("sudo -n true")

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not ("installer waiting" in calls.read_text() and refreshes() > 3):
            time.sleep(0.05)
        assert refreshes() > 3, calls.read_text()  # sudo kept fresh while the installer runs
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        deadline, last, stable = time.monotonic() + 10, refreshes(), 0
        while time.monotonic() < deadline and stable < 3:
            time.sleep(0.3)
            now = refreshes()
            stable, last = (stable + 1 if now == last else 0), now
        assert stable >= 3, "the sudo refresh outlived the run"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def test_macos_refuses_root_and_missing_command_line_tools(tmp_path):
    r, _, _ = _macos(tmp_path, brew_on_path=True, FAKE_UID="0")
    assert r.returncode == 1 and "not as root" in r.stderr
    (tmp_path / "clt").mkdir()
    r, calls, _ = _macos(tmp_path / "clt", brew_on_path=True, FAKE_NO_CLT="1")
    assert r.returncode == 1 and "xcode-select --install" in r.stderr
    assert not [c for c in calls if c.startswith("brew install")], calls


def _helper(tmp_path, os_type, *, root_owned_gitconfig=False, dst="/usr/local/bin/oto-git-credential-helper"):
    """install_credential_helper from the real installer: sudo and git are
    logging fakes, brew answers a prefix under ``tmp_path``; ``install`` is
    the real one with sudo's copy logged and never run. Returns the run, the
    calls and the prefix."""
    fakes, tools, prefix = tmp_path / "helperbin", tmp_path / "helpertools", tmp_path / "brew"
    for d in (fakes, tools, prefix / "bin", prefix / "etc"):
        d.mkdir(parents=True, exist_ok=True)
    bodies = {
        "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_CALLS"\n',
        "git": '#!/bin/sh\necho "git $*" >> "$FAKE_CALLS"\n',
        "brew": f'#!/bin/sh\n[ "$1" = --prefix ] && echo "{prefix}"\n',
        "id": '#!/bin/sh\ncase "$1" in -un) echo person ;; -g) echo 20 ;; *) echo 501 ;; esac\n',
    }
    for name, body in bodies.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    for name in ("bash", "install"):
        (tools / name).symlink_to(shutil.which(name))
    src = tmp_path / "oto-git-credential-helper"
    src.write_text("#!/bin/sh\n")
    if root_owned_gitconfig:
        conf = prefix / "etc" / "gitconfig"
        conf.write_text("")
        conf.chmod(0o444)
    calls = tmp_path / "helper.log"
    calls.touch()
    script = ('set -euo pipefail\n'
              'info() { echo "[INFO] $*"; }; ok() { echo "[OK] $*"; }\n'
              'warn() { echo "[WARN] $*"; }; err() { echo "[ERROR] $*" >&2; }\n'
              f'SUDO=sudo\nOS_TYPE={os_type}\nSKIP_CREDENTIAL_HELPER=false\n'
              f'CREDENTIAL_HELPER_SRC="{src}"\n'
              f'CREDENTIAL_HELPER_DST="{dst}"\n'
              + _baseline_function("install_credential_helper") + "install_credential_helper\n")
    r = subprocess.run([str(tools / "bash"), "-c", script], env={"PATH": f"{fakes}:{tools}", "FAKE_CALLS": str(calls)},
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    return r, calls.read_text().splitlines(), prefix


def test_linux_helper_creates_a_missing_folder_before_the_copy(tmp_path):
    folder = tmp_path / "usrlocal" / "bin"
    r, calls, _ = _helper(tmp_path, "linux", dst=f"{folder}/oto-git-credential-helper")
    assert r.returncode == 0, r.stdout + r.stderr
    made = calls.index(f"sudo install -d -m 0755 {folder}")
    copied = next(i for i, c in enumerate(calls) if c.endswith(f" {folder}/oto-git-credential-helper")
                  and c.startswith("sudo install -m 0755 "))
    assert made < copied
    assert ("sudo git config --system credential.https://github.com.helper "
            f"{folder}/oto-git-credential-helper") in calls


def test_linux_helper_leaves_an_existing_folders_mode_alone(tmp_path):
    # GNU `install -d -m 0755` resets the mode of a folder that exists: a
    # group-writable /usr/local/bin (Debian's staff layout) would lose it.
    folder = tmp_path / "usrlocal" / "bin"
    folder.mkdir(parents=True)
    r, calls, _ = _helper(tmp_path, "linux", dst=f"{folder}/oto-git-credential-helper")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in calls if " install -d " in c], calls


def test_macos_helper_goes_to_homebrew_with_no_sudo(tmp_path):
    r, calls, prefix = _helper(tmp_path, "macos")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (prefix / "bin" / "oto-git-credential-helper").is_file()
    assert not [c for c in calls if c.startswith("sudo")], calls
    assert f"git config --system credential.https://github.com.helper {prefix}/bin/oto-git-credential-helper" in calls


def test_macos_helper_in_another_accounts_homebrew_warns_and_goes_on(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes a read-only folder")
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin").chmod(0o555)
    try:
        r, calls, _ = _helper(tmp_path, "macos")
    finally:
        (prefix / "bin").chmod(0o755)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[WARN] Could not install the git credential helper" in r.stdout
    assert not [c for c in calls if c.startswith("git config")], calls


def test_macos_helper_gives_back_a_gitconfig_an_older_installer_left_to_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes a read-only file")
    r, calls, prefix = _helper(tmp_path, "macos", root_owned_gitconfig=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"sudo chown 501:20 {prefix}/etc/gitconfig" in calls


@pytest.mark.parametrize("os_type", ["macos", "linux"])
def test_sympy_goes_to_the_users_site_on_macos_only(tmp_path, os_type):
    fakes, tools = tmp_path / "sympybin", tmp_path / "sympytools"
    for d in (fakes, tools):
        d.mkdir()
    bodies = {
        "python3": '#!/bin/sh\necho "python3 $*" >> "$FAKE_CALLS"\n[ "$1" = -c ] && exit 1\nexit 0\n',
        "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_CALLS"\nexit 0\n',
        "brew": '#!/bin/sh\necho "brew $*" >> "$FAKE_CALLS"\n',
    }
    for name, body in bodies.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    (tools / "bash").symlink_to(shutil.which("bash"))
    calls = tmp_path / "sympy.log"
    calls.touch()
    script = ('set -euo pipefail\n'
              'info() { echo "[INFO] $*"; }; ok() { echo "[OK] $*"; }\n'
              'warn() { echo "[WARN] $*"; }; err() { echo "[ERROR] $*" >&2; }\n'
              f'SUDO=sudo\nOS_TYPE={os_type}\n'
              + _baseline_function("install_sympy") + "install_sympy\n")
    r = subprocess.run([str(tools / "bash"), "-c", script], env={"PATH": f"{fakes}:{tools}", "FAKE_CALLS": str(calls)},
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    logged = calls.read_text().splitlines()
    pins = re.findall(r'local sympy_ver="\$\{SYMPY_VERSION:-([^}]+)\}"', _BASELINE)
    if os_type == "macos":
        assert f"python3 -m pip install --quiet --user --break-system-packages sympy=={pins[0]}" in logged
        assert not [c for c in logged if c.startswith(("sudo", "brew"))], logged
    else:
        assert f"sudo python3 -m pip install --quiet sympy=={pins[0]}" in logged


def _npm_fn(tmp_path, fn, *, sudo="sudo", prefix=None, uid=501):
    """One npm helper of the real installer (with ``_npm_sudo``, which the
    repair asks) and the real ``find``, with fakes for npm (answering
    ``prefix``, failing without one), id and sudo. The files under
    ``tmp_path`` are this test's own: an ``id -u`` of 501 makes every one of
    them another account's, the test's own uid none. Returns the run and the
    calls logged."""
    fakes, tools, home = tmp_path / "npmbin", tmp_path / "npmtools", tmp_path / "home"
    for d in (fakes, tools, home / ".npm" / "_cacache"):
        d.mkdir(parents=True)
    (home / ".npm" / "_cacache" / "index").write_text("")
    bodies = {
        "npm": (f'#!/bin/sh\necho "{prefix}"\n' if prefix else "#!/bin/sh\nexit 1\n"),
        "id": '#!/bin/sh\ncase "$1" in -un) echo person ;; -g) echo 20 ;; *) echo "$FAKE_UID" ;; esac\n',
        "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_CALLS"\n',
    }
    for name, body in bodies.items():
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    for name in ("bash", "find"):
        (tools / name).symlink_to(shutil.which(name))
    calls = tmp_path / "npm.log"
    calls.touch()
    script = ('set -euo pipefail\n'
              'info() { echo "[INFO] $*"; }; ok() { echo "[OK] $*"; }\n'
              'warn() { echo "[WARN] $*"; }; err() { echo "[ERROR] $*" >&2; }\n'
              f'SUDO="{sudo}"\n' + "".join(_baseline_function(n) for n in dict.fromkeys(("_npm_sudo", fn)))
              + f'echo "answer=[$({fn})]"\n')
    r = subprocess.run([str(tools / "bash"), "-c", script],
                       env={"PATH": f"{fakes}:{tools}", "FAKE_CALLS": str(calls), "HOME": str(home),
                            "FAKE_UID": str(uid)},
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    return r, calls.read_text().splitlines()


def test_npm_needs_no_sudo_where_the_person_owns_the_prefix(tmp_path):
    # Homebrew's prefix on a Mac, nvm's on Linux; a lib/node_modules not made
    # yet is judged by the folder above it.
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True)
    r, _ = _npm_fn(tmp_path, "_npm_sudo", prefix=prefix)
    assert "answer=[]" in r.stdout, r.stdout + r.stderr


def test_npm_takes_sudo_for_a_system_prefix_or_an_unknown_one(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes everywhere")
    prefix = tmp_path / "usr"
    (prefix / "lib" / "node_modules").mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "lib" / "node_modules").chmod(0o555)
    try:
        (tmp_path / "a").mkdir()
        r, _ = _npm_fn(tmp_path / "a", "_npm_sudo", prefix=prefix)
        assert "answer=[sudo]" in r.stdout, r.stdout + r.stderr
    finally:
        (prefix / "lib" / "node_modules").chmod(0o755)
    (tmp_path / "b").mkdir()
    r, _ = _npm_fn(tmp_path / "b", "_npm_sudo")
    assert "answer=[sudo]" in r.stdout
    (tmp_path / "c").mkdir()
    r, _ = _npm_fn(tmp_path / "c", "_npm_sudo", sudo="")
    assert "answer=[]" in r.stdout


@pytest.mark.parametrize("foreign", [True, False])
def test_npm_cache_left_to_root_is_given_back(tmp_path, foreign):
    r, calls = _npm_fn(tmp_path, "_repair_npm_files", uid=501 if foreign else os.getuid())
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == ([f"sudo chown -R -h 501:20 {tmp_path / 'home' / '.npm'}"] if foreign else [])


def _prefix_with_root_leftovers(prefix: Path) -> None:
    """A Homebrew prefix after the 1.9.0 installer's `sudo npm install -g`:
    two packages and their links, beside a link and a file that are not npm's."""
    for pkg in ("@openai/codex", "npm"):
        (prefix / "lib" / "node_modules" / pkg).mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "codex").symlink_to(Path("..", "lib", "node_modules", "@openai", "codex", "bin", "codex.js"))
    (prefix / "bin" / "docker").symlink_to("/Applications/Docker.app/Contents/Resources/bin/docker")
    (prefix / "bin" / "oto-git-credential-helper").write_text("#!/bin/sh\n")


def test_npm_packages_left_to_root_in_the_persons_prefix_are_given_back(tmp_path):
    """Else the next CLI pin bump fails the re-pair: npm runs without sudo
    and cannot replace a package folder root owns."""
    prefix = tmp_path / "brew"
    _prefix_with_root_leftovers(prefix)
    r, calls = _npm_fn(tmp_path, "_repair_npm_files", prefix=prefix)
    assert r.returncode == 0, r.stdout + r.stderr
    given = {c.removeprefix("sudo chown -R -h 501:20 ") for c in calls}
    assert given == {str(tmp_path / "home" / ".npm"), str(prefix / "lib" / "node_modules" / "@openai"),
                     str(prefix / "lib" / "node_modules" / "npm"), str(prefix / "bin" / "codex")}
    assert "An older installer left" in r.stdout


def test_npm_files_already_the_persons_and_a_system_prefix_are_left_alone(tmp_path):
    prefix = tmp_path / "brew"
    _prefix_with_root_leftovers(prefix)
    (tmp_path / "own").mkdir()
    r, calls = _npm_fn(tmp_path / "own", "_repair_npm_files", prefix=prefix, uid=os.getuid())
    assert r.returncode == 0 and calls == []
    if os.geteuid() == 0:
        return
    # NodeSource's /usr is root's by design: only the cache is looked at.
    (prefix / "lib" / "node_modules").chmod(0o555)
    try:
        (tmp_path / "system").mkdir()
        r, calls = _npm_fn(tmp_path / "system", "_repair_npm_files", prefix=prefix)
    finally:
        (prefix / "lib" / "node_modules").chmod(0o755)
    assert r.returncode == 0 and calls == [f"sudo chown -R -h 501:20 {tmp_path / 'system' / 'home' / '.npm'}"]


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


def _dev_setup(tmp_path, *flags, active="", systemctl=True, node_major=None,
               node_at="/usr/bin/node"):
    """Run a copy of dev-setup.sh from a scratch platform root whose
    installer is a stub that logs and exits 7, so a run past the guard stops
    at its second step. PATH holds only logging fakes of node (answering
    ``node_major``, the pinned major by default), uv, docker, curl and sudo
    (which runs nothing), readlink (resolving every path to ``node_at``), id
    (a non-root user, so the script always goes through the fake sudo),
    systemctl unless ``systemctl`` is False, and the system tools the script
    runs before that step. Returns the run and the calls logged, systemctl's
    apart."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    for name in ("dev-setup.sh", "versions.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    shutil.copy2(REPO / "VERSIONS.md", root / "VERSIONS.md")
    (root / "scripts" / "install-baseline-tools.sh").write_text(
        'echo installer >> "$FAKE_CALLS"\nexit 7\n')
    node = re.search(r"^NODE_VERSION=(\S+)", (REPO / "VERSIONS.md").read_text(), re.M)
    assert node, "VERSIONS.md has no NODE_VERSION"
    major = node_major or node.group(1).split(".")[0]
    bodies = {name: f'#!/bin/sh\necho "{name} $*" >> "$FAKE_CALLS"\n' for name in ("uv", "docker", "curl")}
    bodies["node"] = f'#!/bin/sh\necho "node $*" >> "$FAKE_CALLS"\necho v{major}.0.0\n'
    # `curl ... | sudo -E bash -`: the fake drains the pipe BEFORE it records,
    # so its line follows curl's (curl has exited once the pipe closes). With
    # the shell's own `read`: the PATH below has no `cat`.
    bodies["sudo"] = ('#!/bin/sh\nif [ "$1" = -E ]; then while IFS= read -r _; do :; done; fi\n'
                      'echo "sudo $*" >> "$FAKE_CALLS"\nexit 0\n')
    bodies["readlink"] = f'#!/bin/sh\necho "{node_at}"\n'
    bodies["id"] = '#!/bin/sh\necho 1000\n'
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


@pytest.mark.parametrize("major", ["24", None])
def test_dev_setup_stops_on_a_node_outside_usr_before_any_change(tmp_path, major):
    # A version manager's node first on PATH hides the one NodeSource would
    # install, whatever its major: the run stops before sudo, curl or apt.
    nvm = "/home/tester/.nvm/versions/node/v26.12.0/bin/node"
    r, _, calls = _dev_setup(tmp_path, node_major=major, node_at=nvm)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"node at {nvm} is not under /usr" in r.stderr
    assert not [c for c in calls if c.startswith(("sudo", "curl", "installer"))], calls


def test_dev_setup_moves_node_through_one_nodesource_source(tmp_path):
    # A host the baseline installer set up first carries nodesource.list for
    # its major: it goes before NodeSource's own setup writes the new source.
    r, _, calls = _dev_setup(tmp_path, node_major="24")
    assert r.returncode == 7, r.stdout + r.stderr
    pin = re.search(r"^NODE_VERSION=(\d+)\.", (REPO / "VERSIONS.md").read_text(), re.M).group(1)
    steps = [c for c in calls if c.startswith(("sudo", "curl"))]
    assert steps == [
        "sudo rm -f /etc/apt/sources.list.d/nodesource.list",
        f"curl -fsSL https://deb.nodesource.com/setup_{pin}.x",
        "sudo -E bash -",
        "sudo apt-get install -y nodejs",
    ], steps
    assert calls[-1] == "installer"


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

import gzip
import io

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


def _snapshot_dir(tmp_path, *, version="1.8.0", shape="1.9", folder="upgrade-20261030-120000") -> Path:
    """An upgrade.sh snapshot: the dump under its timestamped name, the env
    and compose copies, and the manifest a 1.9.0 (``trees=none``) or a 1.8.x
    (``agents=skipped``) upgrade.sh wrote."""
    d = tmp_path / folder
    d.mkdir()
    dump = "otodock-otodock-20261030-120000.sql.gz"
    with gzip.open(d / dump, "wb") as fh:
        fh.write(b"SELECT 1;\n")
    for name in (".env", "docker-compose.yml"):
        (d / name).write_text("# the copy upgrade.sh made\n")
    trees = ["trees=none"] if shape == "1.9" else ["agents=skipped", "ssh_keys=skipped"]
    lines = [f"otodock_version={version}", "created_at=2026-10-30T12:00:00Z", "topology=docker",
             "project=otodock", "database=otodock", f"db={dump}", "env=1", *trees,
             "credential_keys=" + json.dumps({KEY_A: "JWT_SECRET"}), "errors=0", "complete=1"]
    (d / "manifest").write_text("\n".join(lines) + "\n")
    return d


@pytest.mark.parametrize("shape, folder", [
    ("1.9", "upgrade-20261030-120000"),
    ("1.8", "renamed-by-hand"),          # the 1.8.x manifest's own shape
    ("1.8", "upgrade-20261030-120000-2"),
], ids=["trees-none", "1.8-shape", "1.8-folder-name"])
def test_an_upgrade_snapshot_restores_the_database_and_says_it_holds_no_trees(
        tmp_path, fakebin, shape, folder):
    containers, vol = _stack(tmp_path)
    d = _snapshot_dir(tmp_path, shape=shape, folder=folder)
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "the snapshot upgrade.sh takes before it changes anything" in r.stderr
    assert "copy .env\n  and the compose files from" in r.stderr
    assert "not readable" not in r.stderr and "skipped the trees" not in r.stderr
    assert _fed(tmp_path) and (vol / "stale.txt").exists()


def test_a_1_8_shaped_manifest_with_a_host_line_is_a_backup_run(tmp_path, fakebin):
    """backup.sh always records the host: its skipped trees keep their reason."""
    containers, _ = _stack(tmp_path)
    d = _snapshot_dir(tmp_path, shape="1.8", folder="renamed-by-hand")
    (d / "manifest").write_text((d / "manifest").read_text() + "host=box\n")
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "backup skipped the trees" in r.stderr and "upgrade.sh takes" not in r.stderr


def test_an_older_upgrade_snapshot_says_the_running_release_migrates_it_again(tmp_path, fakebin):
    containers, _ = _stack(tmp_path)
    d = _snapshot_dir(tmp_path, version="1.7.1")
    r = _restore(tmp_path, fakebin, containers, d)
    assert r.returncode == 0, r.stderr
    assert "taken on OtoDock 1.7.1 and this install runs 1.8.0" in r.stderr
    assert "migrates the restored database to 1.8.0 again" in r.stderr
    # A backup.sh run as old says nothing of the kind.
    (tmp_path / "two").mkdir()
    containers2, _ = _stack(tmp_path / "two")
    r = _restore(tmp_path / "two", fakebin, containers2,
                 _run_dir(tmp_path / "two", version="1.7.1", keys={KEY_A: "JWT_SECRET"}))
    assert r.returncode == 0, r.stderr
    assert "migrates the restored database" not in r.stderr


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


# ── compose.sh: Collabora's scheme and the Docker Engine floor ─────────

# A stand-in docker for compose.sh: `version` answers FAKE_API; every other
# call is logged with the COLLABORA_SSL_TERMINATION it saw.
FAKE_COMPOSE_DOCKER = r'''#!/bin/sh
if [ "$1" = version ]; then echo "${FAKE_API-1.51}"; exit 0; fi
if [ "$1" = compose ] && [ "$2" = version ]; then echo "${FAKE_COMPOSE-2.35.1}"; exit 0; fi
echo "$* term=${COLLABORA_SSL_TERMINATION:-unset}" >> "$FAKE_LOG"
exit 0
'''


@pytest.mark.parametrize("config_env, shell, api, args, rc, term", [
    ("DASHBOARD_PUBLIC_URL=https://otodock.example.com\n", {}, "1.51", ["up", "-d"], 0, "true"),
    ('DASHBOARD_PUBLIC_URL="HTTPS://otodock.example.com"\n', {}, "1.51", ["up", "-d"], 0, "true"),
    ("DASHBOARD_PUBLIC_URL=http://192.168.1.5:8400\nCOLLABORA_SSL_TERMINATION=true\n", {}, "1.51",
     ["up", "-d"], 0, "false"),
    ("", {}, "1.45", ["up", "-d"], 0, "false"),
    ("DASHBOARD_PUBLIC_URL=http://h:8400\n", {"COLLABORA_SSL_TERMINATION": "true"}, "1.51", ["up"], 0, "true"),
    ("", {}, "1.44", ["up", "-d"], 1, None),
    ("", {}, "1.44", ["logs", "-f"], 0, "false"),
    ("", {}, "1.44", ["down"], 0, "false"),
    ("export DASHBOARD_PUBLIC_URL=https://x.example  # the public address\n", {}, "1.51", ["up"], 0, "true"),
    ("DASHBOARD_PUBLIC_URL = 'https://x.example'\n", {}, "1.51", ["up"], 0, "true"),
])
def test_compose_sh_derives_collaboras_scheme_and_needs_engine_26(tmp_path, config_env, shell, api, args, rc, term):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    for name in ("compose.sh", "versions.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    (root / "config.env").write_text("POSTGRES_PASSWORD=x\n" + config_env)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_COMPOSE_DOCKER)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COLLABORA_", "OTODOCK_"))}
    env.update({"PATH": f"{bindir}:{env['PATH']}", "FAKE_LOG": str(log), "FAKE_API": api, **shell})
    r = subprocess.run(["bash", str(root / "scripts" / "compose.sh"), *args], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == rc, r.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    compose_calls = [c for c in calls if c.startswith("compose ") and " version" not in c]
    if rc:
        assert "Docker Engine 26 or later" in r.stderr and not compose_calls
    else:
        assert compose_calls and compose_calls[-1].endswith(f"term={term}")



def test_compose_sh_needs_compose_2_26(tmp_path):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    for name in ("compose.sh", "versions.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    (root / "config.env").write_text("POSTGRES_PASSWORD=x\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_COMPOSE_DOCKER)
    (bindir / "docker").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COLLABORA_", "OTODOCK_"))}
    env.update({"PATH": f"{bindir}:{env['PATH']}", "FAKE_LOG": str(tmp_path / "log"),
                "FAKE_COMPOSE": "2.25.0"})
    r = subprocess.run(["bash", str(root / "scripts" / "compose.sh"), "up", "-d"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "Docker Compose 2.26 or later" in r.stderr


# ── install.sh: the Docker floor and the editor's scheme line ──────────

def _install_steps(tmp_path, env_text, api="1.51", compose="2.35.1", script="docker_floor; collabora_scheme_line"):
    (tmp_path / ".env").write_text(env_text)
    (tmp_path / ".env").chmod(0o600)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_COMPOSE_DOCKER)
    (bindir / "docker").chmod(0o755)
    body = ('say() { echo "$*"; }\nfail() { echo "$*" >&2; exit 1; }\n'
            + "".join(_install_function(n) for n in
                      ("env_value", "at_least", "docker_floor", "collabora_scheme_line"))
            + script + "\n")
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(tmp_path / "log"),
           "FAKE_API": api, "FAKE_COMPOSE": compose}
    return subprocess.run(["bash", "-euo", "pipefail", "-c", body], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("api, compose, message", [
    ("1.44", "2.35.1", "Docker Engine 26 or later"),
    ("1.51", "2.25.0", "Docker Compose 2.26 or later"),
    ("", "2.35.1", "Docker Engine 26 or later"),
])
def test_install_refuses_an_old_docker_before_writing_anything(tmp_path, api, compose, message):
    r = _install_steps(tmp_path, "", api=api, compose=compose, script="docker_floor")
    assert r.returncode == 1 and message in r.stderr


@pytest.mark.parametrize("env_text, want", [
    ("DASHBOARD_PUBLIC_URL=https://x.example\n", ["COLLABORA_SSL_TERMINATION=true"]),
    ("DASHBOARD_PUBLIC_URL=http://x:8400\n", []),
    ("#DASHBOARD_PUBLIC_URL=https://x\n", []),
    ("DASHBOARD_PUBLIC_URL=https://x.example\nexport COLLABORA_SSL_TERMINATION=false\n",
     ["COLLABORA_SSL_TERMINATION=true"]),
    ("DASHBOARD_PUBLIC_URL=https://x.example\n#COLLABORA_SSL_TERMINATION=false\n",
     ["#COLLABORA_SSL_TERMINATION=false", "COLLABORA_SSL_TERMINATION=true"]),
    ("DASHBOARD_PUBLIC_URL=http://x:8400\nCOLLABORA_SSL_TERMINATION=true\n",
     ["COLLABORA_SSL_TERMINATION=false"]),
])
def test_install_writes_the_editors_scheme_line(tmp_path, env_text, want):
    r = _install_steps(tmp_path, env_text)
    assert r.returncode == 0, r.stderr
    env = tmp_path / ".env"
    assert [x for x in env.read_text().splitlines() if "COLLABORA_SSL_TERMINATION" in x] == want
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and not (tmp_path / ".env.scheme").exists()
    r = _install_steps(tmp_path, env.read_text())
    assert r.returncode == 0 and "setting COLLABORA_SSL_TERMINATION" not in r.stdout


def test_the_image_boot_check_reads_pastas_first_line():
    """With the AVX2 build in the image, pasta's version is its first line on
    every runner; a warning ahead of it (no AVX2 binary on an AVX2 host) is
    what the check must refuse, so it never searches past the first line."""
    boot = (SCRIPTS / "image-boot-check.sh").read_text()
    cut = next(line for line in boot.splitlines() if line.startswith('got="${got%%'))
    for output, first in (
        ("pasta 2026_10_02.cba3570\nCopyright Red Hat\n", "pasta 2026_10_02.cba3570"),
        ("Can't run AVX2 build, using non-AVX2 version: No such file or directory\n"
         "pasta 2026_10_02.cba3570\n", "Can't run AVX2 build, using non-AVX2 version: No such file or directory"),
    ):
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\ngot="$1"\n{cut}\nprintf %s "$got"', "_", output],
                           capture_output=True, text=True, timeout=30)
        assert r.stdout == first, r.stderr
