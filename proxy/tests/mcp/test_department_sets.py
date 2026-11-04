"""Department sets as the catalog serves them (``mcps/department.schema.json``,
``services/community/community_department_sets.py``).

The proof a released install depends on: a set entry lives under the
registry's own ``departments`` key, which the 1.9.0 reader never looks at;
what the schema refuses carries a sentence; the rules only code states hold;
every set a catalog checkout serves loads.
"""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from services.community import community_department_sets as sets
from storage.agents import db_departments
from tests._paths import REPO_ROOT

SIBLINGS = REPO_ROOT.parent
AGENT_CATALOGS = [SIBLINGS / "otodock-community-agents-prep", SIBLINGS / "otodock-community-agents"]

YACHT = {
    "schema_version": "1",
    "slug": "yacht-office",
    "name": "Yacht office",
    "description": "The five agents of a yacht management office.",
    "version": "0.1.0",
    "color": "#1E3A8A",
    "platform_min_version": "1.9.1",
    "mode": "down_across",
    "reach": "adjacent",
    "levels": ["Head", "Team"],
    "members": [
        {"template": "yacht-manager", "level": "Head"},
        {"template": "yacht-finance", "level": "Team"},
        {"template": "yacht-crew", "level": "Team"},
    ],
    "library": {"source": "yacht-manager", "subdir": "vessel", "writable": False},
    "schedules": [{"member": "yacht-manager", "task": "weekly-bulletin"}],
    "bindings": [{"from": "yacht-manager", "app": "owner-report", "to": "yacht-finance", "to_app": "finance-board"}],
}


def _with(**over) -> dict:
    doc = copy.deepcopy(YACHT)
    doc.update(over)
    return doc


# --- reading a set -------------------------------------------------------------------

def test_a_set_loads_with_its_defaults_read_as_absent():
    dset = sets.load_set_from_dict(_with(mode=None, reach=None, library=None, schedules=None, bindings=None,
                                         description=None, color=None))
    assert dset.slug == "yacht-office" and dset.name == "Yacht office" and dset.version == "0.1.0"
    assert dset.mode == db_departments.DEFAULT_MODE and dset.reach == sets.DEFAULT_REACH
    assert dset.levels == ["Head", "Team"]
    assert dset.templates == ["yacht-manager", "yacht-finance", "yacht-crew"]
    assert dset.level_rank("Team") == 1
    assert dset.library is None and dset.schedules == [] and dset.bindings == []
    assert dset.description == "" and dset.color == ""


def test_a_set_keeps_its_library_schedules_and_bindings():
    dset = sets.load_set_from_dict(YACHT)
    assert dset.mode == "down_across" and dset.reach == "adjacent"
    assert dset.library == sets.SetLibrary(source="yacht-manager", subdir="vessel", writable=False)
    assert dset.schedules == [sets.SetSchedule(member="yacht-manager", task="weekly-bulletin")]
    assert dset.bindings == [sets.SetBinding(from_member="yacht-manager", app="owner-report",
                                             to_member="yacht-finance", to_app="finance-board")]
    assert dset.platform_min_version == "1.9.1"


@pytest.mark.parametrize("over, sentence", [
    ({"slug": "Yacht"}, "department.json: slug: invalid slug 'Yacht'"),
    ({"name": "a/b"}, "department.json: name: invalid name 'a/b'"),
    ({"version": "1"}, "department.json: version: invalid version '1'"),
    ({"mode": "sideways"}, "department.json: mode must be one of 'off', 'down', 'down_across', 'both', null"),
    ({"levels": []}, "department.json: levels must name 1 to 8 levels"),
    ({"levels": ["Head", "Head"]}, "department.json: levels must name 1 to 8 levels, each once"),
    ({"members": []}, "department.json: members must name 1 to 24 members"),
    ({"members": [{"template": "yacht-manager"}]}, "department.json: members[0]: missing required field 'level'"),
    ({"library": {"source": "yacht-manager", "subdir": "../x"}}, "department.json: library.subdir: invalid folder"),
])
def test_a_schema_finding_is_a_sentence(over, sentence):
    with pytest.raises(sets.SetValidationError) as exc:
        sets.load_set_from_dict(_with(**over))
    assert any(p.startswith(sentence) for p in exc.value.problems), exc.value.problems
    for p in exc.value.problems:
        assert "$defs" not in p and "#/" not in p and "is not valid under" not in p


def test_the_rules_only_code_states_come_at_once():
    doc = _with(members=[{"template": "yacht-manager", "level": "Head"},
                         {"template": "yacht-manager", "level": "Captain"}],
                library={"source": "yacht-crew"},
                schedules=[{"member": "yacht-crew", "task": "x"}],
                bindings=[{"from": "yacht-manager", "app": "a", "to": "yacht-manager", "to_app": "b"}])
    with pytest.raises(sets.SetValidationError) as exc:
        sets.load_set_from_dict(doc)
    assert exc.value.problems == [
        "department.json: members[1].template names 'yacht-manager' a second time",
        "department.json: members[1].level 'Captain' is not one of the levels",
        "department.json: library.source 'yacht-crew' is not a member",
        "department.json: schedules[0].member 'yacht-crew' is not a member",
        "department.json: bindings[0] binds 'yacht-manager' to itself",
    ]


def test_a_set_folder_needs_its_readme_and_the_folder_name(tmp_path):
    folder = tmp_path / "yacht-office"
    folder.mkdir()
    (folder / sets.MANIFEST).write_text(json.dumps(YACHT))
    with pytest.raises(sets.SetValidationError, match="README.md"):
        sets.load_set_from_dir(folder)
    (folder / "README.md").write_text("# Yacht office\n")
    assert sets.load_set_from_dir(folder).slug == "yacht-office"
    other = tmp_path / "other"
    other.mkdir()
    (other / sets.MANIFEST).write_text(json.dumps(YACHT))
    (other / "README.md").write_text("x")
    with pytest.raises(sets.SetValidationError, match="must match the folder name"):
        sets.load_set_from_dir(other)
    (other / sets.MANIFEST).write_text("{not json")
    with pytest.raises(sets.SetValidationError, match="Invalid JSON"):
        sets.load_set_from_dir(other)


# --- the registry ----------------------------------------------------------------------

REGISTRY = {
    "registry_version": "1",
    "agents": [{"slug": "yacht-manager", "version": "0.2.2", "apps": [
        {"slug": "owner-report", "title": "Owner report", "visibility": "agent", "sig": "sig-a",
         "owner_approval": False, "app_json": {"title": "Owner report"}, "blueprint_json": None,
         "description": "The reports.", "picture_url": ""}],
        "checks": [{"name": "records", "description": "Records", "sections": ["judge"], "applies": ["chats"],
                    "mandatory": True, "script": "", "sig": "sig-c"}]},
               {"slug": "yacht-finance", "version": "0.2.2"}],
    "departments": [YACHT, {"slug": "Bad slug"}, "junk"],
}


def test_the_registry_helpers_read_the_departments_key():
    assert [e["slug"] for e in sets.sets_in(REGISTRY)] == ["yacht-office"]
    assert sets.find_set(REGISTRY, "yacht-office") is YACHT
    assert sets.find_set(REGISTRY, "none") is None
    assert [e["slug"] for e in sets.sets_of_template(REGISTRY, "yacht-crew")] == ["yacht-office"]
    assert sets.sets_of_template(REGISTRY, "personal-assistant") == []
    assert sets.sets_in({"agents": []}) == []
    dset, problems = sets.read_entry(YACHT)
    assert dset is not None and problems == []
    assert sets.read_entry({"slug": "x"})[0] is None
    assert sets.missing_members(dset, REGISTRY) == ["yacht-crew"]


def test_the_consent_preview_has_the_dialogs_shape():
    apps, checks = sets.consent_preview(sets.template_entry(REGISTRY, "yacht-manager"))
    (app,) = apps
    assert app["slug"] == "owner-report" and app["sig"] == "sig-a" and app["description"] == "The reports."
    assert app["row"]["kind"] == "folder"
    (check,) = checks
    assert check["name"] == "records" and check["mandatory"] is True
    assert sets.consent_of(sets.template_entry(REGISTRY, "yacht-manager")) == ({"owner-report": "sig-a"}, {"records": "sig-c"})
    assert sets.consent_preview({"slug": "bare"}) == ([], [])


def test_the_1_9_0_reader_never_sees_a_set():
    """The 1.9.0 Browse route's loop (``api/mcp/community.py`` at the
    release commit dc9e1a5cc) reads ``registry.get("agents", [])`` and
    nothing else, so a registry that carries sets lists the templates it
    always did. The loop is vendored here as that release shipped it."""
    augmented = []
    for entry in REGISTRY.get("agents", []):
        augmented.append({**entry, "installed_as": []})
    assert [a["slug"] for a in augmented] == ["yacht-manager", "yacht-finance"]
    assert not any(a.get("members") for a in augmented)


def test_the_release_reader_of_1_9_0_reads_the_agents_list_alone():
    """The vendored loop above is the released one: its source at the
    release commit names the agents list and no departments key."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "show", "dc9e1a5cc:proxy/api/mcp/community.py"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        pytest.skip("the 1.9.0 release commit is not in this checkout")
    source = out.stdout
    assert 'for entry in registry.get("agents", [])' in source
    assert sets.REGISTRY_KEY not in source


# --- what the catalogs serve ----------------------------------------------------------

@pytest.mark.parametrize("catalog", AGENT_CATALOGS, ids=lambda p: p.name)
def test_every_set_a_catalog_serves_loads_and_names_served_templates(catalog):
    if not catalog.is_dir():
        pytest.skip(f"{catalog.name} is not checked out beside this repository")
    folders = sorted(p.parent for p in catalog.glob(f"*/{sets.MANIFEST}"))
    if not folders:
        pytest.skip(f"{catalog.name} serves no set")
    registry = json.loads((catalog / "registry.json").read_text(encoding="utf-8"))
    listed = {e["slug"] for e in sets.sets_in(registry)}
    for folder in folders:
        dset = sets.load_set_from_dir(folder)
        assert dset.slug == folder.name and dset.slug in listed
        assert sets.missing_members(dset, registry) == []
        for member in dset.members:
            assert (catalog / member.template / "agent.json").is_file()
