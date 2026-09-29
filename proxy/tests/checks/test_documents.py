"""services/checks/documents — the check document (CHECKS.md): what is
accepted, where it lives, how the index follows the files (a hand edit is
picked up and shown as "file"), the refs, the writes and the deletes.

env TEST_DATABASE_URL=postgresql://otodock:otodock@localhost:5433/otodock_test \
    venv/bin/python -m pytest tests/checks/test_documents.py -q
"""

import json

import pytest

import config
from services.checks import documents as d
from storage import database as task_store
from storage.checks import db_checks
from storage.pg import get_conn

AGENT = "checks-doc-agent"


@pytest.fixture
def tree(temp_db, tmp_path, monkeypatch):
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Checks Doc Agent", created_by="alice-sub")
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    (root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True, exist_ok=True)
    return root / AGENT


def _doc(**over):
    doc = {"name": "coding", "description": "Lint and a look",
           "judge": {"rubric": "Is the code sound?"}}
    doc.update(over)
    return doc


def test_validation_accepts_the_shape_and_fills_the_defaults():
    out = d.validate_check_doc(_doc())
    assert out["applies"] == ["chats", "tasks", "delegations"]
    assert out["rounds"] == d.DEFAULT_ROUNDS and out["mandatory"] is False
    assert out["condition"] == {} and out["inputs"] == []
    assert out["judge"]["judge_on"] == "auto" and out["judge"]["timeout"] == d.JUDGE_TIMEOUT_DEFAULT
    assert out["judge"]["mcps"] == [] and out["judge"]["threshold"] is None
    full = d.validate_check_doc(_doc(
        mandatory=True, applies=["chats"], rounds=1, inputs=["knowledge/style.md"],
        condition={"kinds": ["code"], "events": ["commit", "tool:mcp__x__y"], "places": ["git"],
                   "globs": ["workspace/src/**"], "commands": [r"make\s+deploy"]},
        schema={"type": "object", "required": ["ok"]},
        script={"run": "lint.sh", "timeout": 30},
        handler={"app": "review-app", "handler": "judge"},
        judge={"rubric": "r", "engine": "codex-cli", "model": "gpt-5", "threshold": 0.7,
               "mcps": ["file-tools"], "judge_on": "platform", "timeout": 120},
    ))
    assert full["mandatory"] and full["applies"] == ["chats"] and full["rounds"] == 1
    assert full["condition"]["events"] == ["commit", "tool:mcp__x__y"]
    assert full["script"] == {"run": "lint.sh", "timeout": 30}
    assert full["judge"]["threshold"] == 0.7 and full["judge"]["judge_on"] == "platform"
    # A user's own check is never mandatory.
    assert d.validate_check_doc(_doc(mandatory=True), owner="alice")["mandatory"] is False


@pytest.mark.parametrize("bad, words", [
    ({"name": "Bad Name", "judge": {"rubric": "r"}}, "name"),
    ({"name": "x", "extra": 1, "judge": {"rubric": "r"}}, "unknown keys"),
    ({"name": "x"}, "at least one of"),
    ({"name": "x", "applies": ["phone"], "judge": {"rubric": "r"}}, "applies"),
    ({"name": "x", "rounds": 9, "judge": {"rubric": "r"}}, "rounds"),
    ({"name": "x", "condition": {"kinds": ["blob"]}, "judge": {"rubric": "r"}}, "kinds"),
    ({"name": "x", "condition": {"events": ["deploy"]}, "judge": {"rubric": "r"}}, "events"),
    ({"name": "x", "condition": {"commands": ["("]}, "judge": {"rubric": "r"}}, "pattern"),
    # RE2: no backreference, no lookaround, a bounded length — refused at
    # save with RE2's own words.
    ({"name": "x", "condition": {"commands": [r"(a)\1"]}, "judge": {"rubric": "r"}}, "RE2"),
    ({"name": "x", "condition": {"commands": [r"git (?!status)"]}, "judge": {"rubric": "r"}}, "RE2"),
    ({"name": "x", "condition": {"commands": ["a" * 600]}, "judge": {"rubric": "r"}}, "512"),
    ({"name": "x", "condition": {"globs": ["*" * 300]}, "judge": {"rubric": "r"}}, "globs"),
    ({"name": "x", "schema": {"type": "string", "pattern": "^(?=a)"}}, "RE2"),
    ({"name": "x", "schema": {"properties": {"a": {"patternProperties": {"(?<=x)y": {}}}}}}, "RE2"),
    ({"name": "x", "schema": {"patternProperties": {"^a": {}}, "unevaluatedProperties": False}},
     "unevaluatedProperties"),
    ({"name": "x", "inputs": ["../etc/passwd"], "judge": {"rubric": "r"}}, "inputs"),
    ({"name": "x", "inputs": ["/etc/passwd"], "judge": {"rubric": "r"}}, "inputs"),
    ({"name": "x", "script": {"run": "../x.sh"}}, "script.run"),
    ({"name": "x", "script": {"run": "check.json"}}, "script.run"),
    ({"name": "x", "script": {"run": "a.sh", "timeout": 99999}}, "timeout"),
    ({"name": "x", "judge": {"rubric": ""}}, "rubric"),
    ({"name": "x", "judge": {"rubric": "r", "threshold": 2}}, "threshold"),
    ({"name": "x", "judge": {"rubric": "r", "judge_on": "moon"}}, "judge_on"),
    ({"name": "x", "judge": {"rubric": "r", "engine": "gpt"}}, "engine"),
    ({"name": "x", "judge": {"rubric": "r", "timeout": 5}}, "timeout"),
    ({"name": "x", "judge": {"rubric": "r", "mcps": ["file-tools", "memory-mcp"]}}, "never gets memory-mcp"),
    ({"name": "x", "judge": {"rubric": "r", "mcps": ["checks-mcp"]}}, "never gets checks-mcp"),
    ({"name": "x", "schema": {"type": "nothing"}}, "schema"),
    ({"name": "x", "handler": {"app": "A B", "handler": "h"}}, "handler"),
    ({"name": "x", "mandatory": "yes", "judge": {"rubric": "r"}}, "mandatory"),
])
def test_validation_refuses(bad, words):
    with pytest.raises(d.CheckError) as e:
        d.validate_check_doc(bad)
    assert words in str(e.value)


def test_inputs_stay_in_the_trees_the_check_may_name():
    # A person's check names its own tree; an agent check names no one's;
    # a hidden segment (the credential dirs, the CLI state) is never an input.
    mine = d.validate_check_doc(_doc(inputs=["users/alice/notes.md", "knowledge/style.md"]),
                                owner="alice")
    assert mine["inputs"] == ["users/alice/notes.md", "knowledge/style.md"]
    for owner, path in [("alice", "users/bob/workspace/private.md"),
                        ("alice", "users/alice"),
                        ("", "users/alice/notes.md"),
                        ("alice", "users/alice/.credentials/github.json"),
                        ("", "knowledge/.credentials/token.json"),
                        ("", "workspace/repo/.git/config")]:
        with pytest.raises(d.CheckError) as e:
            d.validate_check_doc(_doc(inputs=[path]), owner=owner)
        assert "inputs" in str(e.value), (owner, path)


def test_write_load_index_and_hand_edit(tree):
    it = d.write_check(AGENT, "", _doc(script={"run": "lint.sh"}), "#!/bin/sh\necho ok\n",
                       updated_by="alice-sub")
    folder = tree / "config" / "checks" / "coding"
    assert (folder / "check.json").is_file() and (folder / "lint.sh").is_file()
    assert it.ref == "agent:coding" and it.script_sha256 and not it.problems
    row = db_checks.get_index(AGENT, "", "coding")
    assert row["updated_by"] == "alice-sub" and row["doc_sha256"] == it.doc_sha256
    assert row["script_sha256"] == it.script_sha256
    # The config repo tracks it.
    assert (tree / "config" / ".git").is_dir()
    # A hand edit: the index follows the file and says so.
    doc = json.loads((folder / "check.json").read_text())
    doc["description"] = "edited by hand"
    (folder / "check.json").write_text(json.dumps(doc))
    items = d.load_checks(AGENT, "")
    assert items[0].doc["description"] == "edited by hand"
    assert db_checks.get_index(AGENT, "", "coding")["updated_by"] == "file"
    # A broken hand edit is listed with its problem and the row is kept.
    (folder / "check.json").write_text("{not json")
    items = d.load_checks(AGENT, "")
    assert items[0].problems and db_checks.get_index(AGENT, "", "coding") is not None
    assert d.resolve_ref(AGENT, "coding", "alice") is None
    # Removing the folder drops the row.
    assert d.delete_check(AGENT, "", "coding")
    assert d.load_checks(AGENT, "") == [] and db_checks.get_index(AGENT, "", "coding") is None
    assert not d.delete_check(AGENT, "", "coding")


def test_a_folder_missing_once_keeps_its_row(tree):
    # A rename window (a sync writing temp-and-rename) must not drop the row
    # and re-add it as "file": the drop needs two loads in a row.
    d.write_check(AGENT, "", _doc(name="tone"), None, updated_by="alice-sub")
    folder = tree / "config" / "checks" / "tone"
    aside = tree / "config" / "tone-aside"
    folder.rename(aside)
    assert d.load_checks(AGENT, "") == []
    assert db_checks.get_index(AGENT, "", "tone")["updated_by"] == "alice-sub"
    aside.rename(folder)
    items = d.load_checks(AGENT, "")
    assert [it.name for it in items] == ["tone"]
    assert db_checks.get_index(AGENT, "", "tone")["updated_by"] == "alice-sub"
    # Gone for two loads: dropped.
    folder.rename(aside)
    d.load_checks(AGENT, "")
    d.load_checks(AGENT, "")
    assert db_checks.get_index(AGENT, "", "tone") is None


def test_a_script_section_needs_its_file(tree):
    with pytest.raises(d.CheckError) as e:
        d.write_check(AGENT, "", _doc(script={"run": "lint.sh"}), None, updated_by="alice-sub")
    assert "send its text" in str(e.value)
    with pytest.raises(d.CheckError):
        d.write_check(AGENT, "", _doc(), "echo", updated_by="alice-sub")  # text without a section


def test_a_users_own_checks_and_the_refs(tree):
    d.write_check(AGENT, "", _doc(name="coding", mandatory=True), None, updated_by="alice-sub")
    mine = d.write_check(AGENT, "alice", _doc(name="tone", mandatory=True), None, updated_by="alice-sub")
    assert mine.ref == "user:tone" and mine.mandatory is False
    assert (tree / "users" / "alice" / "checks" / "tone" / "check.json").is_file()
    assert d.resolve_ref(AGENT, "agent:coding", "alice").ref == "agent:coding"
    assert d.resolve_ref(AGENT, "user:tone", "alice").ref == "user:tone"
    assert d.resolve_ref(AGENT, "tone", "alice").ref == "user:tone"
    assert d.resolve_ref(AGENT, "user:tone", "bob") is None
    assert d.resolve_ref(AGENT, "user:coding", "alice") is None
    assert d.resolve_ref(AGENT, "coding", "") .ref == "agent:coding"
    assert [r["name"] for r in db_checks.list_index(AGENT)] == ["coding", "tone"]
    assert d.describe(mine)["sections"] == ["judge"]


def test_the_name_in_the_document_must_be_the_folders(tree):
    folder = tree / "config" / "checks" / "other"
    folder.mkdir(parents=True)
    (folder / "check.json").write_text(json.dumps(_doc(name="coding")))
    items = d.load_checks(AGENT, "")
    assert items[0].name == "other" and "not the folder's" in items[0].problems[0]


def test_symlinked_and_odd_folders_are_ignored(tree):
    root = tree / "config" / "checks"
    (root / "Bad Name").mkdir(parents=True)
    (root / "Bad Name" / "check.json").write_text("{}")
    real = tree / "workspace" / "elsewhere"
    real.mkdir(parents=True)
    (real / "check.json").write_text(json.dumps(_doc(name="linked")))
    (root / "linked").symlink_to(real)
    assert d.load_checks(AGENT, "") == []
    # By name too (a ref, an attach), and nothing is written through it.
    assert d.resolve_ref(AGENT, "agent:linked", "") is None
    with pytest.raises(d.CheckError):
        d.write_check(AGENT, "", _doc(name="linked", description="rewritten"), None, updated_by="x")
    assert json.loads((real / "check.json").read_text())["description"] == "Lint and a look"
