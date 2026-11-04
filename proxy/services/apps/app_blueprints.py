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
from core import layout
from core.session.session_state import get_user_tz
from services.apps import app_deploy, releases
from services.infra import path_confinement, safe_fs
from storage import database as task_store

logger = logging.getLogger("otodock.apps")

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


# A bundle's origin reaches every session's prompt: a slug of the catalog's
# shape and a release of at most four digits a part, matched whole.
_ORIGIN_SLUG_RE = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")
_ORIGIN_VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
# A bundle's source (the row it was exported from, services/apps/app_follow.py):
# an agent slug of the shape agent_store.sanitize_slug makes (2 to 40
# characters, a digit first allowed), the app slug's shape, a username of
# word characters, a row id and a release number, each matched whole.
_SOURCE_AGENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]")
_SOURCE_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,40}")


def origin_of(row: dict) -> tuple[str, str]:
    """Where a row's code came from: its origin columns, else, for a copy a
    template seeded before the columns existed, the template of its
    ``template_ref`` and the agent's installed version; ``("", "")`` for
    the agent's own app."""
    if row.get("from_template"):
        return str(row["from_template"]), str(row.get("from_version") or "")
    ref = str(row.get("template_ref") or "")
    if ":" in ref:
        from storage.agents import agent_store
        agent = agent_store.get_agent(str(row.get("agent") or "")) or {}
        return ref.split(":", 1)[0], str(agent.get("community_template_version") or "")
    return "", ""


def clean_origin(blueprint: dict) -> tuple[str, str] | None:
    """A bundle's origin as the row keeps it: a template slug and a
    release, else nothing (a bundle is the agent's own file: its words
    never reach a prompt unchecked)."""
    slug = str(blueprint.get("from_template") or "")
    version = str(blueprint.get("from_version") or "")
    if not _ORIGIN_SLUG_RE.fullmatch(slug):
        return None
    return slug, version if _ORIGIN_VERSION_RE.fullmatch(version) else ""


def clean_source(blueprint: dict) -> dict | None:
    """A bundle's source as the row keeps it (``follows``): the row's key,
    its id and the release the bundle took, else nothing."""
    raw = blueprint.get("source")
    if not isinstance(raw, dict):
        return None
    from services.apps.slugs import APP_SLUG_RE
    agent, slug = str(raw.get("agent") or ""), str(raw.get("slug") or "")
    username, row_id = str(raw.get("username") or ""), str(raw.get("id") or "")
    release = raw.get("release")
    if not (_SOURCE_AGENT_RE.fullmatch(agent) and APP_SLUG_RE.fullmatch(slug) and _SOURCE_ID_RE.fullmatch(row_id)):
        return None
    if username and not _SOURCE_USER_RE.fullmatch(username):
        return None
    if not isinstance(release, int) or isinstance(release, bool) or not 0 <= release <= 999999:
        return None
    return {"id": row_id, "agent": agent, "username": username, "slug": slug, "release": release}


def _release_files(release_dir: Path) -> list[str]:
    """The files a release holds, from its manifest: never a walk of the
    release folder, which also keeps the data snapshot taken before the
    release went live."""
    manifest = releases.read_manifest(release_dir)
    if manifest is None:
        raise BlueprintError("The list of the release's files is gone. Deploy the app again.")
    return sorted(manifest["files"])


def export_bundle(row: dict, *, release_dir: Path | None = None) -> tuple[bytes, dict]:
    """``(archive bytes, blueprint)`` of the app's WORKING folder, or of one
    release of it (``release_dir``, read by its manifest): the files a
    release takes, ``app.json`` with its buttons by slug, and
    ``blueprint.json`` naming the tasks, the row the bundle came from
    (``source``: what a copy follows) and, for a template's copy, the
    template and version the code came from."""
    working = config.get_agent_dir(row["agent"]) / (row.get("rel_path") or "")
    source = release_dir if release_dir is not None else working
    # The tree first (no link anywhere in it; the release caps are the
    # export's), then the manifest and the page, each read without following
    # a link.
    try:
        rels = (_release_files(source) if release_dir is not None
                else [rel for rel, _p in releases.walk_tree(source)])
    except releases.ReleaseInvalid as e:
        raise BlueprintError(str(e)) from e
    if "app.json" not in rels:
        raise BlueprintError("only an app with a folder (app.json) can be exported")
    doc = app_deploy.read_app_json(source)
    app_deploy.require_entry_page(source)
    items, actions = _task_items(doc.get("actions") or [])
    doc["actions"] = actions
    blueprint = {"format": FORMAT, "slug": row["slug"], "title": doc.get("title") or row.get("title") or row["slug"],
                 "tasks": items, "requires": doc.get("requires") or {},
                 "source": {"id": row["id"], "agent": row["agent"], "username": row.get("username") or "",
                            # The live release, also for a working folder with
                            # edits not released yet: a copy is never offered
                            # an older release than the one it holds.
                            "slug": row["slug"], "release": releases.current_number(row)}}
    from_template, from_version = origin_of(row)
    if from_template:
        blueprint["from_template"] = from_template
        blueprint["from_version"] = from_version
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        def _add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        _add(BUNDLE_NAME, json.dumps(blueprint, indent=2, sort_keys=True).encode("utf-8"))
        total = 0
        for rel in rels:
            if rel == "app.json":
                _add(rel, json.dumps(doc, indent=2, sort_keys=True).encode("utf-8"))
                continue
            # A template's copy keeps the template's own blueprint in its
            # folder; the one written above is the export's and a second
            # entry would replace it at the import.
            if rel == BUNDLE_NAME:
                continue
            try:
                if release_dir is not None:
                    read = releases.read_release_file(source, rel)
                    if read is None:
                        raise BlueprintError(f"{rel} is missing from the release. Deploy the app again.")
                    data = read
                else:
                    data = releases.read_tree_file(source, rel, max_size=releases.MAX_RELEASE_FILE_BYTES)
            except releases.ReleaseDamaged as exc:
                raise BlueprintError(f"{rel} no longer matches the release. Deploy the app again.") from exc
            except OSError as exc:
                raise BlueprintError(f"{rel} changed while it was exported; export again") from exc
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
    except path_confinement.PathOutsideRoot as exc:
        raise BlueprintError(f"the bundle holds an unsafe path {name!r}") from exc
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
            raise BlueprintError(f"not a .otoapp archive: {e}") from e
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
                    except OSError as exc:
                        raise BlueprintError(f"the bundle holds {rel!r} twice, as a file and a folder") from exc
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
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise BlueprintError("blueprint.json is not valid JSON") from exc
                    continue
                # Every member lands beneath the extraction directory through
                # the helpers: no link on the way is ever written through.
                try:
                    safe_fs.atomic_write_beneath(str(tmp), rel, content, mkdirs=True, fsync=False)
                except OSError as exc:
                    raise BlueprintError(f"{rel} could not be unpacked from the bundle") from exc
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
    except psycopg.errors.UniqueViolation as exc:
        existing = task_store.find_template_task(
            agent, item_slug, created_by if scope == "user" else None)
        if not existing:
            raise BlueprintError(f"bundle task {slug!r}: exists under another owner") from exc
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
    from services.apps import manifest as _mf
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
                        triggers: list[dict] | None = None,
                        origin: tuple[str, str] | None = None,
                        follows: dict | None = None,
                        voids_approval: bool = True, replace: bool = True,
                        hold: bool = False, keep_tasks: dict[str, str] | None = None) -> dict:
    """Write ``source_dir`` as the scope's ``apps/<slug>/`` with its buttons
    pointing at freshly seeded tasks, void any approval the slug had, and
    run the deploy pipeline — release 1 lands pending unless the manifest
    is empty, or unless a template seed carries the installer's consent
    (``cold`` / ``approve_as`` / ``template_ref``: APPS.md "Blueprints and
    templates"); then the blueprint's ``triggers`` are seeded at the row
    (``_seed_trigger``; the result's ``triggers`` says what became of
    each). ``DeployError`` and ``BlueprintError`` carry the reason.

    A release another agent's app sends (services/apps/app_follow.py) passes ``voids_approval=False`` (the deploy judges the standing
    approval by the manifest's signature), ``replace=False`` (the bundle's
    files are written over the folder and the files the folder's walk lists
    that the bundle lacks are removed; dotfiles, ``node_modules`` and
    ``data/`` are left alone), ``hold=True`` (the release waits for a
    person whatever the manifest says) and ``keep_tasks`` (button id to task
    id: a button the folder already binds keeps its task and the bundle's
    item for it is not seeded). ``follows`` (``clean_source``) is written on
    the row after the deploy when the row is another than the source and
    the source row exists on this install."""
    from services.infra import file_bookkeeping
    shared = not username
    keep = dict(keep_tasks or {})
    try:
        files = await asyncio_to_thread(releases.walk_tree, source_dir)
    except releases.ReleaseInvalid as e:
        raise BlueprintError(str(e)) from e
    doc = await asyncio_to_thread(app_deploy.read_app_json, source_dir)
    try:
        await asyncio_to_thread(app_deploy.require_entry_page, source_dir)
    except app_deploy.DeployError as e:
        raise BlueprintError(str(e)) from e
    items = [t for t in (tasks or []) if not (isinstance(t, dict) and str(t.get("slug") or "") in keep)]
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
    except path_confinement.PathOutsideRoot as exc:
        raise BlueprintError("the app folder would leave the workspace") from exc

    def _folder_is_plain() -> None:
        # Judged before any task is seeded, so a refused write leaves no
        # orphan rows; the write below judges it again at the open.
        agents_root, dest_rel = releases.agents_rel(root)
        try:
            st = safe_fs.lstat_beneath(agents_root, dest_rel)
        except FileNotFoundError:
            return
        except safe_fs.SymlinkRefused as exc:
            raise BlueprintError(f"apps/{slug} sits behind a link, make it a real folder") from exc
        if stat.S_ISLNK(st.st_mode):
            raise BlueprintError(f"apps/{slug} is a link, make it a real folder")

    await asyncio_to_thread(_folder_is_plain)
    task_ids: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise BlueprintError("bundle tasks must be objects")
        task_ids[str(item.get("slug") or "")] = await asyncio_to_thread(
            _seed_task, item, agent, created_by, scope, slug, template_slug)
    _rewrite_actions(doc, {**keep, **task_ids})
    await asyncio_to_thread(app_deploy.validate_app_json, doc, agent, shared)
    if not replace:
        # The overlay: the files the folder holds and the bundle does not go
        # through the files' own delete, so a paired machine drops them too.
        wanted = {rel for rel, _path in files}

        def _dropped() -> list[str]:
            try:
                return [rel for rel, _path in releases.walk_tree(root) if rel not in wanted]
            except releases.ReleaseInvalid as exc:
                raise BlueprintError(str(exc)) from exc

        from services.apps import app_follow
        await app_follow.delete_files(agent, root, await asyncio_to_thread(_dropped))

    def _write() -> None:
        # The folder is written beneath the agents tree and the files are read
        # beneath their own root: a link in place of the folder (or above it),
        # or one a file turned into after the walk, is refused, never followed.
        agents_root, dest_rel = releases.agents_rel(root)
        src_root, src_base = releases.tree_root(source_dir)
        try:
            if replace:
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
        except safe_fs.SymlinkRefused as exc:
            raise BlueprintError(f"apps/{slug} is a link, or sits behind one: make it a real folder") from exc
        except OSError as exc:
            raise BlueprintError(f"apps/{slug} could not be written; try again") from exc

    await asyncio_to_thread(_write)
    try:
        await file_bookkeeping.push_tree_write(agent, root, agent_dir, writer="app-import")
    except Exception:
        logger.exception("App import %s: workspace push failed (continuing)", slug)
    existing = await asyncio_to_thread(task_store.get_app_by_slug, agent, username, slug)
    if existing is not None and not template_ref and voids_approval:
        # A blueprint import is never pre-approved. A template seed's approval
        # is the consent's business instead: ``approve_as`` grants it and a
        # changed manifest stales it on its own (the signature moves), so a
        # re-release with the same manifest keeps the copy live.
        await asyncio_to_thread(task_store.clear_app_approval, existing["id"])
    out = await app_deploy.deploy_folder(agent, username, owner_sub, slug, root, rel_dir,
                                         cold=cold, approve_as=approve_as,
                                         template_ref=template_ref, origin=origin, hold=hold)
    out["tasks"] = task_ids
    if follows and out.get("app_id") and str(out.get("status") or "") not in (
            app_deploy.RESULT_REFUSED, app_deploy.RESULT_REJECTED):
        from services.apps import app_follow
        await asyncio_to_thread(app_follow.record, str(out["app_id"]), (agent, username, slug), follows,
                                int(out.get("release") or 0))
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
