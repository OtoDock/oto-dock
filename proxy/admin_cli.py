"""The admin command line: the platform's break-glass tools, run beside the
server (``scripts/otodock-admin.sh`` runs it in the proxy container or the
source install's venv).

    admin_cli.py user reset-password <email> [--password-stdin]
    admin_cli.py user disable-2fa <email> [--passkeys]
    admin_cli.py support-info
    admin_cli.py jobs list
    admin_cli.py jobs run <job>

It reads the configuration and the database the server reads, through the
same store functions the dashboard's routes call; it never migrates the
database. A command that changes something first names the config file
and the database it is about to change, and writes a log line to stderr
and the platform's log file: the dashboard's line for a password reset,
the same words with the command line as the actor for 2FA and passkeys.
Exit status: 0 done, 1 refused, 2 usage.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("otodock")

DONE, REFUSED, USAGE = 0, 1, 2

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
    return media_pipeline.sweep_host_media_cache() + task_store.sweep_expired_media_tokens()


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


# The platform's minute sweep (startup.py ``sweep_items``), item by item:
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
# The command line
# ---------------------------------------------------------------------------

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
        _warn_on_another_release()
        return args.run(args)
    except Refused as exc:
        print(f"admin_cli: {exc}", file=sys.stderr)
        return REFUSED
    finally:
        pg.close_pool(timeout=2.0)
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


if __name__ == "__main__":
    sys.exit(main())
