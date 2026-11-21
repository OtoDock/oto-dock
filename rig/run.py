#!/usr/bin/env python3
"""The rig's runner: drives a running OtoDock install as a person and as an
outside program, and says pass or fail.

    python3 rig/run.py --base-url http://<host>:8400 --run-dir <folder> --layer scripted

It reads the scenarios of the layer under ``scenarios/``, ``live/`` and
``claims/`` in file order, writes ``report-<layer>.json`` and
``report-<layer>.md`` in the run folder, and exits 0 when the layer passed, 1
when it did not, 2 on wrong usage. On a host without the rig's marker file the
scenarios that install or restart something do not run: the wizard above all,
since a fresh install is exactly when anyone may take its first account.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import inspect
import json
import os
import pwd
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib import report as report_lib
from lib.chat import TurnFailed
from lib.person import ApiError, Person
from lib.scenario import (
    CARRIED,
    FAIL,
    FIXED,
    INCONCLUSIVE,
    KNOWN,
    NOT_SUPPORTED,
    PASS,
    REGISTRY,
    SKIPPED,
    CheckFailed,
    Inconclusive,
    NotSupported,
)

SCRIPTED_KEY = "rig-scripted-key-0000000000000000"
SCRIPTED_MODEL = "scripted"
CHEAP_CLAUDE = "claude-haiku-5-5"
ROOM_GB = 3.0
ROOM_WAIT_S = 240
# The rig's own machinery (docker, compose, a backup) waits longer on a disk under
# pressure, by the share of the time the disk stalls work, up to this much. A wait for
# what a person sees (a chat that goes on, a page) never scales.
SLOW_MAX = 4.0
IO_PRESSURE = Path("/proc/pressure/io")
# What a row may change on the install and the runner puts back after it, pass or fail.
KEPT_FILES = (".env", "docker-compose.yml", "docker-compose.phone.yml", "docker-compose.override.yml")
# A failed row's evidence: the last lines of each log since the row started.
EVIDENCE_LINES = 600
SATELLITE_UNIT = "oto-dock-satellite"
# The rig's install, set by install/fresh.sh and the engines scenario.
IDLE_TIMEOUT_S = 60
SESSIONS_PER_PERSON = 64
# engine word -> (the engine's id, the scripted layer's model)
# The server's own code in its container, and one edit of it for a broken form.
PROXY_CODE = "/opt/otodock/proxy/"
PROXY_PY = "/opt/otodock/proxy/venv/bin/python"
_BREAK_ONE_LINE = (
    "import base64,json,pathlib,sys;"
    "path,old,new=json.loads(base64.b64decode(sys.argv[1]));"
    "p=pathlib.Path(path);s=p.read_text();"
    "assert s.count(old)==1,path;"
    "p.write_text(s.replace(old,new))"
)
ENGINES = {
    "claude": ("claude-code-cli", CHEAP_CLAUDE),
    "codex": ("codex-cli", SCRIPTED_MODEL),
    "direct": ("direct-llm", SCRIPTED_MODEL),
}


def model_for(ctx, word: str) -> str:
    """The model a chat on an engine asks for: the scripted layer's, or with
    real accounts on this host (the live layer's ``live-accounts``) the
    cheapest one the engine lists, the last by tier, on a provider one of
    them serves. An engine lists the models of every provider it can run, and
    the scripted model has no account once the real ones are connected: a
    chat on either is refused."""
    engine, scripted = ENGINES[word]
    if not ctx.state.get("real_models"):
        return scripted
    rows = ctx.state.get("capabilities", {}).get(engine, {}).get("models", [])
    served = ctx.state.get("live_providers", {}).get(engine)
    tiered = [m for m in rows if m.get("value") and m.get("tier") and m.get("value") != SCRIPTED_MODEL
              and (not served or m.get("provider") in served)]
    tiered.sort(key=lambda m: (m["tier"], m.get("pricing_input") or 0))
    return tiered[-1]["value"] if tiered else scripted


def engine_of(ctx, word: str) -> tuple[str, str]:
    """The engine's id and the model a probe opens its chat with (``model_for``)."""
    return ENGINES[word][0], model_for(ctx, word)


def rig_home() -> Path:
    """This user's home by the password database: a fake HOME isolates nothing from
    a service manager or a container engine, so nothing here reads HOME."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def host_is_a_rig() -> bool:
    """The marker in the real home holds this host's name and machine id, so a
    copied file or a cloned VM does not pass. Under a test nothing is a rig."""
    if os.environ.get("RIG_TEST_FAKES"):
        return False
    try:
        marker = (rig_home() / ".otodock-rig" / "this-host-is-a-rig").read_text().split()
        machine_id = Path("/etc/machine-id").read_text().strip()
    except OSError:
        return False
    return marker == [os.uname().nodename, machine_id]


@dataclass
class Context:
    base: str
    run_dir: Path
    layer: str
    models_url: str = ""
    models_log: Path | None = None
    install_dir: Path = field(default_factory=lambda: rig_home() / "otodock")
    state_dir: Path | None = None
    is_rig: bool = False
    upgrade_target: str = ""
    candidate_dir: str = ""
    machine_user: str = ""
    agent: str = "personal-assistant"
    secrets: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    slow: float = 1.0

    def __post_init__(self):
        self.people: dict[str, Person] = {}
        self.raw_dir = self.run_dir / "raw"
        self.state_dir = self.state_dir or self.run_dir

    def require_rig(self) -> None:
        """Everything that acts on this host passes here first."""
        if not self.is_rig:
            raise CheckFailed("this host has no rig marker: nothing on it is touched")

    # -- the run's own accounts -----------------------------------------------
    def password(self, who: str) -> str:
        path = self.state_dir / "secrets.json"
        if not self.secrets and path.exists():
            self.secrets = json.loads(path.read_text())
        if who not in self.secrets:
            self.secrets[who] = secrets.token_urlsafe(18)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self.secrets, handle)
        return self.secrets[who]

    def email(self, who: str) -> str:
        return f"{who}@rig.invalid"

    def person(self, who: str) -> Person:
        if who not in self.people:
            self.people[who] = Person(self.base, self.email(who), self.password(who)).sign_in()
        return self.people[who]

    @property
    def owner(self) -> Person:
        return self.person("owner")

    def owner_signs_in(self) -> bool:
        """The install still knows its owner: a sign-in with the run's own password.
        False when the server does not answer or refuses it."""
        try:
            Person(self.base, self.email("owner"), self.password("owner")).sign_in()
        except (ApiError, OSError):
            return False
        return True

    # -- this host ------------------------------------------------------------
    def machinery(self, seconds: float) -> float:
        """A bound on the rig's own machinery, grown by the disk's pressure."""
        return seconds * self.slow

    def compose(self, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
        self.require_rig()
        return subprocess.run(["docker", "compose", *args], cwd=self.install_dir, capture_output=True,
                              text=True, timeout=self.machinery(timeout), check=False)

    def proxy_exec(self, command: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        return self.compose("exec", "-T", "otodock-proxy", "sh", "-c", command, timeout=timeout)

    def sql(self, query: str) -> str:
        """One statement in the install's database: its rows as plain lines."""
        done = self.compose("exec", "-T", "otodock-postgres", "psql", "-U", "otodock", "-d", "otodock", "-Atc", query,
                            timeout=60)
        if done.returncode != 0:
            raise CheckFailed(f"psql exited {done.returncode}: {done.stderr[-300:]}")
        return done.stdout.strip()

    def script(self, name: str, *args: str, env: dict | None = None,
               timeout: float = 600.0) -> subprocess.CompletedProcess:
        """One of the candidate's operator scripts (backup.sh, restore.sh) as a person
        runs it: copied into the install's folder and run there, asking nothing."""
        self.require_rig()
        shutil.copy(Path(self.candidate_dir) / "scripts" / name, self.install_dir / name)
        return subprocess.run(["bash", name, *args], cwd=self.install_dir, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=self.machinery(timeout), check=False,
                              env={**os.environ, **(env or {})})

    def docker(self, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        """The docker command on the rig, outside compose (a volume)."""
        self.require_rig()
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=self.machinery(timeout),
                              check=False)

    def admin(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        """The admin command as a person runs it: the candidate's script, in the install's folder."""
        self.require_rig()
        shutil.copy(Path(self.candidate_dir) / "scripts" / "otodock-admin.sh", self.install_dir / "otodock-admin.sh")
        return subprocess.run(["bash", "otodock-admin.sh", *args], cwd=self.install_dir, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=self.machinery(timeout), check=False)

    def break_product(self, path: str, old: str, new: str) -> None:
        """A broken form's wrong behaviour, in the product and not in the script: one
        edit of the server's own code in its container (``path`` under the proxy
        folder), loaded by its next start. ``mend_product`` recreates the container
        from its image."""
        edit = base64.b64encode(json.dumps([PROXY_CODE + path, old, new]).encode()).decode()
        done = self.proxy_exec(f"{PROXY_PY} -c {shlex.quote(_BREAK_ONE_LINE)} {edit}", timeout=60)
        if done.returncode != 0:
            raise CheckFailed(f"the broken form's edit of {path} failed: {done.stderr[-300:]}")

    def mend_product(self) -> None:
        done = self.compose("up", "-d", "--force-recreate", "--no-deps", "otodock-proxy", timeout=300)
        if done.returncode != 0:
            raise CheckFailed(f"the container did not come back from its image: {done.stderr[-300:]}")

    def env_value(self, key: str) -> str:
        for line in (self.install_dir / ".env").read_text().splitlines():
            if line.startswith(f"{key}="):
                return shlex.split(line.split("=", 1)[1] or "''")[0] if line.split("=", 1)[1] else ""
        return ""

    def model_calls(self) -> list[dict]:
        if not self.models_log or not self.models_log.exists():
            return []
        return [json.loads(ln) for ln in self.models_log.read_text().splitlines() if ln.strip()]


def mem_available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def wait_for_room(ctx: Context) -> bool:
    """A machine out of memory swaps through the disk every other VM shares, so a run
    waits for idle chats to end before a scenario, and stops when they do not."""
    if not ctx.is_rig:
        return True
    deadline = time.monotonic() + ROOM_WAIT_S
    while mem_available_gb() < ROOM_GB:
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)
    return True


def io_pressure(path: Path = IO_PRESSURE) -> float:
    """The share of the last minute in which some work waited on the disk, in percent."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith("some "):
                return float(dict(part.split("=", 1) for part in line.split()[1:])["avg60"])
    except (OSError, KeyError, ValueError):
        pass
    return 0.0


def slowness(pressure: float) -> float:
    """How much longer the rig's own machinery may take: a disk that stalls work half
    the time doubles it, never past SLOW_MAX."""
    stalled = min(max(pressure, 0.0), 99.0) / 100
    return round(min(SLOW_MAX, max(1.0, 1 / (1 - stalled))), 2)


def kept_files(ctx: Context) -> dict[str, bytes | None]:
    """The install's files a row may change, as they are before it."""
    out: dict[str, bytes | None] = {}
    for name in KEPT_FILES:
        try:
            out[name] = (ctx.install_dir / name).read_bytes()
        except FileNotFoundError:
            out[name] = None
    return out


def put_back(ctx: Context, before: dict[str, bytes | None]) -> list[str]:
    """The files a row left changed, put back as they were, and the install started
    again from them: a row that failed half way never hands the next rows a broken
    install. The names it put back."""
    now = kept_files(ctx)
    changed = [name for name, data in before.items() if now.get(name) != data]
    for name in changed:
        path = ctx.install_dir / name
        data = before[name]
        if data is None:
            path.unlink(missing_ok=True)
            continue
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        path.write_bytes(data)
    if changed:
        done = ctx.compose("up", "-d", "--remove-orphans", timeout=900)
        if done.returncode != 0:
            print(f"    the install did not start from its files put back: {done.stderr[-300:]}", flush=True)
        _answers_again(ctx)
    return changed


def _answers_again(ctx: Context) -> None:
    from lib.person import Http
    deadline = time.monotonic() + ctx.machinery(300)
    while time.monotonic() < deadline:
        try:
            status, body = Http(ctx.base).call("GET", "/health", timeout=5)
            if status == 200 and (body or {}).get("status") == "ok":
                break
        except OSError:
            pass
        time.sleep(5)
    ctx.people.clear()


def redact(text: str, values: list[str]) -> str:
    """``text`` with each value and every secret line the run's scan knows taken out:
    the one way the rig keeps a secret out of what it saves."""
    from lib import secret_scan
    for value in values:
        text = text.replace(value, "[redacted]")
    data = text.encode()
    for _what, pattern in secret_scan.LINES:
        data = pattern.sub(b"[redacted]", data)
    return data.decode(errors="replace")


def _secret_values(ctx: Context) -> list[str]:
    """Every value the run's secret scan looks for, the longest first."""
    from lib import secret_scan
    values = list(secret_scan.env_secrets(ctx.install_dir / ".env").values())
    if ctx.state_dir:
        values += secret_scan.run_secrets(ctx.state_dir).values()
    return sorted(values, key=len, reverse=True)


def write_evidence(ctx: Context, name: str, part: str, text: str) -> str:
    """One piece of a failed row's evidence, ``evidence/<name>-<part>.log``, its last
    lines with every secret the run's scan looks for taken out. The file written."""
    folder = ctx.run_dir / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [line[:2000] for line in text.splitlines()[-EVIDENCE_LINES:]]
    path = folder / f"{name}-{part}.log"
    path.write_text(redact("\n".join(lines) + "\n", _secret_values(ctx)))
    return str(path.relative_to(ctx.run_dir))


def save_evidence(ctx: Context, name: str, since: float) -> list[str]:
    """A failed row's own minutes of the server's and the compute machine's logs and
    of the paired machine's journal, into the run folder, with every secret the run's
    scan looks for taken out: a container made again later in the run takes its log
    with it, so a row that makes one again saves them itself first. The files written."""
    start = since - 5
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
    written = []
    for part, read in (
            ("proxy", lambda: ctx.compose("logs", "--no-color", "--timestamps", "--since", stamp, "otodock-proxy",
                                          timeout=120)),
            ("compute", lambda: ctx.compose("logs", "--no-color", "--timestamps", "--since", stamp, "otodock-compute",
                                            timeout=120)),
            ("machine", lambda: subprocess.run(
                ["journalctl", "--user", "-u", SATELLITE_UNIT, "--since", f"@{int(start)}", "--no-pager", "-o",
                 "short-iso"], capture_output=True, text=True, timeout=ctx.machinery(60), check=False))):
        try:
            done = read()
        except (OSError, subprocess.TimeoutExpired) as exc:
            text = f"not read: {type(exc).__name__}: {exc}\n"
        else:
            text = done.stdout or done.stderr
        written.append(write_evidence(ctx, name, part, text))
    return written


def load_scenarios(layer: str) -> None:
    folders = {"scripted": ["scenarios", "claims"], "live": ["live", "claims"], "upgrade": ["upgrade"]}[layer]
    for folder in folders:
        for path in sorted((HERE / folder).glob("[a-z0-9]*.py")):
            importlib.import_module(f"{folder}.{path.stem}")


def passed_key(layer: str, name: str) -> str:
    """What passed is kept per layer: a claim runs in every layer it is listed in."""
    return f"{layer}:{name}"


def decide(item, layer: str, passed: set[str], only: set[str], is_rig: bool) -> tuple[str, str]:
    """What to do with one scenario of a layer, from what passed on this install:
    ``("run", "")``, ``(CARRIED, why)`` or ``(SKIPPED, why)``. A need is met by a
    pass in this layer, or in the scripted layer the others build on."""
    if not only and passed_key(layer, item.name) in passed:
        return CARRIED, "passed on this install, at this build, in an earlier run"
    if item.needs_rig and not is_rig:
        return SKIPPED, "this host has no rig marker"
    missing = [n for n in item.needs
               if passed_key(layer, n) not in passed and passed_key("scripted", n) not in passed]
    if missing:
        return SKIPPED, f"needs {', '.join(missing)}"
    return "run", ""


def under_broken(item, layer: str) -> tuple[bool, str]:
    """What a broken run does with one scenario: (run its broken form, why it is
    skipped). A scenario with a broken form runs it. One with none is skipped, except
    in the upgrade layer: each upgrade run installs the old release fresh, so nothing
    is carried, and its steps with no broken form (``old-*``, ``upgrade-script``) run
    as written for the checks after the update to be reached."""
    if "broken" in inspect.signature(item.func).parameters:
        return True, ""
    return False, ("" if layer == "upgrade" else "has no broken form")


def record(passed: set[str], layer: str, name: str, result: str) -> None:
    """What a later run may carry: a pass, a fixed bug, what the engine does not
    support. A known failure runs again every time."""
    key = passed_key(layer, name)
    if result in (PASS, FIXED, NOT_SUPPORTED):
        passed.add(key)
    else:
        passed.discard(key)


def run_one(item, ctx: Context, *, broken: bool = False) -> dict:
    """``broken`` runs the check against a setup made wrong on purpose (its
    ``broken`` form): it must fail, which proves it can."""
    t0 = time.monotonic()
    detail = ""
    attempts = 1 + item.retries
    result = FAIL
    for attempt in range(attempts):
        try:
            if item.machine and ctx.is_rig:
                from scenarios.s50_machine import machine_ready
                machine_ready(ctx)
            detail = (item.func(ctx, broken=True) if broken else item.func(ctx)) or ""
            result = PASS
            break
        except NotSupported as exc:
            result, detail = NOT_SUPPORTED, str(exc)
            break
        except Inconclusive as exc:
            result, detail = INCONCLUSIVE, str(exc)
        except (CheckFailed, ApiError, TurnFailed, TimeoutError) as exc:
            result, detail = FAIL, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 (a scenario's bug is a failure of that scenario, never of the run)
            result, detail = FAIL, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        if attempt + 1 < attempts:
            detail = ""
    if item.known:
        if result == PASS:
            result = FIXED
        elif result == FAIL and item.known_match in detail:
            result = KNOWN
        detail = f"{item.known} {detail}".strip()
    if item.changes_with and result == PASS:
        detail = f"the current behaviour, which changes when {item.changes_with}. {detail}".strip()
    return {"name": item.name, "result": result, "seconds": round(time.monotonic() - t0), "detail": detail,
            "about": item.about}


def wait_ready(ctx: Context) -> None:
    """The server started again and its start's hold over: ``/health/ready`` answers 200
    (a turn sent before is refused "OtoDock is starting"). The one wait every row that
    starts the server again uses before it sends a turn."""
    from lib.person import Http

    def ready():
        try:
            status, _body = Http(ctx.base).call("GET", "/health/ready", timeout=5)
        except OSError:
            return None
        return status == 200 or None
    Person(ctx.base, "", "").wait_until("the proxy back and its start's hold over", ready,
                                        timeout=ctx.machinery(900), every=5)
    ctx.people.clear()


def install_lost(ctx: Context) -> bool:
    """After a row on a rig whose owner signed in before: the owner cannot sign in
    within the rig's bound, and still cannot once the install was started again from
    its files. A row that just restarted the server is given the bound first."""
    from lib.person import Http

    def answers() -> bool:
        try:
            status, body = Http(ctx.base).call("GET", "/health", timeout=5)
        except OSError:
            return False
        return status == 200 and (body or {}).get("status") == "ok"

    def signs_in(seconds: float) -> bool:
        # A sign-in is tried only once the server answers: each refused one counts
        # against the address's attempts.
        deadline = time.monotonic() + seconds
        while True:
            if answers() and ctx.owner_signs_in():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(10)
    if signs_in(ctx.machinery(120)):
        return False
    print("    the owner cannot sign in: the install is started again from its files", flush=True)
    done = ctx.compose("up", "-d", "--remove-orphans", timeout=900)
    if done.returncode != 0:
        print(f"    the install did not start: {done.stderr[-300:]}", flush=True)
    _answers_again(ctx)
    return not signs_in(ctx.machinery(120))


def run_row(item, ctx: Context, *, broken: bool = False) -> dict:
    """One row as the runner runs it on a rig: its machinery's bounds grown by the
    disk's pressure, the install's files put back after it whatever it did, and a
    failure's logs kept in the run folder."""
    started = time.time()
    if ctx.is_rig:
        ctx.slow = slowness(io_pressure())
        if ctx.slow > 1:
            print(f"    the disk stalls work: the rig's own waits are {ctx.slow:g} times as long", flush=True)
    before = kept_files(ctx) if ctx.is_rig and ctx.layer != "upgrade" else None
    outcome = run_one(item, ctx, broken=broken)
    notes = []
    if before is not None:
        put = put_back(ctx, before)
        if put:
            notes.append(f"the row left {', '.join(put)} changed: put back")
    if ctx.is_rig and outcome["result"] in (FAIL, INCONCLUSIVE):
        saved = save_evidence(ctx, item.name, started)
        notes.append(f"logs: {', '.join(saved)}")
    if notes:
        outcome["detail"] = f"{outcome['detail']} ({'; '.join(notes)})".strip()
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--layer", default="scripted", choices=["scripted", "live", "upgrade"])
    parser.add_argument("--only", default="", help="comma-separated scenario names")
    parser.add_argument("--list", action="store_true", help="print the layer's scenarios and exit")
    parser.add_argument("--models-url", default="")
    parser.add_argument("--models-log", default="")
    parser.add_argument("--install-dir", default=str(rig_home() / "otodock"))
    parser.add_argument("--state-dir", default="", help="the install's accounts and what passed on it; default on a rig: ~/.otodock-rig/install")
    parser.add_argument("--upgrade-target", default="")
    parser.add_argument("--candidate-dir", default="")
    parser.add_argument("--broken", action="store_true",
                        help="run the checks that have a broken form against a setup made wrong on purpose")
    args = parser.parse_args(argv)

    load_scenarios(args.layer)
    items = REGISTRY.layer(args.layer) + (REGISTRY.layer("claims") if args.layer in ("scripted", "live") else [])
    if args.list:
        for item in items:
            print(f"{item.name}: {item.about}")
        return 0
    only = {w for w in args.only.split(",") if w}
    unknown = only - {i.name for i in items}
    if unknown:
        print(f"no such scenario: {sorted(unknown)}", file=sys.stderr)
        return 2

    is_rig = host_is_a_rig()
    state_dir = Path(args.state_dir) if args.state_dir else (rig_home() / ".otodock-rig" / "install" if is_rig else None)
    ctx = Context(base=args.base_url.rstrip("/"), run_dir=Path(args.run_dir), layer=args.layer,
                  models_url=args.models_url, models_log=Path(args.models_log) if args.models_log else None,
                  install_dir=Path(args.install_dir), is_rig=is_rig, state_dir=state_dir,
                  upgrade_target=args.upgrade_target, candidate_dir=args.candidate_dir)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = ctx.state_dir / "state.json"
    if state_file.exists():
        ctx.state = json.loads(state_file.read_text())
    started = time.time()
    results: list[dict] = []
    passed: set[str] = set(ctx.state.get("passed", []))
    # A new build of the install: what passed on the old one says nothing about it.
    build = _build(ctx)
    if build and build != "no answer" and ctx.state.get("build") != build:
        if passed:
            print(f"the install's build is {build}, not {ctx.state.get('build')}: what passed before is cleared", flush=True)
        passed.clear()
        ctx.state["build"] = build
    # Whether the owner signed in on this install: from then on a row after which the
    # owner cannot is a lost install, and the run ends there.
    had_owner = ctx.is_rig and ctx.owner_signs_in()
    lost_at = ""
    for item in items:
        if only and item.name not in only:
            continue
        if lost_at:
            results.append({"name": item.name, "result": SKIPPED, "seconds": 0, "about": item.about,
                            "detail": f"not run: the install was lost at {lost_at}"})
            continue
        action, why = decide(item, args.layer, passed, only, ctx.is_rig)
        run_broken, skip = under_broken(item, args.layer) if args.broken else (False, "")
        if skip:
            action, why = SKIPPED, skip
        if action != "run":
            results.append({"name": item.name, "result": action, "seconds": 0, "about": item.about, "detail": why})
            continue
        if not wait_for_room(ctx):
            print(f"under {ROOM_GB} GB of memory for {ROOM_WAIT_S} s: stopping before {item.name}", flush=True)
            results.append({"name": item.name, "result": FAIL, "seconds": ROOM_WAIT_S, "about": item.about,
                            "detail": f"this machine stayed under {ROOM_GB} GB of memory: the run stopped here"})
            break
        print(f"--- {item.name}", flush=True)
        outcome = run_row(item, ctx, broken=run_broken)
        print(f"    {outcome['result']} in {outcome['seconds']} s {str(outcome['detail'])[:400]}", flush=True)
        results.append(outcome)
        if not run_broken:
            record(passed, args.layer, item.name, outcome["result"])
        state_file.write_text(json.dumps({**ctx.state, "passed": sorted(passed)}))
        if ctx.is_rig:
            if had_owner and install_lost(ctx):
                lost_at = item.name
                print(f"the install was lost at {item.name}: the rows after it do not run", flush=True)
            had_owner = had_owner or ctx.owner_signs_in()
    summary = report_lib.write_report(ctx.run_dir, layer=args.layer, results=results, started=started,
                                      context={"base": ctx.base, "build": _health(ctx), "proxy image": _build(ctx),
                                               "models": "real" if ctx.state.get("real_models") else "scripted-only",
                                               **departures(ctx)}, lost_at=lost_at)
    print(f"{args.layer}: {summary['verdict']} (release: {summary['release']}) {summary['counts']} in {summary['seconds']} s", flush=True)
    return 0 if summary["verdict"] == PASS else 1


def departures(ctx: Context) -> dict:
    """What this install does that a person's does not, for the report's header."""
    cap = ""
    with contextlib.suppress(OSError):
        cap = (ctx.run_dir / "io-cap.log").read_text().strip().splitlines()[-1]
    out = {"io cap on the container engine": cap or "not read"}
    if ctx.is_rig:
        out.update({
            "session idle timeout": f"{IDLE_TIMEOUT_S} s (admin setting)",
            "sessions per person": f"{SESSIONS_PER_PERSON} (OTODOCK_MAX_SESSIONS_PER_USER)",
            "Claude Code": "CLAUDE_BIN names the rig's wrapper (a rig key goes to the scripted server)",
            "memory": f"each scenario waits for {ROOM_GB:g} GB free",
            "slow disk": f"the rig's own docker and compose waits grow with the disk's pressure, up to {SLOW_MAX:g} times",
        })
    return out


def _build(ctx: Context) -> str:
    """What the install that is up runs. On a rig, its proxy container's image: the
    build ``/health`` names is the dashboard's, and a change to the proxy alone keeps
    it. Elsewhere, that build."""
    if ctx.is_rig:
        done = ctx.compose("images", "-q", "otodock-proxy", timeout=60)
        image = done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else ""
        if image:
            return image.removeprefix("sha256:")[:16]
    return _health(ctx)


def _health(ctx: Context) -> str:
    from lib.person import Http
    try:
        _, body = Http(ctx.base).call("GET", "/health", timeout=10)
        return str((body or {}).get("build", ""))
    except OSError:
        return "no answer"


if __name__ == "__main__":
    sys.exit(main())
