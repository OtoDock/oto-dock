"""The key-coverage contract: every module with a CATALOG keeps it (none of
today's format modules has one yet; each picks it up when it moves), the
test format keeps it, and broken twins prove each kind of drift is caught."""

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
_SKIPPED_DIRS = {"venv", ".venv", "__pycache__", ".pytest_cache"}
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import contract
import toyfmt

from catalog import Catalog, KeySpec, OpSpec
from results import Code, OpError


@pytest.mark.parametrize("module", contract.catalogue_modules() or ["(none yet)"])
def test_every_catalogue_module_keeps_the_contract(module, tmp_path):
    if module == "(none yet)":
        pytest.skip("no format module has moved onto the catalogue yet")
    assert contract.check_module(module, str(tmp_path)) == []


def test_the_test_format_keeps_the_contract(tmp_path):
    fixtures = toyfmt.contract_fixtures(str(tmp_path))
    assert contract.run_examples(toyfmt.CATALOG, toyfmt.contract_job, str(tmp_path), fixtures) == []
    assert contract.static_problems(toyfmt.CATALOG, folder=_HERE) == []


def test_discovery_reads_source_only(tmp_path):
    files = {
        "a_fmt.py": "CATALOG = Catalog(\n    'x', [])\n",
        "b_fmt.py": "# CATALOG = Catalog( in a comment\nX = 1\n",
        "c_fmt.py": "CATALOG: Catalog = Catalog('x', [])\n",
        "d_fmt.py": "CATALOG = _build()\n",
        "formats/__init__.py": "",
        "formats/xlsx/__init__.py": "",
        "formats/xlsx/catalog.py": "CATALOG = Catalog('write_xlsx', OPS)\n",
        "formats/docx/__init__.py": "CATALOG = build()\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert contract.catalogue_modules(tmp_path) == ["a_fmt", "c_fmt", "d_fmt", "formats.docx",
                                                    "formats.xlsx.catalog"]


@pytest.mark.parametrize("module, library", [("word", "docx"), ("excel", "openpyxl"), ("powerpoint", "pptx"),
                                             ("pdf", "fitz"), ("images", "PIL")])
def test_a_writer_with_a_catalogue_is_discovered(module, library):
    """A writer the description tests treat as a catalogue writer is held to
    the contract too: discovery cannot miss it and skip silently."""
    pytest.importorskip(library)
    import importlib

    mod = importlib.import_module(module)
    if not hasattr(mod, "CATALOG"):
        pytest.skip(f"{module} has not moved onto the catalogue yet")
    owners = [importlib.import_module(name) for name in contract.catalogue_modules()]
    assert any(getattr(owner, "CATALOG", None) is mod.CATALOG for owner in owners)


# --------------------------------------------------------------- broken twins

def _reads_undeclared(job, ctx, op):
    job.lines.append(op["text"] + op.get("suffix", ""))


def _helper_reads(fmt):
    return fmt.get("font_colour", "")


def _reads_in_a_helper(job, ctx, op):
    job.lines.append(op["text"] + _helper_reads(op))


def _dynamic_key(job, ctx, op):
    name = ctx.op + "_text"
    job.lines.append(op.get(name, op["text"]))


def _copies(job, ctx, op):
    data = dict(op)
    job.lines.append(data["text"])


def _reads_an_alias(job, ctx, op):
    job.lines.append(op.get("words") or op["text"])


def _fails(job, ctx, op):
    raise OpError(Code.NOT_FOUND, "nothing to work on")


def _loops_over_literals(job, ctx, op):
    for side in ("top", "bottom"):
        key = f"margin_{side}"
        if key in op:
            job.lines.append(str(op[key]))
    for key, label in (("text", "T"), ("note", "N")):
        job.lines.append(label + str(op.get(key, "")))


def _hands_the_op_to_a_method(job, ctx, op):
    job.lines.append(op["text"])
    job.__dict__.update(op)


_TEXT = (KeySpec("text", ("str",), required=True),)


def _spec(name, handler, keys=_TEXT, **kw):
    return OpSpec(name, handler, keys, "TEST", examples=kw.pop("examples", ({"op": name, "text": "hi"},)), **kw)


@pytest.mark.parametrize("spec, runtime, static", [
    (_spec("undeclared", _reads_undeclared), "reads undeclared keys ['suffix']", "reads 'suffix'"),
    (_spec("helper", _reads_in_a_helper), "reads undeclared keys ['font_colour']", "reads 'font_colour'"),
    (_spec("dynamic", _dynamic_key), "reads undeclared keys ['dynamic_text']", "cannot resolve"),
    (_spec("copier", _copies), "reads the whole operation", "copies the operation"),
    (_spec("alias", _reads_an_alias, keys=(KeySpec("text", ("str",), required=True, aliases=("words",)),)),
     "reads undeclared keys ['words']", "reads 'words'"),
    (_spec("failing", _fails), "failed: nothing to work on", None),
    (_spec("method", _hands_the_op_to_a_method), "reads the whole operation", "cannot follow"),
])
def test_each_kind_of_drift_is_caught(spec, runtime, static, tmp_path):
    catalog = Catalog("t", [spec])
    found = contract.run_examples(catalog, toyfmt.contract_job, str(tmp_path))
    assert any(runtime in p for p in found), found
    found = contract.static_problems(catalog, folder=_HERE)
    if static is None:
        assert found == []
    else:
        assert any(static in p for p in found), found


def test_literal_loops_and_tuple_targets_resolve(tmp_path):
    keys = (KeySpec("text", ("str",)), KeySpec("note", ("str",)), KeySpec("margin_top", ("int",)),
            KeySpec("margin_bottom", ("int",)))
    catalog = Catalog("t", [_spec("loops", _loops_over_literals, keys=keys,
                                  examples=({"op": "loops", "text": "a", "margin_top": 1},))])
    assert contract.static_problems(catalog, folder=_HERE) == []
    assert contract.run_examples(catalog, toyfmt.contract_job, str(tmp_path)) == []
    narrower = Catalog("t", [_spec("loops", _loops_over_literals, keys=keys[:3],
                                   examples=({"op": "loops", "text": "a"},))])
    assert any("reads 'margin_bottom'" in p for p in contract.static_problems(narrower, folder=_HERE))


def test_an_op_without_examples_and_an_unexercised_one_of_branch_fail(tmp_path):
    catalog = Catalog("t", [
        OpSpec("bare", _reads_undeclared, (KeySpec("text", ("str",)),), "TEST"),
        OpSpec("either", toyfmt._add_line, (KeySpec("text", ("str",)), KeySpec("color", ("color",))), "TEST",
               one_of=(("text", "color"),), examples=({"op": "either", "text": "a"},)),
    ])
    found = contract.run_examples(catalog, toyfmt.contract_job, str(tmp_path))
    assert "bare: no example" in found and "either: no example gives color" in found


# ----------------------------------------------------- helpers and loops

def _named(fmt, name):
    return fmt.get(name, "")


def _named_default(fmt, name="text"):
    return fmt[name]


def _reads_through_a_key_helper(job, ctx, op):
    job.lines.append(_named(op, "text") + _named(op, name="note") + _named_default(op))


def _reads_an_undeclared_key_through_a_helper(job, ctx, op):
    job.lines.append(_named(op, "text") + _named(op, "suffix"))


def _loops_over_a_dict_literal(job, ctx, op):
    ranges = {"text": 1, "note": 2}
    for key, _width in ranges.items():
        job.lines.append(str(op.get(key, "")))
    for key in {"text": 0}:
        job.lines.append(str(op.get(key, "")))


def _picks_a_key_by_condition(job, ctx, op):
    key = "text" if ctx.op else "note"
    job.lines.append(op.get(key, ""))


def _nested_helper(job, ctx, op):
    def inner(values):
        return values.get("text", "")

    job.lines.append(inner(op))


def _imports_inside(job, ctx, op):
    from toyfmt import _add_line

    _add_line(job, ctx, op)


def _reads_whole(job, ctx, op):
    job.lines.append(",".join(f"{k}={v}" for k, v in op.items() if k != "op"))


_TEXT_NOTE = (KeySpec("text", ("str",), required=True), KeySpec("note", ("str",)))


@pytest.mark.parametrize("handler", [_reads_through_a_key_helper, _loops_over_a_dict_literal, _nested_helper,
                                     _picks_a_key_by_condition])
def test_helpers_given_a_literal_key_and_literal_loops_resolve(handler, tmp_path):
    catalog = Catalog("t", [_spec("h", handler, keys=_TEXT_NOTE)])
    assert contract.static_problems(catalog, folder=_HERE) == []
    assert contract.run_examples(catalog, toyfmt.contract_job, str(tmp_path)) == []


def test_a_key_handed_to_a_helper_must_be_declared():
    catalog = Catalog("t", [_spec("h", _reads_an_undeclared_key_through_a_helper, keys=_TEXT_NOTE)])
    found = contract.static_problems(catalog, folder=_HERE)
    assert any("reads 'suffix'" in p for p in found) and not any("cannot resolve" in p for p in found)


def test_a_helper_imported_inside_the_handler_is_followed():
    keys = (*_TEXT_NOTE, KeySpec("bold", ("bool",)), KeySpec("color", ("color",)))
    catalog = Catalog("t", [_spec("h", _imports_inside, keys=keys)])
    assert contract.static_problems(catalog, folder=_HERE) == []
    narrower = Catalog("t", [_spec("h", _imports_inside, keys=_TEXT_NOTE)])
    found = contract.static_problems(narrower, folder=_HERE)
    assert any("reads 'bold'" in p for p in found) and any("reads 'color'" in p for p in found)


def test_an_op_that_reads_the_whole_operation_may_say_so(tmp_path):
    strict = Catalog("t", [_spec("w", _reads_whole, keys=_TEXT_NOTE)])
    assert any("reads the whole operation" in p for p in contract.static_problems(strict, folder=_HERE))
    assert any("reads the whole operation" in p
               for p in contract.run_examples(strict, toyfmt.contract_job, str(tmp_path)))
    exempt = Catalog("t", [_spec("w", _reads_whole, keys=_TEXT_NOTE, reads_whole_op=True)])
    assert contract.static_problems(exempt, folder=_HERE) == []
    assert contract.run_examples(exempt, toyfmt.contract_job, str(tmp_path)) == []


def test_the_static_pass_follows_sibling_and_package_modules(tmp_path, monkeypatch):
    """``from m import f``, ``import m; m.f(op)`` and a relative import inside
    a package are followed into their modules."""
    files = {
        "zz_helpers.py": "def read_title(values):\n    return values.get('title', '')\n",
        "zz_more.py": "def read_size(values, key):\n    return values.get(key)\n",
        "zz_pkg/__init__.py": "",
        "zz_pkg/text_h.py": "def read_body(values):\n    return values['body']\n",
        "zz_pkg/handlers.py": (
            "import zz_more\n"
            "from zz_helpers import read_title\n"
            "from .text_h import read_body\n"
            "from . import text_h\n\n\n"
            "def handle(job, ctx, op):\n"
            "    return read_title(op), zz_more.read_size(op, 'size'), read_body(op), text_h.read_body(op)\n"
        ),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    handlers = importlib.import_module("zz_pkg.handlers")
    keys = (KeySpec("title", ("str",)), KeySpec("size", ("int",)), KeySpec("body", ("str",)))
    catalog = Catalog("t", [OpSpec("h", handlers.handle, keys, "T")])
    assert contract.static_problems(catalog, folder=tmp_path) == []
    narrower = Catalog("t", [OpSpec("h", handlers.handle, keys[:1], "T")])
    found = contract.static_problems(narrower, folder=tmp_path)
    assert any("reads 'size'" in p for p in found) and any("reads 'body'" in p for p in found)
    for name in ("zz_pkg.handlers", "zz_pkg.text_h", "zz_pkg", "zz_helpers", "zz_more"):
        sys.modules.pop(name, None)


# ------------------------------------------------ non-literal key arguments

def _get_sized(values, name="size"):
    return values.get(name)


def _loop_over_job_keys(job, ctx, op):
    for key in job.wanted:
        _get_sized(op, key)


def _keyword_from_job(job, ctx, op):
    _get_sized(op, name=job.key)


def _starred_from_job(job, ctx, op):
    _get_sized(op, *job.names)


def _double_starred_from_job(job, ctx, op):
    _get_sized(op, **job.options)


def _default_used(job, ctx, op):
    _get_sized(op)


@pytest.mark.parametrize("handler", [_loop_over_job_keys, _keyword_from_job, _starred_from_job,
                                     _double_starred_from_job])
def test_a_key_argument_the_call_does_not_give_as_a_literal_is_unresolved(handler):
    """A literal default never stands in for a value the call site passes
    non-literally (a loop variable over job data, a keyword, * or **)."""
    catalog = Catalog("t", [OpSpec("h", handler, (KeySpec("size", ("int",)),), "T")])
    found = contract.static_problems(catalog, folder=_HERE)
    assert any("cannot resolve" in p for p in found), found


def test_a_literal_default_still_counts_when_nothing_is_passed():
    catalog = Catalog("t", [OpSpec("h", _default_used, (KeySpec("size", ("int",)),), "T")])
    assert contract.static_problems(catalog, folder=_HERE) == []


# ------------------------------------------------------- dict-literal loops

def _reassigned_mapping(job, ctx, op):
    mapping = {"size": 1}
    if job.lines:
        mapping = job.mapping
    for key in mapping:
        op.get(key)


def _second_literal(job, ctx, op):
    mapping = {"size": 1}
    mapping = {"evil": 2}
    for key in mapping:
        op.get(key)


def _mutated_mapping(job, ctx, op):
    mapping = {"size": 1}
    mapping["evil"] = 2
    for key in mapping:
        op.get(key)


def _updated_mapping(job, ctx, op):
    mapping = {"size": 1}
    mapping.update(job.more)
    for key, _value in mapping.items():
        op.get(key)


def _aliased_mapping(job, ctx, op):
    mapping = {"size": 1}
    alias = mapping
    alias["evil"] = 2
    for key in mapping:
        op.get(key)


def _loop_aliased_mapping(job, ctx, op):
    mapping = {"size": 1}
    for found in (mapping,):
        found["evil"] = 2
    for key in mapping:
        op.get(key)


def _shadowed_parameter(job, ctx, op, mapping=None):
    if job.lines:
        mapping = {"size": 1}
    for key in mapping:
        op.get(key)


def _read_only_mapping(job, ctx, op):
    mapping = {"size": 1}
    if "size" in mapping and mapping["size"] and mapping.get("size"):
        for key in mapping.keys():
            op.get(key)


@pytest.mark.parametrize("handler", [_reassigned_mapping, _second_literal, _mutated_mapping, _updated_mapping,
                                     _aliased_mapping, _loop_aliased_mapping, _shadowed_parameter])
def test_a_dict_literal_counts_only_when_bound_once_and_never_changed(handler):
    catalog = Catalog("t", [OpSpec("h", handler, (KeySpec("size", ("int",)),), "T")])
    found = contract.static_problems(catalog, folder=_HERE)
    assert any("cannot resolve" in p or "reads 'evil'" in p for p in found), found


def test_a_dict_literal_read_but_never_handed_on_still_counts():
    catalog = Catalog("t", [OpSpec("h", _read_only_mapping, (KeySpec("size", ("int",)),), "T")])
    assert contract.static_problems(catalog, folder=_HERE) == []


def _copies_then_reads(job, ctx, op):
    data = dict(op)
    job.lines.append(data.get("text", ""))


def test_the_whole_op_exemption_covers_iteration_never_a_copy():
    exempt = Catalog("t", [_spec("c", _copies_then_reads, keys=_TEXT_NOTE, reads_whole_op=True)])
    found = contract.static_problems(exempt, folder=_HERE)
    assert any("copies the operation" in p for p in found), found


def test_every_source_and_test_file_compiles_without_a_warning():
    """An invalid escape (a backslash before a character Python gives no
    meaning there) in an ordinary string compiles with a SyntaxWarning
    today and is an error in a later Python."""
    import warnings

    folder = _HERE.parent
    files = [p for p in sorted(folder.rglob("*.py")) if _SKIPPED_DIRS.isdisjoint(p.parts)]
    warned = []
    for path in files:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as exc:
                warned.append(f"{path.relative_to(folder)}:{exc.lineno}: {exc.msg}")
    assert len(files) > 100 and warned == []
