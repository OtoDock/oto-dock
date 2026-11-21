"""The compute machine's start: the compose service's command and the
bare-metal unit's ``ExecStart``.

It imports no module of the proxy (the proxy's configuration writes its own
secrets into a file it finds, and on bare metal that file is out of reach):
1. waits for the credential the proxy writes at its boot
   (``OTODOCK_COMPUTE_LINK_DIR/credential``, ``services/remote/compute_bootstrap``),
   with no limit: a first boot builds the database first;
2. refuses an agents folder whose marker is not the credential's (an override
   file or a unit that mounts another folder than the proxy's), reading the
   credential again every few seconds until the two agree: after a restore
   onto a new host it starts before the proxy writes the new one;
3. refuses a community tools folder it cannot write (``community_problem``);
4. runs the sandbox's preflight (``sessions/sandbox_host.preflight``) and
   refuses with its reason, which names the cause and its fix where the
   machine can tell (the tap device, a kernel switch on user namespaces);
   a pasta older than this release names is said and never refused;
5. writes ``satellite.conf`` (``kind = compute``, ``tools = own``) and becomes
   the satellite, which takes the stop signal itself.

The bundled tools' environments come with the image (``proxy/Dockerfile``) or,
on bare metal, from the build an install and an update run in the same
checkout: this start builds nothing. The community tools are this machine's own (``tools = own``):
their folder, ``mcps/community``, is a volume of its own (on bare metal a
folder the unit binds there), and the satellite builds each tool the platform
sends it in its sandbox.

On another server (``OTODOCK_COMPUTE_FILES=own``) the machine keeps its own
copy of the folders: the credential is the one its host service wrote when the
server was paired (``scripts/remote-compute-install.sh``), no marker is checked
(the copy is not the platform's folder), its agents and tools folders are made
here, and the configuration says ``files = own``. The platform syncs the copy
and installs the tools a session needs into its tools folder, as on a paired
machine. ``--check <satellite version>`` is the install's one look before it
pairs: the satellite's version and the sandbox's preflight, ``ok`` or why not.

A refusal writes ``refused: <reason>`` into the status file the healthcheck
reads and waits for the stop signal: the container reads unhealthy with the
reason, never a restart loop. ``running`` is written just before the
satellite takes over. The satellite ends with exit code 75 when the platform
refuses the credential's secret or no longer has its row, so the restart runs
this start again and reads the credential the proxy wrote. On another server
nobody writes it again: the satellite leaves the refused sign-in's mark
(``compute-refused``), and the start says so and waits until the machine is
paired again, trying one sign-in every hour (a removal while it waits reaches
it then).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".oto-dock"
STATUS = STATE / "compute-status"
#: The refused sign-in's mark the satellite leaves on another host
#: (``satellite/transport/compute_link.REFUSED_FILE``).
REFUSED = STATE / "compute-refused"
CONF = STATE / "satellite.conf"
MARKER = ".otodock-platform"
_POLL_S = 2.0
#: How often a refused marker reads the credential and the marker again.
_REREAD_S = 5.0
#: How long a refused sign-in waits before it tries the platform once more.
_RETRY_REFUSED_S = 3600.0
_PREFLIGHT = ("import sys\n"
              "from satellite._vendored import sandbox_render\n"
              "from satellite.config import load_config\n"
              "from satellite.sessions import sandbox_host\n"
              "reason = sandbox_host.preflight(load_config(__import__('pathlib').Path(sys.argv[1])))\n"
              "print(reason or sandbox_render.pasta_floor_note(sys.argv[2]))\n"
              "sys.exit(1 if reason else 0)\n")


def say(line: str) -> None:
    print(f"compute: {line}", flush=True)


def _status(text: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_name(".compute-status.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, STATUS)


def _stop(_signum, _frame) -> None:
    say("stopped")
    sys.exit(0)


def refuse(reason: str) -> None:
    """Say why, keep the reason for the healthcheck, wait for the stop."""
    say(f"refused: {reason}")
    _status(f"refused: {reason}")
    while True:
        signal.pause()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _pasta_floor() -> str:
    """``PASST_MIN_VERSION`` of this release's VERSIONS.md ("" when it
    cannot be read): the pasta the launcher's options are written against."""
    try:
        text = (ROOT / "VERSIONS.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*PASST_MIN_VERSION\s*=\s*([^\s#]+)\s*$", text, re.MULTILINE)
    return m.group(1) if m else ""


def wait_for_credential(folder: Path, *, own_copy: bool = False) -> dict:
    """The sign-in file once it holds a sign-in: beside the platform the
    proxy writes it at its boot; on another server (``own_copy``) its host
    service wrote it at the pairing."""
    path = folder / "credential"
    said = False
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("machine_id") and data.get("machine_secret"):
                return data
        except (OSError, ValueError):
            pass
        if not said:
            if own_copy:
                say(f"waiting for the sign-in file {path}. Pair the machine again from its card.")
                _status("waiting for the sign-in file")
            else:
                say(f"waiting for the platform to write {path}")
                _status("waiting for the platform")
            said = True
        time.sleep(_POLL_S)


def wait_for_agreement(link: Path, agents: Path) -> dict:
    """The credential once its marker is the agents folder's: a refusal says
    why once and reads both again until they agree, or the stop."""
    said = ""
    while True:
        credential = wait_for_credential(link)
        problem = marker_problem(agents, str(credential.get("marker") or ""))
        if not problem:
            return credential
        if problem != said:
            say(f"refused: {problem}")
            _status(f"refused: {problem}")
            said = problem
        time.sleep(_REREAD_S)


def sign_in_mark(credential: dict) -> str:
    """The mark the satellite leaves of a refused sign-in
    (``satellite/transport/compute_link.sign_in_mark``): one formula."""
    text = f"{credential.get('machine_id')}\n{credential.get('machine_secret')}"
    return hashlib.sha256(text.encode()).hexdigest()


def wait_for_new_sign_in(link: Path, credential: dict) -> dict:
    """On another server, a credential the platform refused waits until the
    machine is paired again (the old one would only be refused again), and
    tries one sign-in every hour, so a removal while it waits still reaches
    it; the mark of another sign-in holds nothing."""
    try:
        refused = REFUSED.read_text(encoding="utf-8").strip()
    except OSError:
        return credential
    said = False
    waited = 0.0
    while sign_in_mark(credential) == refused:
        # Every pass: a wait for the file in between (written again) wrote its own.
        _status("refused: the platform refused this sign-in. Pair it again from its card.")
        if not said:
            say("refused: the platform refused this sign-in. Pair it again from its card.")
            said = True
        if waited >= _RETRY_REFUSED_S:
            say("trying the sign-in once more")
            return credential
        time.sleep(_REREAD_S)
        waited += _REREAD_S
        credential = wait_for_credential(link, own_copy=True)
    REFUSED.unlink(missing_ok=True)
    return credential


def marker_problem(agents: Path, marker: str) -> str:
    """Why the agents folder is not the one the proxy writes, "" when it is."""
    try:
        held = (agents / MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        held = ""
    if not held or held != marker:
        return ("the agents folder is not the platform's. Mount the same agents folder as the "
                "proxy (a compose override file that moves it for otodock-proxy must move it "
                "for otodock-compute too)")
    return ""


def community_problem(mcps: Path) -> str:
    """Why this machine cannot keep the community tools in
    ``mcps/community``, "" when it can: the folder is made when it is
    missing, and one that cannot be written (a compose file that still
    mounts the proxy's volume there read-only, a unit without the bind) is
    refused, never worked around."""
    community = mcps / "community"
    with contextlib.suppress(OSError):
        community.mkdir(parents=True, exist_ok=True)
    if community.is_dir() and os.access(community, os.W_OK | os.X_OK):
        return ""
    return (f"the community tools folder cannot be written: {community}. With Docker, use this release's "
            "docker-compose.yml: it mounts the volume otodock-compute-tools there for otodock-compute. "
            "Without Docker, run scripts/dev-setup.sh --service again.")


def satellite_url(base: str) -> str:
    """The platform's address (``http://host:port``) as the satellite's link
    (``ws://host:port/v1/satellite``); a link given whole is kept."""
    scheme, _, rest = base.strip().partition("://")
    scheme = {"http": "ws", "https": "wss"}.get(scheme.lower(), scheme.lower())
    rest = rest.rstrip("/")
    if "/" not in rest:
        rest += "/v1/satellite"
    return f"{scheme}://{rest}"


def write_conf(credential: dict, *, agents: Path, mcps: Path, platform_url: str,
               own_copy: bool = False, own_tools: bool = False) -> None:
    lines = [
        "[satellite]",
        f"machine_id = {credential['machine_id']}",
        f"machine_secret = {credential['machine_secret']}",
        f"platform_url = {satellite_url(platform_url)}",
        f"agents_dir = {agents}",
        f"mcps_dir = {mcps}",
        "kind = compute",
    ]
    if own_copy:
        lines.append("files = own")
    elif own_tools:
        # The install's own machine keeps the community tools itself. One
        # with its own copy keeps every folder, tools among them.
        lines.append("tools = own")
    # The engines' binaries: the ones .env names for the proxy (CLAUDE_BIN,
    # CODEX_BIN, passed by the compose file), else the image's own.
    for section, key, binary in (("cli", "claude_bin", "claude"), ("codex", "codex_bin", "codex")):
        found = _env(f"{binary.upper()}_BIN") or shutil.which(binary)
        if found:
            lines += ["", f"[{section}]", f"{key} = {found}"]
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_name(".satellite.conf.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, CONF)


_CHECK = ("import sys\n"
          "from satellite.config import SATELLITE_VERSION, load_config\n"
          "from satellite.sessions import sandbox_host\n"
          "if SATELLITE_VERSION != sys.argv[2]:\n"
          "    print(f'this image runs satellite {SATELLITE_VERSION}, the platform runs {sys.argv[2]}')\n"
          "    sys.exit(1)\n"
          "reason = sandbox_host.preflight(load_config(__import__('pathlib').Path(sys.argv[1])))\n"
          "print(reason or 'ok')\n"
          "sys.exit(1 if reason else 0)\n")


def check(version: str) -> int:
    """The install's look before it pairs: this image's satellite is the
    platform's ``version`` and the sandbox runs here. Writes only under
    HOME (a scratch one in the install's container), signs in to nothing."""
    scratch = STATE / "check"
    agents = scratch / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    mcps = Path(_env("OTODOCK_COMPUTE_MCPS_DIR", str(ROOT / "mcps")))
    write_conf({"machine_id": "check", "machine_secret": "check"}, agents=agents, mcps=mcps,
               platform_url="http://127.0.0.1", own_copy=True)
    satellite = ROOT / "satellite" / "venv" / "bin" / "python"
    done = subprocess.run([str(satellite), "-c", _CHECK, str(CONF), version], cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, check=False)
    print((done.stdout.strip().splitlines() or [done.stderr.strip()[-300:] or f"exit {done.returncode}"])[-1],
          flush=True)
    return 0 if done.returncode == 0 else 1


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2]))
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    link = _env("OTODOCK_COMPUTE_LINK_DIR")
    platform_url = _env("OTODOCK_COMPUTE_PLATFORM_URL")
    if not link or not platform_url:
        refuse("OTODOCK_COMPUTE_LINK_DIR and OTODOCK_COMPUTE_PLATFORM_URL must be set")
    agents = Path(_env("OTODOCK_COMPUTE_AGENTS_DIR", str(ROOT / "agents")))
    mcps = Path(_env("OTODOCK_COMPUTE_MCPS_DIR", str(ROOT / "mcps")))
    own_copy = _env("OTODOCK_COMPUTE_FILES").lower() == "own"
    if own_copy:
        credential = wait_for_new_sign_in(Path(link), wait_for_credential(Path(link), own_copy=True))
        agents.mkdir(parents=True, exist_ok=True)
        mcps.mkdir(parents=True, exist_ok=True)
    else:
        credential = wait_for_agreement(Path(link), agents)
        problem = community_problem(mcps)
        if problem:
            refuse(problem)
    write_conf(credential, agents=agents, mcps=mcps, platform_url=platform_url, own_copy=own_copy,
               own_tools=not own_copy)
    satellite = ROOT / "satellite" / "venv" / "bin" / "python"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    checked = subprocess.run([str(satellite), "-c", _PREFLIGHT, str(CONF), _pasta_floor()], cwd=ROOT, env=env,
                             capture_output=True, text=True, check=False)
    if checked.returncode != 0:
        refuse("this machine cannot run the platform's sandbox: "
               + (checked.stdout.strip() or checked.stderr.strip()[-300:] or f"exit {checked.returncode}"))
    if checked.stdout.strip():
        # An older pasta than this release names: said, never refused.
        say(checked.stdout.strip())
    _status("running")
    say("signing in")
    os.chdir(ROOT)
    # The program is the image's own interpreter and its arguments are fixed. The
    # environment is this service's own, set by the compose file, plus the code's
    # root. Nothing a session or a person wrote reaches the call (reviewed 2026-11-19).
    os.execve(str(satellite), [str(satellite), "-m", "satellite", "--config", str(CONF)], env)  # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args


if __name__ == "__main__":
    main()
