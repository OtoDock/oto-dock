"""Template updates for installed community agents (COMMUNITY-AGENTS-
REGISTRY.md "Updates").

An installed agent remembers the template version it came from and the
as-installed hashes of every piece (``community_template_data.baseline``).
When the catalog carries a newer version, a manager plans and applies the
update here: a piece still equal to what the template installed is
REPLACED with the new version, a piece someone changed since is KEPT and
the new version is stored beside it under ``config/community/<version>/``
for the manager to take; new pieces are ADDED; nothing is ever removed.
The version moves last, so a failure mid-way leaves what was applied and a
retry resumes against the pieces' own hashes.

One update runs per agent at a time (an in-process lock; the proxy is one
process) and the apply runs as a background job: the route answers 202,
the presser is told by notification when the report is in.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

import config
from services.apps import app_deploy
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
from storage.agents import agent_store, template_sig
from storage.agents.community_agent_template_store import (
    AppItem,
    CommunityAgentTemplate,
    TemplateValidationError,
    load_template_from_dict,
    load_template_from_dir,
    substitute_template_vars,
    template_to_persistable_dict,
)
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.community-agent-updater")

_agent_locks: dict[str, asyncio.Lock] = {}
_jobs: dict[str, dict] = {}
# A time.monotonic() stamp, which counts from boot: 0 would hold the first
# sweep back until the host has been up a day.
_last_notify_sweep = float("-inf")
NOTIFY_SWEEP_S = 24 * 3600
BESIDE_DIR = "community"   # config/community/<version>/… holds the kept pieces' new versions
# The version names a folder under config/community: a release number (it
# starts with a digit, so it never names user-apps/ or dashboards/ there).
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
REMOVED = "removed locally"   # the kept reason of a piece the manager deleted after the install


# ── detection ───────────────────────────────────────────────────────────────


def catalog_is_newer(catalog_version: str | None, installed: str | None) -> bool:
    from services.community.community_catalog import _catalog_is_newer
    return _catalog_is_newer(catalog_version, installed)


def detect(agent_row: dict | None, entry: dict | None) -> dict:
    """``{installed_version, catalog_version, update_available, compat_ok}``
    for one installed agent against its catalog entry. A locally authored
    agent (``local:`` provenance) never updates."""
    from services.community import community_catalog
    installed = str((agent_row or {}).get("community_template_version") or "")
    catalog = str((entry or {}).get("version") or "") if entry else ""
    provenance = str((agent_row or {}).get("community_template") or "")
    compat = bool(entry) and community_catalog.platform_version_ok(entry.get("platform_min_version"))
    return {
        "installed_version": installed,
        "catalog_version": catalog,
        "update_available": bool(entry) and not provenance.startswith("local:")
        and catalog_is_newer(catalog, installed),
        "compat_ok": compat,
    }


async def installed_versions() -> dict[str, dict]:
    """``{agent_slug: {template, version}}`` for every template install."""
    from storage.pg import get_conn

    def _q():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT slug, community_template, community_template_version FROM agents "
                "WHERE community_template IS NOT NULL").fetchall()
        return {r["slug"]: {"template": r["community_template"],
                            "version": r["community_template_version"] or ""} for r in rows}
    return await asyncio.to_thread(_q)


def status(agent_slug: str) -> dict:
    """The running or last update job of an agent."""
    job = _jobs.get(agent_slug)
    if not job:
        return {"running": False}
    return {"running": job["status"] in ("starting", "running"), **{k: v for k, v in job.items() if k != "task"}}


# ── the catalog side ────────────────────────────────────────────────────────


async def _fetch_new(agent_row: dict) -> tuple[CommunityAgentTemplate, Path, dict]:
    """The catalog's current version of the agent's template, loaded, with
    its registry entry. Refuses a local template, a catalog version that is
    not newer, and a version this platform cannot run."""
    from services.community import community_agents_catalog, community_catalog
    provenance = str(agent_row.get("community_template") or "")
    if not provenance:
        raise HTTPException(400, "this agent was not installed from a community template")
    if provenance.startswith("local:"):
        raise HTTPException(400, "a locally authored template has no catalog version to update from")
    try:
        registry = await community_agents_catalog.fetch_registry()
    except Exception as exc:
        raise HTTPException(502, f"Could not load community-agents catalog: {exc}")
    entry = next((e for e in registry.get("agents", []) if e.get("slug") == provenance), None)
    if not entry:
        raise HTTPException(404, f"the template '{provenance}' is no longer in the catalog")
    found = detect(agent_row, entry)
    if not found["update_available"]:
        raise HTTPException(409, f"already at {found['installed_version'] or 'the catalog version'}")
    community_catalog.require_platform_compat(provenance, entry.get("platform_min_version"))
    extracted = await community_agents_catalog.fetch_and_extract_template(provenance)
    try:
        template = await asyncio.to_thread(load_template_from_dir, extracted)
        if not _VERSION_RE.match(template.version):
            raise TemplateValidationError(f"version {template.version!r} is not a release number")
    except TemplateValidationError as exc:
        shutil.rmtree(extracted.parent, ignore_errors=True)
        raise HTTPException(400, f"Invalid catalog template: {exc}")
    return template, extracted, entry


def consent_lines(template: CommunityAgentTemplate, old: CommunityAgentTemplate | None) -> dict:
    """The apps and checks of the new version the presser consents to, in
    the install dialog's shape, each marked new or changed against the
    installed version (an unchanged one needs no new consent)."""
    from services.community import template_app_seeder
    old_apps = {a.slug: a for a in (old.apps if old else [])}
    old_checks = {c.name: c for c in (old.checks if old else [])}
    apps = []
    for a in template.apps:
        assert a.dir is not None
        doc = json.loads((a.dir / template_sig.MANIFEST_DOC).read_text(encoding="utf-8"))
        bp = a.blueprint or None
        row = template_app_seeder.preview_shape(doc, a.visibility, bp)
        row["slug"] = a.slug
        prev = old_apps.get(a.slug)
        apps.append({
            "slug": a.slug, "title": a.title or a.slug, "visibility": a.visibility, "sig": a.sig,
            "owner_approval": a.owner_approval, "row": row,
            "blueprint_tasks": [{"slug": str(t.get("slug") or ""), "description": str(t.get("description") or ""),
                                 "prompt": str(t.get("prompt") or "")}
                                for t in (bp or {}).get("tasks") or [] if isinstance(t, dict)],
            "blueprint_triggers": [{"slug": str(t.get("slug") or ""), "handler": str(t.get("handler") or ""),
                                    "description": str(t.get("description") or "")}
                                   for t in (bp or {}).get("triggers") or [] if isinstance(t, dict)],
            "change": "new" if prev is None else ("changed" if prev.sig != a.sig else "same"),
        })
    checks = []
    for c in template.checks:
        prev = old_checks.get(c.name)
        entry = {"name": c.name, "description": str(c.doc.get("description") or ""),
                 "sections": [s for s in ("schema", "script", "handler", "judge") if c.doc.get(s)],
                 "applies": list(c.doc.get("applies") or []), "script": c.script_name or "",
                 "handler_app": str((c.doc.get("handler") or {}).get("app") or "")
                 if isinstance(c.doc.get("handler"), dict) else ""}
        checks.append({
            "name": c.name, "description": entry["description"], "mandatory": c.mandatory,
            "applies": entry["applies"], "script": entry["script"], "sig": c.sig,
            "words": template_app_seeder.check_words(entry),
            "change": "new" if prev is None else ("changed" if prev.sig != c.sig else "same"),
        })
    return {"apps": apps, "checks": checks}


# ── the layers ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _Run:
    agent: str
    agent_dir: Path
    new: CommunityAgentTemplate
    old: CommunityAgentTemplate | None
    old_data: dict
    baseline: dict
    apply: bool
    by_sub: str
    by_role: str
    # The layers run in worker threads; a config write is pushed to the
    # satellites on this loop.
    loop: asyncio.AbstractEventLoop | None = None
    report: dict = dataclasses.field(default_factory=lambda: {
        "replaced": [], "added": [], "kept": [], "unchanged": 0,
        "pending_apps": [], "offered_checks": [], "members": {}, "mcps": {}, "ignored_fields": []})

    @property
    def beside(self) -> Path:
        return self.agent_dir / layout.CONFIG / BESIDE_DIR / self.new.version

    def sub(self, text: str) -> str:
        return substitute_template_vars(text, self.agent)

    def replaced(self, what: str) -> None:
        self.report["replaced"].append(what)

    def added(self, what: str) -> None:
        self.report["added"].append(what)

    def same(self) -> None:
        self.report["unchanged"] += 1

    def removed(self, what: str) -> None:
        """A piece the installed version had and the agent no longer has was
        removed on purpose: the update leaves it out and says so (the
        reseed route exists for a recovery)."""
        self.report["kept"].append({"what": what, "reason": REMOVED, "path": "", "new_path": ""})

    def kept(self, what: str, reason: str, path: str, new_rel: str, new_text: str | None = None,
             new_tree: Path | None = None) -> None:
        """A locally changed piece stays; the new version lands beside it
        (a file's text, or an app folder's tree) for "Take the new version"."""
        if self.apply:
            from services.community.template_app_seeder import guard
            dest = guard(self.agent_dir, self.beside / new_rel)
            if new_text is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new_text, encoding="utf-8")
            elif new_tree is not None:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(new_tree, dest, symlinks=False)
        self.report["kept"].append({
            "what": what, "reason": reason, "path": path,
            "new_path": f"config/{BESIDE_DIR}/{self.new.version}/{new_rel}",
        })


def _sha(text: str) -> str:
    return template_sig.sha256_text(text)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _file_layer(run: _Run, what: str, path: Path, rel: str, new_text: str, base_sha: str | None,
                new_rel: str, *, add_when_missing: bool, commit: bool = False) -> None:
    """The rule every file piece follows: missing → added (or left alone),
    equal to the new → unchanged, equal to the baseline → replaced,
    anything else → kept with the new version beside."""
    from services.community.template_app_seeder import guard
    local = _read(path)
    if local is None:
        if not add_when_missing:
            return
        if run.apply:
            guard(run.agent_dir, path).parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
            _push(run, path, commit=commit)
        run.added(what)
        return
    if local == new_text:
        run.same()
        return
    if base_sha and _sha(local) == base_sha:
        if run.apply:
            guard(run.agent_dir, path).write_text(new_text, encoding="utf-8")
            _push(run, path, commit=commit)
        run.replaced(what)
        return
    run.kept(what, "edited locally" if base_sha else "no record of the installed version",
             rel, new_rel, new_text=new_text)


def _push(run: _Run, path: Path, *, commit: bool) -> None:
    """A config write reaches the satellites like any other, and the persona
    is committed so the admin revert route can undo it."""
    from services.infra import file_bookkeeping, git_writer
    rel = path.relative_to(run.agent_dir).as_posix()
    if commit:
        with contextlib.suppress(Exception):
            git_writer.commit_paths(run.agent_dir / layout.CONFIG, [path],
                                    f"template update: {rel} to {run.new.version}")
    if run.loop is not None and not run.loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            file_bookkeeping.push_file_write(run.agent, rel, path, writer=None), run.loop)


def _layer_persona(run: _Run) -> None:
    _file_layer(run, "the persona (config/agent.md)", run.agent_dir / layout.CONFIG / "agent.md",
                "config/agent.md", run.new.prompt_md, run.baseline.get("persona"), "agent.md",
                add_when_missing=True, commit=True)


def _layer_description(run: _Run) -> None:
    """The agent's description column follows the template's while it still
    reads what the installed version said."""
    row = agent_store.get_agent(run.agent) or {}
    local = str(row.get("description") or "")
    new = run.new.description
    if local == new:
        run.same()
        return
    old = run.old.description if run.old and run.old.description else None
    if old is not None and local == old:
        if run.apply:
            agent_store.update_agent(run.agent, description=new)
        run.replaced("the description")
        return
    run.report["kept"].append({"what": "the description",
                               "reason": "edited locally" if old else "no record of the installed version",
                               "path": "", "new_path": ""})


def _strip_context(rel: str) -> str:
    return rel.split("/", 1)[1] if rel.startswith("context/") else rel


def _layer_context(run: _Run) -> None:
    """The record keeps no context text, only the baseline's hashes: a
    file the baseline knows and the disk lacks was removed."""
    base = run.baseline.get("context") or {}
    old_names = {_strip_context(r) for r in base}
    for rel, content in sorted(run.new.context_files.items()):
        stripped = _strip_context(rel)
        path = run.agent_dir / layout.CONFIG / layout.CONTEXT / stripped
        if stripped in old_names and not path.is_file():
            run.removed(f"context/{stripped}")
            continue
        _file_layer(run, f"context/{stripped}", path,
                    f"config/context/{stripped}", run.sub(content), base.get(rel),
                    f"context/{stripped}", add_when_missing=True)


def _layer_setup(run: _Run) -> None:
    """The agent-wide guide is rewritten only while the old one is still
    there: a completed setup is never resurrected."""
    if run.new.setup_md is None:
        return
    path = run.agent_dir / layout.CONFIG / layout.CONTEXT / "setup.md"
    if not path.is_file():
        run.report.setdefault("notes", []).append("the setup guide was completed; the new one was not added")
        return
    _file_layer(run, "the setup guide (setup.md)", path, "config/context/setup.md",
                run.sub(run.new.setup_md), run.baseline.get("setup"), "setup.md", add_when_missing=False)


def _layer_user_setup(run: _Run) -> None:
    """The canonical per-user guide follows the file rule; a member's own
    copy is replaced while it is still the installed version's text, or
    the old canonical text when the canonical itself moves to the new
    version — a guide a manager edited (kept) stays in every copy made
    from it, and a member mid-onboarding on an edited copy keeps theirs."""
    if run.new.user_setup_md is None:
        return
    canonical = run.agent_dir / layout.CONFIG / "user-setup.md"
    old_text = _read(canonical)
    new_text = run.sub(run.new.user_setup_md)
    base = run.baseline.get("user_setup")
    follows = old_text is None or old_text == new_text or bool(base and _sha(old_text) == base)
    _file_layer(run, "the per-user guide (user-setup.md)", canonical, "config/user-setup.md",
                new_text, base, "user-setup.md", add_when_missing=True)
    replaced = kept = 0
    users_dir = run.agent_dir / layout.USERS
    if users_dir.is_dir():
        for udir in sorted(p for p in users_dir.iterdir() if p.is_dir()):
            copy = udir / layout.CONTEXT / "user-setup.md"
            text = _read(copy)
            if text is None or text == new_text:
                continue
            if (base and _sha(text) == base) or (follows and text == old_text):
                if run.apply:
                    copy.write_text(new_text, encoding="utf-8")
                    _push(run, copy, commit=False)
                replaced += 1
            else:
                kept += 1
    run.report["members"]["user_setup_replaced"] = replaced
    run.report["members"]["user_setup_kept"] = kept


def _members(agent: str) -> list[dict]:
    from storage.identity import db_users
    return db_users.get_agent_users(agent)


def _layer_items(run: _Run) -> None:
    """Tasks, triggers and notifications: a new item is seeded for the agent
    and for every member its role admits; an existing row whose template
    fields still equal the installed version's takes the new ones; an
    edited row is kept."""
    from services.community import community_agent_installer as inst
    from storage.automation import db_tasks, notification_store
    old_tasks = {t.slug: t for t in (run.old.tasks if run.old else [])}
    old_notifs = {n.slug: n for n in (run.old.notifications if run.old else [])}
    old_trigs = {t.slug: t for t in (run.old.triggers if run.old else [])}
    members = _members(run.agent)
    tslug = run.new.slug

    def _task_fields(item) -> dict:
        return {
            "prompt": item.prompt, "name": item.description or item.slug,
            "schedule": item.cron if item.schedule_kind == "cron" else None,
            "interval_seconds": item.interval_seconds if item.schedule_kind == "interval" else None,
            "run_at": item.run_at if item.schedule_kind == "run_at" else None,
        }

    def _row_matches(row: dict, item) -> bool:
        want = _task_fields(item)
        return all(str(row.get(k) or "") == str(v or "") for k, v in want.items())

    for item in run.new.tasks:
        what = f"the task '{item.slug}'"
        rows: list[dict] = []
        if item.scope == "agent":
            r = db_tasks.find_template_task(run.agent, item.slug)
            rows = [r] if r else []
        else:
            for m in members:
                r = db_tasks.find_template_task(run.agent, item.slug, m["sub"])
                if r:
                    rows.append(r)
        if not rows:
            if item.slug in old_tasks:
                run.removed(what)
                continue
            if run.apply:
                if item.scope == "agent":
                    inst._create_task_idempotent(item, run.agent, run.by_sub, tslug)
                else:
                    for m in members:
                        inst._seed_tasks_for_user(run.agent, dataclasses.replace(run.new, tasks=[item]),
                                                  m["sub"], m["agent_role"])
            run.added(what)
            continue
        prev = old_tasks.get(item.slug)
        for r in rows:
            if _row_matches(r, item):
                run.same()
            elif prev is not None and _row_matches(r, prev):
                if run.apply:
                    db_tasks.update_dynamic_task(r["id"], _task_fields(item))
                    inst.schedule_task_row(r["id"])
                run.replaced(what if item.scope == "agent" else f"{what} of {r.get('created_by') or 'a member'}")
            else:
                run.report["kept"].append({"what": what, "reason": "edited locally", "path": "", "new_path": ""})

    for item in run.new.triggers:
        what = f"the trigger '{item.slug}'"
        key = f"{item.slug}__task"
        rows = []
        if item.scope == "agent":
            r = db_tasks.find_template_task(run.agent, key)
            rows = [r] if r else []
        else:
            for m in members:
                r = db_tasks.find_template_task(run.agent, key, m["sub"])
                if r:
                    rows.append(r)
        if not rows:
            if item.slug in old_trigs:
                run.removed(what)
                continue
            if run.apply:
                if item.scope == "agent":
                    inst._seed_trigger_with_paired_task(item, run.agent, run.by_sub, tslug)
                else:
                    for m in members:
                        inst._seed_triggers_for_user(run.agent, dataclasses.replace(run.new, triggers=[item]),
                                                     m["sub"], m["agent_role"])
            run.added(what)
            continue
        prev = old_trigs.get(item.slug)
        for r in rows:
            if str(r.get("prompt") or "") == item.prompt:
                run.same()
            elif prev is not None and str(r.get("prompt") or "") == prev.prompt:
                if run.apply:
                    db_tasks.update_dynamic_task(r["id"], {"prompt": item.prompt,
                                                           "name": f"[trigger] {item.description or item.slug}"})
                run.replaced(what)
            else:
                run.report["kept"].append({"what": what, "reason": "edited locally", "path": "", "new_path": ""})

    def _notif_fields(item) -> dict:
        return {
            "title": item.title, "body": item.body,
            "schedule": item.cron if item.schedule_kind == "cron" else None,
            "interval_seconds": item.interval_seconds if item.schedule_kind == "interval" else None,
            "run_at": item.run_at if item.schedule_kind == "run_at" else None,
        }

    for item in run.new.notifications:
        what = f"the notification '{item.slug}'"
        ids = ([f"notif-{tslug}-{item.slug}-{run.agent}"] if item.scope == "agent"
               else [f"notif-{tslug}-{item.slug}-{run.agent}-{m['sub']}" for m in members])
        rows = [r for r in (notification_store.get_notification(i) for i in ids) if r]
        if not rows:
            if item.slug in old_notifs:
                run.removed(what)
                continue
            if run.apply:
                if item.scope == "agent":
                    inst._seed_notifications(run.agent, dataclasses.replace(run.new, notifications=[item]), None)
                else:
                    for m in members:
                        inst._seed_notifs_for_user(run.agent, dataclasses.replace(run.new, notifications=[item]),
                                                   m["sub"], m["agent_role"])
            run.added(what)
            continue
        prev = old_notifs.get(item.slug)
        want = _notif_fields(item)
        for r in rows:
            def _eq(fields: dict) -> bool:
                return all(str(r.get(k) or "") == str(v or "") for k, v in fields.items())
            if _eq(want):
                run.same()
            elif prev is not None and _eq(_notif_fields(prev)):
                if run.apply:
                    notification_store.update_notification(r["id"], dict(
                        want, notification_type="recurring" if (want["schedule"] or want["interval_seconds"])
                        else "one_time"))
                    inst.schedule_notification_row(r["id"])
                run.replaced(what)
            else:
                run.report["kept"].append({"what": what, "reason": "edited locally", "path": "", "new_path": ""})


def _layer_dashboards(run: _Run) -> None:
    from storage import database as task_store
    base = run.baseline.get("dashboards") or {}
    old_dash = {d.slug for d in (run.old.dashboards if run.old else [])}
    src_dir = run.agent_dir / layout.CONFIG / BESIDE_DIR / "dashboards"
    # "Take the new version" replaces the presser's own copy: another
    # member's kept copy is listed without it.
    presser = task_store.get_username_by_sub(run.by_sub) or ""
    for d in run.new.dashboards:
        html = run.sub(d.html or "")
        if not html.strip():
            continue
        if run.apply:
            from services.community.template_app_seeder import guard
            src_dir.mkdir(parents=True, exist_ok=True)
            guard(run.agent_dir, src_dir / f"{d.slug}.html").write_text(html, encoding="utf-8")
        targets: list[tuple[str, Path, str]] = []
        if d.visibility == "agent":
            targets.append(("", run.agent_dir / layout.WORKSPACE / "apps" / f"{d.slug}.html",
                            f"{layout.WORKSPACE}/apps/{d.slug}.html"))
        else:
            users_dir = run.agent_dir / layout.USERS
            for udir in sorted(p for p in users_dir.iterdir() if p.is_dir()) if users_dir.is_dir() else []:
                p = udir / layout.WORKSPACE / "apps" / f"{d.slug}.html"
                if p.is_file():
                    targets.append((udir.name, p, f"{layout.user_rel(udir.name)}/{layout.WORKSPACE}/apps/{d.slug}.html"))
        for username, path, rel in targets:
            what = f"the dashboard '{d.slug}'" + (f" of {username}" if username else "")
            local = _read(path)
            if local is None:
                if d.slug in old_dash:
                    run.removed(what)
                    continue
                if run.apply:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(html, encoding="utf-8")
                    task_store.upsert_app(run.agent, "", None, d.slug, title=d.title, rel_path=rel,
                                          actions_json="[]")
                run.added(what)
            elif local == html:
                run.same()
            elif base.get(d.slug) and _sha(local) == base[d.slug]:
                if run.apply:
                    path.write_text(html, encoding="utf-8")
                    _push(run, path, commit=False)
                run.replaced(what)
            else:
                run.kept(what, "edited locally" if base.get(d.slug) else "no record of the installed version",
                         rel, f"dashboards/{d.slug}.html", new_text=html)
                if username and username != presser:
                    run.report["kept"][-1]["new_path"] = ""


def _tree_unchanged(folder: Path, old: AppItem | None, template_doc: dict) -> bool:
    """D6: the working tree minus app.json still hashes to the installed
    version's tree, and the manifest still reads as the template's (an app
    with fire_task buttons is compared without their targets, which the
    seed rewrote; a record from before that baseline, on its tree alone).
    Every file is read without following a link: a file turned into one
    after the walk makes the tree an edited one. Blocking: a worker-thread
    call."""
    from services.apps import releases
    if old is None:
        return False
    try:
        tree = template_sig.tree_sha(folder, [rel for rel, _path in releases.walk_tree(folder)])
    except Exception:
        return False
    if tree != old.tree_sha:
        return False
    try:
        local_doc = json.loads(template_sig.read_file(folder, template_sig.MANIFEST_DOC).decode("utf-8"))
    except (OSError, ValueError):
        local_doc = None
    fires = [a for a in ((local_doc or template_doc).get("actions") or [])
             if isinstance(a, dict) and a.get("type") == "fire_task"]
    if fires:
        if not old.app_json_masked_sha:
            return True
        return local_doc is not None and template_sig.sha256_text(
            template_sig.canonical(template_sig.targets_masked(local_doc))) == old.app_json_masked_sha
    if local_doc is None:
        return False
    return template_sig.sha256_text(template_sig.canonical(local_doc)) == old.app_json_sha


def _plan_copy_triggers(run: _Run, item: AppItem, old: AppItem | None, owner_sub: str | None,
                        seen: set[str]) -> list[dict]:
    """Which of the new blueprint's triggers the re-import seeds for one
    copy (APPS.md "Blueprints and templates"): one with a row is passed
    (the seed re-points a stale handler and leaves the rest); one the
    installed version knew and the member deleted is left out — removed
    locally; one new to the record is added. Reported once per trigger,
    not per copy."""
    from storage.automation import trigger_store
    old_slugs = {str(t.get("slug") or "") for t in (old.blueprint.get("triggers") or [])} if old else set()
    out: list[dict] = []
    for t in item.blueprint.get("triggers") or []:
        if not isinstance(t, dict):
            continue
        slug = str(t.get("slug") or "")
        what = f"the trigger '{slug}' of the app '{item.slug}'"
        existing = trigger_store.find_template_trigger(
            run.agent, f"{item.slug}__{slug}", owner_sub if item.visibility == "user" else None)
        if existing is not None:
            out.append(t)
            continue
        if slug in old_slugs:
            if what not in seen:
                seen.add(what)
                run.removed(what)
            continue
        out.append(t)
        if what not in seen:
            seen.add(what)
            run.added(what)
    return out


def _pause_stale_triggers(run: _Run, item: AppItem, app_id: str, doc: dict) -> None:
    """After a copy took the new version: a seeded trigger whose handler
    the new manifest no longer declares is paused with the reason (an
    update never deletes); the report's notes say so."""
    from api.apps import manifest as _mf
    from storage.automation import trigger_store
    names = set(_mf.parse_handlers({"handlers": json.dumps(doc.get("handlers") or {})}).get("on_trigger") or [])
    for t in trigger_store.list_triggers_for_app(app_id):
        if not t.get("community_template") or not t.get("enabled") or (t.get("handler") or "") in names:
            continue
        trigger_store.set_trigger_enabled(t["id"], False)
        trigger_store.set_last_error(t["id"], f"the app's new version has no handler '{t.get('handler')}'")
        # Named as the blueprint names it (the report's other lines do), not
        # by the row's ``<app>-<slug>``.
        bslug = str(t.get("community_template_item_slug") or "").split("__", 1)[-1] or str(t.get("slug") or "")
        note = f"the trigger '{bslug}' of the app '{item.slug}' is paused: the new version has no handler '{t.get('handler')}'"
        notes = run.report.setdefault("notes", [])
        if note not in notes:
            notes.append(note)


async def _layer_apps(run: _Run, new_data: dict) -> None:
    """Shared apps and every member's copy: an unchanged tree takes a new
    release through the cold path (approved when the presser's consent
    covers it, else pending on the card) with the blueprint's triggers
    seeded at it (``_plan_copy_triggers``); an edited one keeps its release
    and gets the new tree beside it; a member without a copy gets one."""
    from services.apps import app_blueprints
    from services.community import template_app_seeder as seeder
    from storage import database as task_store
    old_apps = {a.slug: a for a in (run.old.apps if run.old else [])}
    members = _members(run.agent)
    trig_seen: set[str] = set()
    for item in run.new.apps:
        assert item.dir is not None
        doc = json.loads((item.dir / template_sig.MANIFEST_DOC).read_text(encoding="utf-8"))
        ref = seeder.template_ref(run.new.slug, item.slug)
        folder_rel = "apps" if item.visibility != "user" else "user-apps"
        if item.visibility == "user" and run.apply:
            await asyncio.to_thread(seeder.write_seed_source, run.agent, item)
        copies: list[tuple[str, str | None]] = []
        if item.visibility != "user":
            copies.append(("", None))
        else:
            rows = await asyncio.to_thread(task_store.list_rows_by_template_ref, run.agent, ref)
            seen = set()
            for r in rows:
                if r.get("template_state") in ("opted_out", "removed"):
                    continue
                copies.append((r.get("username") or "", r.get("owner_sub")))
                seen.add(r.get("owner_sub"))
            for m in members:
                if m["sub"] in seen or item not in seeder.user_items(run.new, m["agent_role"]):
                    continue
                who = await asyncio.to_thread(task_store.get_username_by_sub, m["sub"]) or m["sub"]
                if run.apply:
                    st, _detail = await seeder.seed_user_copy(run.agent, m["sub"], item.slug, data=new_data)
                    if st == "pending":
                        run.report["pending_apps"].append(f"{item.slug} ({who})")
                    if st not in ("seeded", "pending"):
                        continue
                run.added(f"the app '{item.slug}' for {who}")
        for username, owner_sub in copies:
            what = f"the app '{item.slug}'" + (f" of {username}" if username else "")
            folder = (run.agent_dir / layout.scope_workspace(username)
                      / "apps" / item.slug)
            row = await asyncio.to_thread(task_store.get_app_by_slug, run.agent, username, item.slug)
            if row is None and not folder.is_dir():
                if item.visibility != "user":
                    if item.slug in old_apps:
                        run.removed(what)
                        continue
                    if run.apply:
                        out = await seeder.seed_shared(run.agent, dataclasses.replace(run.new, apps=[item]), new_data)
                        run.report["pending_apps"].extend(p["slug"] for p in out.get("pending", []))
                    run.added(what)
                continue
            if row is not None and not row.get("template_ref"):
                run.report["kept"].append({"what": what, "reason": "own app", "path": "", "new_path": ""})
                continue
            if row is not None and row.get("hidden"):
                # The dashboard's X parked it; a release would unhide it.
                run.report["kept"].append({"what": what, "reason": "hidden by its owner", "path": "", "new_path": ""})
                continue
            prev_item = old_apps.get(item.slug)
            if (prev_item is None or prev_item.sig != item.sig) \
                    and await asyncio.to_thread(_tree_unchanged, folder, item, doc):
                # A retry after an update that failed later on: this copy
                # took the new version already (the record still names the
                # old one), so it is neither edited nor to be taken again.
                run.same()
                continue
            if await asyncio.to_thread(_tree_unchanged, folder, old_apps.get(item.slug), doc):
                prev = old_apps.get(item.slug)
                if prev is not None and prev.sig == item.sig:
                    run.same()
                    continue
                trig_items = await asyncio.to_thread(
                    _plan_copy_triggers, run, item, prev, owner_sub, trig_seen)
                if run.apply:
                    by, why = seeder.admits(item, seeder.consent_entry(new_data, "apps", item.slug),
                                            run.agent, username, owner_sub)
                    try:
                        res = await app_blueprints.import_folder(
                            run.agent, username, owner_sub, item.slug, item.dir,
                            template_slug=run.new.slug, tasks=list(item.blueprint.get("tasks") or []),
                            cold=True, approve_as=by, template_ref=ref, triggers=trig_items)
                    except Exception as e:
                        run.report["kept"].append({"what": what, "reason": f"the new version failed to deploy: {e}",
                                                   "path": "", "new_path": ""})
                        continue
                    if res.get("status") == app_deploy.RESULT_REFUSED:
                        problems = "; ".join(str(p.get("message") or p) if isinstance(p, dict) else str(p)
                                             for p in (res.get("problems") or [])[:3])
                        run.report["kept"].append({"what": what, "path": "", "new_path": "",
                                                   "reason": "the new version was refused: " + (problems or "see the app's card")})
                        continue
                    if res.get("status") == app_deploy.RESULT_PENDING_APPROVAL:
                        run.report["pending_apps"].append(item.slug + (f" ({username})" if username else ""))
                    if res.get("app_id"):
                        await asyncio.to_thread(_pause_stale_triggers, run, item, str(res["app_id"]), doc)
                run.replaced(what)
            else:
                rel = f"{layout.scope_workspace(username)}/apps/{item.slug}"
                run.kept(what, "edited locally" if old_apps.get(item.slug) else "no record of the installed version",
                         rel, f"{folder_rel}/{item.slug}", new_tree=item.dir)
                # "Take the new version" replaces the presser's own copy:
                # another member's kept copy is listed without it.
                if username and owner_sub != run.by_sub:
                    run.report["kept"][-1]["new_path"] = ""


def _check_hashes(doc: dict, script_sha: str, *, mandatory: bool | None = None) -> tuple[str, str]:
    from services.checks import documents
    d = dict(doc)
    if mandatory is not None:
        d["mandatory"] = mandatory
    clean = documents.validate_check_doc(d, owner="")
    return documents.sha256_text(documents.canonical_json(clean)), script_sha or ""


def _layer_checks(run: _Run, new_data: dict) -> None:
    from services.checks import documents
    from services.community import template_app_seeder as seeder
    old_checks = {c.name: c for c in (run.old.checks if run.old else [])}
    for item in run.new.checks:
        what = f"the check '{item.name}'"
        entry = seeder.consent_entry(new_data, "checks", item.name)
        consented = bool(entry and entry.get("sig") == item.sig)
        new_doc = dict(item.doc)
        if not consented:
            new_doc["mandatory"] = False
        local = documents.load_check(run.agent, "", item.name)
        if local is None:
            if item.name in old_checks:
                run.removed(what)
                continue
            if run.apply:
                try:
                    documents.write_check(run.agent, "", new_doc, item.script,
                                          updated_by=f"template:{run.new.slug}@{run.new.version}")
                except documents.CheckError as e:
                    run.report["kept"].append({"what": what, "reason": f"refused: {e}", "path": "", "new_path": ""})
                    continue
            run.added(what)
            if not consented:
                run.report["offered_checks"].append(item.name)
            continue
        have = (local.doc_sha256, local.script_sha256 or "")
        try:
            target = _check_hashes(new_doc, item.script_sha256)
        except documents.CheckError:
            target = ("", "")
        if have == target:
            run.same()
            continue
        # The installed version's check as the install wrote it (mandatory
        # when its consent covered it, else offered), and the same document
        # with only ``mandatory`` flipped — a manager's choice on the Checks
        # page, which the new version keeps.
        prev = old_checks.get(item.name)
        installed: tuple[str, str] | None = None
        flipped: tuple[str, str] | None = None
        if prev is not None:
            old_entry = seeder.consent_entry(run.old_data, "checks", item.name)
            was_mandatory = bool(prev.mandatory and old_entry and old_entry.get("sig") == prev.sig)
            if prev.doc:
                with contextlib.suppress(documents.CheckError):
                    installed = _check_hashes(prev.doc, prev.script_sha256, mandatory=was_mandatory)
                    flipped = _check_hashes(prev.doc, prev.script_sha256, mandatory=not was_mandatory)
            else:
                installed = (prev.doc_sha256, prev.script_sha256 or "")
        if have == installed or (flipped is not None and flipped != installed and have == flipped):
            if have != installed:
                new_doc["mandatory"] = local.mandatory
            if run.apply:
                try:
                    documents.write_check(run.agent, "", new_doc, item.script,
                                          updated_by=f"template:{run.new.slug}@{run.new.version}")
                except documents.CheckError as e:
                    run.report["kept"].append({"what": what, "reason": f"refused: {e}", "path": "", "new_path": ""})
                    continue
            run.replaced(what)
            if not new_doc.get("mandatory") and not consented:
                run.report["offered_checks"].append(item.name)
        else:
            text = documents.canonical_json(documents.validate_check_doc(new_doc, owner=""))
            run.kept(what, "edited locally" if prev else "no record of the installed version",
                     f"config/checks/{item.name}/check.json", f"checks/{item.name}/check.json", new_text=text)
            if item.script is not None and item.script_name and run.apply:
                from services.community.template_app_seeder import guard
                p = guard(run.agent_dir, run.beside / "checks" / item.name / item.script_name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(item.script, encoding="utf-8")


async def _layer_mcps(run: _Run) -> None:
    """MCPs and skill packages the new version adds cascade like an install;
    one already enabled for the agent, or with an open request for it, is
    left alone, and one the installed version required that a manager has
    since disabled stays disabled (removed locally). The platform's CORE
    MCPs are re-checked too: one the platform gained since the record was
    written (``core_mcps`` in the record; a record without the list is
    from before this rule, so every core MCP not enabled counts as new) is
    enabled the way a fresh install would — the agent's own migration to a
    new core tool, run only when a manager updates it."""
    from services.community import community_agent_installer as inst
    from services.mcp import mcp_registry
    from storage.mcp import mcp_request_store, mcp_store
    enabled = set(await asyncio.to_thread(mcp_store.get_manager_enabled_mcps, run.agent))
    open_pairs = await asyncio.to_thread(mcp_request_store.open_requests_by_pair)
    old_names = {m.name for m in run.old.mcps} if run.old else set()
    old_packages = {p.name for p in run.old.skill_packages} if run.old else set()
    wanted, removed = [], []
    for m in run.new.mcps:
        if m.name in enabled or (m.name, run.agent) in open_pairs:
            continue
        if m.name in old_names:
            removed.append(m.name)
            continue
        wanted.append(m)
    packages = [p for p in run.new.skill_packages if p.name not in old_packages]
    core: list[str] = []
    if run.new.core_mcps != "none":
        recorded = run.old_data.get("core_mcps")
        known = set(recorded) if isinstance(recorded, list) else None
        core = [n for n in mcp_registry.core_mcp_names()
                if n not in enabled and (known is None or n not in known)]
    run.report["mcps"] = {"ready": [], "requested": [], "new": [m.name for m in wanted], "removed": removed,
                          "core": core}
    for name in removed:
        run.removed(f"the MCP '{name}'")
    for name in core:
        run.added(f"the core MCP '{name}'")
    if run.apply and core:
        try:
            await asyncio.to_thread(mcp_registry.assign_core_mcps, run.agent, core)
        except Exception:
            logger.exception("update %s: core MCPs %s not enabled", run.agent, core)
    if not wanted and not packages:
        return
    if not run.apply:
        return
    batch_id = str(uuid.uuid4())
    created_requests: list = []
    if wanted:
        cascade = await inst._cascade_required_mcps(
            template=dataclasses.replace(run.new, mcps=wanted), target_slug=run.agent,
            installer_user_sub=run.by_sub, installer_role=run.by_role, batch_id=batch_id)
        run.report["mcps"]["ready"] = cascade["ready_mcps"]
        run.report["mcps"]["requested"] = [r.get("mcp_name", "") if isinstance(r, dict) else str(r)
                                           for r in cascade["created_requests"]]
        created_requests = cascade["created_requests"]
    if packages:
        with contextlib.suppress(Exception):
            await inst._cascade_skill_packages(
                dataclasses.replace(run.new, skill_packages=packages), run.agent,
                installer_user_sub=run.by_sub, installer_role=run.by_role, batch_id=batch_id)
    if created_requests:
        from services.community import community_installer
        with contextlib.suppress(Exception):
            await community_installer.notify_batch_created(
                batch_id=batch_id, requester_sub=run.by_sub, target_agent_slug=run.agent,
                template_slug=run.new.slug)


def _layer_default_for_new_users(run: _Run) -> None:
    block = run.new.default_for_new_users
    if not block.get("enabled"):
        return
    # The installed version asked for it already: an empty role now is an
    # admin's choice (or a creator's install that could not set it), never
    # a gap for the update to fill.
    if run.old is not None and run.old.default_for_new_users.get("enabled"):
        return
    from services.agents import shared_only_members
    row = shared_only_members.agent_row(run.agent) or {}
    if row.get("default_for_new_users_role"):
        return
    if not roles.is_admin(run.by_role) or (
            not roles.allowed_on_shared_only(block["role"])
            and shared_only_members.is_shared_only_row(row)):
        # A creator's update cannot set it; on a Shared-only agent a
        # default below the editor tier would attach people who open no chat.
        run.report["ignored_fields"].append("default_for_new_users")
        return
    if run.apply:
        agent_store.set_default_for_new_users_role(run.agent, block["role"])
    run.added("auto-attach for new users")


# ── plan and apply ──────────────────────────────────────────────────────────


def _load_old(data: dict | None) -> CommunityAgentTemplate | None:
    if not data:
        return None
    try:
        return load_template_from_dict(data)
    except TemplateValidationError:
        return None


async def _run_layers(run: _Run, new_data: dict) -> dict:
    await asyncio.to_thread(_layer_persona, run)
    await asyncio.to_thread(_layer_description, run)
    await asyncio.to_thread(_layer_context, run)
    await asyncio.to_thread(_layer_setup, run)
    await asyncio.to_thread(_layer_user_setup, run)
    await asyncio.to_thread(_layer_items, run)
    await asyncio.to_thread(_layer_dashboards, run)
    await _layer_apps(run, new_data)
    await asyncio.to_thread(_layer_checks, run, new_data)
    await _layer_mcps(run)
    await asyncio.to_thread(_layer_default_for_new_users, run)
    return run.report


async def plan(agent_slug: str, by_sub: str, by_role: str) -> dict:
    """What an update would do, without writing: the report per piece plus
    the consent lines of the new version's apps and checks."""
    row = await asyncio.to_thread(agent_store.get_agent, agent_slug)
    if not row:
        raise HTTPException(404, "no such agent")
    template, extracted, _entry = await _fetch_new(row)
    try:
        old_data = await asyncio.to_thread(agent_store.get_community_template_data, agent_slug) or {}
        run = _Run(agent=agent_slug, agent_dir=config.get_agent_dir(agent_slug), new=template,
                   old=_load_old(old_data), old_data=old_data, baseline=old_data.get("baseline") or {},
                   apply=False, by_sub=by_sub, by_role=by_role, loop=asyncio.get_running_loop())
        new_data = template_to_persistable_dict(template, agent_slug=agent_slug)
        new_data["consent"] = old_data.get("consent") or {"apps": {}, "checks": {}}
        report = await _run_layers(run, new_data)
        return {
            "agent_slug": agent_slug, "from_version": str(row.get("community_template_version") or ""),
            "to_version": template.version, "template_slug": template.slug,
            "display_name": template.display_name, **report, **consent_lines(template, run.old),
            "consent_scope": "everyone" if roles.is_admin(by_role) else "own",
            "has_baseline": bool(run.baseline),
        }
    finally:
        shutil.rmtree(extracted.parent, ignore_errors=True)


async def apply(agent_slug: str, by_sub: str, by_role: str, *, from_version: str,
                app_consent: dict[str, str] | None, check_consent: dict[str, str] | None) -> dict:
    """Start the update as a job: ``{job_id}``; 409 when the agent is not
    at ``from_version`` any more or an update is already running."""
    row = await asyncio.to_thread(agent_store.get_agent, agent_slug)
    if not row:
        raise HTTPException(404, "no such agent")
    if str(row.get("community_template_version") or "") != from_version:
        raise HTTPException(409, f"already at {row.get('community_template_version') or 'another version'}")
    lock = _agent_locks.setdefault(agent_slug, asyncio.Lock())
    if lock.locked() or (_jobs.get(agent_slug) or {}).get("status") in ("starting", "running"):
        raise HTTPException(409, "an update of this agent is already running")
    # The slot is taken before the fetch: a second press during the download
    # is refused, not queued behind the first.
    job_id = uuid.uuid4().hex[:12]
    _jobs[agent_slug] = {"id": job_id, "status": "starting", "started_at": datetime.now(timezone.utc).isoformat(),
                         "by": by_sub, "to_version": "", "report": None, "error": ""}
    try:
        template, extracted, _entry = await _fetch_new(row)
    except BaseException:
        _jobs.pop(agent_slug, None)
        raise
    _jobs[agent_slug].update(status="running", to_version=template.version)

    async def _job() -> None:
        async with lock:
            try:
                now_row = await asyncio.to_thread(agent_store.get_agent, agent_slug) or {}
                if str(now_row.get("community_template_version") or "") != from_version:
                    raise RuntimeError(f"the agent moved to {now_row.get('community_template_version') or 'another version'} "
                                       "before this update ran")
                report = await _apply_now(agent_slug, template, by_sub, by_role,
                                          app_consent=app_consent, check_consent=check_consent)
                _jobs[agent_slug].update(status="done", report=report,
                                         finished_at=datetime.now(timezone.utc).isoformat())
                await _notify_report(agent_slug, by_sub, report)
            except Exception as e:
                logger.exception("template update of %s failed", agent_slug)
                _jobs[agent_slug].update(status="failed", error=str(e),
                                         finished_at=datetime.now(timezone.utc).isoformat())
                await _notify_failed(agent_slug, by_sub, str(e))
            finally:
                shutil.rmtree(extracted.parent, ignore_errors=True)

    _jobs[agent_slug]["task"] = asyncio.create_task(_job())
    return {"job_id": job_id, "to_version": template.version}


async def _apply_now(agent_slug: str, template: CommunityAgentTemplate, by_sub: str, by_role: str, *,
                     app_consent: dict[str, str] | None, check_consent: dict[str, str] | None) -> dict:
    from services.community import template_app_seeder as seeder
    old_data = await asyncio.to_thread(agent_store.get_community_template_data, agent_slug) or {}
    run = _Run(agent=agent_slug, agent_dir=config.get_agent_dir(agent_slug), new=template,
               old=_load_old(old_data), old_data=old_data, baseline=old_data.get("baseline") or {},
               apply=True, by_sub=by_sub, by_role=by_role, loop=asyncio.get_running_loop())
    # The new record, with the presser's consent for the new version's
    # apps and checks over the old consent (an unchanged app keeps its
    # entry: the same signature, the same person).
    new_data = template_to_persistable_dict(template, agent_slug=agent_slug)
    from services.community import community_agent_installer as inst
    inst.stamp_core_mcps(new_data, template)
    seeder.record_consent(new_data, template, app_consent, check_consent, by_sub)
    old_consent = old_data.get("consent") or {}
    for kind in ("apps", "checks"):
        for key, entry in (old_consent.get(kind) or {}).items():
            new_data["consent"][kind].setdefault(key, entry)
    report = await _run_layers(run, new_data)
    # The version moves LAST: what was applied above stands on its own.
    await asyncio.to_thread(agent_store.set_community_template_data, agent_slug, new_data)
    await asyncio.to_thread(agent_store.set_community_template_version, agent_slug, template.version)
    report["from_version"] = str(old_data.get("version") or "")
    report["to_version"] = template.version
    report["agent_slug"] = agent_slug
    return report


async def take(agent_slug: str, new_path: str, by_sub: str, by_role: str) -> dict:
    """"Take the new version" for a piece the update kept: the copy stored
    beside it replaces the live piece through the same writers."""
    from services.apps import app_blueprints
    from services.checks import documents
    from services.community import template_app_seeder as seeder
    from storage import database as task_store
    agent_dir = config.get_agent_dir(agent_slug)
    try:
        parts = normalize_rel_path(new_path).split("/") if not new_path.startswith("/") else []
    except PathOutsideRoot:
        parts = []
    if len(parts) < 4 or parts[0] != layout.CONFIG or parts[1] != BESIDE_DIR:
        raise HTTPException(400, "not a stored new version")
    version = parts[2]
    rel = Path(*parts[3:])
    src = agent_dir / layout.CONFIG / BESIDE_DIR / version / rel
    if not seeder.unlinked(src, agent_dir):
        raise HTTPException(400, "not a stored new version")
    data = await asyncio.to_thread(agent_store.get_community_template_data, agent_slug) or {}
    template = _load_old(data)
    run = _Run(agent=agent_slug, agent_dir=agent_dir, new=template, old=None, old_data=data,
               baseline={}, apply=True, by_sub=by_sub, by_role=by_role,
               loop=asyncio.get_running_loop()) if template else None
    head = rel.parts[0]
    if head in ("apps", "user-apps") and len(rel.parts) >= 2:
        if not src.is_dir():
            raise HTTPException(404, "the stored copy is gone")
        slug = rel.parts[1]
        username = "" if head == "apps" else (
            await asyncio.to_thread(task_store.get_username_by_sub, by_sub) or "")
        if head == "user-apps" and not username:
            raise HTTPException(400, "you have no personal copy to replace")
        item = next((a for a in (template.apps if template else []) if a.slug == slug), None)
        by, _why = seeder.admits(item, seeder.consent_entry(data, "apps", slug), agent_slug, username, by_sub) \
            if item else ("", "")
        if by and not await asyncio.to_thread(seeder.copy_matches, item, src):
            by = ""
        res = await app_blueprints.import_folder(
            agent_slug, username, by_sub if username else None, slug, src,
            template_slug=str(data.get("slug") or ""), tasks=seeder._blueprint_tasks(src), cold=True,
            approve_as=by, template_ref=seeder.template_ref(str(data.get("slug") or ""), slug),
            triggers=seeder._blueprint_triggers(src))
        return {"status": res.get("status"), "path": res.get("path")}
    if head == "checks" and len(rel.parts) >= 3:
        folder = src.parent
        try:
            doc = json.loads(seeder.guard(agent_dir, folder / "check.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "the stored copy is gone")
        script_name = (doc.get("script") or {}).get("run") if isinstance(doc.get("script"), dict) else None
        if script_name and not seeder.unlinked(folder / str(script_name), agent_dir):
            raise HTTPException(400, "not a stored new version")
        script = _read(folder / script_name) if script_name else None
        try:
            await asyncio.to_thread(documents.write_check, agent_slug, "", doc, script,
                                    updated_by=f"template:{data.get('slug')}@{version}")
        except documents.CheckError as e:
            raise HTTPException(400, str(e))
        return {"status": "replaced", "path": f"config/checks/{rel.parts[1]}/check.json"}
    text = _read(src)
    if text is None:
        raise HTTPException(404, "the stored copy is gone")
    if str(rel) == "agent.md":
        dest, commit = agent_dir / layout.CONFIG / "agent.md", True
    elif str(rel) == "user-setup.md":
        dest, commit = agent_dir / layout.CONFIG / "user-setup.md", False
    elif str(rel) == "setup.md":
        dest, commit = agent_dir / layout.CONFIG / layout.CONTEXT / "setup.md", False
    elif head == layout.CONTEXT and len(rel.parts) >= 2:
        dest, commit = agent_dir / layout.CONFIG / layout.CONTEXT / Path(*rel.parts[1:]), False
    elif head == "dashboards" and len(rel.parts) == 2:
        # A per-user dashboard's stored copy replaces the caller's own copy,
        # never the shared file.
        slug = rel.parts[1].removesuffix(".html")
        d = next((x for x in (template.dashboards if template else []) if x.slug == slug), None)
        if d is not None and d.visibility != "agent":
            username = await asyncio.to_thread(task_store.get_username_by_sub, by_sub) or ""
            dest = layout.user_dir(agent_dir, username) / layout.WORKSPACE / "apps" / rel.parts[1]
            if not username or not dest.is_file():
                raise HTTPException(400, "you have no personal copy to replace")
            commit = False
        else:
            dest, commit = agent_dir / layout.WORKSPACE / "apps" / rel.parts[1], False
    else:
        raise HTTPException(400, "not a piece an update keeps")
    if not seeder.unlinked(dest, agent_dir):
        raise HTTPException(400, f"{dest.relative_to(agent_dir).as_posix()} is reached through a link")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    if run is not None:
        _push(run, dest, commit=commit)
    return {"status": "replaced", "path": dest.relative_to(agent_dir).as_posix()}


# ── notifications ───────────────────────────────────────────────────────────


def _report_words(report: dict) -> str:
    bits = []
    if report.get("replaced"):
        bits.append(f"{len(report['replaced'])} piece(s) replaced")
    if report.get("added"):
        bits.append(f"{len(report['added'])} added")
    kept = [k for k in report.get("kept") or [] if k.get("reason") != REMOVED]
    removed = len(report.get("kept") or []) - len(kept)
    if kept:
        bits.append(f"{len(kept)} kept as you had them (the new version is stored beside each)")
    if removed:
        bits.append(f"{removed} left out (removed locally)")
    if report.get("pending_apps"):
        bits.append(f"{len(report['pending_apps'])} app release(s) wait for approval on their cards")
    if report.get("offered_checks"):
        bits.append(f"{len(report['offered_checks'])} check(s) offered, not mandatory")
    return "; ".join(bits) or "nothing needed changing"


async def _notify_report(agent_slug: str, by_sub: str, report: dict) -> None:
    from services.notifications import notification_manager
    with contextlib.suppress(Exception):
        await notification_manager.fire_notification(
            title=f"{notification_manager.agent_label(agent_slug)} updated to {report.get('to_version')}",
            body=_report_words(report) + ".", severity="success", scope="user", target=by_sub,
            source="community_agent", source_id=agent_slug, href=f"/agents/{agent_slug}/config")


async def _notify_failed(agent_slug: str, by_sub: str, error: str) -> None:
    from services.notifications import notification_manager
    with contextlib.suppress(Exception):
        await notification_manager.fire_notification(
            title=f"The update of {notification_manager.agent_label(agent_slug)} failed",
            body=f"{error[:300]}. What was applied stands; press Update again to resume.",
            severity="warning", scope="user", target=by_sub,
            source="community_agent", source_id=agent_slug, href=f"/agents/{agent_slug}/config")


async def maybe_notify_updates() -> int:
    """Once a day: the managers of every installed agent whose template has
    a newer catalog version hear about it, once per new version."""
    global _last_notify_sweep
    now = time.monotonic()
    if now - _last_notify_sweep < NOTIFY_SWEEP_S:
        return 0
    _last_notify_sweep = now
    from services.community import community_agents_catalog
    from services.notifications import notification_manager
    from storage.identity import db_users
    try:
        registry = await community_agents_catalog.fetch_registry()
    except Exception:
        return 0
    entries = {e.get("slug"): e for e in registry.get("agents", [])}
    told = 0
    for slug, info in (await installed_versions()).items():
        entry = entries.get(info["template"])
        row = {"community_template": info["template"], "community_template_version": info["version"]}
        found = detect(row, entry)
        if not found["update_available"]:
            continue
        lock = _agent_locks.get(slug)
        if lock is not None and lock.locked():
            continue                  # an update is writing the record now
        data = await asyncio.to_thread(agent_store.get_community_template_data, slug) or {}
        if not data or data.get("update_notified") == found["catalog_version"]:
            continue
        managers = [m["sub"] for m in await asyncio.to_thread(db_users.get_agent_users, slug)
                    if roles.can_manage(m.get("agent_role"))]
        for sub in managers:
            with contextlib.suppress(Exception):
                await notification_manager.fire_notification(
                    title=f"Version {found['catalog_version']} of {notification_manager.agent_label(slug)}'s "
                          f"template is available",
                    body=f"Installed: {found['installed_version'] or 'unknown'}. Open the agent's Config tab "
                         f"to see what the update changes and apply it.",
                    severity="info", scope="user", target=sub, source="community_agent",
                    source_id=slug, href=f"/agents/{slug}/config")
                told += 1
        # The mark lands on the record as it is now: an update that saved
        # a new record while the notices went out must not be undone.
        fresh = await asyncio.to_thread(agent_store.get_community_template_data, slug) or {}
        if fresh and fresh.get("version") == data.get("version"):
            fresh["update_notified"] = found["catalog_version"]
            await asyncio.to_thread(agent_store.set_community_template_data, slug, fresh)
    return told
