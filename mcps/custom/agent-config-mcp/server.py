"""Agent-Config-MCP — agents inspect + modify their own settings from chat.

Stdio MCP server exposing tools that mutate the calling agent's own row
(``display_name``, ``description``, ``color``, ``default_model``,
``default_layer``, ``execution_paths``) and let the agent declare its
post-install setup complete via :func:`complete_setup`.

Designed to be auto-assigned to every agent at creation time (manifest
``category=core``, ``assignment_mode=auto`` resolved via the existing
core-MCP auto-assignment in ``api/agents.py``).

Permission resolution lives in :func:`_resolve_tool_set` and runs once at
module load using the auto-injected ``OTO_*`` env vars (mirror of
``mcps-mcp/server.py``):

- ``viewer`` (any scope) → ``complete_setup`` only; viewers don't manage
  agents, but they do walk their own per-user onboarding.
- ``editor``/``manager``/``admin`` → the full tool set (the proxy refuses
  the owner-tier writes an editor cannot make, per call).
- ``scope=agent`` — a person's chat on a Shared-only agent, or a user-tied
  phone route on one, carrying that person's own role — → the same rows as
  above. The manifest's ``exclude_from`` keeps this MCP out of task,
  trigger, meeting and external-caller sessions, so no service session
  ever loads it.

All HTTP calls go through ``PROXY_URL`` + ``PROXY_API_KEY`` (auto-injected,
session-scoped JWT) so the platform applies the calling user's role server
side. Local checks are best-effort defense.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

# The sibling module (the tool schemas, pure data): found on sys.path[0] when
# the MCP runs as ``python server.py`` from its folder, and put there by the
# proxy's test loader while this module executes.
from agent_config_tool_schemas import _TOOL_SCHEMAS  # noqa: E402


# ---------------------------------------------------------------------------
# Env + permission matrix
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
ROLE = os.environ.get("OTO_ROLE", "")

# The tier questions the tools gate on, answered by the proxy in the env
# (core/sandbox/oto_env.py): a separate process cannot import the proxy
# and carries no role vocabulary of its own.
CAN_MANAGE = os.environ.get("OTO_CAN_MANAGE_AGENT", "") == "true"
CAN_EDIT = os.environ.get("OTO_CAN_EDIT_AGENT", "") == "true"
SCOPE = os.environ.get("OTO_SCOPE", "")
# Non-empty for task-fired sessions (scheduled / one-time / trigger /
# delegated worker): the "unattended" signal the persona write keys on.
TASK_TYPE = os.environ.get("OTO_TASK_TYPE", "")

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

_READ_TOOLS = {
    "get_agent_config",
    "list_available_models",
    "list_context_files",
    "get_memory_settings",
    "list_knowledge_libraries",
}
_WRITE_TOOLS = {
    # Platform-role tools (admin/creator, server-enforced; CRITICAL tier —
    # every call needs the human's in-chat approval):
    "share_knowledge_folder",
    "attach_knowledge_library",
    "detach_knowledge_library",
    "set_department",
    "update_persona",
    "update_display_name",
    "update_description",
    "update_color",
    "update_default_model",
    "update_execution_layers",
    "update_default_layer",
    "update_default_scope",
    "update_default_execution_mode",
    "set_visibility_mode",
    "update_user_memory_enabled",
    "update_agent_memory_enabled",
    "complete_setup",
}


def _resolve_tool_set() -> set[str]:
    if ROLE and not CAN_EDIT:
        # A person below the editor tier (a viewer, a contributor) gets
        # exactly complete_setup: per-user onboarding (user-setup.md)
        # targets the default-attach audience, who join as viewers — the
        # endpoint's user scope only ever touches the caller's own file.
        # Everything else stays manager-tier.
        #
        # Scope-independent on purpose: a Shared-only agent mounts agent-scope
        # for HUMAN chats too (OTO_SCOPE=="agent" with a real viewer driving),
        # so keying on scope advertised a surface every endpoint then refused.
        # No service session loads this MCP (the manifest excludes task,
        # meeting and external), so only a person's session reaches here.
        return {"complete_setup"}
    if SCOPE in ("user", "agent"):
        return _READ_TOOLS | _WRITE_TOOLS
    return set()


ENABLED_TOOLS = _resolve_tool_set()

HEX_COLOR_REGEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# HTTP helper (mirrors mcps-mcp pattern)
# ---------------------------------------------------------------------------

class _ApiError(RuntimeError):
    pass


async def _request(method: str, path: str, **kwargs) -> Any:
    headers = kwargs.pop("headers", {}) or {}
    if API_KEY and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if AGENT_NAME and "X-Agent-Name" not in headers:
        headers["X-Agent-Name"] = AGENT_NAME
    url = f"{PROXY_URL}{path}"
    timeout = kwargs.pop("timeout", 10.0)
    # One retry by default (a 5xx or a network error waits a second and tries
    # again); a caller that must answer fast — tools/list — asks for one attempt.
    attempts = max(1, int(kwargs.pop("attempts", 2)))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 500 and attempt < attempts - 1:
                await asyncio.sleep(1.0)
                continue
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    js = resp.json()
                    detail = js.get("detail") or js
                except Exception:
                    pass
                raise _ApiError(f"{method} {path} → {resp.status_code}: {detail}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        except _ApiError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(1.0)
                continue
            raise _ApiError(f"{method} {path}: {exc}") from exc
    if last_exc:
        raise _ApiError(f"{method} {path}: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# The engine catalog — the platform's registered engines, from
# ``GET /v1/execution-layers`` (a dict keyed by engine id, each value the
# engine's descriptor). Read once per process: the registry is static for the
# proxy's lifetime, and this MCP lives for one session. Nothing here names an
# engine — a fourth engine the platform registers is valid the moment it
# appears in the catalog, and the interactive gate reads its descriptor.
#
# ``None`` when the proxy cannot be reached: every reader then defers to the
# proxy, which validates every PATCH itself (an unknown engine id fails
# closed there), so the only thing lost is a hint in the tool schema.
# ---------------------------------------------------------------------------

_ENGINE_CATALOG: dict[str, dict] | None = None


async def _engine_catalog() -> dict[str, dict] | None:
    global _ENGINE_CATALOG
    if _ENGINE_CATALOG is not None:
        return _ENGINE_CATALOG
    try:
        # One short attempt: tools/list is the first thing every session does
        # and must not block on the default retry (~21 s against a dead proxy).
        data = await _request("GET", "/v1/execution-layers", timeout=3.0, attempts=1)
    except _ApiError:
        return None
    if not isinstance(data, dict) or not data:
        return None
    _ENGINE_CATALOG = {k: v for k, v in data.items() if isinstance(v, dict)}
    return _ENGINE_CATALOG


async def _valid_layers() -> list[str] | None:
    """The engine ids the platform registers, sorted; None without a catalog."""
    catalog = await _engine_catalog()
    return sorted(catalog) if catalog is not None else None


async def _interactive_layers() -> list[str] | None:
    """The engines with a native TUI (``runtime.supports_interactive_pty`` on
    their descriptor) — the only ones a default session mode applies to."""
    catalog = await _engine_catalog()
    if catalog is None:
        return None
    return sorted(
        path for path, layer in catalog.items()
        if (layer.get("runtime") or {}).get("supports_interactive_pty")
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _tool_get_agent_config() -> str:
    info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
    if not isinstance(info, dict):
        return "❌ Error: unexpected response from /v1/agents/{name}/info"
    lines = [f"# Agent config — `{AGENT_NAME}`"]
    for key in ("display_name", "description", "color",
                "default_model", "default_effort",
                "execution_path", "execution_paths",
                "community_template", "community_template_version",
                "setup_completed_at"):
        if key in info and info[key] not in (None, ""):
            value = info[key]
            if isinstance(value, list):
                value = ", ".join(value)
            lines.append(f"- **{key}**: {value}")
    return "\n".join(lines)


async def _tool_list_available_models() -> str:
    """List every model available across the platform, grouped by execution
    layer. Each layer is annotated as **currently enabled** for this agent
    OR **available — would need update_execution_layers first**, so the LLM
    can plan the right multi-step update.

    Endpoint shape: ``GET /v1/execution-layers`` returns a dict keyed by
    engine id (the engines the platform registers) where each value carries
    ``display_name`` + ``models[]`` (each model is ``{value, label,
    supports_xhigh?, provider?, tier?, tier_label?, good_at?}``) and
    ``auto_model`` / ``auto_model_label`` — what an agent with no
    ``default_model`` runs on that engine right now. Read fresh on every
    call: the model list and the auto default follow the admin's enablement.
    """
    data = await _request("GET", "/v1/execution-layers")
    if not isinstance(data, dict) or not data:
        return "❌ Error: unexpected /v1/execution-layers response"

    # Discover this agent's current execution_paths so we can flag which
    # layers are already enabled vs need a separate ``update_execution_layers``
    # call. ``GET /v1/agents/{name}/info`` returns execution_paths as a
    # list[str] with the default first.
    info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
    paths_list = info.get("execution_paths") or [] if isinstance(info, dict) else []
    if not isinstance(paths_list, list):
        paths_list = []
    current_layers = set(paths_list)
    current_default = info.get("execution_path") if isinstance(info, dict) else None
    if current_default:
        current_layers.add(current_default)

    lines = [
        "# Available models", "",
        "Tier = capability, most capable first: 1 frontier (complex coding, "
        "the hardest reasoning and judgement), 2 strong, 3 balanced, 4 fast "
        "(mechanical, routine); untiered = a local or custom model nobody "
        "rated. Complex, open-ended or judgement-heavy work belongs on tier 1; "
        "only mechanical, well-defined work should run lower; never assume a "
        "newer or bigger-sounding id is stronger.", "",
    ]
    for layer_path, layer in sorted(data.items()):
        if not isinstance(layer, dict):
            continue
        models = layer.get("models") or []
        # Filter out the "System Default" placeholder (value=="") — agents
        # set real model IDs. Tier order: the table is the ranking.
        real_models = sorted(
            (m for m in models if m.get("value")),
            key=lambda m: (m.get("tier") or 99, models.index(m)),
        )
        if not real_models:
            continue
        enabled_here = layer_path in current_layers
        is_default = layer_path == current_default
        flag = (
            "**default for this agent**" if is_default
            else "**enabled for this agent**" if enabled_here
            else "_available — call `update_execution_layers` to add it_"
        )
        display = layer.get("display_name") or layer_path
        lines.append(f"## {display} (`{layer_path}`) — {flag}")
        lines.append("")
        auto = layer.get("auto_model") or ""
        if auto:
            auto_label = layer.get("auto_model_label") or auto
            lines.append(
                f"Auto (no `default_model`) currently runs `{auto}`"
                + (f" ({auto_label})" if auto_label != auto else "") + " on this engine."
            )
            lines.append("")
        lines.append("| Model ID | Label | Tier | Good at |")
        lines.append("|---|---|---|---|")
        for m in real_models:
            tier = m.get("tier")
            tier_cell = f"{tier} {m.get('tier_label') or ''}".strip() if tier else "untiered"
            lines.append(
                f"| `{m['value']}` | {m.get('label', '')} | {tier_cell} | {m.get('good_at') or ''} |")
        lines.append("")
    lines.append(
        "Multi-step model change: if the model you want is on a layer "
        "_not_ already enabled for this agent, first call "
        "`update_execution_layers` to add it, then `update_default_model`, "
        "then optionally `update_default_layer` to switch the default."
    )
    return "\n".join(lines)


async def _tool_update_display_name(new_name: str) -> str:
    new_name = (new_name or "").strip()
    if not (1 <= len(new_name) <= 80):
        return "❌ Error: display_name must be 1–80 characters"
    await _request("PATCH", f"/v1/agents/{AGENT_NAME}", json={"display_name": new_name})
    return f"✅ Display name updated to **{new_name}**."


async def _tool_update_description(new_description: str) -> str:
    new_description = (new_description or "").strip()
    if not (1 <= len(new_description) <= 500):
        return "❌ Error: description must be 1–500 characters"
    await _request("PATCH", f"/v1/agents/{AGENT_NAME}", json={"description": new_description})
    return "✅ Description updated."


async def _tool_update_color(hex_color: str) -> str:
    hex_color = (hex_color or "").strip()
    if not HEX_COLOR_REGEX.fullmatch(hex_color):
        return "❌ Error: color must be #RRGGBB (e.g. #3B82F6)"
    await _request("PATCH", f"/v1/agents/{AGENT_NAME}", json={"color": hex_color})
    return f"✅ Color updated to {hex_color}."


async def _tool_update_default_model(model_id: str) -> str:
    model_id = (model_id or "").strip()
    if not model_id:
        return "❌ Error: model_id is required"
    # Validate against the platform's available list. ``/v1/execution-layers``
    # is keyed by layer path; each layer has ``models[]`` of ``{value, label}``.
    data = await _request("GET", "/v1/execution-layers")
    if not isinstance(data, dict):
        return "❌ Error: unexpected /v1/execution-layers response"
    model_to_layer: dict[str, str] = {}
    for layer_path, layer in data.items():
        if not isinstance(layer, dict):
            continue
        for m in (layer.get("models") or []):
            value = m.get("value")
            if value:
                model_to_layer[value] = layer_path
    if model_id not in model_to_layer:
        return (
            f"❌ Error: model '{model_id}' not in the platform's enabled "
            f"models. Call `list_available_models` to see what's available."
        )
    # Soft warning: if the model's layer isn't in the agent's execution_paths
    # yet, the change is accepted (the platform allows pre-staging a model for
    # a not-yet-enabled layer) but we surface the gap so the agent knows to
    # call ``update_execution_layers`` next.
    target_layer = model_to_layer[model_id]
    info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
    paths_list = info.get("execution_paths") or [] if isinstance(info, dict) else []
    if not isinstance(paths_list, list):
        paths_list = []
    current_layers = set(paths_list)
    current_default = info.get("execution_path") if isinstance(info, dict) else None
    if current_default:
        current_layers.add(current_default)
    await _request("PATCH", f"/v1/agents/{AGENT_NAME}", json={"default_model": model_id})
    note = ""
    if target_layer not in current_layers:
        note = (
            f"\n\n⚠ This model belongs to the `{target_layer}` layer, which "
            f"is NOT in this agent's execution_paths. To actually use it, "
            f"call `update_execution_layers` to add `{target_layer}`, then "
            f"`update_default_layer({target_layer!r})`."
        )
    return f"✅ Default model set to `{model_id}` (on `{target_layer}` layer).{note}"


async def _tool_update_execution_layers(layers: list[str]) -> str:
    """Set this agent's execution_paths list.

    Wire format (per ``UpdateAgentRequest`` in ``api/agents.py``):
    PATCH ``/v1/agents/{slug}`` with ``{"execution_paths": [...]}``. The
    server uses ``execution_paths[0]`` as the new default layer and stores
    the rest as auxiliary paths. So whichever layer should remain the
    default must stay first in the list — we preserve that here by
    placing the current default layer first if it survives the change,
    otherwise the caller must call `update_default_layer` afterwards.
    """
    if not isinstance(layers, list) or not layers:
        return "❌ Error: layers must be a non-empty list"
    # The platform's registered engines; without a catalog the PATCH's own
    # validation answers (an unknown id fails closed on the proxy).
    valid = await _valid_layers()
    if valid is not None:
        bad = [layer for layer in layers if layer not in valid]
        if bad:
            return f"❌ Error: invalid execution layer(s): {bad}. Valid: {valid}"
    info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
    current_default = info.get("execution_path") if isinstance(info, dict) else None
    # Preserve the current default at position 0 if it survives the change.
    ordered = list(layers)
    if current_default and current_default in ordered:
        ordered.remove(current_default)
        ordered.insert(0, current_default)
    elif current_default and current_default not in layers:
        # The caller is dropping the current default — accept it; the new
        # primary becomes layers[0]. Surface this in the response so the
        # LLM knows it just swapped the default by side effect.
        pass
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"execution_paths": ordered},
    )
    new_default = ordered[0]
    if current_default and current_default not in layers:
        return (
            f"✅ Execution layers updated to {ordered}. Default layer "
            f"changed from `{current_default}` to `{new_default}` because "
            f"the old default was dropped."
        )
    return f"✅ Execution layers updated to {ordered}. Default layer remains `{new_default}`."


async def _tool_update_default_layer(layer: str) -> str:
    """Set the default execution layer.

    The agents table stores execution_paths as ``[default, ...auxiliary]``.
    The API doesn't expose a separate ``execution_path`` setter — to change
    the default, we reorder the list with the desired default at index 0
    and PATCH ``execution_paths`` with the reordered list.
    """
    layer = (layer or "").strip()
    if not layer:
        return "❌ Error: layer is required"
    valid = await _valid_layers()
    if valid is not None and layer not in valid:
        return f"❌ Error: invalid layer '{layer}'. Valid: {valid}"
    info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
    current_paths = info.get("execution_paths") or [] if isinstance(info, dict) else []
    if not isinstance(current_paths, list):
        # Belt-and-suspenders for older response shapes — should be a list now.
        current_paths = []
    if layer not in current_paths:
        return (
            f"❌ Error: layer `{layer}` is not in this agent's execution_paths "
            f"({current_paths}). Call `update_execution_layers` first to "
            f"add it."
        )
    # Reorder: layer first, then the rest in their original order.
    reordered = [layer] + [p for p in current_paths if p != layer]
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"execution_paths": reordered},
    )
    return f"✅ Default layer set to `{layer}`."


async def _tool_list_context_files() -> str:
    """Use the proxy's ``/v1/agents/{slug}/files`` endpoint to enumerate the
    agent's file tree, then filter to ``config/context/`` (the auto-loaded
    context directory). Reading the host filesystem directly
    isn't reliable from inside the bwrap sandbox — the MCP's view of the
    filesystem doesn't necessarily match the proxy's ``PLATFORM_DATA_DIR``,
    and the proxy already has a sanitized endpoint for this.
    """
    data = await _request("GET", f"/v1/agents/{AGENT_NAME}/files")
    if not isinstance(data, dict):
        return "❌ Error: unexpected /v1/agents/{slug}/files response"
    tree = data.get("tree") or []
    # The endpoint returns ``tree`` as a LIST of top-level nodes — wrap it in
    # a synthetic dir root so the descent is uniform (tolerate a dict root
    # too, should the endpoint shape ever change).
    root = tree if isinstance(tree, dict) else {"type": "dir", "children": tree}
    # Walk the tree to find the ``config/context`` subtree.
    context_subtree = _find_subtree(root, ["config", "context"])
    if context_subtree is None:
        return "_`config/context/` not found for this agent._"
    rows: list[tuple[str, int, str]] = []
    for child in (context_subtree.get("children") or []):
        name = child.get("name", "")
        if not name:
            continue
        _collect_files(child, name, rows)
    if not rows:
        return (
            "_`config/context/` is empty — nothing auto-loads into context. "
            "Drop markdown or text files in there (or use the dashboard's "
            "Workspace editor) to give the agent persistent reference docs._"
        )
    total = sum(sz for _, sz, _ in rows)
    out = ["# Auto-loaded context files", "", "| File | Size | Last modified |", "|---|---|---|"]
    for rel, sz, mtime in sorted(rows):
        out.append(f"| `{rel}` | {sz} | {mtime} |")
    out.append(f"\n_Total: {len(rows)} file(s), {total} bytes._")
    return "\n".join(out)


_PERSONA_MAX_BYTES = 64 * 1024


async def _tool_update_persona(content: str) -> str:
    """Replace ``config/agent.md`` — the agent's persona, prompt section 1.

    Goes through the proxy's file API (not a raw filesystem write): that is
    the only path that carries the manager/admin role check, the config-dir
    git commit and the satellite fan-out. The sandbox route (Write/Edit on
    ``/config/agent.md``) stays available for CLI layers, but Direct LLM has
    no file tools at all — without this tool those agents can never author
    their own persona.

    Pinned to sessions with a human present and an owner-tier role — the
    sandbox mount table's rule (``config/`` is owner-only) restated where
    the tool can enforce it. "Human present" is ``OTO_ROLE`` non-empty and
    no ``OTO_TASK_TYPE``: scope is NOT the signal, because a Shared-only
    agent mounts agent-scope for HUMAN chats too (``OTO_SCOPE == "agent"``
    with a manager driving); a phone caller carries ``viewer`` (refused by
    the owner-tier check), a trigger fire the agent's intrinsic ``manager``
    or ``admin`` (refused by ``TASK_TYPE``), and task fires carry their
    creator's role but run
    unattended. A no-user session reaching the file API would otherwise slip
    past the role check on the proxy side, and rewriting an agent's soul is
    not something an unattended session should do.
    """
    if not ROLE or TASK_TYPE:
        return (
            "❌ The persona can only be rewritten from a session with a "
            "user present — this session has none. Ask a manager of this "
            "agent to make the change from their own chat."
        )
    if not CAN_MANAGE:
        return (
            "❌ Manager or admin role on this agent is required to rewrite "
            "the persona."
        )
    text = (content or "").strip()
    if not text:
        return (
            "❌ Refusing to write an empty persona — that is the state this "
            "tool exists to fix. Send the full persona text."
        )
    if len(text.encode("utf-8")) > _PERSONA_MAX_BYTES:
        return (
            f"❌ Persona too large ({len(text.encode('utf-8'))} bytes; max "
            f"{_PERSONA_MAX_BYTES}). The persona is role and judgment, not a "
            "manual — long reference material belongs in `config/context/` "
            "or `/knowledge/`."
        )
    await _request(
        "PUT",
        f"/v1/agents/{AGENT_NAME}/files/config/agent.md",
        json={"content": text + "\n"},
    )
    return (
        "✅ Persona written to `config/agent.md` "
        f"({len(text.encode('utf-8'))} bytes). It loads first in every "
        "session — this one keeps the prompt it started with, so the new "
        "persona takes effect in the next new session."
    )


def _find_subtree(tree: dict, path_parts: list[str]) -> dict | None:
    """Descend the file-tree dict by name. Tree node shape:
    ``{name, type: "dir"|"file", children?: [...]}``.
    """
    cursor: dict | None = tree
    for part in path_parts:
        if not cursor or cursor.get("type") != "dir":
            return None
        children = cursor.get("children") or []
        next_node = next((c for c in children if c.get("name") == part), None)
        if not next_node:
            return None
        cursor = next_node
    return cursor


def _collect_files(node: dict, rel_path: str, out: list[tuple[str, int, str]]) -> None:
    """Recursively collect (relative_path, size, mtime) for every file under
    ``node``. ``rel_path`` is the path SO FAR, relative to wherever the
    caller decided the root is (here: the agent's ``config/context/``).
    """
    if not isinstance(node, dict):
        return
    if node.get("type") == "file":
        size = int(node.get("size", 0) or 0)
        # The endpoint's timestamp field is ``modified`` (ISO 8601).
        mtime = node.get("modified", "") or ""
        out.append((rel_path, size, mtime))
        return
    if node.get("type") == "dir":
        for child in (node.get("children") or []):
            name = child.get("name", "")
            if not name:
                continue
            _collect_files(child, f"{rel_path}/{name}", out)


async def _tool_update_default_scope(default_scope: str) -> str:
    """Set the agent's `default_scope` to ``user`` or ``agent``.

    Drives the default `scope` value for every scope-aware MCP (tasks,
    notifications, triggers, meetings, memory). Personal-leaning agents
    use ``user``; operational agents that mostly do shared work use
    ``agent``. The user-facing system prompt reflects this immediately
    on new sessions — already-warm sessions need a fresh chat to pick
    up the change.
    """
    default_scope = (default_scope or "").strip().lower()
    if default_scope not in ("user", "agent"):
        return (
            f"❌ Error: invalid default_scope '{default_scope}'. "
            "Valid values: `user` or `agent`."
        )
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"default_scope": default_scope},
    )
    return (
        f"✅ default_scope set to `{default_scope}`. New sessions will use "
        f"this scope as the default for tasks / notifications / triggers / "
        f"meetings / memories."
    )


async def _tool_update_default_execution_mode(mode: str) -> str:
    """Set this agent's default SESSION MODE for new chats & tasks.

    - `interactive` — run the native CLI as a live terminal (TUI) session.
    - `-p` — the normal headless stream.
    - `` (empty) — unset; fall back to the platform default.

    Only valid when this agent's DEFAULT model runs on an engine whose
    descriptor declares ``supports_interactive_pty`` (a native TUI the platform
    runs under a terminal): an engine without one has nothing to run
    interactively. Governs NEW chats + tasks; meetings always run headless
    regardless. Already-warm sessions need a fresh chat to pick up the change.
    """
    mode = (mode or "").strip()
    if mode not in ("", "interactive", "-p"):
        return (
            f"❌ Error: invalid mode '{mode}'. "
            "Valid values: `interactive`, `-p`, or `` (empty, to unset)."
        )
    # When SETTING a real mode, mirror the backend gate: the agent's default
    # model must be on an engine with a TUI. Which engines those are is read
    # from their descriptors (never a literal pair).
    if mode in ("interactive", "-p"):
        info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
        default_model = info.get("default_model") if isinstance(info, dict) else ""
        if default_model:
            data = await _request("GET", "/v1/execution-layers")
            layers_for_model: set[str] = set()
            interactive: set[str] = set()
            if isinstance(data, dict):
                for layer_path, layer in data.items():
                    if not isinstance(layer, dict):
                        continue
                    if (layer.get("runtime") or {}).get("supports_interactive_pty"):
                        interactive.add(layer_path)
                    for m in (layer.get("models") or []):
                        if m.get("value") == default_model:
                            layers_for_model.add(layer_path)
            if layers_for_model and not (layers_for_model & interactive):
                return (
                    f"❌ Error: default_execution_mode only applies to engines with a "
                    f"native terminal ({', '.join(sorted(interactive)) or 'none registered'}). "
                    f"This agent's default model `{default_model}` runs on "
                    f"`{'/'.join(sorted(layers_for_model))}`. Switch the default model "
                    f"first if you want interactive."
                )
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"default_execution_mode": mode},
    )
    desc = "unset (platform default)" if not mode else f"`{mode}`"
    return (
        f"✅ Default session mode set to {desc}. New chats & tasks for this agent "
        f"will start in this mode (meetings always run headless)."
    )


# Visibility modes: the 2×2 of (collaborative × default_scope). See
# proxy/core/session/visibility.py.
_VISIBILITY_MODES = {
    "personal_shared": (True, "user"),
    "shared_personal": (True, "agent"),
    "personal_only": (False, "user"),
    "shared_only": (False, "agent"),
}


async def _tool_set_visibility_mode(mode: str) -> str:
    """Set this agent's visibility mode — how it relates to users.

    - `personal_shared` — each person has a private space; a shared team space
      is also available.
    - `shared_personal` — work lives in one shared space; each person also keeps
      personal files.
    - `personal_only` — fully private per person; NO shared space; separate
      chats and memory.
    - `shared_only` — ONE shared workspace + ONE shared chat history for
      everyone; no personal space.

    Changing modes NEVER deletes folders — it only gates what each session
    mounts and sees. New sessions reflect the change (warm sessions need a
    fresh chat).
    """
    mode = (mode or "").strip().lower()
    if mode not in _VISIBILITY_MODES:
        return (
            f"❌ Error: invalid mode '{mode}'. Valid: "
            + ", ".join(f"`{m}`" for m in _VISIBILITY_MODES)
        )
    collaborative, default_scope = _VISIBILITY_MODES[mode]
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"collaborative": collaborative, "default_scope": default_scope},
    )
    return (
        f"✅ Visibility mode set to `{mode}` "
        f"(collaborative={collaborative}, default_scope=`{default_scope}`). "
        f"New sessions will use this mode."
    )


async def _tool_get_memory_settings() -> str:
    """Show the per-agent memory toggle state.

    Reads ``GET /v1/internal/memory/agent-settings/{agent}`` which returns
    the per-agent overrides — when a key is ``null`` the platform-wide
    default applies. The platform-wide defaults live in
    ``GET /v1/internal/memory/settings`` (admin-managed); this tool
    surfaces only the per-agent layer.
    """
    data = await _request("GET", f"/v1/internal/memory/agent-settings/{AGENT_NAME}")
    if not isinstance(data, dict):
        return "❌ Error: unexpected /v1/internal/memory/agent-settings response"
    rows = [f"# Memory settings — `{AGENT_NAME}`"]
    for key in ("user_memory_enabled", "agent_memory_enabled"):
        val = data.get(key)
        display = "platform default" if val is None else val
        rows.append(f"- **{key}**: {display}")
    return "\n".join(rows)


async def _tool_update_user_memory_enabled(enabled: bool) -> str:
    """Enable or disable per-user memory for this agent (the
    `/memories/user/` scope). Overrides the platform-wide default.

    When disabled, `memory` tool writes to the user scope are rejected and
    the user-memory section is no longer injected into user-scope sessions
    on this agent.
    """
    if not isinstance(enabled, bool):
        return "❌ Error: `enabled` must be true or false"
    await _request(
        "PATCH", f"/v1/internal/memory/agent-settings/{AGENT_NAME}",
        json={"key": "user_memory_enabled", "value": enabled},
    )
    state = "enabled" if enabled else "disabled"
    return (
        f"✅ Per-user memory ({state}) for `{AGENT_NAME}`. New sessions on "
        f"this agent will see the change."
    )


async def _tool_update_agent_memory_enabled(enabled: bool) -> str:
    """Enable or disable the shared agent memory for this agent (the
    `/memories/agent/` scope). Overrides the platform-wide default.

    When disabled, `memory` tool writes to the agent scope are rejected
    and the agent-memory section is no longer injected into ANY session
    on this agent (user-scope OR agent-scope).
    """
    if not isinstance(enabled, bool):
        return "❌ Error: `enabled` must be true or false"
    await _request(
        "PATCH", f"/v1/internal/memory/agent-settings/{AGENT_NAME}",
        json={"key": "agent_memory_enabled", "value": enabled},
    )
    state = "enabled" if enabled else "disabled"
    return (
        f"✅ Shared agent memory ({state}) for `{AGENT_NAME}`. New sessions "
        f"on this agent will see the change."
    )


async def _tool_complete_setup(summary: str = "", scope: str = "") -> str:
    """Mark this agent's post-install setup complete.

    Single HTTP call to ``POST /v1/agents/{slug}/complete-setup`` which (in
    the proxy process, with direct host-FS access) deletes
    ``config/context/setup.md`` if present + stamps ``agents.setup_completed_at``
    + notifies the installer. The MCP doesn't touch the filesystem itself
    because the bwrap sandbox doesn't reliably expose the host agents
    directory at the path the MCP would expect.

    Response body shape (from the backend endpoint):
        {status: "completed" | "already_complete",
         setup_md_removed: bool,
         ...}

    Idempotent — calling again after the stamp is set re-tries the file
    delete (in case it failed earlier) but is otherwise a no-op."""
    summary = (summary or "").strip()
    scope = (scope or "").strip()
    payload: dict = {"summary": summary}
    if scope:
        payload["scope"] = scope
    try:
        result = await _request(
            "POST", f"/v1/agents/{AGENT_NAME}/complete-setup",
            json=payload,
        )
    except _ApiError as exc:
        return f"❌ Error: {exc}"

    if not isinstance(result, dict):
        return "✅ Setup marked complete."

    status = result.get("status")
    removed = bool(result.get("setup_md_removed"))
    summary_tail = f" — {summary}" if summary else ""

    if status == "user_setup_complete":
        if result.get("user_setup_removed"):
            return (
                f"✅ Your setup marked complete — `user-setup.md` removed "
                f"from your context.{summary_tail}"
            )
        return (
            f"ℹ️ Your setup was already complete (no `user-setup.md` in "
            f"your context).{summary_tail}"
        )

    if status == "already_complete":
        if removed:
            return (
                f"ℹ️ Setup was already marked complete on the backend, but a "
                f"stale `setup.md` was still on disk — removed it now. It "
                f"will no longer auto-load as context.{summary_tail}"
            )
        return (
            f"ℹ️ Setup was already marked complete and `setup.md` isn't on "
            f"disk. Nothing to do.{summary_tail}"
        )

    # status == "completed"
    if removed:
        return f"✅ Setup marked complete. `setup.md` removed.{summary_tail}"
    return f"✅ Setup marked complete. (No `setup.md` was present.){summary_tail}"


# ---------------------------------------------------------------------------
# Shared knowledge libraries + departments (platform-role tools). The
# proxy enforces admin/creator on every mutation (require_creator_interactive
# — real-user-backed principals only); these tools sit in the CRITICAL
# permission tier, so the human approves each call in chat before it runs.
# ---------------------------------------------------------------------------

def _lib_path(source: str, subdir: str) -> str:
    return (f"/knowledge/shared/{source}/{subdir}/" if subdir
            else f"/knowledge/shared/{source}/")


def _lib_label(name: str, source: str, subdir: str) -> str:
    where = f"{source}/{subdir}" if subdir else f"{source} (whole folder)"
    return f'"{name}" ({where})' if name else where


async def _tool_list_knowledge_libraries() -> str:
    """This agent's library state + (for admins/creators) every library."""
    state = await _request(
        "GET", f"/v1/agents/{AGENT_NAME}/knowledge-attachments")
    lines: list[str] = []
    own = state.get("libraries") or []
    if own:
        lines.append("📚 This agent shares these knowledge libraries:")
        for lib in own:
            consumers = lib.get("consumers") or []
            clist = ", ".join(
                f"{c['consumer_agent']}{' (RW)' if c.get('writable') else ''}"
                for c in consumers) or "no consumers yet"
            lines.append(
                f"  - {_lib_label(lib.get('name') or '', AGENT_NAME, lib.get('subdir') or '')} "
                f"— attached to: {clist}")
    else:
        lines.append(
            "This agent shares no knowledge libraries.")
    attachments = state.get("attachments") or []
    if attachments:
        lines.append("Attached libraries:")
        for a in attachments:
            mode = "read-write" if a.get("writable") else "read-only"
            sub = a.get("subdir") or ""
            lines.append(
                f"  - {_lib_label(a.get('name') or '', a['source_agent'], sub)} "
                f"at {_lib_path(a['source_agent'], sub)} ({mode})")
    else:
        lines.append("No libraries attached to this agent.")
    try:
        allrows = await _request("GET", "/v1/knowledge-libraries")
        libs = allrows.get("libraries") or []
        if libs:
            lines.append("All libraries on this installation:")
            for lib in libs:
                lines.append(
                    f"  - {_lib_label(lib.get('name') or '', lib['source_agent'], lib.get('subdir') or '')} "
                    f"({lib.get('consumers', 0)} consumer(s))")
    except _ApiError:
        pass  # non-admin/creator callers see only their own agent's state
    return "\n".join(lines)


async def _tool_share_knowledge_folder(enable: bool, name: str = "",
                                       subdir: str = "") -> str:
    """Promote / un-promote one of THIS agent's knowledge libraries."""
    label = (name or "").strip()
    sub = (subdir or "").strip().strip("/")
    if enable and not label:
        return (
            "❌ Error: a library name is required when sharing — it is how "
            "people identify this library in the dashboard. Ask the user "
            "what to call it (e.g. 'Brand Guidelines'), then call again "
            "with name set."
        )
    resp = await _request(
        "PUT", f"/v1/agents/{AGENT_NAME}/knowledge-library",
        json={"enabled": bool(enable), "name": label, "subdir": sub},
    )
    if resp.get("status") == "shared":
        what = (f"knowledge subfolder '{sub}/'" if sub
                else "whole knowledge folder")
        return (
            f"✅ This agent's {what} is now a shared knowledge "
            f"library named \"{resp.get('name') or label}\". Attach it to "
            "other agents from their agent settings (Shared knowledge) or "
            "with attach_knowledge_library in the consumer agent's chat — "
            f"they read it at {_lib_path(AGENT_NAME, sub)}."
        )
    detached = resp.get("detached_consumers") or []
    tail = (
        f" Detached consumers: {', '.join(detached)} — their mirrors are "
        "being removed."
    ) if detached else ""
    what = f"library '{sub}/'" if sub else "whole-folder library"
    return f"✅ The {what} is no longer shared.{tail}"


async def _tool_attach_knowledge_library(source_agent: str, writable: bool,
                                         subdir: str = "") -> str:
    """Attach a shared library to THIS agent (or update its writable flag)."""
    source_agent = (source_agent or "").strip()
    sub = (subdir or "").strip().strip("/")
    if not source_agent:
        return "❌ Error: source_agent is required."
    resp = await _request(
        "PUT", f"/v1/agents/{AGENT_NAME}/knowledge-attachments",
        json={"source_agent": source_agent, "writable": bool(writable),
              "subdir": sub},
    )
    mode = "read-write" if resp.get("writable") else "read-only"
    return (
        f"✅ Library '{_lib_label('', source_agent, sub)}' attached {mode} at "
        f"`{_lib_path(source_agent, sub)}`. Content materializes now; "
        "sessions get the mount from their next start (already-running "
        "ones won't see it)."
    )


async def _tool_detach_knowledge_library(source_agent: str,
                                         subdir: str = "") -> str:
    source_agent = (source_agent or "").strip()
    sub = (subdir or "").strip().strip("/")
    if not source_agent:
        return "❌ Error: source_agent is required."
    await _request(
        "DELETE",
        f"/v1/agents/{AGENT_NAME}/knowledge-attachments/{source_agent}"
        + (f"?subdir={sub}" if sub else ""),
    )
    return (
        f"✅ Library '{_lib_label('', source_agent, sub)}' detached — its "
        "mirror is being removed from this agent."
    )


async def _tool_set_department(department: str, level: str) -> str:
    """Assign THIS agent to a department + level (or clear with empty args).

    Resolves names → ids via GET /v1/departments (case-insensitive)."""
    department = (department or "").strip()
    level = (level or "").strip()
    if not department:
        await _request(
            "PATCH", f"/v1/agents/{AGENT_NAME}",
            json={"department_id": "", "department_level_id": ""},
        )
        return "✅ Department assignment cleared."
    depts = await _request("GET", "/v1/departments")
    rows = depts if isinstance(depts, list) else depts.get("departments", [])
    match = next(
        (d for d in rows
         if d.get("name", "").lower() == department.lower()
         or d.get("id") == department),
        None,
    )
    if match is None:
        names = ", ".join(d.get("name", "?") for d in rows) or "none exist"
        return f"❌ Error: no department '{department}'. Available: {names}."
    levels = match.get("levels") or []
    if not level and len(levels) == 1:
        lvl = levels[0]
    else:
        lvl = next(
            (l for l in levels
             if l.get("name", "").lower() == level.lower()
             or l.get("id") == level),
            None,
        )
    if lvl is None:
        lnames = ", ".join(l.get("name", "?") for l in levels)
        return (f"❌ Error: no level '{level}' in '{match.get('name')}'. "
                f"Levels: {lnames}.")
    await _request(
        "PATCH", f"/v1/agents/{AGENT_NAME}",
        json={"department_id": match["id"], "department_level_id": lvl["id"]},
    )
    return (f"✅ Assigned to department '{match.get('name')}' at level "
            f"'{lvl.get('name')}'. Delegation edges recompiled; sessions "
            "pick the new roster up at their next start.")


# ---------------------------------------------------------------------------
# MCP dispatch (the tool schemas: agent_config_tool_schemas.py)
# ---------------------------------------------------------------------------



_TOOL_HANDLERS = {
    "get_agent_config": lambda args: _tool_get_agent_config(),
    "list_available_models": lambda args: _tool_list_available_models(),
    "update_display_name": lambda args: _tool_update_display_name(args.get("new_name", "")),
    "update_description": lambda args: _tool_update_description(args.get("new_description", "")),
    "update_color": lambda args: _tool_update_color(args.get("hex_color", "")),
    "update_default_model": lambda args: _tool_update_default_model(args.get("model_id", "")),
    "update_execution_layers": lambda args: _tool_update_execution_layers(args.get("layers", [])),
    "update_default_layer": lambda args: _tool_update_default_layer(args.get("layer", "")),
    "update_default_scope": lambda args: _tool_update_default_scope(args.get("default_scope", "")),
    "update_default_execution_mode": lambda args: _tool_update_default_execution_mode(args.get("mode", "")),
    "set_visibility_mode": lambda args: _tool_set_visibility_mode(args.get("mode", "")),
    "list_context_files": lambda args: _tool_list_context_files(),
    "update_persona": lambda args: _tool_update_persona(args.get("content", "")),
    "get_memory_settings": lambda args: _tool_get_memory_settings(),
    "update_user_memory_enabled": lambda args: _tool_update_user_memory_enabled(args.get("enabled")),
    "update_agent_memory_enabled": lambda args: _tool_update_agent_memory_enabled(args.get("enabled")),
    "complete_setup": lambda args: _tool_complete_setup(
        args.get("summary", ""), args.get("scope", ""),
    ),
    "list_knowledge_libraries": lambda args: _tool_list_knowledge_libraries(),
    "share_knowledge_folder": lambda args: _tool_share_knowledge_folder(
        args.get("enable"), args.get("name", ""), args.get("subdir", ""),
    ),
    "attach_knowledge_library": lambda args: _tool_attach_knowledge_library(
        args.get("source_agent", ""), args.get("writable", False),
        args.get("subdir", ""),
    ),
    "detach_knowledge_library": lambda args: _tool_detach_knowledge_library(
        args.get("source_agent", ""), args.get("subdir", ""),
    ),
    "set_department": lambda args: _tool_set_department(
        args.get("department", ""), args.get("level", ""),
    ),
}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("agent-config-mcp")


async def _engine_schemas() -> dict[str, dict[str, Any]]:
    """``_TOOL_SCHEMAS`` with the engine-typed tools filled from the
    platform's catalog: the two layer ``enum``s and the three descriptions
    that name engines. Without a catalog the enums are left out (the proxy
    validates every PATCH) and the descriptions point at
    ``list_available_models``."""
    schemas = copy.deepcopy(_TOOL_SCHEMAS)
    valid = await _valid_layers()
    interactive = await _interactive_layers()
    layers_items = schemas["update_execution_layers"]["inputSchema"]["properties"]["layers"]["items"]
    layer_prop = schemas["update_default_layer"]["inputSchema"]["properties"]["layer"]
    if valid:
        layers_items["enum"] = list(valid)
        layer_prop["enum"] = list(valid)
        engines_text = ", ".join(valid)
    else:
        engines_text = "the engines the platform registers (see list_available_models)"
    interactive_text = (
        ", ".join(interactive) if interactive
        else "the engines whose descriptor declares supports_interactive_pty (see list_available_models)"
    )
    for name in ("update_execution_layers", "update_default_layer", "update_default_execution_mode"):
        schemas[name]["description"] = schemas[name]["description"].format(
            engines=engines_text, interactive=interactive_text,
        )
    return schemas


@server.list_tools()
async def list_tools() -> list[Tool]:
    schemas = await _engine_schemas()
    return [
        Tool(name=name, description=schema["description"], inputSchema=schema["inputSchema"])
        for name, schema in schemas.items()
        if name in ENABLED_TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name not in ENABLED_TOOLS:
        return [TextContent(type="text", text=f"❌ Error: tool '{name}' not available in this session")]
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"❌ Error: unknown tool '{name}'")]
    try:
        result = await handler(arguments or {})
    except _ApiError as exc:
        return [TextContent(type="text", text=f"❌ Error: {exc}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"❌ Error: {type(exc).__name__}: {exc}")]
    return [TextContent(type="text", text=str(result))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
