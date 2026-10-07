"""Community-agent template loader + validator.

Parses a directory laid out per the community-agents schema into a
``CommunityAgentTemplate`` dataclass. The directory is an extracted
tarball from ``OtoDock/community-agents`` (fetched by
``services.community.community_agents_catalog.fetch_and_extract_template``).

Schema reference: ``OtoDock/community-agents/CONTRIBUTING.md`` (the public
contributor guide — the authoritative spec for template authors).
This module ONLY parses + validates; it doesn't talk to the DB or
filesystem outside of the template dir.

New template kinds live in new folders so a released loader that does not
know them drops them whole (COMMUNITY-AGENTS-REGISTRY.md "Compatibility"):
``apps/<slug>/`` (one shared app each) and ``user-apps/<slug>/`` (one per
member) hold folder apps, ``checks/<name>/`` holds agent checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storage.agents import template_sig
from auth import roles
from core import layout

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

SLUG_REGEX = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
HEX_COLOR_REGEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
VALID_TASK_SCOPES = {"user", "agent"}
VALID_TRIGGER_SCOPES = {"user", "agent"}
VALID_NOTIFICATION_SCOPES = {"user", "agent"}
# Per-agent roles: the authority's set (``user_agents.agent_role`` CHECK).
VALID_DEFAULT_USER_ROLES = set(roles.AGENT_ROLES)


class TemplateValidationError(ValueError):
    """Raised when a template directory contains invalid or missing files."""


@dataclass
class TaskItem:
    slug: str
    description: str
    scope: str
    prompt: str
    schedule_kind: str           # 'cron' | 'interval' | 'run_at'
    cron: str | None
    interval_seconds: int | None
    run_at: str | None
    default_state: str           # 'paused' | 'active'
    auto_create_for_new_users: bool
    roles: list[str] | None      # None = all roles


@dataclass
class TriggerItem:
    slug: str
    description: str
    scope: str
    prompt: str
    default_state: str
    auto_create_for_new_users: bool
    roles: list[str] | None


@dataclass
class NotificationItem:
    slug: str
    title: str
    body: str
    deep_link: str | None
    scope: str
    schedule_kind: str           # 'cron' | 'interval' | 'run_at'
    cron: str | None
    interval_seconds: int | None
    run_at: str | None
    default_state: str
    auto_create_for_new_users: bool
    roles: list[str] | None


@dataclass
class DashboardItem:
    """One template-shipped app dashboard (``dashboards.json`` item +
    its HTML file under ``dashboards/``). HTML-only v1: no actions manifest
    — a seeded dashboard is display-only (zero approval surface); the agent
    can re-pin it with actions later. ``html`` carries the file content at
    load time; it is NOT persisted to the DB — the installer materializes a
    copy under ``config/community/dashboards/`` for late-joiner seeding."""
    slug: str
    title: str
    file: str                     # dashboards/<name>.html (template-relative)
    visibility: str               # "agent" (one shared pin) | "user" (per-user)
    auto_pin_for_new_users: bool  # user-visibility: pin for later joiners too
    html: str = ""


# Mirrors api/apps/manifest.APP_SLUG_RE (storage must not import the API
# layer): 1-40 chars of [a-z0-9-], starting alphanumeric.
_DASHBOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MAX_DASHBOARDS = 4
_MAX_APPS = 4
_MAX_CHECKS = 8
_MAX_DASHBOARD_BYTES = 1024 * 1024  # local-template snapshots cap files at 1MB
APP_FOLDERS = (("apps", "agent"), ("user-apps", "user"))
BASELINE_FORMAT = 1
# A blueprint's ``triggers`` (APPS.md "Blueprints and templates"): the
# trigger slug rule of ``trigger_manager._SLUG_RE``, and the seeded slug is
# ``<app slug>-<slug>`` — within the same 64 characters.
_TRIGGER_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_BLUEPRINT_TRIGGERS = 8


@dataclass
class AppItem:
    """One template-shipped folder app (APPS.md "Blueprints and templates"):
    ``apps/<slug>/`` is imported once as the agent's shared app,
    ``user-apps/<slug>/`` once per member. ``requires.mcps`` of its manifest
    join the template's MCP requirements so the installer's cascade assigns
    them. ``sig`` is what the install dialog consents to and the installer
    checks (``template_sig``); ``tree_sha`` and ``app_json_sha`` are the
    baseline an update compares a copy against; ``owner_approval`` says the
    manifest does things only the copy's owner may approve. ``dir`` is None
    for an item rehydrated from the database (the seed source is on disk).
    ``blueprint`` is ``blueprint.json`` as shipped: the tasks its buttons
    fire and, for a per-user app, ``auto_create_for_new_users`` and
    ``roles`` as the other per-user items have them. ``app_json_masked_sha``
    is ``app_json_sha`` with the buttons' task targets left out
    (``template_sig.targets_masked``), the baseline of a copy whose
    ``fire_task`` buttons the importer rewrote ("" in an older record)."""
    slug: str
    dir: Path | None
    requires_mcps: list[str] = field(default_factory=list)
    visibility: str = "agent"
    title: str = ""
    tree_sha: str = ""
    app_json_sha: str = ""
    sig: str = ""
    owner_approval: bool = False
    auto_create_for_new_users: bool = True
    roles: list[str] | None = None
    blueprint: dict = field(default_factory=dict)
    app_json_masked_sha: str = ""


@dataclass
class CheckItem:
    """One template-shipped agent check (``checks/<name>/check.json`` and,
    when the document names one, its script beside it; CHECKS.md). The
    document is validated at load with the checks' own validator, so a bad
    check fails the template before any state exists. Hashes are the
    checks' own (``doc_sha256`` over the canonical document, ``script_sha256``
    over the script), which the index table keeps too."""
    name: str
    doc: dict
    script_name: str | None
    script: str | None
    doc_sha256: str
    script_sha256: str
    mandatory: bool = False
    sig: str = ""


@dataclass
class McpRequirement:
    name: str
    min_version: str | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class SkillPackageRequirement:
    """One standalone community skill package the template needs
    (``skills.json`` → ``required[]``). ``skills`` selects ids within the
    package; empty = all of the package's skills default-on — mirroring
    ``McpRequirement.skills`` for MCP-bundled skills."""
    name: str
    skills: list[str] = field(default_factory=list)


@dataclass
class CommunityAgentTemplate:
    """Parsed + validated template directory."""

    slug: str
    display_name: str
    description: str
    color: str
    version: str
    # Manifests are engine-agnostic: they do NOT pin an AI engine or model.
    # The engine is chosen at install time from what's actually connected on the
    # platform + the installer's account (see
    # ``services/subscription_pool.default_execution_layer_for_creator``); the
    # model defaults to Auto.
    prompt_md: str
    readme_md: str
    mcps: list[McpRequirement]
    tasks: list[TaskItem]
    triggers: list[TriggerItem]
    notifications: list[NotificationItem]
    setup_md: str | None        # contents of setup.md, if present
    context_files: dict[str, str]  # {"context/path.md": content, ...} for copy-out
    source_dir: Path             # absolute path to the template root
    # v3: per-agent default scope for memory / tasks / notifications /
    # triggers / meetings. Reads from manifest's optional ``default_scope``
    # field; defaults to "user" if unset.
    # Per-user onboarding file (user-setup.md): seeded into each user's
    # context/ on attach; completed (deleted) per user via
    # complete_setup(scope="user"). May coexist with the agent-scope setup.md.
    user_setup_md: str | None = None
    # Standalone skill packages (skills.json) — installed from the skills
    # catalog and assigned to the agent at install time.
    skill_packages: list[SkillPackageRequirement] = field(default_factory=list)
    # Template-shipped app dashboards (dashboards.json + dashboards/).
    dashboards: list[DashboardItem] = field(default_factory=list)
    # Template-shipped folder apps (apps/<slug>/ shared, user-apps/<slug>/
    # per member) and agent checks (checks/<name>/).
    apps: list[AppItem] = field(default_factory=list)
    checks: list[CheckItem] = field(default_factory=list)
    default_scope: str = "user"
    # Visibility-modes: with ``default_scope`` selects the agent's mode
    # (Personal+shared / Shared+personal / Personal only / Shared only).
    # Manifest's optional ``collaborative`` field; defaults to True.
    collaborative: bool = True
    # When ``enabled=True`` the template's home-platform will
    # auto-attach every newly-created user to this agent with the given
    # ``role``. Empty dict = disabled (no auto-attach). Admins can override
    # the manifest's choice per-install via the agent's Setup tab; the
    # persisted column ``agents.default_for_new_users_role`` is the active
    # value at runtime.
    default_for_new_users: dict = field(default_factory=dict)
    # Core-MCP opt-out. "all" (default): the installer assigns the platform's
    # full core set on top of ``mcps.json`` — templates never LIST core MCPs,
    # so each platform fills in ITS OWN core set (a private install carries
    # core MCPs the public catalog couldn't even name). "none": the agent
    # gets ONLY what ``mcps.json`` lists (possibly nothing) — for
    # single-purpose agents like a phone caller persona. Platforms ≤1.4.0
    # ignore the field (their loader drops unknown keys), so a "none"
    # template installs there with the old behavior.
    core_mcps: str = "all"


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def load_template_from_dir(template_dir: Path) -> CommunityAgentTemplate:
    """Parse, validate, and return one template.

    Required files: ``agent.json``, the persona (``agent.md``, or its pre-1.4
    name ``prompt.md`` — ``agent.md`` wins when both exist), ``mcps.json``,
    ``README.md``. Optional: ``tasks.json``, ``triggers.json``,
    ``notifications.json``, ``skills.json``, ``setup.md``, ``user-setup.md``, ``context/``.

    Raises ``TemplateValidationError`` on any schema violation.
    """
    if not template_dir.is_dir():
        raise TemplateValidationError(f"Template dir not found: {template_dir}")

    agent_json = _load_required_json(template_dir / "agent.json")
    persona_path = template_dir / "agent.md"
    if not persona_path.is_file():
        persona_path = template_dir / "prompt.md"
    if not persona_path.is_file():
        raise TemplateValidationError(
            f"Missing required file: {template_dir / 'agent.md'} "
            "(or its pre-1.4 name prompt.md)"
        )
    prompt_md = _load_required_text(persona_path)
    mcps_json = _load_required_json(template_dir / "mcps.json")
    readme_md = _load_required_text(template_dir / "README.md")

    _validate_agent_json(agent_json)
    mcps = _parse_mcps_json(mcps_json)

    tasks_path = template_dir / "tasks.json"
    tasks = _parse_tasks_json(_load_optional_json(tasks_path)) if tasks_path.is_file() else []

    triggers_path = template_dir / "triggers.json"
    triggers = _parse_triggers_json(_load_optional_json(triggers_path)) if triggers_path.is_file() else []

    skills_path = template_dir / "skills.json"
    skill_packages = (
        _parse_skills_json(_load_optional_json(skills_path))
        if skills_path.is_file() else []
    )

    notifications_path = template_dir / "notifications.json"
    notifications = (
        _parse_notifications_json(_load_optional_json(notifications_path))
        if notifications_path.is_file()
        else []
    )

    setup_path = template_dir / "setup.md"
    setup_md = setup_path.read_text(encoding="utf-8") if setup_path.is_file() else None

    # Per-user onboarding: seeded into each user's context/ on attach and
    # completed (deleted) per user via complete_setup(scope="user"). May
    # coexist with the agent-scope setup.md (its presence IS the declaration
    # — no manifest flag).
    user_setup_path = template_dir / "user-setup.md"
    user_setup_md = (
        user_setup_path.read_text(encoding="utf-8")
        if user_setup_path.is_file() else None
    )

    context_dir = template_dir / "context"
    context_files: dict[str, str] = {}
    if context_dir.is_dir():
        for p in context_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in {".md", ".txt", ".markdown"}:
                rel = p.relative_to(template_dir).as_posix()
                context_files[rel] = p.read_text(encoding="utf-8")

    raw_default_scope = agent_json.get("default_scope", "user") or "user"
    if raw_default_scope not in ("user", "agent"):
        raise TemplateValidationError(
            f"default_scope must be 'user' or 'agent', got {raw_default_scope!r}"
        )

    dashboards_path = template_dir / "dashboards.json"
    dashboards = (
        _parse_dashboards_json(
            _load_optional_json(dashboards_path), template_dir,
            collaborative=bool(agent_json.get("collaborative", True)),
            default_scope=raw_default_scope,
        )
        if dashboards_path.is_file() else []
    )

    apps = _discover_apps(
        template_dir,
        collaborative=bool(agent_json.get("collaborative", True)),
        default_scope=raw_default_scope,
    )
    known = {m.name for m in mcps}
    for item in apps:
        for name in item.requires_mcps:
            if name not in known:
                mcps.append(McpRequirement(name=name))
                known.add(name)
    checks = _discover_checks(template_dir)

    raw_core_mcps = agent_json.get("core_mcps", "all") or "all"
    if raw_core_mcps not in ("all", "none"):
        raise TemplateValidationError(
            f"core_mcps must be 'all' or 'none', got {raw_core_mcps!r}"
        )

    default_for_new_users = _parse_default_for_new_users(
        agent_json.get("default_for_new_users")
    )

    return CommunityAgentTemplate(
        slug=agent_json["slug"],
        display_name=agent_json["display_name"],
        description=agent_json.get("description", ""),
        color=agent_json.get("color", "#6B7280"),
        version=agent_json["version"],
        prompt_md=prompt_md,
        readme_md=readme_md,
        mcps=mcps,
        tasks=tasks,
        triggers=triggers,
        notifications=notifications,
        setup_md=setup_md,
        user_setup_md=user_setup_md,
        skill_packages=skill_packages,
        dashboards=dashboards,
        apps=apps,
        checks=checks,
        context_files=context_files,
        source_dir=template_dir.resolve(),
        default_scope=raw_default_scope,
        collaborative=bool(agent_json.get("collaborative", True)),
        default_for_new_users=default_for_new_users,
        core_mcps=raw_core_mcps,
    )


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def substitute_template_vars(content: str, agent_slug: str) -> str:
    """Replace ``{agent_slug}`` literals in template content with the actual
    slug. Deliberately the one variable, so no other braces in markdown or
    code fences are touched. Applied to context files, both setup guides
    and dashboard HTML at install; never to the persona or an app folder.
    """
    return content.replace("{agent_slug}", agent_slug)


def template_to_persistable_dict(template: CommunityAgentTemplate, *,
                                 agent_slug: str = "") -> dict[str, Any]:
    """Serialize what the platform keeps of a template after the tarball is
    gone: ``agents.community_template_data``, written right after
    ``agent_store.create_agent``.

    Two readers. ``on_user_added_to_agent`` re-seeds a later member from
    ``slug`` / ``version``, the item lists, the dashboards' metadata, the
    per-user apps and ``default_for_new_users``. The template updater
    compares the installed pieces against ``baseline`` — SHA-256 of every
    text the install wrote, taken over the SUBSTITUTED content where
    substitution applies (which is why ``agent_slug`` is needed; without
    it the baseline is left out), and of every seeded item's projection
    (``baseline_format`` names the projection so a later platform can read
    an older record). The persona, the context files and the setup guides
    themselves live on disk; the apps' seed source under
    ``config/community/``; the checks in ``config/checks/``. ``consent`` is
    the installer's slot (COMMUNITY-AGENTS-REGISTRY.md "Consent").
    """
    out: dict[str, Any] = {
        "slug": template.slug,
        "version": template.version,
        # The description the install wrote to the agent row: an update
        # replaces the row's only while it still reads this.
        "description": template.description,
        "tasks": [_task_to_dict(t) for t in template.tasks],
        "triggers": [_trigger_to_dict(t) for t in template.triggers],
        "notifications": [_notification_to_dict(n) for n in template.notifications],
        # Dashboard METADATA only — the HTML lives on disk under the agent's
        # ``config/community/dashboards/`` (written at install), so a
        # late-joiner seed re-reads the file instead of bloating the JSONB.
        "dashboards": [_dashboard_to_dict(d) for d in template.dashboards],
        "default_for_new_users": dict(template.default_for_new_users),
        "baseline_format": BASELINE_FORMAT,
        "apps": [_app_to_dict(a) for a in template.apps if a.visibility == "agent"],
        "user_apps": [_app_to_dict(a) for a in template.apps if a.visibility == "user"],
        "checks": [_check_to_dict(c) for c in template.checks],
        "consent": {"apps": {}, "checks": {}},
        # The MCPs and skill packages the installed version required: an
        # update cascades only what the new version adds, so one a manager
        # disabled since stays disabled.
        "mcps": [{"name": m.name, "min_version": m.min_version, "skills": list(m.skills)}
                 for m in template.mcps],
        "skill_packages": [{"name": p.name, "skills": list(p.skills)} for p in template.skill_packages],
    }
    if agent_slug:
        out["baseline"] = template_baseline(template, agent_slug)
    return out


def template_baseline(template: CommunityAgentTemplate, agent_slug: str) -> dict[str, Any]:
    """The as-installed hashes an update compares against (D6 of the
    lane's plan; ``BASELINE_FORMAT``)."""
    sub = substitute_template_vars
    return {
        "persona": _sha(template.prompt_md),
        "context": {rel: _sha(sub(text, agent_slug))
                    for rel, text in sorted(template.context_files.items())},
        "setup": _sha(sub(template.setup_md, agent_slug)) if template.setup_md is not None else None,
        "user_setup": (_sha(sub(template.user_setup_md, agent_slug))
                       if template.user_setup_md is not None else None),
        "dashboards": {d.slug: _sha(sub(d.html or "", agent_slug)) for d in template.dashboards},
        "items": {
            **{f"task:{t.slug}": _sha(template_sig.canonical(item_projection("task", _task_to_dict(t))))
               for t in template.tasks},
            **{f"trigger:{t.slug}": _sha(template_sig.canonical(item_projection("trigger", _trigger_to_dict(t))))
               for t in template.triggers},
            **{f"notification:{n.slug}": _sha(template_sig.canonical(
                item_projection("notification", _notification_to_dict(n))))
               for n in template.notifications},
        },
    }


# The fields of a seeded item that are the TEMPLATE's (an update may replace
# them when they still match); what a person changes after the seed —
# enabled/paused, the roles, the per-user flag — is never part of it.
_ITEM_PROJECTIONS = {
    "task": ("prompt", "schedule_kind", "cron", "interval_seconds", "run_at", "description"),
    "trigger": ("prompt", "description"),
    "notification": ("title", "body", "deep_link", "schedule_kind", "cron", "interval_seconds", "run_at"),
}


def item_projection(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in _ITEM_PROJECTIONS[kind]}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_template_from_dict(data: dict[str, Any]) -> CommunityAgentTemplate:
    """Reconstruct a partial template from ``agents.community_template_data``.

    Rehydrates just the bits ``on_user_added_to_agent`` needs.
    Fields not persisted by ``template_to_persistable_dict`` come back as
    sensible empties (``""`` / ``[]`` / ``False``). This is intentional —
    those fields are only consumed during the initial install, never on
    the user-join path, so reconstructing them would just add noise.
    """
    if not isinstance(data, dict):
        raise TemplateValidationError(
            f"persisted template must be a dict, got {type(data).__name__}"
        )
    return CommunityAgentTemplate(
        slug=str(data.get("slug", "")),
        display_name="",
        description=str(data.get("description", "") or ""),
        color="",
        version=str(data.get("version", "")),
        prompt_md="",
        readme_md="",
        mcps=[McpRequirement(name=str(m.get("name", "")), min_version=m.get("min_version"),
                             skills=list(m.get("skills") or []))
              for m in data.get("mcps", []) if isinstance(m, dict) and m.get("name")],
        skill_packages=[SkillPackageRequirement(name=str(p.get("name", "")), skills=list(p.get("skills") or []))
                        for p in data.get("skill_packages", []) if isinstance(p, dict) and p.get("name")],
        tasks=[_task_from_dict(t) for t in data.get("tasks", [])],
        triggers=[_trigger_from_dict(t) for t in data.get("triggers", [])],
        notifications=[_notification_from_dict(n) for n in data.get("notifications", [])],
        dashboards=[_dashboard_from_dict(d) for d in data.get("dashboards", [])],
        apps=[_app_from_dict(a, "agent") for a in data.get("apps", [])]
        + [_app_from_dict(a, "user") for a in data.get("user_apps", [])],
        checks=[_check_from_dict(c) for c in data.get("checks", [])],
        setup_md=None,
        context_files={},
        source_dir=Path("/dev/null"),
        default_scope="user",
        default_for_new_users=dict(data.get("default_for_new_users") or {}),
    )


def _app_to_dict(a: AppItem) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slug": a.slug, "title": a.title, "tree_sha": a.tree_sha,
        "app_json_sha": a.app_json_sha, "app_json_masked_sha": a.app_json_masked_sha, "sig": a.sig,
    }
    if a.visibility == "user":
        out.update(owner_approval=a.owner_approval,
                   auto_create_for_new_users=a.auto_create_for_new_users, roles=a.roles)
    # The blueprint's triggers, so an update knows which a copy had (a
    # trigger the member deleted is never brought back — "removed locally").
    triggers = [{"slug": str(t.get("slug") or ""), "handler": str(t.get("handler") or "")}
                for t in (a.blueprint or {}).get("triggers") or [] if isinstance(t, dict)]
    if triggers:
        out["triggers"] = triggers
    return out


def _app_from_dict(a: dict[str, Any], visibility: str) -> AppItem:
    triggers = [t for t in (a.get("triggers") or []) if isinstance(t, dict)]
    return AppItem(
        slug=str(a.get("slug", "")), dir=None, visibility=visibility,
        title=str(a.get("title", "")), tree_sha=str(a.get("tree_sha", "")),
        app_json_sha=str(a.get("app_json_sha", "")), sig=str(a.get("sig", "")),
        app_json_masked_sha=str(a.get("app_json_masked_sha", "")),
        owner_approval=bool(a.get("owner_approval", False)),
        auto_create_for_new_users=bool(a.get("auto_create_for_new_users", True)),
        roles=a.get("roles"),
        blueprint={"triggers": triggers} if triggers else {},
    )


def _check_to_dict(c: CheckItem) -> dict[str, Any]:
    # The validated document rides along (a few KB at most): a template
    # update needs the installed version's "offered" variant, which only
    # the document gives.
    return {"name": c.name, "doc": dict(c.doc), "doc_sha256": c.doc_sha256,
            "script_sha256": c.script_sha256, "mandatory": c.mandatory, "sig": c.sig}


def _check_from_dict(c: dict[str, Any]) -> CheckItem:
    return CheckItem(
        name=str(c.get("name", "")), doc=dict(c.get("doc") or {}), script_name=None, script=None,
        doc_sha256=str(c.get("doc_sha256", "")), script_sha256=str(c.get("script_sha256", "")),
        mandatory=bool(c.get("mandatory", False)), sig=str(c.get("sig", "")),
    )


def _dashboard_to_dict(d: DashboardItem) -> dict[str, Any]:
    return {
        "slug": d.slug, "title": d.title, "file": d.file,
        "visibility": d.visibility,
        "auto_pin_for_new_users": d.auto_pin_for_new_users,
    }


def _dashboard_from_dict(d: dict[str, Any]) -> DashboardItem:
    return DashboardItem(
        slug=str(d.get("slug", "")),
        title=str(d.get("title", "")),
        file=str(d.get("file", "")),
        visibility=str(d.get("visibility", "agent")),
        auto_pin_for_new_users=bool(d.get("auto_pin_for_new_users", True)),
        html="",  # content re-read from config/community/dashboards/ on seed
    )


def available_visibilities(collaborative: bool, default_scope: str) -> set[str]:
    """The pin visibilities a MODE offers (VISIBILITY-MODES.md) — the
    template's at load, the installed agent's at every later seed."""
    if collaborative:
        return {"user", "agent"}
    return {"agent"} if default_scope == "agent" else {"user"}


def _discover_apps(template_dir: Path, *, collaborative: bool = True,
                   default_scope: str = "user") -> list[AppItem]:
    """``apps/<slug>/`` (shared) and ``user-apps/<slug>/`` (per member)
    directories holding an ``app.json``, at most four in all, one slug
    namespace, slugs by the pinned-apps rule, each kind only where the
    template's mode offers it. The manifest is read for ``requires.mcps``
    (the cascade), the hashes (the consent, the baseline) and the rules a
    per-user app must keep; the full validation is the importer's at
    install."""
    out: list[AppItem] = []
    seen: set[str] = set()
    available = available_visibilities(collaborative, default_scope)
    for folder, visibility in APP_FOLDERS:
        root = template_dir / folder
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
            if not (d / template_sig.MANIFEST_DOC).is_file():
                continue
            label = f"{folder}/{d.name}"
            if not _DASHBOARD_SLUG_RE.match(d.name):
                raise TemplateValidationError(
                    f"{label}: the folder name must be 1-40 chars of [a-z0-9-], starting alphanumeric")
            if d.name in seen:
                raise TemplateValidationError(
                    f"{label}: the slug is used by another app of this template")
            seen.add(d.name)
            if visibility not in available:
                raise TemplateValidationError(
                    f"{label}: a {'per-user' if visibility == 'user' else 'shared'} app is not "
                    f"offered by this template's mode (offers: {', '.join(sorted(available))})")
            out.append(_read_app_item(d, label, visibility))
            if len(out) > _MAX_APPS:
                raise TemplateValidationError(f"at most {_MAX_APPS} apps per template")
    return out


def _read_app_item(d: Path, label: str, visibility: str) -> AppItem:
    from services.apps import releases
    # Every file of the folder is read without following a link
    # (``template_sig.read_file``), the two documents and the hashed tree.
    try:
        doc = json.loads(template_sig.read_file(d, template_sig.MANIFEST_DOC).decode("utf-8"))
    except OSError:
        raise TemplateValidationError(f"{label}/app.json is not a regular file")
    except ValueError as e:
        raise TemplateValidationError(f"{label}/app.json is not valid JSON: {e}")
    if not isinstance(doc, dict):
        raise TemplateValidationError(f"{label}/app.json must be an object")
    blueprint: dict = {}
    try:
        blueprint = json.loads(template_sig.read_file(d, template_sig.BLUEPRINT_DOC).decode("utf-8"))
    except FileNotFoundError:
        pass
    except OSError:
        raise TemplateValidationError(f"{label}/blueprint.json is not a regular file")
    except ValueError as e:
        raise TemplateValidationError(f"{label}/blueprint.json is not valid JSON: {e}")
    if not isinstance(blueprint, dict):
        raise TemplateValidationError(f"{label}/blueprint.json must be an object")
    if visibility == "user":
        _check_user_app_rules(doc, label)
    _check_blueprint_triggers(doc, blueprint, label, d.name)
    req = doc.get("requires") or {}
    names = ([str(n) for n in (req.get("mcps") or []) if isinstance(n, str)]
             if isinstance(req, dict) else [])
    try:
        files = releases.walk_tree(d)
    except releases.ReleaseInvalid as e:
        raise TemplateValidationError(f"{label}: {e.reason}")
    if not any(rel == "client/index.html" for rel, _ in files):
        raise TemplateValidationError(f"{label}: client/index.html is missing — the page every viewer opens")
    try:
        tree = template_sig.tree_sha(d, [rel for rel, _ in files])
    except OSError:
        raise TemplateValidationError(f"{label}: a file changed while it was read; it must be a regular file")
    roles = blueprint.get("roles")
    if roles is not None and not isinstance(roles, list):
        raise TemplateValidationError(f"{label}/blueprint.json: roles must be a list or null")
    return AppItem(
        slug=d.name, dir=d.resolve(), requires_mcps=names, visibility=visibility,
        title=str(doc.get("title") or "").strip()[:80], tree_sha=tree,
        app_json_sha=template_sig.sha256_text(template_sig.canonical(doc)),
        app_json_masked_sha=template_sig.sha256_text(template_sig.canonical(template_sig.targets_masked(doc))),
        sig=template_sig.template_app_sig(doc, blueprint or None, tree),
        owner_approval=visibility == "user" and template_sig.needs_owner(doc),
        auto_create_for_new_users=bool(blueprint.get("auto_create_for_new_users", True)),
        roles=[str(r) for r in roles] if roles is not None else None,
        blueprint=blueprint,
    )


def _check_user_app_rules(doc: dict, label: str) -> None:
    """What a per-user app may not declare: a public route aims at ONE row
    (a trigger is fine since 1.7: each copy gets its own, owned by its
    member — ``_check_blueprint_triggers``), and a personal app's files
    stay under the owner's tree."""
    if doc.get("inbound"):
        raise TemplateValidationError(f"{label}: a per-user app cannot receive inbound hooks")
    files = doc.get("files")
    if isinstance(files, dict):
        for mode in ("read", "write"):
            for prefix in files.get(mode) or []:
                if str(prefix).startswith(layout.KNOWLEDGE):
                    raise TemplateValidationError(
                        f"{label}: a per-user app's files stay under the owner's tree, never knowledge/")


def _check_blueprint_triggers(doc: dict, blueprint: dict, label: str, app_slug: str) -> None:
    """``blueprint.json``'s ``triggers`` — ``[{slug, handler, description}]``,
    at most eight: the triggers the seed creates with each copy (one per
    member for a per-user app, the agent's for a shared one), each aimed
    at one of the manifest's ``handlers.on_trigger`` names. The seeded
    trigger's slug is ``<app slug>-<slug>``."""
    items = blueprint.get("triggers")
    if items is None:
        return
    if not isinstance(items, list):
        raise TemplateValidationError(f"{label}/blueprint.json: triggers must be a list")
    if len(items) > _MAX_BLUEPRINT_TRIGGERS:
        raise TemplateValidationError(
            f"{label}/blueprint.json: at most {_MAX_BLUEPRINT_TRIGGERS} triggers")
    handlers = doc.get("handlers")
    names = list((handlers or {}).get("on_trigger") or []) if isinstance(handlers, dict) else []
    seen: set[str] = set()
    for t in items:
        if not isinstance(t, dict):
            raise TemplateValidationError(f"{label}/blueprint.json: each trigger must be an object")
        slug = str(t.get("slug") or "")
        if not _TRIGGER_SLUG_RE.match(slug) or len(app_slug) + 1 + len(slug) > 64:
            raise TemplateValidationError(
                f"{label}/blueprint.json: trigger slug {slug!r} must be lowercase letters, digits and "
                f"dashes, and '{app_slug}-{slug}' at most 64 characters")
        if slug in seen:
            raise TemplateValidationError(f"{label}/blueprint.json: trigger {slug!r} is listed twice")
        seen.add(slug)
        handler = str(t.get("handler") or "")
        if handler not in names:
            raise TemplateValidationError(
                f"{label}/blueprint.json: trigger {slug!r} aims at {handler!r}, which is not one of the "
                f"manifest's handlers.on_trigger ({', '.join(names) or 'none'})")


def _discover_checks(template_dir: Path) -> list[CheckItem]:
    """``checks/<name>/check.json`` (+ the script it names), at most eight,
    validated with the checks' own validator; the name is the folder's."""
    from services.checks import documents
    root = template_dir / "checks"
    if not root.is_dir():
        return []
    out: list[CheckItem] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        doc_path = d / documents.DOC_FILE
        if not doc_path.is_file():
            continue
        label = f"checks/{d.name}"
        try:
            raw = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise TemplateValidationError(f"{label}/check.json is not valid JSON: {e}")
        try:
            clean = documents.validate_check_doc(raw, owner="")
        except documents.CheckError as e:
            raise TemplateValidationError(f"{label}: {e}")
        if clean["name"] != d.name:
            raise TemplateValidationError(
                f"{label}: the document's name must be the folder's ({clean['name']!r})")
        script_name = (clean.get("script") or {}).get("run")
        script: str | None = None
        script_sha = ""
        if script_name:
            script_path = d / script_name
            if script_path.is_symlink() or not script_path.is_file():
                raise TemplateValidationError(f"{label}: the script {script_name} is not in the check's folder")
            data = script_path.read_bytes()
            if len(data) > documents.MAX_SCRIPT_BYTES:
                raise TemplateValidationError(f"{label}: the script is larger than 256 KB")
            try:
                script = data.decode("utf-8")
            except UnicodeDecodeError:
                raise TemplateValidationError(f"{label}: the script is not UTF-8")
            script_sha = documents.sha256_text(data)
        out.append(CheckItem(
            name=clean["name"], doc=clean, script_name=script_name, script=script,
            doc_sha256=documents.sha256_text(documents.canonical_json(clean)),
            # The hash the agent's index will hold (the script as written),
            # so an update sees an untouched check as untouched; the
            # signature stays over the bytes as shipped.
            script_sha256=(documents.sha256_text(documents.script_as_written(script))
                           if script is not None else ""),
            mandatory=bool(clean.get("mandatory")),
            sig=template_sig.check_sig(raw, script_sha),
        ))
        if len(out) > _MAX_CHECKS:
            raise TemplateValidationError(f"at most {_MAX_CHECKS} checks per template")
    return out


def _parse_dashboards_json(
    data: Any, template_dir: Path, *, collaborative: bool, default_scope: str,
) -> list[DashboardItem]:
    """Validate ``dashboards.json`` and load each item's HTML file.

    Shape: ``{"dashboards": [{slug, title?, file, visibility?,
    auto_pin_for_new_users?}]}``. Files live under ``dashboards/`` inside the
    template. ``visibility`` must be one the template's MODE offers — an
    "agent" dashboard on a Personal-only template (or "user" on Shared-only)
    is a manifest error, not an install-time surprise.
    """
    if not isinstance(data, dict) or not isinstance(data.get("dashboards"), list):
        raise TemplateValidationError(
            'dashboards.json must be {"dashboards": [...]}'
        )
    items = data["dashboards"]
    if len(items) > _MAX_DASHBOARDS:
        raise TemplateValidationError(
            f"at most {_MAX_DASHBOARDS} dashboards per template"
        )
    if collaborative:
        available = {"user", "agent"}
    else:
        available = {"agent"} if default_scope == "agent" else {"user"}
    out: list[DashboardItem] = []
    seen: set[str] = set()
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise TemplateValidationError(f"dashboards[{i}] must be an object")
        slug = str(raw.get("slug", "")).strip().lower()
        if not _DASHBOARD_SLUG_RE.match(slug):
            raise TemplateValidationError(
                f"dashboards[{i}].slug must be 1-40 chars of [a-z0-9-], "
                "starting alphanumeric"
            )
        if slug in seen:
            raise TemplateValidationError(f"duplicate dashboard slug {slug!r}")
        seen.add(slug)
        vis = str(raw.get("visibility", "agent") or "agent")
        if vis not in ("user", "agent"):
            raise TemplateValidationError(
                f"dashboards[{i}].visibility must be 'user' or 'agent'"
            )
        if vis not in available:
            raise TemplateValidationError(
                f"dashboards[{i}]: visibility {vis!r} is not offered by this "
                f"template's mode (offers: {', '.join(sorted(available))})"
            )
        file_name = str(raw.get("file", "")).strip()
        file_path = (template_dir / "dashboards" / file_name)
        if (not file_name or "/" in file_name or "\\" in file_name
                or not file_name.endswith(".html")):
            raise TemplateValidationError(
                f"dashboards[{i}].file must be a bare *.html name under "
                "dashboards/"
            )
        if not file_path.is_file():
            raise TemplateValidationError(
                f"dashboards[{i}]: missing file dashboards/{file_name}"
            )
        html = file_path.read_text(encoding="utf-8")
        if len(html.encode("utf-8", errors="ignore")) > _MAX_DASHBOARD_BYTES:
            raise TemplateValidationError(
                f"dashboards/{file_name} exceeds the 1MB dashboard cap"
            )
        out.append(DashboardItem(
            slug=slug,
            title=str(raw.get("title", "")).strip() or slug,
            file=file_name,
            visibility=vis,
            auto_pin_for_new_users=bool(raw.get("auto_pin_for_new_users", True)),
            html=html,
        ))
    return out


def _task_to_dict(t: TaskItem) -> dict[str, Any]:
    return {
        "slug": t.slug, "description": t.description, "scope": t.scope,
        "prompt": t.prompt, "schedule_kind": t.schedule_kind,
        "cron": t.cron, "interval_seconds": t.interval_seconds, "run_at": t.run_at,
        "default_state": t.default_state,
        "auto_create_for_new_users": t.auto_create_for_new_users,
        "roles": t.roles,
    }


def _task_from_dict(raw: dict[str, Any]) -> TaskItem:
    return TaskItem(
        slug=str(raw["slug"]),
        description=str(raw.get("description", "")),
        scope=str(raw["scope"]),
        prompt=str(raw["prompt"]),
        schedule_kind=str(raw["schedule_kind"]),
        cron=raw.get("cron"),
        interval_seconds=raw.get("interval_seconds"),
        run_at=raw.get("run_at"),
        default_state=str(raw.get("default_state", "paused")),
        auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", True)),
        roles=raw.get("roles"),
    )


def _trigger_to_dict(t: TriggerItem) -> dict[str, Any]:
    return {
        "slug": t.slug, "description": t.description, "scope": t.scope,
        "prompt": t.prompt, "default_state": t.default_state,
        "auto_create_for_new_users": t.auto_create_for_new_users,
        "roles": t.roles,
    }


def _trigger_from_dict(raw: dict[str, Any]) -> TriggerItem:
    return TriggerItem(
        slug=str(raw["slug"]),
        description=str(raw.get("description", "")),
        scope=str(raw["scope"]),
        prompt=str(raw["prompt"]),
        default_state=str(raw.get("default_state", "paused")),
        auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", False)),
        roles=raw.get("roles"),
    )


def _notification_to_dict(n: NotificationItem) -> dict[str, Any]:
    return {
        "slug": n.slug, "title": n.title, "body": n.body,
        "deep_link": n.deep_link, "scope": n.scope,
        "schedule_kind": n.schedule_kind,
        "cron": n.cron, "interval_seconds": n.interval_seconds, "run_at": n.run_at,
        "default_state": n.default_state,
        "auto_create_for_new_users": n.auto_create_for_new_users,
        "roles": n.roles,
    }


def _notification_from_dict(raw: dict[str, Any]) -> NotificationItem:
    return NotificationItem(
        slug=str(raw["slug"]),
        title=str(raw["title"]),
        body=str(raw["body"]),
        deep_link=raw.get("deep_link"),
        scope=str(raw["scope"]),
        schedule_kind=str(raw["schedule_kind"]),
        cron=raw.get("cron"),
        interval_seconds=raw.get("interval_seconds"),
        run_at=raw.get("run_at"),
        default_state=str(raw.get("default_state", "active")),
        auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", True)),
        roles=raw.get("roles"),
    )


def _parse_default_for_new_users(raw: Any) -> dict[str, Any]:
    """Validate the optional ``default_for_new_users`` block on agent.json.

    Empty / missing → ``{}`` (feature disabled for this template).
    Present → must have ``enabled: bool`` and (if enabled=True) a valid
    ``role`` matching ``user_agents.agent_role`` enum.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TemplateValidationError(
            f"agent.json: default_for_new_users must be an object, "
            f"got {type(raw).__name__}"
        )
    enabled = bool(raw.get("enabled", False))
    if not enabled:
        return {}
    role = raw.get("role")
    if role not in VALID_DEFAULT_USER_ROLES:
        raise TemplateValidationError(
            f"agent.json: default_for_new_users.role must be one of "
            f"{sorted(VALID_DEFAULT_USER_ROLES)}, got {role!r}"
        )
    return {"enabled": True, "role": role}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _load_required_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TemplateValidationError(f"Required file missing: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateValidationError(f"Invalid JSON in {path.name}: {exc}")


def _load_required_text(path: Path) -> str:
    if not path.is_file():
        raise TemplateValidationError(f"Required file missing: {path.name}")
    return path.read_text(encoding="utf-8")


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateValidationError(f"Invalid JSON in {path.name}: {exc}")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_agent_json(data: dict[str, Any]) -> None:
    required = ("slug", "display_name", "version")
    for k in required:
        if not data.get(k):
            raise TemplateValidationError(f"agent.json: missing required field '{k}'")
    if not SLUG_REGEX.fullmatch(data["slug"]):
        raise TemplateValidationError(
            f"agent.json: invalid slug '{data['slug']}' "
            "(must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]$)"
        )
    color = data.get("color", "")
    if color and not HEX_COLOR_REGEX.fullmatch(color):
        raise TemplateValidationError(
            f"agent.json: invalid color '{color}' (must be #RRGGBB)"
        )


def _parse_mcps_json(data: dict[str, Any]) -> list[McpRequirement]:
    if not isinstance(data, dict):
        raise TemplateValidationError("mcps.json: top-level must be an object")
    required = data.get("required") or []
    if not isinstance(required, list):
        raise TemplateValidationError("mcps.json: 'required' must be a list")
    result: list[McpRequirement] = []
    for idx, raw in enumerate(required):
        if not isinstance(raw, dict):
            raise TemplateValidationError(
                f"mcps.json: required[{idx}] must be an object"
            )
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise TemplateValidationError(
                f"mcps.json: required[{idx}].name must be a non-empty string"
            )
        skills = raw.get("skills") or []
        if not isinstance(skills, list):
            raise TemplateValidationError(
                f"mcps.json: required[{idx}].skills must be a list"
            )
        result.append(McpRequirement(
            name=name,
            min_version=raw.get("min_version"),
            skills=[str(s) for s in skills],
        ))
    return result


def _parse_skills_json(data: dict[str, Any]) -> list[SkillPackageRequirement]:
    if not isinstance(data, dict):
        raise TemplateValidationError("skills.json: top-level must be an object")
    required = data.get("required") or []
    if not isinstance(required, list):
        raise TemplateValidationError("skills.json: 'required' must be a list")
    result: list[SkillPackageRequirement] = []
    for idx, raw in enumerate(required):
        if not isinstance(raw, dict):
            raise TemplateValidationError(
                f"skills.json: required[{idx}] must be an object"
            )
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise TemplateValidationError(
                f"skills.json: required[{idx}].name must be a non-empty string"
            )
        skills = raw.get("skills") or []
        if not isinstance(skills, list):
            raise TemplateValidationError(
                f"skills.json: required[{idx}].skills must be a list"
            )
        result.append(SkillPackageRequirement(
            name=name,
            skills=[str(s) for s in skills],
        ))
    return result


def _parse_schedule(raw: dict[str, Any], item_label: str) -> tuple[str, str | None, int | None, str | None]:
    """Returns (kind, cron, interval_seconds, run_at). Exactly one of the
    three value fields is non-None.

    ``raw`` must use the ``schedule: {type, ...}`` block where ``type`` is
    one of ``"cron"`` / ``"interval"`` / ``"run_at"``.
    """
    if "schedule" not in raw or not raw["schedule"]:
        raise TemplateValidationError(
            f"{item_label}: missing 'schedule' object"
        )
    sched = raw["schedule"]
    if not isinstance(sched, dict):
        raise TemplateValidationError(
            f"{item_label}: schedule must be an object"
        )
    kind = sched.get("type")
    if kind == "cron":
        cron = sched.get("cron")
        if not cron or not isinstance(cron, str):
            raise TemplateValidationError(
                f"{item_label}: schedule.cron must be a non-empty string"
            )
        _validate_cron(cron, item_label)
        return ("cron", cron, None, None)
    if kind == "interval":
        iv = sched.get("interval_seconds")
        if not isinstance(iv, int) or iv <= 0:
            raise TemplateValidationError(
                f"{item_label}: schedule.interval_seconds must be a positive integer"
            )
        return ("interval", None, iv, None)
    if kind == "run_at":
        ra = sched.get("run_at")
        if not ra or not isinstance(ra, str):
            raise TemplateValidationError(
                f"{item_label}: schedule.run_at must be an ISO datetime string"
            )
        return ("run_at", None, None, ra)
    raise TemplateValidationError(
        f"{item_label}: unknown schedule.type '{kind}' "
        "(must be 'cron', 'interval', or 'run_at')"
    )


_CRON_FIELD_RE = re.compile(r"^[\d*/,\-LW#?]+$")


def _validate_cron(cron: str, item_label: str) -> None:
    """Light-touch cron validation. APScheduler does the real parse at
    schedule time; this just rejects obviously-bad expressions early."""
    fields = cron.strip().split()
    if len(fields) not in (5, 6):
        raise TemplateValidationError(
            f"{item_label}: invalid cron '{cron}' (must have 5 or 6 fields)"
        )
    for f in fields:
        if not _CRON_FIELD_RE.fullmatch(f):
            raise TemplateValidationError(
                f"{item_label}: invalid cron field '{f}' in '{cron}'"
            )


def _parse_tasks_json(data: dict[str, Any]) -> list[TaskItem]:
    tasks_raw = data.get("tasks") or []
    if not isinstance(tasks_raw, list):
        raise TemplateValidationError("tasks.json: 'tasks' must be a list")
    out: list[TaskItem] = []
    for idx, raw in enumerate(tasks_raw):
        label = f"tasks.json[{idx}]"
        slug = raw.get("slug")
        if not slug or not isinstance(slug, str):
            raise TemplateValidationError(f"{label}: missing slug")
        if not SLUG_REGEX.fullmatch(slug):
            raise TemplateValidationError(f"{label}: invalid slug '{slug}'")
        scope = raw.get("scope")
        if scope not in VALID_TASK_SCOPES:
            raise TemplateValidationError(
                f"{label}: invalid scope '{scope}' (must be 'user' or 'agent')"
            )
        prompt = raw.get("prompt")
        if not prompt or not isinstance(prompt, str):
            raise TemplateValidationError(f"{label}: missing prompt")
        kind, cron, iv, ra = _parse_schedule(raw, label)
        default_state = raw.get("default_state", "paused")
        if default_state not in ("paused", "active"):
            raise TemplateValidationError(
                f"{label}: invalid default_state '{default_state}'"
            )
        roles = raw.get("roles")
        if roles is not None and not isinstance(roles, list):
            raise TemplateValidationError(
                f"{label}: roles must be a list or null"
            )
        out.append(TaskItem(
            slug=slug,
            description=str(raw.get("description", "")),
            scope=scope,
            prompt=prompt,
            schedule_kind=kind,
            cron=cron,
            interval_seconds=iv,
            run_at=ra,
            default_state=default_state,
            auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", True)),
            roles=roles,
        ))
    return out


def _parse_triggers_json(data: dict[str, Any]) -> list[TriggerItem]:
    raw_list = data.get("triggers") or []
    if not isinstance(raw_list, list):
        raise TemplateValidationError("triggers.json: 'triggers' must be a list")
    out: list[TriggerItem] = []
    for idx, raw in enumerate(raw_list):
        label = f"triggers.json[{idx}]"
        slug = raw.get("slug")
        if not slug or not isinstance(slug, str):
            raise TemplateValidationError(f"{label}: missing slug")
        if not SLUG_REGEX.fullmatch(slug):
            raise TemplateValidationError(f"{label}: invalid slug '{slug}'")
        scope = raw.get("scope")
        if scope not in VALID_TRIGGER_SCOPES:
            raise TemplateValidationError(
                f"{label}: invalid scope '{scope}'"
            )
        prompt = raw.get("prompt")
        if not prompt or not isinstance(prompt, str):
            raise TemplateValidationError(f"{label}: missing prompt")
        default_state = raw.get("default_state", "paused")
        if default_state not in ("paused", "active"):
            raise TemplateValidationError(
                f"{label}: invalid default_state '{default_state}'"
            )
        roles = raw.get("roles")
        if roles is not None and not isinstance(roles, list):
            raise TemplateValidationError(
                f"{label}: roles must be a list or null"
            )
        out.append(TriggerItem(
            slug=slug,
            description=str(raw.get("description", "")),
            scope=scope,
            prompt=prompt,
            default_state=default_state,
            auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", False)),
            roles=roles,
        ))
    return out


def _parse_notifications_json(data: dict[str, Any]) -> list[NotificationItem]:
    raw_list = data.get("notifications") or []
    if not isinstance(raw_list, list):
        raise TemplateValidationError("notifications.json: 'notifications' must be a list")
    out: list[NotificationItem] = []
    for idx, raw in enumerate(raw_list):
        label = f"notifications.json[{idx}]"
        slug = raw.get("slug")
        if not slug or not isinstance(slug, str):
            raise TemplateValidationError(f"{label}: missing slug")
        if not SLUG_REGEX.fullmatch(slug):
            raise TemplateValidationError(f"{label}: invalid slug '{slug}'")
        scope = raw.get("scope")
        if scope not in VALID_NOTIFICATION_SCOPES:
            raise TemplateValidationError(
                f"{label}: invalid scope '{scope}'"
            )
        title = raw.get("title")
        body = raw.get("body")
        if not title or not isinstance(title, str):
            raise TemplateValidationError(f"{label}: missing title")
        if not body or not isinstance(body, str):
            raise TemplateValidationError(f"{label}: missing body")
        if len(title) > 80:
            raise TemplateValidationError(
                f"{label}: title exceeds 80 chars"
            )
        if len(body) > 500:
            raise TemplateValidationError(
                f"{label}: body exceeds 500 chars"
            )
        kind, cron, iv, ra = _parse_schedule(raw, label)
        default_state = raw.get("default_state", "active")
        if default_state not in ("paused", "active"):
            raise TemplateValidationError(
                f"{label}: invalid default_state '{default_state}'"
            )
        roles = raw.get("roles")
        if roles is not None and not isinstance(roles, list):
            raise TemplateValidationError(
                f"{label}: roles must be a list or null"
            )
        out.append(NotificationItem(
            slug=slug,
            title=title,
            body=body,
            deep_link=raw.get("deep_link"),
            scope=scope,
            schedule_kind=kind,
            cron=cron,
            interval_seconds=iv,
            run_at=ra,
            default_state=default_state,
            auto_create_for_new_users=bool(raw.get("auto_create_for_new_users", True)),
            roles=roles,
        ))
    return out
