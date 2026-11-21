"""A list from a button and a declared file field: the schema's bounded shape, the
press's check of every item, the floor a list button gets, the file field
judged for the presser inside the app's folders, and the lint's sentence."""

from __future__ import annotations

import copy
import json

import pytest
from fastapi import HTTPException

import config
from auth.providers import UserContext
from services.apps import manifest as _mf

OPS = {"type": "array", "maxItems": 4, "items": {"oneOf": [
    {"type": "object", "properties": {
        "op": {"const": "update_clip"},
        "track": {"type": "string", "enum": ["video", "audio"]},
        "index": {"type": "integer", "minimum": 0, "maximum": 199},
        "volume_db": {"type": "number", "minimum": -60, "maximum": 12}},
     "required": ["index", "volume_db"]},
    {"type": "object", "properties": {
        "op": {"const": "set_transition"},
        "index": {"type": "integer", "minimum": 1, "maximum": 199},
        "transition": {"type": "string", "enum": ["cut", "fade"]},
        "src": {"type": "string", "maxLength": 300, "file": "read"}},
     "required": ["op", "index", "transition"]}]}}


def _schema(**props) -> dict:
    return {"type": "object", "properties": props}


def test_a_list_is_normalized_with_op_required_and_nothing_else_allowed():
    norm, err = _mf.validate_args_schema(_schema(operations=OPS), tool=True)
    assert err == ""
    variants = norm["properties"]["operations"]["items"]["oneOf"]
    assert variants[0]["required"] == ["op", "index", "volume_db"]
    assert variants[1]["required"] == ["op", "index", "transition"]
    assert all(v["additionalProperties"] is False for v in variants)
    assert variants[1]["properties"]["src"]["file"] == "read"
    # Validating the canonical form again gives it back unchanged.
    again, err = _mf.validate_args_schema(norm, tool=True)
    assert err == "" and again == norm


def test_a_schema_with_neither_signs_as_it_always_did():
    """The text an approved manifest of 1.9.1 was signed over: nothing new
    is added to a schema with no list and no file field."""
    norm, err = _mf.validate_args_schema(_schema(
        text={"type": "string", "maxLength": 9, "description": "d"},
        n={"type": "integer", "minimum": 0}, pick={"type": "string", "enum": ["a"]},
        on={"type": "boolean"}), tool=True)
    assert err == ""
    assert json.dumps(norm, sort_keys=True, separators=(",", ":")) == (
        '{"additionalProperties":false,"properties":{"n":{"minimum":0,"type":"integer"},'
        '"on":{"type":"boolean"},"pick":{"enum":["a"],"type":"string"},'
        '"text":{"description":"d","maxLength":9,"type":"string"}},"type":"object"}')


@pytest.mark.parametrize(("props", "tool", "fragment"), [
    ({"operations": OPS}, False, "a list is for a tool button only"),
    ({"a": OPS, "b": OPS}, True, "at most one list property"),
    ({"o": {**OPS, "maxItems": 0}}, True, "maxItems must be 1..32"),
    ({"o": {**OPS, "maxItems": 33}}, True, "maxItems must be 1..32"),
    ({"o": {k: v for k, v in OPS.items() if k != "maxItems"}}, True, "maxItems must be"),
    ({"o": {**OPS, "items": {"anyOf": []}}}, True, "items must be"),
    ({"o": {**OPS, "items": {"oneOf": []}}}, True, "oneOf must list 1..16 operations"),
    ({"o": {**OPS, "pattern": "x"}}, True, "unsupported keys"),
    ({"o": {**OPS, "items": {"oneOf": [OPS["items"]["oneOf"][0]] * 2}}}, True, "declared twice"),
    ({"o": {**OPS, "items": {"oneOf": [{"type": "object", "properties": {"op": {"enum": ["x"]}}}]}}},
     True, "op: {\"const\""),
    ({"o": {**OPS, "items": {"oneOf": [{"type": "object", "properties": {
        "op": {"const": "x"}, "nested": {"type": "object"}}}]}}}, True, "type must be one of"),
    ({"o": {**OPS, "items": {"oneOf": [{"type": "object", "properties": {
        "op": {"const": "x"}, "s": {"type": "string"}}}]}}}, True, "requires maxLength"),
    ({"o": {**OPS, "items": {"oneOf": [{"type": "object", "additionalProperties": True,
                                        "properties": {"op": {"const": "x"}}}]}}}, True, "takes no other fields"),
    ({"p": {"type": "integer", "file": "read"}}, True, "file is for a string"),
    ({"p": {"type": "string", "enum": ["a"], "file": "read"}}, True, "file is read or write"),
    ({"p": {"type": "string", "maxLength": 9, "file": "move"}}, True, "file is read or write"),
    ({"p": {"type": "string", "maxLength": 9, "file": "read"}}, False, "unsupported keys"),
])
def test_what_a_list_and_a_file_field_refuse(props, tool, fragment):
    norm, err = _mf.validate_args_schema(_schema(**props), tool=tool)
    assert norm is None and fragment in err, err


def test_every_item_of_a_press_is_held_to_its_operation():
    schema, _ = _mf.validate_args_schema(_schema(operations=OPS), tool=True)

    def check(items):
        return _mf.validate_args(schema, {"operations": items})

    good = [{"op": "update_clip", "index": 1, "volume_db": -6},
            {"op": "set_transition", "index": 2, "transition": "fade"}]
    out, err = check(good)
    assert err == "" and out == {"operations": good}
    assert out["operations"][0] is not good[0]   # a copy: a rewrite never touches the page's object
    assert "more than 4 items" in check(good * 3)[1]
    assert "must be a list" in check({"op": "update_clip"})[1]
    assert "op must be one of" in check([{"op": "delete_all"}])[1]
    assert "op must be one of" in check([{"index": 1}])[1]
    assert "unknown fields ['path']" in check([{**good[0], "path": "/x"}])[1]
    assert "missing fields ['volume_db']" in check([{"op": "update_clip", "index": 1}])[1]
    assert "above maximum" in check([{"op": "update_clip", "index": 1, "volume_db": 40}])[1]
    assert "not in enum" in check([{"op": "set_transition", "index": 1, "transition": "spin"}])[1]
    assert "must be an object" in check(["update_clip"])[1]


def _validate(action: dict) -> dict:
    canonical, err = _mf.validate_actions([action], "agent-x", True)
    assert err == "", err
    return json.loads(canonical)[0]


@pytest.fixture
def tool_mcp(monkeypatch):
    monkeypatch.setattr(_mf, "assigned_mcp_keys", lambda agent: {"video-tools": "video-tools"})


def _list_action(**over) -> dict:
    return {"id": "edit", "label": "Edit", "type": "mcp_tool", "mcp": "video-tools",
            "tool": "edit_composition", "fixed_args": {"path": "/workspace/s/edit.vproj.json"},
            "args_schema": _schema(operations=OPS), **over}


@pytest.mark.parametrize(("given", "stored"), [
    (None, "contributor"), ("viewer", "contributor"), ("editor", "editor"), ("external", "external")])
def test_a_list_button_gets_the_contributor_floor_unless_raised(tool_mcp, given, stored):
    action = _list_action(**({"min_role": given} if given else {}))
    entry = _validate(action)
    assert entry["min_role"] == stored
    # The canonical entry validates to itself.
    assert _validate(entry) == entry


def test_a_button_without_a_list_keeps_no_floor(tool_mcp):
    entry = _validate({"id": "probe", "label": "Probe", "type": "mcp_tool", "mcp": "video-tools",
                       "tool": "probe_media", "args_schema": _schema(n={"type": "integer"})})
    assert "min_role" not in entry


def test_the_file_fields_a_press_filled():
    action = {"args_schema": _mf.validate_args_schema(_schema(
        operations=OPS, out={"type": "string", "maxLength": 99, "file": "write"}), tool=True)[0]}
    validated = {"out": "workspace/s/a.mp4", "operations": [
        {"op": "update_clip", "index": 0, "volume_db": 1},
        {"op": "set_transition", "index": 1, "transition": "cut", "src": "workspace/s/b.mp4"}]}
    assert sorted(_mf.file_fields(action, validated), key=str) == sorted(
        [("out", None, "out", "write"), ("operations", 1, "src", "read")], key=str)


# ── The press judges a file field for the presser ────────────────────────


AGENT = "list-agent"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    for rel in ("workspace/studio/a.mp4", "workspace/other/b.mp4"):
        f = agents / AGENT / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents.resolve())
    return agents


ROW = {"id": "app-l", "agent": AGENT, "slug": "studio", "username": "", "owner_sub": None,
       "files": json.dumps({"read": ["workspace/studio"], "write": ["workspace/studio"]})}


def _person(role: str, *, member: bool = True) -> UserContext:
    return UserContext(sub=f"{role}-sub", email=f"{role}@t", name=role, role="member",
                       agents=[AGENT] if member else [], agent_roles={AGENT: role} if member else {})


def _field_action(mode: str) -> dict:
    return {"args_schema": _mf.validate_args_schema(
        _schema(path={"type": "string", "maxLength": 300, "file": mode}), tool=True)[0]}


def _judge(action, value, user, *, actor="", handler=""):
    from api.apps.app_actions import _judge_file_fields
    return _judge_file_fields(ROW, action, {"path": value}, user, actor, handler)


def test_a_file_field_is_given_to_the_tool_as_the_checked_place(tree):
    out = _judge(_field_action("read"), "workspace/studio/a.mp4", _person("viewer"))
    assert out == {"path": "/workspace/studio/a.mp4"}
    out = _judge(_field_action("write"), "/workspace/studio/new.mp4", _person("editor"))
    assert out == {"path": "/workspace/studio/new.mp4"}


@pytest.mark.parametrize(("mode", "value", "user", "fragment"), [
    ("read", "workspace/other/b.mp4", "editor", "outside the app's declared read prefixes"),
    ("read", "workspace/studio/../other/b.mp4", "editor", ""),
    ("write", "workspace/studio/new.mp4", "viewer", ""),
    ("read", "workspace/studio/a.mp4", "stranger", "the agent's files are for its members"),
])
def test_a_file_field_beyond_the_presser_is_refused(tree, mode, value, user, fragment):
    person = _person("viewer", member=False) if user == "stranger" else _person(user)
    with pytest.raises(HTTPException) as refused:
        _judge(_field_action(mode), value, person)
    assert refused.value.status_code == 403 and fragment in refused.value.detail


def test_a_file_field_on_a_shared_link_is_refused(tree):
    from api.apps.app_actions import FILE_ON_A_LINK
    with pytest.raises(HTTPException) as refused:
        _judge(_field_action("read"), "workspace/studio/a.mp4", None, actor="share:s1")
    assert refused.value.detail == FILE_ON_A_LINK


def test_the_apps_own_handler_is_judged_as_the_app(tree):
    out = _judge(_field_action("write"), "workspace/studio/x.mp4", None,
                 actor="delivery:d1", handler="digest")
    assert out == {"path": "/workspace/studio/x.mp4"}


def test_a_list_items_file_field_is_judged_and_rewritten_in_a_copy(tree):
    from api.apps.app_actions import _judge_file_fields
    action = {"args_schema": _mf.validate_args_schema(_schema(operations=OPS), tool=True)[0]}
    item = {"op": "set_transition", "index": 1, "transition": "cut", "src": "workspace/studio/a.mp4"}
    validated = {"operations": [item]}
    out = _judge_file_fields(ROW, action, validated, _person("viewer"), "", "")
    assert out["operations"][0]["src"] == "/workspace/studio/a.mp4"
    assert item["src"] == "workspace/studio/a.mp4"
    with pytest.raises(HTTPException):
        _judge_file_fields(ROW, action, {"operations": [{**item, "src": "workspace/other/b.mp4"}]},
                           _person("viewer"), "", "")


# ── What check_app says ──────────────────────────────────────────────────


def test_check_app_warns_on_a_list_button_outside_people_may_press():
    from services.apps import app_lint
    open_list = json.dumps([_list_action(min_role="external")])
    floored = json.dumps([_list_action(min_role="contributor"), _list_action(id="raw")])
    warned = [f for f in app_lint.lint_html("<p>x</p>", open_list) if f.rule == "app.action-floor"]
    assert len(warned) == 1 and "sends a list of edits" in warned[0].message
    assert not [f for f in app_lint.lint_html("<p>x</p>", floored) if f.rule == "app.action-floor"]


def test_check_app_warns_on_a_file_field_with_no_folder_of_its_kind():
    from services.apps import app_lint
    write = _list_action(min_role="editor")
    write["args_schema"]["properties"]["path"] = {"type": "string", "file": "write"}
    read = {"id": "peek", "label": "Peek", "type": "mcp_tool", "mcp": "file-tools", "tool": "read_document",
            "min_role": "editor",
            "args_schema": {"properties": {"path": {"type": "string", "file": "read"}}}}
    found = [f for f in app_lint.lint_html("<p>x</p>", json.dumps([write, read]))
             if f.rule == "app.file-folder"]
    # The list's own operations name a file to read (OPS), the top level one to write.
    assert sorted(f.message for f in found) == [
        "The button edit names a file, but the app declares no folder it may read. Add one to its files block.",
        "The button edit names a file, but the app declares no folder it may write. Add one to its files block.",
        "The button peek names a file, but the app declares no folder it may read. Add one to its files block."]

    def warned(files):
        doc = {"actions": [write, read], "files": files}
        return sorted(f.message.split(" names")[0] for f in app_lint._lint_manifest(
            json.dumps(doc), app_lint.declared_from_doc(doc)) if f.rule == "app.file-folder")
    # A write folder serves a read too, a read folder never a write.
    assert warned({"write": ["out"]}) == []
    assert warned({"read": ["media"]}) == ["The button edit"]
    # A file field inside a list's operation counts: OPS names one, nothing else does.
    assert [f.message for f in app_lint.lint_html("<p>x</p>", json.dumps([_list_action(min_role="editor")]))
            if f.rule == "app.file-folder"] == [
        "The button edit names a file, but the app declares no folder it may read. Add one to its files block."]
    plain = copy.deepcopy(_list_action(min_role="editor"))
    del plain["args_schema"]["properties"]["operations"]["items"]["oneOf"][1]["properties"]["src"]
    assert not [f for f in app_lint.lint_html("<p>x</p>", json.dumps([plain])) if f.rule == "app.file-folder"]


def test_only_a_schema_with_a_list_may_pass_the_older_size_cap():
    """5 KB of choices: refused without a list, as 1.9.1 refuses it, for any
    kind of button; admitted beside a list, which a 1.9.1 platform refuses
    anyway."""
    wide = {"type": "string", "enum": [f"{i:03d}" + "x" * 96 for i in range(50)]}
    for tool in (False, True):
        assert _mf.validate_args_schema(_schema(choice=wide), tool=tool) == (None, "args_schema too large")
    norm, err = _mf.validate_args_schema(_schema(choice=wide, operations=OPS), tool=True)
    assert err == "" and norm is not None
