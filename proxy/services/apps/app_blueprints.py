"""Blueprints (APPS.md "Blueprints and templates"): an app travels as a
folder — a community template's ``apps/<slug>/``, or a ``<slug>.otoapp``
archive an agent exports and imports. An import writes the folder into the
scope's workspace, seeds the tasks the bundle carries BY SLUG, rewrites the
buttons to the new ids, and cuts release 1 as PENDING through the deploy
pipeline: the card asks before anything runs, whoever authored it. An
import never pre-approves.

Constraints: actions travel by slug (a raw ``task_id`` in a bundle is
refused); an ``mcp_tool`` naming an MCP the agent lacks refuses the import
with the name; the archive reader takes regular files and directories
only, under the release caps, never ``extractall``; ``data/``, dotfiles and
``node_modules`` never travel; the seeded tasks are idempotent by
``community_template_item_slug`` (``<app slug>__<task slug>``).
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

import config
from core.session.session_state import get_user_tz
from services.apps import app_deploy, releases
from services.infra import path_confinement, safe_fs
from storage import database as task_store
from core import layout

logger = logging.getLogger("claude-proxy.apps")

FORMAT = 1
BUNDLE_NAME = "blueprint.json"
MAX_BUNDLE_TASKS = 16
MAX_BUNDLE_TRIGGERS = 8
MAX_BUNDLE_BYTES = 40 * 1024 * 1024
_TASK_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_NEVER_PARTS = frozenset({"node_modules", "data"})


class BlueprintError(Exception):
    """The bundle cannot be read or imported; the message is for the agent."""


# ── export ──────────────────────────────────────────────────────────────────


def _task_items(actions: list[dict]) -> tuple[list[dict], list[dict]]:
    """The bundle's task items (keyed by the button's id) and the actions
    with ``task`` in place of ``task_id``."""
    items: list[dict] = []
    out: list[dict] = []
    for a in actions:
        a = dict(a)
        if a.get("type") == "fire_task":
            dyn = task_store.get_dynamic_task(str(a.get("task_id") or ""))
            if not dyn:
                raise BlueprintError(f"button {a.get('id')!r}: its task is gone — fix the app first")
            items.append({"slug": a["id"], "description": dyn.get("name") or a["id"],
                          "prompt": dyn.get("prompt") or ""})
            a.pop("task_id", None)
            a["task"] = a["id"]
        a.pop("task_name", None)
        a.pop("mcp_available", None)
        out.append(a)
    return items, out


def export_bundle(row: dict) -> tuple[bytes, dict]:
    """``(archive bytes, blueprint)`` of the app's WORKING folder: the files
    a release would take, ``app.json`` with its buttons by slug, and
    ``blueprint.json`` naming the tasks."""
    source = config.get_agent_dir(row["agent"]) / (row.get("rel_path") or "")
    # The tree first (no link anywhere in it; the release caps are the
    # export's), then the manifest and the page, each read without following
    # a link.
    try:
        files = releases.walk_tree(source)
    except releases.ReleaseInvalid as e:
        raise BlueprintError(str(e))
    if not any(rel == "app.json" for rel, _p in files):
        raise BlueprintError("only an app with a folder (app.json) can be exported")
    doc = app_deploy.read_app_json(source)
    app_deploy.require_entry_page(source)
    items, actions = _task_items(doc.get("actions") or [])
    doc["actions"] = actions
    blueprint = {"format": FORMAT, "slug": row["slug"], "title": doc.get("title") or row.get("title") or row["slug"],
                 "tasks": items, "requires": doc.get("requires") or {}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        def _add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        _add(BUNDLE_NAME, json.dumps(blueprint, indent=2, sort_keys=True).encode("utf-8"))
        total = 0
        for rel, _path in files:
            if rel == "app.json":
                _add(rel, json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
                continue
            try:
                data = releases.read_tree_file(source, rel, max_size=releases.MAX_RELEASE_FILE_BYTES)
            except OSError:
                raise BlueprintError(f"{rel} changed while it was exported; export again")
            total += len(data)
            if total > releases.MAX_RELEASE_BYTES:
                raise BlueprintError("the app is larger than 32 MB")
            _add(rel, data)
    return buf.getvalue(), blueprint


# ── read ────────────────────────────────────────────────────────────────────


def _safe_rel(name: str) -> str:
    """A bundle member's path, clean and relative (the value joined below the
    extraction dir is the one returned here, never the raw name)."""
    if not name or name.startswith("/"):
        raise BlueprintError(f"the bundle holds an unsafe path {name!r}")
    try:
        rel = path_confinement.normalize_rel_path(name)
    except path_confinement.PathOutsideRoot:
        raise BlueprintError(f"the bundle holds an unsafe path {name!r}")
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts) or any(p in _NEVER_PARTS for p in parts):
        raise BlueprintError(f"the bundle holds {name!r} — dotfiles, node_modules and data never travel")
    return rel


def read_bundle(data: bytes) -> tuple[Path, dict]:
    """Extract a ``.otoapp`` into a fresh temporary directory under the
    release caps; ``(folder, blueprint)``. The caller removes the folder."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise BlueprintError("the bundle is larger than 40 MB")
    tmp = Path(tempfile.mkdtemp(prefix="otodock-app-import-"))
    blueprint: dict | None = None
    total = 0
    count = 0
    entries = 0
    try:
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        except (tarfile.TarError, OSError, EOFError) as e:
            raise BlueprintError(f"not a .otoapp archive: {e}")
        with tar:
            # Member by member, every entry counted (folders too): the whole
            # index up front was gigabytes for a small archive of empty
            # entries before any cap applied.
            for m in tar:
                entries += 1
                if entries > 2 * releases.MAX_RELEASE_FILES:
                    raise BlueprintError("the bundle exceeds 32 MB or 500 files")
                if m.isdir():
                    rel = _safe_rel(m.name.rstrip("/"))
                    try:
                        safe_fs.mkdirs_beneath(str(tmp), rel)
                    except OSError:
                        raise BlueprintError(f"the bundle holds {rel!r} twice, as a file and a folder")
                    continue
                if not m.isfile():
                    raise BlueprintError(f"the bundle holds {m.name!r}, which is not a regular file")
                rel = _safe_rel(m.name)
                if m.size > releases.MAX_RELEASE_FILE_BYTES:
                    raise BlueprintError(f"{rel} is larger than 2 MB")
                total += m.size
                count += 1
                if total > releases.MAX_RELEASE_BYTES or count > releases.MAX_RELEASE_FILES:
                    raise BlueprintError("the bundle exceeds 32 MB or 500 files")
                fh = tar.extractfile(m)
                if fh is None:
                    raise BlueprintError(f"{rel} could not be read from the bundle")
                content = fh.read()
                if rel == BUNDLE_NAME:
                    try:
                        blueprint = json.loads(content.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise BlueprintError("blueprint.json is not valid JSON")
                    continue
                # Every member lands beneath the extraction directory through
                # the helpers: no link on the way is ever written through.
                try:
                    safe_fs.atomic_write_beneath(str(tmp), rel, content, mkdirs=True, fsync=False)
                except OSError:
                    raise BlueprintError(f"{rel} could not be unpacked from the bundle")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if not isinstance(blueprint, dict) or int(blueprint.get("format") or 0) != FORMAT:
        shutil.rmtree(tmp, ignore_errors=True)
        raise BlueprintError("the bundle has no blueprint.json of format 1")
    if not (tmp / "app.json").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise BlueprintError("the bundle has no app.json")
    return tmp, blueprint


# ── import ──────────────────────────────────────────────────────────────────


def _seed_task(item: dict, agent: str, created_by: str, scope: str, app_slug: str,
               template_slug: str) -> str:
    """One trigger-type task for a bundle item, idempotent by
    ``<app slug>__<task slug>``; the existing row's id when it is there."""
    import psycopg
    slug = str(item.get("slug") or "")
    if not _TASK_SLUG_RE.match(slug):
        raise BlueprintError(f"bundle task {slug!r}: not a valid slug")
    prompt = str(item.get("prompt") or "").strip()
    if not prompt or len(prompt) > 8000:
        raise BlueprintError(f"bundle task {slug!r}: a prompt of 1 to 8000 characters is required")
    item_slug = f"{app_slug}__{slug}"
    task_id = f"task-app-{app_slug}-{slug}-{agent}-{created_by or 'agent'}"[:120]
    # A shared app's task follows the platform clock (NULL; its created_by
    # is the agent slug, never a person); a personal one takes the owner's
    # zone when their dashboard reported it.
    user_tz = get_user_tz(created_by) if scope == "user" else None
    from services.scheduler import task_kinds
    try:
        task_store.create_dynamic_task(
            task_id=task_id, agent=agent, name=str(item.get("description") or slug)[:200],
            prompt=prompt, llm_mode="cli", task_type=task_kinds.TRIGGER, schedule=None, run_at=None,
            delay_seconds=None, timeout_seconds=600, created_by=created_by, scope=scope,
            notification_mode="manual", notify_severity="info", user_tz=user_tz,
            community_template=template_slug, community_template_item_slug=item_slug,
        )
        return task_id
    except psycopg.errors.UniqueViolation:
        existing = task_store.find_template_task(
            agent, item_slug, created_by if scope == "user" else None)
        if not existing:
            raise BlueprintError(f"bundle task {slug!r}: exists under another owner")
        return existing["id"]


def _seed_trigger(item: dict, agent: str, created_by: str, scope: str, app_slug: str,
                  template_slug: str, app_id: str, handler_names: list[str]) -> str:
    """One trigger for a blueprint item (APPS.md "Blueprints and templates"),
    aimed at the row just deployed — a member's own for a personal copy, the
    agent's for a shared app — idempotent by ``<app slug>__<trigger slug>``.
    An existing row keeps its name and its paused state (the member's); its
    handler is re-pointed only when the one it has is no longer in the
    manifest, and a row ``detach_app`` left (``app_id`` NULL) is re-attached
    and enabled. A slug the owner already uses skips the trigger, never the
    copy. Answers ``created`` / ``exists`` / ``repointed`` / ``reattached`` /
    ``skipped: <reason>``."""
    import psycopg
    from services.scheduler import trigger_manager
    from storage.automation import trigger_store
    slug = str(item.get("slug") or "")
    handler = str(item.get("handler") or "")
    if not slug or handler not in handler_names:
        return f"skipped: trigger {slug!r} aims at no handler of the app"
    item_slug = f"{app_slug}__{slug}"
    existing = trigger_store.find_template_trigger(agent, item_slug, created_by if scope == "user" else None)
    if existing is not None:
        if not existing.get("app_id"):
            trigger_store.update_trigger(existing["id"], {"app_id": app_id, "handler": handler})
            trigger_store.set_trigger_enabled(existing["id"], True)
            trigger_store.set_last_error(existing["id"], "")
            return "reattached"
        if (existing.get("app_id") or "") != app_id:
            return "skipped: aimed at another app"
        if (existing.get("handler") or "") not in handler_names:
            trigger_store.update_trigger(existing["id"], {"handler": handler})
            return "repointed"
        return "exists"
    try:
        trigger_manager.register_trigger(
            name=str(item.get("description") or slug)[:200], scope=scope, agent=agent,
            created_by=created_by, slug=f"{app_slug}-{slug}", app_id=app_id, handler=handler,
            require_approval=False,
            trigger_id=f"trig-app-{app_slug}-{slug}-{agent}-{created_by or 'agent'}"[:120],
            community_template=template_slug, community_template_item_slug=item_slug)
    except psycopg.errors.UniqueViolation:
        return f"skipped: a trigger '{app_slug}-{slug}' already exists in this scope"
    except (trigger_manager.TriggerValidationError, trigger_manager.TriggerConflict) as e:
        return f"skipped: {e}"
    return "created"


def _seed_triggers(items: list[dict], agent: str, created_by: str, scope: str, app_slug: str,
                   template_slug: str, app_id: str, doc: dict) -> dict[str, str]:
    from api.apps import manifest as _mf
    pseudo = {"handlers": json.dumps(doc.get("handlers") or {})}
    names = list(_mf.parse_handlers(pseudo).get("on_trigger") or [])
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BlueprintError("bundle triggers must be objects")
        slug = str(item.get("slug") or "")
        try:
            out[slug] = _seed_trigger(item, agent, created_by, scope, app_slug, template_slug, app_id, names)
        except Exception as e:
            logger.exception("App import %s: trigger %s not seeded", app_slug, slug)
            out[slug] = f"skipped: {e}"
    return out


def _rewrite_actions(doc: dict, task_ids: dict[str, str]) -> None:
    for a in doc.get("actions") or []:
        if not isinstance(a, dict) or a.get("type") != "fire_task":
            continue
        if a.get("task_id"):
            raise BlueprintError(f"button {a.get('id')!r}: actions travel by slug — "
                                 "use \"task\": \"<bundle task slug>\", never task_id")
        key = str(a.pop("task", "") or "")
        if key not in task_ids:
            raise BlueprintError(f"button {a.get('id')!r}: the bundle has no task {key!r}")
        a["task_id"] = task_ids[key]


async def import_folder(agent: str, username: str, owner_sub: str | None, slug: str,
                        source_dir: Path, *, template_slug: str, tasks: list[dict] | None,
                        cold: bool = False, approve_as: str = "",
                        template_ref: str | None = None,
                        triggers: list[dict] | None = None) -> dict:
    """Write ``source_dir`` as the scope's ``apps/<slug>/`` with its buttons
    pointing at freshly seeded tasks, void any approval the slug had, and
    run the deploy pipeline — release 1 lands pending unless the manifest
    is empty, or unless a template seed carries the installer's consent
    (``cold`` / ``approve_as`` / ``template_ref``: APPS.md "Blueprints and
    templates"); then the blueprint's ``triggers`` are seeded at the row
    (``_seed_trigger``; the result's ``triggers`` says what became of
    each). ``DeployError`` and ``BlueprintError`` carry the reason."""
    from services.infra import file_bookkeeping
    shared = not username
    try:
        files = await asyncio_to_thread(releases.walk_tree, source_dir)
    except releases.ReleaseInvalid as e:
        raise BlueprintError(str(e))
    doc = await asyncio_to_thread(app_deploy.read_app_json, source_dir)
    try:
        await asyncio_to_thread(app_deploy.require_entry_page, source_dir)
    except app_deploy.DeployError as e:
        raise BlueprintError(str(e))
    items = list(tasks or [])
    if len(items) > MAX_BUNDLE_TASKS:
        raise BlueprintError(f"at most {MAX_BUNDLE_TASKS} tasks in a bundle")
    trigger_items = list(triggers or [])
    if len(trigger_items) > MAX_BUNDLE_TRIGGERS:
        raise BlueprintError(f"at most {MAX_BUNDLE_TRIGGERS} triggers in a bundle")
    created_by = agent if shared else (owner_sub or "")
    scope = "agent" if shared else "user"
    agent_dir = config.get_agent_dir(agent)
    rel_dir = f"{layout.scope_workspace(username)}/apps/{slug}"
    try:
        root = path_confinement.join_under(agent_dir, rel_dir)
    except path_confinement.PathOutsideRoot:
        raise BlueprintError("the app folder would leave the workspace")

    def _folder_is_plain() -> None:
        # Judged before any task is seeded, so a refused write leaves no
        # orphan rows; the write below judges it again at the open.
        agents_root, dest_rel = releases.agents_rel(root)
        try:
            st = safe_fs.lstat_beneath(agents_root, dest_rel)
        except FileNotFoundError:
            return
        except safe_fs.SymlinkRefused:
            raise BlueprintError(f"apps/{slug} sits behind a link, make it a real folder")
        if stat.S_ISLNK(st.st_mode):
            raise BlueprintError(f"apps/{slug} is a link, make it a real folder")

    await asyncio_to_thread(_folder_is_plain)
    task_ids: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BlueprintError("bundle tasks must be objects")
        task_ids[str(item.get("slug") or "")] = await asyncio_to_thread(
            _seed_task, item, agent, created_by, scope, slug, template_slug)
    _rewrite_actions(doc, task_ids)
    await asyncio_to_thread(app_deploy.validate_app_json, doc, agent, shared)

    def _write() -> None:
        # The folder is written beneath the agents tree and the files are read
        # beneath their own root: a link in place of the folder (or above it),
        # or one a file turned into after the walk, is refused, never followed.
        agents_root, dest_rel = releases.agents_rel(root)
        src_root, src_base = releases.tree_root(source_dir)
        try:
            safe_fs.rmtree_beneath(agents_root, dest_rel, missing_ok=True)
            for rel, _path in files:
                if rel == "app.json":
                    safe_fs.atomic_write_beneath(
                        agents_root, f"{dest_rel}/{rel}",
                        json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"),
                        mode=releases.RELEASE_FILE_MODE, mkdirs=True, fsync=False)
                else:
                    safe_fs.copy_file_beneath(
                        src_root, f"{src_base}/{rel}" if src_base else rel, agents_root, f"{dest_rel}/{rel}",
                        max_size=releases.MAX_RELEASE_FILE_BYTES, mode=releases.RELEASE_FILE_MODE,
                        mkdirs=True)
        except safe_fs.SymlinkRefused:
            raise BlueprintError(f"apps/{slug} is a link, or sits behind one: make it a real folder")
        except OSError:
            raise BlueprintError(f"apps/{slug} could not be written; try again")

    await asyncio_to_thread(_write)
    try:
        await file_bookkeeping.push_tree_write(agent, root, agent_dir, writer="app-import")
    except Exception:
        logger.exception("App import %s: workspace push failed (continuing)", slug)
    existing = await asyncio_to_thread(task_store.get_app_by_slug, agent, username, slug)
    if existing is not None and not template_ref:
        # A blueprint import is never pre-approved. A template seed's approval
        # is the consent's business instead: ``approve_as`` grants it and a
        # changed manifest stales it on its own (the signature moves), so a
        # re-release with the same manifest keeps the copy live.
        await asyncio_to_thread(task_store.clear_app_approval, existing["id"])
    out = await app_deploy.deploy_folder(agent, username, owner_sub, slug, root, rel_dir,
                                         cold=cold, approve_as=approve_as,
                                         template_ref=template_ref)
    out["tasks"] = task_ids
    out["triggers"] = {}
    if trigger_items and out.get("app_id") and str(out.get("status") or "") not in (
            app_deploy.RESULT_REFUSED, app_deploy.RESULT_REJECTED):
        # After the row exists (the trigger aims at it); a pending copy's
        # trigger waits with it — the drain holds its wakes as "unapproved".
        out["triggers"] = await asyncio_to_thread(
            _seed_triggers, trigger_items, agent, created_by, scope, slug, template_slug,
            str(out["app_id"]), doc)
    out["path"] = "/" + rel_dir
    logger.info("App imported: agent=%s slug=%s scope=%s status=%s tasks=%d", agent, slug,
                "shared" if shared else "personal", out.get("status"), len(task_ids))
    return out


async def asyncio_to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
