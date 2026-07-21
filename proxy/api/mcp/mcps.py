"""MCP Server management REST API.

Admin endpoints for viewing, enabling/disabling, configuring MCPs,
managing Docker containers, and agent assignments.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user
from services.mcp import mcp_registry
from storage import mcp_store

logger = logging.getLogger("claude-proxy.mcp-api")
router = APIRouter()


def _require_admin(user: UserContext | None) -> UserContext:
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def _require_manage(user: UserContext | None, agent: str | None = None) -> UserContext:
    if not user:
        raise HTTPException(403, "Authentication required")
    if agent:
        if not user.can_manage_agent(agent):
            raise HTTPException(403, "Manager access required for this agent")
    elif user.role not in ("admin", "creator"):
        raise HTTPException(403, "Admin or creator only")
    return user


# Core MCPs an admin MAY platform-disable: the parallelism features. Their
# backends gate on mcp_state (delegation spawns, meeting creation), so the
# toggle is a real kill-switch, not just a config hide. Everything else in
# core is load-bearing plumbing.
_PLATFORM_DISABLEABLE_CORE = frozenset({"meetings-mcp", "delegation-mcp"})


# ---------------------------------------------------------------------------
# List all MCPs (admin)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/mcps")
async def list_mcps(user: UserContext = Depends(get_current_user)):
    """Return full MCP inventory for the admin dashboard."""
    _require_admin(user)

    manifests = mcp_registry.get_all_manifests()
    states = await asyncio.to_thread(mcp_store.get_all_mcp_states)

    # "Enabled for" per MCP: the agents where the MCP is actually active —
    # visible (auto-mode, or explicit-mode admin-authorized) AND manager-enabled
    # AND platform-enabled. `get_agent_mcps_all_placements` is the canonical
    # configuration view (it applies the explicit-mode authorization filter a
    # raw agent_mcps read lacks). Only agents with at least one manager-enabled
    # MCP can have an active one, so iterate that set — its keys are exactly the
    # `agent_mcps.agent_name` identifiers the placement query joins on.
    def _enabled_agents_by_mcp() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for agent_name in mcp_store.get_all_manager_enabled_mcps():
            for manifest in mcp_registry.get_agent_mcps_all_placements(agent_name):
                out.setdefault(manifest.name, []).append(agent_name)
        return out

    mcp_agents = await asyncio.to_thread(_enabled_agents_by_mcp)

    # Docker status (async). Containerized installs: an image-less docker MCP
    # (core file-tools) runs as an OPERATOR-MANAGED compose sibling there — the
    # proxy never drives its lifecycle (startup_docker_mcps skips it; the
    # socket-proxy blocks builds), so probing the per-MCP compose project would
    # always answer "not_found". Mark those unmanaged instead of probing.
    from core.config import deployment as _deployment
    _in_t2 = _deployment.in_docker_compose()
    docker_unmanaged: set[str] = set()
    docker_statuses: dict[str, str] = {}
    for name, m in manifests.items():
        if m.server.runtime != "docker":
            continue
        if _in_t2 and not (getattr(m.server, "image", "") or ""):
            docker_unmanaged.add(name)
            continue
        if states.get(name, False):
            try:
                from services.mcp import docker_manager
                docker_statuses[name] = await asyncio.to_thread(
                    docker_manager.get_container_status, m
                )
            except Exception:
                docker_statuses[name] = "unknown"

    result = []
    for name, m in manifests.items():
        cred = mcp_registry.get_credential_schema(name)
        cred_type = cred.get("type", "none") if cred else "none"
        cred_label = cred.get("label") if cred and cred_type != "none" else None

        entry = {
            "name": name,
            "label": m.label,
            "description": m.description,
            "version": m.version,
            "category": m.category,
            "runtime": m.server.runtime,
            "transport": m.server.transport,
            "source": m.server.source,
            "enabled": states.get(name, False),
            "can_disable": (m.category != "core"
                            or name in _PLATFORM_DISABLEABLE_CORE),
            "patched": m.patched,
            "patch_note": m.patch_note or "",
            "credential_type": cred_type,
            "credential_label": cred_label,
            "skills": [
                {"id": s.id, "description": s.description}
                for s in m.skills
            ],
            "config_fields": [
                {"key": f.key, "label": f.label, "input_type": f.input_type, "default": f.default}
                for f in m.config
            ],
            "assignment_mode": m.assignment_mode,
            "capability_available": mcp_registry.manifest_capability_available(m),
            "agents": sorted(mcp_agents.get(name, [])),
        }

        # Instance config info (for MCPs with per-instance, per-agent config)
        if m.instances:
            entry["instances"] = {
                "delivery": m.instances.delivery,
                "fields": [
                    {
                        "key": f.key, "label": f.label, "input_type": f.input_type,
                        "default": f.default, "required": f.required, "secret": f.secret,
                    }
                    for f in m.instances.fields
                ],
                "max_instances": m.instances.max_instances,
            }

        # Config values from DB
        config_vals = await asyncio.to_thread(mcp_store.get_mcp_config_values, name)
        entry["config_values"] = config_vals

        # Internal-network access toggle (homelab MCPs declaring network_targets).
        # When on, the sandbox carves egress to the MCP's configured target host
        # Default from the manifest; admin override in _network_access.
        if m.network_targets:
            entry["has_network_targets"] = True
            _na = config_vals.get("_network_access")
            entry["network_access"] = (
                m.network_access_default if _na is None else (_na == "true")
            )

        # Credential fields + configured status (for inline credential editing)
        if cred and cred_type == "infra":
            entry["credential_fields"] = cred.get("fields", [])
            entry["server_config_fields"] = cred.get("server_config_fields", [])
            from storage import credential_store
            stored = await asyncio.to_thread(credential_store.get_infra_credentials, name)
            entry["credential_configured"] = bool(stored)
            entry["credential_configured_keys"] = list(stored.keys()) if stored else []
        elif cred and cred_type == "per_user":
            entry["credential_fields"] = cred.get("fields", [])
            entry["server_config_fields"] = cred.get("server_config_fields", [])
            entry["credential_configured"] = False
            entry["credential_configured_keys"] = []

            # App credential info for OAuth MCPs (e.g. google-oauth-app)
            app_cred_name = cred.get("app_credential", "")
            if app_cred_name and cred.get("app_credential_fields"):
                from storage import credential_store
                stored = await asyncio.to_thread(
                    credential_store.get_infra_credentials, app_cred_name
                )
                entry["app_credential"] = app_cred_name
                entry["app_credential_fields"] = cred["app_credential_fields"]
                entry["app_credential_configured"] = bool(stored)
                entry["app_credential_configured_keys"] = list(stored.keys()) if stored else []

            # Expose provider_id so the dashboard can render the right
            # callback URL (`/v1/oauth/{provider_id}/callback`) and
            # provider-specific helper text in the app-credentials form.
            # NB: cred["oauth"] is a bool flag; the real metadata lives
            # under cred["oauth_meta"] (set by get_credential_schema).
            oauth_meta = cred.get("oauth_meta") or {}
            if oauth_meta.get("provider_id"):
                entry["provider_id"] = oauth_meta["provider_id"]

        # Hosted-relay info — INDEPENDENT of credential type, so it
        # runs for EVERY MCP (incl. `type: "none"` ones like image-search /
        # image-gen, which were previously skipped because this lived inside the
        # `per_user` branch). `hosted.oauth_app` drives the per-MCP OAuth toggle;
        # `hosted.api_key_relay` is surfaced to the per-instance manager.
        hosted = cred.get("hosted") if cred else None
        if hosted:
            entry["hosted"] = hosted
            oauth_app = hosted.get("oauth_app")
            if oauth_app:
                entry["hosted_oauth_mode"] = config_vals.get(
                    "_hosted_service_mode",
                    oauth_app.get("default_mode", "hosted"),
                )

        # Docker status. Unmanaged (operator-owned compose sibling on a
        # containerized install): no status, and the dashboard hides the
        # pill + start/stop/restart controls — the proxy can't drive it.
        if m.server.runtime == "docker":
            if name in docker_unmanaged:
                entry["docker_managed"] = False
            else:
                entry["docker_status"] = docker_statuses.get(name, "not_checked")

        # Generic tool filter. Dashboard renders
        # the regex field when `tool_filter_supported` is true; greys it
        # out with tooltip otherwise. `tool_filter_arg_name` lets the UI
        # show "Will pass: --enabled-tools <regex>" as a preview.
        if m.tool_filter is not None:
            entry["tool_filter_supported"] = True
            entry["tool_filter_arg_name"] = m.tool_filter.arg_name
        else:
            entry["tool_filter_supported"] = False
            entry["tool_filter_arg_name"] = ""
        state_row = await asyncio.to_thread(mcp_store.get_mcp_state, name)
        entry["tool_filter_regex"] = (
            (state_row or {}).get("tool_filter_regex") or ""
        )

        result.append(entry)

    # Sort: core first, then custom, then community, alphabetical within each
    category_order = {"core": 0, "custom": 1, "community": 2}
    result.sort(key=lambda m: (category_order.get(m["category"], 3), m["label"]))

    return {"mcps": result}


# ---------------------------------------------------------------------------
# Enable / Disable MCPs
# ---------------------------------------------------------------------------

@router.patch("/v1/admin/mcps/{name}/enable")
async def enable_mcp(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    if not mcp_registry.get_manifest(name):
        raise HTTPException(404, f"MCP '{name}' not found")
    await asyncio.to_thread(mcp_store.set_mcp_enabled, name, True)

    # Auto-start Docker container if applicable. Surface the result so the
    # admin UI can show a toast — previously this swallowed failures with a
    # log line and returned "enabled" regardless, leaving the admin confused
    # why their MCP wasn't actually serving traffic.
    m = mcp_registry.get_manifest(name)
    docker_status: str | None = None
    docker_error: str | None = None
    if m and m.server.runtime == "docker":
        try:
            from services.mcp import docker_manager
            ok = await asyncio.to_thread(docker_manager.start_container, m)
            docker_status = "started" if ok else "failed"
            if not ok:
                docker_error = (
                    "docker compose up -d exited non-zero. Likely cause: "
                    "Docker daemon not running, image build failed, or port "
                    "conflict. Check proxy logs for the compose output."
                )
        except Exception as e:
            docker_status = "failed"
            docker_error = (
                f"{type(e).__name__} while starting the container. "
                "Check proxy logs for the full error."
            )
            logger.warning("Failed to auto-start Docker MCP %s: %s", name, e)

    return {
        "status": "enabled",
        "name": name,
        "docker_status": docker_status,
        "docker_error": docker_error,
    }


@router.patch("/v1/admin/mcps/{name}/disable")
async def disable_mcp(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    if not mcp_registry.get_manifest(name):
        raise HTTPException(404, f"MCP '{name}' not found")

    m = mcp_registry.get_manifest(name)
    if m and m.category == "core" and name not in _PLATFORM_DISABLEABLE_CORE:
        raise HTTPException(400, "Core MCPs cannot be disabled at the platform level")

    await asyncio.to_thread(mcp_store.set_mcp_enabled, name, False)

    # Stop Docker container if applicable
    if m and m.server.runtime == "docker":
        try:
            from services.mcp import docker_manager
            await asyncio.to_thread(docker_manager.stop_container, m)
        except Exception as e:
            logger.warning("Failed to stop Docker MCP %s: %s", name, e)

    return {"status": "disabled", "name": name}


# ---------------------------------------------------------------------------
# Tool filter
# ---------------------------------------------------------------------------


class _ToolFilterRequest(BaseModel):
    regex: str = ""


@router.put("/v1/admin/mcps/{name}/tool-filter")
async def set_mcp_tool_filter(
    name: str,
    body: _ToolFilterRequest,
    user: UserContext = Depends(get_current_user),
):
    """Update the per-MCP runtime tool filter regex.

    Empty string clears the filter. Rejects MCPs that don't declare
    ``tool_filter`` in their manifest (the runtime would silently ignore
    the regex — fail loudly instead so the admin doesn't think it's
    taking effect).

    Restarts the Docker container if applicable so the new flag takes
    effect immediately (the container's ENTRYPOINT reads
    ``$ENABLED_TOOLS_FLAG`` at startup, not per-request).
    """
    _require_admin(user)
    m = mcp_registry.get_manifest(name)
    if m is None:
        raise HTTPException(404, f"MCP '{name}' not found")
    if m.tool_filter is None:
        raise HTTPException(
            400,
            f"MCP '{name}' does not declare a tool_filter block in its "
            f"manifest. Runtime filtering is unavailable for this MCP.",
        )

    regex = (body.regex or "").strip()
    await asyncio.to_thread(mcp_store.set_tool_filter_regex, name, regex)

    # Restart Docker container (if applicable) so the new
    # ENABLED_TOOLS_FLAG takes effect. Stdio MCPs pick up the new value
    # on their next session spawn — no proxy-side restart needed.
    docker_restarted = False
    if m.server.runtime == "docker":
        try:
            from services.mcp import docker_manager
            docker_restarted = await asyncio.to_thread(
                docker_manager.restart_container, m,
            )
        except Exception as e:
            logger.warning(
                "tool_filter saved but failed to restart Docker MCP %s: %s",
                name, e,
            )

    return {
        "status": "ok",
        "name": name,
        "tool_filter_regex": regex,
        "docker_restarted": docker_restarted,
    }


# ---------------------------------------------------------------------------
# MCP Config Values
# ---------------------------------------------------------------------------

class McpConfigRequest(BaseModel):
    values: dict[str, str]


@router.get("/v1/admin/mcps/{name}/config")
async def get_mcp_config(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    if not mcp_registry.get_manifest(name):
        raise HTTPException(404, f"MCP '{name}' not found")
    values = await asyncio.to_thread(mcp_store.get_mcp_config_values, name)
    return {"name": name, "values": values}


@router.put("/v1/admin/mcps/{name}/config")
async def set_mcp_config(
    name: str, req: McpConfigRequest, user: UserContext = Depends(get_current_user)
):
    _require_admin(user)
    if not mcp_registry.get_manifest(name):
        raise HTTPException(404, f"MCP '{name}' not found")
    await asyncio.to_thread(mcp_store.set_mcp_config_values, name, req.values)
    return {"status": "saved", "name": name}


# ---------------------------------------------------------------------------
# Hosted Service Mode
# ---------------------------------------------------------------------------


class HostedServiceModeRequest(BaseModel):
    mode: str  # "self_managed" or "hosted"


@router.put("/v1/admin/mcps/{name}/hosted-service-mode")
async def set_hosted_service_mode(
    name: str, body: HostedServiceModeRequest,
    user: UserContext = Depends(get_current_user),
):
    """Set hosted OAuth mode for an MCP.

    hosted = route the OAuth exchange/refresh through the OtoDock relay
    (which holds OtoDock's client_secret); self_managed = admin provides
    their own OAuth app credentials. Applies only to MCPs declaring
    ``hosted.oauth_app``.
    """
    _require_admin(user)
    manifest = mcp_registry.get_manifest(name)
    if not manifest or not (manifest.hosted and manifest.hosted.oauth_app):
        raise HTTPException(400, f"MCP '{name}' does not support hosted OAuth")
    if body.mode not in ("self_managed", "hosted"):
        raise HTTPException(400, "Mode must be 'self_managed' or 'hosted'")
    await asyncio.to_thread(
        mcp_store.set_mcp_config_value, name, "_hosted_service_mode", body.mode
    )
    return {"status": "ok", "name": name, "mode": body.mode}


class NetworkAccessRequest(BaseModel):
    enabled: bool


@router.put("/v1/admin/mcps/{name}/network-access")
async def set_network_access(
    name: str, body: NetworkAccessRequest,
    user: UserContext = Depends(get_current_user),
):
    """Toggle internal-network access for an MCP that declares network_targets.

    When on, the agent sandbox carves egress to the MCP's configured target
    host(s). Rejects MCPs that declare no ``network_targets`` (the
    toggle would have no effect — fail loud, like the tool-filter endpoint).
    Unavailable on hosted OtoDock (no operator LAN).
    """
    _require_admin(user)
    manifest = mcp_registry.get_manifest(name)
    if manifest is None:
        raise HTTPException(404, f"MCP '{name}' not found")
    if not manifest.network_targets:
        raise HTTPException(
            400, f"MCP '{name}' declares no network_targets — internal-network "
            f"access does not apply to it.")
    if config.OTODOCK_CLOUD:
        raise HTTPException(
            400, "Internal-network access is unavailable on hosted OtoDock.")
    await asyncio.to_thread(
        mcp_store.set_mcp_config_value, name, "_network_access",
        "true" if body.enabled else "false",
    )
    return {"status": "ok", "name": name, "network_access": body.enabled}


# ---------------------------------------------------------------------------
# Docker Lifecycle
# ---------------------------------------------------------------------------

@router.post("/v1/admin/mcps/{name}/docker/start")
async def docker_start(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    m = mcp_registry.get_manifest(name)
    if not m or m.server.runtime != "docker":
        raise HTTPException(400, "Not a Docker MCP")
    from services.mcp import docker_manager
    ok = await asyncio.to_thread(docker_manager.start_container, m)
    if not ok:
        raise HTTPException(500, "Failed to start container")
    return {"status": "started", "name": name}


@router.post("/v1/admin/mcps/{name}/docker/stop")
async def docker_stop(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    m = mcp_registry.get_manifest(name)
    if not m or m.server.runtime != "docker":
        raise HTTPException(400, "Not a Docker MCP")
    from services.mcp import docker_manager
    ok = await asyncio.to_thread(docker_manager.stop_container, m)
    if not ok:
        raise HTTPException(500, "Failed to stop container")
    return {"status": "stopped", "name": name}


@router.post("/v1/admin/mcps/{name}/docker/restart")
async def docker_restart(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    m = mcp_registry.get_manifest(name)
    if not m or m.server.runtime != "docker":
        raise HTTPException(400, "Not a Docker MCP")
    from services.mcp import docker_manager
    ok = await asyncio.to_thread(docker_manager.restart_container, m)
    if not ok:
        raise HTTPException(500, "Failed to restart container")
    return {"status": "restarted", "name": name}


@router.get("/v1/admin/mcps/{name}/docker/status")
async def docker_status(name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    m = mcp_registry.get_manifest(name)
    if not m or m.server.runtime != "docker":
        raise HTTPException(400, "Not a Docker MCP")
    from services.mcp import docker_manager
    status = await asyncio.to_thread(docker_manager.get_container_status, m)
    return {"name": name, "status": status}


# ---------------------------------------------------------------------------
# Agent MCP Assignments
# ---------------------------------------------------------------------------

class AgentMcpRequest(BaseModel):
    mcps: list[str]


@router.get("/v1/agents/{name}/mcps")
async def get_agent_mcps(name: str, user: UserContext = Depends(get_current_user)):
    """List all MCPs visible to an agent with their manager-enabled state.

    Visibility = auto-mode MCPs (always available) + explicit-mode MCPs the admin
    has authorized via at least one instance (agent in instance.agents OR
    instance.assigned_to_all=True). Each row indicates whether the manager has
    currently enabled it for the agent. Both rows can be toggled by the manager;
    the only difference for `authorized_by="admin"` rows is the `via admin` UI
    hint shown next to them.
    """
    _require_manage(user, name)

    visible = await asyncio.to_thread(mcp_registry.get_visible_mcps_for_agent, name)
    # Standalone skill packages never surface as MCPs: the Skills tab is
    # their single control surface (GET/PATCH /v1/agents/{name}/skills).
    # This endpoint also feeds mcps-mcp's list_enabled_mcps — a tool-less
    # "MCP" row would confuse both humans and agents.
    visible = [m for m in visible if m.category != "skill"]
    enabled_set = set(
        await asyncio.to_thread(mcp_store.get_manager_enabled_mcps, name)
    )

    mcps = []
    for m in visible:
        cred = mcp_registry.get_credential_schema(m.name)
        mcps.append({
            "name": m.name,
            "label": m.label,
            "description": m.description,
            "category": m.category,
            "assignment_mode": m.assignment_mode,
            "credential_type": cred.get("type", "none") if cred else "none",
            # Lets the UI render the service-account binding dropdown only
            # where it applies — probing the options endpoint for every row
            # painted a 400 per non-capable MCP in the console.
            "has_service_account": bool(cred.get("has_service_account")) if cred else False,
            "enabled": m.name in enabled_set,
            "authorized_by": "auto" if m.assignment_mode == "auto" else "admin",
        })

    # Sort: core category first, then alphabetical by label.
    mcps.sort(key=lambda x: (x["category"] != "core", x["label"]))

    return {"mcps": mcps}


@router.put("/v1/agents/{name}/mcps")
async def set_agent_mcps(
    name: str, req: AgentMcpRequest, user: UserContext = Depends(get_current_user)
):
    """Set the manager-enabled MCP set for an agent (replace all).

    Validates each name against the agent's current visibility set; any names
    not visible (e.g. admin revoked authorization between the UI fetch and this
    write) are rejected with 400 + a `not_visible` list so the frontend can
    re-fetch and surface an actionable error.

    Auth: any user with manage rights on this agent (admin OR per-agent manager).
    """
    _require_manage(user, name)

    visible_manifests = await asyncio.to_thread(
        mcp_registry.get_visible_mcps_for_agent, name,
    )
    # Skill packages are managed via the Skills tab, never through this
    # replace-all endpoint: reject them in the payload (two control surfaces
    # would fight), and PRESERVE any already-assigned ones — the MCPs tab
    # and mcps-mcp's GET-then-PUT round-trip a skill-filtered list, so a
    # plain replace would silently wipe standalone skill enablement.
    skill_pkg_names = {m.name for m in visible_manifests if m.category == "skill"}
    in_payload = [n for n in req.mcps if n in skill_pkg_names]
    if in_payload:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Skill packages are managed via the Skills tab",
                "skill_packages": in_payload,
            },
        )
    visible_names = {m.name for m in visible_manifests}
    not_visible = [n for n in req.mcps if n not in visible_names]
    if not_visible:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MCPs not visible to this agent",
                "not_visible": not_visible,
            },
        )
    prior = set(await asyncio.to_thread(mcp_store.get_manager_enabled_mcps, name))
    preserved_skill_pkgs = sorted(prior & skill_pkg_names)
    effective_mcps = list(req.mcps) + preserved_skill_pkgs

    # Audit log: managers enabling assigned_to_all-only instances. Helps admins
    # see who is consuming shared resources without a separate audit table.
    newly_enabled = set(effective_mcps) - prior
    if newly_enabled:
        for mcp_name in newly_enabled:
            m = mcp_registry.get_manifest(mcp_name)
            if not m or m.assignment_mode != "explicit":
                continue
            # Check whether the visibility came ONLY from assigned_to_all
            instances = await asyncio.to_thread(mcp_store.get_mcp_instances, mcp_name)
            has_explicit = any(name in inst["agents"] for inst in instances)
            has_catchall = any(inst["assigned_to_all"] for inst in instances)
            if not has_explicit and has_catchall:
                logger.info(
                    "manager %s enabled assigned_to_all MCP %s on agent %s",
                    user.sub, mcp_name, name,
                )

    await asyncio.to_thread(mcp_store.set_manager_enabled_mcps, name, effective_mcps)

    # Auto-create skill rows for newly enabled MCPs (idempotent — preserves prior
    # state if a skill was customized then disabled then re-enabled).
    for mcp_name in effective_mcps:
        m = mcp_registry.get_manifest(mcp_name)
        if m:
            for skill in m.skills:
                await asyncio.to_thread(
                    mcp_store.ensure_agent_skill,
                    name, skill.id, True, skill.default_exclude_from,
                )

    return {"status": "saved", "agent": name, "mcps": effective_mcps}


@router.get("/v1/agents/{name}/ssh-hosts")
async def get_agent_ssh_hosts(
    name: str,
    target_os: str = "linux",
    user: UserContext = Depends(get_current_user),
):
    """The agent's authorized ssh-hosts instances, with ready-to-run commands.

    Backs the ssh-hosts MCP's ``list_ssh_hosts`` tool — the queryable twin of
    the SSH Hosts prompt block (static context fades in long sessions).
    Returns key NAMES only; key material never leaves the materializer /
    session-file-broker path. ``target_os`` gates the ControlMaster mux
    options in ``command`` (no unix-socket mux on Windows).

    Auth: the agent's OWN session JWT (the tool path — any role, matching the
    prompt block every session already receives), or manage rights on the
    agent (dashboard/debug callers).
    """
    if not (user and user.is_session and user.agent == name):
        _require_manage(user, name)

    if mcp_registry.get_manifest("ssh-hosts") is None:
        raise HTTPException(404, "ssh-hosts MCP is not installed")
    agent_mcps = await asyncio.to_thread(mcp_registry.get_agent_mcps, name)
    if not any(m.name == "ssh-hosts" for m in agent_mcps):
        raise HTTPException(403, "ssh-hosts is not enabled for this agent")

    from services.mcp.dynamic_context import format_ssh_host_command

    mux = (target_os or "").strip().lower() in ("linux", "darwin")
    instances = await asyncio.to_thread(
        mcp_store.get_mcp_instances_for_agent, "ssh-hosts", name,
    )
    hosts = []
    for inst in instances:
        fv = inst.get("field_values", {}) or {}
        command = format_ssh_host_command(fv, mux=mux)
        if command is None:
            continue
        host = (fv.get("host") or "").strip()
        hosts.append({
            "name": (fv.get("name") or "").strip() or host,
            "host": host,
            "port": str(fv.get("port") or "22").strip() or "22",
            "username": (fv.get("username") or "").strip(),
            "key_name": (fv.get("key_name") or "").strip(),
            "command": command,
        })
    return {"hosts": hosts}


# ---------------------------------------------------------------------------
# Agent Skill Assignments
# ---------------------------------------------------------------------------

class SkillUpdateRequest(BaseModel):
    enabled: bool
    exclude_from: list[str] = []


@router.get("/v1/agents/{name}/skills")
async def get_agent_skills(name: str, user: UserContext = Depends(get_current_user)):
    """List skills for an agent with their state — the Skills tab's data source.

    Two sources, one list: skills bundled with ASSIGNED MCPs (configuration
    view — a device-local MCP's skills must be configurable with no target in
    context) plus skills from VISIBLE standalone skill packages
    (``category: "skill"``), including unassigned ones so the tab can offer
    enabling them (``assigned: false`` → PATCH enable auto-assigns the
    package). ``standalone`` + ``loading`` drive the tab's source label and
    loading-mode badge.
    """
    _require_manage(user, name)

    db_skills = await asyncio.to_thread(mcp_store.get_agent_skills, name)
    skill_map = {s["skill_id"]: s for s in db_skills}

    assigned = mcp_registry.get_agent_mcps_all_placements(name)
    assigned_names = {m.name for m in assigned}
    visible_skill_pkgs = [
        m for m in await asyncio.to_thread(
            mcp_registry.get_visible_mcps_for_agent, name)
        if m.category == "skill" and m.name not in assigned_names
    ]

    result = []
    for m in list(assigned) + visible_skill_pkgs:
        pkg_assigned = m.name in assigned_names
        for skill_def in m.skills:
            db_entry = skill_map.get(skill_def.id)
            result.append({
                "id": skill_def.id,
                "mcp_name": m.name,
                "mcp_label": m.label,
                "description": skill_def.description,
                "standalone": m.category == "skill",
                "loading": skill_def.loading,
                "assigned": pkg_assigned,
                # An unassigned package's skills are OFF regardless of rows.
                "enabled": (pkg_assigned
                            and (db_entry["enabled"] if db_entry else True)),
                "exclude_from": db_entry["exclude_from"] if db_entry else skill_def.default_exclude_from,
                "default_exclude_from": skill_def.default_exclude_from,
            })

    return {"skills": result}


@router.patch("/v1/agents/{name}/skills/{skill_id}")
async def update_agent_skill(
    name: str, skill_id: str, req: SkillUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Toggle a skill / edit its context exclusions.

    Manager-gated like MCP assignment (same trust tier — was admin-only when
    no UI existed). Enabling a skill whose provider is an UNASSIGNED
    standalone package auto-assigns the package (the Skills tab is the only
    control surface for skill packages; the MCPs replace-all PUT rejects and
    preserves them). Unknown skill ids 404 instead of writing blind rows.
    """
    _require_manage(user, name)

    provider = mcp_registry.find_skill_provider(skill_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_id}")

    if req.enabled and provider.category == "skill":
        visible = await asyncio.to_thread(
            mcp_registry.get_visible_mcps_for_agent, name)
        if not any(m.name == provider.name for m in visible):
            raise HTTPException(
                status_code=400,
                detail=f"Skill package {provider.name} is not available to "
                       f"this agent",
            )
        enabled_pkgs = set(await asyncio.to_thread(
            mcp_store.get_manager_enabled_mcps, name))
        if provider.name not in enabled_pkgs:
            await asyncio.to_thread(mcp_store.add_agent_mcp, name, provider.name)

    await asyncio.to_thread(
        mcp_store.set_agent_skill, name, skill_id, req.enabled, req.exclude_from
    )
    return {"status": "saved", "agent": name, "skill_id": skill_id}


# ---------------------------------------------------------------------------
# SSH Key Management (file-based, used by ssh_key_select input type)
# ---------------------------------------------------------------------------

def _ssh_keys_dir():
    m = mcp_registry.get_manifest("ssh-hosts")
    if not m:
        return config.MCPS_DIR / "custom" / "ssh-hosts" / "keys"
    return m.mcp_dir / "keys"


@router.get("/v1/admin/ssh/keys")
async def list_ssh_keys(user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    keys_dir = _ssh_keys_dir()
    keys_dir.mkdir(parents=True, exist_ok=True)
    keys = []
    for f in sorted(keys_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            keys.append({"name": f.name, "size": f.stat().st_size})
    return {"keys": keys}


@router.post("/v1/admin/ssh/keys")
async def upload_ssh_key(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    if not file.filename:
        raise HTTPException(400, "No filename")
    # Sanitize filename
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
    keys_dir = _ssh_keys_dir()
    keys_dir.mkdir(parents=True, exist_ok=True)
    dest = keys_dir / safe_name
    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(400, "Key file too large (max 64KB)")
    dest.write_bytes(content)
    dest.chmod(0o600)
    return {"status": "uploaded", "name": safe_name}


@router.delete("/v1/admin/ssh/keys/{key_name}")
async def delete_ssh_key(key_name: str, user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', key_name)
    key_path = _ssh_keys_dir() / safe_name
    if not key_path.is_file():
        raise HTTPException(404, "Key not found")
    key_path.unlink()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# MCP Instance Management (generalized per-instance, per-agent config)
# ---------------------------------------------------------------------------


class McpInstanceRequest(BaseModel):
    instance_name: str
    field_values: dict[str, str] = {}
    agents: list[str] = []
    # When True, this instance authorizes ALL current and future agents (in
    # addition to any explicit names in `agents`). Used for shared resources
    # like a platform-wide Google Maps API key. Per the precedence rule in
    # mcp_store.get_instance_for_agent_env_delivery, explicit assignment in
    # `agents` still takes precedence at runtime — `assigned_to_all` is the
    # catch-all fallback.
    assigned_to_all: bool = False
    # 'self_managed' (inject field_values) | 'hosted' (route through
    # the OtoDock relay). Admin-created instances default to self_managed —
    # they bring their own key. `managed_by` is server-set (never client-set):
    # 'admin' for these, 'system' only for the startup auto-instance pass.
    hosted_mode: str = "self_managed"


@router.get("/v1/admin/mcps/{name}/instances")
async def list_mcp_instances(name: str, user: UserContext = Depends(get_current_user)):
    """List all instances for an MCP with field schema."""
    _require_admin(user)
    manifest = mcp_registry.get_manifest(name)
    if not manifest or not manifest.instances:
        raise HTTPException(400, f"MCP '{name}' does not use instances")

    instances = await asyncio.to_thread(mcp_store.get_mcp_instances, name)

    # Strip secret field values from response (return configured status only)
    secret_keys = {f.key for f in manifest.instances.fields if f.secret}
    for inst in instances:
        configured_keys = []
        for k in secret_keys:
            if inst.get("field_values", {}).get(k):
                configured_keys.append(k)
                inst["field_values"][k] = ""
        if configured_keys:
            inst["configured_keys"] = configured_keys

    return {
        "instances": instances,
        "fields": [
            {
                "key": f.key, "label": f.label, "input_type": f.input_type,
                "default": f.default, "required": f.required, "secret": f.secret,
            }
            for f in manifest.instances.fields
        ],
        "delivery": manifest.instances.delivery,
        "max_instances": manifest.instances.max_instances,
    }


async def _retry_install_failed_for_instance(
    name: str, agents: list[str], assigned_to_all: bool, admin_sub: str,
) -> list[int]:
    """Auto-retry ``install_failed`` requests now authorized by a new instance.

    When admin saves an instance (create or update) that authorizes some
    agents — either by listing them in ``agents`` or via ``assigned_to_all``
    — sweep the request queue for ``install_failed`` rows on this MCP whose
    agent is now covered, and run ``approve_request`` inline for each. The
    install step is a no-op (manifest already exists) and
    ``_ensure_agent_authorized_for_instance_mcp`` now hits its
    ``already_authorized`` branch because admin just put the agent on the
    instance. The request flips ``install_failed → installing → installed``
    + the requester gets the standard "approved" notification.

    Returns the list of retried request IDs (empty if no matches).
    Failures are logged and swallowed so the parent instance-save still
    returns 200 — auto-retry is a UX nicety, not a hard contract.
    """
    from storage import mcp_request_store
    from services.community import community_installer

    failed = await asyncio.to_thread(
        mcp_request_store.list_install_failed_for_mcp, name,
    )
    if not failed:
        return []

    retried: list[int] = []
    for req in failed:
        if not (assigned_to_all or req["agent_slug"] in agents):
            continue
        try:
            await community_installer.approve_request(
                req["id"], admin_sub,
                admin_note=req.get("admin_note") or "Auto-retried after instance save.",
            )
            retried.append(req["id"])
        except Exception:
            logger.exception(
                "Auto-retry failed for request %s (mcp=%s agent=%s)",
                req["id"], name, req["agent_slug"],
            )
    return retried


@router.post("/v1/admin/mcps/{name}/instances")
async def create_mcp_instance(
    name: str, req: McpInstanceRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a new MCP instance.

    On success, sweeps the request queue and auto-retries any
    ``install_failed`` requests for this MCP whose agent is authorized
    by the new instance — eliminates the manual Retry click for the
    common "manager requests explicit-mode MCP → admin sees failure →
    admin creates instance → request stays failed until manual retry"
    flow.
    """
    _require_admin(user)
    manifest = mcp_registry.get_manifest(name)
    if not manifest or not manifest.instances:
        raise HTTPException(400, f"MCP '{name}' does not use instances")

    instance_id = await asyncio.to_thread(
        mcp_store.upsert_mcp_instance, name,
        {
            "instance_name": req.instance_name,
            "field_values": req.field_values,
            "agents": req.agents,
            "assigned_to_all": req.assigned_to_all,
            "hosted_mode": req.hosted_mode,
            # managed_by is server-set: admin-created instances are 'admin'.
            # 'system' is reserved for the startup auto-instance pass.
            "managed_by": "admin",
        },
    )

    retried = await _retry_install_failed_for_instance(
        name, req.agents, req.assigned_to_all, user.sub,
    )
    return {"status": "created", "id": instance_id, "retried_request_ids": retried}


@router.put("/v1/admin/mcps/{name}/instances/{instance_id}")
async def update_mcp_instance(
    name: str, instance_id: int, req: McpInstanceRequest,
    user: UserContext = Depends(get_current_user),
):
    """Update an MCP instance. Empty secret fields preserve existing values.

    Auto-retries any ``install_failed`` requests whose agent is now
    authorized — same hook as create. Useful when admin first creates a
    bare instance, then later adds an agent to it (this PUT triggers the
    retry just like the agent-on-create path does).
    """
    _require_admin(user)
    manifest = mcp_registry.get_manifest(name)
    if not manifest or not manifest.instances:
        raise HTTPException(400, f"MCP '{name}' does not use instances")

    # Fetch the existing row once — used for secret preservation AND the
    # system-instance guard below.
    existing = await asyncio.to_thread(mcp_store.get_mcp_instances, name)
    existing_inst = next((i for i in existing if i["id"] == instance_id), None)

    # Guard: admin may rename/scope/delete a platform-managed ('system')
    # instance but NOT flip it to self_managed — it has no key to fall back
    # on. They create a separate self-managed instance instead.
    if (
        existing_inst
        and existing_inst.get("managed_by") == "system"
        and req.hosted_mode != "hosted"
    ):
        raise HTTPException(
            400,
            "Cannot convert a platform-managed instance to self-managed. "
            "Create a separate self-managed instance instead.",
        )

    # Preserve existing secret values when not provided
    secret_keys = {f.key for f in manifest.instances.fields if f.secret}
    if secret_keys and existing_inst:
        for k in secret_keys:
            if not req.field_values.get(k):
                req.field_values[k] = existing_inst["field_values"].get(k, "")

    # Update by primary key — NOT upsert. Previous version called
    # upsert_mcp_instance which keyed on (mcp_name, instance_name),
    # so renaming an instance silently INSERTed a fresh row and left
    # the original orphaned. update_mcp_instance_by_id targets the
    # ``{instance_id}`` in the URL directly.
    try:
        updated = await asyncio.to_thread(
            mcp_store.update_mcp_instance_by_id, instance_id, name,
            {
                "instance_name": req.instance_name,
                "field_values": req.field_values,
                "agents": req.agents,
                "assigned_to_all": req.assigned_to_all,
                "hosted_mode": req.hosted_mode,
            },
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not updated:
        raise HTTPException(
            404, f"Instance {instance_id} not found for MCP '{name}'",
        )

    retried = await _retry_install_failed_for_instance(
        name, req.agents, req.assigned_to_all, user.sub,
    )
    return {"status": "updated", "id": instance_id, "retried_request_ids": retried}


@router.delete("/v1/admin/mcps/{name}/instances/{instance_id}")
async def delete_mcp_instance(
    name: str, instance_id: int,
    user: UserContext = Depends(get_current_user),
):
    """Delete an MCP instance.

    Platform-managed ('system') instances — the auto-created "OtoDock Hosted"
    relay instances — are NOT deletable (there is no UI to recreate one once
    gone), so they're rejected here; admins rename/re-scope instead.
    Admin-created instances delete normally.
    """
    _require_admin(user)
    sys_inst = await asyncio.to_thread(mcp_store.get_system_instance, name)
    if sys_inst and sys_inst.get("id") == instance_id:
        raise HTTPException(
            400,
            "Platform-managed (OtoDock Hosted) instances can't be deleted — "
            "rename or re-scope it instead.",
        )
    await asyncio.to_thread(
        mcp_store.delete_mcp_instance_with_tombstone, instance_id,
    )
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# MCP Delete
# ---------------------------------------------------------------------------


@router.delete("/v1/admin/mcps/{name}")
async def delete_mcp(name: str, user: UserContext = Depends(get_current_user)):
    """Delete an MCP — removes DB data, credentials, and folder."""
    import shutil

    _require_admin(user)

    manifest = mcp_registry.get_manifest(name)
    if not manifest:
        raise HTTPException(404, f"MCP '{name}' not found")

    if manifest.category not in ("community", "skill"):
        raise HTTPException(
            400,
            "Only community MCPs and skill packages can be deleted. Core and "
            "custom MCPs ship with the platform.",
        )

    # Remove all DB data (state, agent assignments, skills, config values).
    # Skill ids are bare identifiers — pass the manifest's ids so the
    # agent_skills rows go with the provider.
    await asyncio.to_thread(
        mcp_store.delete_mcp_all_data, name,
        [s.id for s in (manifest.skills or [])],
    )

    # Remove credentials (infra, service accounts, all user credentials)
    from storage import credential_store
    await asyncio.to_thread(credential_store.delete_all_mcp_credentials, name)

    # Self-host (T1/T2): tear the Docker container + its named volumes down
    # before removing the folder — otherwise a delete orphans a running
    # container and its data volumes (the compose file lives in the folder we
    # are about to rmtree, so this must happen first). On cloud (external-pool)
    # there is no per-install container — the MCP is just a connection to the
    # OtoDock-owned central pool — so we skip teardown and the DB-config removal
    # above is the whole delete.
    from core.config import deployment
    if (
        manifest.server.runtime == "docker"
        and deployment.current_mode() != deployment.EXTERNAL_POOL
    ):
        from services.mcp import docker_manager
        await asyncio.to_thread(docker_manager.remove_container, manifest)

    # Remove folder
    mcp_dir = manifest.mcp_dir
    if mcp_dir.exists():
        shutil.rmtree(mcp_dir, ignore_errors=True)

    # Re-scan manifests
    mcp_registry.scan_manifests()

    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# MCP Version Checking & Update
# ---------------------------------------------------------------------------

@router.get("/v1/admin/mcps/check-updates")
async def check_mcp_updates(user: UserContext = Depends(get_current_user)):
    """Check npm/pypi + the community catalog for newer MCP versions (admin).

    Detection lives in ``services/mcp/mcp_updater`` so the manual button and the
    weekly automatic-update job share one implementation.
    """
    _require_admin(user)
    from services.mcp import mcp_updater
    return await mcp_updater.detect_available_updates()


@router.post("/v1/admin/mcps/{name}/update")
async def update_mcp_version(name: str, user: UserContext = Depends(get_current_user)):
    """Update an MCP to its latest version (admin).

    npm/pypi MCPs converge to the community catalog folder and install the latest
    version within the catalog ``version_constraint`` (also picks up integration
    manifest changes); Docker MCPs re-fetch from the catalog and pull (T2) /
    rebuild (T1) the image. The actual work lives in
    ``services/mcp/mcp_updater.update_one`` (shared with the weekly automatic-update
    job), which holds the per-MCP install lock for the duration.
    """
    _require_admin(user)
    from services.mcp import mcp_updater
    return await mcp_updater.update_one(name)


@router.get("/v1/admin/mcps/auto-update-log")
async def get_mcp_auto_update_log(user: UserContext = Depends(get_current_user)):
    """Recent automatic-update run history for the admin Setup card (admin).

    Returns the most recent per-MCP result rows (the dashboard groups them by
    ``run_id``) plus the last-run timestamp — which is set even when a run found
    nothing to update, so the status line can show "last run … — up to date".
    """
    _require_admin(user)
    from storage import database as _db, mcp_autoupdate_store
    from services.mcp import mcp_autoupdate
    runs = await asyncio.to_thread(mcp_autoupdate_store.recent_runs, 50)
    last_run_at = await asyncio.to_thread(
        _db.get_platform_setting, mcp_autoupdate.LAST_RUN_KEY,
    )
    return {"runs": runs, "last_run_at": last_run_at or ""}


# ---------------------------------------------------------------------------
# MCP Install / Update (zip upload)
# ---------------------------------------------------------------------------

# Source-spec safety (leading-dash / shell-char rejection) + the recognised
# registry prefixes (npm/pypi/git+/docker) live in services/mcp/mcp_installer.py
# ``parse_source`` / ``_spec_is_safe`` — the single source of truth. An
# unrecognised prefix already parses to None there (not installed).
_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB


@router.post("/v1/admin/mcps/install")
async def install_mcp(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
):
    """Install or update an MCP from a zip upload.

    The zip must contain a folder with a manifest.json at the root (or
    directly a manifest.json + supporting files). The shared install pipeline
    in ``services.community.community_installer`` handles validation, copy, dependency
    install, rollback, and .env regeneration.
    """
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path
    from services.community import community_installer

    _require_admin(user)

    content = await file.read()
    if len(content) > _MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"File too large (max {_MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are accepted")

    tmp = Path(tempfile.mkdtemp(prefix="mcp-install-"))
    try:
        zip_path = tmp / "upload.zip"
        zip_path.write_bytes(content)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
                if len(infos) > 10000:
                    raise HTTPException(400, "Zip has too many entries")
                # The total DECLARED uncompressed size is the real bomb guard: a
                # single huge member or many members both sum here and are capped.
                # (A per-entry compression-ratio heuristic is intentionally NOT
                # used — valid DEFLATE legitimately reaches ~1000:1 on repetitive
                # content, so it false-positives without catching anything the
                # total-size cap doesn't.)
                total_uncompressed = 0
                for info in infos:
                    if info.filename.startswith("/") or ".." in info.filename:
                        raise HTTPException(400, f"Invalid path in zip: {info.filename}")
                    total_uncompressed += info.file_size
                    if total_uncompressed > config.MCP_ZIP_DECOMPRESSED_MAX:
                        raise HTTPException(400, "Zip expands to more than the allowed size")
                zf.extractall(tmp / "extracted")
        except zipfile.BadZipFile:
            raise HTTPException(400, "Invalid zip file")

        extracted = tmp / "extracted"

        # Find the folder with manifest.json — either the extract root or
        # one level down (zip contained a wrapping folder).
        mcp_root = extracted if (extracted / "manifest.json").is_file() else None
        if mcp_root is None:
            for sd in extracted.iterdir():
                if sd.is_dir() and (sd / "manifest.json").is_file():
                    mcp_root = sd
                    break
        if mcp_root is None:
            raise HTTPException(400, "No manifest.json found in zip")

        return await community_installer.install_from_extracted_folder(mcp_root)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
