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
- ``manager``/``admin`` → the full tool set.
- ``scope=agent`` service sessions (task / phone / trigger, ``ROLE==""``)
  → the full tool set (the agent is editing its own row; nothing escalates
  beyond the platform's per-endpoint role checks).

All HTTP calls go through ``PROXY_URL`` + ``PROXY_API_KEY`` (auto-injected,
session-scoped JWT) so the platform applies the calling user's role server
side. Local checks are best-effort defense.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool


# ---------------------------------------------------------------------------
# Env + permission matrix
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
ROLE = os.environ.get("OTO_ROLE", "")
SCOPE = os.environ.get("OTO_SCOPE", "")

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")

_READ_TOOLS = {
    "get_agent_config",
    "list_available_models",
    "list_context_files",
    "get_memory_settings",
}
_WRITE_TOOLS = {
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
    if ROLE == "viewer":
        # Viewers get exactly complete_setup: per-user onboarding
        # (user-setup.md) targets the default-attach audience, who join as
        # viewers — the endpoint's user scope only ever touches the caller's
        # own file. Everything else stays manager-tier.
        #
        # Scope-independent on purpose: a Shared-only agent mounts agent-scope
        # for HUMAN chats too (OTO_SCOPE=="agent" with a real viewer driving),
        # so keying on scope advertised a surface every endpoint then refused.
        # Service sessions carry ROLE=="" and are unaffected.
        return {"complete_setup"}
    if SCOPE in ("user", "agent"):
        return _READ_TOOLS | _WRITE_TOOLS
    return set()


ENABLED_TOOLS = _resolve_tool_set()

HEX_COLOR_REGEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Must match ``api/agents.py::create_agent::valid_paths`` and the keys in
# ``core/session_manager._LAYERS``. ``codex-cli`` not ``codex``.
VALID_LAYERS = {"claude-code-cli", "direct-llm", "codex-cli"}


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
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 500 and attempt == 0:
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
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise _ApiError(f"{method} {path}: {exc}") from exc
    if last_exc:
        raise _ApiError(f"{method} {path}: {last_exc}")
    return None


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
    layer path (e.g. ``"claude-code-cli"``, ``"codex-cli"``, ``"direct-llm"``)
    where each value carries ``display_name`` + ``models[]`` (each model is
    ``{value, label, supports_xhigh?, provider?}``).
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

    lines = ["# Available models", ""]
    for layer_path, layer in sorted(data.items()):
        if not isinstance(layer, dict):
            continue
        models = layer.get("models") or []
        # Filter out the "System Default" placeholder (value=="") — agents
        # set real model IDs.
        real_models = [m for m in models if m.get("value")]
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
        lines.append("| Model ID | Label |")
        lines.append("|---|---|")
        for m in real_models:
            lines.append(f"| `{m['value']}` | {m.get('label', '')} |")
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
    bad = [layer for layer in layers if layer not in VALID_LAYERS]
    if bad:
        return f"❌ Error: invalid execution layer(s): {bad}. Valid: {sorted(VALID_LAYERS)}"
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
    if layer not in VALID_LAYERS:
        return f"❌ Error: invalid layer '{layer}'. Valid: {sorted(VALID_LAYERS)}"
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

    Only valid when this agent's DEFAULT model runs on a CLI execution layer
    (claude-code-cli or codex-cli): the interactive terminal IS that CLI's own
    TUI, so a Direct-LLM default has nothing to run interactively. Governs NEW
    chats + tasks; meetings always run headless regardless. Already-warm
    sessions need a fresh chat to pick up the change.
    """
    mode = (mode or "").strip()
    if mode not in ("", "interactive", "-p"):
        return (
            f"❌ Error: invalid mode '{mode}'. "
            "Valid values: `interactive`, `-p`, or `` (empty, to unset)."
        )
    # When SETTING a real mode, mirror the backend gate: the agent's default
    # model must be on a CLI layer (Direct-LLM can't run the interactive TUI).
    if mode in ("interactive", "-p"):
        info = await _request("GET", f"/v1/agents/{AGENT_NAME}/info")
        default_model = info.get("default_model") if isinstance(info, dict) else ""
        if default_model:
            data = await _request("GET", "/v1/execution-layers")
            layers_for_model: set[str] = set()
            if isinstance(data, dict):
                for layer_path, layer in data.items():
                    if not isinstance(layer, dict):
                        continue
                    for m in (layer.get("models") or []):
                        if m.get("value") == default_model:
                            layers_for_model.add(layer_path)
            if layers_for_model and not (layers_for_model & {"claude-code-cli", "codex-cli"}):
                return (
                    f"❌ Error: default_execution_mode only applies to CLI execution "
                    f"layers. This agent's default model `{default_model}` runs on "
                    f"`{'/'.join(sorted(layers_for_model))}` (not claude-code-cli / "
                    f"codex-cli). Switch the default model first if you want interactive."
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
# Tool schemas + MCP dispatch
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_agent_config": {
        "description": (
            "Inspect this agent's current settings (display name, description, "
            "color, default model, execution layers, community-template "
            "provenance). Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "list_available_models": {
        "description": (
            "List models available across the platform's enabled execution "
            "layers. Call this before `update_default_model` to know which "
            "model IDs are valid."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "update_display_name": {
        "description": (
            "Change this agent's display name (shown in agent picker, chat "
            "header, and cards). The slug stays the same — only the human "
            "label changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "new_name": {
                    "type": "string",
                    "minLength": 1, "maxLength": 80,
                    "description": "1–80 chars; any printable Unicode.",
                },
            },
            "required": ["new_name"],
            "additionalProperties": False,
        },
    },
    "update_description": {
        "description": "Change this agent's description (1–500 chars).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "new_description": {
                    "type": "string",
                    "minLength": 1, "maxLength": 500,
                },
            },
            "required": ["new_description"],
            "additionalProperties": False,
        },
    },
    "update_color": {
        "description": (
            "Change this agent's accent color (used in UI badges + cards). "
            "Pass a hex code like `#3B82F6`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hex_color": {
                    "type": "string",
                    "pattern": "^#[0-9A-Fa-f]{6}$",
                },
            },
            "required": ["hex_color"],
            "additionalProperties": False,
        },
    },
    "update_default_model": {
        "description": (
            "Set this agent's default model. Must be one of the model_id "
            "values from `list_available_models`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string"},
            },
            "required": ["model_id"],
            "additionalProperties": False,
        },
    },
    "update_execution_layers": {
        "description": (
            "Set which execution layers this agent can use. Must be a non-"
            "empty subset of [claude-code-cli, codex-cli, direct-llm]. The "
            "current default layer is preserved at position 0 if it survives "
            "the change; otherwise the new layers[0] becomes the default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layers": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string", "enum": [
                        "claude-code-cli", "codex-cli", "direct-llm",
                    ]},
                },
            },
            "required": ["layers"],
            "additionalProperties": False,
        },
    },
    "update_default_layer": {
        "description": (
            "Set the default execution layer. Must already be in this agent's "
            "execution_paths list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": [
                    "claude-code-cli", "codex-cli", "direct-llm",
                ]},
            },
            "required": ["layer"],
            "additionalProperties": False,
        },
    },
    "update_default_scope": {
        "description": (
            "Set this agent's `default_scope` — `user` for personal-leaning "
            "agents (tasks / notifications / memories default to the user), "
            "`agent` for operational agents where most work is shared across "
            "all users of this agent. (For the full mode, prefer "
            "`set_visibility_mode`.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "default_scope": {"type": "string", "enum": ["user", "agent"]},
            },
            "required": ["default_scope"],
            "additionalProperties": False,
        },
    },
    "update_default_execution_mode": {
        "description": (
            "Set this agent's default SESSION MODE for new chats & tasks — "
            "`interactive` runs the native CLI as a live terminal (TUI), `-p` "
            "is the normal headless stream, `` (empty) unsets it (platform "
            "default). Only valid when this agent's DEFAULT model runs on a CLI "
            "execution layer (claude-code-cli / codex-cli); Direct-LLM can't run "
            "interactively. Meetings always run headless regardless."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["interactive", "-p", ""]},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    "set_visibility_mode": {
        "description": (
            "Set this agent's visibility mode — how it relates to users. One of: "
            "`personal_shared` (each person private + a shared team space), "
            "`shared_personal` (one shared space + personal files too), "
            "`personal_only` (fully private per person, NO shared space), "
            "`shared_only` (ONE shared workspace + ONE shared chat history for "
            "everyone, no personal space). Changing modes never deletes folders."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["personal_shared", "shared_personal",
                             "personal_only", "shared_only"],
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    "list_context_files": {
        "description": (
            "List files auto-loaded into this agent's context from "
            "`config/context/`. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "get_memory_settings": {
        "description": (
            "Inspect this agent's memory toggle overrides (per-agent layer "
            "above the platform-wide default). Shows the state of "
            "`user_memory_enabled` and `agent_memory_enabled`. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "update_user_memory_enabled": {
        "description": (
            "Enable or disable per-user memory for this agent (the "
            "`/memories/user/` scope). Overrides the platform-wide default. "
            "Disabling stops the user-memory prompt section from injecting "
            "and rejects `memory` tool writes to that scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
    },
    "update_agent_memory_enabled": {
        "description": (
            "Enable or disable the shared agent memory for this agent "
            "(the `/memories/agent/` scope). Overrides the platform-wide "
            "default. Disabling stops the agent-memory section from "
            "injecting in every session and rejects `memory` tool writes "
            "to that scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
    },
    "complete_setup": {
        "description": (
            "Mark this agent's post-install setup complete. Two scopes: "
            "'agent' removes the agent-wide `config/context/setup.md` "
            "(manager-level; call ONLY when every checklist item in setup.md "
            "is verified done), 'user' removes the current user's own "
            "`user-setup.md` onboarding from their context (call when their "
            "personal onboarding is finished or they decline it). Omit scope "
            "when only one applies — it is resolved automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Optional one-line summary of what was configured (for the audit trail).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["agent", "user"],
                    "description": "Which setup to complete; omit to auto-resolve when only one applies.",
                },
            },
            "additionalProperties": False,
        },
    },
}


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
    "get_memory_settings": lambda args: _tool_get_memory_settings(),
    "update_user_memory_enabled": lambda args: _tool_update_user_memory_enabled(args.get("enabled")),
    "update_agent_memory_enabled": lambda args: _tool_update_agent_memory_enabled(args.get("enabled")),
    "complete_setup": lambda args: _tool_complete_setup(
        args.get("summary", ""), args.get("scope", ""),
    ),
}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("agent-config-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name=name, description=schema["description"], inputSchema=schema["inputSchema"])
        for name, schema in _TOOL_SCHEMAS.items()
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
