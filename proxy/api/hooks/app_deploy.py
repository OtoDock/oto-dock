"""The folder-app hooks an agent's display tools call (APPS.md "Deploy
pipeline"): deploy, check, status, preview, logs, restart. Session-JWT
gated like every hook; the pin authority (``_resolve_pin_visibility`` +
``_require_shared_pin_authority``) decides who may deploy; the others
resolve the row the way unpin does.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from api.hooks.pins import (
    _find_app_row, _require_shared_pin_authority, _resolve_pin_visibility,
)
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import get_session_security
from core.session.visibility import SCOPE_AGENT
from services.apps import app_deploy, app_sandbox, app_supervisor, releases
from services.infra import path_confinement, safe_fs
from storage import database as task_store
from storage import db_apps
from core import layout

logger = logging.getLogger("claude-proxy")
router = APIRouter()


class HookAppScreenshotRequest(BaseModel):
    session_id: str
    slug: str
    visibility: str = ""
    source: str = "working"   # working | live


class HookAppDeployRequest(BaseModel):
    session_id: str
    slug: str
    visibility: str = ""
    tail: int = 200
    # purge: the slug typed again (the same confirmation the dialog asks).
    confirm: str = ""


async def _ctx(authorization: str | None, session_id: str):
    """The session's security context once its token matches the session
    and its holder still stands (the person exists, the token is current)."""
    await verify_session_match_async(authorization, session_id)
    ctx = get_session_security(session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    return ctx


def _slug(raw: str) -> str:
    from api.apps import manifest as _mf
    slug = (raw or "").strip().lower()
    if not _mf.APP_SLUG_RE.match(slug):
        raise HTTPException(status_code=400,
                            detail="slug must be 1-40 chars of [a-z0-9-], starting alphanumeric")
    return slug


def _under_agent(ctx, rel: str) -> Path:
    """A path of the caller's agent tree from a request-derived relative
    path, joined in the shape the scanner recognizes as confined."""
    try:
        return path_confinement.join_under(config.get_agent_dir(ctx.agent), rel)
    except path_confinement.PathOutsideRoot:
        raise HTTPException(status_code=400, detail="the path leaves the agent's tree")


def _scoped(ctx, username: str, rel: str) -> Path:
    """``_under_agent`` for a path of an app folder, refused when a link on
    the way leads out of the scope's workspace (``confine_to_scope``)."""
    path = _under_agent(ctx, rel)
    try:
        app_deploy.confine_to_scope(ctx.agent, username, path)
    except app_deploy.DeployError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return path


async def _folder_source(ctx, slug: str, visibility: str) -> tuple[str, str | None, str]:
    """(username, owner_sub, rel_dir) of the caller's ``apps/<slug>`` folder
    under the pin authority."""
    username, owner_sub = await asyncio.to_thread(_resolve_pin_visibility, ctx, visibility)
    _require_shared_pin_authority(ctx, username)
    root = layout.scope_workspace(username)
    return username, owner_sub, f"{root}/apps/{slug}"


async def deploy_from_session(session_id: str, ctx, slug: str, visibility: str) -> dict:
    """Shared by the deploy hook and ``pin_app`` on a folder."""
    username, owner_sub, rel_dir = await _folder_source(ctx, slug, visibility)
    source = _under_agent(ctx, rel_dir)
    try:
        out = await app_deploy.deploy_folder(ctx.agent, username, owner_sub, slug, source, rel_dir,
                                             session_id=session_id)
    except app_deploy.DeployError as e:
        raise HTTPException(status_code=400, detail=str(e))
    out["scope"] = db_apps.app_scope(username)
    out["path"] = "/" + rel_dir
    logger.info("Hook app deploy: session=%s agent=%s slug=%s status=%s release=%s problems=%s",
                session_id[:8], ctx.agent, slug, out.get("status"), out.get("release"),
                out.get("problems", 0))
    return out


@router.post("/v1/hooks/apps/deploy")
async def hook_app_deploy(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    ctx = await _ctx(authorization, req.session_id)
    return await deploy_from_session(req.session_id, ctx, _slug(req.slug), req.visibility)


async def _smoke_copy(row: dict, source: Path) -> dict:
    """``check_app``'s smoke start on a copy of the working tree (the copy
    the render takes), never on the folder itself, which a session can
    still change after its check; the copy goes when the smoke ends."""
    from services.apps import app_render
    try:
        tree = await asyncio.to_thread(app_render.copy_working_tree, row, source)
    except (releases.ReleaseInvalid, app_deploy.DeployError) as e:
        return {"ok": False, "server": "failed", "reason": str(e)}
    try:
        return await app_supervisor.smoke(row, tree)
    finally:
        await asyncio.to_thread(shutil.rmtree, tree, True)


@router.post("/v1/hooks/apps/check")
async def hook_app_check(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    """Validate app.json and smoke-start the working tree's server on a
    scratch port with scratch data; nothing is copied, nothing goes live."""
    ctx = await _ctx(authorization, req.session_id)
    slug = _slug(req.slug)
    username, owner_sub, rel_dir = await _folder_source(ctx, slug, req.visibility)
    source = _scoped(ctx, username, rel_dir)
    from core.remote import remote_file_flow
    if remote_file_flow.is_remote_session(req.session_id):
        try:
            await app_deploy.pull_folder(req.session_id, rel_dir)
        except app_deploy.DeployError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # The tree first (no link anywhere in it), then the manifest and the
    # page, each read without following a link.
    try:
        files = await asyncio.to_thread(releases.walk_tree, source)
        doc = await asyncio.to_thread(app_deploy.read_app_json, source)
        m = app_deploy.validate_app_json(doc, ctx.agent, shared=not username)
        await asyncio.to_thread(app_deploy.require_entry_page, source)
    except (app_deploy.DeployError, releases.ReleaseInvalid) as e:
        return {"status": "invalid", "reason": e.reason, "slug": slug}
    from services.apps import app_lint
    findings = await asyncio.to_thread(app_lint.lint_tree, source, files,
                                       app_lint.declared_from_manifest(m))
    lint = app_lint.summary(findings)
    manifest = {"actions": len(__import__("json").loads(m.actions_json)),
                "files": bool(m.files_json), "egress": bool(m.egress_json),
                "handlers": bool(m.handlers_json), "exports": bool(m.exports_json),
                "bindings": bool(m.bindings_json), "requires": bool(m.requires_json),
                "steps": bool(m.steps_json), "secrets": bool(m.secrets_json),
                "inbound": bool(m.inbound_json), "external": bool(m.external_json)}
    if lint["problems"]:
        # A page or server the sandbox would refuse: named first, the smoke
        # would only add noise under it.
        return {"status": app_deploy.RESULT_REFUSED, "slug": slug, "files": len(files),
                "title": m.title, "manifest": manifest, **lint}
    row = await asyncio.to_thread(task_store.get_app_by_slug, ctx.agent, username, slug)
    if row is not None and db_apps.app_kind_of(row).serves_tree:
        # A registered app: the working tree is rendered the way a viewer
        # would see it (APPS.md "Deploy pipeline"); the render's start of
        # the server is the smoke.
        from services.apps import app_render
        try:
            report = await app_render.render_working_tree(
                row, source, approved=task_store.app_actions_approved(row))
        except releases.ReleaseInvalid as e:
            # A file swapped for a link between the walk above and the copy:
            # the rule's refusal, never an error page.
            raise HTTPException(status_code=400, detail=str(e))
        server = "up" if (report.status in ("ok", "soft") and app_sandbox.server_entry(source)) else (
            "none" if not app_sandbox.server_entry(source) else report.status)
        if report.status == "unavailable":
            smoke = await _smoke_copy(row, source)
            server = smoke.get("server", "none")
            status = "ok" if smoke.get("ok") else "failed"
            extra = {k: v for k, v in smoke.items() if k != "ok"}
        else:
            status = "failed" if report.status == "hard" else "ok"
            extra = {}
        return {"status": status, "slug": slug, "files": len(files), "title": m.title,
                "manifest": manifest, "server": server, "render": report.as_dict(),
                **lint, **extra}
    # Not deployed yet: no page to render (the first deploy renders before
    # it goes live); the server is smoke-started on scratch data.
    stub = {"id": f"check-{slug}", "agent": ctx.agent, "slug": slug, "username": username,
            "owner_sub": owner_sub, "actions": m.actions_json, "title": m.title}
    try:
        smoke = await _smoke_copy(stub, source)
    finally:
        await asyncio.to_thread(releases.remove_release_dir, stub)
    return {"status": "ok" if smoke.get("ok") else "failed", "slug": slug,
            "files": len(files), "title": m.title, "manifest": manifest, **lint,
            "render": {"status": "unavailable",
                       "reason": "the app is not deployed yet; the first deploy renders it "
                                 "before it goes live"},
            **{k: v for k, v in smoke.items() if k != "ok"}}


@router.post("/v1/hooks/apps/screenshot")
async def hook_app_screenshot(req: HookAppScreenshotRequest, authorization: str | None = Header(None)):
    """``screenshot_app``: the pictures and the verdict of the working tree
    (or the live release) without a deploy (APPS.md "Deploy pipeline")."""
    ctx = await _ctx(authorization, req.session_id)
    slug = _slug(req.slug)
    _vis, row = await _find_app_row(ctx, slug, req.visibility)
    if not db_apps.app_kind_of(row).serves_tree:
        raise HTTPException(status_code=400, detail="screenshot_app is for folder apps")
    from services.apps import app_render
    approved = task_store.app_actions_approved(row)
    if (req.source or "working") == "live":
        report = await app_render.render_live(row, approved=approved)
    else:
        username, _owner, rel_dir = await _folder_source(ctx, slug, req.visibility)
        from core.remote import remote_file_flow
        if remote_file_flow.is_remote_session(req.session_id):
            try:
                await app_deploy.pull_folder(req.session_id, rel_dir)
            except app_deploy.DeployError as e:
                raise HTTPException(status_code=400, detail=str(e))
        source = _scoped(ctx, username, rel_dir)
        if not source.is_dir():
            raise HTTPException(status_code=400, detail=f"no folder at apps/{slug} in your scope")
        try:
            report = await app_render.render_working_tree(row, source, approved=approved)
        except releases.ReleaseInvalid as e:
            # The working tree holds a link: the rule's refusal, as the
            # check and the deploy answer it.
            raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "app_id": row["id"], "slug": row["slug"],
            "source": req.source or "working", "render": report.as_dict()}


@router.post("/v1/hooks/apps/status")
async def hook_app_status(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    ctx = await _ctx(authorization, req.session_id)
    _vis, row = await _find_app_row(ctx, req.slug, req.visibility)
    return {"status": "ok", "app_id": row["id"], "slug": row["slug"],
            "kind": row.get("kind") or "file", **await asyncio.to_thread(app_deploy.status, row)}


@router.post("/v1/hooks/apps/preview")
async def hook_app_preview(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    """Copy the working tree to the preview slot (its own data, seeded from
    the live database), start it, and answer with the URL the owner or an
    editor opens."""
    ctx = await _ctx(authorization, req.session_id)
    slug = _slug(req.slug)
    username, _owner, rel_dir = await _folder_source(ctx, slug, req.visibility)
    _vis, row = await _find_app_row(ctx, slug, req.visibility)
    if not db_apps.app_kind_of(row).serves_tree:
        raise HTTPException(status_code=400, detail="preview is for folder apps")
    from core.remote import remote_file_flow
    if remote_file_flow.is_remote_session(req.session_id):
        try:
            await app_deploy.pull_folder(req.session_id, rel_dir)
        except app_deploy.DeployError as e:
            raise HTTPException(status_code=400, detail=str(e))
    source = _scoped(ctx, username, rel_dir)
    try:
        sha = await app_deploy.cut_preview(row, source)
    except app_deploy.DeployError as e:
        raise HTTPException(status_code=400, detail=str(e))
    server = "none"
    if (releases.preview_dir(row) / "server").is_dir():
        try:
            inst = await app_supervisor.start(row, "preview")
            server = inst.state
        except app_supervisor.AppUnavailable as e:
            server = f"failed: {e.reason}"
        except Exception:  # noqa: BLE001 — the log has the detail, the agent gets the state
            logger.exception("App preview %s: the server did not start", row.get("slug"))
            server = "failed"
    return {"status": "ok", "app_id": row["id"], "url": f"/apps/{row['id']}?preview=1",
            "sha": sha, "server": server}


@router.post("/v1/hooks/apps/logs")
async def hook_app_logs(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    ctx = await _ctx(authorization, req.session_id)
    found_vis, row = await _find_app_row(ctx, req.slug, req.visibility)
    # What a server printed (its users' data included) is for the people
    # who manage the app, as the dashboard's log route is.
    if found_vis == SCOPE_AGENT:
        _require_shared_pin_authority(ctx, "")
    tail = max(1, min(int(req.tail or 200), 2000))
    text = await asyncio.to_thread(app_supervisor.read_log_tail, row, tail)
    from api.apps import app_proxy
    return {"status": "ok", "app_id": row["id"], "log": text,
            "sockets_refused": app_proxy.refused_sockets(row["id"]),
            **app_supervisor.status(row["id"])}


@router.post("/v1/hooks/apps/purge")
async def hook_app_purge(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    """Delete the app with its data (APPS.md "Lifecycle"): the pin
    authority plus the slug typed again."""
    ctx = await _ctx(authorization, req.session_id)
    found_vis, row = await _find_app_row(ctx, req.slug, req.visibility)
    if found_vis == SCOPE_AGENT:
        _require_shared_pin_authority(ctx, "")
    if (req.confirm or "").strip().lower() != row["slug"]:
        raise HTTPException(status_code=400, detail="pass confirm=<slug> to delete the app and its data")
    from services.apps import app_lifecycle
    out = await app_lifecycle.purge(row)
    logger.info("Hook app purge: session=%s slug=%s", req.session_id[:8], row["slug"])
    return out


@router.post("/v1/hooks/apps/restart")
async def hook_app_restart(req: HookAppDeployRequest, authorization: str | None = Header(None)):
    ctx = await _ctx(authorization, req.session_id)
    found_vis, row = await _find_app_row(ctx, req.slug, req.visibility)
    if found_vis == SCOPE_AGENT:
        _require_shared_pin_authority(ctx, "")
    await app_supervisor.stop(row["id"], "live")
    try:
        inst = await app_supervisor.ensure_up(row)
        state = inst.state
        error = ""
    except app_supervisor.AppUnavailable as e:
        state, error = e.state, e.reason
    return {"status": "ok", "app_id": row["id"], "server": state, "error": error}


class HookDescribeRequest(BaseModel):
    session_id: str
    agent: str
    slug: str


@router.post("/v1/hooks/apps/describe")
async def hook_app_describe(req: HookDescribeRequest, authorization: str | None = Header(None)):
    """``describe_app(agent, slug)`` (APPS.md "Bindings"): the signed exports
    of an APPROVED shared folder app of an agent the session may reach —
    its own, a delegation target, or one the session's user is a member of;
    404 otherwise (never an oracle)."""
    from api.apps import app_bindings
    ctx = await _ctx(authorization, req.session_id)
    slug = _slug(req.slug)
    agent = (req.agent or "").strip().lower() or ctx.agent
    reach = await asyncio.to_thread(app_bindings.reachable_agents, ctx.agent, ctx.username or "")
    row = await asyncio.to_thread(task_store.get_app_by_slug, agent, "", slug) if agent in reach else None
    if not row or row.get("hidden") or not db_apps.app_kind_of(row).serves_tree \
            or not task_store.app_actions_approved(row):
        raise HTTPException(status_code=404, detail=f"no app '{slug}' of agent '{agent}' to bind to")
    return {"status": "ok", **app_bindings.describe(row)}


class HookExportRequest(BaseModel):
    session_id: str
    slug: str
    visibility: str = ""


class HookImportRequest(BaseModel):
    session_id: str
    path: str
    visibility: str = ""
    slug: str = ""


@router.post("/v1/hooks/apps/export")
async def hook_app_export(req: HookExportRequest, authorization: str | None = Header(None)):
    """``export_app(slug)`` (APPS.md "Blueprints and templates"): the app's
    working folder as ``apps/<slug>.otoapp`` in the same scope, its buttons
    by slug and the tasks they point at inside; under the pin authority."""
    from services.apps import app_blueprints
    ctx = await _ctx(authorization, req.session_id)
    slug = _slug(req.slug)
    username, _owner, rel_dir = await _folder_source(ctx, slug, req.visibility)
    row = await asyncio.to_thread(task_store.get_app_by_slug, ctx.agent, username, slug)
    if not row or not db_apps.app_kind_of(row).serves_tree:
        raise HTTPException(status_code=404, detail=f"no folder app '{slug}' in this scope")
    _scoped(ctx, username, row.get("rel_path") or rel_dir)
    try:
        data, blueprint = await asyncio.to_thread(app_blueprints.export_bundle, row)
    except (app_blueprints.BlueprintError, app_deploy.DeployError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    apps_rel = rel_dir.rsplit("/", 1)[0]
    rel = os.path.normpath(f"{apps_rel}/{slug}.otoapp")
    if not rel.startswith(apps_rel + "/"):
        raise HTTPException(status_code=400, detail="the path leaves the agent's tree")
    target = _scoped(ctx, username, rel)
    # Written beneath the agents tree in one rename: a link planted at the
    # bundle's name, next to it or above it is never written through.
    try:
        await asyncio.to_thread(safe_fs.atomic_write_beneath, config.AGENTS_DIR,
                                f"{ctx.agent}/{rel}", data, mkdirs=True)
    except OSError:
        raise HTTPException(status_code=400, detail=f"{slug}.otoapp could not be written there: "
                                                    "a link, a folder or a file is in the way")
    from services.infra import file_bookkeeping
    try:
        await file_bookkeeping.push_file_write(ctx.agent, rel, target, writer="app-export")
    except Exception:
        logger.exception("App export %s: workspace push failed (continuing)", slug)
    return {"status": "ok", "slug": slug, "path": "/" + rel, "bytes": len(data),
            "tasks": [t["slug"] for t in blueprint.get("tasks") or []]}


@router.post("/v1/hooks/apps/import")
async def hook_app_import(req: HookImportRequest, authorization: str | None = Header(None)):
    """``import_app(path)``: a ``.otoapp`` in the caller's workspace becomes
    ``apps/<slug>/`` of the chosen scope, its tasks seeded by slug, release 1
    pending on the card — never pre-approved."""
    from services.apps import app_blueprints
    ctx = await _ctx(authorization, req.session_id)
    try:
        rel = path_confinement.normalize_rel_path((req.path or "").strip())
    except path_confinement.PathOutsideRoot:
        rel = ""
    if not rel.endswith(".otoapp"):
        raise HTTPException(status_code=400, detail="path must name a .otoapp file in the workspace")
    username, owner_sub, _rel_dir = await _folder_source(ctx, "x", req.visibility)
    scope_root = layout.scope_workspace(username)
    # The bundle may sit in the shared workspace or in the caller's own
    # tree; another user's tree is never readable here.
    if rel.startswith(f"{layout.WORKSPACE}/") or (ctx.username and rel.startswith(f"{layout.scope_workspace(ctx.username)}/")):
        full_rel = rel
    elif layout.is_personal(rel):
        raise HTTPException(status_code=403, detail="another user's files are not readable here")
    else:
        full_rel = f"{scope_root}/{rel}"
    from core.remote import remote_file_flow
    if remote_file_flow.is_remote_session(req.session_id):
        await remote_file_flow.pull_through(req.session_id, full_rel)
    _scoped(ctx, "" if full_rel.startswith(f"{layout.WORKSPACE}/") else ctx.username, full_rel)
    # The bundle is read as a regular file beneath the agent's tree, never
    # through a link.
    try:
        data = await asyncio.to_thread(safe_fs.read_bytes_beneath, config.AGENTS_DIR,
                                       f"{ctx.agent}/{full_rel}", max_size=app_blueprints.MAX_BUNDLE_BYTES)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no file at {full_rel}")
    except safe_fs.FileTooLarge:
        raise HTTPException(status_code=400, detail="the bundle is larger than 40 MB")
    except OSError:
        raise HTTPException(status_code=400, detail=f"{full_rel} is not a regular file in the workspace")
    try:
        folder, blueprint = await asyncio.to_thread(app_blueprints.read_bundle, data)
    except app_blueprints.BlueprintError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        slug = _slug(req.slug or str(blueprint.get("slug") or ""))
        _scoped(ctx, username, f"{layout.scope_workspace(username)}/apps/{slug}")
        out = await app_blueprints.import_folder(
            ctx.agent, username, owner_sub, slug, folder,
            template_slug=f"bundle:{slug}", tasks=blueprint.get("tasks") or [])
    except (app_blueprints.BlueprintError, app_deploy.DeployError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    out["scope"] = db_apps.app_scope(username)
    return out
