"""The compute machine: the install's own compute service runs every Claude Code and
Codex session and pre-warm, a restart of it loses no turn, admission refuses on it with
today's words, and a restore onto volumes of a new host starts sessions there."""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from lib.chat import ChatSession, TurnFailed, assistant_text, script
from lib.scenario import CheckFailed, check, scenario
from run import ENGINES

from scenarios.s55_continuity import (
    BROKEN_WAIT_S,
    CONTINUE_WAIT_S,
    HOLD,
    _continued,
    _cut_cards,
    _cut_in_flight,
    _gone_on,
    _healthy,
)

# The words a refusal for memory carries today, on the platform and on a compute machine.
LOW_MEMORY = "This server is low on memory:"


def _signed_in(ctx, wait_s: int = 300) -> str:
    done = ctx.admin("compute", "--wait", str(wait_s), timeout=wait_s + 60)
    check(done.returncode == 0, f"the compute machine did not sign in: {done.stdout} {done.stderr}")
    return done.stdout.strip()


# Each process's first two arguments, read from /proc (the image has no ps): the
# program and, for an interpreter, its script; a launcher's inline code never matches.
_PROCS = ("for p in /proc/[0-9]*; do { tr '\\0' '\\n' < $p/cmdline; } 2>/dev/null | head -2 | tr '\\n' ' '; echo; done"
          " | grep -E '(^|/)(claude|codex)(\\.js)?( |$)' || true")


def _engine_procs(ctx, service: str) -> str:
    """The engines' processes inside a service's container (the scripted Claude Code
    wrapper and Codex), one line each."""
    done = ctx.compose("exec", "-T", service, "sh", "-c", _PROCS, timeout=60)
    return done.stdout


@scenario("compute-sessions", needs=("engines",), needs_rig=True)
def compute_sessions(ctx, broken: bool = False):
    """The compute machine signs in, and a Claude Code chat, a Codex chat and a new
    chat's pre-warm run on it, never in the proxy's container. Broken: a session is
    expected in the proxy."""
    _signed_in(ctx)
    seen: dict[str, tuple[str, str]] = {}
    for word in ("claude", "codex"):
        engine, model = ENGINES[word]
        chat = ChatSession(ctx.owner, agent=ctx.agent)
        try:
            chat.open(engine=engine, model=model, text="Wait a little. " + script(
                {"tool": "shell", "input": {"command": 'python3 -c "import time; time.sleep(20)"'}},
                {"text": "Done waiting."}))
            procs = ("", "")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                chat.next_frame(timeout=1)
                procs = (_engine_procs(ctx, "otodock-compute"), _engine_procs(ctx, "otodock-proxy"))
                if procs[0]:
                    break
            seen[word] = procs
            if not any(f.get("type") == "done" for f in chat.frames):
                chat.wait_done()
            check("Done waiting." in assistant_text(chat.rows()), f"{word}: the turn did not end")
        finally:
            chat.close()
    machines = ctx.owner.ok("GET", "/v1/admin/remote-machines", timeout=10)["machines"]
    own = [m for m in machines if m.get("pairing_scope") == "compute" and (m.get("compute") or {}).get("own")]
    check(len(own) == 1 and own[0].get("status") == "online", f"the install's compute machine: {own}")
    for word, (on_compute, in_proxy) in seen.items():
        if broken:
            check(bool(in_proxy), f"{word}: no session in the proxy, as a broken form expects")
        check(bool(on_compute), f"{word}: no engine ran on the compute machine")
        check(not in_proxy, f"{word}: an engine ran in the proxy: {in_proxy[:300]}")
    # A new chat's pre-warm: built through the builders, so on the compute machine.
    engine, model = ENGINES["claude"]
    warm = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        warm.pre_warm(engine=engine, model=model)
        ctx.owner.wait_until("a pre-warm on the compute machine",
                             lambda: bool(_engine_procs(ctx, "otodock-compute")), timeout=120, every=2)
        check(not _engine_procs(ctx, "otodock-proxy"), "the pre-warm ran in the proxy")
    finally:
        warm.close()
    return f"both engines and a pre-warm ran on {own[0]['name']}"


@scenario("compute-restart", needs=("turn-claude", "turn-codex"), needs_rig=True)
def compute_restart(ctx, broken: bool = False):
    """The compute service restarted while the proxy runs: each engine's turn it was
    running goes on by itself after its return, said in the chat, and the proxy never
    stopped. Broken: the compute service stays stopped."""
    _signed_in(ctx)
    answer = "The answer after the compute machine came back."
    cut = {word: _cut_in_flight(ctx, word, answer=answer) for word in ("claude", "codex")}
    started = ctx.compose("ps", "-q", "otodock-proxy").stdout.strip()
    done = ctx.compose("stop" if broken else "restart", "otodock-compute", timeout=240)
    check(done.returncode == 0, f"the compute service's restart failed: {done.stderr[-300:]}")
    try:
        if not broken:
            _signed_in(ctx)
        for word, (chat_id, _tag, _path) in cut.items():
            rows = _gone_on(ctx, word, chat_id, answer, BROKEN_WAIT_S if broken else CONTINUE_WAIT_S)
            check(len(_continued(rows)) == 1, f"{word}: one continue: {len(_continued(rows))}")
            check(not _cut_cards(rows), f"{word}: no card asks the person to send again")
            check(assistant_text(rows).count(answer) == 1, f"{word}: the answer once")
        check(ctx.compose("ps", "-q", "otodock-proxy").stdout.strip() == started, "the proxy was restarted")
    finally:
        if broken:
            ctx.compose("start", "otodock-compute", timeout=240)
    return "both engines' cut turns went on after the compute machine's restart"


def _set_env(ctx, key: str, value: str | None) -> None:
    env = ctx.install_dir / ".env"
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    env.write_text("\n".join(lines) + "\n")


@scenario("compute-admission", needs=("engines",), needs_rig=True)
def compute_admission(ctx, broken: bool = False):
    """Short of memory on the compute machine (a floor no host has free), a second chat
    is refused with today's words; the first one is the empty box's last-resort admit.
    Broken: the second chat is expected to start."""
    engine, model = ENGINES["claude"]
    _set_env(ctx, "SESSION_RESERVE_FLOOR_MB", "100000000")
    first = second = None
    try:
        done = ctx.compose("restart", "otodock-proxy", timeout=240)
        check(done.returncode == 0, f"the restart failed: {done.stderr[-300:]}")
        _healthy(ctx)
        _signed_in(ctx)
        first = ChatSession(ctx.owner, agent=ctx.agent)
        first.open(engine=engine, model=model, text="Wait. " + script(
            {"tool": "shell", "input": {"command": HOLD}}, {"text": "Done."}))
        ctx.owner.wait_until("the first chat on the compute machine",
                             lambda: bool(_engine_procs(ctx, "otodock-compute")), timeout=120, every=2)
        second = ChatSession(ctx.owner, agent=ctx.agent)
        refused = ""
        try:
            second.open(engine=engine, model=model, text="Hello. " + script({"text": "Hi."}))
            second.wait_done(timeout=120)
        except TurnFailed as e:
            refused = str(e)
        if broken:
            check(not refused, f"the second chat was refused: {refused[:200]}")
        check(LOW_MEMORY in refused and "Free some memory on the server, then try again." in refused,
              f"the refusal's words: {refused[:300]!r}")
    finally:
        for chat in (first, second):
            if chat is not None:
                chat.close()
        _set_env(ctx, "SESSION_RESERVE_FLOOR_MB", None)
        ctx.compose("restart", "otodock-proxy", timeout=240)
        _healthy(ctx)
    return "refused on the compute machine with today's words"


def _restore(ctx, run_dir, *, new_host: bool, stale_secret: bool = False, broken: bool = False) -> None:
    """Stop, restore and start again: onto the volumes a new host has (no compute
    state, no credential, no tools built), or onto this host's with a database whose sign-in no
    longer verifies the kept credential. Broken: the compute service left stopped."""
    ctx.compose("stop", "otodock-proxy", "otodock-compute", timeout=300)
    if new_host:
        for volume in ("otodock-compute", "otodock-compute-link", "otodock-compute-tools"):
            ctx.docker("volume", "rm", volume)
    restored = ctx.script("restore.sh", str(run_dir), "--yes", timeout=1800)
    check(restored.returncode == 0, f"restore.sh failed: {restored.stderr[-500:]}")
    if stale_secret:
        ctx.sql("UPDATE remote_machines SET machine_secret_hash = 'x' WHERE pairing_scope = 'compute'")
    start_as_a_person(ctx, "the start after the restore")
    if broken:
        ctx.compose("stop", "otodock-compute", timeout=300)
    _healthy(ctx)
    _signed_in(ctx, wait_s=60 if broken else 600)
    engine, model = ENGINES["claude"]
    chat = ChatSession(ctx.owner, agent=ctx.agent)
    try:
        chat.open(engine=engine, model=model, text="Hello. " + script({"text": "Back on compute."}))
        chat.wait_done()
        check("Back on compute." in assistant_text(chat.rows()), "no answer after the restore")
    finally:
        chat.close()


@scenario("compute-restore", needs=("engines",), needs_rig=True)
def compute_restore(ctx, broken: bool = False):
    """A backup, then a restore refused while the compute service runs; a restore onto
    the volumes a new host has, after which the install mints the compute machine's
    sign-in again and a chat runs on it; and a restore onto this host whose database's
    sign-in no longer verifies the kept one, after which the compute machine reads the
    new one and a chat runs. Broken: the compute service left stopped after the restore."""
    # The backup holds the install's .env: never in the run folder, which leaves the rig.
    out = Path(tempfile.mkdtemp(prefix="compute-restore-"))
    try:
        done = ctx.script("backup.sh", env={"OTODOCK_BACKUP_DIR": str(out)}, timeout=1800)
        check(done.returncode == 0, f"backup.sh failed: {done.stderr[-500:]}")
        check("stopping the compute service for the backup" in done.stderr,
              "the backup did not stop the compute service")
        _signed_in(ctx)
        run_dir = next(p for p in sorted(out.iterdir()) if p.is_dir())
        refused = ctx.script("restore.sh", str(run_dir), "--yes", timeout=600)
        check(refused.returncode == 1, "a restore ran while the platform ran")
        _restore(ctx, run_dir, new_host=True, broken=broken)
        _restore(ctx, run_dir, new_host=False, stale_secret=True)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return "restored onto a new host's volumes and onto this host's with a stale sign-in: each time the compute machine signed in and a chat ran"


@contextlib.contextmanager
def _machine_away(ctx):
    """The rig's paired machine kept off while the install runs on an empty database:
    on another host it would still reach the old one. A machine that reaches a platform
    which does not know it uninstalls itself, as the product means it to."""
    from scenarios.s50_machine import UNIT, _installed
    stopped = ctx.is_rig and _installed() and subprocess.run(
        ["systemctl", "--user", "stop", UNIT], capture_output=True, timeout=ctx.machinery(60), check=False).returncode == 0
    try:
        yield
    finally:
        if stopped:
            subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True, timeout=ctx.machinery(60),
                           check=False)


# How long the proxy image's own health check gives a first start before compose calls
# it unhealthy (its HEALTHCHECK: a start period of 300 s, then 6 checks 10 s apart).
HEALTH_ALLOWS_S = 300 + 6 * 10


def _answers_after(ctx, seconds: float) -> float | None:
    """Seconds until the server answers ``/health`` ok, or None past ``seconds``."""
    from lib.person import Http
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            status, body = Http(ctx.base).call("GET", "/health", timeout=5)
            if status == 200 and (body or {}).get("status") == "ok":
                return time.monotonic() - started
        except OSError:
            pass
        time.sleep(5)
    return None


def start_as_a_person(ctx, what: str) -> str:
    """``up -d`` as a person runs it on a slow disk: a first start whose server is still
    in its first migrations when its health check gives up says "dependency failed to
    start"; the person waits for the server and runs it once more. What it took."""
    started = time.monotonic()
    up = ctx.compose("up", "-d", timeout=900)
    if up.returncode == 0:
        return f"{what} took {time.monotonic() - started:.0f} s"
    waited = _answers_after(ctx, ctx.machinery(900))
    if waited is None:
        raise CheckFailed(f"{what} failed, and the server did not answer within {ctx.machinery(900):.0f} s: "
                          f"{up.stderr[-300:]}")
    answered = time.monotonic() - started
    again = ctx.compose("up", "-d", timeout=900)
    check(again.returncode == 0, f"{what} failed twice: {again.stderr[-300:]}")
    return (f"{what} needed a second up -d: the server answered after {answered:.0f} s, past the "
            f"{HEALTH_ALLOWS_S} s its own health check allows")


def _put_back(ctx, run_dir) -> None:
    """The install restored from the row's own backup, started, and its owner signing in."""
    ctx.compose("stop", "otodock-proxy", "otodock-compute", timeout=300)
    restored = ctx.script("restore.sh", str(run_dir), "--yes", timeout=1800)
    check(restored.returncode == 0, f"restore.sh failed: {restored.stderr[-300:]}")
    start_as_a_person(ctx, "the start after the put-back")
    ctx.people.clear()
    deadline = time.monotonic() + ctx.machinery(300)
    while not ctx.owner_signs_in():
        check(time.monotonic() < deadline, "the owner cannot sign in after the put-back")
        time.sleep(10)


@scenario("compute-restore-fresh-host", needs=("engines",), needs_rig=True)
def compute_restore_fresh_host(ctx, broken: bool = False):
    """The documented restore onto a new host (docs.otodock.io, Backup & restore, "Moving to a new server"):
    a backup, every volume gone, `up -d` (the new host's own database, compute row
    and sign-in), the platform stopped, `restore.sh`, `up -d`. The compute service
    starts with the new host's sign-in while the proxy writes the restored
    install's: it waits for the two to agree, signs in and a chat runs on it.
    Broken: the compute service left stopped after the restore."""
    out = Path(tempfile.mkdtemp(prefix="compute-fresh-host-"))
    try:
        done = ctx.script("backup.sh", env={"OTODOCK_BACKUP_DIR": str(out)}, timeout=1800)
        check(done.returncode == 0, f"backup.sh failed: {done.stderr[-500:]}")
        run_dir = next(p for p in sorted(out.iterdir()) if p.is_dir())
        try:
            with _machine_away(ctx):
                down = ctx.compose("down", "-v", timeout=600)
                check(down.returncode == 0, f"down -v failed: {down.stderr[-300:]}")
                first = start_as_a_person(ctx, "the new host's first start")
                _healthy(ctx)
                _signed_in(ctx, wait_s=900)
                stop = ctx.compose("stop", "otodock-proxy", "otodock-compute", timeout=300)
                check(stop.returncode == 0, f"the stop failed: {stop.stderr[-300:]}")
                restored = ctx.script("restore.sh", str(run_dir), "--yes", timeout=1800)
                check(restored.returncode == 0, f"restore.sh failed: {restored.stderr[-500:]}")
            after = start_as_a_person(ctx, "the start after the restore")
            if broken:
                ctx.compose("stop", "otodock-compute", timeout=300)
            ctx.people.clear()
            _healthy(ctx)
            said = _signed_in(ctx, wait_s=60 if broken else 600)
            check(said == "The compute machine is signed in.", f"chats run on the platform: {said!r}")
            engine, model = ENGINES["claude"]
            chat = ChatSession(ctx.owner, agent=ctx.agent)
            try:
                chat.open(engine=engine, model=model, text="Hello. " + script({"text": "Back on compute."}))
                chat.wait_done()
                check("Back on compute." in assistant_text(chat.rows()), "no answer after the restore")
                machines = ctx.owner.ok("GET", "/v1/admin/remote-machines", timeout=10)["machines"]
                own = [m for m in machines if (m.get("compute") or {}).get("own")]
                check(len(own) == 1 and own[0]["compute"]["sessions"] >= 1,
                      f"the chat's session is not on the compute machine: {own}")
            finally:
                chat.close()
        except BaseException as failed:
            # Whatever happened after the volumes went, the run's next rows need the
            # install back: from the row's own backup, with the machine kept away
            # until it holds the machine again.
            if not ctx.owner_signs_in():
                with _machine_away(ctx):
                    try:
                        _put_back(ctx, run_dir)
                    except Exception as put:  # noqa: BLE001 (both failures are the row's detail)
                        raise CheckFailed(f"{failed} The install could not be put back from the row's backup: "
                                          f"{put}") from failed
            raise
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return (f"the documented restore onto a new host: the compute machine waited for the restored sign-in and a "
            f"chat ran ({first}, {after})")

