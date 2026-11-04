"""The operation catalogue: every resolve rule, the construction refusals,
help, and the path resolution that runs before any worker."""

import asyncio
import pickle
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from catalog import Catalog, KeySpec, OpSpec
from results import Code, OpError, ResolvedPath


def _h(job, ctx, op):
    pass


def _catalog(**kwargs):
    return Catalog("write_test", [
        OpSpec("add_table", _h, (
            KeySpec("headers", ("list",), required=True, help="column titles"),
            KeySpec("rows", ("list",), default=[]),
            KeySpec("style", ("str",), default="Table Grid"),
            KeySpec("color", ("color",), aliases=("colour", "font_color")),
            KeySpec("level", ("int",)),
            KeySpec("scale", ("float",)),
            KeySpec("bold", ("bool",)),
            KeySpec("width", ("length",), unit="in"),
            KeySpec("spacing", ("spacing",), unit="x"),
            KeySpec("type", ("str",)),
            KeySpec("header", ("dict",), fields=(
                KeySpec("fill", ("color",)), KeySpec("bold", ("bool",)), KeySpec("size", ("int",), required=True),
            )),
            KeySpec("series", ("list",), item=(KeySpec("name", ("str",)), KeySpec("values", ("list",), required=True))),
            KeySpec("widths", ("list",), of=("length",), unit="in"),
            KeySpec("text", ("str",)),
            KeySpec("place", ("int", "str")),
        ), "TABLES", aliases=("table", "insert_table"), note="appends a table",
            examples=({"op": "add_table", "headers": ["a"]},)),
        OpSpec("add_image", _h, (
            KeySpec("image_path", ("path",), required=True, aliases=("image", "path")),
            KeySpec("save_to", ("out_path",)),
            KeySpec("files", ("list",), of=("path",)),
            KeySpec("frame", ("dict",), fields=(KeySpec("source", ("path",)),)),
        ), "IMAGES"),
        OpSpec("write_cells", _h, (
            KeySpec("cells", ("list",)), KeySpec("data", ("list",)), KeySpec("sheet", ("str",)),
        ), "CELLS", one_of=(("cells", "data"),)),
        OpSpec("xml_patch", _h, (
            KeySpec("action", ("str",), required=True), KeySpec("xpath", ("str",), required=True),
        ), "RAW"),
        OpSpec("add_annotation", _h, (
            KeySpec("annotation_type", ("str",), aliases=("type",)),
        ), "PAGES"),
    ], key_aliases={"sheet_name": "sheet"}, **kwargs)


def _resolve(raw, **kw):
    return _catalog().resolve(raw, **kw)


def _error(raw, **kw) -> OpError:
    with pytest.raises(OpError) as caught:
        _resolve(raw, **kw)
    return caught.value


# ------------------------------------------------------------------ dispatch

def test_an_explicit_op_wins_and_type_is_then_an_ordinary_key():
    _, op, notes = _resolve({"op": "add_table", "type": "bar", "headers": ["a"]})
    assert op == {"op": "add_table", "type": "bar", "headers": ["a"]} and notes == []


def test_a_type_naming_the_op_is_a_dispatch_echo():
    _, op, _ = _resolve({"op": "add_table", "type": "add_table", "headers": ["a"]})
    assert "type" not in op


@pytest.mark.parametrize("key", ["op", "operation", "action", "type"])
def test_any_dispatch_key_names_the_op(key):
    assert _resolve({key: "add_table", "headers": ["a"]})[1]["op"] == "add_table"


def test_op_names_are_case_folded_and_aliases_resolve():
    assert _resolve({"op": " Add_Table ", "headers": ["a"]})[1]["op"] == "add_table"
    assert _resolve({"op": "INSERT_TABLE", "headers": ["a"]})[1]["op"] == "add_table"


def test_a_dispatch_key_the_op_declares_stays_its_key():
    _, op, _ = _resolve({"type": "xml_patch", "action": "replace", "xpath": "/a"})
    assert op == {"op": "xml_patch", "action": "replace", "xpath": "/a"}
    _, op, _ = _resolve({"op": "add_annotation", "type": "underline"})
    assert op == {"op": "add_annotation", "annotation_type": "underline"}


def test_naming_the_op_with_its_own_key_says_to_use_op():
    error = _error({"action": "xml_patch", "xpath": "/a"})
    assert error.code is Code.MISSING_KEY and "under 'op'" in error.hint


def test_another_dispatch_key_is_dropped_with_a_note():
    _, op, notes = _resolve({"op": "add_image", "action": "insert", "image_path": "x.png"})
    assert "action" not in op and notes == ["'action' ignored: the operation is named by 'op'"]


def test_a_misspelled_op_is_not_overruled_by_a_later_key():
    error = _error({"op": "add_tabel", "type": "add_table", "headers": ["a"]})
    assert error.code is Code.UNKNOWN_OP and "add_tabel" in error.message


def test_two_later_keys_naming_two_ops_are_refused():
    error = _error({"action": "add_table", "type": "add_image", "headers": ["a"]})
    assert error.code is Code.UNKNOWN_OP and "under 'op'" in error.hint


@pytest.mark.parametrize("raw", [{}, {"op": ""}, {"op": None}, {"op": 5}, {"headers": []}])
def test_an_item_naming_no_operation_fails(raw):
    assert _error(raw).code is Code.UNKNOWN_OP


def test_an_unknown_op_suggests_the_closest():
    error = _error({"op": "add_tabel"})
    assert error.hint.startswith("Did you mean 'add_table'") and "Valid operations: add_table, add_image" in error.hint


def test_a_non_object_is_not_an_operation():
    assert _error(["op", "add_table"]).code is Code.BAD_ITEM


# ---------------------------------------------------------------------- keys

def test_unknown_keys_fail_together_with_suggestions():
    error = _error({"op": "add_table", "headers": ["a"], "colr": "red", "levle": 2})
    assert error.code is Code.UNKNOWN_KEY and error.op == "add_table"
    assert "'colr' (Did you mean 'color'?)" in error.message and "'levle' (Did you mean 'level'?)" in error.message
    assert error.hint.startswith("Accepted keys: headers, rows, style")


def test_an_unknown_key_fails_even_when_null():
    assert _error({"op": "add_table", "headers": ["a"], "colour_x": None}).code is Code.UNKNOWN_KEY


def test_prose_keys_are_dropped_with_a_note():
    _, op, notes = _resolve({"op": "add_table", "headers": ["a"], "comment": "make it red"})
    assert "comment" not in op and notes == ["'comment' ignored: it is prose, not a key of add_table"]


def test_aliases_map_and_the_canonical_spelling_wins():
    assert _resolve({"op": "add_table", "headers": ["a"], "colour": "red"})[1]["color"] == "red"
    _, op, notes = _resolve({"op": "add_table", "headers": ["a"], "colour": "red", "color": "blue"})
    assert op["color"] == "blue" and notes == ["'colour' ignored: 'color' was given too"]
    _, op, notes = _resolve({"op": "add_table", "headers": ["a"], "font_color": "red", "colour": "blue"})
    assert op["color"] == "red" and notes == ["'colour' ignored: 'color' was given too"]


def test_format_wide_key_aliases_apply_to_ops_that_declare_the_key():
    assert _resolve({"op": "write_cells", "cells": [], "sheet_name": "S"})[1]["sheet"] == "S"
    assert _error({"op": "add_table", "headers": ["a"], "sheet_name": "S"}).code is Code.UNKNOWN_KEY


def test_null_is_missing():
    _, op, _ = _resolve({"op": "add_table", "headers": ["a"], "style": None})
    assert "style" not in op
    assert _error({"op": "add_table", "headers": None}).code is Code.MISSING_KEY


def test_required_and_one_of():
    error = _error({"op": "add_image"})
    assert error.code is Code.MISSING_KEY and "image_path" in error.message
    error = _error({"op": "write_cells", "sheet": "S"})
    assert error.code is Code.ONE_OF and "cells or data" in error.message
    assert _resolve({"op": "write_cells", "data": [[1]]})[1]["data"] == [[1]]


def test_the_callers_dict_is_never_mutated():
    raw = {"op": "add_table", "headers": ["a"], "header": {"size": "12"}, "colour": "red"}
    before = {"op": "add_table", "headers": ["a"], "header": {"size": "12"}, "colour": "red"}
    _, op, _ = _resolve(raw)
    assert raw == before and op["header"] == {"size": 12}


# --------------------------------------------------------------------- types

@pytest.mark.parametrize("key, given, kept", [
    ("level", "2", 2), ("level", "12.0", 12), ("level", "1e3", 1000), ("level", 2.0, 2), ("level", " 7 ", 7),
    ("scale", 2, 2), ("scale", "1.5", 1.5), ("scale", "-3e-1", -0.3),
    ("bold", "true", True), ("bold", "FALSE", False), ("bold", 1, True), ("bold", 0, False),
    ("text", 12, "12"), ("text", 1.5, "1.5"), ("place", "end", "end"), ("place", "3", "3"), ("place", 3.0, 3),
    ("width", "2.5cm", "2.5cm"), ("width", 1.5, 1.5), ("width", "-0.5in", "-0.5in"),
    ("spacing", "150%", "150%"), ("color", [1, 1, 1], [1, 1, 1]), ("color", {"theme": "accent1"}, {"theme": "accent1"}),
])
def test_values_coerce_where_the_intent_is_plain(key, given, kept):
    _, op, _ = _resolve({"op": "add_table", "headers": ["a"], key: given})
    assert op[key] == kept and type(op[key]) is type(kept)


@pytest.mark.parametrize("key, given, words", [
    ("level", 2.5, "whole number"), ("level", "2.5", "whole number"), ("level", True, "whole number"),
    ("level", "1_000", "whole number"), ("level", "١٢", "whole number"), ("scale", float("nan"), "a number"),
    ("scale", "inf", "a number"), ("bold", "yes", "true or false"), ("bold", 2, "true or false"),
    ("text", True, "text"), ("headers", "a,b", "a list"), ("header", [1], "an object"),
])
def test_values_that_do_not_coerce_fail_naming_the_type(key, given, words):
    error = _error({"op": "add_table", "headers": ["a"], key: given} if key != "headers" else
                   {"op": "add_table", key: given})
    assert error.code is Code.BAD_TYPE and words in error.message


def test_a_comma_decimal_names_the_fix():
    error = _error({"op": "add_table", "headers": ["a"], "scale": "2,5"})
    assert error.code is Code.BAD_TYPE and "dot as the decimal separator" in error.hint
    error = _error({"op": "add_table", "headers": ["a"], "width": "2,5cm"})
    assert error.code is Code.BAD_VALUE and "dot as the decimal separator" in error.hint


def test_lengths_spacings_and_colours_are_validated_before_any_work():
    assert _error({"op": "add_table", "headers": ["a"], "width": "2em"}).code is Code.BAD_VALUE
    assert _error({"op": "add_table", "headers": ["a"], "spacing": "double"}).code is Code.BAD_VALUE
    error = _error({"op": "add_table", "headers": ["a"], "color": "blurple"})
    assert error.code is Code.BAD_VALUE and error.message.startswith("'color': ")


def test_a_catalogue_without_a_theme_refuses_theme_colours():
    catalog = _catalog(color_theme=False)
    with pytest.raises(OpError) as caught:
        catalog.resolve({"op": "add_table", "headers": ["a"], "color": "accent1"})
    assert caught.value.code is Code.BAD_VALUE


def test_nested_fields_items_and_scalar_lists():
    _, op, _ = _resolve({"op": "add_table", "headers": ["a"], "header": {"fill": "red", "bold": "true", "size": 9},
                         "series": [{"name": "A", "values": [1]}], "widths": [1, "2cm"]})
    assert op["header"] == {"fill": "red", "bold": True, "size": 9}
    error = _error({"op": "add_table", "headers": ["a"], "header": {"fil": "red", "size": 1}})
    assert error.code is Code.UNKNOWN_KEY and "'header.fil' (Did you mean 'fill'?)" in error.message
    error = _error({"op": "add_table", "headers": ["a"], "header": {"fill": "red"}})
    assert error.code is Code.MISSING_KEY and "header.size" in error.message
    error = _error({"op": "add_table", "headers": ["a"], "series": [{"name": "A", "values": [1]}, {"name": "B"}]})
    assert error.code is Code.MISSING_KEY and "series[1].values" in error.message
    error = _error({"op": "add_table", "headers": ["a"], "series": ["A"]})
    assert error.code is Code.BAD_TYPE and "series[0]" in error.message
    error = _error({"op": "add_table", "headers": ["a"], "widths": [1, "2em"]})
    assert error.code is Code.BAD_VALUE


def test_resolving_twice_gives_the_same_op():
    catalog = _catalog()
    _, first, _ = catalog.resolve({"type": "Table", "headers": ["a"], "level": "2", "colour": "red",
                                   "header": {"size": "3"}})
    _, second, notes = catalog.resolve(first)
    assert first == second and notes == []


def test_the_worker_refuses_a_path_the_parent_did_not_resolve():
    catalog = _catalog()
    error = None
    with pytest.raises(OpError) as caught:
        catalog.resolve({"op": "add_image", "image_path": "/raw/x.png"}, require_resolved_paths=True)
    error = caught.value
    assert error.code is Code.BAD_PATH and "not resolved" in error.message
    _, op, _ = catalog.resolve({"op": "add_image", "image_path": ResolvedPath("/agents/a/x.png")},
                               require_resolved_paths=True)
    assert isinstance(op["image_path"], ResolvedPath)
    with pytest.raises(OpError):
        catalog.resolve({"op": "add_image", "image_path": ResolvedPath("/a"), "files": ["/raw"]},
                        require_resolved_paths=True)
    with pytest.raises(OpError):
        catalog.resolve({"op": "add_image", "image_path": ResolvedPath("/a"), "frame": {"source": "/raw"}},
                        require_resolved_paths=True)


# -------------------------------------------------------------- construction

@pytest.mark.parametrize("ops, message", [
    ([OpSpec("a", _h, (), "G"), OpSpec("a", _h, (), "G")], "two operations"),
    ([OpSpec("a", _h, (), "G"), OpSpec("b", _h, (), "G", aliases=("a",))], "names both"),
    ([OpSpec("a", _h, (), "G", aliases=("describe",))], "help spelling"),
    ([OpSpec("help", _h, (), "G")], "help spelling"),
    ([OpSpec("a", _h, (KeySpec("x"), KeySpec("x")), "G")], "twice"),
    ([OpSpec("a", _h, (KeySpec("x"), KeySpec("y", aliases=("x",))), "G")], "collides"),
    ([OpSpec("a", _h, (KeySpec("x"),), "G", one_of=(("x", "z"),))], "undeclared"),
    ([OpSpec("a", _h, (KeySpec("x", required=True),), "G", examples=({"op": "a"},))], "does not resolve"),
])
def test_a_faulty_catalogue_is_refused(ops, message):
    with pytest.raises(ValueError, match=message):
        Catalog("t", ops)


def test_key_specs_check_themselves():
    with pytest.raises(ValueError, match="unknown type"):
        KeySpec("x", ("number",))
    with pytest.raises(ValueError, match="dict type"):
        KeySpec("x", ("str",), fields=(KeySpec("y"),))
    with pytest.raises(ValueError, match="required key has no default"):
        KeySpec("x", required=True, default=1)
    assert KeySpec("x", "int").type == ("int",)


def test_a_format_alias_colliding_with_a_declared_key_is_refused():
    with pytest.raises(ValueError, match="collides"):
        Catalog("t", [OpSpec("a", _h, (KeySpec("sheet"), KeySpec("sheet_name")), "G")],
                key_aliases={"sheet_name": "sheet"})


# ---------------------------------------------------------------------- help

def test_the_whole_catalogue():
    text = _catalog(notes="UNITS: inches.").help()
    lines = text.splitlines()
    assert lines[0].startswith('write_test operations: each is {"op": "<name>", ...keys}')
    assert "\nTABLES\n  add_table: headers (column titles), rows? (default []), style? (default 'Table Grid')" in text
    assert "width? (in)" in text and "— appends a table" in text
    assert "\nCELLS\n  write_cells: cells, data, sheet? — one of cells | data required" in text
    assert text.index("\nMETA\n  help: name?") < text.index("UNITS: inches.")


def test_one_operation_and_its_aliases():
    catalog = _catalog()
    one = catalog.help("table")
    assert one.splitlines()[0].startswith("add_table: headers")
    assert '  example: {"op": "add_table", "headers": ["a"]}' in one
    assert "  also accepted as: table, insert_table" in one and "  appends a table" in one
    assert catalog.help("help").startswith("help: name?")
    unknown = catalog.help("add_photo")
    assert unknown.startswith("No operation named 'add_photo'.") and "Valid operations:" in unknown


def test_one_line_help():
    assert "\n" not in _catalog(one_line_help=True).help("add_table")


@pytest.mark.parametrize("spelling", ["help", "describe", "describe_ops", "catalog", "catalogue", "list_ops", "?",
                                      "HELP"])
def test_help_spellings_and_name_keys(spelling):
    catalog = _catalog()
    assert catalog.is_help({"op": spelling})
    for name_key in ("name", "op_name", "operation_name", "topic"):
        answer = catalog.answer_help([{"op": spelling, name_key: "add_image"}])
        assert answer.startswith("add_image: image_path")
    assert not catalog.is_help({"op": "add_table"}) and not catalog.is_help("help")


def test_several_help_items_answer_in_order():
    answer = _catalog().answer_help([{"op": "help", "name": "add_image"}, {"op": "help", "name": "xml_patch"}])
    first, second = answer.split("\n\n")
    assert first.startswith("add_image:") and second.startswith("xml_patch:")


# --------------------------------------------------------------- batches

def test_resolve_batch_keeps_every_problem_and_the_notes():
    ops, problems, notes = _catalog().resolve_batch([
        (0, {"op": "add_table", "headers": ["a"], "comment": "x"}),
        (2, {"op": "nope"}),
        (5, {"op": "add_table", "colr": 1, "headers": []}),
    ])
    assert [i for i, _ in ops] == [0]
    assert [(p.index, p.code, p.op) for p in problems] == [(2, Code.UNKNOWN_OP, ""),
                                                           (5, Code.UNKNOWN_KEY, "add_table")]
    assert notes == {0: ["'comment' ignored: it is prose, not a key of add_table"]}


def _resolver(log):
    async def resolve(value, writing=False, **kw):
        log.append((value, writing))
        if "forbidden" in value:
            raise ValueError(f"Cannot open '{value}': outside this agent")
        return f"/agents/a/{value}"
    return resolve


def test_preresolve_marks_every_path_and_dedups(monkeypatch):
    log = []
    monkeypatch.setattr("shared._resolve_path", _resolver(log))
    catalog = _catalog()
    canonical, _, _ = catalog.resolve_batch([
        (0, {"op": "add_image", "image_path": "x.png", "save_to": "out.png", "files": ["x.png", "y.png"],
             "frame": {"source": "z.png"}}),
        (1, {"op": "add_table", "headers": ["a"]}),
    ])
    ops, problems = asyncio.run(catalog.preresolve(canonical, main_path="/agents/a/doc.docx"))
    assert problems == []
    first = ops[0][1]
    assert first["image_path"] == "/agents/a/x.png" and isinstance(first["image_path"], ResolvedPath)
    assert isinstance(first["save_to"], ResolvedPath) and all(isinstance(f, ResolvedPath) for f in first["files"])
    assert isinstance(first["frame"]["source"], ResolvedPath)
    assert ("out.png", True) in log and log.count(("x.png", False)) == 1
    assert ops[1] == (1, {"op": "add_table", "headers": ["a"]})
    assert catalog.path_values(ops, writing=True) == ["/agents/a/out.png"]
    assert sorted(catalog.path_values(ops, writing=False)) == ["/agents/a/x.png", "/agents/a/x.png",
                                                              "/agents/a/y.png", "/agents/a/z.png"]


def test_preresolve_fails_the_op_with_the_proxy_reason(monkeypatch):
    monkeypatch.setattr("shared._resolve_path", _resolver([]))
    catalog = _catalog()
    ops, problems = asyncio.run(catalog.preresolve([
        (3, {"op": "add_image", "image_path": "forbidden.png"}),
        (4, {"op": "add_image", "image_path": "fine.png"}),
    ]))
    assert [i for i, _ in ops] == [4]
    assert problems[0].index == 3 and problems[0].code is Code.BAD_PATH
    assert "outside this agent" in problems[0].message


def test_a_side_output_cannot_be_the_main_file(monkeypatch):
    monkeypatch.setattr("shared._resolve_path", _resolver([]))
    catalog = _catalog()
    _, problems = asyncio.run(catalog.preresolve(
        [(0, {"op": "add_image", "image_path": "x.png", "save_to": "doc.docx"})], main_path="/agents/a/doc.docx"))
    assert problems[0].code is Code.BAD_PATH and "cannot overwrite" in problems[0].message


def test_a_resolved_path_pickles_to_itself():
    value = pickle.loads(pickle.dumps(ResolvedPath("/agents/a/x")))
    assert isinstance(value, ResolvedPath) and value == "/agents/a/x"
    assert type(value.strip()) is str


def test_a_huge_integer_fails_its_op_instead_of_the_call():
    error = _error({"op": "add_table", "headers": ["a"], "scale": 10 ** 400})
    assert error.code is Code.BAD_TYPE and "a number" in error.message
    assert _error({"op": "add_table", "headers": ["a"], "width": 10 ** 400}).code is Code.BAD_VALUE
    assert _resolve({"op": "add_table", "headers": ["a"], "level": 10 ** 400})[1]["level"] == 10 ** 400
    ops, problems, _ = _catalog().resolve_batch([(0, {"op": "add_table", "headers": ["a"], "scale": 10 ** 400})])
    assert ops == [] and problems[0].index == 0
