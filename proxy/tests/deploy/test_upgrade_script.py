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
# like pg_dump; `inspect` reports FAKE_HEALTH for a health format and
# FAKE_STATUS (running) for a status one; `image inspect` knows
# FAKE_LOCAL_IMAGES; `compose config` runs the real docker when REAL_DOCKER is
# set, else exits FAKE_CONFIG_RC (FAKE_BARE_CONFIG_RC for a call without -f,
# the check of the files in place); `compose pull` and `compose up` exit
# FAKE_PULL_RC and FAKE_UP_RC (FAKE_UP_RC for the first up only).
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
cs = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
if args[:1] == ["info"]:
    sys.exit(0)
if args[:1] == ["ps"]:
    want_all = "-a" in args
    labels = [args[i + 1].partition("=")[2] for i, a in enumerate(args) if a == "--filter"]
    for c in cs:
        if not want_all and not c.get("running", True):
            continue
        if all(c["labels"].get(l.partition("=")[0]) == l.partition("=")[2] for l in labels):
            print(c["id"])
    sys.exit(0)
if args[:1] == ["exec"]:
    print("-- dump of " + args[1])
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    if "Health" in fmt:
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
        if "-f" not in args:
            sys.exit(int(os.environ.get("FAKE_BARE_CONFIG_RC", "0")))
        sys.exit(int(os.environ.get("FAKE_CONFIG_RC", "0")))
    if sub == "pull":
        sys.exit(int(os.environ.get("FAKE_PULL_RC", "0")))
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
    assert not any(a[:1] == ["exec"] for a in _calls(tmp_path))
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
    assert not any(a[:1] == ["exec"] for a in _calls(tmp_path))
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
    execs = [a for a in _calls(tmp_path) if a[:1] == ["exec"]]
    assert execs == [["exec", "pg-t2", "pg_dump", "-U", "otodock", "--clean", "--if-exists", "otodock"]]
    assert (inst / "docker-compose.yml").read_text() == "# the 1.7.0 base file\n"
    assert "done: this install runs 1.7.0 (was 1.6.1)" in r.stdout


def test_no_db_backup_takes_no_dump(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=(PROXY,))
    assert r.returncode == 0, r.stderr
    assert not any(a[:1] == ["exec"] for a in _calls(tmp_path))
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
    assert not any(a[:1] == ["exec"] for a in _calls(tmp_path))


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
    assert "restore.sh" in r.stderr
    assert dump.name in r.stderr
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
