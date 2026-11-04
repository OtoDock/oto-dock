"""The Claude CLI truncates MCP tool descriptions at 2048 characters, so
anything past that never reaches the model. The writers keep their
parameter shapes in a `help` op and a summary under the cap; a writer's
catalogue and its dispatcher are one table, held here to its help, so an
operation can never fall out of the model's view.

A writer built on `catalog.Catalog` exposes it as `CATALOG`; a writer not
yet moved onto it is read from its own tables (`_HELP_GROUPS` and
`_HANDLERS`, or write_xlsx's `_OPS` and `_OP_GROUPS`) until it moves."""

import asyncio
import re
import sys
from pathlib import Path

import pytest

_CAP = 2048
_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))

# The writer tools, their module and the library their tests need.
_WRITERS = [
    ("write_docx", "word", "docx"),
    ("write_xlsx", "excel", "openpyxl"),
    ("write_pptx", "powerpoint", "pptx"),
    ("edit_pdf", "pdf", "fitz"),
    ("edit_image", "images", "PIL"),
]
# Editors with no help op yet: their description names every operation.
_EDITORS = [("edit_pdf", "pdf", "fitz"), ("edit_image", "images", "PIL")]


def _descriptions() -> dict[str, str]:
    import server

    return {tool.name: tool.description for tool in server.TOOLS}


def _module(name: str, library: str):
    pytest.importorskip(library)
    return __import__(name)


def _groups(mod) -> dict[str, list[str]]:
    """Each help group's operations."""
    if hasattr(mod, "CATALOG"):
        return mod.CATALOG.groups()
    if hasattr(mod, "_OP_GROUPS"):
        return {group: list(names) for group, names in mod._OP_GROUPS}
    return {group: [name for name, _ in ops] for group, ops in mod._HELP_GROUPS}


def _dispatched(mod) -> set[str]:
    if hasattr(mod, "CATALOG"):
        return set(mod.CATALOG.ops)
    if hasattr(mod, "_OP_GROUPS"):
        return set(mod._HANDLERS)
    return set(mod._HANDLERS) - set(getattr(mod, "_ALIASES", {}))


def _whole_help(mod, handler: str, tmp_path, monkeypatch) -> str:
    monkeypatch.setattr("shared._resolve_path", _refuse)
    if hasattr(mod, "_resolve_path"):
        monkeypatch.setattr(mod, "_resolve_path", _refuse)
    return asyncio.run(getattr(mod, handler)({"path": str(tmp_path / "never.doc"),
                                              "operations": [{"op": "help"}]}))


async def _refuse(*a, **k):
    raise AssertionError("a help-only call resolved the path")


def test_write_xlsx_description_fits_the_cli_cap():
    d = _descriptions()["write_xlsx"]
    assert len(d) <= _CAP - 40, len(d)  # margin for a stray edit
    assert '{"op": "help"}' in d and "anchor" in d and "DATES" in d


def test_no_tool_is_over_the_cap():
    over = {name: len(d) for name, d in _descriptions().items() if len(d) > _CAP - 40}
    assert over == {}


@pytest.mark.parametrize("tool, module, library", _WRITERS)
def test_every_operation_is_in_help_and_in_exactly_one_group(tool, module, library, tmp_path,
                                                             monkeypatch):
    mod = _module(module, library)
    grouped = [name for names in _groups(mod).values() for name in names]
    assert sorted(grouped) == sorted(set(grouped)), "an operation sits in two groups"
    assert set(grouped) - {"help"} == _dispatched(mod) - {"help"}
    handler = {"word": "handle_write_docx", "excel": "handle_write_xlsx", "powerpoint": "handle_write_pptx",
               "pdf": "handle_edit_pdf", "images": "handle_edit_image"}[module]
    whole = _whole_help(mod, handler, tmp_path, monkeypatch)
    for name in _dispatched(mod) - {"help"}:
        assert len(re.findall(rf"\n  {re.escape(name)}: ", whole)) == 1, name


@pytest.mark.parametrize("tool, module, library", _WRITERS)
def test_the_description_names_the_help_op_and_a_catalogue_writer_its_groups(tool, module, library):
    mod = _module(module, library)
    d = _descriptions()[tool]
    assert '{"op": "help"}' in d
    if hasattr(mod, "CATALOG"):
        assert "best_effort" in d
        missing = [group for group in mod.CATALOG.groups() if group.lower() not in d.lower()]
        assert missing == []


@pytest.mark.parametrize("tool, module, library", _WRITERS)
def test_the_catalogue_is_the_dispatcher(tool, module, library):
    mod = _module(module, library)
    if hasattr(mod, "CATALOG"):
        for spec in mod.CATALOG.ops.values():
            home = Path(sys.modules[spec.handler.__module__].__file__).resolve()
            assert callable(spec.handler) and _HERE.resolve() in home.parents, spec.name
        return
    if hasattr(mod, "_OP_GROUPS"):
        assert set(mod._HANDLERS) | {"help"} == set(mod._OPS)
        return
    for name, handler in mod._HANDLERS.items():
        assert callable(handler) and handler.__module__ == module, name


@pytest.mark.parametrize("tool, module, library", _EDITORS)
def test_the_editors_descriptions_name_every_operation_they_dispatch(tool, module, library):
    mod = _module(module, library)
    if hasattr(mod, "CATALOG"):
        pytest.skip("an editor with a catalogue answers help; the writers' rule holds it")
    d = _descriptions()[tool]
    missing = [name for name in sorted(mod._HANDLERS) if not re.search(rf"\b{name}\b", d)]
    assert missing == []


@pytest.mark.parametrize("module, handler, library", [
    ("word", "handle_write_docx", "docx"), ("powerpoint", "handle_write_pptx", "pptx"),
])
def test_help_alone_writes_nothing(monkeypatch, tmp_path, module, handler, library):
    mod = _module(module, library)
    target = tmp_path / "never.doc"
    whole = _whole_help(mod, handler, tmp_path, monkeypatch)
    assert not target.exists()
    for name in _dispatched(mod):
        assert f"\n  {name}: " in whole, name
    one = asyncio.run(getattr(mod, handler)(
        {"path": str(target), "operations": [{"op": "help", "name": "add_image"}]}))
    assert one.startswith("add_image: ") and "image_path" in one
    if not hasattr(mod, "CATALOG") or mod.CATALOG.one_line_help:
        assert "\n" not in one
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

    for target in (word, sys.modules["shared"]):
        if hasattr(target, "_resolve_path"):
            monkeypatch.setattr(target, "_resolve_path", _ident)
        if hasattr(target, "_push_preview"):
            monkeypatch.setattr(target, "_push_preview", _no_preview)
    monkeypatch.setattr("shared._notify_file_written", _no_preview)
    target_file = tmp_path / "a.docx"
    out = asyncio.run(word.handle_write_docx({"path": str(target_file), "best_effort": True, "operations": [
        {"op": "add_paragraph", "text": "hello"},
        {"op": "help", "name": "add_table"},
        {"op": "add_photo"},
    ]}))
    assert target_file.exists()
    assert out.index(" saved: ") < out.index("add_table: headers")
    assert "add_photo" in out and '{"op": "help"' in out


def test_help_names_an_alias_by_its_operation(monkeypatch):
    pytest.importorskip("pptx")
    import powerpoint
    monkeypatch.setattr("shared._resolve_path", _refuse)
    out = asyncio.run(powerpoint.handle_write_pptx({"path": "x.pptx", "operations": [
        {"op": "help", "name": "set_background_color"}]}))
    assert out.startswith("set_background: ")


def test_the_descriptions_say_who_sees_what_and_name_real_tools():
    """The screenshots come back to the agent only; the display MCP's tool
    is display_images; the render sentence is true whichever engine
    draws."""
    d = _descriptions()
    assert ("The pages come back to you only, so you can review layout, sizing and content: the person does not "
            "see them. To show the person a document, use preview_document.") in d["screenshot_document"]
    assert "shown inline in the chat so you can review" not in d["screenshot_document"]
    for tool in ("create_chart", "edit_image"):
        assert "do NOT call display_images separately." in d[tool], tool
    assert not [name for name, text in d.items() if re.search(r"\bdisplay_image\b", text)]
    assert ("render: true also renders the file and runs the checks that read the rendered pages: a deck's text "
            "measured as it is drawn, and a workbook recalculated in a copy and printed, so a chart cut by a page "
            "break or columns that run onto another page are found (slower; use it on a finished deck or "
            "workbook).") in d["check_document"]


def test_the_descriptions_say_what_the_code_does():
    """Each sentence here once said the opposite of the code beside it."""
    d = _descriptions()
    assert ("(a Word document or a PDF holds no live chart; a workbook or deck gets one from add_chart in "
            "write_xlsx or write_pptx)") in d["create_chart"]
    assert "an element of 257+ attributes" in d["write_docx"]
    assert "fill_template ({{ key }} tokens)" in d["write_pptx"]
    assert "convert_format (png/jpeg/webp/tiff/bmp/gif)" in d["edit_image"]
    assert "PowerPoint (.pptx .pptm .potx .potm .ppsx .ppsm)" in d["check_document"]


def test_every_skill_a_description_names_ships_with_the_server():
    d = _descriptions()
    named = {(tool, name) for tool, text in d.items() for name in re.findall(r"skill `([a-z0-9-]+)`", text)}
    assert {tool for tool, _ in named} >= {"write_xlsx", "edit_image"}
    missing = [(tool, name) for tool, name in named if not (_HERE / "skills" / name / "SKILL.md").is_file()]
    assert missing == []
    assert "Recipes: the skill `photo-editing-guide`." in d["edit_image"]
