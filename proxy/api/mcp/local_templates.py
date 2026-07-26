"""Local agent-template API — agents creating agents.

An agent authors a community-agent template folder somewhere it can write
and hands the path to these endpoints, which run it through the exact
pipeline that installs catalog templates. Driven by ``agent-creator-mcp``;
the dashboard has no UI for this yet.

Authorization has two independent layers, and the second is the real one:

1. The MCP shows its tools only when ``OTO_PLATFORM_ROLE`` is
   ``admin``/``creator``. Cosmetic — env is baked at session spawn.
2. Every endpoint here requires a session-backed platform creator/admin,
   resolved live from the users table by the auth layer, and resolves the
   caller-supplied path through the session's own path policy.

The template folder is COPIED into a private snapshot before it is parsed:
the authoring agent owns that directory and could otherwise swap a
subdirectory for a symlink between the check and the read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user

logger = logging.getLogger("claude-proxy.local-templates")
router = APIRouter()

# The parser reads every context file into memory with no cap of its own,
# so these are load-bearing, not belt-and-braces.
_MAX_FILES = 300
_MAX_FILE_BYTES = 1 * 1024 * 1024
_MAX_TOTAL_BYTES = 20 * 1024 * 1024


class LocalTemplateBody(BaseModel):
    path: str


class InstallLocalTemplateBody(BaseModel):
    path: str
    target_slug: str | None = None


def _require_creator_or_admin(user: UserContext | None) -> UserContext:
    if not user:
        raise HTTPException(403, "Authentication required")
    if user.role not in ("admin", "creator"):
        raise HTTPException(
            403, "Creating agents requires a platform creator or admin account",
        )
    return user


def _resolve_template_dir(user: UserContext, raw_path: str) -> Path:
    """Resolve a caller-supplied template path against the session's policy.

    Requires a real session principal: the path policy is built from the
    session's security context, and the RBAC re-imposition keys on the
    session's own username. A master-key or user-less principal has neither.
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(400, "path is required")
    if not user.session_id or not user.acting_sub:
        raise HTTPException(
            403,
            "This endpoint is callable only from an agent session with a "
            "signed-in user",
        )

    from core.session import session_state
    from services import path_policy_v2
    from auth.path_policy import enforce_agent_tree_rbac

    ctx = session_state.get_session_security(user.session_id)
    if ctx is None:
        raise HTTPException(
            409, "Session security context unavailable — retry in a moment",
        )

    policy_ctx = path_policy_v2.context_from_security(ctx)
    resolution = path_policy_v2.resolve_path_for_session(policy_ctx, raw_path.strip())
    if not resolution.allowed:
        raise HTTPException(403, resolution.error or "access denied")
    ref = getattr(resolution, "path_ref", None)
    if ref is None or ref.kind != "agent_tree":
        # A satellite-host path lives on the remote machine's disk; the proxy
        # cannot read it. Authoring must happen inside the synced agent tree.
        raise HTTPException(
            400,
            "The template folder must live inside the agent's workspace "
            "(e.g. /workspace/... or /users/<you>/workspace/...), not "
            "elsewhere on the machine's filesystem",
        )
    decision = enforce_agent_tree_rbac(resolution, ctx, writing=False)
    if not decision.allowed:
        raise HTTPException(403, decision.reason or "access denied")

    # ``access_path`` is the sandbox-virtual form on local sessions; the
    # agent-tree-relative slug on ``path_ref`` is what maps to proxy-side
    # disk, and it is the same value the RBAC check above authorized.
    import config
    agent_root = config.get_agent_dir(ctx.agent).resolve()
    resolved = (agent_root / ref.value).resolve()
    if not resolved.is_relative_to(agent_root):
        raise HTTPException(403, "access denied")
    if not resolved.is_dir():
        raise HTTPException(400, f"Not a directory: {raw_path}")
    return resolved


def _snapshot_template_dir(src: Path) -> Path:
    """Copy ``src`` into a private temp dir, rejecting anything unexpected.

    Only regular files and real directories are copied — no symlinks, no
    devices — so the parser (which globs and follows symlinked dirs) can
    never reach outside what was validated here. The caller deletes the
    snapshot.
    """
    dest = Path(tempfile.mkdtemp(prefix="oto-local-template-"))
    files = 0
    total = 0
    try:
        for root, dirnames, filenames in os.walk(src, followlinks=False):
            root_path = Path(root)
            rel_root = root_path.relative_to(src)
            # Skip VCS noise; reject symlinked dirs outright.
            for name in list(dirnames):
                if name == ".git":
                    dirnames.remove(name)
                elif (root_path / name).is_symlink():
                    raise HTTPException(
                        400,
                        f"Symlinks are not allowed in a template folder: "
                        f"{rel_root / name}",
                    )
            (dest / rel_root).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                src_file = root_path / name
                rel = rel_root / name
                st = src_file.lstat()
                if src_file.is_symlink():
                    raise HTTPException(
                        400,
                        f"Symlinks are not allowed in a template folder: {rel}",
                    )
                if not os.path.isfile(src_file):
                    raise HTTPException(
                        400, f"Only regular files are allowed: {rel}",
                    )
                files += 1
                if files > _MAX_FILES:
                    raise HTTPException(
                        400, f"Template folder has too many files (max {_MAX_FILES})",
                    )
                if st.st_size > _MAX_FILE_BYTES:
                    raise HTTPException(
                        400,
                        f"{rel} is too large "
                        f"(max {_MAX_FILE_BYTES // 1024} KB per file)",
                    )
                total += st.st_size
                if total > _MAX_TOTAL_BYTES:
                    raise HTTPException(
                        400,
                        f"Template folder is too large "
                        f"(max {_MAX_TOTAL_BYTES // (1024 * 1024)} MB total)",
                    )
                shutil.copyfile(src_file, dest / rel, follow_symlinks=False)
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


async def _load_snapshot(user: UserContext, raw_path: str):
    """Resolve → snapshot → parse. Returns ``(template, snapshot_dir)``.

    The caller owns the snapshot dir and must delete it.
    """
    from storage.community_agent_template_store import (
        TemplateValidationError,
        load_template_from_dir,
    )

    src = _resolve_template_dir(user, raw_path)
    snapshot = await asyncio.to_thread(_snapshot_template_dir, src)
    try:
        template = await asyncio.to_thread(load_template_from_dir, snapshot)
    except TemplateValidationError as exc:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise HTTPException(400, detail={"error": "invalid_template", "message": str(exc)})
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return template, snapshot


# ---------------------------------------------------------------------------
# GET /v1/agents/local-template/building-blocks
# ---------------------------------------------------------------------------

@router.get("/v1/agents/local-template/building-blocks")
async def local_template_building_blocks(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """MCPs, skill packages and taken slugs an authoring agent can draw on.

    Projects explicit fields only — a manifest carries config field
    definitions and env templates that have no business in an agent's
    context.
    """
    _require_creator_or_admin(user)
    from services.mcp import mcp_registry
    from services.mcp.mcp_registry import _first_sentence
    from services.community import community_catalog
    from storage import agent_store

    mcps: list[dict] = []
    skill_packages: list[dict] = []
    installed_names: set[str] = set()
    for name, manifest in mcp_registry.get_all_manifests().items():
        installed_names.add(name)
        entry = {
            "name": name,
            "label": manifest.label or name,
            "description": _first_sentence(manifest.description or ""),
            "category": manifest.category,
            "installed": True,
            "skills": [s.id for s in manifest.skills],
        }
        if manifest.category == "skill":
            skill_packages.append(entry)
        else:
            mcps.append(entry)

    catalog_error = ""
    try:
        registry = await community_catalog.fetch_registry()
        for entry in registry.get("mcps", []):
            name = entry.get("name") or ""
            if not name or name in installed_names:
                continue
            mcps.append({
                "name": name,
                "label": entry.get("label") or name,
                "description": _first_sentence(entry.get("description") or ""),
                "category": "community",
                "installed": False,
                "skills": [],
            })
    except Exception as exc:
        catalog_error = str(exc)
        logger.warning("building-blocks: MCP catalog unreachable: %s", exc)

    try:
        skills_registry = await community_catalog.fetch_skills_registry()
        for entry in skills_registry.get("skills", []):
            name = entry.get("name") or ""
            if not name or name in installed_names:
                continue
            skill_packages.append({
                "name": name,
                "label": entry.get("label") or name,
                "description": _first_sentence(entry.get("description") or ""),
                "category": "skill",
                "installed": False,
                "skills": [],
            })
    except Exception as exc:
        if not catalog_error:
            catalog_error = str(exc)
        logger.warning("building-blocks: skills catalog unreachable: %s", exc)

    slugs = [a["slug"] for a in await asyncio.to_thread(agent_store.get_all_agents)]
    return {
        "mcps": sorted(mcps, key=lambda m: (not m["installed"], m["name"])),
        "skill_packages": sorted(
            skill_packages, key=lambda s: (not s["installed"], s["name"]),
        ),
        "agent_slugs": sorted(slugs),
        "catalog_error": catalog_error,
    }


# ---------------------------------------------------------------------------
# POST /v1/agents/local-template/validate
# ---------------------------------------------------------------------------

@router.post("/v1/agents/local-template/validate")
async def validate_local_template(
    body: LocalTemplateBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Dry-run a template folder. No side effects — validate as often as needed.

    Validation problems come back as ``ok: false`` + ``errors`` (the agent
    self-corrects from them); 4xx is reserved for authorization and path
    problems, which the agent cannot fix by editing files.
    """
    u = _require_creator_or_admin(user)
    from services.community import community_agent_installer as installer
    from storage import agent_store

    try:
        template, snapshot = await _load_snapshot(u, body.path)
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("error") == "invalid_template":
            return {"ok": False, "errors": [detail["message"]]}
        raise

    try:
        errors: list[str] = []
        mcp_plan = {"ready": [], "needs_install": [], "needs_admin_request": []}
        skill_plan = {"ready": [], "missing": [], "admin_required": []}

        try:
            await installer._preflight_check_mcps(template.mcps)
        except HTTPException as exc:
            detail = exc.detail
            missing = detail.get("missing", []) if isinstance(detail, dict) else []
            errors.append(
                "These MCPs are not installed and not in any catalog: "
                + ", ".join(str(m) for m in missing),
            )
        else:
            from services.mcp import mcp_registry
            installed = set(mcp_registry.get_all_manifests().keys())
            for req in template.mcps:
                if req.name in installed:
                    mcp_plan["ready"].append(req.name)
                elif u.role == "admin":
                    mcp_plan["needs_install"].append(req.name)
                else:
                    mcp_plan["needs_admin_request"].append(req.name)

        try:
            await installer._preflight_check_skill_packages(
                template.skill_packages, u.role,
            )
        except HTTPException as exc:
            detail = exc.detail
            kind = detail.get("error") if isinstance(detail, dict) else ""
            names = detail.get("missing", []) if isinstance(detail, dict) else []
            if kind == "skills_require_admin":
                skill_plan["admin_required"] = [str(n) for n in names]
                errors.append(
                    "These skill packages must be installed by a platform "
                    "admin first: " + ", ".join(str(n) for n in names),
                )
            else:
                skill_plan["missing"] = [str(n) for n in names]
                errors.append(
                    "These skill packages are not in any catalog: "
                    + ", ".join(str(n) for n in names),
                )
        else:
            skill_plan["ready"] = [p.name for p in template.skill_packages]

        slug = agent_store.sanitize_slug(template.slug)
        slug_available = bool(slug) and not await asyncio.to_thread(
            agent_store.agent_exists, slug,
        )
        suggested = ""
        if slug and not slug_available:
            suggested = await asyncio.to_thread(installer._propose_free_slug, slug)

        return {
            "ok": not errors,
            "errors": errors,
            "slug": slug,
            "slug_available": slug_available,
            "suggested_slug": suggested,
            "mcp_plan": mcp_plan,
            "skill_plan": skill_plan,
            "seeds": {
                "tasks": len(template.tasks),
                "triggers": len(template.triggers),
                "notifications": len(template.notifications),
                "context_files": len(template.context_files),
                "has_setup": template.setup_md is not None,
                "has_user_setup": template.user_setup_md is not None,
            },
        }
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


# ---------------------------------------------------------------------------
# POST /v1/agents/install-from-local-template
# ---------------------------------------------------------------------------

@router.post("/v1/agents/install-from-local-template")
async def install_from_local_template(
    body: InstallLocalTemplateBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Create a new agent from a locally authored template folder.

    The caller becomes the new agent's manager. Platform admins get MCPs
    installed inline; creators get the admin-approval request queue — the
    same split the catalog install path uses.
    """
    u = _require_creator_or_admin(user)
    from services.community import community_agent_installer

    template, snapshot = await _load_snapshot(u, body.path)
    try:
        result = await community_agent_installer.install_from_extracted_template(
            template=template,
            target_slug=body.target_slug or template.slug,
            installer_user_sub=u.acting_sub,
            installer_role=u.role,
            source_label=f"local:{template.slug}",
            # Provenance: keeps a locally authored agent from matching a
            # catalog entry that happens to share its slug in Browse.
            template_ref=f"local:{template.slug}",
            # Attaching every future platform user is an admin decision.
            allow_default_for_new_users=(u.role == "admin"),
        )
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)

    result["dashboard_path"] = f"/agents/{result['agent_slug']}"
    result.pop("agent", None)
    return result
