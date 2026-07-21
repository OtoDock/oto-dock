"""Community MCP + agent-template catalog API.

Powers the dashboard's Browse Community MCPs UI plus the install /
manager-request / admin-approval and community-agent-template flows. Catalog
reads proxy GitHub raw via :mod:`services.community.community_catalog` and
augment each entry with local platform state (installed, enabled_for_agents,
update_available); the admin/manager actions live under
``/v1/admin/community/mcps/...``, ``/v1/agents/{slug}/mcp-requests``, and
``/v1/community/agents/...``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user
from services.community import community_catalog, community_installer

logger = logging.getLogger("claude-proxy.community-api")
router = APIRouter()


def _require_admin(user: UserContext | None) -> UserContext:
    """Admin-only entry point (install / approve / reject)."""
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def _require_creator_or_admin(user: UserContext | None) -> UserContext:
    """Browse access is available to creators and admins.

    Creators see the catalog from the agent settings flow; admins see it
    from the MCP servers admin page. Members don't have either entry point,
    so we keep the API symmetric with the UI.
    """
    if not user:
        raise HTTPException(403, "Authentication required")
    if user.role not in ("admin", "creator"):
        raise HTTPException(403, "Admin or creator only")
    return user


# ---------------------------------------------------------------------------
# GET /v1/community/mcps
# ---------------------------------------------------------------------------

@router.get("/v1/community/mcps")
async def list_community_mcps(
    agent: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List every MCP in the community catalog with platform-state augmentation.

    Pass ``?agent=<slug>`` to scope the ``pending_request`` field to that
    agent — the manager-side Browse drawer uses this so a row's Request
    button can flip to "Pending" without an extra round trip. Admin context
    (no ``agent`` param) sets ``pending_request`` to ``None`` everywhere but
    populates ``pending_request_count`` (across all agents).
    """
    _require_creator_or_admin(user)

    if agent and not user.can_manage_agent(agent):
        raise HTTPException(403, "Manager access required for this agent")

    try:
        registry = await community_catalog.fetch_registry()
    except Exception as exc:
        logger.exception("Failed to load community registry")
        raise HTTPException(502, f"Could not load community catalog: {exc}")

    installed_versions, enabled_for_agents, pending_requests, installed_manifest_hashes = (
        await community_catalog.collect_local_state()
    )

    augmented = [
        community_catalog.augment_entry(
            entry, installed_versions, enabled_for_agents,
            pending_requests=pending_requests,
            agent_slug=agent,
            installed_manifest_hashes=installed_manifest_hashes,
        )
        for entry in registry.get("mcps", [])
    ]

    return {
        "registry_version": registry.get("registry_version"),
        "updated_at": registry.get("updated_at"),
        "platform_min_version": registry.get("platform_min_version"),
        "fetched_from": community_catalog.REGISTRY_RAW_URL,
        "mcps": augmented,
    }


# ---------------------------------------------------------------------------
# GET /v1/community/mcps/{name}
# ---------------------------------------------------------------------------

@router.get("/v1/community/mcps/{name}")
async def get_community_mcp(
    name: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Return the registry entry + manifest + README for one community MCP.

    The registry entry is the catalog summary; the manifest is the full MCP
    descriptor the platform installer would consume; the README is the
    markdown shown in the detail dialog of the Browse UI.
    """
    _require_creator_or_admin(user)

    try:
        registry = await community_catalog.fetch_registry()
    except Exception as exc:
        logger.exception("Failed to load community registry")
        raise HTTPException(502, f"Could not load community catalog: {exc}")

    entry = next(
        (m for m in registry.get("mcps", []) if m.get("name") == name),
        None,
    )
    if entry is None:
        raise HTTPException(404, f"MCP '{name}' not found in community catalog")

    installed_versions, enabled_for_agents, pending_requests, installed_manifest_hashes = (
        await community_catalog.collect_local_state()
    )
    augmented = community_catalog.augment_entry(
        entry, installed_versions, enabled_for_agents,
        pending_requests=pending_requests,
        installed_manifest_hashes=installed_manifest_hashes,
    )

    try:
        manifest = await community_catalog.fetch_manifest(name)
    except Exception as exc:
        logger.warning("Failed to fetch manifest for %s: %s", name, exc)
        manifest = None

    try:
        readme = await community_catalog.fetch_readme(name)
    except Exception as exc:
        logger.warning("Failed to fetch README for %s: %s", name, exc)
        readme = None

    return {
        "entry": augmented,
        "manifest": manifest,
        "readme": readme,
    }


# ---------------------------------------------------------------------------
# POST /v1/admin/community/mcps/{name}/install  (async background job)
# GET  /v1/admin/community/mcps/installs        (progress poll)
# ---------------------------------------------------------------------------

# Background catalog-install tasks. Held in a module set + discarded on done so
# the event loop can't GC a still-running install (asyncio keeps only a weak
# reference to a bare ``create_task`` result). NOT awaited at shutdown — a 900s
# image pull must never block proxy shutdown; an orphaned install on shutdown is
# the same outcome as a mid-install process kill, which the catalog tolerates.
_install_tasks: set[asyncio.Task] = set()


async def _notify_install_failed(name: str, error: str, admin_sub: str) -> None:
    """Durable failure notification for a direct admin catalog install.

    The manager-request/approve flow has its own ``_notify_request_failed``; the
    direct Browse→Install path had none (it surfaced failures synchronously via
    the HTTP error). Now that it's a background job, reuse the same notification
    machinery so the rollback reason reaches the admin even if the drawer closed.
    Success fires nothing — a completed install is self-evident (the catalog flips
    to Installed).
    """
    from services.notifications import notification_manager
    body = (
        f"Install failed for **{name}**. The catalog is unchanged (rolled back); "
        f"you can retry from Browse Community MCPs."
    )
    excerpt = (error or "")[:300]
    if excerpt:
        body += f"\n\n```\n{excerpt}\n```"
    await notification_manager.fire_notification(
        title="MCP install failed",
        body=body,
        # Platform severity vocabulary is info/success/warning/danger —
        # "error" renders unstyled and plays no sound.
        severity="warning",
        scope="user",
        target=admin_sub,
        source="mcp",
        source_id=f"install:{name}",
    )


async def _run_catalog_install(name: str, admin_sub: str) -> None:
    """Background worker: run the install, drive the progress registry, and fire
    a failure notification on error. Holds the per-MCP install lock for the whole
    pipeline so it can't race the approve-request path on the install dir.
    """
    from core.credentials import catalog_install_registry

    async def _progress(ev: dict) -> None:
        await catalog_install_registry.update(
            name, phase=ev.get("phase"), pct=ev.get("pct"), message=ev.get("message"),
        )

    try:
        async with catalog_install_registry.lock_for(name):
            result = await community_installer.install_from_catalog(
                name, progress_cb=_progress,
            )
        await catalog_install_registry.finish(name, result)
        logger.info(
            "Installed community MCP %s v%s (status=%s)",
            result.get("name"), result.get("version"), result.get("status"),
        )
    except HTTPException as exc:
        msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await catalog_install_registry.fail(name, msg)
        await _notify_install_failed(name, msg, admin_sub)
    except Exception as exc:
        logger.exception("Background install of %s failed", name)
        msg = f"{type(exc).__name__}: {exc}"
        await catalog_install_registry.fail(name, msg)
        await _notify_install_failed(name, msg, admin_sub)


@router.post("/v1/admin/community/mcps/{name}/install")
async def install_community_mcp(
    name: str,
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Start a background install (or update) of a community MCP from the catalog.

    Admin only. Returns ``202`` immediately with the running job; the dashboard
    polls ``GET /v1/admin/community/mcps/installs`` for progress and renders a
    per-MCP bar. A double-click or a second admin hitting the same MCP joins the
    existing job (no second install). The pipeline (validation, copy, npm/pip/
    docker, rollback on failure) runs in the background and completes even if the
    page is closed; failures fire a durable notification.
    """
    _require_admin(user)
    from core.credentials import catalog_install_registry

    # Confirm the MCP exists in the catalog (precise 404) and capture its
    # runtime/label for the job's display before kicking off the background work.
    try:
        registry = await community_catalog.fetch_registry()
    except Exception as exc:
        raise HTTPException(502, f"Could not load community catalog: {exc}")
    entry = next((m for m in registry.get("mcps", []) if m.get("name") == name), None)
    if entry is None:
        raise HTTPException(404, f"MCP '{name}' not found in community catalog")

    job, is_new = await catalog_install_registry.start(
        name,
        triggered_by=user.sub,
        runtime=entry.get("runtime", ""),
        label=entry.get("label") or name,
    )
    if is_new:
        task = asyncio.create_task(_run_catalog_install(name, user.sub))
        _install_tasks.add(task)
        task.add_done_callback(_install_tasks.discard)

    return JSONResponse(
        status_code=202, content={"job": job.to_dict(), "started": is_new},
    )


@router.get("/v1/admin/community/mcps/installs")
async def list_catalog_installs(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """In-flight + recently-completed catalog installs. Admin only.

    The Browse drawer polls this (~1.5s) while open to render per-MCP progress
    bars. Terminal jobs linger briefly (catalog_install_registry retention) so a
    poll reliably catches the done/failed state. Admin-global, like the existing
    ``/v1/admin/mcp-requests`` list.
    """
    _require_admin(user)
    from core.credentials import catalog_install_registry
    return {"installs": [j.to_dict() for j in catalog_install_registry.snapshot()]}


# ---------------------------------------------------------------------------
# Community skills catalog (standalone skill packages)
# ---------------------------------------------------------------------------

@router.get("/v1/community/skills")
async def list_community_skills(
    agent: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List the community-skills catalog with platform-state augmentation.

    Mirrors ``GET /v1/community/mcps`` (same augmentation + ``?agent=``
    scoping) with one deliberate difference: an unreachable, uncached
    registry degrades to an EMPTY catalog with ``catalog_unreachable: true``
    instead of a 502 — the Skills tab renders a banner, never an error.
    """
    _require_creator_or_admin(user)
    if agent and not user.can_manage_agent(agent):
        raise HTTPException(403, "Manager access required for this agent")

    registry = await community_catalog.fetch_skills_registry()
    installed_versions, enabled_for_agents, pending_requests, installed_manifest_hashes = (
        await community_catalog.collect_local_state()
    )
    augmented = [
        community_catalog.augment_entry(
            entry, installed_versions, enabled_for_agents,
            pending_requests=pending_requests,
            agent_slug=agent,
            installed_manifest_hashes=installed_manifest_hashes,
        )
        for entry in registry.get("skills", [])
    ]
    return {
        "registry_version": registry.get("registry_version"),
        "updated_at": registry.get("updated_at"),
        "platform_min_version": registry.get("platform_min_version"),
        "fetched_from": community_catalog.SKILLS_REGISTRY_RAW_URL,
        "catalog_unreachable": bool(registry.get("catalog_unreachable")),
        "skills": augmented,
    }


@router.get("/v1/community/skills/{name}")
async def get_community_skill(
    name: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Registry entry + manifest + README for one community skill package."""
    _require_creator_or_admin(user)

    registry = await community_catalog.fetch_skills_registry()
    entry = next(
        (s for s in registry.get("skills", []) if s.get("name") == name), None,
    )
    if entry is None:
        raise HTTPException(404, f"Skill package '{name}' not found in skills catalog")

    installed_versions, enabled_for_agents, pending_requests, installed_manifest_hashes = (
        await community_catalog.collect_local_state()
    )
    augmented = community_catalog.augment_entry(
        entry, installed_versions, enabled_for_agents,
        pending_requests=pending_requests,
        installed_manifest_hashes=installed_manifest_hashes,
    )
    try:
        manifest = await community_catalog.fetch_skills_manifest(name)
    except Exception:
        manifest = None
    try:
        readme = await community_catalog.fetch_skills_readme(name)
    except Exception:
        readme = ""
    return {"entry": augmented, "manifest": manifest, "readme": readme}


async def _run_skill_catalog_install(name: str, admin_sub: str) -> None:
    """Background worker for a skills-catalog install (mirrors the MCP twin;
    shares the job registry — package/MCP names can't collide by install-time
    guard, so job keys stay unique)."""
    from core.credentials import catalog_install_registry
    from services.community import skills_installer

    async def _progress(ev: dict) -> None:
        await catalog_install_registry.update(
            name, phase=ev.get("phase"), pct=ev.get("pct"), message=ev.get("message"),
        )

    try:
        async with catalog_install_registry.lock_for(name):
            result = await skills_installer.install_skill_package_from_catalog(
                name, progress_cb=_progress,
            )
        await catalog_install_registry.finish(name, result)
        logger.info(
            "Installed community skill package %s v%s (status=%s)",
            result.get("name"), result.get("version"), result.get("status"),
        )
    except HTTPException as exc:
        msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await catalog_install_registry.fail(name, msg)
        await _notify_install_failed(name, msg, admin_sub)
    except Exception as exc:
        logger.exception("Background skill install of %s failed", name)
        msg = f"{type(exc).__name__}: {exc}"
        await catalog_install_registry.fail(name, msg)
        await _notify_install_failed(name, msg, admin_sub)


@router.post("/v1/admin/community/skills/{name}/install")
async def install_community_skill(
    name: str,
    user: UserContext = Depends(get_current_user),
) -> JSONResponse:
    """Start a background install (or update) of a skill package. Admin only.

    Same 202 + job semantics as the MCP install endpoint; progress rides the
    same job registry, polled via either installs listing.
    """
    _require_admin(user)
    from core.credentials import catalog_install_registry

    registry = await community_catalog.fetch_skills_registry()
    entry = next(
        (s for s in registry.get("skills", []) if s.get("name") == name), None,
    )
    if entry is None:
        if registry.get("catalog_unreachable"):
            raise HTTPException(502, "Skills catalog is unreachable")
        raise HTTPException(404, f"Skill package '{name}' not found in skills catalog")

    job, is_new = await catalog_install_registry.start(
        name,
        triggered_by=user.sub,
        runtime="none",
        label=entry.get("label") or name,
    )
    if is_new:
        task = asyncio.create_task(_run_skill_catalog_install(name, user.sub))
        _install_tasks.add(task)
        task.add_done_callback(_install_tasks.discard)

    return JSONResponse(
        status_code=202, content={"job": job.to_dict(), "started": is_new},
    )


@router.get("/v1/admin/community/skills/installs")
async def list_skill_catalog_installs(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Alias of the shared install-jobs snapshot (jobs are keyed by name and
    names are unique across both catalogs)."""
    _require_admin(user)
    from core.credentials import catalog_install_registry
    return {"installs": [j.to_dict() for j in catalog_install_registry.snapshot()]}


# ---------------------------------------------------------------------------
# Manager request flow
# ---------------------------------------------------------------------------

class CreateRequestBody(BaseModel):
    mcp_name: str
    # Optional human justification — surfaced on the admin Requests page +
    # in the per-admin notification body. The ``mcps-mcp`` MCP tool marks
    # this required (LLMs compose context-aware reasons cheaply); the UI
    # treats it as optional to keep the manager flow low-friction.
    reason: str = ""
    # Which catalog the requested name lives in: 'mcp' (community-mcps,
    # default) or 'skill' (community-skills). Drives validation here and the
    # installer dispatch in approve_request.
    kind: str = "mcp"


class AdminResolveBody(BaseModel):
    admin_note: str = ""


def _augment_request_row(row: dict) -> dict:
    """Normalise a request row for the API response (coerce the SERIAL ``id`` to int)."""
    return {
        **row,
        # Coerce SERIAL id to int just in case psycopg returned ``str`` (it
        # doesn't normally, but the API contract is explicit).
        "id": int(row["id"]),
    }


@router.post("/v1/agents/{slug}/mcp-requests")
async def create_mcp_request(
    slug: str,
    body: CreateRequestBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Create an MCP assignment request for an agent.

    Manager flow: creates a pending row, notifies every admin. Admin then
    approves via ``POST /v1/admin/mcp-requests/{id}/approve``.

    Admin flow: the request is **auto-approved inline** — same
    ``approve_request`` orchestration runs synchronously, returning the
    resolved row (``installed`` on success, ``install_failed`` with an
    ``admin_note`` for explicit-mode MCPs missing instance config).
    Skips the queue + admin notification entirely so the admin doesn't
    end up approving their own requests through the dashboard. The
    ``reason`` field is still persisted (audit trail), even though no
    second pair of eyes will read it.

    The MCP must exist in the catalog. If already enabled on the agent
    we 409 (nothing to do).
    """
    if not user:
        raise HTTPException(403, "Authentication required")
    if not user.can_manage_agent(slug):
        raise HTTPException(403, "Manager access required for this agent")

    if body.kind not in ("mcp", "skill"):
        raise HTTPException(400, f"Invalid request kind: {body.kind!r}")

    # Confirm catalog entry exists (defense against drive-by POSTs) — against
    # the catalog matching the request kind.
    if body.kind == "skill":
        skills_registry = await community_catalog.fetch_skills_registry()
        if not any(s.get("name") == body.mcp_name
                   for s in skills_registry.get("skills", [])):
            raise HTTPException(
                404,
                f"Skill package '{body.mcp_name}' not found in skills catalog",
            )
    else:
        try:
            registry = await community_catalog.fetch_registry()
        except Exception as exc:
            raise HTTPException(502, f"Could not load community catalog: {exc}")
        if not any(m.get("name") == body.mcp_name for m in registry.get("mcps", [])):
            raise HTTPException(404, f"MCP '{body.mcp_name}' not found in community catalog")

    # Short-circuit when the MCP is already enabled on the agent — no need to
    # bother the admin.
    from storage import mcp_store, mcp_request_store
    current = await asyncio.to_thread(mcp_store.get_manager_enabled_mcps, slug)
    if body.mcp_name in current:
        raise HTTPException(409, f"{body.mcp_name} is already enabled on {slug}")

    try:
        row = await asyncio.to_thread(
            mcp_request_store.create_request,
            body.mcp_name, slug, user.sub,
            (body.reason or "").strip(),
            None,
            body.kind,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    if user.role == "admin":
        # Admin requester → no queue paperwork; resolve inline.
        # ``approve_request`` handles the full cascade (install if
        # missing → attach to instance for explicit-mode MCPs → enable
        # for agent → notification to the requester). For an admin
        # requesting on their own behalf, the requester notification
        # they get back to themselves is the same "request approved"
        # they'd get for a manager-flow approval — small price for a
        # uniform code path.
        try:
            resolved = await community_installer.approve_request(
                row["id"], user.sub,
                admin_note="Auto-approved (admin self-request).",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Admin auto-approve failed for %s", row["id"])
            raise HTTPException(500, f"Auto-approve failed: {exc}")
        return _augment_request_row(resolved)

    # Manager flow: queue + notify every admin.
    await community_installer.notify_request_created(row)
    return _augment_request_row(row)


@router.get("/v1/agents/{slug}/mcp-requests")
async def list_agent_mcp_requests(
    slug: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List MCP requests for one agent. Managers see only their own; admins see all."""
    if not user:
        raise HTTPException(403, "Authentication required")
    if not user.can_manage_agent(slug):
        raise HTTPException(403, "Manager access required for this agent")

    from storage import mcp_request_store
    requested_by = None if user.role == "admin" else user.sub
    rows = await asyncio.to_thread(
        mcp_request_store.list_requests_for_agent, slug, requested_by,
    )
    return {"requests": [_augment_request_row(r) for r in rows]}


@router.post("/v1/agents/{slug}/mcp-requests/{request_id}/cancel")
async def cancel_mcp_request(
    slug: str,
    request_id: int,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Manager cancels their own pending request before approval."""
    if not user:
        raise HTTPException(403, "Authentication required")
    if not user.can_manage_agent(slug):
        raise HTTPException(403, "Manager access required for this agent")

    updated = await community_installer.cancel_request(request_id, user.sub)
    if updated["agent_slug"] != slug:
        # Don't leak request IDs from other agents.
        raise HTTPException(404, f"Request {request_id} not found on agent {slug}")
    return _augment_request_row(updated)


@router.get("/v1/admin/mcp-requests")
async def list_admin_mcp_requests(
    open_only: bool = False,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """All requests across all agents. Admin only."""
    _require_admin(user)
    from storage import mcp_request_store
    if open_only:
        rows = await asyncio.to_thread(mcp_request_store.list_open_requests)
    else:
        rows = await asyncio.to_thread(mcp_request_store.list_all_requests)
    pending = await asyncio.to_thread(mcp_request_store.count_pending)
    return {
        "requests": [_augment_request_row(r) for r in rows],
        "pending_count": pending,
    }


@router.post("/v1/admin/mcp-requests/{request_id}/approve")
async def approve_mcp_request(
    request_id: int,
    body: AdminResolveBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Approve a pending request: install if needed + enable for agent. Admin only.

    Also serves as the retry endpoint when the request is in ``install_failed``
    state — re-runs the install path with the same request id.
    """
    _require_admin(user)
    updated = await community_installer.approve_request(
        request_id, user.sub, admin_note=body.admin_note,
    )
    return _augment_request_row(updated)


@router.post("/v1/admin/mcp-requests/{request_id}/reject")
async def reject_mcp_request(
    request_id: int,
    body: AdminResolveBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Reject a pending request with an optional explanatory note. Admin only."""
    _require_admin(user)
    updated = await community_installer.reject_request(
        request_id, user.sub, admin_note=body.admin_note,
    )
    return _augment_request_row(updated)


# ---------------------------------------------------------------------------
# Community agents catalog
# ---------------------------------------------------------------------------

@router.get("/v1/community/agents")
async def list_community_agents(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List every agent template in the community-agents catalog.

    Each entry is augmented with ``installed_as`` — the list of agent slugs
    on this platform that were installed from this template.
    """
    _require_creator_or_admin(user)
    from services.community import community_agents_catalog

    try:
        registry = await community_agents_catalog.fetch_registry()
    except Exception as exc:
        logger.exception("Failed to load agents registry")
        raise HTTPException(502, f"Could not load community-agents catalog: {exc}")

    installed_as = await community_agents_catalog.collect_local_state()
    augmented = [
        community_agents_catalog.augment_entry(entry, installed_as)
        for entry in registry.get("agents", [])
    ]
    return {
        "registry_version": registry.get("registry_version"),
        "updated_at": registry.get("updated_at"),
        "platform_min_version": registry.get("platform_min_version"),
        "fetched_from": community_agents_catalog.REGISTRY_RAW_URL,
        "agents": augmented,
    }


@router.get("/v1/community/agents/{template_slug}")
async def get_community_agent(
    template_slug: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Return the registry entry + manifest + README for one template."""
    _require_creator_or_admin(user)
    from services.community import community_agents_catalog

    try:
        registry = await community_agents_catalog.fetch_registry()
    except Exception as exc:
        logger.exception("Failed to load agents registry")
        raise HTTPException(502, f"Could not load community-agents catalog: {exc}")

    entry = next(
        (a for a in registry.get("agents", []) if a.get("slug") == template_slug),
        None,
    )
    if not entry:
        raise HTTPException(404, f"Template '{template_slug}' not in catalog")

    try:
        manifest = await community_agents_catalog.fetch_manifest(template_slug)
    except Exception as exc:
        logger.warning("manifest fetch failed for %s: %s", template_slug, exc)
        manifest = None
    try:
        readme = await community_agents_catalog.fetch_readme(template_slug)
    except Exception as exc:
        logger.warning("readme fetch failed for %s: %s", template_slug, exc)
        readme = ""

    installed_as = await community_agents_catalog.collect_local_state()
    return {
        "entry": community_agents_catalog.augment_entry(entry, installed_as),
        "manifest": manifest,
        "readme": readme,
    }


@router.get("/v1/community/agents/{template_slug}/preview")
async def preview_community_agent_install(
    template_slug: str,
    target_slug: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Dry-run the install — show which MCPs are ready vs. need admin work.

    Used by the unified ``AgentInstallModal`` to render the cascade preview
    before the user commits.
    """
    _require_creator_or_admin(user)
    from services.community import community_agents_catalog
    from services.mcp import mcp_registry
    from storage import agent_store, mcp_store

    try:
        registry = await community_agents_catalog.fetch_registry()
    except Exception as exc:
        raise HTTPException(502, f"Could not load community-agents catalog: {exc}")

    entry = next(
        (a for a in registry.get("agents", []) if a.get("slug") == template_slug),
        None,
    )
    if not entry:
        raise HTTPException(404, f"Template '{template_slug}' not in catalog")

    proposed_slug = target_slug or template_slug
    slug_taken = await asyncio.to_thread(agent_store.agent_exists, proposed_slug)
    suggested = None
    if slug_taken:
        from services.community.community_agent_installer import _propose_free_slug
        suggested = await asyncio.to_thread(_propose_free_slug, proposed_slug)

    # Walk the required-MCPs list and classify.
    required = entry.get("required_mcps") or []
    mcp_status = []
    for raw in required:
        name = raw.get("name") if isinstance(raw, dict) else None
        if not name:
            continue
        local_manifest = await asyncio.to_thread(mcp_registry.get_manifest, name)
        catalog_name = None
        try:
            mcps_registry = await community_catalog.fetch_registry()
            catalog_name = next(
                (m["name"] for m in mcps_registry.get("mcps", []) if m.get("name") == name),
                None,
            )
        except Exception as exc:
            logger.debug(f"Community registry lookup for MCP {name} failed: {exc}")
        if local_manifest is None:
            mcp_status.append({
                "name": name,
                "installed": False,
                "request_type": "install" if catalog_name else None,
                "blocked": catalog_name is None,
                "needs_request": True,
                "reason": "Not installed" + ("; in community catalog" if catalog_name else "; NOT in any catalog"),
            })
            continue
        # Installed
        assignment = getattr(local_manifest, "assignment_mode", "auto")
        if assignment != "explicit":
            mcp_status.append({
                "name": name, "installed": True, "request_type": None,
                "blocked": False, "needs_request": False,
                "reason": "Auto-mode — ready",
            })
            continue
        instances = await asyncio.to_thread(mcp_store.get_mcp_instances, name)
        if any(i.get("assigned_to_all") for i in instances) or any(
            proposed_slug in (i.get("agents") or []) for i in instances
        ):
            mcp_status.append({
                "name": name, "installed": True, "request_type": None,
                "blocked": False, "needs_request": False,
                "reason": "Explicit-mode — instance authorizes this agent",
            })
            continue
        if instances:
            mcp_status.append({
                "name": name, "installed": True, "request_type": "access",
                "blocked": False, "needs_request": user.role != "admin",
                "reason": "Explicit-mode — agent will be attached to an instance",
            })
        else:
            mcp_status.append({
                "name": name, "installed": True, "request_type": "access",
                "blocked": True, "needs_request": True,
                "reason": "Explicit-mode — no instances configured (admin must create one first)",
            })

    return {
        "template_slug": template_slug,
        "target_slug": proposed_slug,
        "slug_available": not slug_taken,
        "suggested_slug": suggested,
        "required_mcps": mcp_status,
        "will_create_tasks_agent_scope": sum(
            1 for t in (entry.get("tasks_agent_scope") or [])
        ),
        "platform_compat_ok": True,  # platform_min_version check deferred
    }


@router.post("/v1/admin/agents/{slug}/reseed-template-items")
async def admin_reseed_template_items(
    slug: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Re-fire :func:`on_user_added_to_agent` for every user currently attached.

    Recovery path. Useful when per-user seeding failed mid-loop
    during a default-attach pass (a partial transaction left some users
    with their items, others without) or when an admin manually pruned
    items that should be re-seeded.

    Idempotent: each per-user seed call hits ``idx_dyn_tasks_tpl_user`` /
    ``idx_triggers_tpl_user`` / ``idx_notifs_tpl_user`` so existing items
    don't get duplicated. Returns the sum of newly-seeded items across
    all users.

    Admin only.
    """
    _require_admin(user)
    from storage import agent_store, database as user_store
    from services.community import community_agent_installer

    if not await asyncio.to_thread(agent_store.agent_exists, slug):
        raise HTTPException(404, f"Agent '{slug}' not found")
    agent = await asyncio.to_thread(agent_store.get_agent, slug)
    if not agent or not agent.get("community_template"):
        raise HTTPException(
            400,
            f"Agent '{slug}' is not a community-template install — "
            "nothing to re-seed.",
        )
    if not await asyncio.to_thread(agent_store.get_community_template_data, slug):
        raise HTTPException(
            400,
            f"Agent '{slug}' has no persisted template data. Reinstall the "
            "template to populate community_template_data, then retry.",
        )

    pairs = await asyncio.to_thread(user_store.get_agent_users, slug)
    totals = {"tasks": 0, "triggers": 0, "notifications": 0, "users": len(pairs)}
    per_user: list[dict] = []
    for pair in pairs:
        sub = pair["sub"]
        role = pair["agent_role"]
        try:
            counts = await asyncio.to_thread(
                community_agent_installer.on_user_added_to_agent,
                slug, sub, role,
            )
        except Exception as exc:
            logger.exception("reseed failed for (%s, %s)", slug, sub)
            per_user.append({
                "sub": sub, "role": role,
                "error": f"{type(exc).__name__} (see proxy logs)",
            })
            continue
        totals["tasks"] += counts["tasks"]
        totals["triggers"] += counts["triggers"]
        totals["notifications"] += counts["notifications"]
        per_user.append({"sub": sub, "role": role, **counts})

    logger.info("Admin reseed for %s: %s", slug, totals)
    return {"agent_slug": slug, "totals": totals, "per_user": per_user}


class InstallFromCommunityBody(BaseModel):
    template_slug: str
    target_slug: str | None = None  # default to template_slug
    manager_user: str | None = None  # admin-only: install on behalf of someone


@router.post("/v1/agents/install-from-community")
async def install_from_community(
    body: InstallFromCommunityBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Install a community-agents template into a new agent.

    Manager-callable. Cascades required MCPs — pending requests for
    missing/explicit MCPs are tagged with a shared ``batch_id`` so the admin
    sees one combined notification instead of N.
    """
    u = _require_creator_or_admin(user)
    from services.community import community_agent_installer

    installer_sub = u.sub
    if body.manager_user:
        if u.role != "admin":
            raise HTTPException(403, "Only admins can install on behalf of others")
        installer_sub = body.manager_user

    target = body.target_slug or body.template_slug
    try:
        result = await community_agent_installer.install_from_catalog(
            template_slug=body.template_slug,
            target_slug=target,
            installer_user_sub=installer_sub,
            installer_role=u.role,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Community agent install failed for %s", body.template_slug)
        raise HTTPException(500, f"Install failed: {exc}")


