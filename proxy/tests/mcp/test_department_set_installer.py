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

import config as app_config
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


def _not_done(job: dict) -> list[str]:
    """The job's lines that say something was not done: the ones that never fold."""
    return [job["lines"][i] for i in job["not_done"]]


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
    assert by["clerk"]["choice"] == "clerk"
    # One that sits in a department made by hand is an option, never preselected.
    assert by["head"]["action"] == installer.ACTION_NEW and by["head"]["slug"] == "head-2"
    assert by["head"]["choice"] == installer.USE_NEW
    assert by["head"]["options"] == [{"slug": "head", "display_name": "Head", "department": "Other"}]
    # A creator who does not manage the standalone agent gets a new one.
    plan = installer.plan_members(dset, REGISTRY, _creator([]))
    assert {r["template"]: r["action"] for r in plan}["clerk"] == installer.ACTION_NEW
    # The person may ask for a new one instead of the adoption.
    plan = installer.plan_members(dset, REGISTRY, _admin(), choices={"clerk": installer.USE_NEW})
    assert {r["template"]: r["slug"] for r in plan}["clerk"] == "clerk-2"


def _creator_as(sub: str, agents: list[str]) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="creator", agents=list(agents),
                       agent_roles={a: "member" for a in agents})


def test_one_rule_says_who_sees_a_department(catalog):
    """An admin sees every department; anyone else one they created or one
    that holds an agent they can access: the list route's rule."""
    mine = db_departments.create_department("Mine", CREATOR, level_names=["Head"])
    theirs = db_departments.create_department("Theirs", ADMIN, level_names=["Head"])
    assert assignment.can_see_department(_admin(), theirs, []) is True
    assert assignment.can_see_department(_creator([]), mine, []) is True
    assert assignment.can_see_department(_creator([]), theirs, ["head"]) is False
    assert assignment.can_see_department(_creator(["head"]), theirs, ["head"]) is True
    assert assignment.can_see_department(_member(["head"]), theirs, ["head"]) is True
    assert assignment.can_see_department(_member([]), theirs, ["head"]) is False


def _head_agent(slug, created_by=ADMIN, template="head"):
    agent_store.create_agent(slug, slug.title(), created_by=created_by, community_template=template,
                             community_template_version="1.0.0")


def _place_in(name, slug, community_set=""):
    dept = db_departments.create_department(name, ADMIN, level_names=["Head"], community_set=community_set,
                                            community_set_version="0.1.0" if community_set else "")
    db_departments.place_members(dept["id"], [(slug, dept["levels"][0]["id"])])
    return dept


def test_the_agents_a_person_may_use_for_a_member(catalog):
    """One definition: an agent of the member's template that the person
    manages, on its own or in a department made by hand, never in one
    installed from the catalog, never another template's."""
    _head_agent("head")                                   # on its own
    _head_agent("head-b")                                 # in a department made by hand
    _place_in("Marketing", "head-b")
    _head_agent("head-c")                                 # in a department installed from the catalog
    _place_in("Lady X", "head-c", community_set="other-set")
    _head_agent("clerk-x", template="clerk")              # another template's
    dset = sets.load_set_from_dict(SET)
    head = {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _admin())}["head"]
    assert head["options"] == [{"slug": "head", "display_name": "Head", "department": ""},
                               {"slug": "head-b", "display_name": "Head-B", "department": "Marketing"}]
    # Two usable agents: nothing is guessed.
    assert head["choice"] == installer.USE_NEW and head["action"] == installer.ACTION_NEW
    # A creator manages none of them: no line.
    assert {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _creator([]))}["head"]["options"] == []
    assert {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _creator(["head-b"]))}["head"][
        "options"] == [{"slug": "head-b", "display_name": "Head-B", "department": "Marketing"}]


def test_the_preselection_takes_one_agent_on_its_own_and_nothing_else(catalog):
    dset = sets.load_set_from_dict(SET)

    def head():
        return {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _admin())}["head"]

    assert head()["options"] == [] and head()["choice"] == installer.USE_NEW
    _head_agent("head-b")
    marketing = _place_in("Marketing", "head-b")
    # One agent, in a department: offered, not preselected.
    assert head()["choice"] == installer.USE_NEW and head()["action"] == installer.ACTION_NEW
    agent_store.update_agent("head-b", department_id="", department_level_id="")
    # One agent on its own: preselected, as before.
    assert (head()["choice"], head()["action"], head()["slug"]) == ("head-b", installer.ACTION_ADOPT, "head-b")
    _head_agent("head")
    # Two on their own: nothing is guessed.
    assert head()["choice"] == installer.USE_NEW
    # A chosen agent in a department moves, and the row names where from.
    db_departments.place_members(marketing["id"], [("head-b", marketing["levels"][0]["id"])])
    row = {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _admin(), {"head": "head-b"})}["head"]
    assert (row["action"], row["slug"]) == (installer.ACTION_MOVE, "head-b")
    assert row["moves_from"] == {"id": marketing["id"], "name": "Marketing"}


@pytest.mark.parametrize("case", ["other template", "not managed", "catalog department", "unknown",
                                  "kept here", "api key", "no person"])
def test_a_choice_outside_the_usable_agents_is_refused(catalog, case):
    """The preview and the install refuse, in one sentence, a choice that
    names no usable agent of that member for this person."""
    _head_agent("head-own")
    _head_agent("head-cat")
    _place_in("Lady X", "head-cat", community_set="other-set")
    _head_agent("clerk-x", template="clerk")
    agent_store.create_agent("head-theirs", "Theirs", created_by=CREATOR, community_template="head",
                             community_template_version="1.0.0")
    dset = sets.load_set_from_dict(SET)
    u, choice, name = _admin(), "head-own", ""
    if case == "other template":
        choice = "clerk-x"
    elif case == "not managed":
        u, choice = _creator([]), "head-theirs"
    elif case == "catalog department":
        choice = "head-cat"
    elif case == "unknown":
        choice = "ghost"
    elif case == "kept here":
        first = _run_install(_admin(), name="Lady G", choices={"head": "head-own"})
        assert first["status"] == db_set_installs.DONE, first["lines"]
        name, choice = "Lady G", "head-own"
        # head is kept in Lady G: its member has no line, so no choice.
        assert {r["template"]: r for r in installer.plan_members(dset, REGISTRY, _admin(), None, name)}["head"][
            "options"] == []
    elif case == "api key":
        u = UserContext(sub=ADMIN, email="a@test.com", name="A", role="admin", agents=[], agent_roles={},
                        is_api_key=True, api_key_id="k-1")
    elif case == "no person":
        u = UserContext(sub="session:x", email="", name="", role="admin", agents=["head-own"],
                        agent_roles={"head-own": "manager"}, agent="head-own", is_api_key=True)
        assert u.acting_sub is None
    with pytest.raises(installer.SetInstallRefused) as exc:
        installer.plan_members(dset, REGISTRY, u, {"head": choice}, name)
    assert exc.value.status == 400
    assert exc.value.detail == f"{choice} cannot fill the place of Head. Choose another agent or install a new one."


def test_a_department_a_creator_sees_but_may_not_place_into_is_left_alone(catalog):
    """The two rules on one install. A creator who manages one agent of the
    admin's set department sees it, may not place into it, and installs the
    set under the same name: the preview names the kept agent's department,
    the install makes the creator's own department and places nothing in the
    admin's, whose record stays as it was."""
    first = _run_install(_admin(), name="Lady G")
    theirs = db_departments.get_department(first["department_id"])
    creator = _creator(["head"])
    assert assignment.can_see_department(creator, theirs, db_departments.member_slugs(theirs["id"])) is True
    assert assignment.may_place_into(creator, theirs) is False
    assert installer.reusable_department(sets.load_set_from_dict(SET), "Lady G", creator) is None
    plan = {r["template"]: r for r in installer.plan_members(sets.load_set_from_dict(SET), REGISTRY, creator,
                                                             None, "Lady G")}
    assert plan["head"]["action"] == installer.ACTION_NEW and plan["head"]["options"] == []
    job = _run_install(creator, name="Lady G")
    assert job["status"] == db_set_installs.DONE, job["lines"]
    assert job["department_id"] != theirs["id"]
    assert db_departments.member_slugs(theirs["id"]) == ["clerk", "head", "scribe"]
    assert db_departments.set_member(theirs["id"], "head") == "head"
    assert db_departments.set_member(job["department_id"], "head") == "head-2"
    # The admin installs under the creator's name again: the newest department
    # of that name is reused, and the admin may place into any department.
    again = _run_install(_admin(), name="Lady G")
    assert again["status"] == db_set_installs.DONE and again["department_id"] == job["department_id"]
    assert db_departments.set_member(job["department_id"], "head") == "head-2"


def test_one_reading_of_a_departments_agents_serves_both_rules(catalog):
    """``members_by_department`` is what the visibility rule and the
    placement rule both judge on: an agent with a department and no level
    counts for both."""
    dept = db_departments.create_department("Eng", ADMIN, level_names=["Head"])
    for slug in ("one", "two"):
        agent_store.create_agent(slug, slug, created_by=ADMIN)
    db_departments.place_members(dept["id"], [("one", dept["levels"][0]["id"])])
    agent_store.update_agent("two", department_id=dept["id"], department_level_id="")
    assert db_departments.members_by_department()[dept["id"]] == ["one", "two"]
    assert db_departments.member_slugs(dept["id"]) == ["one", "two"]
    assert assignment.may_place_into(_creator(["one"]), dept) is False
    assert assignment.may_place_into(_creator(["one", "two"]), dept) is True
    assert assignment.can_see_department(_creator(["two"]), dept, db_departments.member_slugs(dept["id"])) is True


def test_a_chosen_agent_moves_through_the_one_move_and_its_other_managers_are_told(catalog, monkeypatch):
    """The agent leaves a department made by hand through
    ``assignment.assign``; the set's record names it; the result says so;
    its other managers hear of it, the person who installed does not."""
    _head_agent("head")
    agent_store.create_agent("helper", "Helper", created_by=ADMIN)
    marketing = db_departments.create_department("Marketing", ADMIN, level_names=["Head", "Team"])
    db_departments.place_members(marketing["id"], [("head", marketing["levels"][0]["id"]),
                                                   ("helper", marketing["levels"][1]["id"])])
    from storage.identity import db_users
    db_users.add_user_agent(CREATOR, "head", "manager", ADMIN)
    db_users.add_user_agent(ADMIN, "head", "manager", ADMIN)
    real, calls, told = assignment.assign, [], []

    async def spy(agent, department_id, level_id, u, **kw):
        calls.append((agent, department_id))
        return await real(agent, department_id, level_id, u, **kw)

    async def fire(**kw):
        told.append(kw)
        return []

    monkeypatch.setattr(installer.assignment, "assign", spy)
    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fire)
    job = _run_install(_admin(), choices={"head": "head"})
    assert job["status"] == db_set_installs.DONE, job["lines"]
    by = {m["template"]: m for m in job["members"]}
    assert by["head"]["action"] == installer.ACTION_MOVE and by["head"]["status"] == installer.MEMBER_ADOPTED
    assert calls == [("head", job["department_id"])]
    assert agent_store.get_agent("head")["department_id"] == job["department_id"]
    assert agent_store.get_agent("helper")["department_id"] == marketing["id"]
    assert db_departments.set_member(job["department_id"], "head") == "head"
    assert "Head moved here from Marketing." in job["lines"]
    assert "Head moved here from Marketing." not in _not_done(job)
    moved = [kw for kw in told if kw.get("source_id") == "head"]
    assert [kw["target"] for kw in moved] == [CREATOR]
    assert moved[0]["title"] == "Head moved to Lady G"
    assert moved[0]["body"] == "It left the Marketing department when the Lady G department was installed."
    assert moved[0]["href"] == "/agents/head"


def test_a_choice_made_stale_before_the_install_acts_places_nothing(catalog, monkeypatch):
    """The job asks again right before it acts: an agent moved elsewhere
    since the preview fails its member, and nothing is placed."""
    _head_agent("head")
    marketing = _place_in("Marketing", "head")
    sales = db_departments.create_department("Sales", ADMIN, level_names=["Head"])
    real_start = installer._install_members

    async def moved_first(job_id, *a, **kw):
        db_departments.place_members(sales["id"], [("head", sales["levels"][0]["id"])])
        return await real_start(job_id, *a, **kw)

    monkeypatch.setattr(installer, "_install_members", moved_first)
    job = _run_install(_admin(), choices={"head": "head"})
    assert job["status"] == db_set_installs.FAILED
    by = {m["template"]: m for m in job["members"]}
    assert by["head"]["status"] == installer.MEMBER_FAILED
    assert by["head"]["detail"] == "The agent you chose has changed since the dialog opened. Open the dialog again."
    assert agent_store.get_agent("head")["department_id"] == sales["id"]
    assert job["department_id"] in ("", None)
    assert marketing["id"] != sales["id"]


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
    assert (app_config.get_agent_dir("head") / "knowledge" / "facts").is_dir()
    # The schedule is paused, the lines say what happened, the person was told.
    text = "\n".join(job["lines"])
    assert "Installed Head as head." in text and "The department Lady G holds Head, Clerk and Scribe." in text
    assert "The library Lady G is shared from Head to Clerk and Scribe, read only." in text
    assert "1 schedule is paused. Its agent's setup page says whether to start it." in text
    assert "The brief" not in text
    assert _not_done(job) == []
    assert catalog["fired"] and catalog["fired"][-1]["title"] == "Lady G is installed"
    # The record of who fills each member of the set: what a binding reads.
    assert {t: db_departments.set_member(dept["id"], t) for t in ("head", "clerk", "scribe")} == {
        "head": "head", "clerk": "clerk", "scribe": "scribe"}


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
    assert _not_done(job) == ["Scribe was not installed: The MCP x is in no catalog.",
                              "The installed agents stand alone: Head and Clerk."]
    assert catalog["fired"][-1]["title"] == "The department was not installed"
    # A retry adopts the two and installs the third: the department stands.
    monkeypatch.setattr(installer.community_agent_installer, "install_from_catalog", real)
    job = _run_install(_admin())
    assert job["status"] == db_set_installs.DONE, job["lines"]
    assert [m["status"] for m in job["members"]] == [installer.MEMBER_ADOPTED, installer.MEMBER_ADOPTED,
                                                      installer.MEMBER_INSTALLED]
    assert "Uses Head (head), installed before." in "\n".join(job["lines"])
    assert len(db_departments.list_departments()) == 1


def test_a_second_department_from_the_same_set_takes_suggested_slugs_and_records_them(catalog):
    first = _run_install(_admin(), name="Lady G")
    assert first["status"] == db_set_installs.DONE
    second = _run_install(_admin(), name="Lady H")
    assert second["status"] == db_set_installs.DONE, second["lines"]
    assert {m["slug"] for m in second["members"]} == {"head-2", "clerk-2", "scribe-2"}
    assert len(db_departments.list_departments()) == 2
    # Each department records its own members: the second one's app reads clerk-2.
    assert db_departments.set_member(first["department_id"], "clerk") == "clerk"
    assert db_departments.set_member(second["department_id"], "clerk") == "clerk-2"
    # No line says the app reads nothing any more.
    text = "\n".join(second["lines"])
    assert "first name" not in text and "shows nothing" not in text
    assert db_knowledge_libraries.library_name("head-2", "facts") == "Lady H"


def test_a_new_copy_in_a_reused_department_replaces_the_recorded_member(catalog):
    first = _run_install(_admin())
    dept_id = first["department_id"]
    again = _run_install(_admin(), choices={"clerk": installer.USE_NEW})
    assert again["status"] == db_set_installs.DONE and again["department_id"] == dept_id, again["lines"]
    # The new copy is the member now; the old one stays placed with no place of its own.
    assert db_departments.set_member(dept_id, "clerk") == "clerk-2"
    assert agent_store.get_agent("clerk")["department_id"] == dept_id
    assert db_departments.set_member(dept_id, "head") == "head"
    # Installed again under its name: the old copy is adopted, the new copy
    # keeps its place (only "Install a new one instead" replaces a member).
    third = _run_install(_admin())
    assert third["status"] == db_set_installs.DONE and third["department_id"] == dept_id, third["lines"]
    assert db_departments.set_member(dept_id, "clerk") == "clerk-2"


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
    (app_config.get_agent_dir("head") / "knowledge").mkdir(parents=True, exist_ok=True)
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
    assert after["not_done"] == [0]
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
    assert _not_done(db_set_installs.get(job["id"])) == [installer.INTERRUPTED_PLACED_LINE]
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
    (app_config.get_agent_dir("head") / "knowledge").mkdir(parents=True, exist_ok=True)
    dset = sets.load_set_from_dict(SET)
    out = asyncio.run(installer.place_from_set(dset, REGISTRY, "Lady G", _admin(), "clerk"))
    dept = out["department"]
    assert dept is not None and set(out["members"]) == {"head", "clerk"}
    assert agent_store.get_agent("head")["department_id"] == dept["id"]
    assert agent_store.get_agent("clerk")["department_id"] == dept["id"]
    assert "The department Lady G holds Head and Clerk." in "\n".join(out["lines"])
    # The record names the whole set: the place nobody fills is empty, never
    # left for a binding to look outside the department.
    assert db_departments.set_member(dept["id"], "head") == "head"
    assert db_departments.set_member(dept["id"], "clerk") == "clerk"
    assert db_departments.set_member(dept["id"], "scribe") == ""
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
    # The deleted member's place is emptied, the others stay recorded.
    assert db_departments.set_member(dept_id, "scribe") == ""
    assert db_departments.set_member(dept_id, "clerk") == "clerk"
    unassigned = db_departments.delete_department(dept_id)
    assert sorted(unassigned) == ["clerk", "head"]
    # The department's record goes with it.
    assert db_departments.set_members_all() == {}
    assert agent_store.get_agent("clerk")["department_id"] == "" and agent_store.get_agent("head")


def test_the_template_updater_never_writes_a_department_field():
    source = (Path(installer.__file__).parents[1] / "community" / "community_agent_updater.py").read_text(encoding="utf-8")
    assert "department_id" not in source and "department_level_id" not in source


# --- the placement rules ------------------------------------------------------------------

def test_the_move_rule_refuses_a_member_a_creator_without_manager_and_a_no_user_session(temp_db):
    _make_user(ADMIN, "admin@test.com", "admin")
    agent_store.create_agent("alpha", "Alpha", created_by=ADMIN)
    dept = db_departments.create_department("Eng", CREATOR, level_names=["Head", "Team"])
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
    # Not into an empty department the creator did not make.
    theirs = db_departments.create_department("Ops", ADMIN, level_names=["Head"])
    with pytest.raises(assignment.AssignmentRefused) as exc:
        assignment.validate("alpha", theirs["id"], theirs["levels"][0]["id"], _creator(["alpha"]))
    assert exc.value.status == 403 and exc.value.detail == assignment.PLACE_REFUSED
    row = asyncio.run(assignment.assign("alpha", dept["id"], level, _creator(["alpha"])))
    assert row["department_level_id"] == level
    row = asyncio.run(assignment.assign("alpha", "", "", _admin()))
    assert row["department_id"] == ""
    assert json.loads(json.dumps(assignment.can_edit_department(_admin(), dept))) is True
    assert assignment.can_edit_department(_creator([]), theirs) is False


def test_a_head_whose_knowledge_folder_is_a_link_shares_nothing_through_it(catalog, monkeypatch, tmp_path):
    """The library's folder is made beneath the head's real folder with no
    link followed: a knowledge folder replaced by a link gets nothing made
    where it points, and the job says the library was not shared."""
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    real = installer._share_library

    async def planting(*a, **k):
        knowledge = app_config.get_agent_dir("head") / "knowledge"
        if knowledge.is_dir() and not knowledge.is_symlink():
            import shutil
            shutil.rmtree(knowledge)
        knowledge.symlink_to(stand_in)
        return await real(*a, **k)
    monkeypatch.setattr(installer, "_share_library", planting)
    job = _run_install(_admin())
    assert list(stand_in.iterdir()) == []
    assert any(line.startswith("The library was not shared") for line in job["lines"]), job["lines"]
    assert [line for line in _not_done(job) if line.startswith("The library was not shared")], job["lines"]


def test_an_agent_that_was_not_moved_is_a_line_that_never_folds(catalog, monkeypatch):
    """A move the rules refuse leaves the agent where it was; the job goes
    on and its line is one of those that never fold."""
    _head_agent("head")
    marketing = db_departments.create_department("Marketing", ADMIN, level_names=["Head", "Team"])
    db_departments.place_members(marketing["id"], [("head", marketing["levels"][0]["id"])])

    async def refuse(agent, department_id, level_id, u, **kw):
        raise assignment.AssignmentRefused(403, "You do not manage Marketing.")

    monkeypatch.setattr(installer.assignment, "assign", refuse)
    job = _run_install(_admin(), choices={"head": "head"})
    assert agent_store.get_agent("head")["department_id"] == marketing["id"]
    # The hand-made head has no seeded schedule: that line never folds either.
    assert _not_done(job) == ["Head was not moved here: You do not manage Marketing.",
                              "These schedules were not found: brief (Head)."]
    assert "Installed Clerk as clerk." in job["lines"]


def test_the_schedules_line_counts_the_paused_ones_and_names_the_others(catalog, monkeypatch):
    """Paused schedules are counted, not named; one running already is a
    plain line, one not found a line that never folds."""
    from types import SimpleNamespace as S
    rows = {("head", "brief"): {"name": "The brief", "enabled": False},
            ("head", "weekly"): {"name": "Weekly", "enabled": False},
            ("clerk", "daily"): {"name": "Daily", "enabled": True}}
    monkeypatch.setattr(installer.db_tasks, "find_template_task", lambda slug, task: rows.get((slug, task)))
    for slug, name in (("head", "Head"), ("clerk", "Clerk")):
        agent_store.create_agent(slug, name, created_by=ADMIN)
    dset = S(schedules=[S(member="head", task="brief"), S(member="head", task="weekly"),
                        S(member="clerk", task="daily"), S(member="clerk", task="gone")])
    lines = installer.JobLines()
    installer._check_schedules(dset, {"head": "head", "clerk": "clerk"}, lines)
    assert list(lines) == ["2 schedules are paused. Each agent's setup page says which to start.",
                           "These schedules run already: Daily (Clerk).",
                           "These schedules were not found: gone (Clerk)."]
    assert lines.not_done == [2]
