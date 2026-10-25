"""The catalog schemas beside the MCP manifest schema
(``mcps/agent-template.schema.json``, ``mcps/skill.schema.json``) and the
module that reads them (``storage/catalog_schema.py``).

The proof a released install depends on: every template and every skill
the catalogs serve today, and every template document the public catalog
ever served, passes; what the schema refuses carries a sentence, and what a
template may leave null is read as absent; every rule that needs words has
them, so no finding shows a schema path or the validator's own text.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tarfile
import typing
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from services.community import community_agent_template_store as store, template_sig
from services.mcp.skill_format import FrontMatterError, load_frontmatter
from storage import catalog_schema as cs
from tests._paths import CUSTOM_MCPS, PROXY_DIR, REPO_ROOT

TEMPLATE = cs.load_schema(cs.TEMPLATE_SCHEMA)
SKILL = cs.load_schema(cs.SKILL_SCHEMA)
SIBLINGS = REPO_ROOT.parent
AGENT_CATALOGS = [SIBLINGS / "otodock-community-agents-prep", SIBLINGS / "otodock-community-agents"]
SKILL_TREES = [CUSTOM_MCPS, SIBLINGS / "otodock-community-mcps-prep", SIBLINGS / "otodock-community-mcps",
               SIBLINGS / "otodock-community-skills-prep", SIBLINGS / "otodock-community-skills"]
SKILL_CATALOGS = SKILL_TREES[3:]
# The words jsonschema writes, which a finding must never show.
VALIDATOR_WORDS = ("does not match", "is not valid under", "should be non-empty", "is too long",
                   "is too short", "$defs", "#/", "should not be valid")


def _head(tree: Path) -> str:
    """The commit a catalog checkout stands at, printed with each proof: a
    stale checkout proves an old tree."""
    out = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                         check=False)
    return out.stdout.strip() or "no commit"


def _template_problems(folder: Path) -> list[str]:
    document, problems = cs.template_document(folder, TEMPLATE, template_sig.read_file)
    return problems or cs.template_problems(document, TEMPLATE)


# --- the schemas themselves -------------------------------------------------------

@pytest.mark.parametrize("name", [cs.TEMPLATE_SCHEMA, cs.SKILL_SCHEMA])
def test_the_schemas_are_valid_draft_2020_12_with_a_versioned_id(name):
    schema = cs.load_schema(name)
    Draft202012Validator.check_schema(schema)
    m = re.fullmatch(r"https://raw\.githubusercontent\.com/OtoDock/oto-dock/v(\d+\.\d+\.\d+)/mcps/(.+)", schema["$id"])
    assert m and m.group(2) == name, schema["$id"]
    # The $id names the release the tree is called at its next freeze, the
    # MCP manifest schema's rule: never older than the newest release.
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                          re.MULTILINE)
    key = lambda v: tuple(int(p) for p in v.split("."))
    assert key(m.group(1)) >= key(released[0])
    assert re.fullmatch(r"\d+\.\d+\.\d+", schema["x-otodock-first-release"])
    assert cs.schema_path(name).parent == REPO_ROOT / "mcps"


def _hint_nodes(node, where: str, out: list[str]) -> None:
    """Every node whose failure has no words of its own and no hint. The
    interiors of not, if, anyOf and oneOf never report on their own."""
    if isinstance(node, list):
        for i, sub in enumerate(node):
            _hint_nodes(sub, f"{where}/{i}", out)
        return
    if not isinstance(node, dict):
        return
    needs = {"pattern", "anyOf", "oneOf", "not", "minLength", "maxLength", "minimum", "maximum",
             "minItems", "maxItems", "propertyNames", "uniqueItems", "minProperties", "maxProperties"}
    if needs & node.keys() and not node.get("x-otodock-hint"):
        out.append(where)
    for key in ("properties", "patternProperties", "$defs"):
        for name, sub in (node.get(key) or {}).items():
            _hint_nodes(sub, f"{where}/{key}/{name}", out)
    for key in ("items", "additionalProperties", "then", "else"):
        if isinstance(node.get(key), dict):
            _hint_nodes(node[key], f"{where}/{key}", out)
    if isinstance(node.get("allOf"), list):
        _hint_nodes(node["allOf"], f"{where}/allOf", out)


@pytest.mark.parametrize("schema", [TEMPLATE, SKILL], ids=["template", "skill"])
def test_every_rule_that_needs_words_has_a_hint(schema):
    missing: list[str] = []
    _hint_nodes(schema, "#", missing)
    assert missing == []


def test_every_listed_document_has_its_shape():
    for pattern in TEMPLATE["x-otodock-documents"]:
        example = pattern.replace("*", "x")
        covered = example in TEMPLATE["properties"] or any(
            re.search(rx, example) for rx in TEMPLATE["patternProperties"])
        assert covered, pattern


def _properties(node, out: set) -> set:
    if isinstance(node, dict):
        out |= set(node.get("properties") or {})
        for value in node.values():
            _properties(value, out)
    elif isinstance(node, list):
        for value in node:
            _properties(value, out)
    return out


# The loader's functions that read a template's documents (the persisted
# record has readers of its own).
_DOCUMENT_READERS = {
    "load_template_from_dir", "_discover_apps", "_read_app_item", "_check_blueprint_triggers",
    "_discover_checks", "_parse_dashboards_json", "_parse_default_for_new_users", "_parse_mcps_json",
    "_parse_skills_json", "_parse_schedule", "_parse_tasks_json", "_parse_triggers_json",
    "_parse_notifications_json",
}


def test_every_field_the_loader_reads_is_described():
    source = (PROXY_DIR / "services" / "community" / "community_agent_template_store.py").read_text(encoding="utf-8")
    read: set[str] = set()
    for fn in ast.walk(ast.parse(source)):
        if isinstance(fn, ast.FunctionDef) and fn.name in _DOCUMENT_READERS:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get" and node.args \
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    read.add(node.args[0].value)
                elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                        and isinstance(node.slice.value, str):
                    read.add(node.slice.value)
    # Document names and the folder's own keys, not fields.
    read = {k for k in read if not k.endswith(".json") and "/" not in k}
    assert read, "the loader's reads were not found"
    assert read - _properties(TEMPLATE, set()) == set()


def test_the_enums_equal_the_vocabularies_they_mirror():
    from api.tasks import tasks as tasks_api
    from auth import roles
    from core.session import visibility
    defs = TEMPLATE["$defs"]
    assert defs["agentRoles"]["items"]["enum"] == list(roles.AGENT_ROLES)
    assert defs["agentJson"]["properties"]["default_for_new_users"]["then"]["properties"]["role"]["enum"] == \
        list(roles.AGENT_ROLES)
    assert tuple(defs["scope"]["enum"]) == visibility.SCOPES
    # null reads as absent: the default mode.
    modes = [m for m in defs["task"]["properties"]["notification_mode"]["enum"] if m is not None]
    ddl = (PROXY_DIR / "storage" / "automation" / "schema.py").read_text(encoding="utf-8")
    check = re.search(r"CHECK \(notification_mode IN \(([^)]*)\)\)", ddl)
    assert check and modes == [v.strip().strip("'") for v in check.group(1).split(",")]
    annotation = tasks_api.CreateScheduledTaskRequest.model_fields["notification_mode"].annotation
    assert set(modes) == set(typing.get_args(annotation))
    assert store.DEFAULT_NOTIFICATION_MODE in modes


# --- what the catalogs serve -------------------------------------------------------

@pytest.mark.parametrize("catalog", AGENT_CATALOGS, ids=lambda p: p.name)
def test_every_template_a_catalog_serves_passes_and_loads(catalog):
    if not catalog.is_dir():
        pytest.skip(f"{catalog.name} is not checked out beside this repository")
    head = _head(catalog)
    print(f"{catalog.name} at {head}")
    folders = sorted(p.parent for p in catalog.glob("*/agent.json"))
    assert folders
    for folder in folders:
        assert _template_problems(folder) == [], f"{catalog.name}@{head} {folder.name}"
        assert store.load_template_from_dir(folder).slug == folder.name


def test_every_template_the_public_catalog_ever_served_passes(tmp_path):
    public = AGENT_CATALOGS[1]
    if not (public / ".git").exists():
        pytest.skip("the public agents catalog is not checked out beside this repository")
    revisions = subprocess.run(["git", "-C", str(public), "rev-list", "HEAD"], capture_output=True, text=True,
                               check=True).stdout.split()
    assert revisions
    print(f"{public.name} at {_head(public)}: {len(revisions)} revisions walked")
    for rev in revisions:
        archive = tmp_path / f"{rev}.tar"
        subprocess.run(["git", "-C", str(public), "archive", "-o", str(archive), rev], check=True)
        root = tmp_path / rev
        root.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(root, filter="data")
        for agent_json in sorted(root.glob("*/agent.json")):
            assert _template_problems(agent_json.parent) == [], f"{rev[:9]} {agent_json.parent.name}"


def _declared_skills(tree: Path):
    for manifest in sorted(tree.glob("*/manifest.json")):
        for sk in json.loads(manifest.read_text(encoding="utf-8")).get("skills") or []:
            yield manifest.parent, sk


@pytest.mark.parametrize("tree", SKILL_TREES, ids=lambda p: p.name)
def test_every_skill_front_matter_shipped_or_served_passes(tree):
    if not tree.is_dir():
        pytest.skip(f"{tree.name} is not checked out beside this repository")
    head = _head(tree)
    print(f"{tree.name} at {head}")
    seen = 0
    for folder, sk in _declared_skills(tree):
        fm = load_frontmatter((folder / sk["file"]).read_text(encoding="utf-8"))
        if tree in SKILL_CATALOGS:
            assert fm is not None, f"{folder.name}/{sk['file']}"
        if fm is None:
            continue
        seen += 1
        assert cs.skill_problems(fm, SKILL, sk["file"]) == [], f"{tree.name}@{head} {folder.name}/{sk['file']}"
        assert fm["name"] == sk["id"]
    if tree in SKILL_CATALOGS or tree == CUSTOM_MCPS:
        assert seen


# --- the document -------------------------------------------------------------------

def _write(root: Path, files: dict[str, object]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return root


AGENT = {"slug": "good-one", "display_name": "Good", "version": "1.0.0"}
BASE = {"agent.json": AGENT, "agent.md": "You help.", "README.md": "Helps.", "mcps.json": {"required": []}}


def test_the_document_holds_the_documents_and_the_top_level_files(tmp_path):
    root = _write(tmp_path / "t", {**BASE, "package.json": "{not json", "context/notes.md": "n",
                                   "user-apps/home/app.json": {"title": "Home"},
                                   "user-apps/home/client/index.html": "<p>",
                                   "user-apps/home/data/state.json": "{bad", "user-apps/.draft/app.json": "{bad",
                                   ".hidden/agent.json": "{bad", "checks/lint/check.json": {"name": "lint"}})
    document, problems = cs.template_document(root, TEMPLATE, template_sig.read_file)
    assert problems == []
    assert document["agent.json"] == AGENT and document["user-apps/home/app.json"] == {"title": "Home"}
    assert document["checks/lint/check.json"] == {"name": "lint"}
    # A file that is no document is present, unread; nothing deeper is listed.
    assert document["package.json"] is True and document["README.md"] is True
    assert not any(k.startswith(("context/", ".hidden", "user-apps/.draft")) or "/client/" in k or "/data/" in k
                   for k in document)


def test_the_document_never_follows_a_link(tmp_path):
    outside = _write(tmp_path / "outside", {"app.json": {"title": "Elsewhere"}, "agent.json": AGENT})
    root = _write(tmp_path / "t", {k: v for k, v in BASE.items() if k != "agent.json"})
    os.symlink(outside / "agent.json", root / "agent.json")
    (root / "apps").mkdir()
    os.symlink(outside, root / "apps" / "linked")
    document, problems = cs.template_document(root, TEMPLATE, template_sig.read_file)
    assert problems == ["agent.json cannot be read: it must be a regular file (not a link) of at most 2 MB"]
    assert "apps/linked/app.json" not in document
    with pytest.raises(store.TemplateValidationError, match="agent.json cannot be read"):
        store.load_template_from_dir(root)


def test_a_document_that_is_not_json_keeps_its_sentence(tmp_path):
    root = _write(tmp_path / "t", {**BASE, "tasks.json": "{not json"})
    with pytest.raises(store.TemplateValidationError, match=r"^Invalid JSON in tasks.json: "):
        store.load_template_from_dir(root)


def test_unknown_keys_and_files_pass(tmp_path):
    root = _write(tmp_path / "t", {**BASE, "agent.json": {**AGENT, "default_layer": "x", "future": {"a": 1}},
                                   "mcps.json": {"required": [{"name": "google-workspace", "skills": ["Apps Script"],
                                                               "extra": True}], "later": 1},
                                   "brand-new.json": {"x": 1}, "notes.txt": "n"})
    assert _template_problems(root) == []
    assert cs.skill_problems({"name": "a-skill", "description": "Does a.", "allowed-tools": "Bash(*)",
                              "hooks": {"x": 1}, "metadata": {"version": "2"}}, SKILL, "x") == []


# --- refusals, each a sentence -------------------------------------------------------

TASK = {"slug": "daily-task", "scope": "agent", "prompt": "p", "schedule": {"type": "cron", "cron": "0 8 * * *"}}
NOTE = {"slug": "monthly", "title": "T", "body": "B", "scope": "user", "schedule": {"type": "cron", "cron": "0 9 1 * *"}}
HOME = {"title": "Home", "handlers": {"on_trigger": ["wake"]}}


def _broken(over: dict) -> dict:
    files = dict(BASE)
    for rel, body in over.items():
        if body is None:
            files.pop(rel, None)
        else:
            files[rel] = body
    return files


REFUSED = {
    "no agent.json": ({"agent.json": None}, "Required file missing: agent.json"),
    "no README": ({"README.md": None}, "Required file missing: README.md"),
    "no persona": ({"agent.md": None}, "Missing required file: agent.md (or its pre-1.4 name prompt.md)"),
    "no mcps.json": ({"mcps.json": None}, "Required file missing: mcps.json"),
    "agent.json not an object": ({"agent.json": [1]}, "agent.json must be an object"),
    "missing version": ({"agent.json": {"slug": "good-one", "display_name": "G"}},
                        "agent.json: missing required field 'version'"),
    "bad slug": ({"agent.json": {**AGENT, "slug": "Bad"}}, "agent.json: slug: invalid slug 'Bad'"),
    "slug with a newline": ({"agent.json": {**AGENT, "slug": "good-one\n"}}, "invalid slug 'good-one\\n'"),
    "empty name": ({"agent.json": {**AGENT, "display_name": " "}}, "agent.json: display_name must not be empty"),
    "bad color": ({"agent.json": {**AGENT, "color": "red"}}, "invalid color 'red' (must be #RRGGBB)"),
    "bad scope": ({"agent.json": {**AGENT, "default_scope": "team"}},
                  "agent.json: default_scope must be one of 'user', 'agent', null (got 'team')"),
    "bad core_mcps": ({"agent.json": {**AGENT, "core_mcps": "some"}}, "agent.json: core_mcps must be one of"),
    "string bool": ({"agent.json": {**AGENT, "collaborative": "no"}},
                    "agent.json: collaborative must be true or false"),
    "default role missing": ({"agent.json": {**AGENT, "default_for_new_users": {"enabled": True}}},
                             "agent.json: default_for_new_users: missing required field 'role'"),
    "default role bad": ({"agent.json": {**AGENT, "default_for_new_users": {"enabled": True, "role": "god"}}},
                         "default_for_new_users.role must be one of 'manager'"),
    "mcps not an object": ({"mcps.json": [1]}, "mcps.json must be an object"),
    "mcps required not a list": ({"mcps.json": {"required": "x"}}, "mcps.json: required must be a list"),
    "mcp without a name": ({"mcps.json": {"required": [{}]}}, "mcps.json: required[0]: missing required field 'name'"),
    "mcp skills not a list": ({"mcps.json": {"required": [{"name": "m", "skills": "x"}]}},
                              "mcps.json: required[0].skills must be a list"),
    "skill package skills": ({"skills.json": {"required": [{"name": "s", "skills": 1}]}},
                             "skills.json: required[0].skills must be a list"),
    "tasks not a list": ({"tasks.json": {"tasks": {}}}, "tasks.json: tasks must be a list"),
    "task bad slug": ({"tasks.json": {"tasks": [{**TASK, "slug": "X"}]}}, "tasks.json: tasks[0].slug: invalid slug"),
    "task bad scope": ({"tasks.json": {"tasks": [{**TASK, "scope": "all"}]}}, "tasks[0].scope must be one of"),
    "task no prompt": ({"tasks.json": {"tasks": [{**TASK, "prompt": ""}]}}, "tasks[0].prompt must not be empty"),
    "task no schedule": ({"tasks.json": {"tasks": [{k: v for k, v in TASK.items() if k != "schedule"}]}},
                         "tasks.json: tasks[0]: missing required field 'schedule'"),
    "task schedule type": ({"tasks.json": {"tasks": [{**TASK, "schedule": {"type": "weekly"}}]}},
                           "tasks[0].schedule.type must be one of 'cron', 'interval', 'run_at'"),
    "task cron missing": ({"tasks.json": {"tasks": [{**TASK, "schedule": {"type": "cron"}}]}},
                          "tasks[0].schedule: missing required field 'cron'"),
    "invalid cron": ({"tasks.json": {"tasks": [{**TASK, "schedule": {"type": "cron", "cron": "0 9 *"}}]}},
                     "invalid cron '0 9 *'"),
    "interval": ({"tasks.json": {"tasks": [{**TASK, "schedule": {"type": "interval", "interval_seconds": 0}}]}},
                 "interval_seconds must be a whole number of seconds, at least 1"),
    "run_at": ({"tasks.json": {"tasks": [{**TASK, "schedule": {"type": "run_at"}}]}},
               "missing required field 'run_at'"),
    "default state": ({"tasks.json": {"tasks": [{**TASK, "default_state": "on"}]}},
                      "tasks[0].default_state must be one of 'paused', 'active'"),
    "roles not a list": ({"tasks.json": {"tasks": [{**TASK, "roles": "manager"}]}}, "tasks[0].roles must be a list or null"),
    "a role unknown": ({"tasks.json": {"tasks": [{**TASK, "roles": ["managers"]}]}},
                       "tasks[0].roles[0] must be one of"),
    "notification mode": ({"tasks.json": {"tasks": [{**TASK, "notification_mode": "loud"}]}},
                          "tasks[0].notification_mode must be one of 'auto', 'manual', 'none', null (got 'loud')"),
    "model id": ({"tasks.json": {"tasks": [{**TASK, "override_model": "two words"}]}}, "invalid model id 'two words'"),
    "engine id": ({"tasks.json": {"tasks": [{**TASK, "layer": "Claude Code"}]}}, "invalid engine id 'Claude Code'"),
    "trigger prompt": ({"triggers.json": {"triggers": [{"slug": "on-push", "scope": "agent"}]}},
                       "triggers.json: triggers[0]: missing required field 'prompt'"),
    "title too long": ({"notifications.json": {"notifications": [{**NOTE, "title": "t" * 81}]}},
                       "notifications[0].title must be 1 to 80 characters"),
    "body too long": ({"notifications.json": {"notifications": [{**NOTE, "body": "b" * 501}]}},
                      "notifications[0].body must be 1 to 500 characters"),
    "five dashboards": ({"dashboards.json": {"dashboards": [{"slug": f"d{i}", "file": "d.html"} for i in range(5)]}},
                        "dashboards.json: dashboards: at most 4 dashboards per template"),
    "dashboard file": ({"dashboards.json": {"dashboards": [{"slug": "d", "file": "sub/d.html"}]}},
                       "must be a bare *.html name under dashboards/"),
    "per-user inbound": ({"user-apps/home/app.json": {**HOME, "inbound": {"routes": ["x"]}},
                          "user-apps/home/client/index.html": "<p>"},
                         "user-apps/home/app.json: inbound: a per-user app cannot receive inbound hooks"),
    "per-user knowledge": ({"user-apps/home/app.json": {**HOME, "files": {"write": ["knowledge/x"]}},
                            "user-apps/home/client/index.html": "<p>"},
                           "never knowledge/ (got 'knowledge/x')"),
    "blueprint not an object": ({"user-apps/home/app.json": HOME, "user-apps/home/blueprint.json": [1],
                                 "user-apps/home/client/index.html": "<p>"},
                                "user-apps/home/blueprint.json must be an object"),
    "blueprint roles": ({"user-apps/home/app.json": HOME, "user-apps/home/blueprint.json": {"roles": "x"},
                         "user-apps/home/client/index.html": "<p>"},
                        "user-apps/home/blueprint.json: roles must be a list or null"),
    "nine triggers": ({"user-apps/home/app.json": HOME, "user-apps/home/client/index.html": "<p>",
                       "user-apps/home/blueprint.json": {"triggers": [{"slug": f"t{i}", "handler": "wake"}
                                                                      for i in range(9)]}},
                      "triggers: at most 8 triggers in a blueprint"),
    "trigger slug": ({"user-apps/home/app.json": HOME, "user-apps/home/client/index.html": "<p>",
                      "user-apps/home/blueprint.json": {"triggers": [{"slug": "Bad!", "handler": "wake"}]}},
                     "invalid trigger slug 'Bad!'"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_refusal_is_a_sentence(tmp_path, name):
    over, sentence = REFUSED[name]
    root = _write(tmp_path / "t", _broken(over))
    with pytest.raises(store.TemplateValidationError) as e:
        store.load_template_from_dir(root)
    assert sentence in str(e.value), str(e.value)
    for problem in e.value.problems:
        assert not any(w in problem for w in VALIDATOR_WORDS), problem


APP = {"user-apps/home/client/index.html": "<p>"}

# What a template may leave null (a field read with a default, or read by
# no release): accepted, and read as absent.
ACCEPTED = {
    "description null": ({"agent.json": {**AGENT, "description": None}}, lambda t: t.description == ""),
    "color null": ({"agent.json": {**AGENT, "color": None}}, lambda t: t.color == "#6B7280"),
    "default_scope null": ({"agent.json": {**AGENT, "default_scope": None}}, lambda t: t.default_scope == "user"),
    "core_mcps null": ({"agent.json": {**AGENT, "core_mcps": None}}, lambda t: t.core_mcps == "all"),
    "collaborative null": ({"agent.json": {**AGENT, "collaborative": None}}, lambda t: t.collaborative is False),
    "schema_version a number": ({"agent.json": {**AGENT, "schema_version": 1}}, lambda t: t.slug == "good-one"),
    "unread fields null": ({"agent.json": {**AGENT, **dict.fromkeys(
        ("category", "author", "author_url", "license", "platform_min_version", "deprecation_note", "tags",
         "internal", "deprecated", "$schema"))}}, lambda t: t.slug == "good-one"),
    "default_for_new_users off": ({"agent.json": {**AGENT, "default_for_new_users": {"enabled": None, "role": None}}},
                                  lambda t: t.default_for_new_users == {}),
    "mcps required null": ({"mcps.json": {"required": None}}, lambda t: t.mcps == []),
    "an mcp's skills null": ({"mcps.json": {"required": [{"name": "m", "skills": None}]}},
                             lambda t: t.mcps[0].skills == []),
    "skill packages null": ({"skills.json": {"required": None}}, lambda t: t.skill_packages == []),
    "tasks null": ({"tasks.json": {"tasks": None}}, lambda t: t.tasks == []),
    "triggers null": ({"triggers.json": {"triggers": None}}, lambda t: t.triggers == []),
    "notifications null": ({"notifications.json": {"notifications": None}}, lambda t: t.notifications == []),
    "a task's description and flag null": (
        {"tasks.json": {"tasks": [{**TASK, "description": None, "auto_create_for_new_users": None,
                                   "notification_mode": None}]}},
        lambda t: (t.tasks[0].description, t.tasks[0].auto_create_for_new_users, t.tasks[0].notification_mode)
        == ("", False, "manual")),
    "a trigger's description null": ({"triggers.json": {"triggers": [{"slug": "on-push", "scope": "agent",
                                                                      "prompt": "p", "description": None}]}},
                                     lambda t: t.triggers[0].description == ""),
    "a dashboard's title and visibility null": (
        {"dashboards.json": {"dashboards": [{"slug": "board", "file": "d.html", "title": None, "visibility": None,
                                             "auto_pin_for_new_users": None}]}, "dashboards/d.html": "<p>"},
        lambda t: (t.dashboards[0].title, t.dashboards[0].visibility) == ("board", "agent")),
    "an app's title and requires null": ({"user-apps/home/app.json": {"title": None, "requires": None}, **APP},
                                         lambda t: (t.apps[0].title, t.apps[0].requires_mcps) == ("", [])),
    "an app's mcps null": ({"user-apps/home/app.json": {"requires": {"mcps": None}}, **APP},
                           lambda t: t.apps[0].requires_mcps == []),
    "a blueprint's lists null": (
        {"user-apps/home/app.json": HOME, **APP, "user-apps/home/blueprint.json": {
            "format": None, "tasks": None, "triggers": None, "roles": None, "auto_create_for_new_users": None}},
        lambda t: (t.apps[0].roles, t.apps[0].auto_create_for_new_users) == (None, False)),
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_a_null_the_loader_reads_as_absent_is_accepted(tmp_path, name):
    over, holds = ACCEPTED[name]
    root = _write(tmp_path / "t", _broken(over))
    assert _template_problems(root) == []
    assert holds(store.load_template_from_dir(root))


def test_every_finding_comes_at_once_up_to_the_cap(tmp_path):
    tasks = [{"slug": f"Task{i}", "scope": "agent", "prompt": "p", "schedule": {"type": "cron", "cron": "0 8 * * *"}}
             for i in range(25)]
    root = _write(tmp_path / "t", _broken({"tasks.json": {"tasks": tasks}, "agent.json": {**AGENT, "color": "red"}}))
    with pytest.raises(store.TemplateValidationError) as e:
        store.load_template_from_dir(root)
    problems = e.value.problems
    assert len(problems) == cs.MAX_PROBLEMS + 1 and problems[-1] == "and 6 more"
    assert problems[0].startswith("agent.json: color: invalid color")
    assert str(e.value) == "; ".join(problems)


def test_the_rules_only_code_can_state_still_hold(tmp_path):
    root = _write(tmp_path / "t", _broken({"dashboards.json": {"dashboards": [{"slug": "d", "file": "d.html"}]}}))
    with pytest.raises(store.TemplateValidationError, match="missing file dashboards/d.html"):
        store.load_template_from_dir(root)
    root = _write(tmp_path / "u", _broken({"user-apps/home/app.json": HOME, "user-apps/home/client/index.html": "<p>",
                                           "user-apps/home/blueprint.json": {"triggers": [{"slug": "a", "handler": "nap"}]}}))
    with pytest.raises(store.TemplateValidationError, match="aims at 'nap'"):
        store.load_template_from_dir(root)


# --- the skill front matter -----------------------------------------------------------

@pytest.mark.parametrize("front, sentence", [
    ({"description": "d"}, "x front matter: missing required field 'name'"),
    ({"name": "Two Words", "description": "d"}, "x front matter: name: invalid skill name 'Two Words'"),
    ({"name": "a-skill\n", "description": "d"}, "invalid skill name 'a-skill\\n'"),
    ({"name": "a" * 65, "description": "d"}, "at most 64 characters"),
    ({"name": "a-skill", "description": " "}, "x front matter: description must not be empty"),
    ({"name": "a-skill", "description": "d", "license": 2.0}, "x front matter: license must be a string"),
    ("a string", "x front matter must be an object"),
])
def test_a_skill_front_matter_finding_is_a_sentence(front, sentence):
    problems = cs.skill_problems(front, SKILL, "x front matter")
    assert sentence in problems or any(sentence in p for p in problems), problems
    assert not any(w in p for p in problems for w in VALIDATOR_WORDS)


def test_the_front_matter_reader_tells_absent_broken_and_present_apart():
    assert load_frontmatter("No fence.\n") is None
    with pytest.raises(FrontMatterError, match="not valid YAML"):
        load_frontmatter("---\nname: [x\n---\nbody\n")
    with pytest.raises(FrontMatterError, match="not a mapping"):
        load_frontmatter("---\n- a\n---\nbody\n")
    assert load_frontmatter("---\nname: a\ndescription: b\n---\nbody\n") == {"name": "a", "description": "b"}
