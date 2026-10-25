"""The Claude CLI truncates MCP tool descriptions at 2048 characters, so
anything past that never reaches the model. The three document writers keep
their parameter shapes in a `help` op and a summary under the cap that names
every operation; a writer's catalogue and its dispatcher are one table, held
here to the description, so an operation can never fall out of the model's
view."""

import ast
import asyncio
import re
import sys
from pathlib import Path

import pytest

_CAP = 2048
_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))


def _descriptions() -> dict[str, str]:
    src = (_HERE / "server.py").read_text()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Tool":
            kw = {k.arg: k.value for k in node.keywords}
            out[ast.literal_eval(kw["name"])] = ast.literal_eval(kw["description"])
    return out


def _catalogue(module: str) -> set[str]:
    mod = __import__(module)
    return {name for _, ops in mod._HELP_GROUPS for name, _ in ops}


def test_write_xlsx_description_fits_the_cli_cap():
    d = _descriptions()["write_xlsx"]
    assert len(d) <= _CAP - 40, len(d)  # margin for a stray edit
    assert '{"op": "help"}' in d and "anchor" in d and "DATES" in d


def test_no_tool_is_over_the_cap():
    over = {name: len(d) for name, d in _descriptions().items() if len(d) > _CAP - 40}
    assert over == {}


@pytest.mark.parametrize("tool, module", [("write_docx", "word"), ("write_pptx", "powerpoint")])
def test_the_description_names_every_operation_and_the_help_op(tool, module):
    pytest.importorskip("docx" if module == "word" else "pptx")
    d = _descriptions()[tool]
    assert '{"op": "help"}' in d
    missing = [name for name in sorted(_catalogue(module))
               if not re.search(rf"\b{name}\b", d)]
    assert missing == []


@pytest.mark.parametrize("module, aliases", [
    ("word", set()), ("powerpoint", {"set_background_color"}),
])
def test_the_catalogue_is_the_dispatcher(module, aliases):
    pytest.importorskip("docx" if module == "word" else "pptx")
    mod = __import__(module)
    assert set(mod._HANDLERS) == _catalogue(module) | aliases
    for name, handler in mod._HANDLERS.items():
        assert callable(handler) and handler.__module__ == module, name


def test_the_xlsx_catalogue_is_the_dispatcher():
    """`_OPS` is write_xlsx's catalogue (keys, help); every operation but
    help has its handler, and every one sits in exactly one help group."""
    import excel

    assert set(excel._HANDLERS) | {"help"} == set(excel._OPS)
    grouped = [name for _, names in excel._OP_GROUPS for name in names]
    assert sorted(grouped) == sorted(excel._OPS)


@pytest.mark.parametrize("tool, module", [("edit_pdf", "pdf"), ("edit_image", "images")])
def test_the_editors_descriptions_name_every_operation_they_dispatch(tool, module):
    pytest.importorskip("fitz" if module == "pdf" else "PIL")
    mod = __import__(module)
    d = _descriptions()[tool]
    missing = [name for name in sorted(mod._HANDLERS) if not re.search(rf"\b{name}\b", d)]
    assert missing == []


async def _refuse(*a, **k):
    raise AssertionError("a help-only call resolved the path")


@pytest.mark.parametrize("module, handler", [
    ("word", "handle_write_docx"), ("powerpoint", "handle_write_pptx"),
])
def test_help_alone_writes_nothing(monkeypatch, tmp_path, module, handler):
    pytest.importorskip("docx" if module == "word" else "pptx")
    mod = __import__(module)
    monkeypatch.setattr(mod, "_resolve_path", _refuse)
    target = tmp_path / "never.doc"
    whole = asyncio.run(getattr(mod, handler)(
        {"path": str(target), "operations": [{"op": "help"}]}))
    assert not target.exists()
    for name in _catalogue(module):
        assert f"\n  {name}: " in whole, name
    one = asyncio.run(getattr(mod, handler)(
        {"path": str(target), "operations": [{"op": "help", "name": "add_image"}]}))
    assert one.startswith("add_image: ") and "image_path" in one and "\n" not in one
    unknown = asyncio.run(getattr(mod, handler)(
        {"path": str(target), "operations": '[{"op": "describe", "name": "add_photo"}]'}))
    assert unknown.startswith("No operation named 'add_photo'") and "add_image" in unknown


def test_help_beside_a_write_follows_its_result(monkeypatch, tmp_path):
    pytest.importorskip("docx")
    import word

    async def _ident(p, writing=False, **kw):
        return p

    async def _no_preview(path):
        pass

    monkeypatch.setattr(word, "_resolve_path", _ident)
    monkeypatch.setattr("shared._resolve_path", _ident)
    monkeypatch.setattr(word, "_push_preview", _no_preview)
    monkeypatch.setattr(word, "_to_agents_relative", lambda p: p)
    target = tmp_path / "a.docx"
    out = asyncio.run(word.handle_write_docx({"path": str(target), "operations": [
        {"op": "add_paragraph", "text": "hello"},
        {"op": "help", "name": "add_table"},
        {"op": "add_photo"},
    ]}))
    assert target.exists()
    assert out.index("Document saved") < out.index("add_table: headers")
    assert '''unknown operation 'add_photo' ({"op": "help"}''' in out


def test_help_names_an_alias_by_its_operation(monkeypatch):
    pytest.importorskip("pptx")
    import powerpoint
    monkeypatch.setattr(powerpoint, "_resolve_path", _refuse)
    out = asyncio.run(powerpoint.handle_write_pptx({"path": "x.pptx", "operations": [
        {"op": "help", "name": "set_background_color"}]}))
    assert out.startswith("set_background: ")
