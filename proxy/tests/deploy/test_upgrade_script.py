"""scripts/upgrade.sh, run against a fake ``docker``, ``curl``, ``ip`` and
``sleep`` on PATH, with a refusing fake of every command that acts on the host.

The upgrade checks the target release's compose files against the install's
own .env before it changes anything, writes no line of .env but
OTODOCK_VERSION and (when needed) COMPOSE_FILE and COLLABORA_SSL_TERMINATION,
stops Collabora before up, builds COMPOSE_FILE base first
without duplicating a file on a re-run, and has three failure regimes: before
``docker compose up`` it puts the files back; an ``up`` that fails before
anything of the new release ran puts the files back and starts the old
release again; once something ran it leaves the new files and points at the
dump. The ``config`` checks run through the real Compose
loader where one is installed (it needs no daemon), so the refusal of a
hostname in OTO_AUDIOSOCKET_PUBLIC_HOST is the loader's own. install.sh lists
an override after the base file on the COMPOSE_FILE line it writes. A server
of 2.0.0 or later waits for its open work between the pull and the swap; an
older one is asked nothing and the script says what that means.
"""
from __future__ import annotations

import gzip
import json
import os
import pty
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.deploy.fake_docker import host_calls, host_commands_named, write_host_fakes

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
REAL_DOCKER = shutil.which("docker")

# A stand-in for the docker CLI. Every call is logged as a JSON argv line.
# `ps` applies the label filters (and -a) to FAKE_CONTAINERS; `exec` answers
# pg_dump, `psql -tAc` with FAKE_DB_VERSION, FAKE_FLOOR or FAKE_KEYS, and the
# admin command line in the server container: `drain` prints two of the
# verb's lines (FAKE_DRAIN_RC=1: the refusal instead, exit 1; FAKE_DRAIN_HOLD
# names a folder where it writes `started` and waits for `release`), `resume`
# prints its line, `compute` prints FAKE_COMPUTE_LINE and exits FAKE_COMPUTE_RC.
# Any other `exec` fails (exit 97). `run` of the folders' compare (the image's
# Python as the entrypoint, the script by its path in the image or as `-c`
# text) prints FAKE_SPLIT; with none, a `-c` text is run for real on what it
# was fed, and an image named in FAKE_NO_COMPARE has no script of its own
# (exit 2). `compose config --format json` prints FAKE_CONFIG_JSON. `inspect`
# of a container's image answers its `image` (sha256:running by default), of
# FAKE_HEALTH for a health format and
# FAKE_STATUS (running) for a status one; `image inspect` knows
# FAKE_LOCAL_IMAGES; `compose config` runs the real docker when REAL_DOCKER is
# set, else exits FAKE_CONFIG_RC (FAKE_BARE_CONFIG_RC for a call without -f,
# the check of the files in place, which also fails after an up when
# FAKE_REFUSED_AFTER_UP is set: the old files put back); a plain `compose config`, the read of the
# project's name, prints `name: FAKE_PROJECT` (otodock), or fails with
# Compose's own line when FAKE_PARSE_RC says the folder's files do not parse;
# `compose pull` and `compose up` exit
# FAKE_PULL_RC and FAKE_UP_RC (FAKE_UP_RC for the first up only).
# The recover-bin carry: `compose config --volumes` prints FAKE_VOLUMES;
# `inspect` of the mounts prints FAKE_MOUNTS, of the writable layer
# FAKE_SIZE_RW; `diff` prints the bin line when FAKE_DIFF_BIN is set, then
# FAKE_DIFF_FILLER more lines; `version` prints FAKE_API_VERSION (the Docker
# Engine API, 1.51 by default) and `compose version --short`
# FAKE_COMPOSE_VERSION (2.35.1); `exec ... du` prints FAKE_DU_K; `info -f`
# prints FAKE_DOCKER_ROOT; `cp` writes a stand-in archive (FAKE_CP_RC);
# `compose run` logs the bytes it read to FAKE_RUN_LOG (FAKE_RUN_RC). After
# the first `compose up`, `ps` lists FAKE_CONTAINERS_AFTER_UP when it is set
# (the server that up started), after the second FAKE_CONTAINERS_AFTER_UP2
# when that is set; a later up exits FAKE_UP_AGAIN_RC (0). `ps` leaves out a
# container a logged `rm` removed and fills a --format with a container's id
# and name; `inspect` of a status and a start time answers per container
# (`status`, `started`: a never-started one carries Docker's zero time;
# `unreadable` fails it).
# FAKE_ENV_LOG gets the COMPOSE_PROJECT_NAME each `compose` pull, stop, up
# and run saw.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
cs = json.loads(os.environ.get("FAKE_CONTAINERS", "[]"))
if args[:1] == ["version"]:
    print(os.environ.get("FAKE_API_VERSION", "1.51"))
    sys.exit(0)
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
with open(os.environ["FAKE_LOG"]) as fh:
    logged = [json.loads(line) for line in fh]
ups = sum(1 for a in logged if a[:2] == ["compose", "up"])
removed = {a[-1] for a in logged if a[:1] == ["rm"]}
if ups >= 2 and os.environ.get("FAKE_CONTAINERS_AFTER_UP2"):
    cs = json.loads(os.environ["FAKE_CONTAINERS_AFTER_UP2"])
elif ups >= 1 and os.environ.get("FAKE_CONTAINERS_AFTER_UP"):
    cs = json.loads(os.environ["FAKE_CONTAINERS_AFTER_UP"])
cs = [c for c in cs if c["id"] not in removed]
if args[:1] == ["ps"]:
    want_all = "-a" in args
    labels = [args[i + 1].partition("=")[2] for i, a in enumerate(args) if a == "--filter"]
    fmt = args[args.index("--format") + 1] if "--format" in args else "{{.ID}}"
    for c in cs:
        if not want_all and not c.get("running", True):
            continue
        if all(c["labels"].get(l.partition("=")[0]) == l.partition("=")[2] for l in labels):
            line = fmt.replace("{{.ID}}", c["id"]).replace("{{.Names}}", c.get("name", c["id"]))
            print(re.sub(r'\{\{\.Label "([^"]+)"\}\}', lambda m: c["labels"].get(m.group(1), ""), line))
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
    if "pg_dump" in args:
        print("-- dump of " + args[1])
        sys.exit(0)
    if args[2:4] == ["/opt/otodock/proxy/venv/bin/python", "/opt/otodock/proxy/admin_cli.py"]:
        if args[4] == "compute":
            if os.environ.get("FAKE_COMPUTE_SIGNAL"):
                import signal
                os.kill(os.getppid(), getattr(signal, "SIG" + os.environ["FAKE_COMPUTE_SIGNAL"]))
            print(os.environ.get("FAKE_COMPUTE_LINE", "The compute machine is signed in."))
            sys.exit(int(os.environ.get("FAKE_COMPUTE_RC", "0")))
        if args[4] == "resume":
            print("The platform takes new work again.")
            sys.exit(0)
        if args[4] == "drain" and os.environ.get("FAKE_DRAIN_RC", "0") != "0":
            sys.stderr.write("admin_cli: The server did not answer the drain. It is not running, "
                             "or it is older than 2.0.\n")
            sys.exit(int(os.environ["FAKE_DRAIN_RC"]))
        if args[4] == "drain":
            print("Waiting for open work: 2 turns. Up to 10 minutes.", flush=True)
            hold = os.environ.get("FAKE_DRAIN_HOLD")
            if hold:
                import time
                open(os.path.join(hold, "started"), "w").close()
                for _ in range(600):
                    if os.path.exists(os.path.join(hold, "release")):
                        break
                    time.sleep(0.05)
            print("Nothing is working. New work is held until the platform is back.")
            sys.exit(0)
    sys.stderr.write("fake docker: an exec it does not know: " + " ".join(args) + "\n")
    sys.exit(97)
if args[:1] == ["logs"]:
    print("old server: its last line before the swap")
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    if "StartedAt" in fmt:
        c = next((c for c in cs if c["id"] == args[-1]), None)
        if c is None or c.get("unreadable"):
            sys.exit(1)
        status = c.get("status") or ("running" if c.get("running", True) else "exited")
        started = c.get("started", "2026-10-01T09:00:00.123456789Z")
        print(fmt.replace("{{.State.Status}}", status).replace("{{.State.StartedAt}}", started))
        sys.exit(0)
    if "{{.Image}}" in fmt:
        c = next((c for c in cs if c["id"] == args[-1]), {})
        print(c.get("image", "sha256:running"))
    elif "Mounts" in fmt:
        print("\n".join(os.environ.get("FAKE_MOUNTS", "/opt/otodock/agents").split()))
    elif "SizeRw" in fmt:
        print(os.environ.get("FAKE_SIZE_RW", "4096"))
    elif "Health" in fmt:
        print(os.environ.get("FAKE_HEALTH", "healthy"))
    else:
        print(os.environ.get("FAKE_STATUS", "running"))
    sys.exit(0)
PY = "/opt/otodock/proxy/venv/bin/python"
if args[:1] == ["run"] and PY in args:
    fed = sys.stdin.read()
    image = args[args.index(PY) + 1]
    if "-c" not in args and image in os.environ.get("FAKE_NO_COMPARE", "").split():
        sys.stderr.write("python: can't open file '/opt/otodock/scripts/compose_mounts.py'\n")
        sys.exit(2)
    if os.environ.get("FAKE_SPLIT"):
        print(os.environ["FAKE_SPLIT"])
    elif "-c" in args:
        import subprocess
        done = subprocess.run([sys.executable, "-c", args[args.index("-c") + 1]], input=fed, text=True)
        sys.exit(done.returncode)
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
    if sub in ("pull", "stop", "up", "run") and os.environ.get("FAKE_ENV_LOG"):
        with open(os.environ["FAKE_ENV_LOG"], "a") as fh:
            fh.write(os.environ.get("COMPOSE_PROJECT_NAME", "-") + "\n")
    if sub == "version":
        if "--short" in args:
            print(os.environ.get("FAKE_COMPOSE_VERSION", "2.35.1"))
        sys.exit(0)
    if sub == "config":
        if os.environ.get("REAL_DOCKER"):
            os.execv(os.environ["REAL_DOCKER"], [os.environ["REAL_DOCKER"], *args])
        if args == ["compose", "config"]:
            if os.environ.get("FAKE_PARSE_RC", "0") != "0":
                sys.stderr.write('service "otodock-compute" has neither an image nor a build context '
                                 'specified: invalid compose project\n')
                sys.exit(int(os.environ["FAKE_PARSE_RC"]))
            print("name: " + os.environ.get("FAKE_PROJECT", "otodock"))
            sys.exit(0)
        if "--images" in args:
            print("\n".join(os.environ.get("FAKE_IMAGES", "").split()))
        if "--volumes" in args:
            print("\n".join(os.environ.get("FAKE_VOLUMES", "").split()))
        if "--format" in args and os.environ.get("FAKE_CONFIG_JSON"):
            print(os.environ["FAKE_CONFIG_JSON"])
        if "-f" not in args:
            if ups and os.environ.get("FAKE_REFUSED_AFTER_UP"):
                sys.exit(1)
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
        rc = os.environ.get("FAKE_UP_RC", "0") if ups == 1 else os.environ.get("FAKE_UP_AGAIN_RC", "0")
        sys.exit(int(rc))
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

# A stand-in for sleep: the seconds asked for are logged, none are slept.
FAKE_SLEEP = '#!/bin/sh\necho "$*" >> "${FAKE_SLEEP_LOG:-/dev/null}"\n'

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
COLLABORA = {"id": "collabora-t2", "labels": {"com.docker.compose.project": "otodock",
                                             "com.docker.compose.service": "collabora"}}
# What a docker compose up leaves: a server it created (a new id) that
# started, or one it created that never ran (Docker's zero start time).
NEVER = "0001-01-01T00:00:00Z"
NEW_PROXY = dict(PROXY, id="proxy-new", name="otodock-otodock-proxy-1")
NEVER_PROXY = dict(NEW_PROXY, running=False, status="created", started=NEVER)
# A community MCP's container: another compose project, a name free to say anything.
EVIL_PG = {"id": "evil-pg", "labels": {"com.docker.compose.project": "otodock-x-mcp-otodock-postgres",
                                       "com.docker.compose.service": "otodock-postgres"}}

# The admin command line as the script runs it in the server container.
ADMIN_CLI = ["/opt/otodock/proxy/venv/bin/python", "/opt/otodock/proxy/admin_cli.py"]

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
    _write_exec(b / "sleep", FAKE_SLEEP)
    write_host_fakes(b)
    yield b
    assert host_calls(b) == [], "a script reached a command that acts on the host"


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


def _env(tmp_path, fakebin, containers=(PG, PROXY), real_config=False, **extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "OTODOCK_", "COMPOSE_", "OTO_"))}
    env.update({
        "PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}",
        "FAKE_CONTAINERS": json.dumps(list(containers)),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
        "FAKE_RAW_ROOT": str(tmp_path / "raw"),
        "FAKE_RUN_LOG": str(tmp_path / "run.log"),
        "FAKE_SLEEP_LOG": str(tmp_path / "sleep.log"),
    })
    if real_config:
        env["REAL_DOCKER"] = REAL_DOCKER
    env.update(extra)
    return env


def _run(inst, tmp_path, fakebin, *args, containers=(PG, PROXY), real_config=False, **extra):
    return subprocess.run(
        ["bash", str(SCRIPTS / "upgrade.sh"), *args], cwd=inst,
        env=_env(tmp_path, fakebin, containers, real_config, **extra),
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
    # Another project's container named for the database is never taken for it.
    assert ("no container of the project otodock is OtoDock's database. These projects have an otodock-postgres "
            "service: otodock-x-mcp-otodock-postgres.") in _said(r)
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
    # Old files beside an override that already names otodock-compute: compose refuses
    # the folder, and the run that put them back is the one that says so.
    refused = ("docker compose refuses this folder's files now. If your override names otodock-compute, run the "
               "update again, or take that part out to use docker compose until you do: 1.6.1 has no such service.")
    assert (refused in _said(r)) == ("FAKE_BARE_CONFIG_RC" in failure)
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
    ({"FAKE_UP_RC": "1", "FAKE_STATUS": "exited", "FAKE_CONTAINERS_AFTER_UP": json.dumps([PG, NEW_PROXY])},
     "docker compose up failed"),
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
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, NEW_PROXY]))
    assert r.returncode == 0, r.stderr
    assert "still starting: waiting for it here" in _said(r)
    assert "done: this install runs 1.7.0 (was 1.6.1)" in r.stdout
    assert _compose_subcommands(tmp_path).count("up -d") == 2
    assert "may already be migrated" not in _said(r)


# ── an up that fails before anything of the new release ran is undone ─────

def test_an_up_that_fails_before_anything_ran_puts_the_old_release_back(tmp_path, fakebin, raw):
    # The shape Compose 2.36 left on a first 1.9.0 start: the old server
    # stopped and renamed <12 hex>_<name> (its start time unchanged), the new
    # one created under the name and never started. Nothing ran the new
    # release, so the old files go back and the old release starts again.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    ino = (inst / ".env").stat().st_ino
    renamed = dict(PROXY, name="0123456789ab_otodock-otodock-proxy-1", running=False, status="exited")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, renamed, NEVER_PROXY]),
             FAKE_CONTAINERS_AFTER_UP2=json.dumps([PG, PROXY]))
    assert r.returncode == 1, r.stderr
    assert _tree(inst) == before
    assert (inst / ".env").stat().st_ino == ino
    said = _said(r)
    assert "the upgrade to 1.7.0 failed before the new server started" in said
    assert "this install is back on 1.6.1" in said and "Nothing was migrated" in said
    assert "bash upgrade.sh --to 1.7.0" in said
    assert "may already be migrated" not in said and "did not finish" not in said
    calls = _calls(tmp_path)
    assert ["rm", "proxy-new"] in calls
    assert ["rename", "proxy-t2", "otodock-otodock-proxy-1"] in calls
    assert not any(a[:1] == ["rm"] and a[-1] == "proxy-t2" for a in calls)
    assert "removing otodock-otodock-proxy-1, which the failed start created and never ran" in r.stdout
    assert "renaming 0123456789ab_otodock-otodock-proxy-1 back to otodock-otodock-proxy-1" in r.stdout
    assert _compose_subcommands(tmp_path).count("up -d") == 2
    (snap,) = _snapshots(inst)
    assert list(snap.glob("otodock-otodock-*.sql.gz"))  # the dump stays


def test_an_up_that_fails_with_the_old_server_untouched_puts_the_old_release_back(tmp_path, fakebin, raw):
    # The shape Compose 2.26 leaves: the old server still runs, the new
    # container waits under a temporary name, never started.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    waiting = dict(NEVER_PROXY, name="0123456789ab_otodock-otodock-proxy-1")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, PROXY, waiting]))
    assert r.returncode == 1, r.stderr
    assert "this install is back on 1.6.1" in _said(r)
    calls = _calls(tmp_path)
    assert ["rm", "proxy-new"] in calls
    assert not any(a[:1] == ["rename"] for a in calls)
    assert (inst / "docker-compose.yml").read_text().startswith("services:\n  otodock-proxy:")


def test_a_rollback_that_cannot_start_the_old_release_says_what_to_do(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1", FAKE_UP_AGAIN_RC="1",
             FAKE_STATUS="exited", FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, NEVER_PROXY]))
    assert r.returncode == 1, r.stderr
    said = _said(r)
    assert "putting 1.6.1 back did not finish" in said and "the database is as it was" in said
    assert "docker ps -a --filter label=com.docker.compose.project=otodock" in said
    assert "back on 1.6.1, with the files" not in said
    assert _tree(inst) == before  # the files went back before the second up
    assert "holds new work" not in said  # this server was never asked to hold any
    # The old files went back beside an override that names otodock-compute, which the
    # old release does not have: compose refuses the folder, and the person is told why.
    refused = ("docker compose refuses this folder's files now. If your override names otodock-compute, take that "
               "part out first: 1.6.1 has no such service.")
    assert refused not in said
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1", FAKE_UP_AGAIN_RC="1", FAKE_REFUSED_AFTER_UP="1",
             FAKE_STATUS="exited", FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, NEVER_PROXY]))
    assert r.returncode == 1 and "putting 1.6.1 back did not finish" in _said(r)
    assert refused in _said(r)


def test_a_rollback_to_files_compose_refuses_starts_nothing(tmp_path, fakebin, raw):
    """The old files go back beside an override that names otodock-compute:
    compose refuses them, so an up cannot start the old release. The
    rollback tried it anyway, met the old server still running and said it
    was "still starting", waited for it and ran a third up. Now it runs no
    up after the files are back and says why the folder is refused."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1", FAKE_UP_AGAIN_RC="1", FAKE_REFUSED_AFTER_UP="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, PROXY]))
    said = _said(r)
    assert r.returncode == 1 and "putting 1.6.1 back did not finish" in said
    assert [a for a in _calls(tmp_path) if a[:2] == ["compose", "up"]] == [_calls_up_once(tmp_path)]
    assert "still starting" not in said and "starting 1.6.1 again" not in said
    assert ("docker compose refuses this folder's files now. If your override names otodock-compute, take that "
            "part out first: 1.6.1 has no such service.") in said


def _calls_up_once(tmp_path) -> list[str]:
    (first,) = [a for a in _calls(tmp_path) if a[:2] == ["compose", "up"]][:1]
    return first


def _never_undone(r, inst) -> None:
    said = _said(r)
    assert r.returncode != 0
    assert "may already be migrated" in said and "putting 1.6.1 back" not in said
    assert (inst / "docker-compose.yml").read_text() == "# the 1.7.0 base file\n"


def test_a_failed_up_after_the_database_init_ran_is_not_undone(tmp_path, fakebin, raw):
    # The target's otodock-db-init converges the database's roles before the
    # server starts: once it ran, the old release may not run on that
    # database, so nothing is undone even though the server never started.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    db_init = {"id": "dbinit-new", "running": False, "status": "exited",
               "labels": {"com.docker.compose.project": "otodock", "com.docker.compose.service": "otodock-db-init"}}
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1", FAKE_STATUS="exited",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, db_init, NEVER_PROXY]))
    _never_undone(r, inst)


def test_a_server_an_earlier_run_created_and_this_one_started_is_not_undone(tmp_path, fakebin, raw):
    # An interrupted earlier run left the new server created and never
    # started; this up started that same container (its start time moved).
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    waiting = dict(PROXY, running=False, status="created", started=NEVER)
    ran = dict(PROXY, running=False, status="exited")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG, waiting), FAKE_UP_RC="1",
             FAKE_STATUS="exited", FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, ran]))
    _never_undone(r, inst)


def test_a_release_raised_from_the_database_is_not_undone(tmp_path, fakebin, raw):
    # .env names an older release than the one that last migrated the
    # database: the copied files would run that older release on it.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_DB_VERSION="1.6.5", FAKE_UP_RC="1",
             FAKE_STATUS="exited", FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, NEVER_PROXY]))
    assert "last migrated by 1.6.5" in r.stdout
    assert "putting 1.6.5 back" not in _said(r)
    assert "may already be migrated" in _said(r)


def test_an_unknown_server_state_is_never_rolled_back(tmp_path, fakebin, raw):
    # A container that cannot be read counts as one that ran: the files
    # stay, and the advice is the restore.
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    ghost = dict(NEVER_PROXY, id="proxy-ghost", unreadable=True)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", FAKE_UP_RC="1", FAKE_STATUS="exited",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, ghost]))
    _never_undone(r, inst)


def test_a_named_project_is_the_one_every_lookup_and_compose_call_takes(tmp_path, fakebin, raw):
    def mine(c):
        return dict(c, labels={**c["labels"], "com.docker.compose.project": "otodock-b"})
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    # Another install on this daemon (the project otodock) is never read.
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG, PROXY),
             OTODOCK_COMPOSE_PROJECT="otodock-b")
    assert r.returncode != 0
    assert "no container of the project otodock-b is OtoDock's database" in _said(r)
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(mine(PG), mine(PROXY)),
             OTODOCK_COMPOSE_PROJECT="otodock-b", FAKE_ENV_LOG=str(tmp_path / "env.log"))
    assert r.returncode == 0, r.stderr
    filters = [a[i + 1] for a in _calls(tmp_path) if a[:1] == ["ps"]
               for i, x in enumerate(a) if x == "--filter" and a[i + 1].startswith("label=com.docker.compose.project=")]
    assert filters and set(filters) == {"label=com.docker.compose.project=otodock-b"}
    assert set((tmp_path / "env.log").read_text().split()) == {"otodock-b"}
    # Unset, the project is the one compose resolves (otodock here), and
    # every compose call is told so.
    (tmp_path / "env.log").unlink()
    inst2 = tmp_path / "second"
    shutil.copytree(inst, inst2, ignore=shutil.ignore_patterns("backups"))
    r = _run(inst2, tmp_path, fakebin, "--to", "1.7.0", FAKE_ENV_LOG=str(tmp_path / "env.log"))
    assert r.returncode == 0, r.stderr
    assert set((tmp_path / "env.log").read_text().split()) == {"otodock"}
    (snap,) = _snapshots(inst)
    assert "project=otodock-b" in (snap / "manifest").read_text().splitlines()


# ── the project when docker compose refuses the folder's files ───────────────

# The fix the update's own refusal asks for: an override that names
# otodock-compute, under an installed release whose compose file has no such
# service. Compose then refuses the folder until the update ran.
NO_PARSE = {"FAKE_PARSE_RC": "1"}
REFUSED_FOLDER = ("docker compose does not accept this folder's files, and the containers started from this folder "
                  "name no single project.")
RUN_AGAIN_NAMED = "Run again with OTODOCK_COMPOSE_PROJECT=<the project of this install>."


def _of(c: dict, project: str, folder, **more) -> dict:
    """``c`` as a container docker compose started from ``folder`` in ``project``."""
    return dict(c, id=f"{c['id']}-{project}", **more,
                labels={**c["labels"], "com.docker.compose.project": project,
                        "com.docker.compose.project.working_dir": str(folder)})


def _projects_used(tmp_path) -> set[str]:
    """The project every label lookup named, and the one every compose pull, stop and up ran under."""
    named = {a[i + 1].rpartition("=")[2] for a in _calls(tmp_path) if a[:1] == ["ps"] for i, x in enumerate(a)
             if x == "--filter" and a[i + 1].startswith("label=com.docker.compose.project=")}
    return named | set((tmp_path / "env.log").read_text().split())


@pytest.mark.parametrize("project", ["otodock", "acme"])
def test_a_folder_compose_refuses_takes_the_project_of_its_own_containers(tmp_path, fakebin, raw, project):
    """Under the name ``otodock``, a guess, a second stack would start beside
    the running one on the same named volumes, a second Postgres on the live
    data folder among them. The project is read from the containers this
    folder started, another install on the same daemon is never taken, and
    --no-db-backup changes nothing of it."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    mine = [_of(c, project, inst) for c in (PG, PROXY, PHONE)]
    theirs = [_of(c, "other", tmp_path / "elsewhere") for c in (PG, PROXY)]
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=(*theirs, *mine), **NO_PARSE,
             FAKE_ENV_LOG=str(tmp_path / "env.log"))
    assert r.returncode == 0, r.stderr
    assert _did(tmp_path, "up") and _projects_used(tmp_path) == {project}
    # With the dump: this project's database, never the other install's.
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(*theirs, *mine), **NO_PARSE,
             FAKE_ENV_LOG=str(tmp_path / "env.log"))
    assert r.returncode == 0, r.stderr
    dumps = [a for a in _calls(tmp_path) if a[:1] == ["exec"] and "pg_dump" in a]
    assert [a[1] for a in dumps] == [f"pg-t2-{project}"]
    (snap,) = [s for s in _snapshots(inst) if (s / "manifest").is_file()]
    assert f"project={project}" in (snap / "manifest").read_text().splitlines()


@pytest.mark.parametrize("containers", ["none", "another-folder", "two-projects"])
def test_a_folder_compose_refuses_with_no_single_project_stops_the_update(tmp_path, fakebin, raw, containers):
    """No container of this folder says the project (the stack was removed,
    the folder was moved), or two projects were started from it: the update
    stops before it reads or changes anything, and never guesses a name."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    found = {"none": [],
             "another-folder": [_of(c, "acme", tmp_path / "elsewhere") for c in (PG, PROXY)],
             "two-projects": [_of(PG, "acme", inst), _of(PROXY, "acme", inst), _of(PROXY, "old", inst)]}[containers]
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=found, **NO_PARSE)
    assert r.returncode == 1
    # The refusal names the projects that hold a database, and never says which one to take.
    dbs = "" if containers == "none" else " These projects have an otodock-postgres service: acme."
    assert f"{REFUSED_FOLDER}{dbs} {RUN_AGAIN_NAMED}" in _said(r)
    assert "unless you changed it" not in _said(r)
    assert _tree(inst) == before and _snapshots(inst) == []
    assert not [a for a in _calls(tmp_path) if a[:1] in (["compose"], ["exec"], ["run"]) and a[:2] != ["compose", "version"]
                and a != ["compose", "config"]]
    # Named by the person, the project is taken as said.
    started = json.dumps([_of(PG, "acme", inst), _of(PROXY, "acme", inst)])
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=found, **NO_PARSE,
             OTODOCK_COMPOSE_PROJECT="acme", FAKE_ENV_LOG=str(tmp_path / "env.log"),
             FAKE_CONTAINERS_AFTER_UP=started)
    assert r.returncode == 0, r.stderr
    assert _projects_used(tmp_path) == {"acme"}


HOLDS_NO_DATABASE = ("no container of the project otodock is OtoDock's database. These projects have an "
                     "otodock-postgres service: acme. Run again with OTODOCK_COMPOSE_PROJECT=<the project of this "
                     "install>. If none is, start this install's stack first.")


@pytest.mark.parametrize("how", ["typed", "typed-folder-refused", "resolved-by-compose"])
def test_a_project_that_holds_no_database_stops_the_update(tmp_path, fakebin, raw, how):
    """The folder was moved or its containers removed, and the project is a
    name no container carries: typed after the refusal's hint, or the one
    compose makes of a new folder's name. Another project's database runs
    on this daemon. With --no-db-backup nothing else stopped the update
    before `up`, so a second stack would start beside the one that runs, on
    the same named volumes. The update refuses before it reads or changes
    anything and names the projects that hold a database."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    theirs = [_of(c, "acme", tmp_path / "elsewhere") for c in (PG, PROXY)]
    env = {"typed": {"OTODOCK_COMPOSE_PROJECT": "otodock"},
           "typed-folder-refused": {"OTODOCK_COMPOSE_PROJECT": "otodock", **NO_PARSE},
           "resolved-by-compose": {}}[how]
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=theirs, **env)
    assert r.returncode == 1
    assert HOLDS_NO_DATABASE in _said(r)
    assert not _did(tmp_path, "up") and not _did(tmp_path, "pull")
    assert _tree(inst) == before and _snapshots(inst) == []
    # A daemon with no OtoDock database at all checks nothing: the name is taken as before.
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=(PROXY,), **env)
    assert HOLDS_NO_DATABASE not in _said(r)


def test_a_folder_compose_refuses_is_found_through_a_link_and_with_its_containers_stopped(tmp_path, fakebin, raw):
    """Compose records the folder as the shell named it or as the disk does:
    both are tried. Stopped containers still say their project. With the
    stack stopped the dump has no database to read, and the script names a
    start that works while compose refuses the folder."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    link = tmp_path / "link"
    link.symlink_to(inst)
    for recorded in (link, inst):
        (tmp_path / "docker.log").unlink(missing_ok=True)
        (tmp_path / "env.log").unlink(missing_ok=True)
        mine = [_of(c, "acme", recorded) for c in (PG, PROXY)]
        r = subprocess.run(["bash", str(SCRIPTS / "upgrade.sh"), "--to", "1.7.0", "--no-db-backup"], cwd=link,
                           env=_env(tmp_path, fakebin, mine, PWD=str(link), **NO_PARSE,
                                    FAKE_ENV_LOG=str(tmp_path / "env.log")),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert _projects_used(tmp_path) == {"acme"}
    (tmp_path / "docker.log").unlink()
    stopped = [_of(c, "acme", inst, running=False) for c in (PG, PROXY)]
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=stopped, **NO_PARSE)
    assert r.returncode == 1
    assert ("no running otodock-postgres container found: start the database (docker start pg-t2-acme) and re-run, "
            "or pass --no-db-backup when you have a fresh backup of your own.") in _said(r)
    assert "docker compose up -d" not in _said(r)
    assert not _did(tmp_path, "pull") and not _did(tmp_path, "up")
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--no-db-backup", containers=stopped, **NO_PARSE,
             FAKE_ENV_LOG=str(tmp_path / "env.log"))
    assert r.returncode == 0, r.stderr
    assert _projects_used(tmp_path) == {"acme"}


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


def test_install_names_the_next_step_when_up_fails(tmp_path, fakebin):
    # A second install.sh is refused in a folder holding the compose files,
    # so a failed start must say to run docker compose up -d there.
    inst = tmp_path / "inst"
    inst.mkdir()
    (tmp_path / "home").mkdir()
    rel = tmp_path / "raw" / "main"
    rel.mkdir(parents=True)
    (rel / "docker-compose.yml").write_text("# the base file\n")
    (rel / "docker-compose.phone.yml").write_text("# the phone overlay\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "OTODOCK_", "COMPOSE_"))}
    env.update({"PATH": f"{fakebin}:{env.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path / "home"),
                "FAKE_LOG": str(tmp_path / "docker.log"), "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
                "FAKE_RAW_ROOT": str(tmp_path / "raw"), "FAKE_UP_RC": "1"})
    r = subprocess.run(["bash", str(SCRIPTS / "install.sh")], cwd=inst, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "docker compose up failed" in r.stderr
    assert "run docker compose up -d in that folder" in " ".join(r.stderr.split())
    assert (inst / "docker-compose.yml").is_file() and (inst / ".env").is_file()


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
    """pull, carry, drain, stop, up (and resume) as they ran: `docker cp` and
    `compose run` are the two ends of one pipeline (they start together), so
    either is "carry"; drain and resume are the admin command line's verbs
    run in the server container."""
    steps: list[str] = []
    for a in _calls(tmp_path):
        if a[:1] == ["cp"]:
            step = "carry"
        elif a[:1] == ["exec"] and a[2:4] == ADMIN_CLI:
            step = a[4]
        elif a[:1] == ["compose"]:
            step = next((s for s in ("pull", "run", "stop", "up") if s in a), "")
        else:
            step = ""
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


CHANGELOG_1_8_0 = """\
# Changelog

## [Unreleased]

## [1.8.0] - 2026-10-13

### Upgrade notes
- **The candidate's note:** admins set BAR.

## [1.7.0] - 2026-09-29

### Upgrade notes
- A 1.7.0 note, below the starting release.
"""


@pytest.mark.parametrize("known_from", [True, False], ids=["from-1.7.0", "from-unknown"])
def test_a_release_candidate_prints_the_notes_of_the_release_it_precedes(
        tmp_path, fakebin, raw, candidate, known_from):
    """The rc is built from its release's commit: its changelog's section is
    the release's, which the one version rule places after the rc."""
    (candidate / "CHANGELOG.md").write_text(CHANGELOG_1_8_0)
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + ("OTODOCK_VERSION=1.7.0\n" if known_from else ""))
    if not known_from:
        (inst / "docker-compose.yml").write_text(
            "services:\n  otodock-proxy:\n    image: ghcr.io/otodock/otodock-proxy:latest\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0-rc2", "--compose-dir", str(candidate), "--dry-run")
    assert r.returncode == 0, r.stderr
    start = "1.7.0" if known_from else "the current release"
    assert f"upgrade notes from {start} to 1.8.0-rc2, the notes of 1.8.0" in r.stdout
    assert "admins set BAR." in r.stdout and "A 1.7.0 note" not in r.stdout
    assert "no upgrade notes" not in r.stdout


def test_a_release_candidate_without_notes_links_its_release(tmp_path, fakebin, raw, candidate):
    (candidate / "CHANGELOG.md").write_text("# Changelog\n\n## [1.8.0] - 2026-10-13\n\n### Fixed\n- A fix.\n")
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=1.7.0\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0-rc2", "--compose-dir", str(candidate), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "no upgrade notes for 1.8.0-rc2" in r.stdout and "releases/tag/v1.8.0\n" in r.stdout


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


def test_a_second_run_that_stops_in_its_checks_says_why_compose_refuses_the_folder(tmp_path, fakebin, raw_1_8):
    """The admin added otodock-compute to the override, as the first run's
    refusal asked, and the second run stops in a check of its own. The folder
    stays one docker compose refuses, and the stop says so, as a stop after
    the files were written already does."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + BAD_YESNO)
    mine = [_of(c, "otodock", inst) for c in (PG, PROXY)]
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0", containers=mine, **NO_PARSE, FAKE_BARE_CONFIG_RC="1")
    said = _said(r)
    assert r.returncode != 0 and "not yes/no words" in said and "stopped before changing anything" in said
    assert ("docker compose refuses this folder's files now. If your override names otodock-compute, run the update "
            "again, or take that part out to use docker compose until you do: 1.6.1 has no such service.") in said
    # A folder compose accepts is told nothing of it.
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0")
    assert "stopped before changing anything" in _said(r) and "refuses this folder's files" not in _said(r)


def test_the_yes_no_rule_is_judged_only_for_a_target_of_1_8_0_or_later(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + BAD_YESNO)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "yes/no" not in r.stderr and "count as ON" not in r.stdout


def test_a_candidate_of_1_8_0_is_judged_by_the_yes_no_rule(tmp_path, fakebin, raw, candidate):
    """A release candidate runs its release's code: the version rule that
    places 1.8.0-rc2 before 1.8.0 must not skip 1.8.0's yes/no check."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + BAD_YESNO)
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0-rc2", "--compose-dir", str(candidate), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "not yes/no words" in r.stderr and "OIDC_ENABLED=yse" in r.stderr


def test_an_install_on_a_candidate_of_1_8_0_already_reads_the_yes_no_rule(tmp_path, fakebin, raw_1_8):
    """From 1.8.0-rc1 to 1.8.0 crosses no change of the yes/no words."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OIDC_ENABLED=1\nOTODOCK_VERSION=1.8.0-rc1\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.8.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "count as ON" not in r.stdout


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
    assert manifest["trees"] == "none" and "agents" not in manifest and "ssh_keys" not in manifest
    assert manifest["credential_keys"] == keys
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


# ── Collabora 26.04: the Engine floor, the scheme line, the stop before up ─────

def test_an_engine_older_than_26_stops_before_anything_changes(tmp_path, fakebin, candidate):
    """The key Collabora signs with is mounted as one file of a volume (a
    subpath, Docker Engine 26): an older engine stops the run while the old
    stack still runs."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             FAKE_API_VERSION="1.44")
    assert r.returncode != 0
    assert "Docker Engine 26 or later" in r.stderr
    assert _tree(inst) == before and not (inst / "backups").exists()
    assert not _did(tmp_path, "pull") and not _did(tmp_path, "up")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate), "--dry-run",
             FAKE_API_VERSION="1.44")
    assert r.returncode == 0 and "the real run stops here" in r.stderr


@pytest.mark.parametrize("url, line, want", [
    ("https://otodock.example.com", None, "COLLABORA_SSL_TERMINATION=true"),
    ("HTTPS://otodock.example.com", "COLLABORA_SSL_TERMINATION=false", "COLLABORA_SSL_TERMINATION=true"),
    ("http://192.168.1.5:8400", "COLLABORA_SSL_TERMINATION=true", "COLLABORA_SSL_TERMINATION=false"),
    ("http://192.168.1.5:8400", None, None),
    (None, None, None),
])
def test_the_collabora_scheme_line_follows_the_dashboard_url(tmp_path, fakebin, candidate, url, line, want):
    """26.04 cannot derive ssl.termination from DASHBOARD_PUBLIC_URL at
    start: the script owns the line, true for https, false (the file's
    default, so no line is added for it) otherwise."""
    env = SECRET_ENV + PHONE_LINE + (f"DASHBOARD_PUBLIC_URL={url}\n" if url else "") + (f"{line}\n" if line else "")
    inst = _install(tmp_path, env)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate))
    assert r.returncode == 0, r.stderr
    lines = [x for x in (inst / ".env").read_text().splitlines() if x.startswith("COLLABORA_SSL_TERMINATION")]
    if want:
        assert lines == [want]
        assert want in r.stdout
    else:
        assert lines == ([line] if line else [])
        assert "COLLABORA_SSL_TERMINATION" not in r.stdout


def test_the_scheme_line_is_named_in_the_dry_run_and_rolled_back_with_the_rest(tmp_path, fakebin, candidate):
    original = SECRET_ENV + PHONE_LINE + "DASHBOARD_PUBLIC_URL=https://otodock.example.com\n"
    inst = _install(tmp_path, original)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "set COLLABORA_SSL_TERMINATION=true in .env" in r.stdout
    assert (inst / ".env").read_text() == original
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             FAKE_IMAGES="ghcr.io/otodock/otodock-proxy:9.9.9")
    assert r.returncode != 0
    assert (inst / ".env").read_text() == original


def test_collabora_stops_before_up_so_its_saves_reach_the_running_server(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--dry-run", containers=(PG, PROXY, COLLABORA))
    assert r.returncode == 0 and "stop Collabora" in r.stdout
    assert not _did(tmp_path, "stop")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", containers=(PG, PROXY, COLLABORA))
    assert r.returncode == 0, r.stderr
    subs = _compose_subcommands(tmp_path)
    assert ["compose", "stop", "-t", "30", "collabora"] in _calls(tmp_path)
    assert subs.index("stop -t") < subs.index("up -d")


def test_no_collabora_running_means_no_stop(tmp_path, fakebin, raw):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0")
    assert r.returncode == 0, r.stderr
    assert not _did(tmp_path, "stop")



def test_a_compose_older_than_2_26_stops_before_anything_changes(tmp_path, fakebin, candidate):
    """Compose 2.25 reads the volume subpath and does not pass it on
    (moby#47687): the editor's key would be the whole volume."""
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             FAKE_COMPOSE_VERSION="2.25.0")
    assert r.returncode != 0 and "older than 2.26" in r.stderr
    assert _tree(inst) == before and not (inst / "backups").exists()
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             FAKE_COMPOSE_VERSION="v2.26.1")
    assert r.returncode == 0, r.stderr


def test_the_scheme_line_holds_through_two_runs(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "DASHBOARD_PUBLIC_URL=https://otodock.example.com\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate))
    assert r.returncode == 0, r.stderr
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.1", "--compose-dir", str(candidate))
    assert r.returncode == 0, r.stderr
    lines = [x for x in (inst / ".env").read_text().splitlines() if "COLLABORA_SSL_TERMINATION" in x]
    assert lines == ["COLLABORA_SSL_TERMINATION=true"]
    assert "setting COLLABORA_SSL_TERMINATION" not in r.stdout


@pytest.mark.parametrize("line, want", [
    ("export COLLABORA_SSL_TERMINATION=false", ["COLLABORA_SSL_TERMINATION=true"]),
    ("# COLLABORA_SSL_TERMINATION=true", ["# COLLABORA_SSL_TERMINATION=true", "COLLABORA_SSL_TERMINATION=true"]),
])
def test_the_scheme_line_reads_every_form(tmp_path, fakebin, candidate, line, want):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE
                    + f"DASHBOARD_PUBLIC_URL=https://otodock.example.com\n{line}\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate))
    assert r.returncode == 0, r.stderr
    assert [x for x in (inst / ".env").read_text().splitlines() if "COLLABORA_SSL_TERMINATION" in x] == want


def test_a_scheme_value_in_the_shell_is_named_and_set_aside(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "DASHBOARD_PUBLIC_URL=https://otodock.example.com\n")
    r = _run(inst, tmp_path, fakebin, "--to", "1.7.0", "--compose-dir", str(candidate),
             COLLABORA_SSL_TERMINATION="false")
    assert r.returncode == 0, r.stderr
    assert "your shell sets COLLABORA_SSL_TERMINATION" in r.stderr
    assert "COLLABORA_SSL_TERMINATION=true" in (inst / ".env").read_text()


# ── the wait for open work before the swap ───────────────────────────────────

CANNOT_WAIT = ("This install runs {}, which cannot wait for open work. Chats and tasks that are "
               "running when the update starts are cut, and they do not continue by themselves. "
               "Run the update when nothing is running. From 2.0 on an update waits for open work.")
PAUSE = "Press Ctrl+C to stop now. The update goes on in 10 seconds."
NO_ANSWER = ("The running server did not say it had finished, so the update does not wait. "
             "Open work is cut and continues after the update.")
ON_2_1 = SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=2.1.0\n"
ON_2_0 = SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=2.0.0\n"
ON_1_9_1 = SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=1.9.1\n"
# A candidate of 1.9.1: older than 2.0, and old enough to update to it.
ON_1_9_1_RC = SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=1.9.1-rc2\n"


@pytest.fixture
def raw_2_1(raw):
    """The published files of v2.1.0 as well: an upgrade from 2.0.0 has a target."""
    rel = raw / "v2.1.0"
    (rel / "scripts").mkdir(parents=True)
    (rel / "docker-compose.yml").write_text("# the 2.1.0 base file\n")
    (rel / "docker-compose.phone.yml").write_text("# the 2.1.0 phone overlay\n")
    shutil.copy2(SCRIPTS / "compose_mounts.py", rel / "scripts" / "compose_mounts.py")
    return raw


def _admin(tmp_path) -> list[list[str]]:
    """Every admin command line call the script ran in a container."""
    return [a for a in _calls(tmp_path) if a[:1] == ["exec"] and a[2:4] == ADMIN_CLI]


def _sleeps(tmp_path) -> list[str]:
    log = tmp_path / "sleep.log"
    return log.read_text().split() if log.exists() else []


def _words(text: str) -> str:
    return " ".join(text.split())


def test_no_script_run_here_names_a_host_command_without_a_fake(tmp_path, fakebin):
    """upgrade.sh and install.sh run with the host's PATH behind the fakes: a
    script may name systemctl, loginctl or sudo only while the fake folder
    holds it, and every fake is what its name resolves to."""
    fakes = {p.name for p in fakebin.iterdir()}
    assert {"docker", "curl", "ip", "sleep"} <= fakes
    for script in ("upgrade.sh", "install.sh"):
        assert host_commands_named(SCRIPTS / script) <= fakes, script
    for name in sorted(fakes):
        found = subprocess.run(["bash", "-c", f"command -v {name}"], env=_env(tmp_path, fakebin),
                               capture_output=True, text=True, timeout=10).stdout.strip()
        assert found == str(fakebin / name), name


@pytest.mark.parametrize("carry, order", [
    ({}, ["pull", "drain", "stop", "up", "resume"]),
    ({**BIN_TARGET, "FAKE_DIFF_BIN": "1"}, ["pull", "carry", "drain", "stop", "up", "resume"]),
], ids=["no-bin-to-carry", "after-the-carry"])
def test_a_server_of_2_0_waits_for_open_work_between_the_pull_and_the_swap(
        tmp_path, fakebin, raw_2_1, carry, order):
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COLLABORA), **carry)
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == order
    assert _admin(tmp_path) == [["exec", "proxy-t2", *ADMIN_CLI, "drain", "--bound", "600"],
                                ["exec", "proxy-t2", *ADMIN_CLI, "resume"]]
    # The verb's own lines, as the script's.
    assert "upgrade.sh: waiting for open work, up to 10 minutes, before the swap\n" in r.stdout
    assert "upgrade.sh: Waiting for open work: 2 turns. Up to 10 minutes.\n" in r.stdout
    assert "upgrade.sh: Nothing is working. New work is held until the platform is back.\n" in r.stdout
    assert "cannot wait for open work" not in r.stdout and NO_ANSWER not in r.stderr
    # The swap is one up of the whole stack.
    ups = [a for a in _calls(tmp_path) if a[:2] == ["compose", "up"]]
    assert ups == [["compose", "up", "-d", "--remove-orphans"]]
    assert "done: this install runs 2.1.0 (was 2.0.0)" in r.stdout


@pytest.mark.parametrize("env_line, shell, bound, words", [
    ("OTODOCK_DRAIN_BOUND_S=120\n", {}, "120", "2 minutes"),
    ("OTODOCK_DRAIN_BOUND_S=120\n", {"OTODOCK_DRAIN_BOUND_S": "0"}, "0", "0 seconds"),
    ("", {"OTODOCK_DRAIN_BOUND_S": "90"}, "90", "90 seconds"),
    ("OTODOCK_DRAIN_BOUND_S=soon\n", {}, "600", "10 minutes"),
])
def test_the_wait_takes_its_bound_from_the_shell_then_the_env_file(
        tmp_path, fakebin, raw_2_1, env_line, shell, bound, words):
    inst = _install(tmp_path, ON_2_0 + env_line)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", **shell)
    assert r.returncode == 0, r.stderr
    assert f"  - wait for open work, up to {words}, before the swap\n" in r.stdout
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", **shell)
    assert r.returncode == 0, r.stderr
    assert _admin(tmp_path)[0] == ["exec", "proxy-t2", *ADMIN_CLI, "drain", "--bound", bound]


def test_a_candidate_of_2_0_can_wait_too(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE + "OTODOCK_VERSION=2.0.0-rc1\n")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert r.returncode == 0, r.stderr
    assert _order(tmp_path) == ["pull", "drain", "up", "resume"]
    # The dump, the wait and its end are everything the script runs inside a container.
    assert [a for a in _changes(tmp_path) if a[:1] == ["exec"]] == [
        ["exec", "pg-t2", "pg_dump", "-U", "otodock", "--clean", "--if-exists", "otodock"],
        ["exec", "proxy-t2", *ADMIN_CLI, "drain", "--bound", "600"],
        ["exec", "proxy-t2", *ADMIN_CLI, "resume"]]


@pytest.mark.parametrize("env_text, runs", [(ON_1_9_1, "1.9.1"), (ON_1_9_1_RC, "1.9.1-rc2")], ids=["1.9.1", "1.9.1-rc2"])
def test_a_server_older_than_2_0_is_asked_nothing_and_the_script_says_so(
        tmp_path, fakebin, raw_2_1, env_text, runs):
    inst = _install(tmp_path, env_text)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", containers=(PG, PROXY, COLLABORA))
    assert r.returncode == 0, r.stderr
    said = _words(r.stdout)
    assert said.count(CANNOT_WAIT.format(runs)) == 1
    assert said.index("cannot wait for open work") < said.index("dry run: nothing is changed")
    assert "wait for open work, up to" not in said and PAUSE not in said
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    assert _tree(inst) == before
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COLLABORA))
    assert r.returncode == 0, r.stderr
    said = _words(r.stdout)
    assert said.count(CANNOT_WAIT.format(runs)) == 1
    assert said.index("cannot wait for open work") < said.index("backing up the database")
    # No terminal: the run goes on at once.
    assert PAUSE not in said and "10" not in _sleeps(tmp_path)
    execs = [a for a in _changes(tmp_path) if a[:1] == ["exec"]]
    assert execs == [["exec", "pg-t2", "pg_dump", "-U", "otodock", "--clean", "--if-exists", "otodock"]]
    assert _order(tmp_path) == ["pull", "stop", "up"]


def test_an_unknown_release_is_asked_nothing_either(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    (inst / "docker-compose.yml").write_text(
        "services:\n  otodock-proxy:\n    image: ghcr.io/otodock/otodock-proxy:latest\n")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert r.returncode == 0, r.stderr
    assert CANNOT_WAIT.format("an older release") in _words(r.stdout)
    assert _admin(tmp_path) == [] and _order(tmp_path) == ["pull", "up"]


@pytest.mark.parametrize("rc, said", [
    ("1", NO_ANSWER),
    ("3", "No server is running, so no work is open. The update goes on."),
    ("2", "The running server is older than 2.0 and cannot wait. Open work is cut by the update."),
])
def test_a_drain_the_server_does_not_answer_warns_and_the_upgrade_goes_on(tmp_path, fakebin, raw_2_1, rc, said):
    """Each answer of the drain has its sentence (3: nothing runs, 2: an older
    command line), and the old server's last log lines stay with the run's
    files: its own log leaves with its container."""
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", FAKE_DRAIN_RC=rc)
    assert r.returncode == 0, r.stderr
    assert f"upgrade.sh: {said}\n" in r.stderr
    # What the verb said is passed on.
    assert "upgrade.sh: admin_cli: The server did not answer the drain." in r.stdout
    assert _order(tmp_path) == ["pull", "drain", "up", "resume"]
    assert "done: this install runs 2.1.0 (was 2.0.0)" in r.stdout
    [log] = list((inst / "backups").glob("upgrade-*/otodock-proxy.log"))
    assert log.read_text() == "old server: its last line before the swap\n"


def test_an_older_server_asked_nothing_still_leaves_its_last_log_lines(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_1_9_1)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert r.returncode == 0, r.stderr
    assert _admin(tmp_path) == []
    calls = _calls(tmp_path)
    logs = [i for i, a in enumerate(calls) if a[:1] == ["logs"]]
    up = next(i for i, a in enumerate(calls) if a[:1] == ["compose"] and "up" in a)
    assert logs and logs[0] < up
    assert list((inst / "backups").glob("upgrade-*/otodock-proxy.log"))


def test_no_running_server_means_nothing_to_wait_for(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", containers=(PG,))
    assert r.returncode == 0 and "wait for open work" not in r.stdout
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG,),
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, PROXY]))
    assert r.returncode == 0, r.stderr
    assert _admin(tmp_path) == [] and _order(tmp_path) == ["pull", "up"]


def test_the_dry_run_names_the_wait_and_asks_the_server_nothing(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", containers=(PG, PROXY, COLLABORA))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    wait = lines.index("  - wait for open work, up to 10 minutes, before the swap")
    assert lines.index("  - run docker compose pull") < wait
    assert wait < lines.index("  - stop Collabora (its open documents save to the running server)")
    assert "cannot wait for open work" not in r.stdout
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    assert _tree(inst) == before


def test_a_run_stopped_while_the_server_waits_calls_the_drain_off(tmp_path, fakebin, raw_2_1):
    """The operator stops the run during the wait: nothing was swapped, so
    the files go back and the server is told to take new work again."""
    inst = _install(tmp_path, ON_2_0)
    before = _tree(inst)
    hold = tmp_path / "hold"
    hold.mkdir()
    proc = subprocess.Popen(
        ["bash", str(SCRIPTS / "upgrade.sh"), "--to", "2.1.0"], cwd=inst,
        env=_env(tmp_path, fakebin, FAKE_DRAIN_HOLD=str(hold)),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not (hold / "started").exists():
            assert proc.poll() is None and time.monotonic() < deadline, "the drain never started"
            time.sleep(0.05)
        proc.terminate()
    finally:
        (hold / "release").touch()
        _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 143, err
    assert _order(tmp_path) == ["pull", "drain", "resume"]
    assert _admin(tmp_path)[-1] == ["exec", "proxy-t2", *ADMIN_CLI, "resume"]
    assert "back as they were" in _words(err)
    assert _tree(inst) == before
    assert not _did(tmp_path, "stop") and not _did(tmp_path, "up")


def test_an_up_rolled_back_after_the_wait_calls_the_drain_off(tmp_path, fakebin, raw_2_1):
    # The old server never stopped (the shape Compose 2.26 leaves): it holds
    # new work until it is told the update is off.
    inst = _install(tmp_path, ON_2_0)
    waiting = dict(NEVER_PROXY, name="0123456789ab_otodock-otodock-proxy-1")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", FAKE_UP_RC="1",
             FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, PROXY, waiting]))
    assert r.returncode == 1, r.stderr
    assert "this install is back on 2.0.0" in _said(r)
    assert _order(tmp_path) == ["pull", "drain", "up", "resume"]
    assert _admin(tmp_path)[-1] == ["exec", "proxy-t2", *ADMIN_CLI, "resume"]


def test_a_rollback_that_stops_says_how_long_new_work_is_held(tmp_path, fakebin, raw_2_1):
    """Nothing calls the drain off here, since the script cannot say what runs:
    the admin reads the limit and how to end it."""
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", FAKE_UP_RC="1", FAKE_UP_AGAIN_RC="1",
             FAKE_STATUS="exited", FAKE_CONTAINERS_AFTER_UP=json.dumps([PG, NEVER_PROXY]))
    assert r.returncode == 1, r.stderr
    said = _said(r)
    assert "putting 2.0.0 back did not finish" in said
    assert "It takes new work again by itself" in said and "within 15 minutes" in said
    assert "bash otodock-admin.sh resume" in said


def test_a_failure_once_the_swap_started_calls_the_drain_off(tmp_path, fakebin, raw_2_1):
    """The old server may still run, and it would hold new work for 15 minutes
    with no reason given. To a new one the call means nothing."""
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", FAKE_HEALTH="starting", OTODOCK_UPGRADE_WAIT_S="0")
    assert r.returncode != 0 and "may already be migrated" in _said(r)
    assert [a[4] for a in _admin(tmp_path)] == ["drain", "resume"]


def test_an_update_that_recreates_nothing_ends_not_holding(tmp_path, fakebin, raw_2_1):
    """A run on the version it is on, the script's own advice after a failure:
    up recreates nothing, so the server that drained is the one that answers.
    It takes new work again before the script says done."""
    inst = _install(tmp_path, ON_2_1)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert r.returncode == 0, r.stderr
    assert "its compose files and .env lines" in _words(r.stdout)
    assert [a[4] for a in _admin(tmp_path)] == ["drain", "resume"]
    calls = _calls(tmp_path)
    up = max(i for i, a in enumerate(calls) if a[:2] == ["compose", "up"])
    resume = next(i for i, a in enumerate(calls) if a[:1] == ["exec"] and a[2:5] == [*ADMIN_CLI, "resume"])
    assert up < resume
    assert "done: this install runs 2.1.0" in r.stdout


def _at_a_terminal(inst, tmp_path, fakebin, *args, **extra) -> tuple[int, str]:
    """upgrade.sh with a terminal on stdin, stdout and stderr: its exit
    status and everything it printed."""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(["bash", str(SCRIPTS / "upgrade.sh"), *args], cwd=inst,
                                env=_env(tmp_path, fakebin, **extra),
                                stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = -1
        printed, deadline = b"", time.monotonic() + 120
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 1.0)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:  # the terminal closed with the script
                break
            if not chunk:
                break
            printed += chunk
        return proc.wait(timeout=30), _words(printed.decode(errors="replace"))
    finally:
        for fd in (master, slave):
            if fd >= 0:
                os.close(fd)


def test_at_a_terminal_an_update_that_cannot_wait_leaves_ten_seconds_to_stop(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_1_9_1)
    rc, said = _at_a_terminal(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run")
    assert rc == 0, said
    assert CANNOT_WAIT.format("1.9.1") in said and PAUSE not in said
    assert "10" not in _sleeps(tmp_path)
    rc, said = _at_a_terminal(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert rc == 0, said
    assert said.index(CANNOT_WAIT.format("1.9.1")) < said.index(PAUSE) < said.index("backing up the database")
    assert _sleeps(tmp_path).count("10") == 1
    assert _admin(tmp_path) == []
    # From 2.0 nothing is asked of the person: the server waits instead.
    (tmp_path / "two").mkdir()
    inst2 = _install(tmp_path / "two", ON_2_0)
    rc, said = _at_a_terminal(inst2, tmp_path, fakebin, "--to", "2.1.0")
    assert rc == 0, said
    assert PAUSE not in said and _sleeps(tmp_path).count("10") == 1
    assert [a[4] for a in _admin(tmp_path)] == ["drain", "resume"]


# ── the compute machine: ROLL's wait after the swap, the shared folders ──────

COMPUTE = {"id": "compute-t2", "labels": {"com.docker.compose.project": "otodock",
                                          "com.docker.compose.service": "otodock-compute"}}


def test_after_the_swap_the_update_waits_for_the_compute_machine(tmp_path, fakebin, raw_2_1):
    """ROLL is the list the wait walks: the server's health, then the
    compute machine's sign-in. The one drain before the swap runs in the
    server's container, never in the compute machine's."""
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE))
    assert r.returncode == 0, r.stderr
    said = _words(r.stdout)
    assert said.index("waiting for open work") < said.index("waiting for the server to report healthy")
    assert said.index("waiting for the server to report healthy") < said.index(
        "waiting for the compute machine to sign in") < said.index("done: this install runs 2.1.0")
    assert "The compute machine is signed in." in said
    admin = _admin(tmp_path)
    assert {a[1] for a in admin} == {"proxy-t2"}
    assert ["exec", "proxy-t2", *ADMIN_CLI, "compute", "--wait", "300"] in admin


def test_a_compute_machine_that_does_not_sign_in_is_said_and_the_update_ends(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0)
    line = ("The compute machine is not signed in. Claude Code and Codex chats run on the "
            "platform itself until it is.")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE),
             FAKE_COMPUTE_RC="1", FAKE_COMPUTE_LINE=line, OTODOCK_UPGRADE_WAIT_S="60")
    assert r.returncode == 0, r.stderr
    assert line in _words(r.stdout) and "done: this install runs 2.1.0" in _words(r.stdout)
    assert (f"It may still be preparing its tools. To see why, run docker compose logs otodock-compute in {inst}."
            in _words(r.stderr))
    assert ["exec", "proxy-t2", *ADMIN_CLI, "compute", "--wait", "60"] in _admin(tmp_path)


def test_an_install_without_a_compute_service_is_not_waited_for(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0")
    assert r.returncode == 0, r.stderr
    assert "compute machine" not in _words(r.stdout)
    assert not any("compute" in a for a in _admin(tmp_path))


SPLIT = ("Your compose override mounts {} for otodock-proxy only. Chats run in otodock-compute, which must see "
         "the same folders. Add the same mounts under otodock-compute in your override, then run the update again "
         "right away: OTODOCK_COMPOSE_PROJECT={} bash upgrade.sh --to {}")
# Said when the installed release's compose file has no otodock-compute: the fixed override breaks it.
UNTIL_THEN = ("Until then docker compose in this folder refuses the override, because {} has no otodock-compute. "
              "The running containers are not affected.")


UNTOUCHED = "stopped before changing anything: the running stack was not touched."


def _compares(tmp_path) -> list[list[str]]:
    """Each run of the folders' compare: a container of an image, its Python as the entrypoint."""
    return [a for a in _calls(tmp_path) if a[:1] == ["run"] and "--entrypoint" in a]


def test_an_override_that_moves_a_shared_folder_for_the_proxy_alone_stops_the_update(tmp_path, fakebin, raw_2_1):
    """The compute machine would work in another folder, and nothing else runs
    a chat. The update stops before the dump and before the pull: it names
    each folder, the fix and the exact second run, and nothing of the install
    was touched or written."""
    inst = _install(tmp_path, ON_2_0)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, dict(PROXY, image="sha256:old"), COMPUTE),
             FAKE_SPLIT="/opt/otodock/agents\n/opt/my-tools", **BIN_TARGET, FAKE_DIFF_BIN="1")
    assert r.returncode == 1
    assert SPLIT.format("/opt/otodock/agents, /opt/my-tools", "otodock", "2.1.0") in _said(r)
    # The installed file has no otodock-compute here: the person is told what the fix does to the folder.
    assert UNTIL_THEN.format("2.0.0") in _said(r)
    assert UNTOUCHED in _said(r) and "back as they were" not in _said(r)
    assert _tree(inst) == before and _snapshots(inst) == []
    assert "OTODOCK_COMPUTE_SESSIONS" not in (inst / ".env").read_text()
    # Read with the image of the server that runs, the target's own compare
    # handed to its Python, and no network. No dump, no pull, no wait for
    # open work, no copy of the recover bin.
    (run,) = _compares(tmp_path)
    at = run.index("--entrypoint")
    assert run[at:at + 3] == ["--entrypoint", "/opt/otodock/proxy/venv/bin/python", "sha256:old"]
    assert run[at + 3:] == ["-c", (SCRIPTS / "compose_mounts.py").read_text().rstrip("\n")]
    assert run[run.index("--network") + 1] == "none" and "--rm" in run
    assert _order(tmp_path) == [] and not _admin(tmp_path)
    assert not any(a[:1] == ["exec"] for a in _changes(tmp_path))
    # What it compared: the files the upgraded stack loads, the new base file standing in for the old.
    read = [a for a in _calls(tmp_path) if a[:1] == ["compose"] and a[-3:] == ["config", "--format", "json"]]
    assert read and all(any(x.endswith("/new/docker-compose.yml") for x in a) for a in read)


def test_the_compare_runs_for_real_on_the_files_the_upgraded_stack_loads(tmp_path, fakebin, raw_2_1):
    """The script's text is handed to the image's Python whole, and what it
    prints is what the person reads: the agents folder moved for the proxy
    alone, and a sandbox option the override still hands the proxy."""
    def vol(source, target):
        return {"type": "volume", "source": source, "target": target}
    config = {"services": {
        "otodock-proxy": {"volumes": [vol("/srv/agents", "/opt/otodock/agents"),
                                      vol("otodock-skills", "/opt/otodock/mcps/skills")],
                          "security_opt": ["seccomp=unconfined", "no-new-privileges:true"]},
        "otodock-compute": {"volumes": [vol("otodock-agents", "/opt/otodock/agents"),
                                        vol("otodock-skills", "/opt/otodock/mcps/skills")]}}}
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE),
             FAKE_CONFIG_JSON=json.dumps(config))
    assert r.returncode == 1
    assert SPLIT.format("/opt/otodock/agents", "otodock", "2.1.0") in _said(r)
    assert "Your compose override sets seccomp=unconfined for otodock-proxy." in _said(r)
    assert UNTOUCHED in _said(r)


def test_a_dry_run_says_the_override_would_stop_the_update(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", containers=(PG, PROXY, COMPUTE),
             FAKE_SPLIT="/opt/otodock/agents")
    assert r.returncode == 0, r.stderr
    assert SPLIT.format("/opt/otodock/agents", "otodock", "2.1.0") in _said(r)
    assert "(the real run stops here)" in _said(r)
    assert "dry run: nothing is changed" in r.stdout and _tree(inst) == before and _snapshots(inst) == []
    assert _order(tmp_path) == []


@pytest.mark.parametrize("server", ["none", "stopped", "older-without-the-compare"])
def test_the_folders_are_compared_with_what_this_install_has(tmp_path, fakebin, raw_2_1, server):
    """A stopped server's image compares as well as a running one's, before
    anything changes. With no server container, or one whose image cannot
    run the compare, the pulled image does it after the pull: the update
    then stops before the swap and puts .env and the compose files back."""
    inst = _install(tmp_path, ON_2_0)
    before = _tree(inst)
    extra = {"FAKE_SPLIT": "/opt/otodock/agents", "FAKE_IMAGES": "ghcr.io/otodock/otodock-proxy:2.1.0"}
    containers = {"none": (PG,), "stopped": (PG, dict(PROXY, running=False, image="sha256:old")),
                  "older-without-the-compare": (PG, dict(PROXY, image="sha256:old"))}[server]
    if server == "older-without-the-compare":
        (raw_2_1 / "v2.1.0" / "scripts" / "compose_mounts.py").unlink()     # not fetched: the image's own copy
        # Its server holds a recover bin the update would carry: a refused update carries nothing.
        extra.update(BIN_TARGET, FAKE_NO_COMPARE="sha256:old", FAKE_DIFF_BIN="1")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=containers, **extra)
    assert r.returncode == 1
    assert SPLIT.format("/opt/otodock/agents", "otodock", "2.1.0") in _said(r)
    assert _tree(inst) == before
    images = [a[a.index("--entrypoint") + 2] for a in _compares(tmp_path)]
    if server == "stopped":
        assert images == ["sha256:old"] and UNTOUCHED in _said(r) and _order(tmp_path) == []
    else:
        assert images[-1] == "ghcr.io/otodock/otodock-proxy:2.1.0" and len(images) == (2 if server != "none" else 1)
        assert "back as they were" in _said(r) and _order(tmp_path) == ["pull"]
    # A dry run with no server to read the folders with says when they are compared.
    if server == "none":
        (tmp_path / "docker.log").unlink()
        r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", "--dry-run", containers=containers, **extra)
        assert r.returncode == 0, r.stderr
        assert "compare the folders of otodock-proxy and otodock-compute (after the pull" in _words(r.stdout)
        assert _compares(tmp_path) == []


NOT_COMPARED = "the folders of your override could not be compared with otodock-compute's: compare them yourself."
# What `docker compose config --format json` gives the compare when both services mount the same folders.
MATCHED = {"FAKE_CONFIG_JSON": json.dumps({"services": {"otodock-proxy": {"volumes": []},
                                                        "otodock-compute": {"volumes": []}}})}


def test_a_compare_that_could_not_run_says_so(tmp_path, fakebin, raw_2_1):
    """No server container to read the folders with, and the pulled image
    cannot run the compare either: a split override would go through
    unread, so the person is told to compare them. A compare that ran says
    nothing of it, nor does a release that ships no compare."""
    inst = _install(tmp_path, ON_2_0)
    after = {"FAKE_CONTAINERS_AFTER_UP": json.dumps([PG, PROXY, COMPUTE])}
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG,), **after)
    assert r.returncode == 0, r.stderr
    assert NOT_COMPARED in _said(r)
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE), **MATCHED)
    assert r.returncode == 0, r.stderr
    assert _compares(tmp_path) and NOT_COMPARED not in _said(r)
    (tmp_path / "docker.log").unlink()
    (raw_2_1 / "v2.1.0" / "scripts" / "compose_mounts.py").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG,), **after)
    assert r.returncode == 0, r.stderr
    assert NOT_COMPARED not in _said(r)


def _rig_words() -> dict[str, str]:
    """The sentences the rig's upgrade scenario looks for in the update's log, read from its file."""
    import ast
    found: dict[str, str] = {}
    for node in ast.parse((REPO / "rig" / "upgrade" / "u10_upgrade.py").read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        target = node.targets[0]
        pairs = [(target.id, value)] if isinstance(target, ast.Name) else zip((e.id for e in target.elts), value, strict=True)
        found.update({name: v for name, v in pairs if isinstance(v, str)})
    return found


def test_the_rigs_upgrade_scenario_reads_a_refusal_as_the_script_prints_it(tmp_path, fakebin, candidate):
    """The scenario judges the update by sentences of its log, with the line
    breaks folded: the refusal wraps its lines. Every sentence it looks for
    is one the script prints, a refused run prints none of the later steps'
    words, and a run that goes through prints each of them."""
    rig = _rig_words()
    inst = _install(tmp_path, ON_1_9_1)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0", "--compose-dir", str(candidate), FAKE_SPLIT="/opt/rig-bin")
    words = _words(r.stdout + r.stderr)
    assert r.returncode == 1
    assert rig["PROXY_ALONE"] in words and rig["UNTOUCHED"] in words and rig["REFUSES_UNTIL"] in words
    assert rig["AGAIN"].format("otodock", "2.0.0", candidate) in words
    assert not any(rig[late] in words for late in ("DUMP", "PULL", "SWAP"))
    (tmp_path / "docker.log").unlink()
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0", "--compose-dir", str(candidate))
    words = _words(r.stdout + r.stderr)
    assert r.returncode == 0, r.stderr
    assert all(rig[step] in words for step in ("DUMP", "PULL", "SWAP", "CANNOT_WAIT"))
    assert rig["PROXY_ALONE"] not in words and rig["UNTOUCHED"] not in words


def test_shared_folders_that_match_change_nothing(tmp_path, fakebin, raw_2_1):
    inst = _install(tmp_path, ON_2_0 + "OTODOCK_COMPUTE_SESSIONS=local\n")
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE), **MATCHED)
    assert r.returncode == 0, r.stderr
    assert "override" not in _words(r.stdout + r.stderr)
    # A line an earlier build wrote is the server's to name at its start, never this script's.
    assert "OTODOCK_COMPUTE_SESSIONS=local" in (inst / ".env").read_text().splitlines()
    assert "OTODOCK_COMPUTE_SESSIONS" not in r.stdout + r.stderr
    (summary,) = [ln for ln in r.stdout.splitlines() if ln.strip().startswith(".env:")]
    assert _words(summary) == ".env: OTODOCK_VERSION=2.1.0"


def test_a_sandbox_privilege_an_override_hands_the_proxy_is_named_and_kept(tmp_path, fakebin, raw_2_1):
    """The proxy starts no sandbox. An override that still relaxes its
    container widens nothing the install did not have: the update goes on,
    says which lines can go, and where a profile set there for the chats
    belongs now."""
    inst = _install(tmp_path, ON_2_0)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE),
             FAKE_SPLIT="proxy: seccomp=unconfined\nproxy: apparmor=otodock_userns\nproxy: /dev/net/tun")
    assert r.returncode == 0, r.stderr
    assert ("Your compose override sets seccomp=unconfined, apparmor=otodock_userns, /dev/net/tun for otodock-proxy. "
            "The server needs none of it now: chats run in otodock-compute. You can delete those lines. If you set "
            "an AppArmor profile there for the chats, set OTODOCK_APPARMOR_PROFILE in .env instead: otodock-compute "
            "reads it.") in _said(r)
    assert _said(r).count("Your compose override sets") == 1 and len(_compares(tmp_path)) == 1    # said once
    assert "for otodock-proxy only" not in _said(r)
    assert "done: this install runs 2.1.0" in _words(r.stdout)


def test_the_update_has_finished_before_it_waits_for_the_compute_machine(tmp_path, fakebin, raw_2_1):
    """A run on the same version takes new work again before the compute wait,
    and a Ctrl+C in that wait never says the database may be migrated."""
    inst = _install(tmp_path, ON_2_1)
    r = _run(inst, tmp_path, fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE))
    assert r.returncode == 0, r.stderr
    assert [a[4] for a in _admin(tmp_path)] == ["drain", "resume", "compute"]
    (tmp_path / "two").mkdir()
    inst2 = _install(tmp_path / "two", ON_2_0)
    r = _run(inst2, tmp_path / "two", fakebin, "--to", "2.1.0", containers=(PG, PROXY, COMPUTE),
             FAKE_COMPUTE_SIGNAL="INT")
    assert r.returncode != 0
    assert "may already be migrated" not in _said(r)


# ── the oldest release an update to 2.0 starts from ──────────────────────────

OLDER_THAN_1_9_1 = ("This install runs {}. Update it to 1.9.1 first with bash upgrade.sh --to 1.9.1, "
                    "then run this again.")
LATEST_IMAGE = "services:\n  otodock-proxy:\n    image: ghcr.io/otodock/otodock-proxy:latest\n"


@pytest.fixture
def raw_2_0(raw_2_1):
    """The published files of v1.9.1, v2.0.0 and v2.0.1 as well."""
    for version in ("1.9.1", "2.0.0", "2.0.1"):
        rel = raw_2_1 / f"v{version}"
        (rel / "scripts").mkdir(parents=True)
        (rel / "docker-compose.yml").write_text(f"# the {version} base file\n")
        (rel / "docker-compose.phone.yml").write_text(f"# the {version} phone overlay\n")
        shutil.copy2(SCRIPTS / "compose_mounts.py", rel / "scripts" / "compose_mounts.py")
    return raw_2_1


def _on(version: str) -> str:
    return SECRET_ENV + PHONE_LINE + f"OTODOCK_VERSION={version}\n"


def _refused_untouched(r, inst, before, tmp_path, runs: str) -> None:
    assert r.returncode == 1
    assert OLDER_THAN_1_9_1.format(runs) in _said(r)
    assert _tree(inst) == before and not (inst / "backups").exists()
    assert not any(_did(tmp_path, sub) for sub in ("pull", "stop", "up", "run"))
    assert not any("pg_dump" in a or a[:1] == ["cp"] for a in _calls(tmp_path))


@pytest.mark.parametrize("env_text, db, runs", [
    (_on("1.9.0"), "", "1.9.0"),
    (SECRET_ENV + PHONE_LINE, "", "1.6.1"),        # the version the compose file names
    (_on("1.9.0"), "1.8.0", "1.9.0"),              # an older database never lowers it
    (_on("1.8.0"), "1.9.0", "1.9.0"),              # a newer database raises it
    (_on("1.9.0-rc3"), "", "1.9.0-rc3"),
], ids=["1.9.0", "compose-1.6.1", "older-db", "newer-db", "1.9.0-rc3"])
def test_an_install_older_than_1_9_1_is_refused_2_0_before_anything_changes(
        tmp_path, fakebin, raw_2_0, env_text, db, runs):
    inst = _install(tmp_path, env_text)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0", FAKE_DB_VERSION=db)
    _refused_untouched(r, inst, before, tmp_path, runs)


def test_an_unknown_version_takes_the_databases_and_is_refused_by_it(tmp_path, fakebin, raw_2_0):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    (inst / "docker-compose.yml").write_text(LATEST_IMAGE)
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.1", FAKE_DB_VERSION="1.9.0")
    _refused_untouched(r, inst, before, tmp_path, "1.9.0")


def test_a_candidate_of_2_0_refuses_an_older_install_too(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, _on("1.9.0"))
    before = _tree(inst)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0-rc1", "--compose-dir", str(candidate))
    _refused_untouched(r, inst, before, tmp_path, "1.9.0")


@pytest.mark.parametrize("env_text, target", [
    (_on("1.8.0"), "1.9.1"),           # the step the refusal names stays open
    (_on("1.6.1"), "1.9.1"),
    (_on("1.9.1"), "2.0.0"),
    (_on("1.9.1-rc2"), "2.0.0"),       # a candidate of 1.9.1 counts as 1.9.1
    (_on("2.0.0"), "2.0.1"),
    (_on("2.0.0-rc1"), "2.0.1"),
], ids=["1.8.0-to-1.9.1", "1.6.1-to-1.9.1", "1.9.1-to-2.0.0", "1.9.1-rc2-to-2.0.0", "2.0.0-to-2.0.1",
        "2.0.0-rc1-to-2.0.1"])
def test_an_update_from_1_9_1_on_or_to_a_target_before_2_0_goes_on(tmp_path, fakebin, raw_2_0, env_text, target):
    inst = _install(tmp_path, env_text)
    r = _run(inst, tmp_path, fakebin, "--to", target, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Update it to 1.9.1 first" not in _said(r)


def test_an_unknown_version_with_no_database_goes_on(tmp_path, fakebin, raw_2_0):
    inst = _install(tmp_path, SECRET_ENV + PHONE_LINE)
    (inst / "docker-compose.yml").write_text(LATEST_IMAGE)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "upgrading from an unknown release to 2.0.0" in r.stdout


def test_a_candidate_of_2_0_takes_a_1_9_1_install(tmp_path, fakebin, candidate):
    inst = _install(tmp_path, ON_1_9_1)
    r = _run(inst, tmp_path, fakebin, "--to", "2.0.0-rc1", "--compose-dir", str(candidate), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Update it to 1.9.1 first" not in _said(r)
