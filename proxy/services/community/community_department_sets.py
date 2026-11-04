"""Department sets: a catalog entry of its own that names agent templates
with their level, the delegation wiring, the head's library, the briefing
schedules and the bindings between the members' apps
(the registry's departments key, department_set_installer.py).

A set is one document, ``department.json`` in its own folder of the
agents catalog, listed under the registry's top-level ``departments`` key.
A platform before 1.9.1 reads the registry's ``agents`` list alone, so a
set never reaches its Browse page. This module reads a set (the schema
``mcps/department.schema.json`` through ``storage/catalog_schema.py``,
then the rules only code states) and answers the registry's questions;
what needs the database (which members are installed, how each is placed)
lives in ``department_set_installer``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.community import template_app_seeder, template_sig
from storage import catalog_schema
from storage.agents import db_departments

REGISTRY_KEY = "departments"
MANIFEST = "department.json"
DEFAULT_REACH = "adjacent"
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


class SetValidationError(ValueError):
    """A set the platform refuses. ``problems`` lists every finding; the
    text joins them."""

    def __init__(self, message: str | list[str]) -> None:
        self.problems = [message] if isinstance(message, str) else list(message)
        super().__init__("; ".join(self.problems))


@dataclass
class SetMember:
    template: str
    level: str


@dataclass
class SetLibrary:
    source: str
    subdir: str = ""
    writable: bool = False


@dataclass
class SetSchedule:
    member: str
    task: str


@dataclass
class SetBinding:
    from_member: str
    app: str
    to_member: str
    to_app: str


@dataclass
class DepartmentSet:
    slug: str
    name: str
    version: str
    levels: list[str]
    members: list[SetMember]
    description: str = ""
    color: str = ""
    mode: str = db_departments.DEFAULT_MODE
    reach: str = DEFAULT_REACH
    library: SetLibrary | None = None
    schedules: list[SetSchedule] = field(default_factory=list)
    bindings: list[SetBinding] = field(default_factory=list)
    platform_min_version: str = ""
    deprecated: bool = False
    deprecation_note: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    author: str = ""
    author_url: str = ""
    license: str = ""

    @property
    def templates(self) -> list[str]:
        return [m.template for m in self.members]

    def level_rank(self, level: str) -> int:
        return self.levels.index(level)


# ---------------------------------------------------------------------------
# Reading a set
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def load_set_from_dict(doc: Any) -> DepartmentSet:
    """A ``department.json`` document as a set: the schema's findings first,
    all at once, then the rules only code states."""
    try:
        schema = catalog_schema.load_schema(catalog_schema.SET_SCHEMA)
    except (OSError, ValueError) as exc:
        raise SetValidationError(
            f"the platform's set schema ({catalog_schema.SET_SCHEMA}) cannot be read: {exc}") from exc
    problems = catalog_schema.set_problems(doc, schema)
    if problems:
        raise SetValidationError(problems)
    levels = [str(x).strip() for x in doc["levels"]]
    members = [SetMember(template=str(m["template"]), level=str(m["level"]).strip()) for m in doc["members"]]
    rules: list[str] = []
    seen: set[str] = set()
    for i, m in enumerate(members):
        if m.template in seen:
            rules.append(f"department.json: members[{i}].template names {m.template!r} a second time")
        seen.add(m.template)
        if m.level not in levels:
            rules.append(f"department.json: members[{i}].level {m.level!r} is not one of the levels")
    library = None
    raw_library = doc.get("library")
    if isinstance(raw_library, dict):
        source = str(raw_library.get("source") or "")
        if source not in seen:
            rules.append(f"department.json: library.source {source!r} is not a member")
        library = SetLibrary(source=source, subdir=_text(raw_library.get("subdir")).strip("/"),
                             writable=bool(raw_library.get("writable")))
    schedules: list[SetSchedule] = []
    for i, s in enumerate(doc.get("schedules") or []):
        if str(s["member"]) not in seen:
            rules.append(f"department.json: schedules[{i}].member {s['member']!r} is not a member")
        schedules.append(SetSchedule(member=str(s["member"]), task=str(s["task"]).strip()))
    bindings: list[SetBinding] = []
    for i, b in enumerate(doc.get("bindings") or []):
        for key in ("from", "to"):
            if str(b[key]) not in seen:
                rules.append(f"department.json: bindings[{i}].{key} {b[key]!r} is not a member")
        if b["from"] == b["to"]:
            rules.append(f"department.json: bindings[{i}] binds {b['from']!r} to itself")
        bindings.append(SetBinding(from_member=str(b["from"]), app=str(b["app"]).strip(),
                                   to_member=str(b["to"]), to_app=str(b["to_app"]).strip()))
    if rules:
        raise SetValidationError(rules)
    return DepartmentSet(
        slug=str(doc["slug"]), name=str(doc["name"]).strip(), version=str(doc["version"]),
        levels=levels, members=members, description=_text(doc.get("description")),
        color=_text(doc.get("color")), mode=doc.get("mode") or db_departments.DEFAULT_MODE,
        reach=doc.get("reach") or DEFAULT_REACH, library=library, schedules=schedules,
        bindings=bindings, platform_min_version=_text(doc.get("platform_min_version")),
        deprecated=bool(doc.get("deprecated")), deprecation_note=_text(doc.get("deprecation_note")),
        category=_text(doc.get("category")),
        tags=[str(t) for t in (doc.get("tags") or []) if isinstance(t, str)],
        author=_text(doc.get("author")), author_url=_text(doc.get("author_url")),
        license=_text(doc.get("license")),
    )


def load_set_from_dir(folder: Path) -> DepartmentSet:
    """A set folder of a catalog checkout (the prep gate's check): the
    document read beneath the folder without following a link, and the
    README the catalog requires."""
    if not folder.is_dir():
        raise SetValidationError(f"Set folder not found: {folder}")
    try:
        data = template_sig.read_file(folder, MANIFEST)
    except OSError as exc:
        raise SetValidationError(f"{MANIFEST} cannot be read: {exc}") from exc
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SetValidationError(f"Invalid JSON in {MANIFEST}: {exc}") from exc
    dset = load_set_from_dict(doc)
    if dset.slug != folder.name:
        raise SetValidationError(f"department.json: slug {dset.slug!r} must match the folder name {folder.name!r}")
    if not (folder / "README.md").is_file():
        raise SetValidationError("Required file missing: README.md")
    return dset


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def sets_in(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """The raw set entries of a registry, in the registry's order; an
    older registry has none."""
    raw = registry.get(REGISTRY_KEY) or []
    return [e for e in raw if isinstance(e, dict) and _SLUG_RE.match(str(e.get("slug") or ""))]


def find_set(registry: dict[str, Any], slug: str) -> dict[str, Any] | None:
    return next((e for e in sets_in(registry) if e.get("slug") == slug), None)


def sets_of_template(registry: dict[str, Any], template_slug: str) -> list[dict[str, Any]]:
    """Every set that names ``template_slug`` as a member (a template may
    sit in more than one)."""
    out = []
    for e in sets_in(registry):
        members = e.get("members") or []
        if any(isinstance(m, dict) and m.get("template") == template_slug for m in members):
            out.append(e)
    return out


def template_entry(registry: dict[str, Any], slug: str) -> dict[str, Any] | None:
    return next((e for e in registry.get("agents") or [] if isinstance(e, dict) and e.get("slug") == slug), None)


def read_entry(entry: dict[str, Any]) -> tuple[DepartmentSet | None, list[str]]:
    """A registry entry as a set, or the problems that keep it off the
    install path (the catalog's lint should never let one through)."""
    try:
        return load_set_from_dict(entry), []
    except SetValidationError as exc:
        return None, list(exc.problems)


def missing_members(dset: DepartmentSet, registry: dict[str, Any]) -> list[str]:
    """The member templates the registry does not carry: a set that names
    one cannot be installed (the Browse page says which)."""
    return [t for t in dset.templates if template_entry(registry, t) is None]


# ---------------------------------------------------------------------------
# What a person consents to
# ---------------------------------------------------------------------------

def consent_preview(entry: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """A template entry's apps and checks the way the install dialog shows
    them (the approval card's row shape, the signature the consent
    carries, each check's words).
    An older registry entry carries none."""
    apps = []
    for a in entry.get("apps") or []:
        if not isinstance(a, dict) or not isinstance(a.get("app_json"), dict):
            continue
        visibility = str(a.get("visibility") or "agent")
        bp = a.get("blueprint_json") if isinstance(a.get("blueprint_json"), dict) else None
        row = template_app_seeder.preview_shape(a["app_json"], visibility, bp)
        row["slug"] = str(a.get("slug") or "")
        apps.append({
            "slug": row["slug"], "title": row["title"] or row["slug"], "visibility": visibility,
            "sig": str(a.get("sig") or ""), "owner_approval": bool(a.get("owner_approval")),
            "description": str(a.get("description") or ""),
            "picture_url": str(a.get("picture_url") or ""),
            "row": row,
            "blueprint_tasks": [{"slug": str(t.get("slug") or ""), "description": str(t.get("description") or ""),
                                 "prompt": str(t.get("prompt") or "")}
                                for t in (bp or {}).get("tasks") or [] if isinstance(t, dict)],
            "blueprint_triggers": [{"slug": str(t.get("slug") or ""), "handler": str(t.get("handler") or ""),
                                    "description": str(t.get("description") or "")}
                                   for t in (bp or {}).get("triggers") or [] if isinstance(t, dict)],
        })
    checks = [{
        "name": str(c.get("name") or ""), "description": str(c.get("description") or ""),
        "mandatory": bool(c.get("mandatory")), "applies": list(c.get("applies") or []),
        "script": str(c.get("script") or ""), "sig": str(c.get("sig") or ""),
        "words": template_app_seeder.check_words(c),
    } for c in (entry.get("checks") or []) if isinstance(c, dict) and c.get("name")]
    return apps, checks


def consent_of(entry: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """The signatures a consent to everything the entry ships carries."""
    apps, checks = consent_preview(entry)
    return {a["slug"]: a["sig"] for a in apps}, {c["name"]: c["sig"] for c in checks}
