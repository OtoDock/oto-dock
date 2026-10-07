"""Declared-actions manifest — validation + run authority (pinned apps).

The manifest is the ONLY bridge from app-page JS to platform actions:
buttons invoke by declared id, never free-form. Approval is what delegates
a fire_task to every app viewer, so the APPROVER must hold the exact
``/v1/tasks/{id}/run`` authority for each target — checked live at approve
time and re-checked (DB-reconstructed, the approver may be offline or
demoted) at every execution.

Shared between the pin hook (api/hooks), the CRUD/exec routes (api/apps)
and the WS send_prompt path (ws/artifact_interactions).
"""

import json
import re

from auth.providers import UserContext
from storage import database as task_store
from core import placement
from auth import roles
from services.scheduler import task_kinds

APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# CHECKS.md: a check's name on a button's ``checks`` list.
CHECK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MCP_TOOL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SCHEMA_PROP_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,39}$")
MAX_ACTIONS = 16
MAX_LABEL_CHARS = 80
MAX_PROMPT_CHARS = 4000
MAX_MANIFEST_BYTES = 32 * 1024
MAX_FIXED_ARGS_BYTES = 4 * 1024
MAX_SCHEMA_BYTES = 4 * 1024
MAX_SCHEMA_PROPS = 16
MAX_STRING_MAXLENGTH = 4000
MAX_ENUM_VALUES = 50
MAX_ENUM_STR_CHARS = 200
MAX_SCHEMA_DESC_CHARS = 500
# Which task kinds a button may target is a fact on the kind
# (``task_kinds.TaskKind.button_target``): one_time targets hard-delete
# themselves after the first successful fire, continuation / delegate
# targets are nonsense for a button; a trigger task is the canonical
# "button task" shape, a scheduled one may be pressed too.
# Read-only platform feeds an app may subscribe to via ``otodock.feed`` —
# answered by the HOST PAGE from the viewer's own authenticated context
# (dashboard AppFrame) or by the platform as the viewer, never by the
# sandboxed frame itself. Declared in the manifest so the approval card
# surfaces them; the catalog (``api/apps/catalog.py``) is the entire attack
# surface, so additions need their own review.
from api.apps.catalog import (  # noqa: E402
    AUDIENCE_METHOD as _CATALOG_AUDIENCE,
    FEEDS as _CATALOG_FEEDS,
    METHODS as _CATALOG_METHODS,
)

ALLOWED_DATA_FEEDS = frozenset(_CATALOG_FEEDS)
ALLOWED_PLATFORM_METHODS = frozenset(_CATALOG_METHODS)
# Per-action role floor (``min_role``, APPS.md): the default floor is
# every viewer of the app and is never stored, so a manifest that names it
# signs exactly like one that omits it.
ACTION_ROLES = roles.AGENT_ROLES


def caller_role(row: dict, user: UserContext | None) -> str:
    """The role an action floor is judged against. A cookie principal's
    per-agent role (the owner of a personal app is its manager; a member of
    the app's agent acts at their row there); a person a share admits holds
    the role the share gives them (SHARING.md: a person share's cap; an
    agent or department placement's cap, capped by their role on the
    receiving agent; the strongest share admitting them, whichever panel
    lists the app), read from the store; anyone else is a viewer. Every bearer principal
    (session JWT, master key, delegation callers) clamps to viewer whatever
    its owner's role, so a prompt never drives a floored button; admin
    passes every floor. The share read is a DB round trip: call it off the
    loop for a viewer who is neither owner nor member."""
    if user is None or getattr(user, "is_api_key", False):
        return roles.VIEWER
    if user.is_admin:
        return roles.ADMIN
    if row.get("username"):
        if (row.get("owner_sub") or "") == user.sub:
            return roles.MANAGER
    elif user.can_access_agent(row.get("agent") or ""):
        return user.acting_role(row.get("agent") or "")
    from storage.sharing import share_store
    role = share_store.effective_share_role(row.get("id") or "", user.sub,
                                            user.agent_roles or {}, False)
    return role or roles.VIEWER


def meets_floor(action: dict, role: str) -> bool:
    return roles.meets_floor(role, action.get("min_role"))


def floor_reason(action: dict) -> str:
    return f"this action needs the {action.get('min_role') or roles.VIEWER} role"


def _min_role(a: dict, aid: str) -> tuple[str | None, str]:
    raw = a.get("min_role")
    if raw in (None, roles.NO_ACCESS, roles.VIEWER):
        return None, ""
    if raw not in ACTION_ROLES:
        return None, f"action {aid!r}: min_role must be viewer, contributor, editor or manager"
    return raw, ""


def check_task_target(task_id: str, agent: str, shared: bool) -> str:
    """'' when the task is a valid fire_task target for this app, else the
    reason. Called at pin, approve, AND exec time (the task may have been
    edited, rescoped, or deleted since approval)."""
    dyn = task_store.get_dynamic_task(task_id)
    if not dyn:
        return "fire_task target not found"
    if (dyn.get("agent") or "") != agent:
        return "fire_task target belongs to another agent"
    target_kind = task_kinds.of_word(dyn.get("task_type"))
    if target_kind is None or not target_kind.button_target:
        return "fire_task target must be a scheduled or trigger task"
    if shared and (dyn.get("scope") or "user") != "agent":
        return "a shared app can only fire agent-scoped tasks"
    return ""


def assigned_mcp_keys(agent: str) -> dict[str, str]:
    """The MCPs an ``mcp_tool`` action may target: visible AND enabled for the
    agent on a LOCAL placement. ``get_agent_mcps``' fail-closed defaults drop
    satellite-only / device-capability MCPs — headless execution runs inside
    the proxy process (like Direct-LLM), so a device MCP must never be a
    button target. Checked at pin, approve, AND exec time.

    Maps BOTH the manifest name (``display-mcp``) and the mcpServers key
    (``server_name or name`` — the segment agents see in their tool names,
    ``mcp__display__…``) to the CANONICAL key: tools execute by
    ``mcp__<key>__<tool>``, so the stored manifest carries the key. A stored
    canonical value maps to itself — exec re-checks with
    ``keys.get(mcp) == mcp``."""
    from services.mcp import mcp_registry
    out: dict[str, str] = {}
    for m in (mcp_registry.get_agent_mcps(agent, placement=placement.LOCAL_PLACEMENT) or []):
        key = getattr(m, "server_name", "") or m.name
        out[m.name] = key
        out[key] = key
    return out


_SCHEMA_ROOT_KEYS = {"type", "properties", "required", "additionalProperties"}
_SCHEMA_PROP_KEYS = {"type", "enum", "maxLength", "minLength",
                     "minimum", "maximum", "description"}
_SCHEMA_TYPES = {"string", "integer", "number", "boolean"}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_args_schema(schema) -> tuple[dict | None, str]:
    """Validate + normalize a declared ``args_schema`` — a deliberately
    BOUNDED JSON-Schema subset (flat object of scalars), hand-checked instead
    of handed to a full validator: the schema is the load-bearing gate between
    page-controlled args and a real tool call, so no ``$ref``/``pattern``/
    nesting surface is admitted at all. ``additionalProperties: false`` is
    FORCED. Strings must be enum-valued or carry ``maxLength`` — every arg is
    length-bounded by construction."""
    if not isinstance(schema, dict):
        return None, "args_schema must be an object"
    if set(schema) - _SCHEMA_ROOT_KEYS:
        bad = sorted(set(schema) - _SCHEMA_ROOT_KEYS)
        return None, f"args_schema: unsupported keys {bad}"
    if schema.get("type") != "object":
        return None, 'args_schema: type must be "object"'
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None, "args_schema: properties object required"
    if len(props) > MAX_SCHEMA_PROPS:
        return None, f"args_schema: at most {MAX_SCHEMA_PROPS} properties"
    norm_props: dict[str, dict] = {}
    for name, p in props.items():
        if not isinstance(name, str) or not SCHEMA_PROP_RE.match(name):
            return None, f"args_schema: invalid property name {name!r}"
        if not isinstance(p, dict):
            return None, f"args_schema: property {name!r} must be an object"
        if set(p) - _SCHEMA_PROP_KEYS:
            bad = sorted(set(p) - _SCHEMA_PROP_KEYS)
            return None, f"args_schema: property {name!r}: unsupported keys {bad}"
        ptype = p.get("type")
        if ptype not in _SCHEMA_TYPES:
            return None, f"args_schema: property {name!r}: type must be one of {sorted(_SCHEMA_TYPES)}"
        np: dict = {"type": ptype}
        enum = p.get("enum")
        if enum is not None:
            if ptype == "boolean":
                return None, f"args_schema: property {name!r}: enum not allowed for boolean"
            if not isinstance(enum, list) or not enum or len(enum) > MAX_ENUM_VALUES:
                return None, f"args_schema: property {name!r}: enum must be 1..{MAX_ENUM_VALUES} values"
            for ev in enum:
                if ptype == "string":
                    if not isinstance(ev, str) or len(ev) > MAX_ENUM_STR_CHARS:
                        return None, f"args_schema: property {name!r}: enum values must be strings ≤{MAX_ENUM_STR_CHARS} chars"
                elif ptype == "integer":
                    if not isinstance(ev, int) or isinstance(ev, bool):
                        return None, f"args_schema: property {name!r}: enum values must be integers"
                elif not _is_num(ev):
                    return None, f"args_schema: property {name!r}: enum values must be numbers"
            np["enum"] = enum
        if ptype == "string":
            max_len = p.get("maxLength")
            if max_len is None and enum is None:
                return None, f"args_schema: property {name!r}: string requires maxLength (or enum)"
            if max_len is not None:
                if not isinstance(max_len, int) or isinstance(max_len, bool) \
                        or not 1 <= max_len <= MAX_STRING_MAXLENGTH:
                    return None, f"args_schema: property {name!r}: maxLength must be 1..{MAX_STRING_MAXLENGTH}"
                np["maxLength"] = max_len
            min_len = p.get("minLength")
            if min_len is not None:
                if not isinstance(min_len, int) or isinstance(min_len, bool) \
                        or min_len < 0 or (max_len is not None and min_len > max_len):
                    return None, f"args_schema: property {name!r}: invalid minLength"
                np["minLength"] = min_len
        elif ptype in ("integer", "number"):
            lo, hi = p.get("minimum"), p.get("maximum")
            for label, v in (("minimum", lo), ("maximum", hi)):
                if v is not None and not _is_num(v):
                    return None, f"args_schema: property {name!r}: {label} must be a number"
            if lo is not None and hi is not None and lo > hi:
                return None, f"args_schema: property {name!r}: minimum > maximum"
            if lo is not None:
                np["minimum"] = lo
            if hi is not None:
                np["maximum"] = hi
        else:  # boolean
            if any(k in p for k in ("maxLength", "minLength", "minimum", "maximum")):
                return None, f"args_schema: property {name!r}: bounds not allowed for boolean"
        desc = p.get("description")
        if desc is not None:
            if not isinstance(desc, str) or len(desc) > MAX_SCHEMA_DESC_CHARS:
                return None, f"args_schema: property {name!r}: description ≤{MAX_SCHEMA_DESC_CHARS} chars"
            np["description"] = desc
        norm_props[name] = np
    required = schema.get("required", [])
    if not isinstance(required, list) or len(set(required)) != len(required) \
            or not all(isinstance(r, str) and r in norm_props for r in required):
        return None, "args_schema: required must list declared property names"
    norm: dict = {"type": "object", "properties": norm_props,
                  "additionalProperties": False}
    if required:
        norm["required"] = required
    if len(task_store.canonical_actions_json(norm).encode("utf-8")) > MAX_SCHEMA_BYTES:
        return None, "args_schema too large"
    return norm, ""


def validate_args(schema: dict, args) -> tuple[dict | None, str]:
    """Exec-time gate: validate page-supplied ``args`` against a stored
    (already-normalized) ``args_schema``. Fail-closed: unknown keys, missing
    required, and any type/enum/bounds mismatch reject the call."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "args must be an object"
    props = schema.get("properties") or {}
    unknown = sorted(set(args) - set(props))
    if unknown:
        return None, f"unknown args {unknown}"
    missing = [r for r in schema.get("required", []) if r not in args]
    if missing:
        return None, f"missing required args {missing}"
    for name, v in args.items():
        p = props[name]
        ptype = p["type"]
        if ptype == "string":
            if not isinstance(v, str):
                return None, f"arg {name!r} must be a string"
            if "maxLength" in p and len(v) > p["maxLength"]:
                return None, f"arg {name!r} too long"
            if "minLength" in p and len(v) < p["minLength"]:
                return None, f"arg {name!r} too short"
        elif ptype == "integer":
            if not isinstance(v, int) or isinstance(v, bool):
                return None, f"arg {name!r} must be an integer"
        elif ptype == "number":
            if not _is_num(v):
                return None, f"arg {name!r} must be a number"
        elif ptype == "boolean":
            if not isinstance(v, bool):
                return None, f"arg {name!r} must be a boolean"
        if ptype in ("integer", "number"):
            if "minimum" in p and v < p["minimum"]:
                return None, f"arg {name!r} below minimum"
            if "maximum" in p and v > p["maximum"]:
                return None, f"arg {name!r} above maximum"
        if "enum" in p and v not in p["enum"]:
            return None, f"arg {name!r} not in enum"
    return dict(args), ""


def merge_fixed_args(fixed: dict, args: dict) -> dict:
    """Declared ``fixed_args`` win — pin-time validation rejects key overlap
    with ``args_schema``, so this is a pure union; the override order is
    belt-and-braces for rows predating that rule."""
    return {**args, **fixed}


def validate_actions(actions, agent: str, shared: bool) -> tuple[str | None, str]:
    """Normalize + validate a declared-actions manifest. Returns
    (canonical_json, "") or (None, reason)."""
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        return None, "actions must be a list"
    if len(actions) > MAX_ACTIONS:
        return None, f"at most {MAX_ACTIONS} actions"
    seen: set[str] = set()
    out: list[dict] = []
    available_mcps: dict[str, str] | None = None  # lazy — most manifests have none
    for a in actions:
        if not isinstance(a, dict):
            return None, "each action must be an object"
        aid = str(a.get("id") or "")
        if not ACTION_ID_RE.match(aid):
            return None, f"invalid action id {aid!r}"
        if aid in seen:
            return None, f"duplicate action id {aid!r}"
        seen.add(aid)
        label = str(a.get("label") or "").strip()
        if not label or len(label) > MAX_LABEL_CHARS:
            return None, f"action {aid!r}: label required (≤{MAX_LABEL_CHARS} chars)"
        min_role, err = _min_role(a, aid)
        if err:
            return None, err
        atype = a.get("type")
        if atype == "fire_task":
            task_id = str(a.get("task_id") or "")
            err = check_task_target(task_id, agent, shared)
            if err:
                return None, f"action {aid!r}: {err}"
            entry = {"id": aid, "label": label, "type": "fire_task",
                     "task_id": task_id}
            if a.get("args_schema") is not None:
                schema, err = validate_args_schema(a["args_schema"])
                if err:
                    return None, f"action {aid!r}: {err}"
                entry["args_schema"] = schema
            if a.get("checks") is not None:
                # CHECKS.md: the checks the press attaches to the run, by
                # name; inside the signed actions text. Resolved at the
                # press (a missing check is a visible error verdict).
                names = a.get("checks")
                if (not isinstance(names, list) or len(names) > 8
                        or any(not isinstance(n, str) or not CHECK_NAME_RE.match(n) for n in names)):
                    return None, f"action {aid!r}: checks is a list of up to eight check names"
                if names:
                    entry["checks"] = list(dict.fromkeys(names))
        elif atype == "send_prompt":
            prompt = str(a.get("prompt") or "").strip()
            if not prompt or len(prompt) > MAX_PROMPT_CHARS:
                return None, f"action {aid!r}: prompt required (≤{MAX_PROMPT_CHARS} chars)"
            entry = {"id": aid, "label": label, "type": "send_prompt",
                     "prompt": prompt}
        elif atype == "mcp_tool":
            if available_mcps is None:
                available_mcps = assigned_mcp_keys(agent)
            mcp = available_mcps.get(str(a.get("mcp") or "")) or ""
            tool = str(a.get("tool") or "")
            if not mcp:
                return None, (f"action {aid!r}: MCP {a.get('mcp')!r} is not "
                              f"available to this agent")
            if not MCP_TOOL_RE.match(tool):
                return None, f"action {aid!r}: invalid tool name"
            fixed = a.get("fixed_args")
            if fixed is None:
                fixed = {}
            if not isinstance(fixed, dict):
                return None, f"action {aid!r}: fixed_args must be an object"
            try:
                fixed_json = task_store.canonical_actions_json(fixed)
            except (TypeError, ValueError):
                return None, f"action {aid!r}: fixed_args not JSON-serializable"
            if len(fixed_json.encode("utf-8")) > MAX_FIXED_ARGS_BYTES:
                return None, f"action {aid!r}: fixed_args too large"
            entry = {"id": aid, "label": label, "type": "mcp_tool",
                     "mcp": mcp, "tool": tool, "fixed_args": fixed}
            if a.get("args_schema") is not None:
                schema, err = validate_args_schema(a["args_schema"])
                if err:
                    return None, f"action {aid!r}: {err}"
                overlap = sorted(set(fixed) & set(schema["properties"]))
                if overlap:
                    return None, (f"action {aid!r}: fixed_args and args_schema "
                                  f"overlap on {overlap}")
                entry["args_schema"] = schema
        elif atype == "data_feed":
            feed = str(a.get("feed") or "")
            if feed not in ALLOWED_DATA_FEEDS:
                return None, (f"action {aid!r}: unknown feed {feed!r} "
                              f"(available: {sorted(ALLOWED_DATA_FEEDS)})")
            entry = {"id": aid, "label": label, "type": "data_feed", "feed": feed}
        elif atype == "platform":
            method = str(a.get("method") or "")
            if method not in ALLOWED_PLATFORM_METHODS:
                return None, (f"action {aid!r}: unknown platform method {method!r} "
                              f"(available: {sorted(ALLOWED_PLATFORM_METHODS)})")
            entry = {"id": aid, "label": label, "type": "platform", "method": method}
            # A write through the platform carries the workspace tier's floor
            # (a contributor writes the shared workspace) unless the author
            # asked for more (APPS.md "Files"); the path is still judged by
            # the caller's own role at the file API.
            if method == "files.write" and not min_role:
                min_role = "contributor"
            # The audience names other people: a person reads it at editor
            # or above whatever the author declared (the app itself reads
            # it unattended), so the card's chip says so.
            if method == _CATALOG_AUDIENCE and roles.rank(min_role) < roles.rank(roles.EDITOR):
                min_role = roles.EDITOR
            if a.get("args_schema") is not None:
                schema, err = validate_args_schema(a["args_schema"])
                if err:
                    return None, f"action {aid!r}: {err}"
                entry["args_schema"] = schema
        else:
            return None, f"action {aid!r}: unknown type {atype!r}"
        if min_role:
            entry["min_role"] = min_role
        out.append(entry)
    canonical = task_store.canonical_actions_json(out)
    if len(canonical.encode("utf-8")) > MAX_MANIFEST_BYTES:
        return None, "manifest too large"
    return canonical, ""


def parse_actions(row: dict) -> list[dict]:
    try:
        actions = json.loads(row.get("actions") or "[]")
        return actions if isinstance(actions, list) else []
    except (TypeError, ValueError):
        return []


def find_action(row: dict, action_id: str) -> dict | None:
    for a in parse_actions(row):
        if a.get("id") == action_id:
            return a
    return None


def parse_egress(row: dict) -> list[str]:
    """The approved egress hosts of a folder app (APPS.md), [] when none."""
    try:
        hosts = json.loads(row.get("egress") or "[]")
    except (TypeError, ValueError):
        return []
    return [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []


def parse_files(row: dict) -> dict[str, list[str]]:
    """The declared file prefixes (``{"read": [...], "write": [...]}``)."""
    try:
        block = json.loads(row.get("files") or "{}")
    except (TypeError, ValueError):
        return {"read": [], "write": []}
    if not isinstance(block, dict):
        return {"read": [], "write": []}
    return {k: [p for p in (block.get(k) or []) if isinstance(p, str)]
            for k in ("read", "write")}


def _parse_block(row: dict, name: str, kind: type):
    """A stored manifest block as its canonical shape, or the empty shape
    (the columns hold canonical JSON text written by the deploy validator;
    anything else reads as empty)."""
    try:
        block = json.loads(row.get(name) or ("[]" if kind is list else "{}"))
    except (TypeError, ValueError):
        return kind()
    return block if isinstance(block, kind) else kind()


def parse_handlers(row: dict) -> dict:
    """``{"on_schedule": {name: {"cron": …}}, "on_trigger": [name…],
    "on_event": {name: [event…]}}`` (APPS.md "Handlers"), keys present only
    when declared."""
    return _parse_block(row, "handlers", dict)


def handler_names(row: dict) -> set[str]:
    """Every handler the manifest declares: the three kinds of the
    ``handlers`` block and the handler each inbound hook names (APPS.md
    "Inbound hooks")."""
    h = parse_handlers(row)
    return (set(h.get("on_schedule") or {}) | set(h.get("on_trigger") or [])
            | set(h.get("on_event") or {})
            | {spec.get("handler") for spec in parse_inbound(row).values()
               if isinstance(spec.get("handler"), str)})


def parse_inbound(row: dict) -> dict:
    """``{name: {verify, secret, handler, header?, prefix?, id_header?}}``
    (APPS.md "Inbound hooks"): the public routes a vendor may call."""
    return {k: v for k, v in _parse_block(row, "inbound", dict).items()
            if isinstance(k, str) and isinstance(v, dict)}


SESSION_DAYS_DEFAULT = 30


def parse_external(row: dict) -> dict:
    """``{"links": [host…], "challenge": [path…], "session_days": n}``
    (APPS.md "External links"), every key present with its default."""
    block = _parse_block(row, "external", dict)
    links = [h for h in (block.get("links") or []) if isinstance(h, str)]
    paths = [p for p in (block.get("challenge") or []) if isinstance(p, str)]
    days = block.get("session_days")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        days = SESSION_DAYS_DEFAULT
    return {"links": links, "challenge": paths, "session_days": int(days)}


def challenge_path(row: dict, path: str) -> bool:
    """Whether ``path`` (the app's own API path, no query) is one the
    manifest marks for a challenge — the entry itself or below it."""
    p = (path.split("?", 1)[0] or "/").rstrip("/") or "/"
    for entry in parse_external(row)["challenge"]:
        if p == entry or p.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def parse_exports(row: dict) -> dict:
    """``{"methods": {…}, "snapshots": {…}, "events": {…}}`` (APPS.md
    "Bindings"), each value ``{"description": …, "min_role"?: …}``."""
    return _parse_block(row, "exports", dict)


def exported_method(row: dict, path: str) -> dict | None:
    """The signed ``exports.methods`` entry the FIRST segment of ``path``
    names, for the broker and for a placed agent's session (APPS.md
    "Bindings", "Agents call apps"): split as sent, no ``lstrip``, so ``/x``
    and ``//x`` name nothing; None for an unexported path."""
    methods = parse_exports(row).get("methods") or {}
    entry = methods.get(path.split("/", 1)[0])
    return entry if isinstance(entry, dict) else None


def parse_bindings(row: dict) -> list[dict]:
    """``[{"name", "agent", "app"}…]`` — the apps this one calls."""
    return [b for b in _parse_block(row, "bindings", list) if isinstance(b, dict)]


def parse_requires(row: dict) -> dict:
    """``{"mcps": [name…], "providers": [provider…]}`` — what the app
    needs to work; informative, never signed."""
    return _parse_block(row, "requires", dict)


def parse_steps(row: dict) -> dict:
    """``{name: {"run": …, "timeout": …, "sha256": …}}`` (APPS.md "Steps"):
    the handlers that run a script instead of the server route."""
    return _parse_block(row, "steps", dict)


def is_step(row: dict, handler: str) -> bool:
    return handler in parse_steps(row)


def parse_secrets(row: dict) -> list[dict]:
    """``[{name, required, description?, sends_to?: {host, header, prefix?},
    env?}]`` (APPS.md "Secrets"): the declared names and where the platform
    sends each value — never a value."""
    return [s for s in _parse_block(row, "secrets", list)
            if isinstance(s, dict) and isinstance(s.get("name"), str)]


def user_can_run_task(u: UserContext, dyn: dict) -> bool:
    """The live-caller variant of the ``/v1/tasks/{id}/run`` permission rule
    (user-scope → creator only; agent-scope → manager any, editor own)."""
    agent = dyn.get("agent") or ""
    if not u.can_access_agent(agent) and not u.is_admin:
        return False
    own = (dyn.get("created_by") or "") == u.sub
    if (dyn.get("scope") or "user") == "user":
        return own or u.is_admin
    return u.can_manage_agent(agent) or (own and u.can_edit_agent(agent))


def sub_can_approve_surface(sub: str, row: dict) -> bool:
    """DB-reconstructed APP-surface approval authority for a stored
    ``approved_by`` — re-checked at every ``mcp_tool`` execution (a demoted
    approver's standing delegation must die, exactly like fire_task's
    ``sub_can_run_task``). Personal rows → the owner; shared rows → editor+
    on the agent; platform admin always, except on a personal row whose
    owner lost the agent: a dormant app (APPS.md "Lifecycle") counts nobody's
    authority, so a link or a button an admin once enabled on it stops too."""
    if not sub:
        return False
    u = task_store.get_user(sub)
    if not u:
        return False
    if row.get("username") and task_store.personal_row_dormant(row):
        return False
    role = roles.effective_role(u.get("role"), task_store.get_user_agent_roles(sub), row.get("agent") or "")
    if roles.is_admin(role):
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == sub
    return roles.can_edit(role)


def sub_can_run_task(sub: str, dyn: dict) -> bool:
    """DB-reconstructed run authority for a stored ``approved_by`` — the
    approver isn't in the request, so their CURRENT platform + per-agent
    role is read back. A demoted approver fails here → "approval stale"."""
    if not sub:
        return False
    u = task_store.get_user(sub)
    if not u:
        return False
    role = roles.effective_role(u.get("role"), task_store.get_user_agent_roles(sub), dyn.get("agent") or "")
    if roles.is_admin(role):
        return True
    if not role:
        return False
    own = (dyn.get("created_by") or "") == sub
    if (dyn.get("scope") or "user") == "user":
        return own
    return roles.can_manage(role) or (own and roles.can_edit(role))
