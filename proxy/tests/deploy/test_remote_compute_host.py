"""The compute machine on another server, on its server: the install script
the pasted command runs (``scripts/remote-compute-install.sh``, served by the
bootstrap door with its values) and the host service
(``scripts/otodock-remote-compute.sh``).

Both run here with every command that acts on the host faked first on PATH
(docker, systemctl, systemd-run, curl, uname, id, chown, sleep, df, and an
install that can fail on purpose), each call logged; a guard fails when a script names a host command the PATH does not
fake, and every path the scripts write lives under a scratch root
(``OTODOCK_COMPUTE_HOST_ROOT``). Nothing here reaches a real service manager,
container engine or home folder.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.deploy.fake_docker import host_calls, host_commands_named, write_host_fakes

_REPO = Path(__file__).resolve().parents[3]
INSTALL = _REPO / "scripts" / "remote-compute-install.sh"
TOOL = _REPO / "scripts" / "otodock-remote-compute.sh"
MACHINE = "compute-0123456789abcdef"
TOKEN = "t" * 43
VERSION = "0.5.177"

# The fake engine: a world of images, containers, volumes and networks in one
# JSON file, every argv logged. ``run`` answers the three runs the scripts make
# (the satellite version read, the sandbox's look, the machine itself).
_FAKE_DOCKER = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
home = os.environ["FAKE_HOME"]
with open(os.path.join(home, "docker.log"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
world_path = os.path.join(home, "world.json")
world = json.load(open(world_path))
def save():
    json.dump(world, open(world_path, "w"))
def flag(name):
    return args[args.index(name) + 1] if name in args else ""
sub = args[0] if args else ""
if sub == "info":
    if world.get("docker_down"):
        sys.exit(1)
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    print("/var/lib/docker" if "DockerRootDir" in fmt else world.get("info", "29.6.1|Ubuntu 24.04 LTS")); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    ref = args[-1]
    if ref not in world["images"]:
        sys.exit(1)
    if "--format" in args:
        print(world.get("arch", "amd64"))
    sys.exit(0)
if args[:2] == ["image", "rm"]:
    world["images"] = [i for i in world["images"] if i != args[2]]; world.setdefault("removed_images", []).append(args[2]); save(); sys.exit(0)
if sub == "pull":
    ref = args[-1]
    if ref in world.get("pullable", []):
        world["images"].append(ref); save(); sys.exit(0)
    sys.exit(1)
if sub == "ps":
    for c in world.get("containers", []):
        print(c)
    sys.exit(0)
if sub in ("rm", "stop"):
    world["containers"] = [c for c in world.get("containers", []) if c != args[-1]]; save(); sys.exit(0)
if sub in ("network", "volume"):
    kind, verb = sub, args[1]
    have = world.setdefault(kind + "s", [])
    if verb == "inspect":
        if args[-1] not in have:
            sys.exit(1)
        if "--format" in args:
            mount = os.path.join(home, "root", "var/lib/docker/volumes", args[-1], "_data")
            print(mount[len(os.path.join(home, "root")):])
        sys.exit(0)
    if verb == "create":
        have.append(args[-1])
        os.makedirs(os.path.join(home, "root", "var/lib/docker/volumes", args[-1], "_data"), exist_ok=True)
        save(); sys.exit(0)
    if verb == "ls":
        for n in have:
            print(n)
        sys.exit(0)
    if verb == "rm":
        world[kind + "s"] = [n for n in have if n != args[-1]]; save(); sys.exit(0)
if sub == "inspect":
    print("healthy"); sys.exit(0)
if sub == "run":
    if "--entrypoint" in args:
        print(world.get("satellite", "VERSION")); sys.exit(0)
    if "--check" in args:
        if world.get("preflight_hang"):
            import time
            time.sleep(30)
        image = args[args.index("/opt/otodock/satellite/venv/bin/python") - 1]
        said = world.get("preflight_by_image", {}).get(image, world.get("preflight", "ok"))
        print(said); sys.exit(0 if said == "ok" else 1)
    world.setdefault("runs", []).append(args); save()
    sys.exit(int(world.get("exit", 0)))
sys.exit(0)
'''

_FAKE_CURL = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
home = os.environ["FAKE_HOME"]
with open(os.path.join(home, "curl.log"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
def arg(name):
    return args[args.index(name) + 1]
if "--data-binary" in args:
    # The install's exchange: headers and body from files.
    sent = {"headers": open(arg("-H")[1:]).read(), "body": open(arg("--data-binary")[1:]).read(), "url": args[-1]}
    json.dump(sent, open(os.path.join(home, "sent.json"), "w"))
    code = os.environ.get("FAKE_CURL_CODE", "200")
    open(arg("-o"), "w").write(json.dumps({"detail": os.environ.get("FAKE_CURL_DETAIL", "")}) if code != "200" else "{}")
    print(code, end="")
    sys.exit(0)
# The host's question to the platform: its headers on stdin, the answer the world holds.
asks_path = os.path.join(home, "asks.json")
asks = json.load(open(asks_path)) if os.path.exists(asks_path) else []
asks.append({"args": args, "stdin": sys.stdin.read()})
json.dump(asks, open(asks_path, "w"))
world = json.load(open(os.path.join(home, "world.json")))
if "host_curl" in world:
    # curl's own failure: no platform answered (it prints its write-out, or nothing).
    print(world["host_curl"]["out"], end="")
    sys.exit(world["host_curl"]["exit"])
answer = world.get("host_answer", {"code": 401, "body": {"detail": "Invalid credentials"}})
print(json.dumps(answer["body"], separators=(",", ":")) + "\n" + str(answer["code"]), end="")
'''

_FAKE_SHELL = {
    "systemctl": 'case "$1" in --version) echo "systemd ${FAKE_SYSTEMD:-249} (249.11)";; is-active) echo active;; esac\n',
    "systemd-run": "",
    "uname": 'case "$1" in -s) echo "${FAKE_OS:-Linux}";; -m) echo "${FAKE_ARCH:-x86_64}";; esac\n',
    "id": 'echo "${FAKE_UID:-0}"\n',
    "chown": "",
    # A fake clock never moves: a test that waits names how many sleeps it allows.
    "sleep": ('n=$(cat "$FAKE_HOME/sleeps" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_HOME/sleeps"\n'
              'echo "$*" >> "$FAKE_HOME/sleep.args"\n'
              'if [ -n "${FAKE_SLEEP_LIMIT:-}" ] && [ "$n" -ge "$FAKE_SLEEP_LIMIT" ]; then exit 1; fi\n'),
    "df": 'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/x 1 1 ${FAKE_DF_KB:-52428800} 1%% /\\n"\n',
    # install as the system's, failing for a path that names FAKE_INSTALL_FAIL.
    "install": ('for a in "$@"; do case "$a" in *"${FAKE_INSTALL_FAIL:-//none//}"*) '
                'echo "install: No space left on device" >&2; exit 1;; esac; done\nexec /usr/bin/install "$@"\n'),
}


@pytest.fixture
def host(tmp_path):
    """A scratch server: fakes first on PATH, the scripts' root under it."""
    bindir, root = tmp_path / "bin", tmp_path / "root"
    bindir.mkdir()
    for name, body in _FAKE_SHELL.items():
        path = bindir / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "$FAKE_HOME/host.log"\n{body}')
        path.chmod(0o755)
    for name, body in (("docker", _FAKE_DOCKER), ("curl", _FAKE_CURL)):
        path = bindir / name
        path.write_text(body.replace("#!PYTHON", f"#!{sys.executable}", 1).replace("VERSION", VERSION))
        path.chmod(0o755)
    write_host_fakes(bindir)
    (root / "run/systemd/system").mkdir(parents=True)
    (root / "dev/net").mkdir(parents=True)
    (root / "dev/net/tun").touch()
    world = {"images": ["ghcr.io/otodock/otodock-proxy:2.0.0"], "networks": [], "volumes": [], "containers": []}
    (tmp_path / "world.json").write_text(json.dumps(world))
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_HOME": str(tmp_path),
           "OTODOCK_COMPUTE_HOST_ROOT": str(root), "LANG": "C.UTF-8"}
    for name in ("docker", "systemctl", "systemd-run", "curl", "sudo", "chown", "id", "uname", "sleep", "df",
                 "install"):
        assert shutil.which(name, path=env["PATH"]) == str(bindir / name), name
    return {"tmp": tmp_path, "bin": bindir, "root": root, "env": env}


def _world(host) -> dict:
    return json.loads((host["tmp"] / "world.json").read_text())


def _set_world(host, **change) -> None:
    (host["tmp"] / "world.json").write_text(json.dumps({**_world(host), **change}))


def _log(host, name: str) -> list:
    path = host["tmp"] / name
    if not path.exists():
        return []
    if name.endswith(".log") and name != "host.log":
        return [json.loads(line) for line in path.read_text().splitlines()]
    return path.read_text().splitlines()


def _script(image="ghcr.io/otodock/otodock-proxy:2.0.0", public=True) -> str:
    """The script the bootstrap door serves, made by the platform's own
    function for this image."""
    from unittest.mock import patch

    import config
    from services.remote import compute_pairing
    repository, tag = image.rsplit(":", 1)
    with patch.object(compute_pairing, "wanted_image", lambda: compute_pairing.Image(repository, tag, public)), \
            patch.object(config, "DASHBOARD_PUBLIC_URL", "http://192.168.50.2:8400"):
        return compute_pairing.bootstrap_script(MACHINE, TOKEN, "192.168.50.2:8400", VERSION).decode()


def _install(host, *, extra_env=None, **kw) -> subprocess.CompletedProcess:
    env = {**host["env"], **(extra_env or {})}
    return subprocess.run(["bash"], input=_script(**kw), env=env, capture_output=True, text=True, timeout=60,
                          check=False)


def _tool(host, *args, extra_env=None) -> subprocess.CompletedProcess:
    env = {**host["env"], **(extra_env or {})}
    return subprocess.run(["bash", str(TOOL), *args], env=env, capture_output=True, text=True, timeout=60,
                          check=False)


def test_every_host_command_the_scripts_name_is_faked_here(host):
    named = host_commands_named(INSTALL) | host_commands_named(TOOL)
    assert named <= {"systemctl", "sudo"}
    for name in named:
        assert (host["bin"] / name).exists()


# ---------------------------------------------------------------------------
# the install
# ---------------------------------------------------------------------------

def test_the_install_checks_pairs_with_a_hash_and_starts_the_service(host):
    r = _install(host)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OtoDock adds this server as a compute machine" in r.stdout and "Paired." in r.stdout
    state = host["root"] / "var/lib/otodock-remote-compute"
    credential = json.loads((state / "sign-in/credential").read_text())
    assert credential["machine_id"] == MACHINE
    secret = credential["machine_secret"]
    assert len(secret) >= 43 and oct((state / "sign-in/credential").stat().st_mode)[-3:] == "600"
    sent = json.loads((host["tmp"] / "sent.json").read_text())
    # Only the hash travels, and the token rides a header file, never argv.
    assert json.loads(sent["body"]) == {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()}
    assert f"X-Pairing-Token: {TOKEN}" in sent["headers"]
    assert sent["url"] == "http://192.168.50.2:8400/v1/satellite/bootstrap"
    everything = json.dumps(_log(host, "docker.log") + _log(host, "curl.log") + _log(host, "host.log"))
    assert secret not in everything and TOKEN not in everything and secret not in r.stdout + r.stderr
    conf = dict(line.split("=", 1) for line in (state / "compute.conf").read_text().splitlines())
    assert conf["MACHINE_ID"] == MACHINE and conf["REPOSITORY"] == "ghcr.io/otodock/otodock-proxy"
    assert conf["TAG"] == "2.0.0" and conf["PLATFORM_URL"] == "http://192.168.50.2:8400"
    assert conf["APPARMOR"] == "unconfined" and conf["PULLED"] == ""
    unit = (host["root"] / "etc/systemd/system/otodock-remote-compute.service").read_text()
    for line in ("ExecStart=/opt/otodock-remote-compute/otodock-remote-compute run",
                 "ExecStop=-/opt/otodock-remote-compute/otodock-remote-compute stop",
                 "ExecStopPost=/opt/otodock-remote-compute/otodock-remote-compute after",
                 "Restart=always", "Wants=docker.service network-online.target",
                 "After=docker.service network-online.target", "OOMScoreAdjust=-500"):
        assert line in unit.splitlines()
    # A stop of Docker alone never leaves the machine stopped after it, and no
    # exit status does either: a removal ring the platform did not confirm
    # starts the machine again (a confirmed one stops the unit by `remove`).
    assert not [line for line in unit.splitlines()
                if line.startswith(("Requires=", "BindsTo=", "RestartPreventExitStatus="))]
    assert (host["root"] / "opt/otodock-remote-compute/otodock-remote-compute").read_text() == TOOL.read_text()
    calls = _log(host, "host.log")
    assert "systemctl daemon-reload" in calls and "systemctl restart otodock-remote-compute.service" in calls
    assert "chown 1000:1000 " + f"{state}/sign-in {state}/sign-in/credential" in calls
    assert not (state / "word").exists()
    # The exchange is bounded in time.
    exchange = next(a for a in _log(host, "curl.log") if "--data-binary" in a)
    assert exchange[exchange.index("--max-time") + 1] == "60" and "--proto" not in exchange
    # The sandbox's one look ran with the machine's options.
    look = next(a for a in _log(host, "docker.log") if a[:1] == ["run"] and "--check" in a)
    for opt in ("seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined", "/dev/net/tun", "1000:1000"):
        assert opt in look
    assert look[-1] == VERSION and not host_calls(host["bin"])


def test_the_install_never_touches_an_installs_own_compute_unit(host):
    own_unit = host["root"] / "etc/systemd/system/otodock-compute.service"
    own_state = host["root"] / "var/lib/otodock-compute"
    own_unit.parent.mkdir(parents=True, exist_ok=True)
    own_unit.write_text("[Unit]\nDescription=the install's own\n")
    own_state.mkdir(parents=True)
    (own_state / "keep").write_text("kept")
    assert _install(host).returncode == 0
    assert _tool(host, "remove", "--yes").returncode == 0
    assert own_unit.read_text() == "[Unit]\nDescription=the install's own\n"
    assert (own_state / "keep").read_text() == "kept"


@pytest.mark.parametrize("change, said", [
    ({"FAKE_UID": "1000"}, "with sudo"),
    ({"FAKE_OS": "Darwin"}, "runs on Linux"),
    ({"FAKE_ARCH": "riscv64"}, "x86-64 or ARM64"),
    ({"FAKE_SYSTEMD": "219"}, "systemd 236"),
])
def test_a_server_that_cannot_run_it_is_refused_in_one_sentence_and_nothing_changes(host, change, said):
    r = _install(host, extra_env=change)
    assert r.returncode == 1 and said in r.stderr and "Nothing was changed." in r.stderr
    assert not (host["root"] / "var/lib/otodock-remote-compute").exists()
    assert not (host["tmp"] / "sent.json").exists()


@pytest.mark.parametrize("world, said", [
    ({"info": "19.03.1|Ubuntu"}, "too old"),
    ({"info": "28.0.1|Docker Desktop"}, "Docker Desktop"),
    ({"satellite": "0.5.170"}, "runs satellite 0.5.170"),
    ({"arch": "arm64"}, "built for arm64"),
    ({"preflight": "bwrap is not installed"}, "bwrap is not installed"),
])
def test_an_image_or_an_engine_that_cannot_serve_is_refused_before_the_pairing(host, world, said):
    _set_world(host, **world)
    r = _install(host)
    assert r.returncode == 1 and said in r.stderr
    assert not (host["tmp"] / "sent.json").exists() and not (host["root"] / "var/lib/otodock-remote-compute").exists()


def test_no_tun_device_is_refused(host):
    (host["root"] / "dev/net/tun").unlink()
    r = _install(host)
    assert r.returncode == 1 and "/dev/net/tun" in r.stderr


def test_a_public_image_is_downloaded_and_one_built_from_source_must_be_loaded(host):
    _set_world(host, images=[], pullable=["ghcr.io/otodock/otodock-proxy:2.0.0"])
    assert _install(host).returncode == 0
    conf = (host["root"] / "var/lib/otodock-remote-compute/compute.conf").read_text()
    assert "PULLED=2.0.0" in conf.splitlines()
    shutil.rmtree(host["root"] / "var/lib/otodock-remote-compute")
    (host["tmp"] / "sent.json").unlink()
    _set_world(host, images=[], pullable=["otodock-compute-walk:a"])
    r = _install(host, image="otodock-compute-walk:a", public=False)
    assert r.returncode == 1 and "built from source, so load its image here first" in r.stderr
    assert not [a for a in _log(host, "docker.log") if a[:1] == ["pull"] and "otodock-compute-walk:a" in a]


def test_a_used_or_expired_command_says_so(host):
    r = _install(host, extra_env={"FAKE_CURL_CODE": "401"})
    assert r.returncode == 1 and "used or has expired" in r.stderr
    assert not (host["root"] / "var/lib/otodock-remote-compute").exists()
    r = _install(host, extra_env={"FAKE_CURL_CODE": "403", "FAKE_CURL_DETAIL": "A compute machine does not link from 127.0.0.1."})
    assert r.returncode == 1 and "does not link from 127.0.0.1" in r.stderr


def test_a_second_machine_on_one_server_is_refused_and_the_same_one_is_paired_again(host):
    assert _install(host).returncode == 0
    state = host["root"] / "var/lib/otodock-remote-compute"
    first = json.loads((state / "sign-in/credential").read_text())["machine_secret"]
    again = _install(host)
    assert again.returncode == 0 and "paired again" in again.stdout
    assert json.loads((state / "sign-in/credential").read_text())["machine_secret"] != first
    conf = state / "compute.conf"
    conf.write_text(conf.read_text().replace(f"MACHINE_ID={MACHINE}", "MACHINE_ID=compute-other"))
    r = _install(host)
    assert r.returncode == 1 and "already runs the compute machine compute-other" in r.stderr


@pytest.mark.parametrize("cap, kept", [("8g", "8g"), ("2048m", "2048m"), ("$(id)", ""), ("8g --privileged", "")])
def test_pairing_again_keeps_the_memory_cap(host, cap, kept):
    _installed(host, MEM_LIMIT=cap)
    assert _install(host).returncode == 0
    assert _conf(host)["MEM_LIMIT"] == kept


def test_the_tools_shape_is_the_platforms():
    """Raised alone, one side would read every server as behind for ever, or
    none ever."""
    from services.remote import compute_pairing
    shapes = re.findall(r"^HOST_SHAPE=([0-9]+)$", TOOL.read_text(), re.MULTILINE)
    assert shapes == [str(compute_pairing.HOST_SHAPE)]


def test_a_damaged_header_runs_nothing(host):
    script = _script().replace("VALUES_B64='", "VALUES_B64='" + base64.b64encode(b"IMAGE=x\n").decode() + "'\n#'", 1)
    r = subprocess.run(["bash"], input=script, env=host["env"], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 1 and "damaged" in r.stderr and not _log(host, "docker.log")
    # A download cut short defines functions and runs nothing.
    cut = _script()
    cut = cut[: cut.rindex("main ")]
    r = subprocess.run(["bash"], input=cut, env=host["env"], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0 and not _log(host, "docker.log") and not _log(host, "host.log")


def test_a_write_that_fails_after_the_pairing_says_so_and_starts_nothing(host):
    """A full disk or a read-only folder after the pairing: the install
    stops there, says so, and starts nothing."""
    r = _install(host, extra_env={"FAKE_INSTALL_FAIL": "/sign-in/credential"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "were not written, and it was not started" in r.stderr and "Started" not in r.stdout
    calls = _log(host, "host.log")
    assert not [c for c in calls if c.startswith("systemctl") and ("restart" in c or "enable" in c)]
    assert not (host["root"] / "etc/systemd/system/otodock-remote-compute.service").exists()


@pytest.mark.parametrize("images, free_gb, refused", [
    ([], 9, "has 9 GB free where Docker keeps its images. A compute machine needs about 10 GB"),
    ([], 11, ""),
    (["ghcr.io/otodock/otodock-proxy:2.0.0"], 4, "has 4 GB free where Docker keeps its images. A compute machine needs about 5 GB"),
    (["ghcr.io/otodock/otodock-proxy:2.0.0"], 6, ""),
])
def test_a_server_short_of_disk_is_refused_before_anything(host, images, free_gb, refused):
    _set_world(host, images=images, pullable=["ghcr.io/otodock/otodock-proxy:2.0.0"])
    r = _install(host, extra_env={"FAKE_DF_KB": str(free_gb * 1024 * 1024)})
    if refused:
        assert r.returncode == 1 and refused in r.stderr and "Nothing was changed." in r.stderr
        assert not [a for a in _log(host, "docker.log") if a[:1] == ["pull"]]
    else:
        assert r.returncode == 0, r.stderr


def test_an_https_install_pins_the_exchange_to_https(host):
    from unittest.mock import patch

    import config
    from services.remote import compute_pairing
    with patch.object(compute_pairing, "wanted_image",
                      lambda: compute_pairing.Image("ghcr.io/otodock/otodock-proxy", "2.0.0", True)), \
            patch.object(config, "DASHBOARD_PUBLIC_URL", "https://agents.example.com"):
        script = compute_pairing.bootstrap_script(MACHINE, TOKEN, "agents.example.com", VERSION).decode()
    r = subprocess.run(["bash"], input=script, env=host["env"], capture_output=True, text=True, timeout=60,
                       check=False)
    assert r.returncode == 0, r.stderr
    exchange = next(a for a in _log(host, "curl.log") if "--data-binary" in a)
    assert exchange[exchange.index("--proto") + 1] == "=https"
    assert exchange[-1] == "https://agents.example.com/v1/satellite/bootstrap"


# ---------------------------------------------------------------------------
# the host service
# ---------------------------------------------------------------------------

def _installed(host, **conf) -> Path:
    assert _install(host).returncode == 0
    path = host["root"] / "var/lib/otodock-remote-compute/compute.conf"
    lines = dict(line.split("=", 1) for line in path.read_text().splitlines())
    lines.update(conf)
    path.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
    return path


def test_the_machine_runs_with_the_options_the_compose_file_had_and_no_secret(host):
    _installed(host)
    r = _tool(host, "run")
    assert r.returncode == 0, r.stderr
    args = _world(host)["runs"][-1]
    state = host["root"] / "var/lib/otodock-remote-compute"
    expected = [
        "--user", "1000:1000", "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
        "--security-opt", "systempaths=unconfined", "--device", "/dev/net/tun", "--pids-limit", "32768",
        "--ulimit", "nofile=65536:524288", "--oom-score-adj", "250", "--stop-timeout", "120",
        "-e", "OTODOCK_COMPUTE_FILES=own", "-e", "OTODOCK_COMPUTE_MCPS_DIR=/opt/otodock/mcps",
        "-e", "OTODOCK_COMPUTE_IMAGE=ghcr.io/otodock/otodock-proxy:2.0.0",
        "-e", "OTODOCK_COMPUTE_HOST_SHAPE=1",
        "-v", "otodock-remote-compute-state:/opt/otodock/compute",
        "-v", "otodock-remote-compute-tools:/opt/otodock/mcps/community",
        "-v", f"{state}/sign-in:/opt/otodock/compute-link:ro",
        "--network", "otodock-remote-compute", "--label", f"io.otodock.remote-compute={MACHINE}",
    ]
    for i in range(0, len(expected), 2):
        pair = expected[i:i + 2]
        assert any(args[j:j + 2] == pair for j in range(len(args))), pair
    assert args[-3:] == ["ghcr.io/otodock/otodock-proxy:2.0.0", "/opt/otodock/satellite/venv/bin/python",
                         "/opt/otodock/proxy/compute_start.py"]
    secret = json.loads((state / "sign-in/credential").read_text())["machine_secret"]
    assert secret not in json.dumps(args) and "--privileged" not in args and "/var/run/docker.sock" not in json.dumps(args)
    assert not any(a.startswith(("-p", "--publish")) for a in args)
    # Its network and volumes carry its label; each new volume is the container user's.
    world = _world(host)
    assert world["networks"] == ["otodock-remote-compute"] and len(world["volumes"]) == 2
    assert any(line.startswith("chown 1000:1000 ") and "otodock-remote-compute-tools/_data" in line
               for line in _log(host, "host.log"))
    # Two volumes: the image's own interpreters under mcps/.uv-python are never covered,
    # and nothing the container writes reaches the host but those two and its state.
    assert not [a for a in args if ".uv-python" in a or "compute-word" in a or "WORD" in a]


def _conf(host) -> dict:
    path = host["root"] / "var/lib/otodock-remote-compute/compute.conf"
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _answers(host, code: int, body: dict) -> None:
    _set_world(host, host_answer={"code": code, "body": body})


def _asks(host) -> list:
    path = host["tmp"] / "asks.json"
    return json.loads(path.read_text()) if path.exists() else []


_IMAGE_201 = {"image": "ghcr.io/otodock/otodock-proxy:2.0.1", "satellite_version": "0.5.178"}


def test_a_new_tag_is_the_platforms_answer_asked_with_the_machines_sign_in(host):
    """The machine's 76 is a doorbell: the host asks the platform, the
    machine's id and secret on curl's input, and follows the tag it names."""
    _installed(host)
    _answers(host, 200, _IMAGE_201)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "76"}).returncode == 0
    conf = _conf(host)
    assert conf["WANTED_TAG"] == "2.0.1" and conf["WANTED_VERSION"] == "0.5.178"
    assert conf["REPOSITORY"] == "ghcr.io/otodock/otodock-proxy"
    ask = _asks(host)[-1]
    secret = json.loads((host["root"] / "var/lib/otodock-remote-compute/sign-in/credential").read_text())["machine_secret"]
    assert ask["stdin"] == f"X-Machine-Id: {MACHINE}\nX-Machine-Secret: {secret}\n"
    assert secret not in json.dumps(ask["args"]) and ask["args"][-1] == (
        "http://192.168.50.2:8400/v1/satellite/compute-host")
    assert ask["args"][ask["args"].index("--max-time") + 1] == "30"


@pytest.mark.parametrize("code, body", [
    (401, {"detail": "Invalid credentials"}),
    (404, _IMAGE_201),
    (500, _IMAGE_201),
    (200, {"image": "evil/otodock:1", "satellite_version": "0.5.178"}),
    (200, {"image": "ghcr.io/otodock/otodock-proxy:2.0.1 --privileged", "satellite_version": "0.5.178"}),
    (200, {"image": "ghcr.io/otodock/otodock-proxy:../x", "satellite_version": "0.5.178"}),
    (200, {"image": "ghcr.io/otodock/otodock-proxy:2.0.0", "satellite_version": "0.5.177"}),
    (200, {"image": "ghcr.io/otodock/otodock-proxy:2.0.1", "satellite_version": "a b"}),
    (200, {"image": ""}),
])
def test_anything_but_another_tag_of_its_own_image_changes_nothing(host, code, body):
    _installed(host)
    _answers(host, code, body)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "76"}).returncode == 0
    assert _conf(host)["WANTED_TAG"] == "" and _conf(host)["REPOSITORY"] == "ghcr.io/otodock/otodock-proxy"


def test_a_word_from_inside_the_container_is_never_followed(host):
    """Whatever the machine says, the host asks the platform: a 76 the
    platform does not confirm changes nothing, and no other exit is asked."""
    _installed(host)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "76"}).returncode == 0
    assert _conf(host)["WANTED_TAG"] == "" and len(_asks(host)) == 1
    for code in ("0", "1", "75", "137"):
        assert _tool(host, "after", extra_env={"EXIT_STATUS": code}).returncode == 0
    assert len(_asks(host)) == 1 and _conf(host)["WANTED_TAG"] == ""


def test_the_wanted_tag_runs_once_its_image_is_here_and_its_preflight_passes(host):
    _installed(host, WANTED_TAG="2.0.1", WANTED_VERSION="0.5.178", WANTED_SINCE="9999999999")
    _set_world(host, pullable=["ghcr.io/otodock/otodock-proxy:2.0.1"])
    assert _tool(host, "run").returncode == 0
    conf = _conf(host)
    assert conf["TAG"] == "2.0.1" and conf["PREVIOUS_TAG"] == "2.0.0" and conf["WANTED_TAG"] == ""
    assert conf["PULLED"] == "2.0.1" and int(conf["TRIAL_UNTIL"]) > 0
    look = next(a for a in _log(host, "docker.log") if a[:1] == ["run"] and "--check" in a
                and "ghcr.io/otodock/otodock-proxy:2.0.1" in a)
    assert look[-1] == "0.5.178" and "apparmor=unconfined" in look
    assert _world(host)["runs"][-1][-3] == "ghcr.io/otodock/otodock-proxy:2.0.1"
    # Each swap keeps one previous tag; the one before it goes when this
    # tool pulled it, and a tag that was on the server before stays.
    path = host["root"] / "var/lib/otodock-remote-compute/compute.conf"
    for wanted, previous in (("2.0.2", "2.0.1"), ("2.0.3", "2.0.2")):
        path.write_text(path.read_text().replace("WANTED_TAG=\n", f"WANTED_TAG={wanted}\n").replace(
            "WANTED_SINCE=0", "WANTED_SINCE=9999999999").replace("WANTED_VERSION=\n", "WANTED_VERSION=0.5.178\n"))
        _set_world(host, pullable=[f"ghcr.io/otodock/otodock-proxy:{wanted}"])
        assert _tool(host, "run").returncode == 0
        assert _conf(host)["TAG"] == wanted and _conf(host)["PREVIOUS_TAG"] == previous
    removed = _world(host).get("removed_images", [])
    assert removed == ["ghcr.io/otodock/otodock-proxy:2.0.1"]
    assert "ghcr.io/otodock/otodock-proxy:2.0.0" in _world(host)["images"]


def test_a_tag_whose_preflight_fails_is_not_switched_to(host):
    _installed(host, WANTED_TAG="2.0.1", WANTED_VERSION="0.5.178", WANTED_SINCE="9999999999")
    _set_world(host, images=["ghcr.io/otodock/otodock-proxy:2.0.0", "ghcr.io/otodock/otodock-proxy:2.0.1"],
               preflight_by_image={"ghcr.io/otodock/otodock-proxy:2.0.1": "bwrap is not installed"})
    r = _tool(host, "run")
    assert r.returncode == 0 and "cannot run here (bwrap is not installed)" in r.stdout
    conf = _conf(host)
    assert conf["TAG"] == "2.0.0" and conf["WANTED_TAG"] == "" and conf["AVOID_TAG"] == "2.0.1"
    assert _world(host)["runs"][-1][-3] == "ghcr.io/otodock/otodock-proxy:2.0.0"


def test_a_new_tag_that_fails_within_its_trial_goes_back_once(host):
    """It ends with anything but a doorbell or a stop within ten minutes of
    its start: back to the tag it ran, and the failed tag waits before it is
    tried again, so the old image signs in and hears the platform."""
    _installed(host, WANTED_TAG="2.0.1", WANTED_VERSION="0.5.178", WANTED_SINCE="9999999999")
    _set_world(host, pullable=["ghcr.io/otodock/otodock-proxy:2.0.1"])
    assert _tool(host, "run").returncode == 0 and _conf(host)["TAG"] == "2.0.1"
    r = _tool(host, "after", extra_env={"EXIT_STATUS": "1"})
    assert r.returncode == 0 and "goes back to 2.0.0" in r.stdout
    conf = _conf(host)
    assert conf["TAG"] == "2.0.0" and conf["PREVIOUS_TAG"] == "" and conf["AVOID_TAG"] == "2.0.1"
    assert _tool(host, "run").returncode == 0 and _world(host)["runs"][-1][-3] == "ghcr.io/otodock/otodock-proxy:2.0.0"
    # Once: a second failure has no tag to go back to.
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "1"}).returncode == 0 and _conf(host)["TAG"] == "2.0.0"
    # The platform names the failed tag again: the host waits it out.
    _answers(host, 200, _IMAGE_201)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "76"}).returncode == 0
    assert _conf(host)["WANTED_TAG"] == "2.0.1"
    r = _tool(host, "run", extra_env={"FAKE_SLEEP_LIMIT": "1"})
    assert "failed here: it is tried again" in r.stdout and _conf(host)["TAG"] == "2.0.0"


@pytest.mark.parametrize("code", ["0", "75", "76", "77"])
def test_a_doorbell_or_a_stop_ends_the_trial_with_no_return(host, code):
    _installed(host, PREVIOUS_TAG="2.0.0", TAG="2.0.1", TRIAL_UNTIL="9999999999")
    _set_world(host, images=["ghcr.io/otodock/otodock-proxy:2.0.0", "ghcr.io/otodock/otodock-proxy:2.0.1"])
    assert _tool(host, "after", extra_env={"EXIT_STATUS": code}).returncode == 0
    assert _conf(host)["TAG"] == "2.0.1" and _conf(host)["TRIAL_UNTIL"] == "0"


@pytest.mark.parametrize("code", ["125", "KILL", "TERM", ""])
def test_a_trial_never_counts_dockers_own_failure_or_a_signal(host, code):
    """125 is the docker client's own failure (and `run`'s while Docker is
    away), a name is a signal to the client: neither is the new tag's."""
    _installed(host, PREVIOUS_TAG="2.0.0", TAG="2.0.1", TRIAL_UNTIL="9999999999")
    r = _tool(host, "after", extra_env={"EXIT_STATUS": code})
    assert r.returncode == 0 and "goes back" not in r.stdout and _conf(host)["TAG"] == "2.0.1"


def test_run_while_docker_does_not_answer_ends_as_dockers_own_failure(host):
    _installed(host)
    _set_world(host, docker_down=True)
    before = len(_log(host, "docker.log"))
    r = _tool(host, "run")
    assert r.returncode == 125 and "Docker does not answer" in r.stdout
    assert not [a for a in _log(host, "docker.log")[before:] if a[:1] == ["run"]]


def test_a_look_at_an_image_that_never_answers_is_bounded(host):
    _installed(host, WANTED_TAG="2.0.1", WANTED_VERSION="0.5.178", WANTED_SINCE="9999999999")
    _set_world(host, images=["ghcr.io/otodock/otodock-proxy:2.0.0", "ghcr.io/otodock/otodock-proxy:2.0.1"],
               preflight_hang=True)
    r = _tool(host, "run", extra_env={"OTODOCK_COMPUTE_CHECK_S": "1"})
    assert r.returncode == 0 and "cannot run here (no answer)" in r.stdout
    conf = _conf(host)
    assert conf["TAG"] == "2.0.0" and conf["AVOID_TAG"] == "2.0.1"
    calls = _log(host, "docker.log")
    check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "--check" in a)
    assert "--name" in calls[check] and calls[check][calls[check].index("--name") + 1] == "otodock-remote-compute-check"
    assert ["rm", "-f", "otodock-remote-compute-check"] in calls[check + 1:]


def test_a_failure_after_the_trial_stays_on_its_tag(host):
    _installed(host, PREVIOUS_TAG="2.0.0", TAG="2.0.1", TRIAL_UNTIL="1")
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "1"}).returncode == 0
    assert _conf(host)["TAG"] == "2.0.1"


def test_a_tag_that_never_comes_gives_way_to_the_one_it_runs(host):
    _installed(host, WANTED_TAG="2.0.1", WANTED_VERSION="0.5.178", WANTED_SINCE="1")
    assert _tool(host, "run").returncode == 0
    assert _conf(host)["TAG"] == "2.0.0" and _conf(host)["WANTED_TAG"] == ""
    # Built from source: the server never downloads it.
    _installed(host, WANTED_TAG="b", WANTED_VERSION="0.5.178", WANTED_SINCE="1", IMAGE_PUBLIC="0")
    assert _tool(host, "run").returncode == 0
    assert not [a for a in _log(host, "docker.log") if a[:1] == ["pull"] and a[-1].endswith(":b")]


def test_a_removal_the_platform_confirms_removes_the_machine_and_only_its_own_objects(host):
    _installed(host)
    assert _tool(host, "run").returncode == 0
    _answers(host, 410, {"removed": True})
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "77"}).returncode == 0
    assert any(line.startswith("systemd-run --unit otodock-remote-compute-removal --collect --quiet ")
               and line.endswith(" remove --yes") for line in _log(host, "host.log"))
    _set_world(host, containers=["c-1"], images=_world(host)["images"] + ["nginx:alpine"])
    r = _tool(host, "remove", "--yes")
    assert r.returncode == 0, r.stderr
    world = _world(host)
    assert not world["containers"] and world["networks"] == []
    calls = _log(host, "docker.log")
    assert ["ps", "-aq", "--filter", "label=io.otodock.remote-compute"] in calls
    assert ["volume", "ls", "-q", "--filter", "label=io.otodock.remote-compute"] in calls
    calls = _log(host, "host.log")
    assert "systemctl disable --now otodock-remote-compute.service" in calls
    # systemd forgets the removed unit's last state too.
    assert "systemctl reset-failed otodock-remote-compute.service" in calls
    for gone in ("var/lib/otodock-remote-compute", "opt/otodock-remote-compute",
                 "etc/systemd/system/otodock-remote-compute.service"):
        assert not (host["root"] / gone).exists(), gone
    # A loaded or pulled-before image is not this tool's to remove.
    assert "nginx:alpine" in world["images"] and "ghcr.io/otodock/otodock-proxy:2.0.0" in world["images"]


@pytest.mark.parametrize("code", [200, 401, 404, 500])
def test_a_removal_the_platform_does_not_confirm_removes_nothing(host, code):
    _installed(host)
    _answers(host, code, {"image": "ghcr.io/otodock/otodock-proxy:2.0.0"})
    r = _tool(host, "after", extra_env={"EXIT_STATUS": "77"})
    assert r.returncode == 0 and "did not confirm a removal" in r.stdout
    assert not [line for line in _log(host, "host.log") if line.startswith("systemd-run")]


def _waited(host) -> list:
    path = host["tmp"] / "sleep.args"
    return path.read_text().split() if path.exists() else []


@pytest.mark.parametrize("ring, code, body", [
    ("76", 401, {"detail": "Invalid credentials"}),
    ("76", 200, {"image": "ghcr.io/otodock/otodock-proxy:2.0.0", "satellite_version": VERSION}),
    ("76", 200, {"image": "evil/otodock:1", "satellite_version": "0.5.178"}),
    ("77", 200, {"image": "ghcr.io/otodock/otodock-proxy:2.0.0"}),
    ("77", 500, {"detail": "x"}),
], ids=["tag-401", "tag-same", "tag-other-repository", "removal-200", "removal-500"])
def test_a_ring_the_platform_does_not_confirm_waits_a_minute_before_the_next_start(host, ring, code, body):
    """The unit starts the machine again after every exit, so a ring nobody
    confirms would spin; it waits a minute (inside the unit's stop timeout)."""
    _installed(host)
    _answers(host, code, body)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": ring}).returncode == 0
    assert "60" in _waited(host)


def test_a_ring_the_platform_confirms_does_not_wait(host):
    _installed(host)
    _answers(host, 200, _IMAGE_201)
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "76"}).returncode == 0
    _answers(host, 410, {"removed": True})
    assert _tool(host, "after", extra_env={"EXIT_STATUS": "77"}).returncode == 0
    assert "60" not in _waited(host)


@pytest.mark.parametrize("failure", ["unreachable", "timeout", "no-secret"])
def test_a_removal_ring_with_no_answer_removes_nothing(host, failure):
    _installed(host)
    if failure == "no-secret":
        (host["root"] / "var/lib/otodock-remote-compute/sign-in/credential").write_text(
            json.dumps({"machine_id": MACHINE}) + "\n")
    else:
        _set_world(host, host_curl={"exit": 7, "out": "\n000"} if failure == "unreachable" else {"exit": 28, "out": ""})
    r = _tool(host, "after", extra_env={"EXIT_STATUS": "77"})
    assert r.returncode == 0 and "did not confirm a removal (answer " in r.stdout
    assert "(answer )" not in r.stdout
    assert not [line for line in _log(host, "host.log") if line.startswith("systemd-run")]
    assert len(_asks(host)) == (0 if failure == "no-secret" else 1)
    assert (host["root"] / "var/lib/otodock-remote-compute/compute.conf").exists()


@pytest.mark.parametrize("code", ["0", "1", "75", "137", ""])
def test_no_exit_but_the_removal_doorbell_removes_anything(host, code):
    """Even a platform that would confirm: no exit status but 77 asks it."""
    _installed(host)
    _answers(host, 410, {"removed": True})
    assert _tool(host, "after", extra_env={"EXIT_STATUS": code}).returncode == 0
    assert not [line for line in _log(host, "host.log") if line.startswith("systemd-run")]
    assert not _asks(host)


def test_a_removal_stops_when_docker_does_not_answer(host):
    _installed(host)
    _set_world(host, docker_down=True)
    r = _tool(host, "remove", "--yes")
    assert r.returncode == 1 and "Docker does not answer. Start it and run the removal again. Nothing was removed." in r.stderr
    assert (host["root"] / "var/lib/otodock-remote-compute/compute.conf").exists()
    assert not [line for line in _log(host, "host.log") if line.startswith("systemctl disable")]


def test_the_removal_refuses_without_its_configuration_or_root_and_touches_nothing(host):
    r = _tool(host, "remove", "--yes")
    assert r.returncode == 1 and "Nothing was touched." in r.stderr
    _installed(host)
    r = _tool(host, "remove", "--yes", extra_env={"FAKE_UID": "1000"})
    assert r.returncode == 1 and "Nothing was touched." in r.stderr
    assert (host["root"] / "var/lib/otodock-remote-compute/compute.conf").exists()


def test_a_configuration_line_is_read_never_run(host, tmp_path):
    path = _installed(host)
    planted = tmp_path / "planted"
    path.write_text(path.read_text() + f"$(touch {planted})\nREPOSITORY=evil/x;touch {planted}\n")
    assert _tool(host, "run").returncode == 0
    assert not planted.exists()
    assert _world(host)["runs"][-1][-3] == "ghcr.io/otodock/otodock-proxy:2.0.0"


def test_the_two_scripts_name_the_same_objects_and_stay_under_the_cap():
    for script in (INSTALL, TOOL):
        text = script.read_text()
        assert 'NAME="otodock-remote-compute"' in text and 'STATE="$ROOT/var/lib/$NAME"' in text
        assert len(text.splitlines()) <= 600
    if shutil.which("shellcheck"):
        done = subprocess.run(["shellcheck", str(INSTALL), str(TOOL)], capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stdout
