"""scripts/upgrade.sh, run against a fake ``docker`` and ``curl`` on PATH.

The upgrade checks the target release's compose files against the install's
own .env before it changes anything, writes no line of .env but
OTODOCK_VERSION and (when needed) COMPOSE_FILE, builds COMPOSE_FILE base first
without duplicating a file on a re-run, and has two failure regimes: before
``docker compose up`` it puts the files back, after it it leaves the new files
and points at the dump. The ``config`` checks run through the real Compose
loader where one is installed (it needs no daemon), so the refusal of a
hostname in OTO_AUDIOSOCKET_PUBLIC_HOST is the loader's own. install.sh lists
an override after the base file on the COMPOSE_FILE line it writes.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
REAL_DOCKER = shutil.which("docker")

# A stand-in for the docker CLI. Every call is logged as a JSON argv line.
# `ps` applies the label filters (and -a) to FAKE_CONTAINERS; `exec` answers
# like pg_dump, and `psql -tAc` with FAKE_DB_VERSION, FAKE_FLOOR or
# FAKE_KEYS; `inspect` reports FAKE_HEALTH for a health format and
# FAKE_STATUS (running) for a status one; `image inspect` knows
# FAKE_LOCAL_IMAGES; `compose config` runs the real docker when REAL_DOCKER is
# set, else exits FAKE_CONFIG_RC (FAKE_BARE_CONFIG_RC for a call without -f,
# the check of the files in place); `compose pull` and `compose up` exit
# FAKE_PULL_RC and FAKE_UP_RC (FAKE_UP_RC for the first up only).
# The recover-bin carry: `compose config --volumes` prints FAKE_VOLUMES;
# `inspect` of the mounts prints FAKE_MOUNTS, of the writable layer
# FAKE_SIZE_RW; `diff` prints the bin line when FAKE_DIFF_BIN is set, then
# FAKE_DIFF_FILLER more lines; `exec ... du` prints FAKE_DU_K; `info -f`
# prints FAKE_DOCKER_ROOT; `cp` writes a stand-in archive (FAKE_CP_RC);
# `compose run` logs the bytes it read to FAKE_RUN_LOG (FAKE_RUN_RC). After
# the first `compose up`, `ps` lists FAKE_CONTAINERS_AFTER_UP when it is set
# (the server that up started).
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
cs = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
if args[:1] == ["info"]:
    if "-f" in args:
        print(os.environ.get("FAKE_DOCKER_ROOT", ""))
    sys.exit(0)
if args[:1] == ["diff"]:
    out = sys.stdout
    if os.environ.get("FAKE_DIFF_BIN"):
        out.write("A /opt/otodock/recover-bin\n")
    for i in range(int(os.environ.get("FAKE_DIFF_FILLER", "3"))):
        out.write(f"A /opt/otodock/proxy/__pycache__/module_{i:06d}.cpython-313.pyc\n")
    sys.exit(0)
if args[:1] == ["cp"]:
    sys.stdout.write("recover-bin/\n" * 10)
    sys.exit(int(os.environ.get("FAKE_CP_RC", "0")))
if args[:1] == ["ps"]:
    want_all = "-a" in args
    labels = [args[i + 1].partition("=")[2] for i, a in enumerate(args) if a == "--filter"]
    if os.environ.get("FAKE_CONTAINERS_AFTER_UP"):
        with open(os.environ["FAKE_LOG"]) as fh:
            if any(json.loads(line)[:2] == ["compose", "up"] for line in fh):
                cs = json.loads(os.environ["FAKE_CONTAINERS_AFTER_UP"])
    for c in cs:
        if not want_all and not c.get("running", True):
            continue
        if all(c["labels"].get(l.partition("=")[0]) == l.partition("=")[2] for l in labels):
            print(c["id"])
    sys.exit(0)
if args[:1] == ["exec"]:
    if "du" in args:
        print(os.environ.get("FAKE_DU_K", "8") + "\t/opt/otodock/recover-bin")
        sys.exit(0)
    if "psql" in args and "-tAc" in args:
        sql = args[args.index("-tAc") + 1]
        if "schema_meta" in sql:
            print(os.environ.get("FAKE_DB_VERSION", ""))
        elif "secret_floor" in sql:
            print(os.environ.get("FAKE_FLOOR", ""))
        else:
            print(os.environ.get("FAKE_KEYS", ""))
        sys.exit(0)
    print("-- dump of " + args[1])
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    if "Mounts" in fmt:
        print("\n".join(os.environ.get("FAKE_MOUNTS", "/opt/otodock/agents").split()))
    elif "SizeRw" in fmt:
        print(os.environ.get("FAKE_SIZE_RW", "4096"))
    elif "Health" in fmt:
        print(os.environ.get("FAKE_HEALTH", "healthy"))
    else:
        print(os.environ.get("FAKE_STATUS", "running"))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if args[2] in os.environ.get("FAKE_LOCAL_IMAGES", "").split() else 1)
if args[:1] == ["compose"]:
    rest, sub, i = args[1:], None, 0
    while i < len(rest):
        a = rest[i]
        if a in ("-f", "--file", "--env-file", "--project-directory", "-p"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        sub = a
        break
    if sub == "version":
        sys.exit(0)
    if sub == "config":
        if os.environ.get("REAL_DOCKER"):
            os.execv(os.environ["REAL_DOCKER"], [os.environ["REAL_DOCKER"], *args])
        if "--images" in args:
            print("\n".join(os.environ.get("FAKE_IMAGES", "").split()))
        if "--volumes" in args:
            print("\n".join(os.environ.get("FAKE_VOLUMES", "").split()))
        if "-f" not in args:
            sys.exit(int(os.environ.get("FAKE_BARE_CONFIG_RC", "0")))
        sys.exit(int(os.environ.get("FAKE_CONFIG_RC", "0")))
    if sub == "pull":
        sys.exit(int(os.environ.get("FAKE_PULL_RC", "0")))
    if sub == "run":
        data = sys.stdin.buffer.read()
        with open(os.environ["FAKE_RUN_LOG"], "a") as fh:
            fh.write(json.dumps({"read": len(data), "args": args}) + "\n")
        sys.exit(int(os.environ.get("FAKE_RUN_RC", "0")))
    if sub == "up":
        with open(os.environ["FAKE_LOG"]) as fh:
            ups = sum(1 for line in fh if json.loads(line)[:2] == ["compose", "up"])
        sys.exit(int(os.environ.get("FAKE_UP_RC", "0")) if ups == 1 else 0)
sys.exit(0)
'''

# A stand-in for curl: the releases/latest redirect answers FAKE_LATEST_URL;
# a raw.githubusercontent.com URL is served from FAKE_RAW_ROOT/<ref>/<path>
# (when the file is not there: exit 22 with the line curl -fsS prints).
FAKE_CURL = r'''#!/usr/bin/env python3
import os, shutil, sys
args, dest, fmt, url = sys.argv[1:], None, None, None
i = 0
while i < len(args):
    a = args[i]
    if a == "-o":
        dest = args[i + 1]; i += 2; continue
    if a == "-w":
        fmt = args[i + 1]; i += 2; continue
    if not a.startswith("-"):
        url = a
    i += 1
with open(os.environ["FAKE_CURL_LOG"], "a") as fh:
    fh.write(url + "\n")
def missing():
    sys.stderr.write("curl: (22) The requested URL returned error: 404\n")
    sys.exit(22)
if url.endswith("/releases/latest"):
    if not os.environ.get("FAKE_LATEST_URL"):
        missing()
    sys.stdout.write(os.environ["FAKE_LATEST_URL"])
    sys.exit(0)
prefix = "https://raw.githubusercontent.com/OtoDock/oto-dock/"
root = os.environ.get("FAKE_RAW_ROOT", "")
src = os.path.join(root, url[len(prefix):]) if url.startswith(prefix) and root else ""
if not src or not os.path.isfile(src):
    missing()
shutil.copyfile(src, dest)
'''

# A stand-in for `ip -o -4 addr show`: one line per address in FAKE_IP_ADDRS,
# each call logged to FAKE_IP_LOG.
FAKE_IP = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ.get("FAKE_IP_LOG", os.devnull), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
for n, a in enumerate(os.environ.get("FAKE_IP_ADDRS", "127.0.0.1").split(), 1):
    print(f"{n}: eth{n}    inet {a}/24 brd 0.0.0.0 scope global eth{n}\\       valid_lft forever")
'''

PG = {"id": "pg-t2", "labels": {"com.docker.compose.project": "otodock",
                                "com.docker.compose.service": "otodock-postgres"}}
PROXY = {"id": "proxy-t2", "labels": {"com.docker.compose.project": "otodock",
                                      "com.docker.compose.service": "otodock-proxy"}}
PHONE = {"id": "phone-t2", "labels": {"com.docker.compose.project": "otodock",
                                      "com.docker.compose.service": "otodock-phone"}}
# A community MCP's container: another compose project, a name free to say anything.
EVIL_PG = {"id": "evil-pg", "labels": {"com.docker.compose.project": "otodock-x-mcp-otodock-postgres",
                                       "com.docker.compose.service": "otodock-postgres"}}

OLD_EXAMPLE = "PROXY_PORT=8400\nDASHBOARD_PUBLIC_URL=http://localhost:8400\n"
NEW_EXAMPLE = OLD_EXAMPLE + (
    "# OTO_PHONE_BIND=\nPROXY_BIND_IP=\nTRUSTED_PROXY=\nADMISSION_QUEUE_WAIT_S=30\n"
)

SECRET_ENV = (
    "# OtoDock configuration\n"
    "POSTGRES_PASSWORD=placeholder-db\n"
    "\n"
    "#OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n"
    "#TRUSTED_PROXY=\n"
    "PROXY_API_KEY=placeholder-api\n"
    "JWT_SECRET=placeholder-jwt\n"
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    _write_exec(b / "docker", FAKE_DOCKER)
    _write_exec(b / "curl", FAKE_CURL)
    _write_exec(b / "ip", FAKE_IP)
    return b


@pytest.fixture
def raw(tmp_path):
    """The published files the fake curl serves: v1.6.1's example, v1.7.0's release."""
    root = tmp_path / "raw"
    (root / "v1.6.1").mkdir(parents=True)
    (root / "v1.6.1" / "config.env.example").write_text(OLD_EXAMPLE)
    rel = root / "v1.7.0"
    rel.mkdir()
    (rel / "docker-compose.yml").write_text("# the 1.7.0 base file\n")
    (rel / "docker-compose.phone.yml").write_text("# the 1.7.0 phone overlay\n")
    (rel / "config.env.example").write_text(NEW_EXAMPLE)
    return root


@pytest.fixture
def raw_1_8(raw):
    """The published files of v1.8.0 as well: the yes/no rule applies from there."""
    rel = raw / "v1.8.0"
    rel.mkdir()
    (rel / "docker-compose.yml").write_text("# the 1.8.0 base file\n")
    (rel / "docker-compose.phone.yml").write_text("# the 1.8.0 phone overlay\n")
    (rel / "config.env.example").write_text(NEW_EXAMPLE)
    return raw


@pytest.fixture
def candidate(tmp_path):
    """A --compose-dir holding the real compose pair of this tree."""
    d = tmp_path / "candidate"
    d.mkdir()
    for name in ("docker-compose.yml", "docker-compose.phone.yml"):
        shutil.copy2(REPO / name, d / name)
    (d / "config.env.example").write_text(NEW_EXAMPLE)
    return d


def _install(tmp_path, env_text: str, *, phone_file: bool = True, override: str | None = None) -> Path:
    inst = tmp_path / "otodock"
    inst.mkdir()
    (inst / ".env").write_text(env_text)
    (inst / ".env").chmod(0o600)
    (inst / "docker-compose.yml").write_text(
        "services:\n  otodock-proxy:\n    image: ghcr.io/otodock/otodock-proxy:${OTODOCK_VERSION:-1.6.1}\n")
    if phone_file:
        (inst / "docker-compose.phone.yml").write_text("# the 1.6.1 phone overlay\n")
    if override is not None:
        (inst / "docker-compose.override.yml").write_text(override)
    return inst


def _run(inst, tmp_path, fakebin, *args, containers=(PG, PROXY), real_config=False, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "OTODOCK_", "COMPOSE_", "OTO_"))}
    env.update({
        "PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(list(containers)),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
        "FAKE_RAW_ROOT": str(tmp_path / "raw"),
        "FAKE_RUN_LOG": str(tmp_path / "run.log"),
    })
    if real_config:
        env["REAL_DOCKER"] = REAL_DOCKER
    env.update(extra)
    return subprocess.run(
        ["bash", str(SCRIPTS / "upgrade.sh"), *args], cwd=inst, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
    )


def _calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _compose_subcommands(tmp_path) -> list[str]:
    return [" ".join(a[1:3]) for a in _calls(tmp_path) if a[:1] == ["compose"]]


def _changes(tmp_path) -> list[list[str]]:
    """Every docker call that changes something: the read-only psql SELECTs
    (the database's version, the key fingerprints) are not among them."""
    return [a for a in _calls(tmp_path)
            if not (a[:1] == ["exec"] and "psql" in a and "-tAc" in a) and a[:1] != ["inspect"]]


def _did(tmp_path, sub: str) -> bool:
    return any(a[:1] == ["compose"] and sub in a for a in _calls(tmp_path))


def _tree(inst: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in inst.iterdir() if p.is_file()}


def _snapshots(inst: Path) -> list[Path]:
    return sorted((inst / "backups").glob("upgrade-*")) if (inst / "backups").is_dir() else []


def _compose_available() -> bool:
    if REAL_DOCKER is None:
        return False
    r = subprocess.run([REAL_DOCKER, "compose", "version"], capture_output=True, timeout=30)
    return r.returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose is not installed")


def _plain_env(**extra) -> dict[str, str]:
    """PATH and HOME only: no shell variable of the host reaches the compose loader."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"), **extra}

PHONE_LINE = "COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml\n"


# ── the check against the real .env (the compose loader's own contract) ──────

@needs_compose
def test_the_compose_pair_accepts_a_sample_env_and_refuses_a_hostname(tmp_path):
    def check(extra: str):
        env_file = tmp_path / "sample.env"
        env_file.write_text("POSTGRES_PASSWORD=sample-pw\n" + extra)
        return subprocess.run(
            [REAL_DOCKER, "compose", "--project-directory", str(tmp_path), "--env-file", str(env_file),
             "-f", str(REPO / "docker-compose.yml"), "-f", str(REPO / "docker-compose.phone.yml"),
             "config", "-q"],
            capture_output=True, text=True, timeout=60, env=_plain_env(),
        )
    assert check("").returncode == 0
    assert check("OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n").returncode == 0
    refused = check("OTO_AUDIOSOCKET_PUBLIC_HOST=pbx.example.lan\n")
    assert refused.returncode != 0
    assert "pbx.example.lan" in refused.stderr
    # OTO_PHONE_BIND takes the publish over, so the hostname stays a dial target only.
    assert check("OTO_AUDIOSOCKET_PUBLIC_HOST=pbx.example.lan\nOTO_PHONE_BIND=192.168.1.10\n").returncode == 0


@needs_compose
def test_a_hostname_stops_the_upgrade_before_anything_changes(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=pbx.example.lan\n")
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate), real_config=True)
    assert r.returncode != 0
    assert "OTO_PHONE_BIND" in r.stderr
    assert "stopped before changing anything" in r.stderr
    assert _tree(inst) == before
    assert _snapshots(inst) == []
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    assert not _did(tmp_path, "pull") and not _did(tmp_path, "up")


@needs_compose
def test_the_real_pair_upgrades_a_phone_install(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             real_config=True, FAKE_LOCAL_IMAGES="unused")
    # the candidate's images are not local here: the run stops before up and puts the files back
    assert r.returncode != 0, r.stdout
    assert "neither on the registry nor on this host" in r.stderr
    assert (inst / ".env").read_text() == SECRET_ENV + PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n"
    assert not _did(tmp_path, "up")
    images = subprocess.run(
        [REAL_DOCKER, "compose", "--env-file", str(inst / ".env"), "-f", str(candidate / "docker-compose.yml"),
         "-f", str(candidate / "docker-compose.phone.yml"), "config", "--images"],
        capture_output=True, text=True, timeout=60, cwd=inst, env=_plain_env(OTODOCK_VERSION="1.7.0"),
    ).stdout.split()
    assert "ghcr.io/otodock/otodock-proxy:1.7.0" in images
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             real_config=True, FAKE_LOCAL_IMAGES=" ".join(images))
    assert r.returncode == 0, r.stderr
    assert (inst / "docker-compose.yml").read_bytes() == (candidate / "docker-compose.yml").read_bytes()
    assert (inst / "docker-compose.phone.yml").read_bytes() == (candidate / "docker-compose.phone.yml").read_bytes()
    assert _did(tmp_path, "--ignore-pull-failures")
    assert ["compose", "up", "-d", "--remove-orphans"] in _calls(tmp_path)


# ── arguments ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args, message", [
    (["--to", "1.7"], "not a release version"),
    (["--to", "latest"], "not a release version"),
    (["--to", "1.7.0;rm -rf /"], "not a release version"),
    (["--compose-dir", "/nowhere"], "needs --to"),
    (["--to", "1.7.0-rc1"], "--compose-dir"),
    (["--to", "1.7.0", "--compose-dir", "/nowhere"], "has no docker-compose.yml"),
])
def test_bad_arguments_are_refused_before_any_docker_call(tmp_path, fakebin, raw, args, message):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, *args)
    assert r.returncode != 0
    assert message in r.stderr
    assert _calls(tmp_path) == []
    assert _tree(inst) == before


def test_usage(tmp_path, fakebin):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--bogus")
    assert r.returncode == 2
    assert "usage:" in r.stderr
    r = _run(inst, tmp_path, fakebin, "--help")
    assert r.returncode == 0
    assert "--dry-run" in r.stdout


def test_not_an_install_dir_or_a_source_checkout(tmp_path, fakebin, raw):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(empty, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode != 0 and "no OtoDock install here" in r.stderr
    inst = _install(tmp_path, SECRET_ENV)
    (inst / "docker-compose.build.yml").write_text("services: {}\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode != 0 and "source checkout" in r.stderr


def test_the_latest_release_comes_from_the_releases_redirect(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--dry-run",
             FAKE_LATEST_URL="https://github.com/OtoDock/oto-dock/releases/tag/v1.7.0")
    assert r.returncode == 0, r.stderr
    assert "upgrading from 1.6.1 to 1.7.0" in r.stdout
    urls = (tmp_path / "curl.log").read_text().splitlines()
    assert "https://raw.githubusercontent.com/OtoDock/oto-dock/v1.7.0/docker-compose.yml" in urls


def test_a_downgrade_is_refused(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + "OTODOCK_VERSION=1.7.0\n")
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.6.1")
    assert r.returncode != 0
    assert "refusing to move it back" in r.stderr
    assert _tree(inst) == before


def test_dry_run_changes_nothing(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE, override="services: {}\n")
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "v1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "set OTODOCK_VERSION=1.7.0 in .env" in r.stdout
    assert ("set COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml:docker-compose.override.yml"
            in r.stdout)
    assert "ADMISSION_QUEUE_WAIT_S" in r.stdout
    assert _tree(inst) == before
    assert not (inst / "backups").exists()
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    assert not _did(tmp_path, "pull") and not _did(tmp_path, "up")


# ── .env: the version line, nothing else ─────────────────────────────────────

@pytest.mark.parametrize("version_line", [
    "OTODOCK_VERSION=1.6.1\n",
    "#OTODOCK_VERSION=1.6.1\n",
    "  # OTODOCK_VERSION=\n",
    "",
])
def test_env_gets_the_version_line_and_nothing_else(tmp_path, fakebin, raw, version_line):
    original = SECRET_ENV + version_line + PHONE_LINE + "#DASHBOARD_PUBLIC_URL=http://your-server:8400\n"
    inst = _install(tmp_path, original)
    ino = (inst / ".env").stat().st_ino
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    after = (inst / ".env").read_text().splitlines()
    before = original.splitlines()
    assert after.count("OTODOCK_VERSION=1.7.0") == 1
    if version_line:
        i = before.index(version_line.rstrip("\n"))
        assert after[i] == "OTODOCK_VERSION=1.7.0"
        assert after[:i] + after[i + 1:] == before[:i] + before[i + 1:]
    else:
        assert after == before + ["OTODOCK_VERSION=1.7.0"]
    # in place (the running proxy has it mounted), still private
    assert (inst / ".env").stat().st_ino == ino
    assert stat.S_IMODE((inst / ".env").stat().st_mode) == 0o600
    # the new settings are named, never written; the active example value is not injected
    text = (inst / ".env").read_text()
    for key in ("ADMISSION_QUEUE_WAIT_S", "PROXY_BIND_IP", "OTO_PHONE_BIND"):
        assert key in r.stdout
        assert key not in text
    assert "TRUSTED_PROXY" not in r.stdout.split("does not set")[-1]  # .env carries it commented
    assert "DASHBOARD_PUBLIC_URL=http://localhost" not in text
    assert text.count("placeholder-api") == 1 and text.count("placeholder-db") == 1


def test_a_shell_variable_does_not_override_the_run(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0",
             OTODOCK_VERSION="1.6.1", COMPOSE_FILE="docker-compose.yml")
    assert r.returncode == 0, r.stderr
    assert "your shell sets OTODOCK_VERSION" in r.stderr
    assert "OTODOCK_VERSION=1.7.0" in (inst / ".env").read_text().splitlines()


# ── COMPOSE_FILE: base first, an override once, the phone choice kept ────────

def _compose_file_lines(inst: Path) -> list[str]:
    return [line for line in (inst / ".env").read_text().splitlines() if line.startswith("COMPOSE_FILE=")]


def test_an_override_is_added_after_the_base_once(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE, override="services: {}\n")
    want = ["COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml:docker-compose.override.yml"]
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == want
    first = (inst / ".env").read_bytes()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")  # a re-run changes nothing
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == want
    assert (inst / ".env").read_bytes() == first
    # the check ran on the files the stack loads: the new pair, then the override
    check = next(a for a in _calls(tmp_path) if a[:1] == ["compose"] and "-f" in a)
    files = [check[i + 1] for i, a in enumerate(check) if a == "-f"]
    assert [Path(f).name for f in files] == [
        "docker-compose.yml", "docker-compose.phone.yml", "docker-compose.override.yml"]
    assert files[2] == "docker-compose.override.yml"  # the operator's own, not a download


def test_no_line_and_no_phone_writes_no_line(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV, override="services: {}\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == []  # compose loads the base and the override by itself


@pytest.mark.parametrize("override, want", [
    (None, "COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml"),
    ("services: {}\n", "COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml:docker-compose.override.yml"),
])
def test_no_line_with_a_phone_container_writes_the_full_list(tmp_path, fakebin, raw, override, want):
    inst = _install(tmp_path, SECRET_ENV, override=override)
    stopped_phone = {**PHONE, "running": False}
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG, PROXY, stopped_phone))
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == [want]


def test_a_phone_opt_out_is_kept(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + "COMPOSE_FILE=docker-compose.yml\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG, PROXY, PHONE))
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == ["COMPOSE_FILE=docker-compose.yml"]


def test_a_line_without_the_base_gets_it_first(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + 'COMPOSE_FILE="./docker-compose.phone.yml"\n')
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert _compose_file_lines(inst) == ["COMPOSE_FILE=docker-compose.yml:./docker-compose.phone.yml"]


# ── the backup, and the two failure regimes ──────────────────────────────────

def test_the_dump_and_the_copies_are_private(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    original = (inst / ".env").read_bytes()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(EVIL_PG, PG, PROXY))
    assert r.returncode == 0, r.stderr
    (snap,) = _snapshots(inst)
    assert stat.S_IMODE(snap.stat().st_mode) == 0o700
    assert (snap / ".env").read_bytes() == original
    assert stat.S_IMODE((snap / ".env").stat().st_mode) == 0o600
    (dump,) = snap.glob("otodock-otodock-*.sql.gz")
    assert gzip.decompress(dump.read_bytes()) == b"-- dump of pg-t2\n"
    execs = [a for a in _changes(tmp_path) if a[:1] == ["exec"]]
    assert execs == [["exec", "pg-t2", "pg_dump", "-U", "otodock", "--clean", "--if-exists", "otodock"]]
    assert (inst / "docker-compose.yml").read_text() == "# the 1.7.0 base file\n"
    assert "done: this install runs 1.7.0 (was 1.6.1)" in r.stdout


def test_no_db_backup_takes_no_dump(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=(PROXY,))
    assert r.returncode == 0, r.stderr
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    (snap,) = _snapshots(inst)
    assert list(snap.glob("*.sql.gz")) == []


def test_no_running_postgres_stops_before_anything_changes(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(EVIL_PG, PROXY))
    assert r.returncode != 0
    assert "no running otodock-postgres" in r.stderr
    assert _tree(inst) == before
    assert _snapshots(inst) == []
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))


def _said(r) -> str:
    return " ".join(r.stderr.split())


@pytest.mark.parametrize("failure", [{"FAKE_PULL_RC": "1"}, {"FAKE_BARE_CONFIG_RC": "1"}])
def test_a_failure_before_up_puts_the_files_back(tmp_path, fakebin, raw, failure):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE, phone_file=False)
    before = _tree(inst)
    ino = (inst / ".env").stat().st_ino
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **failure)
    assert r.returncode != 0
    assert _tree(inst) == before
    assert not (inst / "docker-compose.phone.yml").exists()  # this run created it
    assert (inst / ".env").stat().st_ino == ino
    assert not _did(tmp_path, "up")
    assert "back as they were" in _said(r)
    assert "nothing was migrated" in _said(r)
    (snap,) = _snapshots(inst)
    assert list(snap.glob("*.sql.gz"))  # the dump is kept
    assert not list(inst.glob("*.upgrade-tmp"))


def test_a_bad_download_stops_before_anything_changes(tmp_path, fakebin, raw):
    (raw / "v1.7.0" / "docker-compose.phone.yml").unlink()
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode != 0
    assert "could not download docker-compose.phone.yml" in r.stderr
    assert _tree(inst) == before
    assert _snapshots(inst) == []


@pytest.mark.parametrize("failure, message", [
    ({"FAKE_UP_RC": "1", "FAKE_STATUS": "exited"}, "docker compose up failed"),
    ({"FAKE_HEALTH": "starting", "OTODOCK_UPGRADE_WAIT_S": "0"}, "did not report healthy within 0s"),
    # A crash loop is not waited out, however long the wait is allowed to be.
    ({"FAKE_HEALTH": "starting", "FAKE_STATUS": "restarting", "OTODOCK_UPGRADE_WAIT_S": "600"},
     "not running (state: restarting)"),
])
def test_a_failure_after_up_keeps_the_new_files_and_names_the_dump(tmp_path, fakebin, raw, failure, message):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **failure)
    assert r.returncode != 0
    assert message in _said(r)
    assert (inst / "docker-compose.yml").read_text() == "# the 1.7.0 base file\n"
    assert "OTODOCK_VERSION=1.7.0" in (inst / ".env").read_text().splitlines()
    (snap,) = _snapshots(inst)
    (dump,) = snap.glob("otodock-otodock-*.sql.gz")
    assert "may already be migrated" in _said(r)
    # The snapshot is a restore.sh run directory: the advice names it, not the dump.
    assert f"restore.sh ./backups/{snap.name}" in _said(r)
    assert dump.exists()
    assert "back as they were" not in _said(r)
    assert _compose_subcommands(tmp_path).count("up -d") == 1


def test_a_first_boot_outlasting_the_compose_wait_is_waited_out(tmp_path, fakebin, raw):
    # docker compose up gives up on otodock-proxy the moment Docker calls it
    # unhealthy; a first boot on a slow host can take longer while the proxy is
    # still starting. The script waits for it itself, then starts the services
    # that were waiting on it.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1")
    assert r.returncode == 0, r.stderr
    assert "still starting: waiting for it here" in _said(r)
    assert "done: this install runs 1.7.0 (was 1.6.1)" in r.stdout
    assert _compose_subcommands(tmp_path).count("up -d") == 2
    assert "may already be migrated" not in _said(r)


def test_optional_example_files_fail_quietly(tmp_path, fakebin, raw):
    # Neither release's config.env.example is there: the new-settings list is
    # replaced by the note, and curl's own error line stays off the screen.
    (raw / "v1.6.1" / "config.env.example").unlink()
    (raw / "v1.7.0" / "config.env.example").unlink()
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert "review config.env.example of 1.7.0" in r.stdout
    assert "curl:" not in r.stderr


# ── install.sh writes the same base-first list ───────────────────────────────

@pytest.mark.parametrize("override, want", [
    (False, "COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml"),
    (True, "COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml:docker-compose.override.yml"),
])
def test_install_lists_an_override_after_the_base(tmp_path, fakebin, override, want):
    inst = tmp_path / "inst"
    inst.mkdir()
    (tmp_path / "home").mkdir()
    if override:
        (inst / "docker-compose.override.yml").write_text("services: {}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "OTODOCK_", "COMPOSE_"))}
    env.update({"PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path / "home"),
                "FAKE_LOG": str(tmp_path / "docker.log"), "FAKE_CURL_LOG": str(tmp_path / "curl.log")})
    r = subprocess.run(["bash", str(SCRIPTS / "install.sh")], cwd=inst, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    text = (inst / ".env").read_text()
    assert [line for line in text.splitlines() if line.startswith("COMPOSE_FILE=")] == [want], r.stderr
    # the phone ports follow the public host, else loopback; no stale default
    assert "listen on\n# OTO_AUDIOSOCKET_PUBLIC_HOST above, else on 127.0.0.1" in text
    assert "publish on\n# 127.0.0.1 by default" not in text


# ── the phone publish address: a literal this host does not hold ─────────────

NOT_HELD = "is not an address of this host"


@pytest.mark.parametrize("env_tail, addrs, warned", [
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n", "127.0.0.1 192.168.1.10", False),
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n", "127.0.0.1 10.0.0.4", True),
    ('OTO_AUDIOSOCKET_PUBLIC_HOST="192.168.1.10"  # the PBX dials this\n', "127.0.0.1", True),
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\nOTO_PHONE_BIND=10.0.0.4\n", "10.0.0.4", False),
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=0.0.0.0\n", "10.0.0.4", False),
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=127.0.0.1\n", "10.0.0.4", False),
    ("", "10.0.0.4", False),
])
def test_a_phone_publish_address_this_host_does_not_hold_is_named(
        tmp_path, fakebin, raw, env_tail, addrs, warned):
    """The phone overlay publishes on OTO_PHONE_BIND, else on
    OTO_AUDIOSOCKET_PUBLIC_HOST: a literal address this host does not hold
    passes the file check and fails only when the phone container starts, so
    the upgrade names it before the swap (a warning: the run goes on)."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + env_tail)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_IP_ADDRS=addrs)
    assert r.returncode == 0, r.stderr
    assert (NOT_HELD in r.stderr) is warned, r.stderr
    if warned:
        assert "192.168.1.10" in r.stderr and "OTO_PHONE_BIND" in r.stderr


def test_the_publish_address_is_named_in_a_dry_run(tmp_path, fakebin, raw):
    """Named before anything changes: a dry run, which stops ahead of the
    swap, carries it."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n")
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run", FAKE_IP_ADDRS="10.0.0.4")
    assert r.returncode == 0, r.stderr
    assert NOT_HELD in r.stderr
    assert _tree(inst) == before


@pytest.mark.parametrize("env_tail, containers", [
    # A host name is the file check's to refuse, never this one's to judge.
    (PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=pbx.example.lan\n", (PG, PROXY)),
    (PHONE_LINE + "OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1\n", (PG, PROXY)),
    # The overlay not in use: nothing publishes on the address.
    ("COMPOSE_FILE=docker-compose.yml\nOTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n", (PG, PROXY, PHONE)),
    ("OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10\n", (PG, PROXY)),
])
def test_the_publish_address_check_judges_only_a_literal_the_overlay_uses(
        tmp_path, fakebin, raw, env_tail, containers):
    inst = _install(tmp_path, SECRET_ENV + env_tail)
    ip_log = tmp_path / "ip.log"
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=containers,
             FAKE_IP_ADDRS="10.0.0.4", FAKE_IP_LOG=str(ip_log))
    assert r.returncode == 0, r.stderr
    assert NOT_HELD not in r.stderr
    assert not ip_log.exists()


# ── the recover bin moves out of the old server container ───────────────────

BIN_TARGET = {"FAKE_VOLUMES": "otodock-agents otodock-recover-bin otodock-sessions"}


def _runs(tmp_path) -> list[dict]:
    log = tmp_path / "run.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _order(tmp_path) -> list[str]:
    """pull, carry, up as they ran: `docker cp` and `compose run` are the two
    ends of one pipeline (they start together), so either is "carry"."""
    steps: list[str] = []
    for a in _calls(tmp_path):
        step = "carry" if a[:1] == ["cp"] else (
            next((s for s in ("pull", "run", "up") if s in a), "") if a[:1] == ["compose"] else "")
        step = "carry" if step == "run" else step
        if step and (not steps or steps[-1] != step):
            steps.append(step)
    return steps


def test_the_bin_is_carried_between_the_pull_and_up_past_a_long_diff(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    # The bin line comes first, then more diff than a pipe holds: a
    # `docker diff | grep -q` would die of SIGPIPE under pipefail here.
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **BIN_TARGET,
             FAKE_DIFF_BIN="1", FAKE_DIFF_FILLER="4000")
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["pull", "carry", "up"]
    assert ["cp", "proxy-t2:/opt/otodock/recover-bin", "-"] in _calls(tmp_path)
    (run,) = _runs(tmp_path)
    assert run["read"] > 0                                   # the archive reached the one-shot
    assert run["args"][:6] == ["compose", "run", "--rm", "--no-deps", "-T", "otodock-agents-init"]
    script = run["args"][-1]
    assert script.startswith("mountpoint -q /opt/otodock/recover-bin || exit 3;")
    assert "tar -x -C /opt/otodock/recover-bin --strip-components=1" in script
    assert "chown -R 1000:1000 /opt/otodock/recover-bin" in script
    assert "copying the recover bin" in r.stdout


@pytest.mark.parametrize("extra", [
    {**BIN_TARGET, "FAKE_DIFF_BIN": "1", "FAKE_MOUNTS": "/opt/otodock/agents /opt/otodock/recover-bin"},
    {"FAKE_VOLUMES": "otodock-agents otodock-sessions", "FAKE_DIFF_BIN": "1"},
    {**BIN_TARGET},
], ids=["the-server-mounts-the-volume", "the-target-has-no-volume", "no-bin-in-the-container"])
def test_nothing_to_carry(tmp_path, fakebin, raw, extra):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **extra)
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["pull", "up"]


def test_no_server_container_means_nothing_to_carry(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG,), **BIN_TARGET, FAKE_DIFF_BIN="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, PROXY]))
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["pull", "up"]


def test_a_failed_copy_warns_and_the_upgrade_goes_on(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **BIN_TARGET, FAKE_DIFF_BIN="1", FAKE_RUN_RC="3")
    assert r.returncode == 0, r.stderr
    assert "could not copy the recover bin" in r.stderr
    assert _order(tmp_path) == ["pull", "carry", "up"]
    assert "done: this install runs 1.7.0" in r.stdout


def test_not_enough_disk_skips_the_copy(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **BIN_TARGET, FAKE_DIFF_BIN="1",
             FAKE_DOCKER_ROOT=str(tmp_path), FAKE_DU_K=str(10 ** 12))
    assert r.returncode == 0, r.stderr
    assert "not enough free disk to copy the recover bin" in r.stderr
    assert _order(tmp_path) == ["pull", "up"]
    assert any(a[:1] == ["exec"] and "du" in a for a in _calls(tmp_path))


def test_a_stopped_server_is_sized_by_its_writable_layer(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", **BIN_TARGET, FAKE_DIFF_BIN="1",
             FAKE_STATUS="exited", FAKE_DOCKER_ROOT=str(tmp_path), FAKE_SIZE_RW=str(10 ** 15))
    assert "not enough free disk to copy the recover bin" in r.stderr
    assert not any(a[:1] == ["exec"] and "du" in a for a in _calls(tmp_path))


def test_the_dry_run_names_the_carry_and_copies_nothing(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run", **BIN_TARGET, FAKE_DIFF_BIN="1")
    assert r.returncode == 0, r.stderr
    assert "copy the recover bin out of proxy-t2 into the otodock-recover-bin volume" in r.stdout
    assert _tree(inst) == before
    assert not any(a[:1] in (["exec"], ["cp"]) for a in _changes(tmp_path))
    assert _runs(tmp_path) == []


# ── the version check, the upgrade notes and the yes/no values, before anything changes ──

CHANGELOG_1_7_0 = """\
# Changelog

## [Unreleased]

### Added
- Not yet.

## [1.7.0] — 2026-09-29

### Changed
- **Upgrade note:** the foo changed over
  two lines, and admins set FOO.
- Something that is not a note.

## [1.6.1] — 2026-09-15

### Upgrade notes
- An older note, below the starting release.
"""


@pytest.fixture
def raw_with_notes(raw):
    (raw / "v1.7.0" / "CHANGELOG.md").write_text(CHANGELOG_1_7_0)
    return raw


def test_the_upgrade_notes_are_printed_before_anything_changes(tmp_path, fakebin, raw_with_notes):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "upgrade notes from 1.6.1 to 1.7.0" in r.stdout
    assert "the foo changed over two lines, and admins set FOO." in r.stdout
    assert "An older note" not in r.stdout and "not a note" not in r.stdout
    assert r.stdout.index("the foo changed") < r.stdout.index("dry run: nothing is changed")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert r.stdout.index("the foo changed") < r.stdout.index("backing up the database")


def test_without_a_changelog_the_notes_point_at_the_release_page(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "no CHANGELOG.md for 1.7.0 could be read" in r.stdout
    assert "releases/tag/v1.7.0" in r.stdout
    assert "curl:" not in r.stderr


def test_an_unknown_starting_release_prints_the_target_notes_alone(tmp_path, fakebin, raw_with_notes):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    (inst / "docker-compose.yml").write_text("services:\n  otodock-proxy:\n    image: ghcr.io/otodock/otodock-proxy:latest\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "upgrade notes from the current release to 1.7.0" in r.stdout
    assert "the foo changed" in r.stdout and "An older note" not in r.stdout


def test_the_database_version_refuses_a_downgrade_before_anything_changes(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_DB_VERSION="1.7.1")
    assert r.returncode != 0
    assert "last migrated by OtoDock 1.7.1" in r.stderr and "downgrade" in r.stderr
    assert _tree(inst) == before and _snapshots(inst) == []
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))


def test_the_database_version_raises_the_starting_release(tmp_path, fakebin, raw_with_notes):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run", FAKE_DB_VERSION="1.6.2")
    assert r.returncode == 0, r.stderr
    assert "last migrated by 1.6.2" in r.stdout and "upgrading from there" in r.stdout
    assert "upgrade notes from 1.6.2 to 1.7.0" in r.stdout


BAD_YESNO = "OIDC_ENABLED=yse\nOTODOCK_AIR_GAPPED=1\nCOOKIE_SECURE=auto\nDASHBOARD_ENABLED=auto\n"


def test_a_value_that_is_not_yes_or_no_warns_in_the_dry_run_and_stops_the_real_run(tmp_path, fakebin, raw_1_8):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + BAD_YESNO)
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "not yes/no words" in r.stderr and "OIDC_ENABLED=yse" in r.stderr and "DASHBOARD_ENABLED=auto" in r.stderr
    assert "OTODOCK_AIR_GAPPED=1" not in r.stderr and "COOKIE_SECURE" not in r.stderr
    assert "the real run stops here" in r.stderr
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0")
    assert r.returncode != 0
    assert "not yes/no words" in r.stderr and "refuse to start" in r.stderr and "run the upgrade again" in r.stderr
    assert _tree(inst) == before and _snapshots(inst) == []
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    assert not _did(tmp_path, "pull") and not _did(tmp_path, "up")


def test_the_yes_no_rule_is_judged_only_for_a_target_of_1_8_0_or_later(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + BAD_YESNO)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "yes/no" not in r.stderr and "count as ON" not in r.stdout


def test_values_that_count_as_on_from_1_8_0_are_named_in_both_runs(tmp_path, fakebin, raw_1_8):
    """The keys releases before 1.8.0 read as on for the word "true" alone:
    1, yes and on flip from off to on at this upgrade, and the operator is
    told before the server reads them."""
    flips = ("OIDC_ENABLED=1\nOTODOCK_AIR_GAPPED=yes\nDASHBOARD_ENABLED=On\nMCP_AUTO_INSTALL_SYSTEM_DEPS=on\n"
             "OTODOCK_CLOUD=true\nOTODOCK_RELEASE_CHECK=1\n")
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + flips)
    for extra in (["--dry-run"], []):
        r = _run(inst, tmp_path, fakebin, "--to", "1.8.0", *extra)
        assert r.returncode == 0, r.stderr
        assert "count as ON" in r.stdout
        for named in ("OIDC_ENABLED=1", "OTODOCK_AIR_GAPPED=yes", "DASHBOARD_ENABLED=on", "MCP_AUTO_INSTALL_SYSTEM_DEPS=on"):
            assert named in r.stdout, named
        assert "OTODOCK_CLOUD" not in r.stdout.split("count as ON")[1].split("\n\n")[0]
        assert "OTODOCK_RELEASE_CHECK" not in r.stdout.split("count as ON")[1].split("Set each")[0]
        if extra:
            assert r.stdout.index("count as ON") < r.stdout.index("dry run: nothing is changed")
        else:
            assert r.stdout.index("count as ON") < r.stdout.index("backing up the database")
    # An install already on 1.8.0 crosses nothing: no list.
    (tmp_path / "two").mkdir()
    inst2 = _install(tmp_path / "two", SECRET_ENV + PHONE_LINE + flips + "OTODOCK_VERSION=1.8.0\n")
    r = _run(inst2, tmp_path, fakebin, "--to", "1.8.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "count as ON" not in r.stdout


def test_the_snapshot_is_a_restore_run_directory(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    keys = '{"' + "a" * 32 + '": "JWT_SECRET"}'
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_KEYS=keys)
    assert r.returncode == 0, r.stderr
    (snap,) = _snapshots(inst)
    manifest = dict(line.split("=", 1) for line in (snap / "manifest").read_text().splitlines())
    (dump,) = snap.glob("*.sql.gz")
    assert manifest["db"] == dump.name
    assert manifest["otodock_version"] == "1.6.1" and manifest["complete"] == "1"
    assert manifest["agents"] == "skipped" and manifest["credential_keys"] == keys
    assert "credential_keys_floor" not in manifest
    assert stat.S_IMODE((snap / "manifest").stat().st_mode) & 0o077 == 0
    # A 1.7.1 database has no fingerprint row: the secret floor's record stands in.
    (tmp_path / "two").mkdir()
    inst2 = _install(tmp_path / "two", SECRET_ENV + PHONE_LINE)
    floor = '{"JWT_SECRET": ["' + "b" * 32 + '"]}'
    r = _run(inst2, tmp_path, fakebin, "--to", "1.7.0", FAKE_KEYS="", FAKE_FLOOR=floor)
    assert r.returncode == 0, r.stderr
    (snap,) = _snapshots(inst2)
    manifest = dict(line.split("=", 1) for line in (snap / "manifest").read_text().splitlines())
    assert manifest["credential_keys_floor"] == floor and "credential_keys" not in manifest


def test_a_release_candidate_below_the_database_release_is_a_downgrade(tmp_path, fakebin, raw, candidate):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=1.7.0\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0-rc2", "--compose-dir", str(candidate), FAKE_DB_VERSION="1.8.0")
    assert r.returncode != 0
    assert "last migrated by OtoDock 1.8.0" in r.stderr and "downgrade" in r.stderr
