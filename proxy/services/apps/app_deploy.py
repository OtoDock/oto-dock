"""Deploying a folder app (APPS.md "Deploy pipeline").

``deploy_folder`` is the one path from a working tree to a release the
viewers are served: pull the folder through from a satellite when the
session runs there, validate ``app.json`` (the whole manifest, the caps,
the phase 3 blocks refused), copy the tree into the next release slot,
then either park it as a pending release (the per-app switch, or a
manifest that is not approved) or take it live: copy the database BEFORE
the new server starts (a server migrates at start), start the new release
next to the old one, switch when it answers ``/_health``, point the row,
prune. A failed start removes the copy and leaves the old release
serving. Rollback restores the copy taken before the current release went
live, after snapshotting the database as it is now. Synchronous helpers
run off the loop; the async entry points take the row's deploy lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import config
from api.apps import manifest as _mf
from auth.request_path import has_traversal
from services.apps import app_sandbox, app_supervisor, releases
from services.infra import path_confinement, safe_fs
from storage import database as task_store
from storage.pg import run_db
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.apps")

# The deploy answer's ``status`` (core-seams phase 8): what ``deploy_folder``
# and the approve / reject routes return, what the hooks forward to the
# display MCP verbatim. ``ok`` — a release went live; ``refused`` — the
# static or rendered checks said no (the pins hook's lint says the same
# word); ``pending approval`` — the release is parked on the card;
# ``rejected`` — the human Reject route removed the parked copy (never a
# ``deploy_folder`` outcome). The persisted ``pinned_apps.deploy_state`` is
# ``db_apps.DEPLOY_*`` (idle | pending), a different machine.
RESULT_OK = "ok"
RESULT_REFUSED = "refused"
RESULT_PENDING_APPROVAL = "pending approval"
RESULT_REJECTED = "rejected"
# Why a release that turns the per-app switch off parks (APPS.md "The
# contract"): a deploy raises the switch, only a person lowers it.
LOWERS_APPROVAL = "turns off approval for every deploy"
RESULTS: frozenset[str] = frozenset({RESULT_OK, RESULT_REFUSED, RESULT_PENDING_APPROVAL, RESULT_REJECTED})

APP_JSON_MAX_BYTES = 32 * 1024
MAX_EGRESS_HOSTS = 16
MAX_FILE_PREFIXES = 8
PULL_MAX_FILES = 200
PULL_MAX_BYTES = 32 * 1024 * 1024
KEEP_ROLLBACK_SNAPSHOTS = 3
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Segments a declared file prefix may never name (SHARING.md, APPS.md).
NEVER_SEGMENTS = frozenset({".oauth", ".credentials", "app-releases", "app-data", "shares",
                            "externals", "data"})
ALLOWED_KEYS = frozenset({"title", "actions", "catalog", "files", "egress", "handlers", "exports",
                          "bindings", "requires", "steps", "secrets", "inbound", "external",
                          "deploy_requires_approval", "description"})
# APPS.md "External links": what a link may do — the public hosts the host
# page may open in a new tab, the app's own API paths whose non-GET calls
# from a link need a passed challenge, and the session cookie's lifetime.
MAX_EXTERNAL_LINKS = 16
MAX_CHALLENGE_PATHS = 16
CHALLENGE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]{0,199}$")
SESSION_DAYS_DEFAULT = 30
SESSION_DAYS_MAX = 365
# APPS.md "Inbound hooks": a public route per hook, verified by the platform
# with a declared secret and one of four schemes, then a delivery for the
# named handler — a fourth kind of wake, declared by the hook itself.
INBOUND_SCHEMES = ("stripe", "github", "hmac_sha256", "bearer")
MAX_INBOUND = 8
# APPS.md "Secrets": a declared secret is a name in the env-variable shape,
# a value a person sets later, never the agent. The names the runtime owns
# are refused so a declaration cannot shadow what the server is handed, and
# so are the loader and interpreter hooks: the env reaches the host-side
# launcher (python3, pasta, bwrap) before the sandbox exists.
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SECRET_RESERVED_NAMES = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "PORT",
                                   "BASH_ENV", "ENV", "GCONV_PATH"})
SECRET_RESERVED_PREFIXES = ("OTODOCK_", "OTO_", "BUN_", "LD_", "DYLD_", "PYTHON")
SECRET_HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
SECRET_PREFIX_MAX_CHARS = 32
MAX_SECRETS = 16
# APPS.md "Handlers" / "Bindings": the names and caps of the three blocks.
HANDLER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
EXPORT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
APP_EVENT_RE = re.compile(r"^app:([a-z][a-z0-9_-]{0,39}):([a-z0-9][a-z0-9-]{0,63})$")
# APPS.md "Steps": a handler whose name appears in the `steps` block runs a
# script where the agent's sessions run instead of the server route; the
# event `step:<name>` wakes a server handler when that step finishes.
STEP_EVENT_RE = re.compile(r"^step:([a-z][a-z0-9_-]{0,39})$")
# CHECKS.md "The handler kind": a server handler subscribed to
# `check:<name>` is woken by that check to judge a turn (a synchronous
# POST, the verdict in the 2xx body). The check itself lives in the agent's
# config, so nothing here cross-checks the name.
CHECK_EVENT_RE = re.compile(r"^check:([a-z0-9][a-z0-9_-]{0,63})$")
STEP_RUN_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
STEP_NEVER_PREFIXES = ("client/", "server/", "data/")
MAX_STEPS = 16
STEP_TIMEOUT_DEFAULT_S = 60
STEP_TIMEOUT_MAX_S = 7200          # the session ceiling (config.CLAUDE_TIMEOUT)
STEP_SCRIPT_MAX_BYTES = 256 * 1024
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_HANDLERS = 16
MAX_EXPORTS_PER_KIND = 16
MAX_BINDINGS = 16
MAX_REQUIRES = 16
MAX_DESCRIPTION_CHARS = 200
# The platform events a handler may wake on; the last is personal apps only
# (deliveries are per user and never in the agent-scope slice).
PLATFORM_EVENTS = ("chat_created", "task_finished", "file_changed", "trigger_fired",
                   "notification_created",
                   # CHECKS.md: a chat's turn ended (the sessions feed), a check's
                   # verdict landed (the check_verdicts feed).
                   "turn_finished", "check_verdict")


class DeployError(Exception):
    """The deploy cannot proceed; the message is for the agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def confine_to_scope(agent: str, username: str, path: Path) -> None:
    """Refuse a working folder, or a path in one, that leads out of its
    scope's workspace through a link. The joins are lexical, so a link a
    session planted at ``apps/<slug>`` (or above it) would have the deploy,
    the preview, the export or the import read or write another user's
    files, another agent's app or the platform's own."""
    root = (config.get_agent_dir(agent) / layout.scope_workspace(username)).resolve()
    if not path.resolve().is_relative_to(root):
        raise DeployError(f"{path.name} is a link that leads out of the workspace — "
                          "make it a real folder")


@dataclass
class Manifest:
    title: str
    actions_json: str
    files_json: str
    egress_json: str
    requires_approval: bool | None
    handlers_json: str = ""
    exports_json: str = ""
    bindings_json: str = ""
    requires_json: str = ""
    steps_json: str = ""
    secrets_json: str = ""
    inbound_json: str = ""
    external_json: str = ""

    def blocks(self) -> dict[str, str]:
        """The column texts ``upsert_app`` writes (the signed blocks and
        ``requires``)."""
        return {"files": self.files_json, "egress": self.egress_json,
                "handlers": self.handlers_json, "exports": self.exports_json,
                "bindings": self.bindings_json, "requires": self.requires_json,
                "steps": self.steps_json, "secrets": self.secrets_json,
                "inbound": self.inbound_json, "external": self.external_json}


# ── app.json ────────────────────────────────────────────────────────────────


def read_app_json(source_dir: Path) -> dict:
    """The folder's app.json, read as a regular file inside the tree without
    following a link (a refusal names the rule, never anything the link
    points at)."""
    try:
        raw = releases.read_tree_file(source_dir, "app.json", max_size=APP_JSON_MAX_BYTES)
    except safe_fs.FileTooLarge:
        raise DeployError("app.json is larger than 32 KB")
    except safe_fs.SymlinkRefused:
        raise DeployError("symlink not allowed in an app: app.json")
    except safe_fs.NotRegularFile:
        raise DeployError("app.json is not a regular file")
    except OSError:
        raise DeployError("no app.json in the folder — a folder app needs one (APPS.md)")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise DeployError(f"app.json is not valid JSON: {e}")
    if not isinstance(doc, dict):
        raise DeployError("app.json must be a JSON object")
    return doc


def require_entry_page(source_dir: Path) -> None:
    """``client/index.html`` must be a regular file in the tree, reached
    without a link (the link itself is never followed)."""
    try:
        root, base = releases.tree_root(source_dir)
        st = safe_fs.lstat_beneath(root, f"{base}/client/index.html" if base else "client/index.html")
    except (safe_fs.SymlinkRefused, safe_fs.EscapeRefused):
        raise DeployError("symlink not allowed in an app: client/index.html")
    except OSError:
        raise DeployError("client/index.html is missing (the page every viewer opens)")
    if stat.S_ISLNK(st.st_mode):
        raise DeployError("symlink not allowed in an app: client/index.html")
    if not stat.S_ISREG(st.st_mode):
        raise DeployError("client/index.html is missing (the page every viewer opens)")


def validate_egress(hosts) -> list[str]:
    """Hostnames or public IP addresses, no wildcards, no private ranges."""
    return _validate_public_hosts(hosts, "egress", MAX_EGRESS_HOSTS)


def _validate_public_hosts(hosts, what: str, cap: int) -> list[str]:
    if hosts is None:
        return []
    if not isinstance(hosts, list) or len(hosts) > cap:
        raise DeployError(f"{what} must be a list of at most {cap} hosts")
    out: list[str] = []
    for h in hosts:
        if not isinstance(h, str):
            raise DeployError(f"{what} hosts must be strings")
        host = h.strip().lower().rstrip(".")
        if not host or "*" in host or "/" in host or ":" in host and not host.count(":") > 1:
            raise DeployError(f"{what} host {h!r}: a hostname or an IP address, no port, no wildcard")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if not ip.is_global:
                raise DeployError(f"{what} host {h!r}: private, loopback and link-local addresses are never reachable")
        elif not _HOST_RE.match(host) or host in ("localhost",):
            raise DeployError(f"{what} host {h!r}: not a valid public hostname")
        if host not in out:
            out.append(host)
    return out


def validate_external(block) -> dict:
    """``{links?: [host…], challenge?: [path…], session_days?: n}`` (APPS.md
    "External links"): the public hosts the link's host page may open in a
    new tab (validated as ``egress`` — no wildcard, no private address), the
    app's own API paths whose non-GET calls from a link must carry a passed
    challenge while Turnstile is configured (whole segments, no dot
    segment), and the lifetime of the session cookie in days (1 to 365; 30
    when absent). Keys present only when declared."""
    if block is None:
        return {}
    if not isinstance(block, dict) or any(k not in ("links", "challenge", "session_days") for k in block):
        raise DeployError("external must be an object with links, challenge and/or session_days")
    out: dict = {}
    links = _validate_public_hosts(block.get("links"), "external.links", MAX_EXTERNAL_LINKS)
    if links:
        out["links"] = links
    paths = block.get("challenge")
    if paths is not None:
        if not isinstance(paths, list) or len(paths) > MAX_CHALLENGE_PATHS:
            raise DeployError(f"external.challenge must be a list of at most {MAX_CHALLENGE_PATHS} paths")
        keep: list[str] = []
        for p in paths:
            if not isinstance(p, str) or not CHALLENGE_PATH_RE.match(p.strip()) \
                    or has_traversal(p.strip()):
                raise DeployError(f"external.challenge: {p!r} is not a path of the app's own API "
                                  "(for example /register)")
            path = p.strip().rstrip("/") or "/"
            if path not in keep:
                keep.append(path)
        if keep:
            out["challenge"] = keep
    days = block.get("session_days")
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= SESSION_DAYS_MAX:
            raise DeployError(f"external.session_days is whole days, 1 to {SESSION_DAYS_MAX}")
        out["session_days"] = int(days)
    return out


def validate_files(block, shared: bool) -> dict[str, list[str]]:
    """The declared prefixes, agent-dir-relative (``workspace/…`` or
    ``knowledge/…``; a personal app's are stored under ``users/{owner}/``
    and resolved from the owner at call time). Knowledge is never writable;
    the never-declarable segments are refused literally and by the
    credential-path rule."""
    if block is None:
        return {"read": [], "write": []}
    if not isinstance(block, dict) or any(k not in ("read", "write") for k in block):
        raise DeployError("files must be an object with read and/or write lists")
    from services import path_roles
    out: dict[str, list[str]] = {"read": [], "write": []}
    total = 0
    for mode in ("read", "write"):
        prefixes = block.get(mode) or []
        if not isinstance(prefixes, list):
            raise DeployError(f"files.{mode} must be a list")
        for p in prefixes:
            if not isinstance(p, str):
                raise DeployError("file prefixes must be strings")
            rel = p.strip().strip("/")
            parts = rel.split("/") if rel else []
            if not parts or parts[0] not in (layout.WORKSPACE, layout.KNOWLEDGE):
                raise DeployError(f"file prefix {p!r}: must start with workspace/ or knowledge/")
            try:
                rel = path_confinement.normalize_rel_path(rel)
            except path_confinement.PathOutsideRoot:
                raise DeployError(f"file prefix {p!r}: no empty or dot segments")
            if any(seg in NEVER_SEGMENTS for seg in parts) or (
                    len(parts) >= 3 and parts[1] == "apps" and "data" in parts):
                raise DeployError(f"file prefix {p!r}: that path is never declarable")
            if path_roles.is_protected_credentials_path(rel):
                raise DeployError(f"file prefix {p!r}: that path holds credentials")
            if mode == "write" and parts[0] == layout.KNOWLEDGE:
                raise DeployError(f"file prefix {p!r}: knowledge is read-only for apps")
            stored = rel if shared else f"{layout.USERS}/{{owner}}/{rel}"
            if not shared and parts[0] == layout.KNOWLEDGE:
                raise DeployError(f"file prefix {p!r}: a personal app names the owner's tree only")
            if stored not in out[mode]:
                out[mode].append(stored)
                total += 1
            if total > MAX_FILE_PREFIXES:
                raise DeployError(f"at most {MAX_FILE_PREFIXES} file prefixes")
    return out


def _str_list(value, what: str, cap: int, pattern: re.Pattern | None = None,
              max_chars: int = 64) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > cap:
        raise DeployError(f"{what} must be a list of at most {cap} entries")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not v.strip():
            raise DeployError(f"{what}: entries must be non-empty strings")
        s = v.strip()
        if len(s) > max_chars or (pattern is not None and not pattern.match(s)):
            raise DeployError(f"{what}: {v!r} is not a valid name")
        if s not in out:
            out.append(s)
    return out


def validate_bindings(block) -> list[dict]:
    """``[{name, agent, app}]`` — the apps this one calls, by name (APPS.md
    "Bindings"). Shape only: the target is resolved at call time, so an
    app may declare a binding before its target exists."""
    if block is None:
        return []
    if not isinstance(block, list) or len(block) > MAX_BINDINGS:
        raise DeployError(f"bindings must be a list of at most {MAX_BINDINGS} entries")
    out: list[dict] = []
    seen: set[str] = set()
    for b in block:
        if not isinstance(b, dict) or set(b) != {"name", "agent", "app"}:
            raise DeployError("each binding is an object with name, agent and app")
        name, agent, app = (str(b.get(k) or "").strip() for k in ("name", "agent", "app"))
        if not HANDLER_NAME_RE.match(name):
            raise DeployError(f"binding {name!r}: not a valid name")
        if not AGENT_NAME_RE.match(agent) or not APP_SLUG_RE.match(app):
            raise DeployError(f"binding {name!r}: agent and app must be slugs")
        if name in seen:
            raise DeployError(f"binding {name!r} is declared twice")
        seen.add(name)
        out.append({"name": name, "agent": agent, "app": app})
    return out


def validate_exports(block) -> dict:
    """``{methods, snapshots, events}``, each ``{name: {description,
    min_role?}}`` (APPS.md "Bindings"): what other apps may take from this
    one, with a one-line description so ``describe_app`` can say it."""
    if block is None:
        return {}
    if not isinstance(block, dict) or any(k not in ("methods", "snapshots", "events") for k in block):
        raise DeployError("exports must be an object with methods, snapshots and/or events")
    out: dict = {}
    for kind in ("methods", "snapshots", "events"):
        entries = block.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, dict) or len(entries) > MAX_EXPORTS_PER_KIND:
            raise DeployError(f"exports.{kind} must be an object of at most {MAX_EXPORTS_PER_KIND} names")
        canon: dict = {}
        for name, spec in entries.items():
            if not isinstance(name, str) or not EXPORT_NAME_RE.match(name):
                raise DeployError(f"exports.{kind}: {name!r} is not a valid name")
            if not isinstance(spec, dict) or any(k not in ("description", "min_role") for k in spec):
                raise DeployError(f"exports.{kind}.{name}: an object with description and optional min_role")
            desc = str(spec.get("description") or "").strip()
            if not desc or len(desc) > MAX_DESCRIPTION_CHARS:
                raise DeployError(f"exports.{kind}.{name}: description required "
                                  f"(≤{MAX_DESCRIPTION_CHARS} chars)")
            entry: dict = {"description": desc}
            floor = spec.get("min_role")
            if floor is not None:
                if kind == "events":
                    raise DeployError(f"exports.events.{name}: events carry no min_role")
                if floor not in (roles.EDITOR, roles.MANAGER):
                    raise DeployError(f"exports.{kind}.{name}: min_role must be editor or manager")
                entry["min_role"] = floor
            canon[name] = entry
        if canon:
            out[kind] = canon
    return out


def validate_handlers(block, shared: bool, binding_names: set[str]) -> dict:
    """``{on_schedule: {name: {cron}}, on_trigger: [name…], on_event:
    {name: [event…]}}`` (APPS.md "Handlers"). Names are unique across the
    three kinds (one ``/_handler/<name>`` each); a cron is checked by the
    tasks' own builder; an event is a platform event or
    ``app:<binding>:<name>`` over a declared binding."""
    if block is None:
        return {}
    if not isinstance(block, dict) or any(k not in ("on_schedule", "on_trigger", "on_event")
                                          for k in block):
        raise DeployError("handlers must be an object with on_schedule, on_trigger and/or on_event")
    out: dict = {}
    seen: set[str] = set()

    def _name(n) -> str:
        if not isinstance(n, str) or not HANDLER_NAME_RE.match(n):
            raise DeployError(f"handlers: {n!r} is not a valid handler name")
        if n in seen:
            raise DeployError(f"handlers: {n!r} is declared twice")
        seen.add(n)
        if len(seen) > MAX_HANDLERS:
            raise DeployError(f"at most {MAX_HANDLERS} handlers")
        return n

    sched = block.get("on_schedule")
    if sched is not None:
        if not isinstance(sched, dict):
            raise DeployError("handlers.on_schedule must be an object of name → {cron}")
        from zoneinfo import ZoneInfo
        from services.scheduler import scheduler_triggers
        canon: dict = {}
        for n, spec in sched.items():
            name = _name(n)
            cron = spec.get("cron") if isinstance(spec, dict) else None
            if not isinstance(spec, dict) or set(spec) != {"cron"} or not isinstance(cron, str):
                raise DeployError(f"handlers.on_schedule.{name}: an object with a cron string")
            try:
                scheduler_triggers.build_cron_trigger(cron.strip(), ZoneInfo("UTC"))
            except Exception as e:
                raise DeployError(f"handlers.on_schedule.{name}: not a valid cron ({e})")
            canon[name] = {"cron": cron.strip()}
        if canon:
            out["on_schedule"] = canon
    trig = block.get("on_trigger")
    if trig is not None:
        if not isinstance(trig, list):
            raise DeployError("handlers.on_trigger must be a list of names")
        names = [_name(n) for n in trig]
        if names:
            out["on_trigger"] = names
    ev = block.get("on_event")
    if ev is not None:
        if not isinstance(ev, dict):
            raise DeployError("handlers.on_event must be an object of name → [events]")
        canon = {}
        for n, events in ev.items():
            name = _name(n)
            if not isinstance(events, list) or not events or len(events) > 16:
                raise DeployError(f"handlers.on_event.{name}: a list of one to sixteen events")
            keep: list[str] = []
            for e in events:
                if not isinstance(e, str):
                    raise DeployError(f"handlers.on_event.{name}: events are strings")
                m = APP_EVENT_RE.match(e)
                if m:
                    if m.group(1) not in binding_names:
                        raise DeployError(f"handlers.on_event.{name}: {e!r} names a binding "
                                          "the app does not declare")
                elif STEP_EVENT_RE.match(e):
                    pass    # the step it names is checked once the steps block is read
                elif CHECK_EVENT_RE.match(e):
                    pass    # a check's wake (CHECKS.md); the check is the manager's
                elif e not in PLATFORM_EVENTS:
                    raise DeployError(f"handlers.on_event.{name}: unknown event {e!r} "
                                      f"(platform events: {', '.join(PLATFORM_EVENTS)})")
                elif e == "notification_created" and shared:
                    raise DeployError(f"handlers.on_event.{name}: notification_created is for "
                                      "personal apps only (a shared app never sees an inbox)")
                if e not in keep:
                    keep.append(e)
            canon[name] = keep
        if canon:
            out["on_event"] = canon
    return out


def validate_step_run(run) -> str:
    """A step's script path: relative, inside the app folder, plain segments
    (no dot segment, no hidden file, no ``client/``, ``server/`` or
    ``data/``), so the release's own rules ship it and nothing else can be
    named."""
    if not isinstance(run, str) or not run.strip():
        raise DeployError("run must be a path under the app folder")
    run = run.strip()
    if run.startswith("/") or "\\" in run or "//" in run:
        raise DeployError(f"run {run!r}: a relative path with single slashes")
    parts = run.split("/")
    for seg in parts:
        if not STEP_RUN_SEGMENT_RE.match(seg):
            raise DeployError(f"run {run!r}: {seg!r} is not a plain file or folder name")
    if run == "app.json" or any(run.startswith(p) for p in STEP_NEVER_PREFIXES):
        raise DeployError(f"run {run!r}: a script lives beside client/ and server/, "
                          "for example scripts/<name>.sh")
    return run


def validate_steps(block, handler_names: set[str], source_dir: Path | None) -> dict:
    """``{name: {run, timeout, sha256}}`` (APPS.md "Steps"): the script a
    handler runs instead of the server route. ``name`` must be a declared
    handler; ``run`` a path under the app folder; ``timeout`` in seconds
    (default 60, at most two hours). With ``source_dir`` the script is read
    and its sha256 written into the block, so the signed manifest carries
    the content the approver saw; without it (a shape check before the
    folder exists) the hash is left out and the deploy adds it."""
    if block is None:
        return {}
    if not isinstance(block, dict) or len(block) > MAX_STEPS:
        raise DeployError(f"steps must be an object of at most {MAX_STEPS} handler names")
    out: dict = {}
    for name, spec in block.items():
        if not isinstance(name, str) or not HANDLER_NAME_RE.match(name):
            raise DeployError(f"steps: {name!r} is not a valid handler name")
        if name not in handler_names:
            raise DeployError(f"steps.{name}: not a declared handler (declare its wake under "
                              "handlers.on_schedule, on_trigger or on_event)")
        if not isinstance(spec, dict) or any(k not in ("run", "timeout") for k in spec):
            raise DeployError(f"steps.{name}: an object with run and optional timeout")
        try:
            run = validate_step_run(spec.get("run"))
        except DeployError as e:
            raise DeployError(f"steps.{name}: {e}")
        timeout = spec.get("timeout", STEP_TIMEOUT_DEFAULT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, int) \
                or not 1 <= timeout <= STEP_TIMEOUT_MAX_S:
            raise DeployError(f"steps.{name}: timeout is whole seconds, 1 to {STEP_TIMEOUT_MAX_S}")
        entry: dict = {"run": run, "timeout": int(timeout)}
        if source_dir is not None:
            try:
                data = releases.read_tree_file(source_dir, run, max_size=STEP_SCRIPT_MAX_BYTES)
            except safe_fs.FileTooLarge:
                raise DeployError(f"steps.{name}: {run} is larger than 256 KB")
            except OSError:
                raise DeployError(f"steps.{name}: {run} is not a file in the app folder")
            problem = step_script_problem(data)
            if problem:
                raise DeployError(f"steps.{name}: {run} does not parse — {problem}")
            entry["sha256"] = hashlib.sha256(data).hexdigest()
        out[name] = entry
    return out


def step_script_problem(data: bytes) -> str:
    """The interpreter's own syntax pass over a step script — ``bash -n``,
    ``sh -n``, Python's parser — so a script that cannot parse is refused at
    the check and the deploy instead of dying at its first wake (a lone
    apostrophe inside ``${VAR:?…}`` shipped that way once). A script for
    another interpreter, or one this host lacks, is not checked. Nothing is
    executed: ``-n`` reads and stops."""
    import shlex
    import shutil
    import subprocess
    first = data.split(b"\n", 1)[0].strip()
    words: list[str] = []
    if first.startswith(b"#!"):
        try:
            words = shlex.split(first[2:].decode("utf-8", "replace"))
        except ValueError:
            return "the #! line does not parse"
    interp = words[0].rsplit("/", 1)[-1] if words else "sh"
    if interp == "env" and len(words) > 1:
        interp = words[1]
    if interp in ("bash", "sh", "dash", "zsh"):
        exe = shutil.which(interp)
        cmd = [exe, "-n"] if exe else None
    elif interp in ("python", "python3"):
        exe = shutil.which(interp)
        cmd = [exe, "-c", "import ast, sys; ast.parse(sys.stdin.read())"] if exe else None
    else:
        cmd = None
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode == 0:
        return ""
    lines = [ln for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return (lines[-1] if lines else f"{interp} exited {r.returncode}")[:200]


def validate_secrets(block, egress: list[str]) -> list[dict]:
    """``[{name, required?, description?, sends_to? | env?}]`` (APPS.md
    "Secrets"): the names of the values a person sets after the deploy.
    ``sends_to`` (``{host, header, prefix?}``) names a declared egress host
    the platform adds the value to on the app's outbound calls — the value
    never enters the sandbox; ``env: true`` hands the value to the server's
    environment (the card says so in amber); neither means the platform
    alone uses it (an inbound hook's signing secret). A value is never in
    the manifest."""
    if block is None:
        return []
    if not isinstance(block, list) or len(block) > MAX_SECRETS:
        raise DeployError(f"secrets must be a list of at most {MAX_SECRETS} entries")
    out: list[dict] = []
    seen: set[str] = set()
    for entry in block:
        if not isinstance(entry, dict) or any(
                k not in ("name", "required", "description", "sends_to", "env") for k in entry):
            raise DeployError("secrets: each entry is an object with name, required, "
                              "description, and sends_to or env")
        name = entry.get("name")
        if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
            raise DeployError(f"secrets: {name!r} is not a valid secret name "
                              "(UPPER_CASE, digits and underscores, 2 to 64 characters)")
        if name in SECRET_RESERVED_NAMES or name.startswith(SECRET_RESERVED_PREFIXES):
            raise DeployError(f"secrets: {name} is a name the runtime owns")
        if name in seen:
            raise DeployError(f"secrets: {name} is declared twice")
        seen.add(name)
        required = entry.get("required", False)
        if not isinstance(required, bool):
            raise DeployError(f"secrets.{name}: required must be true or false")
        desc = entry.get("description", "")
        if not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION_CHARS:
            raise DeployError(f"secrets.{name}: description is a string of at most "
                              f"{MAX_DESCRIPTION_CHARS} characters")
        canon: dict = {"name": name, "required": required}
        if desc.strip():
            canon["description"] = desc.strip()
        sends_to = entry.get("sends_to")
        env = entry.get("env", False)
        if not isinstance(env, bool):
            raise DeployError(f"secrets.{name}: env must be true or false")
        if sends_to is not None and env:
            raise DeployError(f"secrets.{name}: sends_to and env are one or the other")
        if sends_to is not None:
            if not isinstance(sends_to, dict) or any(
                    k not in ("host", "header", "prefix") for k in sends_to):
                raise DeployError(f"secrets.{name}: sends_to is an object with host, header "
                                  "and an optional prefix")
            host = sends_to.get("host")
            host = host.strip().lower().rstrip(".") if isinstance(host, str) else ""
            if not host or host not in egress:
                raise DeployError(f"secrets.{name}: sends_to.host must be one of the app's "
                                  "egress hosts")
            header = sends_to.get("header")
            if not isinstance(header, str) or not SECRET_HEADER_RE.match(header):
                raise DeployError(f"secrets.{name}: sends_to.header is not a valid header name")
            if header.lower() in ("host", "content-length", "cookie", "set-cookie",
                                  "transfer-encoding", "connection"):
                raise DeployError(f"secrets.{name}: sends_to.header may not be {header}")
            prefix = sends_to.get("prefix", "")
            if not isinstance(prefix, str) or len(prefix) > SECRET_PREFIX_MAX_CHARS \
                    or any(ord(c) < 32 or ord(c) > 126 for c in prefix):
                raise DeployError(f"secrets.{name}: sends_to.prefix is printable text of at "
                                  f"most {SECRET_PREFIX_MAX_CHARS} characters")
            canon["sends_to"] = {"host": host, "header": header}
            if prefix:
                canon["sends_to"]["prefix"] = prefix
        elif env:
            canon["env"] = True
        out.append(canon)
    return out


def validate_inbound(block, handler_names: set[str], secret_names: set[str]) -> dict:
    """``{name: {verify, secret, handler, header?, prefix?, id_header?}}``
    (APPS.md "Inbound hooks"): a public route per hook, verified by the
    platform with the named declared secret and the named scheme, waking
    ``handler`` with the event. A hook declares its handler (as
    ``on_trigger`` does): the name is unique across the handler kinds and
    is never a step. ``hmac_sha256`` needs the signature header (an optional
    ``prefix`` before the hex digest); ``hmac_sha256`` and ``bearer`` take an
    optional ``id_header`` (the vendor's event id, for de-duplication);
    ``stripe`` and ``github`` know their own headers."""
    if block is None:
        return {}
    if not isinstance(block, dict) or len(block) > MAX_INBOUND:
        raise DeployError(f"inbound must be an object of at most {MAX_INBOUND} hook names")
    out: dict = {}
    handlers_seen: set[str] = set()
    for name, spec in block.items():
        if not isinstance(name, str) or not HANDLER_NAME_RE.match(name):
            raise DeployError(f"inbound: {name!r} is not a valid hook name")
        if not isinstance(spec, dict) or any(
                k not in ("verify", "secret", "handler", "header", "prefix", "id_header") for k in spec):
            raise DeployError(f"inbound.{name}: an object with verify, secret, handler and, for "
                              "hmac_sha256, header (plus an optional prefix and id_header)")
        verify = spec.get("verify")
        if verify not in INBOUND_SCHEMES:
            raise DeployError(f"inbound.{name}: verify is one of {', '.join(INBOUND_SCHEMES)}")
        secret = spec.get("secret")
        if not isinstance(secret, str) or secret not in secret_names:
            raise DeployError(f"inbound.{name}: secret must name a declared secret")
        handler = spec.get("handler")
        if not isinstance(handler, str) or not HANDLER_NAME_RE.match(handler):
            raise DeployError(f"inbound.{name}: {handler!r} is not a valid handler name")
        if handler in handler_names:
            raise DeployError(f"inbound.{name}: {handler!r} is already a schedule, trigger or "
                              "event handler — an inbound hook declares a handler of its own")
        handlers_seen.add(handler)
        canon: dict = {"verify": verify, "secret": secret, "handler": handler}
        header = spec.get("header")
        prefix = spec.get("prefix")
        id_header = spec.get("id_header")
        if verify == "hmac_sha256":
            if not isinstance(header, str) or not SECRET_HEADER_RE.match(header):
                raise DeployError(f"inbound.{name}: hmac_sha256 needs the signature header's name")
            canon["header"] = header
            if prefix is not None:
                if not isinstance(prefix, str) or len(prefix) > SECRET_PREFIX_MAX_CHARS \
                        or any(ord(c) < 32 or ord(c) > 126 for c in prefix):
                    raise DeployError(f"inbound.{name}: prefix is printable text of at most "
                                      f"{SECRET_PREFIX_MAX_CHARS} characters")
                if prefix:
                    canon["prefix"] = prefix
        elif header is not None or prefix is not None:
            raise DeployError(f"inbound.{name}: header and prefix are for hmac_sha256 only")
        if id_header is not None:
            if verify not in ("hmac_sha256", "bearer"):
                raise DeployError(f"inbound.{name}: id_header is for hmac_sha256 and bearer "
                                  "(stripe and github carry their own event id)")
            if not isinstance(id_header, str) or not SECRET_HEADER_RE.match(id_header):
                raise DeployError(f"inbound.{name}: id_header is not a valid header name")
            canon["id_header"] = id_header
        out[name] = canon
    if len(handler_names | handlers_seen) > MAX_HANDLERS:
        raise DeployError(f"at most {MAX_HANDLERS} handlers, inbound hooks included")
    return out


def validate_requires(block) -> dict:
    """``{mcps: [name…], providers: [provider…]}`` — what the app needs to
    work; the card says which are met, nothing is granted by it."""
    if block is None:
        return {}
    if not isinstance(block, dict) or any(k not in ("mcps", "providers") for k in block):
        raise DeployError("requires must be an object with mcps and/or providers")
    out: dict = {}
    for k in ("mcps", "providers"):
        names = _str_list(block.get(k), f"requires.{k}", MAX_REQUIRES)
        if names:
            out[k] = names
    return out


def validate_app_json(doc: dict, agent: str, shared: bool,
                      source_dir: Path | None = None) -> Manifest:
    """The whole manifest, or ``DeployError``. ``source_dir`` is the folder
    the manifest describes: with it the steps' scripts are hashed into the
    block (every stored block comes from a call with it); a shape check
    before the folder exists passes None."""
    unknown = sorted(k for k in doc if k not in ALLOWED_KEYS)
    if unknown:
        raise DeployError(f"app.json: unknown keys {unknown}")
    title = str(doc.get("title") or "").strip()
    if len(title) > 200:
        raise DeployError("app.json: title exceeds 200 characters")
    if doc.get("catalog"):
        raise DeployError("app.json: declare feeds and methods as actions "
                          "(type data_feed / platform), not under catalog")
    actions_json, err = _mf.validate_actions(doc.get("actions") or [], agent, shared)
    if actions_json is None:
        raise DeployError(f"app.json: {err}")
    # A folder app's server owns its data directory, where the per-viewer
    # documents would sit: it keeps per-viewer data in its own database.
    from api.apps.catalog import VIEWER_DATA_METHODS
    per_viewer = sorted({a.get("method") for a in json.loads(actions_json)
                         if a.get("type") == "platform" and a.get("method") in VIEWER_DATA_METHODS})
    if per_viewer:
        raise DeployError(f"app.json: {', '.join(per_viewer)} are for single-file apps; "
                          "a folder app keeps per-viewer data in its own database")
    files = validate_files(doc.get("files"), shared)
    egress = validate_egress(doc.get("egress"))
    try:
        bindings = validate_bindings(doc.get("bindings"))
        exports = validate_exports(doc.get("exports"))
        handlers = validate_handlers(doc.get("handlers"), shared, {b["name"] for b in bindings})
        names = (set(handlers.get("on_schedule") or {}) | set(handlers.get("on_trigger") or [])
                 | set(handlers.get("on_event") or {}))
        steps = validate_steps(doc.get("steps"), names, source_dir)
        for name, events in (handlers.get("on_event") or {}).items():
            for e in events:
                m = STEP_EVENT_RE.match(e)
                if not m:
                    continue
                if m.group(1) not in steps:
                    raise DeployError(f"handlers.on_event.{name}: {e!r} names a step the app "
                                      "does not declare")
                if name in steps:
                    raise DeployError(f"handlers.on_event.{name}: a step does not wake on a step")
        requires = validate_requires(doc.get("requires"))
        secrets = validate_secrets(doc.get("secrets"), egress)
        inbound = validate_inbound(doc.get("inbound"), names, {s["name"] for s in secrets})
        external = validate_external(doc.get("external"))
    except DeployError as e:
        raise DeployError(f"app.json: {e}")
    ra = doc.get("deploy_requires_approval")
    if ra is not None and not isinstance(ra, bool):
        raise DeployError("app.json: deploy_requires_approval must be true or false")
    canon = task_store.canonical_actions_json
    return Manifest(
        title=title, actions_json=actions_json,
        files_json=canon(files) if (files["read"] or files["write"]) else "",
        egress_json=canon(egress) if egress else "",
        requires_approval=ra,
        handlers_json=canon(handlers) if handlers else "",
        exports_json=canon(exports) if exports else "",
        bindings_json=canon(bindings) if bindings else "",
        requires_json=canon(requires) if requires else "",
        steps_json=canon(steps) if steps else "",
        secrets_json=canon(secrets) if secrets else "",
        inbound_json=canon(inbound) if inbound else "",
        external_json=canon(external) if external else "",
    )


# ── the folder on a satellite ───────────────────────────────────────────────


def working_manifest(row: dict, tree_dir: Path) -> Manifest | None:
    """The manifest a deploy of ``tree_dir`` (a copy of the row's working
    tree) would write: its app.json read without following a link and
    validated as the deploy validates it, the steps' scripts hashed; None
    when it would not deploy. Blocking (the step check spawns a parser)."""
    try:
        return validate_app_json(read_app_json(tree_dir), row["agent"], not row.get("username"),
                                 tree_dir)
    except DeployError:
        return None


async def pull_folder(session_id: str, rel_dir: str) -> int:
    """Bring a satellite session's working folder to the platform tree,
    file by file, within the caps; files the satellite no longer has are
    removed platform-side. Returns the number of files pulled.

    A listing with no app file (the folder is missing on the machine, never
    synced down, or holds only what a pull skips) refuses: the manifest
    cannot tell a missing folder from an emptied one, and dropping the
    platform copy on either would lose the app."""
    from core.remote import remote_file_flow
    listing = await remote_file_flow.list_remote_files(session_id, rel_dir)
    if listing is None:
        raise DeployError("could not list the folder on your machine — is the connection up?")
    wanted = []
    for rel in listing:
        inner = rel[len(rel_dir):].lstrip("/")
        parts = inner.split("/")
        if any(p.startswith(".") for p in parts) or "node_modules" in parts or parts[0] == "data":
            continue
        wanted.append((rel, inner))
    if not wanted:
        raise DeployError(f"the folder {rel_dir} is missing or empty on your machine; "
                          "nothing was changed")
    if len(wanted) > PULL_MAX_FILES:
        raise DeployError(f"the folder has more than {PULL_MAX_FILES} files — trim it "
                          "(node_modules and data are never counted)")
    info = remote_file_flow._get_remote_session_info(session_id)
    agent_dir = config.get_agent_dir(info.agent_name) if info else None
    total = 0
    pulled = 0
    for rel, _inner in wanted:
        path = await remote_file_flow.pull_through(session_id, rel)
        if path is None:
            raise DeployError(f"could not read {rel} from your machine")
        pulled += 1
        with contextlib.suppress(OSError):
            total += path.stat().st_size
        if total > PULL_MAX_BYTES:
            raise DeployError(f"the folder is larger than {PULL_MAX_BYTES // (1024 * 1024)} MB")
    if agent_dir is not None:
        try:
            root = path_confinement.join_under(agent_dir, rel_dir)
        except path_confinement.PathOutsideRoot:
            raise DeployError("the folder is outside your workspace")
        keep = {inner for _rel, inner in wanted}

        def _drop_absent() -> None:
            present = releases.walk_tree(root) if root.is_dir() else []
            for rel_path, _p in present:
                if rel_path not in keep:
                    try:
                        agents_root, rel = releases.agents_rel(root / rel_path)
                        safe_fs.unlink_beneath(agents_root, rel, missing_ok=True)
                    except OSError as e:
                        logger.warning("App deploy: %s was not removed (%s)", rel_path, e.strerror or e)

        try:
            await asyncio.to_thread(_drop_absent)
        except releases.ReleaseInvalid as e:
            raise DeployError(str(e))
    return pulled


# ── the database ────────────────────────────────────────────────────────────


def _plain_file_or_absent(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    return stat.S_ISREG(st.st_mode)


def snapshot_db(src: Path, dst: Path) -> bool:
    """A consistent copy through the SQLite backup API (a WAL database
    another process holds open is fine); False when there is nothing.

    The data directory is the app's own server's to write, so the database
    and its WAL and shm siblings are checked to be plain files right before
    the open: SQLite opens by name and would follow a link the app planted
    into another app's or another person's database."""
    for cand in (src, Path(str(src) + "-wal"), Path(str(src) + "-shm")):
        if not _plain_file_or_absent(cand):
            raise DeployError("the app's database is not a plain file, so the deploy "
                              "stops here and nothing is read through it")
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(tmp)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error as e:
        tmp.unlink(missing_ok=True)
        raise DeployError(f"the app's database could not be copied ({e}) — the deploy "
                          "stops here so nothing is lost")
    tmp.replace(dst)
    return True


def restore_db(snapshot: Path, target: Path) -> None:
    """Put a snapshot in the database's place and drop the stale WAL and
    shm files (a replaced main file under an old WAL is corrupt). The copy
    lands beneath the agents root with no link followed and replaces the
    name atomically: the data directory is the app's own to write, so a
    link planted at the database's name is replaced, never written through."""
    root = config.AGENTS_DIR
    target_rel = safe_fs.rel_under(target, root)
    for suffix in ("-wal", "-shm"):
        safe_fs.unlink_beneath(root, target_rel + suffix, missing_ok=True)
    safe_fs.copy_file_beneath(root, safe_fs.rel_under(snapshot, root), root, target_rel,
                              mkdirs=True)


def changed_files(old_dir: Path | None, new_dir: Path) -> dict:
    """What a release changes against another: from the two manifests."""
    new = (releases.read_manifest(new_dir) or {}).get("files", {})
    old = (releases.read_manifest(old_dir) or {}).get("files", {}) if old_dir else {}
    added = sorted(p for p in new if p not in old)
    removed = sorted(p for p in old if p not in new)
    changed = sorted(p for p in new if p in old and new[p].get("sha256") != old[p].get("sha256"))
    return {"added": added, "removed": removed, "changed": changed}


QUOTA_HEADROOM = 0.95


def quota_preflight(row: dict, source_dir: Path, release_bytes: int | None = None) -> str:
    """Why the deploy would not fit (APPS.md "Quota"): on the hard tier the
    release copy plus a database copy must stay under 95 % of the bucket's
    limit. The soft tier and an unlimited scope skip it. Empty = fits."""
    from services.infra import storage_quota as sq
    if not sq.hard_enabled():
        return ""
    username = row.get("username") or ""
    scope_type = "user" if username else "shared"
    limit, _inodes = sq.limits_for(scope_type)
    if not limit:
        return ""
    key = sq.user_scope_key(row["agent"], username) if username else sq.shared_scope_key(row["agent"])
    project = sq.get_project_id(key)
    usage = sq.report_usage(project) if project is not None else None
    if usage is None:
        return ""
    used, _ = usage
    if release_bytes is None:
        release_bytes = releases.tree_bytes(source_dir)
    db = releases.app_data_dir(row) / "app.db"
    db_bytes = db.stat().st_size if db.is_file() else 0
    if used + release_bytes + db_bytes > limit * QUOTA_HEADROOM:
        need = (used + release_bytes + db_bytes) // (1024 * 1024)
        have = int(limit * QUOTA_HEADROOM) // (1024 * 1024)
        return (f"the release and a copy of the database would not fit under the storage "
                f"limit ({need} MB needed, {have} MB allowed) — free space or raise the quota")
    return ""


def _prune_rollback_snapshots(row: dict) -> None:
    base = releases.app_release_dir(row)
    snaps = sorted(base.glob("rollback-*.sqlite"), key=lambda p: p.name)
    for old in snaps[:-KEEP_ROLLBACK_SNAPSHOTS] if len(snaps) > KEEP_ROLLBACK_SNAPSHOTS else []:
        old.unlink(missing_ok=True)


# ── the pipeline ────────────────────────────────────────────────────────────


def _deploy_lock(app_id: str) -> asyncio.Lock:
    from api.apps.apps import _deploy_lock
    return _deploy_lock(app_id)


async def _notify_pending(row: dict, n: int, waiting: str = "", *, lowers: bool = False) -> None:
    from services.notifications.notification_manager import fire_notification
    from storage.identity import db_users
    if row.get("username"):
        targets = [row.get("owner_sub") or ""]
    else:
        targets = [u["sub"] for u in await run_db(db_users.get_agent_users, row["agent"])
                   if roles.can_edit(u.get("agent_role"))]
    title = f"Release {n} of {row.get('title') or row.get('slug')} is waiting for approval"
    body = "Open the app to see what changed and approve or reject the release."
    if lowers:
        body = ("This release turns off approval for every deploy: approving it lets later "
                "deploys of the app go live without asking.")
    if waiting:
        body = f"{waiting}: set it in the app's settings, then approve the release."
    for sub in [t for t in targets if t]:
        await fire_notification(title, body, severity="info", scope="user", target=sub,
                                source="app", source_id=row["id"], agent_slug=row["agent"],
                                href=f"/apps/{row['id']}")


async def go_live(row: dict, n: int, *, start: bool = True) -> dict:
    """Take release ``n`` live (the caller holds the row's deploy lock):
    snapshot the database, start the new server next to the old one, switch
    on health, point the row, prune, announce. A failed start removes the
    copy and raises ``DeployError``; the old release keeps serving. A
    template seed passes ``start=False`` (APPS.md "Blueprints and
    templates"): the row is pointed and the server starts on the first
    open (``app_supervisor.ensure_up``), never at the seed."""
    new_dir = releases.app_release_dir(row) / str(n)
    if not (new_dir / releases.MANIFEST_NAME).is_file():
        raise DeployError(f"release {n} is not on disk")
    # APPS.md "Secrets": a release never goes live while a required secret
    # has no value (the approve route refuses with the same words).
    from services.apps import app_secrets
    waiting = await asyncio.to_thread(app_secrets.waiting_reason, row)
    if waiting:
        raise DeployError(f"{waiting} — set it in the app's settings first")
    db = releases.app_data_dir(row) / "app.db"
    await asyncio.to_thread(snapshot_db, db, releases.db_before_path(row, n))
    if start and app_sandbox.server_entry(new_dir) and task_store.app_actions_approved(row):
        try:
            await app_supervisor.start(row, app_supervisor.LIVE, release_dir=new_dir)
        except (app_sandbox.AppStartError, app_supervisor.AppUnavailable) as e:
            await asyncio.to_thread(shutil.rmtree, new_dir, True)
            await run_db(task_store.set_deploy_state, row["id"], deploy_state=task_store.DEPLOY_IDLE,
                         pending_release=0)
            raise DeployError(f"the new release did not start, the previous one keeps "
                              f"serving: {e}")
    rel, sha = await asyncio.to_thread(releases.point_to, row, n)
    fresh = await run_db(task_store.set_app_release, row["id"], rel, sha) or row
    fresh = await run_db(task_store.set_deploy_state, fresh["id"], deploy_state=task_store.DEPLOY_IDLE,
                         pending_release=0) or fresh
    await asyncio.to_thread(releases.prune, fresh)
    # The handler schedules follow the manifest that just went live.
    from services.apps import app_handlers
    try:
        await app_handlers.sync_rows(fresh)
    except Exception:
        logger.exception("App %s: handler rows out of step after go-live", row.get("slug"))
    from api.apps.apps import announce_deploy
    screens = await announce_deploy(fresh, n, file_updated=False)
    logger.info("App deployed: app=%s release=%d screens=%d", row.get("slug"), n, screens)
    return {"status": RESULT_OK, "app_id": row["id"], "release": n, "screens": screens}


async def deploy_folder(agent: str, username: str, owner_sub: str | None, slug: str,
                        source_dir: Path, rel_dir: str, *, session_id: str = "",
                        cold: bool = False, approve_as: str = "",
                        template_ref: str | None = None) -> dict:
    """The whole pipeline for one working folder; raises ``DeployError``
    with the reason for the agent.

    A template seed runs it ``cold`` (APPS.md "Blueprints and templates"):
    no rendered check (the catalog's gate looked at the app) and no server
    start (the first open starts it); with ``approve_as`` the manifest is
    approved on behalf of that person, whose consent the seeder already
    judged (the target checks a human approval makes run here too, and a
    failing one leaves the release pending with the reason in
    ``consent_refused``); ``template_ref`` stamps the row."""
    confine_to_scope(agent, username, source_dir)
    if session_id:
        from core.remote import remote_file_flow
        if remote_file_flow.is_remote_session(session_id):
            pulled = await pull_folder(session_id, rel_dir)
            logger.info("App deploy: pulled %d file(s) of %s from the satellite", pulled, rel_dir)
    if not source_dir.is_dir():
        raise DeployError(f"no folder at apps/{slug} in your scope")
    # The tree first (no link anywhere in it), then its manifest and page,
    # each read without following a link.
    try:
        release_bytes = await asyncio.to_thread(releases.tree_bytes, source_dir)
    except releases.ReleaseInvalid as e:
        raise DeployError(str(e))
    doc = await asyncio.to_thread(read_app_json, source_dir)
    await asyncio.to_thread(require_entry_page, source_dir)
    existing = await run_db(task_store.get_app_by_slug, agent, username, slug)
    # A deploy while a release waits for approval REPLACES the waiting copy:
    # nothing of it went live, the card re-renders on the new state, and the
    # agent that saw a problem in the pictures ships the fix at once instead
    # of asking the user to approve a release it already wants to replace
    # (found on the internal install, 2026-09-14). The replacement takes a
    # new number, so a card still showing the old copy cannot approve it.
    superseded = 0
    if existing is not None:
        if not task_store.app_kind_of(existing).serves_tree and (existing.get("release_path") or
                                                          existing.get("rel_path", "").endswith(".html")):
            raise DeployError(f"'{slug}' is pinned as a single-file app — unpin it first, "
                              "or use another slug for the folder")
        superseded = int(existing.get("pending_release") or 0)
    elif await run_db(task_store.count_apps, agent, username) >= task_store.MAX_APPS_PER_SCOPE:
        # The same cap a single-file pin meets (``pins.py``): a folder row
        # is a tab on the same strip.
        raise DeployError(f"app limit reached ({task_store.MAX_APPS_PER_SCOPE}) — unpin one first")
    m = await asyncio.to_thread(validate_app_json, doc, agent, not username, source_dir)
    # The static checks (APPS.md "Deploy pipeline"): a problem the sandbox
    # would hit refuses the deploy before any row is touched, with the file
    # and the line for the agent; warnings ride in the answer.
    from services.apps import app_lint
    try:
        files = await asyncio.to_thread(releases.walk_tree, source_dir)
    except releases.ReleaseInvalid as e:
        raise DeployError(str(e))
    findings = await asyncio.to_thread(app_lint.lint_tree, source_dir, files,
                                       app_lint.declared_from_manifest(m))
    lint = app_lint.summary(findings)
    if lint["problems"]:
        return {"status": RESULT_REFUSED, "slug": slug, **lint}
    row = await run_db(
        task_store.upsert_app, agent, username, owner_sub, slug,
        title=m.title or (slug if existing is None else None),
        rel_path=rel_dir, actions_json=m.actions_json, kind=task_store.APP_KIND_FOLDER,
        blocks=m.blocks(), template_ref=template_ref,
    )
    # APPS.md "The contract": a deploy raises the per-app switch at once and
    # never lowers it: a release whose app.json says false parks, and the
    # person who approves that release lowers it (``approve_pending``).
    if m.requires_approval and not row.get("deploy_requires_approval"):
        row = await run_db(task_store.set_deploy_state, row["id"], requires_approval=True) or row
    consent_refused = ""
    if approve_as and not task_store.app_actions_approved(row):
        from api.apps.apps import _manifest_target_error
        from auth.providers import user_context_for_sub
        approver = await asyncio.to_thread(user_context_for_sub, approve_as)
        consent_refused = (await asyncio.to_thread(_manifest_target_error, row, approver)
                           if approver else "the person who consented is gone")
        if not consent_refused:
            await run_db(task_store.approve_app_actions, row["id"],
                         task_store.manifest_sig(row), approve_as)
            row = await run_db(task_store.get_app, row["id"]) or row
    async with _deploy_lock(row["id"]):
        # The row as it stands now: an approval that held the lock while
        # this deploy waited took the waiting copy live, and a live release
        # is not this deploy's to replace.
        row = await run_db(task_store.get_app, row["id"]) or row
        # Raised again under the lock: an approval that lowered the switch
        # while this deploy waited must not let this release go live unasked.
        if m.requires_approval and not row.get("deploy_requires_approval"):
            row = await run_db(task_store.set_deploy_state, row["id"], requires_approval=True) or row
        if superseded and int(row.get("pending_release") or 0) == superseded:
            await asyncio.to_thread(shutil.rmtree, releases.app_release_dir(row) / str(superseded), True)
            row = await run_db(task_store.set_deploy_state, row["id"], deploy_state=task_store.DEPLOY_IDLE,
                               pending_release=0) or row
        else:
            superseded = 0
        why = await asyncio.to_thread(quota_preflight, row, source_dir, release_bytes)
        if why:
            await _restore_live_manifest(row)
            raise DeployError(why)
        try:
            rel, sha, n = await asyncio.to_thread(releases.cut_folder_release, row, source_dir,
                                                  after=superseded)
        except releases.ReleaseInvalid as e:
            await _restore_live_manifest(row)
            raise DeployError(str(e))
        approved = task_store.app_actions_approved(row)
        if not cold:
            # The rendered check of the copy that ships (APPS.md "Deploy
            # pipeline"): a page that fails when loaded never goes live and
            # never parks for approval either; the release copy goes with it.
            from services.apps import app_render
            report = await app_render.render_tree(row, releases.app_release_dir(row) / str(n),
                                                  approved=approved, seed=app_render.seeds_live_data(row, m))
            if report.status == "hard":
                await asyncio.to_thread(shutil.rmtree, releases.app_release_dir(row) / str(n), True)
                await _restore_live_manifest(row)
                return {"status": RESULT_REFUSED, "slug": slug, "app_id": row["id"],
                        "render": report.as_dict(), **lint}
            lint["render"] = report.as_dict()
        if consent_refused:
            lint["consent_refused"] = consent_refused
        # APPS.md "Secrets": a required secret without a value parks the
        # release like an unapproved manifest; the answer says which.
        from services.apps import app_secrets
        waiting = await asyncio.to_thread(app_secrets.waiting_reason, row)
        lowers = m.requires_approval is False and bool(row.get("deploy_requires_approval"))
        if bool(row.get("deploy_requires_approval")) or not approved or waiting:
            row = await run_db(task_store.set_deploy_state, row["id"], deploy_state=task_store.DEPLOY_PENDING,
                               pending_release=n) or row
            # One notification per waiting episode: a deploy that replaces a
            # copy already on the card tells the approver nothing new (three
            # deploys in a row rang three times on the internal install).
            if not superseded:
                try:
                    await _notify_pending(row, n, waiting, lowers=lowers)
                except Exception:
                    logger.exception("App %s: pending-release notification failed", slug)
            if lowers:
                lint = {**lint, "lowers_approval": True, "reason": LOWERS_APPROVAL}
            return {"status": RESULT_PENDING_APPROVAL, "app_id": row["id"], "release": n,
                    "manifest_changed": not approved, "live_release": releases.current_number(row),
                    "superseded": superseded, "waiting": waiting, **lint}
        return {**await go_live(row, n, start=not cold), "superseded": superseded, **lint}


async def approve_pending(row: dict, approver_sub: str, expect_sig: str | None = None,
                          expect_release: int | None = None) -> dict:
    """Approve the pending release (the human route): the manifest it
    carries becomes the approved one in the same step, then it goes live.
    ``expect_sig`` / ``expect_release`` name what the person was shown (the
    card's body; by default the row the caller judged). The row is read
    again under the deploy lock: a deploy that replaced the waiting copy or
    its manifest meanwhile refuses the click instead of riding it."""
    if expect_sig is None:
        expect_sig = task_store.manifest_sig(row)
    if expect_release is None:
        expect_release = int(row.get("pending_release") or 0)
    async with _deploy_lock(row["id"]):
        row = await run_db(task_store.get_app, row["id"])
        if row is None:
            raise DeployError("the app is gone")
        n = int(row.get("pending_release") or 0)
        if not n:
            raise DeployError("no release is waiting for approval")
        if n != expect_release or task_store.manifest_sig(row) != expect_sig:
            raise DeployError("the release changed — review it again")
        # A required secret without a value: the click sets nothing live
        # and approves nothing — the card says what to set first.
        from services.apps import app_secrets
        waiting = await asyncio.to_thread(app_secrets.waiting_reason, row)
        if waiting:
            raise DeployError(f"{waiting} — set it in the app's settings first")
        # A release that turns the switch off (APPS.md "The contract") lowers
        # it once it is live, and only then: a release that fails to start
        # leaves the switch on for the next deploy.
        lowers = bool(row.get("deploy_requires_approval")) and await asyncio.to_thread(
            release_lowers_approval, row, n)
        if not task_store.app_actions_approved(row):
            if not await run_db(task_store.approve_app_actions, row["id"], expect_sig, approver_sub):
                raise DeployError("the release changed — review it again")
            row = await run_db(task_store.get_app, row["id"]) or row
        out = await go_live(row, n)
        if lowers:
            await run_db(task_store.set_deploy_state, row["id"], requires_approval=False)
            logger.info("App %s: approval turned off by the approval of release %d", row.get("slug"), n)
        return out


def release_lowers_approval(row: dict, n: int) -> bool:
    """Whether release ``n``'s own app.json turns the per-app switch off. The
    copy is the platform's (the deploy wrote it into the release tree) and it
    is the code that goes live, so its word is the release's; anything that
    cannot be read lowers nothing. Synchronous."""
    try:
        doc = read_app_json(releases.app_release_dir(row) / str(n))
    except DeployError:
        return False
    return doc.get("deploy_requires_approval") is False


async def _restore_live_manifest(row: dict) -> dict:
    """Put the live release's manifest back on the row: the deploy wrote the
    new one before anything was decided, and a rejected or refused release
    must not leave it there — it would void the approval of the release
    that keeps serving and put a manifest no release carries on the card.
    An approved new manifest stays (its signature is the stored one)."""
    if task_store.app_actions_approved(row):
        return row
    try:
        live = await asyncio.to_thread(releases.live_release_dir, row)
    except releases.ReleaseDamaged:
        live = None
    if live is None:
        return row
    try:
        m = await asyncio.to_thread(_manifest_of, live, row)
    except DeployError:
        return row
    return await run_db(task_store.upsert_app, row["agent"], row.get("username") or "",
                        row.get("owner_sub"), row["slug"], actions_json=m.actions_json,
                        blocks=m.blocks()) or row


def _manifest_of(release_dir: Path, row: dict) -> Manifest:
    """A release's own manifest, validated (the step check spawns a parser:
    a worker thread's job, never the loop's). Synchronous."""
    return validate_app_json(read_app_json(release_dir), row["agent"],
                             shared=not row.get("username"), source_dir=release_dir)


async def reject_pending(row: dict) -> dict:
    """Remove the pending copy and put the live release's manifest back on
    the row (the deploy had written the new one)."""
    n = int(row.get("pending_release") or 0)
    if not n:
        raise DeployError("no release is waiting for approval")
    async with _deploy_lock(row["id"]):
        await asyncio.to_thread(shutil.rmtree, releases.app_release_dir(row) / str(n), True)
        row = await run_db(task_store.set_deploy_state, row["id"], deploy_state=task_store.DEPLOY_IDLE,
                           pending_release=0) or row
        row = await _restore_live_manifest(row)
        return {"status": RESULT_REJECTED, "app_id": row["id"], "release": n}


async def rollback_folder(row: dict) -> dict:
    """Serve the previous release again with the database as it was before
    the current one went live (the caller holds the deploy lock); the
    current database is snapshotted first so nothing is lost for good."""
    if int(row.get("pending_release") or 0):
        raise DeployError("a release is waiting for approval — approve or reject it first")
    cur = releases.current_number(row)
    prev = releases.previous_number(row)
    if prev is None:
        raise DeployError("no previous release")
    await app_supervisor.stop(row["id"], app_supervisor.LIVE)
    db = releases.app_data_dir(row) / "app.db"
    stamp = time.strftime("%Y%m%dT%H%M%S")
    snapshotted = await asyncio.to_thread(snapshot_db, db, releases.rollback_snapshot_path(row, stamp))
    await asyncio.to_thread(_prune_rollback_snapshots, row)
    before = releases.db_before_path(row, cur)
    restored = False
    if before.is_file():
        await asyncio.to_thread(restore_db, before, db)
        restored = True
    rel, sha = await asyncio.to_thread(releases.point_to, row, prev)
    fresh = await run_db(task_store.set_app_release, row["id"], rel, sha) or row
    # The manifest the previous release carries is the served one again;
    # the handler schedules follow it (a departed handler's cron stops).
    try:
        live_dir = await asyncio.to_thread(releases.live_release_dir, fresh)
        m = await asyncio.to_thread(_manifest_of, live_dir, fresh)
        fresh = await run_db(task_store.upsert_app, fresh["agent"], fresh.get("username") or "",
                             fresh.get("owner_sub"), fresh["slug"], actions_json=m.actions_json,
                             blocks=m.blocks()) or fresh
    except (DeployError, releases.ReleaseDamaged):
        logger.exception("App %s: the previous release's manifest did not read back", row.get("slug"))
    from services.apps import app_handlers
    try:
        await app_handlers.sync_rows(fresh)
    except Exception:
        logger.exception("App %s: handler rows out of step after rollback", row.get("slug"))
    from api.apps.apps import announce_deploy
    screens = await announce_deploy(fresh, prev, file_updated=False)
    logger.info("App rolled back: app=%s release=%d db_restored=%s snapshot=%s",
                row.get("slug"), prev, restored, snapshotted)
    return {"release": prev, "screens": screens, "db_restored": restored,
            "snapshot": f"rollback-{stamp}.sqlite" if snapshotted else ""}


async def cut_preview(row: dict, source_dir: Path) -> str:
    """Copy the working tree to the preview slot with its own data, seeded
    from the live database only when the copied code would go live on it
    without a person's review anyway (``app_render.seeds_live_data`` on the
    copy's own manifest: the preview starts on the row's approval alone,
    so it can run code nobody reviewed); returns the preview's tree hash."""
    from services.apps import app_render
    confine_to_scope(row["agent"], row.get("username") or "", source_dir)
    await app_supervisor.stop(row["id"], app_supervisor.PREVIEW)
    pd = releases.preview_dir(row)

    def _copy() -> str:
        releases.copy_tree(source_dir, pd, replace=True)
        live_db = releases.app_data_dir(row) / "app.db"
        if app_render.seeds_live_data(row, working_manifest(row, pd)) and live_db.is_file():
            snapshot_db(live_db, pd / "data" / "app.db")
        return releases.tree_sha(pd)

    try:
        return await asyncio.to_thread(_copy)
    except releases.ReleaseInvalid as e:
        raise DeployError(str(e))


def status(row: dict) -> dict:
    """What ``deploy_status`` and the dashboard card show."""
    pending = int(row.get("pending_release") or 0)
    out = {
        "deploy_state": row.get("deploy_state") or task_store.DEPLOY_IDLE,
        "release": releases.current_number(row),
        "pending_release": pending,
        "manifest_approved": task_store.app_actions_approved(row),
        "deploy_requires_approval": bool(row.get("deploy_requires_approval")),
        "deploying": _deploy_lock(row["id"]).locked(),
        **app_supervisor.status(row["id"]),
    }
    # The last wakes (APPS.md "Handlers"), so a dead delivery is read here
    # without the database.
    from services.apps import app_handlers
    try:
        out["wakes"] = app_handlers.wakes(row)
    except Exception:
        out["wakes"] = []
    # APPS.md "Secrets": each declared name with whether it is set (never a
    # value), and why the release waits when a required one is not.
    from services.apps import app_secrets
    try:
        out["secrets"] = [{k: v for k, v in s.items()
                           if k in ("name", "required", "set", "declared", "sends_to", "env")}
                          for s in app_secrets.status_for(row)]
        out["waiting"] = app_secrets.waiting_reason(row)
    except Exception:
        logger.exception("App %s: the secrets status failed", row.get("slug"))
        out["secrets"], out["waiting"] = [], ""
    if pending:
        try:
            live = releases.live_release_dir(row)
        except releases.ReleaseDamaged:
            live = None
        out["changes"] = changed_files(live, releases.app_release_dir(row) / str(pending))
        if row.get("deploy_requires_approval") and release_lowers_approval(row, pending):
            out["lowers_approval"] = True
    return out
