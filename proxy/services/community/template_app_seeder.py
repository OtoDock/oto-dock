"""Template apps seeded per member (COMMUNITY-AGENTS-REGISTRY.md "Per-user
template apps", APPS.md "Blueprints and templates").

A template's ``user-apps/<slug>/`` is one folder app PER MEMBER: the
installer's copy at install, every later member's copy on attach, from a
seed source the install keeps at ``config/community/user-apps/<slug>/``.
Each copy is a cold deploy — the folder, release 1, the row, no rendered
check and no server start (the first open starts it) — approved with the
installer's recorded consent when that consent may cover the copy
(``admits``: the same signature, no owner-only surface, the installer's
authority still standing), else pending on the owner's card.

The attach hook runs in a worker thread at seven call sites; the deploy
pipeline lives on the loop. So the hook ENQUEUES and one loop-side worker
seeds, one copy at a time under a per-copy lock; the install path awaits
its own seeds so the envelope is complete. A failed seed tells the member
and the managers; a missing copy is enqueued again when the member's apps
list is next read. The shared apps of a template go through the same cold
deploy, once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from services.apps import app_deploy
from storage import database as task_store
from storage.agents import agent_store, template_sig
from storage.agents.community_agent_template_store import (
    AppItem,
    CommunityAgentTemplate,
    TemplateValidationError,
    available_visibilities,
    load_template_from_dict,
)
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.community-agent-installer")

SEED_SUBDIR = ("config", "community", "user-apps")
HEAL_INTERVAL_S = 60.0

_loop: asyncio.AbstractEventLoop | None = None
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_healed: dict[tuple[str, str], float] = {}
# (agent, owner_sub, slug) → the failure the people were told of: a copy
# that keeps failing (the heal retries it every minute) is told once per
# reason, and forgotten when it lands.
_failure_told: dict[tuple[str, str, str], str] = {}


def install(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Startup: the queue and its worker on the loop."""
    global _loop, _queue, _worker
    _loop = loop or asyncio.get_running_loop()
    _queue = asyncio.Queue()
    _worker = _loop.create_task(_drain())


async def stop() -> None:
    global _worker
    if _worker is not None:
        _worker.cancel()
        _worker = None


def template_ref(template_slug: str, slug: str) -> str:
    return f"{template_slug}:{slug}"


def seed_source_dir(agent: str, slug: str) -> Path:
    return config.get_agent_dir(agent).joinpath(*SEED_SUBDIR) / slug


class LinkedPath(ValueError):
    """A path the template machinery would reach through a symlink."""


def unlinked(path: Path, root: Path) -> bool:
    """True when ``path`` lies inside ``root`` with no symlink on the way,
    ``path`` itself included (it need not exist yet)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return os.path.realpath(path) == os.path.join(os.path.realpath(root), *rel.parts)


def guard(root: Path, path: Path) -> Path:
    """``path`` for a read or a write the template machinery makes itself
    under an agent's tree — never through a symlink, which a session can
    plant under ``/config`` (a manager's session writes there)."""
    if not unlinked(path, root):
        raise LinkedPath(f"{path} is reached through a link; it is not followed")
    return path


def write_seed_source(agent: str, item: AppItem) -> Path:
    """The template's copy of a per-user app, kept for later members and
    for an update: the files a release takes (``walk_tree``: no dotfiles,
    no ``data/``, no ``node_modules``, no links), verbatim."""
    from services.apps import releases
    assert item.dir is not None
    dest = guard(config.get_agent_dir(agent), seed_source_dir(agent, item.slug))
    shutil.rmtree(dest, ignore_errors=True)
    for rel, path in releases.walk_tree(item.dir):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return dest


def _blueprint_tasks(folder: Path) -> list[dict]:
    bp = folder / template_sig.BLUEPRINT_DOC
    if not bp.is_file():
        return []
    try:
        doc = json.loads(bp.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    return list(doc.get("tasks") or []) if isinstance(doc, dict) else []


def _blueprint_triggers(folder: Path) -> list[dict]:
    bp = folder / template_sig.BLUEPRINT_DOC
    if not bp.is_file():
        return []
    try:
        doc = json.loads(bp.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    return list(doc.get("triggers") or []) if isinstance(doc, dict) else []


# A membership removal pauses the copy's seeded triggers with this mark; a
# re-attach resumes exactly those (a pause the member set stays).
REMOVED_MARK = "paused: removed from the agent"


def pause_copy_triggers(app_id: str) -> int:
    from storage.automation import trigger_store
    n = 0
    for t in trigger_store.list_triggers_for_app(app_id):
        if not t.get("community_template") or not t.get("enabled"):
            continue
        trigger_store.set_trigger_enabled(t["id"], False)
        trigger_store.set_last_error(t["id"], REMOVED_MARK)
        n += 1
    return n


def resume_copy_triggers(app_id: str) -> int:
    from storage.automation import trigger_store
    n = 0
    for t in trigger_store.list_triggers_for_app(app_id):
        if t.get("enabled") or (t.get("last_error") or "") != REMOVED_MARK:
            continue
        trigger_store.set_trigger_enabled(t["id"], True)
        trigger_store.set_last_error(t["id"], "")
        n += 1
    return n


# ── the dialog ──────────────────────────────────────────────────────────────


def preview_shape(app_json: dict, visibility: str, blueprint: dict | None) -> dict:
    """A template app in the ``GET /v1/apps`` row shape BEFORE any row
    exists, for the install dialog's approval-card components: the raw
    manifest's blocks through the same parsers a stored row goes through,
    a ``fire_task`` button's task named from the blueprint. Nothing is
    approved, nothing is stored."""
    from api.apps import manifest as _mf
    from storage.db_apps import MANIFEST_BLOCKS
    pseudo = {name: (json.dumps(app_json.get(name)) if app_json.get(name) not in (None, "", [], {}) else "")
              for name in (*MANIFEST_BLOCKS, "requires")}
    tasks = {str(t.get("slug")): t for t in (blueprint or {}).get("tasks") or [] if isinstance(t, dict)}
    actions = []
    for a in app_json.get("actions") or []:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if a.get("type") == "fire_task" and a.get("task"):
            a["task_name"] = str(tasks.get(str(a["task"]), {}).get("description") or a["task"])
        actions.append(a)
    steps = {name: {k: v for k, v in spec.items() if k in ("run", "timeout")}
             for name, spec in (app_json.get("steps") or {}).items() if isinstance(spec, dict)}
    return {
        "id": "", "slug": "", "title": str(app_json.get("title") or ""),
        "scope": "personal" if visibility == "user" else "shared", "pin_scope": "standing",
        "position": 0, "rel_path": "", "updated_at": "", "kind": task_store.APP_KIND_FOLDER,
        "actions": actions, "actions_sig": "", "actions_approved": False, "approval_stale": False,
        "can_approve": False, "can_manage": False, "viewer_role": "viewer", "hidden_for_me": False,
        "files": _mf.parse_files(pseudo), "egress": _mf.parse_egress(pseudo),
        "handlers": _mf.parse_handlers(pseudo), "exports": _mf.parse_exports(pseudo),
        "bindings": _mf.parse_bindings(pseudo), "requires": _mf.parse_requires(pseudo),
        "manifest_empty": not actions and not any(pseudo[b] for b in MANIFEST_BLOCKS),
        "steps": steps, "secrets": [{**s, "set": False, "declared": True} for s in _mf.parse_secrets(pseudo)],
        "inbound": _mf.parse_inbound(pseudo), "external": _mf.parse_external(pseudo),
    }


def check_words(entry: dict) -> str:
    """One plain line for a template check on the dialog: what consenting
    to it lets run, and where."""
    sections = set(entry.get("sections") or [])
    parts = []
    if "script" in sections:
        parts.append("runs a script in your members' sessions, on their machines")
    if "judge" in sections:
        parts.append("a judge run per turn")
    if "handler" in sections:
        app = entry.get("handler_app") or "an app"
        parts.append(f"sends the changed files to the app {app}")
    if "schema" in sections:
        parts.append("checks the answer against a schema")
    words = "; ".join(parts)
    applies = entry.get("applies") or []
    if applies and len(applies) < 3:
        words += f" (on {', '.join(applies)})"
    return words


# ── consent ─────────────────────────────────────────────────────────────────


def record_consent(data: dict, template: CommunityAgentTemplate,
                   app_consent: dict[str, str] | None, check_consent: dict[str, str] | None,
                   consent_by: str) -> list[str]:
    """Write the installer's consent into the persisted record: an app or a
    check whose signature the caller rendered matches what the tarball
    holds. Returns the names whose given signature did NOT match (they
    land pending or offered, never approved)."""
    now = datetime.now(timezone.utc).isoformat()
    ignored: list[str] = []
    apps: dict[str, dict] = {}
    for item in template.apps:
        given = (app_consent or {}).get(item.slug)
        if not given or not consent_by:
            continue
        if given == item.sig:
            apps[item.slug] = {"sig": item.sig, "by": consent_by, "at": now}
        else:
            ignored.append(f"app:{item.slug}")
    checks: dict[str, dict] = {}
    for c in template.checks:
        given = (check_consent or {}).get(c.name)
        if not given or not consent_by:
            continue
        if given == c.sig:
            checks[c.name] = {"sig": c.sig, "by": consent_by, "at": now}
        else:
            ignored.append(f"check:{c.name}")
    data["consent"] = {"apps": apps, "checks": checks}
    return ignored


def consent_entry(data: dict | None, kind: str, key: str) -> dict | None:
    entry = ((data or {}).get("consent") or {}).get(kind, {}).get(key)
    return entry if isinstance(entry, dict) else None


def admits(item: AppItem, entry: dict | None, agent: str, username: str,
           owner_sub: str | None) -> tuple[str, str]:
    """Whose consent approves this copy: ``(sub, "")``, or ``("", why
    not)``. A per-user copy that does things only its owner may approve
    is admitted only when the consenting person IS the owner."""
    from api.apps.manifest import sub_can_approve_surface
    if entry is None:
        return "", "no consent was given for this app"
    by = str(entry.get("by") or "")
    if entry.get("sig") != item.sig or not by:
        return "", "the template changed since the consent was given"
    if item.visibility == "user" and item.owner_approval and by != (owner_sub or ""):
        return "", "the owner approves this app on their own card"
    if not sub_can_approve_surface(by, {"agent": agent, "username": username, "owner_sub": owner_sub}):
        return "", "the person who consented may no longer approve it"
    return by, ""


def copy_matches(item: AppItem, folder: Path) -> bool:
    """True when a stored copy of a template app (the per-user seed source,
    a new version kept beside an edited app) still hashes to the signature
    in the record — the one a consent names. The copies live under
    ``/config``, which a manager's session writes: a consent approves what
    its person was shown, never an edit made to the copy afterwards."""
    from services.apps import releases
    try:
        doc = json.loads((folder / template_sig.MANIFEST_DOC).read_text(encoding="utf-8"))
        bp_path = folder / template_sig.BLUEPRINT_DOC
        blueprint = json.loads(bp_path.read_text(encoding="utf-8")) if bp_path.is_file() else {}
        tree = template_sig.tree_sha(releases.walk_tree(folder))
        sig = template_sig.template_app_sig(doc, blueprint or None, tree)
    except Exception:  # noqa: BLE001 — anything unreadable is not the consented copy
        return False
    return bool(item.sig) and sig == item.sig


COPY_CHANGED = "the template's stored copy of the app changed since the consent was given"


# ── seeding ─────────────────────────────────────────────────────────────────


def _lock(agent: str, username: str, slug: str) -> asyncio.Lock:
    return _locks.setdefault((agent, username, slug), asyncio.Lock())


async def seed_shared(agent: str, template: CommunityAgentTemplate, data: dict) -> dict:
    """The template's shared apps, once, through the cold deploy."""
    from services.apps import app_blueprints
    out: dict = {"seeded": [], "pending": [], "failed": []}
    for item in template.apps:
        if item.visibility != "user":
            assert item.dir is not None
            by, why = admits(item, consent_entry(data, "apps", item.slug), agent, "", None)
            try:
                res = await app_blueprints.import_folder(
                    agent, "", None, item.slug, item.dir, template_slug=template.slug,
                    tasks=list(item.blueprint.get("tasks") or []), cold=True, approve_as=by,
                    template_ref=template_ref(template.slug, item.slug),
                    triggers=list(item.blueprint.get("triggers") or []))
            except Exception as e:
                logger.exception("template app %s: import failed", item.slug)
                out["failed"].append({"slug": item.slug, "reason": str(e)})
                continue
            status = str(res.get("status") or "")
            if status == app_deploy.RESULT_REFUSED:
                out["failed"].append({"slug": item.slug, "reason": "; ".join(
                    f.get("message", "") for f in res.get("findings") or []) or "refused"})
            elif status == app_deploy.RESULT_PENDING_APPROVAL:
                out["pending"].append({"slug": item.slug,
                                       "reason": why or res.get("consent_refused") or res.get("waiting") or ""})
            else:
                out["seeded"].append(item.slug)
    return out


async def seed_for_install(agent: str, template: CommunityAgentTemplate, data: dict,
                           installer_sub: str | None) -> dict:
    """At install: the shared apps, the seed sources of the per-user apps,
    and the installer's own copies. Awaited, so the envelope is complete."""
    out = await seed_shared(agent, template, data)
    out["user"] = []
    for item in template.apps:
        if item.visibility != "user":
            continue
        try:
            await asyncio.to_thread(write_seed_source, agent, item)
        except Exception as e:
            logger.exception("template app %s: seed source not written", item.slug)
            out["failed"].append({"slug": item.slug, "reason": str(e)})
            continue
        if installer_sub:
            status, detail = await seed_user_copy(agent, installer_sub, item.slug, data=data)
            _record(out, item.slug, status, detail, key="user")
    return out


def _record(out: dict, slug: str, status: str, detail: str, *, key: str) -> None:
    if status == "seeded":
        out[key].append(slug)
    elif status == "pending":
        out["pending"].append({"slug": slug, "reason": detail})
    elif status == "failed":
        out["failed"].append({"slug": slug, "reason": detail})


def user_items(template: CommunityAgentTemplate, role: str) -> list[AppItem]:
    """The per-user apps a member with ``role`` gets on attach."""
    return [a for a in template.apps if a.visibility == "user" and a.auto_create_for_new_users
            and (not a.roles or role in a.roles)]


def enqueue(agent: str, owner_sub: str, slug: str) -> bool:
    """From any thread: one seed job for the worker. False when no worker
    runs (a test, the standalone scheduler) — the caller seeds directly or
    leaves it to the next heal."""
    if _loop is None or _queue is None or _loop.is_closed():
        return False
    _loop.call_soon_threadsafe(_queue.put_nowait, (agent, owner_sub, slug))
    return True


def enqueue_for_member(agent: str, template: CommunityAgentTemplate, owner_sub: str, role: str) -> int:
    count = 0
    for item in user_items(template, role):
        if enqueue(agent, owner_sub, item.slug):
            count += 1
    return count


async def _drain() -> None:
    assert _queue is not None
    while True:
        agent, owner_sub, slug = await _queue.get()
        try:
            status, detail = await seed_user_copy(agent, owner_sub, slug)
            logger.info("template app %s for %s on %s: %s %s", slug, owner_sub, agent, status, detail)
        except Exception:
            logger.exception("template app %s for %s on %s: seed crashed", slug, owner_sub, agent)
        finally:
            _queue.task_done()


async def seed_user_copy(agent: str, owner_sub: str, slug: str, *,
                         data: dict | None = None) -> tuple[str, str]:
    """One member's copy of one per-user template app: ``("seeded" |
    "pending" | "exists" | "restored" | "kept" | "skipped" | "failed",
    detail)``. Idempotent: an existing row of this template gets its
    schedule rows checked and nothing else; a row the member made
    themselves, a folder without a row, an opted-out copy and a mode that
    offers no personal apps are left alone."""
    from services.apps import app_blueprints, app_handlers
    if data is None:
        data = await asyncio.to_thread(agent_store.get_community_template_data, agent)
    if not data:
        return "skipped", "the agent has no template record"
    try:
        template = load_template_from_dict(data)
    except TemplateValidationError as e:
        return "skipped", f"the template record is unreadable: {e}"
    item = next((a for a in template.apps if a.slug == slug and a.visibility == "user"), None)
    if item is None:
        return "skipped", "the template ships no per-user app of that name"
    row = await asyncio.to_thread(agent_store.get_agent, agent)
    if not row:
        return "skipped", "no such agent"
    if "user" not in available_visibilities(bool(row.get("collaborative", True)),
                                            row.get("default_scope") or "user"):
        return "skipped", "the agent's mode offers no personal apps"
    username = await asyncio.to_thread(task_store.get_username_by_sub, owner_sub) or ""
    if not username:
        return "skipped", "the member has no username"
    ref = template_ref(template.slug, slug)
    async with _lock(agent, username, slug):
        existing = await asyncio.to_thread(task_store.get_app_by_slug, agent, username, slug)
        agent_dir = config.get_agent_dir(agent)
        folder = layout.user_dir(agent_dir, username) / layout.WORKSPACE / "apps" / slug
        if existing is not None:
            if not existing.get("template_ref"):
                return "kept", "the member has an app of their own under that name"
            if existing.get("template_state") == "removed":
                fresh = await asyncio.to_thread(task_store.set_template_state, existing["id"], "",
                                                hidden=False) or existing
                await app_handlers.sync_rows(fresh)
                await asyncio.to_thread(resume_copy_triggers, existing["id"])
                return "restored", "back after a membership removal"
            if not existing.get("hidden"):
                await app_handlers.sync_rows(existing)
            return "exists", ("opted out" if existing.get("template_state") == "opted_out"
                              else "already seeded")
        if folder.is_dir():
            return "kept", "the member has a folder of their own under that name"
        source = seed_source_dir(agent, slug)
        if not source.is_dir():
            return "skipped", "the template's copy of the app is not on this install"
        by, why = admits(item, consent_entry(data, "apps", slug), agent, username, owner_sub)
        if by and not await asyncio.to_thread(copy_matches, item, source):
            by, why = "", COPY_CHANGED
        try:
            res = await app_blueprints.import_folder(
                agent, username, owner_sub, slug, source, template_slug=template.slug,
                tasks=_blueprint_tasks(source), cold=True, approve_as=by, template_ref=ref,
                triggers=_blueprint_triggers(source))
        except Exception as e:
            reason = str(e)
            logger.exception("template app %s for %s: seed failed", slug, username)
            await _notify_failed(agent, owner_sub, item, reason)
            return "failed", reason
    status = str(res.get("status") or "")
    if status == app_deploy.RESULT_REFUSED:
        reason = "; ".join(f.get("message", "") for f in res.get("findings") or []) or "refused"
        await _notify_failed(agent, owner_sub, item, reason)
        return "failed", reason
    _failure_told.pop((agent, owner_sub, slug), None)
    await _notify_triggers_skipped(agent, owner_sub, item, res.get("triggers") or {})
    if status == app_deploy.RESULT_PENDING_APPROVAL:
        return "pending", why or str(res.get("consent_refused") or res.get("waiting") or "")
    return "seeded", f"release {res.get('release')}"


async def restore(row: dict) -> dict:
    """The pin hook's restore of an opted-out or unpinned template copy:
    the row comes back from the template's copy through the same seed."""
    from services.apps import app_blueprints
    agent = row["agent"]
    slug = row["slug"]
    username = row.get("username") or ""
    data = await asyncio.to_thread(agent_store.get_community_template_data, agent)
    template = load_template_from_dict(data) if data else None
    item = next((a for a in (template.apps if template else []) if a.slug == slug), None)
    source = seed_source_dir(agent, slug) if username else None
    if item is None or source is None or not source.is_dir():
        from fastapi import HTTPException
        raise HTTPException(status_code=400,
                            detail=f"the template's copy of '{slug}' is not on this install")
    by, _why = admits(item, consent_entry(data, "apps", slug), agent, username, row.get("owner_sub"))
    if by and not await asyncio.to_thread(copy_matches, item, source):
        by = ""
    async with _lock(agent, username, slug):
        # The import's upsert clears the state and unhides the row itself;
        # an import that fails leaves the opt-out as it was.
        res = await app_blueprints.import_folder(
            agent, username, row.get("owner_sub"), slug, source, template_slug=template.slug,
            tasks=_blueprint_tasks(source), cold=True, approve_as=by,
            template_ref=row.get("template_ref") or template_ref(template.slug, slug),
            triggers=_blueprint_triggers(source))
    if res.get("app_id"):
        await asyncio.to_thread(resume_copy_triggers, str(res["app_id"]))
    res["restored"] = "brought back from the template's copy"
    return res


async def on_user_removed(agent: str, sub: str) -> int:
    """A membership removal: the member's template copies stop and hide,
    marked so a re-attach brings them back (an opt-out is not touched)."""
    from services.apps import app_lifecycle
    username = await asyncio.to_thread(task_store.get_username_by_sub, sub) or ""
    if not username:
        return 0
    rows = [r for r in await asyncio.to_thread(task_store.list_template_rows, agent, username)
            if r.get("template_state") != "opted_out"]
    await app_lifecycle.stop_rows(rows)
    for r in rows:
        await asyncio.to_thread(task_store.set_template_state, r["id"], "removed", hidden=True)
        await asyncio.to_thread(pause_copy_triggers, r["id"])
    return len(rows)


def heal_missing(agent: str, viewer_sub: str, username: str) -> int:
    """From the apps list: a member whose copy never landed (a crash
    mid-loop, a restore) gets it enqueued again, at most once a minute per
    viewer. Synchronous — the list route runs it off the loop."""
    if not username or not viewer_sub:
        return 0
    key = (agent, viewer_sub)
    now = time.monotonic()
    last = _healed.get(key)
    if last is not None and now - last < HEAL_INTERVAL_S:
        return 0
    _healed[key] = now
    data = agent_store.get_community_template_data(agent)
    if not data:
        return 0
    try:
        template = load_template_from_dict(data)
    except TemplateValidationError:
        return 0
    # The row alone: the template names per-agent roles, never the platform one.
    role = roles.row_role(task_store.get_user_agent_roles(viewer_sub), agent)
    if not role:
        return 0
    count = 0
    for item in user_items(template, role):
        if task_store.get_app_by_slug(agent, username, item.slug) is not None:
            continue
        if (layout.user_dir(config.get_agent_dir(agent), username) / layout.WORKSPACE / "apps" / item.slug).is_dir():
            continue
        if enqueue(agent, viewer_sub, item.slug):
            count += 1
    return count


async def _notify_triggers_skipped(agent: str, owner_sub: str, item: AppItem, results: dict) -> None:
    """The copy landed, a trigger of its blueprint did not (a slug the
    member already uses): the member is told which, once per seed."""
    skipped = {slug: r for slug, r in results.items() if str(r).startswith("skipped")}
    if not skipped:
        return
    from services.notifications import notification_manager
    lines = "; ".join(f"'{slug}': {str(r)[len('skipped: '):] or r}" for slug, r in skipped.items())
    try:
        await notification_manager.fire_notification(
            f"{item.title or item.slug}: a trigger was not created",
            f"Your copy of the app is there, but not every trigger that comes with it: {lines}. "
            "Remove or rename yours on the Triggers tab and restore the app to get it.",
            severity="warning", scope="user", target=owner_sub, source="community_agent", source_id=agent)
    except Exception:
        logger.exception("template app %s: trigger notification to %s failed", item.slug, owner_sub)


async def _notify_failed(agent: str, owner_sub: str, item: AppItem, reason: str) -> None:
    from services.notifications import notification_manager
    from storage.identity import db_users
    key, told = (agent, owner_sub, item.slug), f"{item.sig}\n{reason}"
    if _failure_told.get(key) == told:
        return
    _failure_told[key] = told
    title = f"{item.title or item.slug} could not be set up for you"
    body = f"The app that comes with this agent was not deployed: {reason}"
    targets = {owner_sub}
    try:
        for u in await asyncio.to_thread(db_users.get_agent_users, agent):
            if roles.can_manage(u.get("agent_role")):
                targets.add(u["sub"])
    except Exception:
        logger.exception("template app %s: manager lookup failed", item.slug)
    for sub in targets:
        try:
            await notification_manager.fire_notification(
                title, body, severity="warning", scope="user", target=sub,
                source="community_agent", source_id=agent)
        except Exception:
            logger.exception("template app %s: notification to %s failed", item.slug, sub)
