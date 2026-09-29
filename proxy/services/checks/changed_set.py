"""The changed set (CHECKS.md "Conditions"): what the turn touched, from the
tool record the platform kept (``session_events.tool_records``) — the paths
in their tree-relative form, their kinds, the events the commands and the
tool calls stand for, the places — plus the request and the result. One
JSON document, the input of every kind: a file for a script
(``OTODOCK_CHECK_INPUT``), inlined for a judge, the body for a handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core import placement
from core.events import tool_roles
from core.placement import PlacementCapabilities
from services.checks import classify, patterns
from core import layout

logger = logging.getLogger("checks")

MAX_PATHS = 200
MAX_REQUEST_CHARS = 4096
MAX_RESULT_CHARS = 8192
GIT_PROBES_MAX = 8
_TREE_HEADS = layout.FILE_HEADS


@dataclass
class Target:
    """The judged session, resolved once per turn (CHECKS.md "One handler")."""
    session_id: str
    kind: str                   # chats | tasks | delegations
    agent: str
    chat: dict
    username: str = ""
    user_sub: str = ""
    role: str = "viewer"
    scope: str = "user"         # the mount scope
    engine: str = ""
    client_type: str = ""
    security: object = None
    # The session's resolved placement (``core.placement``): the document
    # says ``kind`` = ``placement.site`` (local | machine), the machine id
    # and its name.
    placement: PlacementCapabilities = field(default_factory=PlacementCapabilities)
    cwd: str = ""               # sandbox-virtual, or the machine's work_cwd
    work_cwd: str = ""
    run: dict | None = None     # the task run row, for a task or a delegation

    @property
    def chat_id(self) -> str:
        return str(self.chat.get("id") or "")

    @property
    def model(self) -> str:
        return str(self.chat.get("model") or "")

    @property
    def on_machine(self) -> bool:
        """The turn ran on a machine (a known one — a remote kind without a
        machine id probes the platform copy, as the checks always did)."""
        return self.placement.site == placement.SITE_MACHINE

    @property
    def place_name(self) -> str:
        """The machine's name for the card, its short id when unnamed."""
        return self.placement.label or self.placement.machine_id[:8]


def _relative_of(raw: str, security) -> str:
    """The tree-relative form of a path the tool saw: the sandbox-virtual
    form locally, the satellite-native form remotely; the policy resolver
    normalizes both. Outside the trees: ``""``."""
    try:
        from services import path_policy_v2 as ppv2
        ctx = ppv2.context_from_security(security)
        res = ppv2.resolve_path_for_session(ctx, raw)
    except Exception:
        return ""
    rel = (res.sandbox_relative or "").strip("/")
    if rel and rel.split("/")[0] not in _TREE_HEADS:
        rel = ""
    return rel


def _local_git_root(host_path: str, agent_dir: Path) -> str:
    """Walk up from a host path for a real checkout (``.git/HEAD`` — the
    empty ``.git`` mountpoint a user tree carries is not one), staying
    inside the agent's tree; the tree-relative root or ``""``."""
    if not host_path:
        return ""
    try:
        p = Path(host_path)
        if not p.is_absolute():
            return ""
        base = agent_dir.resolve()
        cur = p if p.is_dir() else p.parent
        for _ in range(64):
            if (cur / ".git" / "HEAD").is_file():
                try:
                    return cur.resolve().relative_to(base).as_posix()
                except ValueError:
                    return cur.as_posix()
            if cur == base or cur == cur.parent:
                return ""
            cur = cur.parent
    except OSError:
        return ""
    return ""


async def _remote_git_root(machine_id: str, native_path: str, agent: str, cache: dict) -> str:
    """The same walk on a machine, through the satellite's ``file_stat``
    (files only, so ``.git/HEAD`` is the probe), at most a handful per turn."""
    try:
        from core.remote.satellite_connection import get_connection_manager
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        if not cm.is_connected(machine_id) or not cm.satellite_supports_file_stat(machine_id):
            return ""
        cur = native_path.replace("\\", "/")
        if not cur.startswith("/") and not re.match(r"^[A-Za-z]:/", cur):
            return ""
        cur = cur.rsplit("/", 1)[0] if "." in cur.rsplit("/", 1)[-1] else cur
        for _ in range(16):
            if cur in cache:
                if cache[cur]:
                    return cur
            else:
                if cache.get("__probes__", 0) >= GIT_PROBES_MAX:
                    return ""
                cache["__probes__"] = cache.get("__probes__", 0) + 1
                st = await cm.stat_file(machine_id, PathRef("satellite_host", f"{cur}/.git/HEAD"),
                                        agent_slug=agent, timeout=5.0)
                cache[cur] = bool(st and st.get("exists"))
                if cache[cur]:
                    return cur
            if "/" not in cur.strip("/"):
                return ""
            cur = cur.rsplit("/", 1)[0] or "/"
            if cur == "/" or re.match(r"^[A-Za-z]:/?$", cur):
                return ""
    except Exception:
        logger.debug("checks: remote git probe failed", exc_info=True)
    return ""


async def build(target: Target, records: list, *, check_name: str, round_no: int,
                request: str, result: str) -> dict:
    """The changed set of one turn for one check."""
    import config
    agent_dir = Path(config.get_agent_dir(target.agent))
    paths: list[dict] = []
    seen: set[str] = set()
    events: set[str] = set()
    tools: list[dict] = []
    git_roots: list[str] = []
    remote_cache: dict = {}
    for rec in records:
        writes = tool_roles.writes(rec.tool_name)  # a write or a delete, by role
        cmd_events = classify.classify_command(rec.command) if rec.command else set()
        events |= cmd_events
        events |= classify.classify_tool(rec.tool_name)
        tools.append({
            "name": rec.tool_name,
            **({"command": rec.command} if rec.command else {}),
            "paths": list(rec.paths), "is_error": bool(rec.is_error),
        })
        for raw in rec.paths:
            if raw in seen or len(paths) >= MAX_PATHS:
                continue
            seen.add(raw)
            rel = _relative_of(raw, target.security)
            entry = {"path": raw, "relative": rel or None, "kind": classify.classify_path(raw),
                     "tool": rec.tool_name, "writes": writes}
            paths.append(entry)
            if writes:
                if not target.on_machine:
                    # The tool saw the sandbox-virtual form; the walk runs
                    # over the platform's own copy of the tree.
                    root = await asyncio.to_thread(
                        _local_git_root, str(agent_dir / rel) if rel else "", agent_dir)
                else:
                    root = await _remote_git_root(target.placement.machine_id, raw, target.agent,
                                                  remote_cache)
                if root and root not in git_roots:
                    git_roots.append(root)
    project = ""
    if target.work_cwd:
        project = target.work_cwd
    elif target.chat.get("project_id"):
        project = f"{workspace_relative_of(target)}/projects/{target.chat['project_id']}"
    roots = {
        "workspace": layout.WORKSPACE,
        "user_workspace": layout.scope_workspace(target.username) if target.username else "",
        "knowledge": layout.KNOWLEDGE,
    }
    return {
        "check": check_name,
        "round": round_no,
        "session": {
            "id": target.session_id, "chat_id": target.chat_id, "agent": target.agent,
            "username": target.username, "scope": target.scope, "kind": target.kind,
            "engine": target.engine, "client_type": target.client_type,
            "placement": {"kind": target.placement.site, "machine_id": target.placement.machine_id,
                          "name": target.place_name},
            "cwd": target.cwd, "roots": roots,
        },
        "paths": paths,
        "events": sorted(events),
        "tools": tools,
        "places": {"project": project, "git_roots": git_roots},
        "request": (request or "")[-MAX_REQUEST_CHARS:],
        "result": (result or "")[-MAX_RESULT_CHARS:],
    }


def condition_matches(condition: dict, changed: dict) -> bool:
    """CHECKS.md "Conditions": no condition = any file written or any
    event; ``always`` = every turn; kinds, events, places, globs and command
    patterns each narrow it (all named facets must hold; within a facet any
    entry matches)."""
    cond = condition or {}
    if cond.get("always"):
        return True
    written = [p for p in changed.get("paths") or [] if p.get("writes")]
    events = set(changed.get("events") or [])
    if not any(k in cond for k in ("kinds", "events", "places", "globs", "commands")):
        return bool(written or events)
    kinds = cond.get("kinds") or []
    if kinds and not (("any" in kinds and written)
                      or any(p.get("kind") in kinds for p in written)):
        return False
    want_events = cond.get("events") or []
    if want_events and not (events & set(want_events)):
        return False
    places = cond.get("places") or []
    if places:
        pl = changed.get("places") or {}
        ok = False
        if "git" in places and pl.get("git_roots"):
            ok = True
        if "project" in places and pl.get("project"):
            proj = str(pl["project"]).rstrip("/") + "/"
            ok = ok or any((p.get("relative") or p.get("path") or "").startswith(proj)
                           for p in written)
        if not ok:
            return False
    globs = cond.get("globs") or []
    if globs and not any(classify.matches_glob(p.get("relative") or p.get("path") or "", g)
                         for p in written for g in globs):
        return False
    commands = cond.get("commands") or []
    if commands:
        texts = [t.get("command") or "" for t in changed.get("tools") or []]
        if not any(patterns.search(c, t) for c in commands for t in texts if t):
            return False
    return True


def input_path_env() -> str:
    """The env name a script reads the changed set from."""
    return "OTODOCK_CHECK_INPUT"


def workspace_relative_of(target: Target) -> str:
    """The session's working tree, relative to the agent folder: the
    person's workspace for a personal session, the agent's for a shared one
    (a logged-in person's shared session still carries their name)."""
    if target.scope == "user" and target.username:
        return layout.scope_workspace(target.username)
    return layout.WORKSPACE


def is_native_path(p: str) -> bool:
    return os.path.isabs(p) and not p.startswith(layout.TREE_ROOTS)
