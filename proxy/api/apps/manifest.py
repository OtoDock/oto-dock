"""Declared-actions manifest — validation + run authority (pinned mini-apps).

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

APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
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
# one_time targets hard-delete themselves after the first successful fire
# (scheduler finally-cleanup); continuation/delegate targets are nonsense
# for a button. Trigger-type is the canonical "button task" shape.
ALLOWED_TASK_TYPES = {"scheduled", "trigger"}
# Read-only platform feeds an app may subscribe to via ``otodock.feed`` —
# answered by the HOST PAGE from the viewer's own authenticated context
# (dashboard AppFrame), never by the sandboxed frame itself. Declared in the
# manifest so the approval card surfaces them; the allowlist is the entire
# attack surface, so additions need their own review.
ALLOWED_DATA_FEEDS = {"active_chats", "project_lanes"}


def check_task_target(task_id: str, agent: str, shared: bool) -> str:
    """'' when the task is a valid fire_task target for this app, else the
    reason. Called at pin, approve, AND exec time (the task may have been
    edited, rescoped, or deleted since approval)."""
    dyn = task_store.get_dynamic_task(task_id)
    if not dyn:
        return "fire_task target not found"
    if (dyn.get("agent") or "") != agent:
        return "fire_task target belongs to another agent"
    if (dyn.get("task_type") or "") not in ALLOWED_TASK_TYPES:
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
    for m in (mcp_registry.get_agent_mcps(agent, is_remote=False) or []):
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
            out.append(entry)
        elif atype == "send_prompt":
            prompt = str(a.get("prompt") or "").strip()
            if not prompt or len(prompt) > MAX_PROMPT_CHARS:
                return None, f"action {aid!r}: prompt required (≤{MAX_PROMPT_CHARS} chars)"
            out.append({"id": aid, "label": label, "type": "send_prompt",
                        "prompt": prompt})
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
            out.append(entry)
        elif atype == "data_feed":
            feed = str(a.get("feed") or "")
            if feed not in ALLOWED_DATA_FEEDS:
                return None, (f"action {aid!r}: unknown feed {feed!r} "
                              f"(available: {sorted(ALLOWED_DATA_FEEDS)})")
            out.append({"id": aid, "label": label, "type": "data_feed",
                        "feed": feed})
        else:
            return None, f"action {aid!r}: unknown type {atype!r}"
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
    on the agent; platform admin always."""
    if not sub:
        return False
    u = task_store.get_user(sub)
    if not u:
        return False
    if (u.get("role") or "") == "admin":
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == sub
    role = task_store.get_user_agent_roles(sub).get(row.get("agent") or "", "")
    return role in ("manager", "editor")


def sub_can_run_task(sub: str, dyn: dict) -> bool:
    """DB-reconstructed run authority for a stored ``approved_by`` — the
    approver isn't in the request, so their CURRENT platform + per-agent
    role is read back. A demoted approver fails here → "approval stale"."""
    if not sub:
        return False
    u = task_store.get_user(sub)
    if not u:
        return False
    if (u.get("role") or "") == "admin":
        return True
    role = task_store.get_user_agent_roles(sub).get(dyn.get("agent") or "", "")
    if not role:
        return False
    own = (dyn.get("created_by") or "") == sub
    if (dyn.get("scope") or "user") == "user":
        return own
    return role == "manager" or (own and role in ("manager", "editor"))
