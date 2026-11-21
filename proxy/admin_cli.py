"""The admin command line: the platform's break-glass tools, run beside the
server (``scripts/otodock-admin.sh`` runs it in the proxy container or the
source install's venv).

    admin_cli.py user reset-password <email> [--password-stdin]
    admin_cli.py user disable-2fa <email> [--passkeys]
    admin_cli.py support-info
    admin_cli.py jobs list
    admin_cli.py jobs run <job>
    admin_cli.py drain [--bound SECONDS] [--away SECONDS] [--quiet] [--probe]
    admin_cli.py resume
    admin_cli.py status
    admin_cli.py compute [--wait SECONDS]

It reads the configuration and the database the server reads, through the
same store functions the dashboard's routes call; it never migrates the
database. A command that changes something first names the config file
and the database it is about to change, and writes a log line to stderr
and the platform's log file: the dashboard's line for a password reset,
the same words with the command line as the actor for 2FA and passkeys.
``drain``, ``resume`` and ``status`` ask the running server instead, on
its own address with the master key, and open no database.
Exit status: 0 done, 1 refused, 2 usage; for the server's commands also 3 when
no server runs and 4 when it is older than 2.0.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("otodock")

DONE, REFUSED, USAGE = 0, 1, 2
# The server's commands only (``drain``, ``resume``, ``status``): nothing
# answers on its address, and a server older than 2.0 (the route is unknown).
NOT_RUNNING, OLDER = 3, 4

# A config value is printed only when its key names none of these.
_SECRET_WORDS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "PASS", "PIN", "CREDENTIAL",
                 "PRIVATE", "SALT", "COOKIE", "DSN")
_YES_NO = frozenset({"true", "false", "yes", "no", "on", "off"})
# The provider a password account carries (the dashboard reset's rule), and
# the passkey mode in which a passkey is the account's second step.
LOCAL_ACCOUNTS = "local"
PASSKEY_SECOND_STEP = "second_factor"
# A port, a size, a timeout: never a phone number or an account number.
_SHORT_NUMBER = re.compile(r"^-?\d{1,8}(\.\d+)?$")


class Refused(Exception):
    """A command that cannot do what was asked: the message says why."""


class Unanswered(Refused):
    """The running server could not be asked: ``code`` is the exit status
    that says why (``REFUSED``, ``NOT_RUNNING``, ``OLDER``)."""

    def __init__(self, message: str, code: int = REFUSED):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# The configuration, before anything reads it
# ---------------------------------------------------------------------------

def config_file_present() -> bool:
    """Whether the configuration config.py would read exists. Importing it
    without one writes new secrets into a fresh config.env, creates
    directories and dials the default local database: a command line run
    from the wrong folder must stop before that."""
    root = Path(__file__).resolve().parent.parent
    config_dir = Path(os.environ.get("PLATFORM_CONFIG_DIR") or root)
    return (config_dir / "config.env").is_file() or bool(os.environ.get("DATABASE_URL"))


# ---------------------------------------------------------------------------
# The audit line
# ---------------------------------------------------------------------------

def actor() -> str:
    """Who ran the command: the launcher names the host's user and machine;
    run directly, this process's own."""
    named = os.environ.get("OTODOCK_ADMIN_ACTOR", "").strip()
    if named:
        return f"command line {named}"
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = str(os.getuid())
    return f"command line {user}@{socket.gethostname()}"


def audit(message: str, *args: object) -> None:
    """The dashboard's log line for the same change: on stderr, appended to
    the platform's log file when it exists (never created here: a file this
    process creates could be one the server cannot open at its next boot),
    and in a container on the server's own stderr, so the container's log
    shows it too."""
    import config
    from core import log_queue

    logger.info(message, *args)
    record = logging.LogRecord("otodock", logging.INFO, __file__, 0, message, args, None)
    line = logging.Formatter(log_queue._FORMAT).format(record) + "\n"
    targets = [config.BASE_DIR / "proxy.log"]
    if config.RUNNING_IN_DOCKER:
        targets.append(Path("/proc/1/fd/2"))
    for target in targets:
        if not target.exists():
            continue
        try:
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            print(f"admin_cli: the log line could not be written to {target} ({exc})",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------

def _local_user(email: str) -> dict:
    from storage import database as task_store
    user = task_store.get_user_by_email(email.strip().lower())
    if not user:
        raise Refused(f"no account has the email {email}")
    return user


def _read_new_password(stdin) -> str:
    if stdin.isatty():
        first = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != first:
            raise Refused("the two passwords differ")
        return first
    return stdin.readline().rstrip("\r\n")


def reset_password(email: str, *, from_stdin: bool, stdin=sys.stdin, out=sys.stdout) -> int:
    """The dashboard's reset (a temporary password the person changes at
    the next sign-in), or a password read from stdin and set as given."""
    from auth.password import (
        check_password_strength,
        generate_temp_password,
        hash_password,
    )
    from auth.providers import mask_email
    from storage import database as task_store

    user = _local_user(email)
    if not str(user.get("auth_provider") or "").startswith(LOCAL_ACCOUNTS):
        raise Refused(f"{email} signs in through single sign-on: its password is the identity "
                      "provider's to reset")
    if from_stdin:
        password = _read_new_password(stdin)
        ok, problem, _score = check_password_strength(password)
        if not ok:
            raise Refused(problem or "that password is too weak")
    else:
        password = generate_temp_password()
    task_store.set_user_password(user["sub"], hash_password(password))
    if not from_stdin:
        task_store.update_user_auth_fields(user["sub"], must_change_password=True)
    audit("Admin %s reset password for %s", actor(), mask_email(user["email"]))
    if not from_stdin:
        print(f"Temporary password for {user['email']}: {password}", file=out)
        print("They choose a new one at their next sign-in.", file=out)
    else:
        print(f"The password of {user['email']} is set.", file=out)
    print("The running platform refuses their older sign-ins and session tokens within a "
          "minute; their open chats stop at their next tool call. A restart ends them at once.",
          file=out)
    return DONE


def disable_2fa(email: str, *, passkeys: bool, out=sys.stdout) -> int:
    """Turn the account's authenticator-app code off (the self-service
    route's change) and, with ``passkeys``, delete its passkeys."""
    from api.auth.webauthn import passkey_login_mode, passkeys_enabled
    from auth.providers import mask_email
    from storage import database as task_store
    from storage.identity import webauthn_store

    user = _local_user(email)
    sub = user["sub"]
    did = []
    # An enrolment never confirmed leaves a secret with the code off: it gates
    # no sign-in, so it is not this command's to clear.
    if user.get("totp_enabled"):
        task_store.update_user_auth_fields(sub, totp_secret_enc=None, totp_recovery_enc=None,
                                           totp_enabled=False)
        audit("Admin %s disabled 2FA for %s", actor(), mask_email(user["email"]))
        did.append("the authenticator code is off")
    keys = webauthn_store.list_credentials(sub)
    if passkeys and keys:
        removed = sum(1 for key in keys if webauthn_store.delete_credential(sub, key["credential_id"]))
        audit("Admin %s removed %d passkey(s) for %s", actor(), removed, mask_email(user["email"]))
        did.append(f"{removed} passkey(s) removed")
        keys = []
    print(f"{user['email']}: " + ("; ".join(did) if did else "nothing to turn off") + ".",
          file=out)
    require_2fa = task_store.get_platform_setting("require_2fa") == "1"
    if keys and passkeys_enabled() and (passkey_login_mode() == PASSKEY_SECOND_STEP or require_2fa):
        print(f"Their {len(keys)} passkey(s) still ask for a second step at sign-in: run this "
              "again with --passkeys to remove them.", file=out)
    if require_2fa:
        print("This platform requires a second factor: they set one up again after signing in.",
              file=out)
    return DONE


# ---------------------------------------------------------------------------
# support-info
# ---------------------------------------------------------------------------

def shown_value(key: str, value: str) -> str:
    """What support-info prints for a config value: yes/no words, short
    numbers and a URL's scheme, host and port; ``set (hidden)`` for anything else, and for
    every key whose name says it holds a secret. A URL is printed only when
    it parses cleanly: a password holding ``#``, ``/`` or ``?`` moves the
    split points, so a URL with an ``@`` anywhere is never printed."""
    if any(word in key.upper() for word in _SECRET_WORDS):
        return "set (hidden)"
    v = value.strip()
    if v.lower() in _YES_NO or _SHORT_NUMBER.match(v):
        return v
    parts = urlsplit(v)
    if "@" in v or not (parts.scheme and parts.netloc):
        return "set (hidden)"
    try:
        port = parts.port
    except ValueError:
        return "set (hidden)"
    host = parts.hostname or ""
    if host and ":" not in host:
        return parts._replace(netloc=host + (f":{port}" if port else ""), path="", query="",
                              fragment="").geturl()
    return "set (hidden)"


_COUNTS = {
    "agents": "SELECT COUNT(*) AS n FROM agents",
    "chats": "SELECT COUNT(*) AS n FROM chats",
    "remote_machines": "SELECT COUNT(*) AS n FROM remote_machines",
    "dynamic_tasks": "SELECT COUNT(*) AS n FROM dynamic_tasks",
    "triggers": "SELECT COUNT(*) AS n FROM triggers",
}


def support_info(out=sys.stdout) -> int:
    import config
    from auth import roles
    from core.config import deployment
    from storage import database as task_store
    from storage.mcp import mcp_store
    from storage.pg import get_conn
    from storage.schema import read_platform_version

    with get_conn() as conn:
        meta = read_platform_version(conn) or {}
        postgres = conn.execute("SHOW server_version").fetchone()["server_version"]
        counts = {name: int(conn.execute(sql).fetchone()["n"]) for name, sql in _COUNTS.items()}
    users = task_store.list_users() or []
    states = mcp_store.get_all_mcp_states()
    by_role: dict[str, int] = {}
    for u in users:
        by_role[u.get("role") or "?"] = by_role.get(u.get("role") or "?", 0) + 1
    two_factor = sum(1 for u in users if u.get("totp_enabled"))
    lines = [
        "OtoDock support info (no secret is printed)",
        f"release: {config.PINNED_OTODOCK_VERSION or 'unknown'}",
        f"database: created by {meta.get('created_by') or 'unknown'}, last migrated by "
        f"{meta.get('last_migrated_by') or 'unknown'}",
        f"python: {sys.version.split()[0]}",
        f"postgres: {postgres}",
        f"runs in a container: {'yes' if deployment.in_docker_compose() else 'no'}",
        f"users: {len(users)} (" + ", ".join(f"{r} {n}" for r, n in sorted(by_role.items()))
        + f"; {two_factor} with an authenticator code)",
        f"admins: {sum(1 for u in users if roles.is_admin(u.get('role')))}",
        f"agents: {counts['agents']}",
        f"chats: {counts['chats']}",
        f"paired machines: {counts['remote_machines']}",
        f"MCP servers: {len(states)} known, {sum(1 for on in states.values() if on)} enabled",
        f"scheduled tasks: {counts['dynamic_tasks']}",
        f"triggers: {counts['triggers']}",
        "config (set keys; values only where they hold no secret):",
    ]
    file_keys = set(config._file_cfg)
    for key in sorted(config._CFG_KEYS_READ | file_keys):
        value = os.environ.get(key) or str(config._file_cfg.get(key) or "")
        if value:
            lines.append(f"  {key} = {shown_value(key, value)}")
    # The keys the boot warning names: read by nothing, compose and the
    # scripts included (most often a misspelt key).
    unknown = config.unknown_config_keys(
        file_keys, config._CFG_KEYS_READ | config.LAZY_CONFIG_KEYS | config.FOREIGN_CONFIG_KEYS)
    if unknown:
        lines.append("config.env keys no OtoDock component reads: " + ", ".join(unknown))
    print("\n".join(lines), file=out)
    return DONE


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def _media_cache() -> int:
    from services.media import media_pipeline
    from storage import database as task_store
    removed = media_pipeline.sweep_host_media_cache()
    expired = task_store.sweep_expired_media_tokens()
    media_pipeline.remove_cache_copies(expired)
    return removed + len(expired)


def _recover_bin_reap() -> int:
    import config
    from storage.files import recover_bin_store
    root = Path(config.RECOVER_BIN_DIR)
    # The reap deletes the rows before the bytes: a root this process cannot
    # write would lose the rows and keep the bytes for good.
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise Refused(f"the recover bin {root} is not writable by this process: run this as "
                      "the account the platform runs as")
    return recover_bin_store.delete_expired()


def _tombstone_reap() -> int:
    from storage.files import file_tombstones_store
    return file_tombstones_store.delete_expired()


# The platform's minute sweep (startup_sweeps.py ``sweep_items``), item by item:
# (slug, the sweep's name, what it does, how to run it from here or None
# when it reads the running server's own state).
JOBS: list[tuple[str, str, str, Callable[[], int] | None]] = [
    ("warmup-registry", "warmup_registry sweep", "forgets warmups that never finished", None),
    ("install-registry", "install_registry sweep", "forgets MCP installs that never finished", None),
    ("transfer-registry", "transfer_registry sweep", "forgets file transfers that never finished",
     None),
    ("catalog-installs", "catalog_install_registry sweep", "forgets stale catalog installs", None),
    ("media-cache", "media cache sweep",
     "removes idle copies of paired machines' media and expired media links", _media_cache),
    ("preview-snapshots", "preview snapshot sweep", "removes previews of deleted chats", None),
    ("recover-bin-reap", "recover-bin reap", "deletes recover bin entries past their 7 days",
     _recover_bin_reap),
    ("tombstone-reap", "tombstone reap", "deletes file sync tombstones past their 30 days",
     _tombstone_reap),
    ("idle-sync", "idle fingerprint sweep", "syncs paired machines' trees that changed", None),
    ("retention", "retention sweep",
     "ages out old session files once a day (Admin, System: Run now)", None),
    ("quota-monitor", "quota monitor sweep", "measures storage against the quotas", None),
    ("subscription-health", "subscription health sweep",
     "warns before an engine sign-in lapses", None),
    ("subscription-windows", "subscription window alert sweep",
     "warns when an engine's weekly window fills", None),
    ("mcp-auto-update", "mcp auto-update sweep", "applies MCP updates once a week", None),
    ("agent-template-updates", "agent template update sweep",
     "tells managers a newer agent template exists", None),
    ("tools-idle", "docker tools idle sweep", "stops Docker tools nobody used for their quiet spell", None),
]


def jobs_list(out=sys.stdout) -> int:
    print("The platform runs these every minute; the ones marked 'here' also run from this "
          "command line (jobs run <job>):", file=out)
    for slug, _name, what, run in JOBS:
        print(f"  {slug:<24} {'here' if run else 'inside the platform':<20} {what}", file=out)
    return DONE


def jobs_run(slug: str, out=sys.stdout) -> int:
    job = next((j for j in JOBS if j[0] == slug), None)
    if job is None:
        raise Refused(f"no job is named {slug!r} (jobs list names them)")
    _slug, _name, what, run = job
    if run is None:
        raise Refused(f"{slug} reads the running platform's own state: it runs inside the "
                      "platform every minute")
    count = run()
    print(f"{slug}: done, {count} removed ({what}).", file=out)
    return DONE


# ---------------------------------------------------------------------------
# drain, resume, status: the running server's open work
# ---------------------------------------------------------------------------

WORK_PATH = "/v1/internal/work"
DRAIN_PATH = "/v1/internal/drain"
# Seconds: how long one request waits for the server's answer, the pause
# between two reads of the work, how long past its bound a drain that never
# reaches "held" is still read, and how long a silent server is asked again
# (a server held by a busy disk right after an image build answers late).
ANSWER_S = 10.0
POLL_S = 2.0
PAST_BOUND_S = 60.0
ASK_FOR_S = 60.0
# How long the paired machines are told the platform may be away.
AWAY_S = 900
# The kinds of open work the server counts, in the order a line names them:
# (the key in the answer's "open" block, one of it, several).
WORK_KINDS = (
    ("turns", "turn", "turns"),
    ("runs", "task run", "task runs"),
    ("calls", "call", "calls"),
    ("duplex", "voice conversation", "voice conversations"),
    ("meetings", "meeting", "meetings"),
    ("terminals", "terminal", "terminals"),
    ("moves", "chat move", "chat moves"),
    ("background", "background job", "background jobs"),
)
NO_ANSWER = "The server did not answer{}. It is not running, or it is older than 2.0."
NOT_RUNNING_LINE = "Nothing answers on the server's address: it is not running."
OLDER_LINE = "The server has no such route: it is older than 2.0."
FORBIDDEN_LINE = ("The server refused the request: this is not its master key, "
                  "or the command does not run on the server's own machine.")
UNREADABLE_LINE = "The server answered something this command cannot read."
# Why one request got no report (``_ask_once``).
_SILENT, _NOBODY, _NO_ROUTE, _FORBIDDEN, _UNREADABLE = "silent", "nobody", "no_route", "forbidden", "unreadable"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The master key goes to the server's own address and nowhere a
    redirect points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def server_url() -> str:
    """Where the running server listens for this machine: loopback and its
    port, or the one address ``PROXY_HOST`` binds it to."""
    import config
    host = (config.HOST or "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{f'[{host}]' if ':' in host else host}:{config.PORT}"


def _ask_once(method: str, path: str, body: dict | None = None) -> tuple[dict | None, str]:
    """One request to the running server with the master key: its report of
    the open work, or None and why (silent: no answer in time, a reset or a
    5xx; nobody listens; no such route, a release older than 2.0; the key or
    the address refused; an answer that is no report)."""
    import config
    request = urllib.request.Request(
        server_url() + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"})
    # No proxy of the environment: the call never leaves this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=ANSWER_S) as resp:
            answer = json.load(resp)
    except urllib.error.HTTPError as e:
        # A 401: the key is not the server's (a shell's own PROXY_API_KEY
        # wins over the config file), said as the refused key it is.
        why = {404: _NO_ROUTE, 403: _FORBIDDEN, 401: _FORBIDDEN}.get(
            e.code, _SILENT if e.code >= 500 else _UNREADABLE)
        return None, why
    except urllib.error.URLError as e:
        return None, _NOBODY if isinstance(e.reason, ConnectionRefusedError) else _SILENT
    except ConnectionRefusedError:
        return None, _NOBODY
    except ValueError:
        return None, _UNREADABLE
    except (OSError, http.client.HTTPException):
        return None, _SILENT
    if isinstance(answer, dict) and isinstance(answer.get("state"), str):
        return answer, ""
    return None, _UNREADABLE


def ask_server(method: str, path: str, body: dict | None = None) -> dict | None:
    """One request to the running server: its report, or None (``_ask_once``)."""
    return _ask_once(method, path, body)[0]


def ask_until_answered(method: str, path: str, body: dict | None = None, *, what: str = "") -> dict:
    """The server's report for ``drain``, ``resume`` and ``status``: a silent
    server is asked again for ``ASK_FOR_S``; a server that is not running,
    is older than 2.0 or refuses the key is said at once, each with its own
    exit status (``Unanswered``)."""
    start = time.monotonic()
    while True:
        answer, why = _ask_once(method, path, body)
        if answer is not None:
            return answer
        if why == _NOBODY:
            raise Unanswered(NOT_RUNNING_LINE, NOT_RUNNING)
        if why == _NO_ROUTE:
            raise Unanswered(OLDER_LINE, OLDER)
        if why == _FORBIDDEN:
            raise Unanswered(FORBIDDEN_LINE)
        if why == _UNREADABLE:
            raise Unanswered(UNREADABLE_LINE)
        waited = time.monotonic() - start
        if waited >= ASK_FOR_S:
            raise Unanswered(f"The server did not answer{what} in {max(1, round(waited))} seconds.")
        time.sleep(POLL_S)


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _counted(n: int, one: str, several: str) -> str:
    return f"{n} {one if n == 1 else several}"


def _span(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return _counted(seconds // 60, "minute", "minutes")
    return _counted(seconds, "second", "seconds")


def open_work(work: dict) -> tuple[int, str]:
    """How many things are working, and their kinds in words ("2 turns, 1
    task run"), empty when nothing is."""
    counts = work.get("open")
    counts = counts if isinstance(counts, dict) else {}
    named = [(_count(counts.get(key)), one, several) for key, one, several in WORK_KINDS]
    return (sum(n for n, _, _ in named),
            ", ".join(_counted(n, one, several) for n, one, several in named if n))


def _held_line(work: dict, bound_s: int) -> str:
    """What a drain says once the server holds new work."""
    total, kinds = open_work(work)
    if work.get("drained", not total):
        said = ["Nothing is working. New work is held until the platform is back."]
    else:
        left = f"{kinds} still working" if kinds else "work still open"
        said = [f"{'The wait ran out' if bound_s else 'New work is held'} with {left}.",
                "They are cut by the restart and continue after it." if total > 1
                else "It is cut by the restart and continues after it."]
    waiting = _count(work.get("waiting_on_people"))
    if waiting:
        said += [f"{_counted(waiting, 'turn waits', 'turns wait')} for a person.",
                 "It is asked again after the restart." if waiting == 1
                 else "They are asked again after the restart."]
    return " ".join(said)


def drain(*, bound: int | None = None, away: int = AWAY_S, quiet: bool = False,
          probe: bool = False, out=sys.stdout) -> int:
    """Ask the server to wait for its open work and hold what is new, and
    follow it until it holds: a line when the work or the state changes,
    one line in all with ``quiet``. ``probe`` only asks whether the server
    knows the route."""
    import config
    from core.drain import HELD, RUNNING
    if probe:
        return DONE if ask_server("GET", WORK_PATH) is not None else REFUSED
    bound_s = config.DRAIN_BOUND_S if bound is None else bound
    work: dict | None = ask_until_answered(
        "POST", DRAIN_PATH, {"action": "drain", "bound_s": bound_s, "away_s": away}, what=" the drain")
    give_up = time.monotonic() + bound_s + PAST_BOUND_S
    seen: tuple | None = None
    while True:
        if work is None:
            # A read the server missed is read again until the drain's own end.
            if time.monotonic() >= give_up:
                raise Refused(NO_ANSWER.format(" the drain"))
            time.sleep(POLL_S)
            work = ask_server("GET", WORK_PATH)
            continue
        state = work["state"]
        if state == HELD:
            break
        # Read back as running: a resume from another shell ended the drain.
        if state == RUNNING and seen is not None:
            raise Refused("The drain was called off. The platform takes new work again.")
        _total, kinds = open_work(work)
        if (state, kinds) != seen:
            if not kinds:
                line = "Nothing is working."
            elif seen is None and bound_s:
                line = f"Waiting for open work: {kinds}. Up to {_span(bound_s)}."
            else:
                line = f"Still working: {kinds}."
            if not quiet:
                print(line, file=out, flush=True)
            seen = (state, kinds)
        if time.monotonic() >= give_up:
            raise Refused("The server did not finish the drain in time. Open work is cut by the "
                          "restart and continues after it.")
        time.sleep(POLL_S)
        work = ask_server("GET", WORK_PATH)
    print(_held_line(work, bound_s), file=out, flush=True)
    return DONE


def resume(out=sys.stdout) -> int:
    """Call a drain off: the server takes new work again."""
    ask_until_answered("POST", DRAIN_PATH, {"action": "resume"}, what=" the resume")
    print("The platform takes new work again.", file=out)
    return DONE


def status(out=sys.stdout) -> int:
    """Busy or idle, in one line, with no change of state."""
    from core.drain import RUNNING
    work = ask_until_answered("GET", WORK_PATH)
    total, kinds = open_work(work)
    busy = work.get("busy")
    starting = work.get("starting") is True
    if not (busy if isinstance(busy, bool) else total > 0):
        # While the tools are prepared a restart starts their build again.
        said = ["Idle."] if starting else ["Idle. A good moment to update."]
    else:
        said = [f"Busy: {kinds}." if kinds else "Busy."]
    waiting = _count(work.get("waiting_on_people"))
    if waiting:
        said.append(f"{_counted(waiting, 'turn waits', 'turns wait')} for a person.")
    queued = _count(work.get("queued"))
    if queued:
        said.append(f"{_counted(queued, 'message waits', 'messages wait')} to be sent.")
    if starting:
        said.append("The platform is starting. New work waits until its compute machine has signed in "
                    "and its tools are ready.")
    if work["state"] != RUNNING:
        said.append("The platform is holding new work for an update.")
    line = compute_line(work)
    if line:
        said.append(line)
    print(" ".join(said), file=out)
    return DONE


def compute_line(work: dict) -> str:
    """The compute machine in one sentence, "" when the server is older
    than 2.0 (it names none)."""
    block = work.get("compute")
    if not isinstance(block, dict):
        return ""
    if signed_in(block):
        return "The compute machine is signed in."
    if block.get("own") is False:
        return ("No compute machine is signed in, and this install runs none of its own. "
                "Chats, tasks and calls cannot run until one is set up.")
    return "The compute machine is not signed in. Chats, tasks and calls cannot run until it is."


def signed_in(block: dict) -> bool:
    """Whether the compute machine this install waits for is signed in. An
    install that runs one of its own waits for that one, the machine that
    shares the platform's folders, as readiness does: one on another server
    never stopped for a backup or an update. An install with none of its
    own, and a server that names no shared machine yet, count any."""
    if block.get("own") is True:
        return block.get("shared", block.get("connected")) is True
    return block.get("connected") is True


def compute(*, wait: int = 0, out=sys.stdout) -> int:
    """Whether the compute machine is signed in, waiting up to ``wait``
    seconds for it: 0 signed in, 1 not (chats, tasks and calls wait for it). Signed
    in counts once the start no longer holds new work: the hold ends a few
    seconds after the sign-in (the machine's sessions adopted), and a chat
    sent before is refused."""
    deadline = time.monotonic() + wait
    while True:
        work = ask_server("GET", WORK_PATH)
        block = work.get("compute") if work else None
        block = block if isinstance(block, dict) else {}
        taking = signed_in(block) and work is not None and work.get("starting") is not True
        if work is not None and (taking or not compute_line(work)):
            print(compute_line(work) or "This server is older than 2.0: it runs no compute machine.", file=out)
            return DONE
        if time.monotonic() >= deadline:
            if work is None:
                raise Refused(NO_ANSWER.format(""))
            if signed_in(block):
                # Signed in, the platform still busy with its start: nothing for the compute service to answer.
                print("The compute machine is signed in. The platform is still starting.", file=out)
                return DONE
            print(compute_line(work), file=out)
            return REFUSED
        time.sleep(POLL_S)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def _seconds(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("a number of seconds, 0 or more")
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="otodock-admin", description="OtoDock's admin command line.")
    sub = p.add_subparsers(dest="command", required=True)
    user = sub.add_parser("user", help="sign-in recovery for one account").add_subparsers(
        dest="action", required=True)
    reset = user.add_parser("reset-password", help="a temporary password, or one from stdin")
    reset.add_argument("email")
    reset.add_argument("--password-stdin", action="store_true",
                       help="read the new password from stdin and set it as given")
    reset.set_defaults(run=lambda a: reset_password(a.email, from_stdin=a.password_stdin),
                       changes=True)
    off = user.add_parser("disable-2fa", help="turn the authenticator code off")
    off.add_argument("email")
    off.add_argument("--passkeys", action="store_true", help="also delete the account's passkeys")
    off.set_defaults(run=lambda a: disable_2fa(a.email, passkeys=a.passkeys), changes=True)
    info = sub.add_parser("support-info", help="versions, counts and settings, secrets left out")
    info.set_defaults(run=lambda a: support_info())
    jobs = sub.add_parser("jobs", help="the platform's background jobs").add_subparsers(
        dest="action", required=True)
    jobs.add_parser("list").set_defaults(run=lambda a: jobs_list())
    run = jobs.add_parser("run")
    run.add_argument("job")
    run.set_defaults(run=lambda a: jobs_run(a.job), changes=True)
    wait = sub.add_parser("drain", help="wait for open work and hold what is new, before a stop")
    wait.add_argument("--bound", type=_seconds, metavar="SECONDS",
                      help="how long to wait for open work (OTODOCK_DRAIN_BOUND_S, 600)")
    wait.add_argument("--away", type=_seconds, default=AWAY_S, metavar="SECONDS",
                      help="how long the paired machines are told the platform may be away (900)")
    wait.add_argument("--quiet", action="store_true", help="print the last line only")
    wait.add_argument("--probe", action="store_true",
                      help="only ask whether the running server can drain (exit 0 or 1)")
    wait.set_defaults(run=lambda a: drain(bound=a.bound, away=a.away, quiet=a.quiet,
                                          probe=a.probe), server=True)
    sub.add_parser("resume", help="call a drain off: take new work again").set_defaults(
        run=lambda a: resume(), server=True)
    sub.add_parser("status", help="busy or idle: whether this is a good moment to update"
                   ).set_defaults(run=lambda a: status(), server=True)
    machine = sub.add_parser("compute", help="whether the compute machine is signed in (exit 0 or 1)")
    machine.add_argument("--wait", type=_seconds, default=0, metavar="SECONDS",
                         help="how long to wait for it to sign in (0)")
    machine.set_defaults(run=lambda a: compute(wait=a.wait), server=True)
    return p


def target_line() -> str:
    """The config file and the database a changing command is about to
    change: a shell may lack the service's environment and reach another
    install's database. The database by host, port and name only (libpq's
    own parser; the user and the password never print)."""
    import psycopg

    import config
    try:
        parts = psycopg.conninfo.conninfo_to_dict(config.DATABASE_URL or "")
        where = (f"{parts.get('host') or 'localhost'}:{parts.get('port') or 5432}/"
                 f"{parts.get('dbname') or '?'}")
    except psycopg.ProgrammingError:
        where = "the one DATABASE_URL names (it does not parse)"
    return f"admin_cli: config {config._config_env}, database {where}"


def _warn_on_another_release() -> None:
    import config
    from storage.pg import get_conn
    from storage.schema import read_platform_version
    with get_conn() as conn:
        meta = read_platform_version(conn) or {}
    migrated = meta.get("last_migrated_by") or ""
    if migrated and config.PINNED_OTODOCK_VERSION and migrated != config.PINNED_OTODOCK_VERSION:
        print(f"admin_cli: the database was last migrated by OtoDock {migrated} and this command "
              f"line is {config.PINNED_OTODOCK_VERSION}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return USAGE if exc.code else DONE
    if not config_file_present():
        print("admin_cli: no config.env here and no DATABASE_URL: run it through "
              "scripts/otodock-admin.sh, or with the environment the platform runs with "
              "(PLATFORM_CONFIG_DIR names the folder holding config.env)", file=sys.stderr)
        return REFUSED
    # The platform's logger is process-wide: the command's handler lives for
    # the command and the logger is left as it was found.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    from storage import pg
    try:
        if getattr(args, "changes", False):
            print(target_line(), file=sys.stderr)
        # A command that asks the running server opens no database.
        if not getattr(args, "server", False):
            _warn_on_another_release()
        return args.run(args)
    except Refused as exc:
        print(f"admin_cli: {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, Unanswered) else REFUSED
    finally:
        pg.close_pool(timeout=2.0)
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


if __name__ == "__main__":
    sys.exit(main())
