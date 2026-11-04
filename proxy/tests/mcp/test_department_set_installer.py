"""The set installer (``services/community/department_set_installer.py``)
and the placement rules (``services/departments/assignment.py``).

The proof: a set install creates the department with the set's mode,
reach and levels, installs or adopts every member, places them all, shares
the head's folder as a library named after the department to the others
read only, and proves the schedules paused; a member that fails leaves the
installed ones standalone and no department; a retry adopts; an installed
agent in another department is left alone and a second copy takes the
suggested slug; the offer places the new agent with the standalone ones;
the template updater never writes a department field; uninstalling a
member leaves the department and deleting the department leaves the
agents; the move's rule refuses a member, a creator without manager and a
no-user session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from auth.providers import UserContext
from services.community import (
    community_department_sets as sets,
    department_set_installer as installer,
)
from services.departments import assignment
from storage.agents import agent_store, db_departments, db_set_installs
from storage.knowledge import db_knowledge_libraries
from tests.mcp.test_community_agent_installer import _make_user, _write_template

ADMIN = "user-admin"
CREATOR = "user-manager"
MEMBER = "user-viewer"

SET = {
    "slug": "office", "name": "Office", "version": "0.1.0", "mode": "down_across", "reach": "adjacent",
    "levels": ["Head", "Team"],
    "members": [{"template": "head", "level": "Head"}, {"template": "clerk", "level": "Team"},
                {"template": "scribe", "level": "Team"}],
    "library": {"source": "head", "subdir": "facts", "writable": False},
    "schedules": [{"member": "head", "task": "brief"}],
    "bindings": [{"from": "head", "app": "board", "to": "clerk", "to_app": "ledger"}],
}
REGISTRY = {"agents": [
    {"slug": "head", "display_name": "Head", "version": "1.0.0", "platform_min_version": "1.0.0"},
    {"slug": "clerk", "display_name": "Clerk", "version": "1.0.0", "platform_min_version": "1.0.0"},
    {"slug": "scribe", "display_name": "Scribe", "version": "1.0.0", "platform_min_version": "1.0.0"},
], "departments": [SET]}


def _admin() -> UserContext:
    return UserContext(sub=ADMIN, email="admin@test.com", name="Admin", role="admin", agents=[], agent_roles={})


def _creator(managed: list[str]) -> UserContext:
    return UserContext(sub=CREATOR, email="c@test.com", name="C", role="creator", agents=list(managed),
                       agent_roles={a: "manager" for a in managed})


def _member(managed: list[str]) -> UserContext:
    return UserContext(sub=MEMBER, email="m@test.com", name="M", role="member", agents=list(managed),
                       agent_roles={a: "manager" for a in managed})


@pytest.fixture
def catalog(tmp_path, temp_db, monkeypatch):
    """A fake catalog: the three templates on disk, the installer's fetch
    answering them, the MCP registry empty, notifications swallowed."""
    _make_user(ADMIN, "admin@test.com", "admin")
    _make_user(CREATOR, "c@test.com", "creator")
    _make_user(MEMBER, "m@test.com", "member")
    folders = {}
    for slug in ("head", "clerk", "scribe"):
        tasks = [{"slug": "brief", "description": "The brief", "scope": "agent", "prompt": "brief",
                  "schedule": {"type": "cron", "cron": "0 8 * * 1"}, "default_state": "paused"}] \
            if slug == "head" else None
        folders[slug] = _write_template(tmp_path / "catalog", slug=slug, tasks=tasks)
    import shutil

    async def fake_extract(template_slug: str) -> Path:
        src = folders[template_slug]
        dst = tmp_path / "extracted" / f"{template_slug}-{len(list((tmp_path / 'extracted').glob('*'))) if (tmp_path / 'extracted').is_dir() else 0}"
        shutil.copytree(src, dst)
        return dst

    monkeypatch.setattr("services.community.community_agents_catalog.fetch_and_extract_template", fake_extract)
    monkeypatch.setattr("services.community.community_agents_catalog.fetch_registry",
                        AsyncMock(return_value=REGISTRY))
    monkeypatch.setattr("services.community.community_catalog.fetch_registry", AsyncMock(return_value={"mcps": []}))
    monkeypatch.setattr("services.mcp.mcp_registry.get_all_manifests", lambda: {})
    fired: list[dict] = []

    async def fake_fire(**kw):
        fired.append(kw)

    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fake_fire)
    monkeypatch.setattr("services.knowledge.library_projector.attach_setup", AsyncMock())
    return {"folders": folders, "fired": fired, "tmp": tmp_path}


def _run_install(u, name="Lady G", consent=False, choices=None):
    """Start the job and run it to its end in one loop."""
    dset = sets.load_set_from_dict(SET)

    async def go():
        spawned = []
        with patch("core.background.spawn", side_effect=lambda coro, **kw: spawned.append(coro)):
            job = await installer.start(dset, REGISTRY, name, u, consent=consent, choices=choices)
        for coro in spawned:
            await coro
        return db_set_installs.get(job["id"])

    return asyncio.run(go())


# --- the plan -------------------------------------------------------------------------

def test_the_plan_installs_new_members_under_free_slugs(catalog):
    plan = installer.plan_members(sets.load_set_from_dict(SET), REGISTRY, _admin())
    assert [(r["template"], r["action"], r["slug"]) for r in plan] == [
        ("head", installer.ACTION_NEW, "head"), ("clerk", installer.ACTION_NEW, "clerk"),
        ("scribe", installer.ACTION_NEW, "scribe")]
    assert plan[0]["display_name"] == "Head" and plan[0]["platform_compat_ok"] is True


def test_the_plan_adopts_a_managed_standalone_agent_and_suffixes_a_placed_one(catalog):
    agent_store.create_agent("clerk", "Clerk", created_by=ADMIN, community_template="clerk",
                             community_template_version="1.0.0")
    agent_store.create_agent("head", "Head", created_by=ADMIN, community_template="head",
                             community_template_version="1.0.0")
    dept = db_departments.create_department("Other", ADMIN, level_names=["Head"])
    db_departments.place_members(dept["id"], [("head", dept["levels"][0]["id"])])
    dset = sets.load_set_from_dict(SET)
    plan = installer.plan_members(dset, REGISTRY, _admin())
    by = {r["template"]: r for r in plan}
    assert by["clerk"]["action"] == installer.ACTION_ADOPT and by["clerk"]["slug"] == "clerk"
    assert by["head"]["action"] == installer.ACTION_NEW and by["head"]["slug"] == "head-2"
    assert by["head"]["placed"] == ["head"]
    # A creator who does not manage the standalone agent gets a new one.
    plan = installer.plan_members(dset, REGISTRY, _creator([]))
    assert {r["template"]: r["action"] for r in plan}["clerk"] == installer.ACTION_NEW
    # The person may ask for a new one instead of the adoption.
    plan = installer.plan_members(dset, REGISTRY, _admin(), choices={"clerk": installer.ACTION_NEW})
    assert {r["template"]: r["slug"] for r in plan}["clerk"] == "clerk-2"


def test_the_department_name_is_a_library_name_too(catalog):
    assert installer.department_name_problem("Lady G") is None
    assert installer.department_name_problem("") == "Give the department a name."
    assert "may not contain" in (installer.department_name_problem("a/b") or "")
    assert installer.department_name_problem("x" * 65) == "A department name has at most 64 characters."
    assert installer.department_name_problem("x" * 64) is None


# --- the job ----------------------------------------------------------------------------

def test_a_set_install_makes_the_department_places_shares_and_proves_the_schedules(catalog):
    job = _run_install(_admin())
    assert job["status"] == db_set_installs.DONE, job["lines"]
    assert [m["status"] for m in job["members"]] == [installer.MEMBER_INSTALLED] * 3
    dept = db_departments.get_department(job["department_id"])
    assert dept["name"] == "Lady G" and dept["mode"] == "down_across" and dept["reach"] == "adjacent"
    assert [lv["name"] for lv in dept["levels"]] == ["Head", "Team"]
    assert dept["community_set"] == "office" and dept["community_set_version"] == "0.1.0"
    levels = {lv["name"]: lv["id"] for lv in dept["levels"]}
    for slug, level in (("head", "Head"), ("clerk", "Team"), ("scribe", "Team")):
        row = agent_store.get_agent(slug)
        assert row["department_id"] == dept["id"] and row["department_level_id"] == levels[level], slug
    # Down and across: the head reaches the two, the two reach each other, nobody reaches up.
    assert set(agent_store.get_delegation_targets("head")) == {"clerk", "scribe"}
    assert set(agent_store.get_delegation_targets("clerk")) == {"scribe"}
    # The library: the head's facts folder, named after the department, read only to the two.
    assert db_knowledge_libraries.library_name("head", "facts") == "Lady G"
    attached = {(a["consumer_agent"], bool(a["writable"])) for a in db_knowledge_libraries.consumers_of("head", "facts")}
    assert attached == {("clerk", False), ("scribe", False)}
    assert (installer.config.get_agent_dir("head") / "knowledge" / "facts").is_dir()
    # The schedule is paused, the lines say what happened, the person was told.
    text = "\n".join(job["lines"])
    assert "Installed Head as head." in text and "The department Lady G holds Head, Clerk and Scribe." in text
    assert "The library Lady G is shared from Head to Clerk and Scribe, read only." in text
    assert "The briefing schedules are paused: The brief (Head)." in text
    assert catalog["fired"] and catalog["fired"][-1]["title"] == "Lady G is installed"


def test_a_failing_member_leaves_standalone_agents_and_no_department(catalog, monkeypatch):
    real = installer.community_agent_installer.install_from_catalog

    async def failing(**kw):
        if kw["template_slug"] == "scribe":
            raise HTTPException(400, {"error": "missing_mcps", "message": "The MCP x is in no catalog."})
        return await real(**kw)

    monkeypatch.setattr(installer.community_agent_installer, "install_from_catalog", failing)
    job = _run_install(_admin())
    assert job["status"] == db_set_installs.FAILED
    assert [m["status"] for m in job["members"]] == [installer.MEMBER_INSTALLED, installer.MEMBER_INSTALLED,
                                                      installer.MEMBER_FAILED]
    assert db_departments.list_departments() == []
    assert agent_store.get_agent("head")["department_id"] == "" and agent_store.get_agent("clerk")
    assert agent_store.get_agent("scribe") is None
    text = "\n".join(job["lines"])
    assert "Scribe was not installed: The MCP x is in no catalog." in text
    assert "The installed agents stand alone: Head and Clerk." in text
    assert catalog["fired"][-1]["title"] == "The department was not installed"
    # A retry adopts the two and installs the third: the department stands.
    monkeypatch.setattr(installer.community_agent_installer, "install_from_catalog", real)
    job = _run_install(_admin())
    assert job["status"] == db_set_installs.DONE, job["lines"]
    assert [m["status"] for m in job["members"]] == [installer.MEMBER_ADOPTED, installer.MEMBER_ADOPTED,
                                                      installer.MEMBER_INSTALLED]
    assert "Uses Head (head), installed before." in "\n".join(job["lines"])
    assert len(db_departments.list_departments()) == 1


def test_a_second_department_from_the_same_set_takes_suggested_slugs_and_notes_the_bindings(catalog):
    first = _run_install(_admin(), name="Lady G")
    assert first["status"] == db_set_installs.DONE
    second = _run_install(_admin(), name="Lady H")
    assert second["status"] == db_set_installs.DONE, second["lines"]
    assert {m["slug"] for m in second["members"]} == {"head-2", "clerk-2", "scribe-2"}
    assert len(db_departments.list_departments()) == 2
    text = "\n".join(second["lines"])
    assert "The app board of Head reads Clerk by its first name, clerk, not clerk-2." in text
    assert db_knowledge_libraries.library_name("head-2", "facts") == "Lady H"


def test_a_retry_with_the_same_name_reuses_the_department_and_the_library(catalog):
    first = _run_install(_admin())
    dept_id = first["department_id"]
    again = _run_install(_admin())
    assert again["status"] == db_set_installs.DONE and again["department_id"] == dept_id
    # The members the department holds are used, never copied.
    assert [m["status"] for m in again["members"]] == [installer.MEMBER_ADOPTED] * 3
    assert "Uses Head (head), in the department already." in "\n".join(again["lines"])
    assert "The department Lady G existed already and was used." in "\n".join(again["lines"])
    assert "The library Lady G was shared already." in "\n".join(again["lines"])
    assert len(db_departments.list_departments()) == 1
    assert len([a for a in agent_store.get_all_agents() if a.get("community_template")]) == 3
    # Another name: a second department with new copies.
    other = _run_install(_admin(), name="Lady H")
    assert other["status"] == db_set_installs.DONE and other["department_id"] != dept_id


def test_a_retry_finds_the_persons_own_department_among_namesakes(catalog):
    dset = sets.load_set_from_dict(SET)
    mine = db_departments.create_department("Office", CREATOR, mode="down_across", reach="adjacent",
                                            level_names=list(SET["levels"]), community_set="office",
                                            community_set_version="0.1.0")
    theirs = db_departments.create_department("Office", ADMIN, mode="down_across", reach="adjacent",
                                              level_names=list(SET["levels"]), community_set="office",
                                              community_set_version="0.1.0")
    assert installer.reusable_department(dset, "Office", _creator([]))["id"] == mine["id"]
    assert installer.reusable_department(dset, "Office", _admin())["id"] == theirs["id"]
    assert installer.reusable_department(dset, "Office", _member([])) is None


def test_a_set_above_the_platform_is_refused_on_every_path(catalog):
    newer = sets.load_set_from_dict({**SET, "platform_min_version": "99.0.0"})
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.start(newer, REGISTRY, "Lady G", _admin(), consent=False))
    assert exc.value.status == 400 and "needs OtoDock 99.0.0 or newer" in exc.value.detail
    agent_store.create_agent("clerk", "Clerk", created_by=ADMIN, community_template="clerk",
                             community_template_version="1.0.0")
    agent_store.create_agent("head", "Head", created_by=ADMIN, community_template="head",
                             community_template_version="1.0.0")
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.place_from_set(newer, REGISTRY, "Lady G", _admin(), "clerk"))
    assert exc.value.status == 400
    registry = {**REGISTRY, "departments": [{**SET, "platform_min_version": "99.0.0"}]}
    assert installer.offer_for(registry, "clerk", _admin()) is None
    assert installer.offer_for(REGISTRY, "clerk", _admin()) is not None
    assert db_set_installs.running_for_set("office") is None


def test_a_wider_library_is_not_reused_for_the_set(catalog):
    agent_store.create_agent("head", "Head", created_by=ADMIN, community_template="head",
                             community_template_version="1.0.0")
    (installer.config.get_agent_dir("head") / "knowledge").mkdir(parents=True, exist_ok=True)
    db_knowledge_libraries.promote("head", created_by=ADMIN, name="Everything", subdir="")
    job = _run_install(_admin())
    assert job["status"] == db_set_installs.DONE, job["lines"]
    text = "\n".join(job["lines"])
    assert "The library Everything already covers the head's facts folder, so none was made for the department." in text
    assert "is shared from" not in text
    assert db_knowledge_libraries.consumers_of("head", "facts") == [] and db_knowledge_libraries.consumers_of("head", "") == []
    assert db_knowledge_libraries.library_name("head", "") == "Everything"


def test_one_job_per_set_at_a_time_and_a_bad_name_are_refused(catalog):
    dset = sets.load_set_from_dict(SET)
    db_set_installs.create("office", "0.1.0", "Lady G", ADMIN, [])
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.start(dset, REGISTRY, "Lady G", _admin(), consent=False))
    assert exc.value.status == 409
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.start(dset, REGISTRY, "a/b", _admin(), consent=False))
    assert exc.value.status == 400
    with pytest.raises(assignment.AssignmentRefused):
        asyncio.run(installer.start(dset, REGISTRY, "Lady G", _member([]), consent=False))


def test_the_store_holds_one_running_job_per_set(catalog, monkeypatch):
    db_set_installs.create("office", "0.1.0", "Lady G", ADMIN, [])
    with pytest.raises(db_set_installs.AlreadyRunning):
        db_set_installs.create("office", "0.1.0", "Lady H", ADMIN, [])
    # Two starts that both passed the early check: the second insert is refused.
    monkeypatch.setattr(installer, "running_job", lambda _slug: None)
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.start(sets.load_set_from_dict(SET), REGISTRY, "Lady H", _admin(), consent=False))
    assert exc.value.status == 409 and exc.value.detail == installer.ALREADY_RUNNING
    assert len([j for j in (db_set_installs.running_for_set("office"),) if j]) == 1


def test_a_restart_marks_the_running_job_interrupted(catalog):
    job = db_set_installs.create("office", "0.1.0", "Lady G", ADMIN, [])
    assert installer.interrupt_at_boot() == [job["id"]]
    after = db_set_installs.get(job["id"])
    assert after["status"] == db_set_installs.INTERRUPTED and after["lines"] == [installer.INTERRUPTED_LINE]
    assert after["ended_at"]
    # Cut off between the department's create and the placement: the empty
    # department goes, the agents stand alone.
    empty = db_departments.create_department("Lady H", ADMIN, level_names=["Head", "Team"],
                                             community_set="office", community_set_version="0.1.0")
    job = db_set_installs.create("office", "0.1.0", "Lady H", ADMIN, [])
    db_set_installs.update(job["id"], department_id=empty["id"])
    assert installer.interrupt_at_boot() == [job["id"]]
    assert db_set_installs.get(job["id"])["lines"] == [installer.INTERRUPTED_LINE]
    assert db_departments.get_department(empty["id"]) is None
    # Cut off after the placement: the department stands, the sentence says so.
    placed = db_departments.create_department("Lady I", ADMIN, level_names=["Head", "Team"],
                                              community_set="office", community_set_version="0.1.0")
    agent_store.create_agent("head-i", "Head", created_by=ADMIN, community_template="head",
                             community_template_version="1.0.0")
    db_departments.place_members(placed["id"], [("head-i", placed["levels"][0]["id"])])
    job = db_set_installs.create("office", "0.1.0", "Lady I", ADMIN, [])
    db_set_installs.update(job["id"], department_id=placed["id"])
    assert installer.interrupt_at_boot() == [job["id"]]
    assert db_set_installs.get(job["id"])["lines"] == [installer.INTERRUPTED_PLACED_LINE]
    assert db_departments.get_department(placed["id"]) is not None
    assert installer.interrupt_at_boot() == []


def test_a_creator_installs_and_wires_what_it_manages(catalog):
    job = _run_install(_creator([]))
    assert job["status"] == db_set_installs.DONE, job["lines"]
    dept = db_departments.get_department(job["department_id"])
    assert dept["created_by_sub"] == CREATOR
    assert db_knowledge_libraries.library_name("head", "facts") == "Lady G"


def test_the_departments_from_a_set_follow_the_visibility_rule(catalog):
    job = _run_install(_admin())
    dset = sets.load_set_from_dict(SET)
    assert installer.departments_from(dset, _admin()) == [
        {"id": job["department_id"], "name": "Lady G", "version": "0.1.0", "members": ["clerk", "head", "scribe"]}]
    assert installer.departments_from(dset, _creator([])) == []
    assert installer.departments_from(dset, _creator(["clerk"]))[0]["name"] == "Lady G"


# --- the offer -------------------------------------------------------------------------

def test_the_offer_places_the_new_agent_with_the_standalone_members(catalog):
    agent_store.create_agent("clerk", "Clerk", created_by=ADMIN, community_template="clerk",
                             community_template_version="1.0.0")
    agent_store.create_agent("head", "Head", created_by=ADMIN, community_template="head",
                             community_template_version="1.0.0")
    (installer.config.get_agent_dir("head") / "knowledge").mkdir(parents=True, exist_ok=True)
    dset = sets.load_set_from_dict(SET)
    out = asyncio.run(installer.place_from_set(dset, REGISTRY, "Lady G", _admin(), "clerk"))
    dept = out["department"]
    assert dept is not None and set(out["members"]) == {"head", "clerk"}
    assert agent_store.get_agent("head")["department_id"] == dept["id"]
    assert agent_store.get_agent("clerk")["department_id"] == dept["id"]
    assert "The department Lady G holds Head and Clerk." in "\n".join(out["lines"])
    # Alone, there is nobody to place with.
    agent_store.create_agent("scribe-x", "Scribe", created_by=ADMIN, community_template="scribe",
                             community_template_version="1.0.0")
    with pytest.raises(installer.SetInstallRefused) as exc:
        asyncio.run(installer.place_from_set(dset, REGISTRY, "Lady H", _admin(), "scribe-x"))
    assert exc.value.status == 409


# --- what a department leaves ------------------------------------------------------------

def test_uninstalling_a_member_leaves_the_department_and_deleting_it_leaves_the_agents(catalog):
    job = _run_install(_admin())
    dept_id = job["department_id"]
    agent_store.delete_agent("scribe")
    assert db_departments.get_department(dept_id) is not None
    assert agent_store.get_agent("clerk")["department_id"] == dept_id
    unassigned = db_departments.delete_department(dept_id)
    assert sorted(unassigned) == ["clerk", "head"]
    assert agent_store.get_agent("clerk")["department_id"] == "" and agent_store.get_agent("head")


def test_the_template_updater_never_writes_a_department_field():
    source = (Path(installer.__file__).parents[1] / "community" / "community_agent_updater.py").read_text(encoding="utf-8")
    assert "department_id" not in source and "department_level_id" not in source


# --- the placement rules ------------------------------------------------------------------

def test_the_move_rule_refuses_a_member_a_creator_without_manager_and_a_no_user_session(temp_db):
    _make_user(ADMIN, "admin@test.com", "admin")
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = db_departments.create_department("Eng", ADMIN, level_names=["Head", "Team"])
    level = dept["levels"][1]["id"]
    with pytest.raises(assignment.AssignmentRefused) as exc:
        assignment.validate("alpha", dept["id"], level, _member(["alpha"]))
    assert exc.value.status == 403 and exc.value.detail == assignment.ONLY_ADMINS_AND_CREATORS
    with pytest.raises(assignment.AssignmentRefused) as exc:
        assignment.validate("alpha", dept["id"], level, _creator([]))
    assert exc.value.status == 403 and exc.value.detail == assignment.MANAGER_REQUIRED
    nobody = UserContext(sub="session:x", email="", name="", role="admin", agents=[], agent_roles={},
                         agent="alpha", is_api_key=True)
    with pytest.raises(assignment.AssignmentRefused):
        assignment.validate("alpha", dept["id"], level, nobody)
    with pytest.raises(assignment.AssignmentRefused) as exc:
        assignment.validate("alpha", dept["id"], "", _admin())
    assert exc.value.status == 400
    with pytest.raises(assignment.AssignmentRefused) as exc:
        assignment.validate("alpha", dept["id"], "lvl-nope", _admin())
    assert exc.value.detail == assignment.LEVEL_NOT_IN_DEPARTMENT
    assert assignment.validate("alpha", dept["id"], level, _creator(["alpha"]))["id"] == dept["id"]
    row = asyncio.run(assignment.assign("alpha", dept["id"], level, _creator(["alpha"])))
    assert row["department_level_id"] == level
    row = asyncio.run(assignment.assign("alpha", "", "", _admin()))
    assert row["department_id"] == ""
    assert json.loads(json.dumps(assignment.can_edit_department(_admin(), dept))) is True
    assert assignment.can_edit_department(_creator([]), dept) is False
