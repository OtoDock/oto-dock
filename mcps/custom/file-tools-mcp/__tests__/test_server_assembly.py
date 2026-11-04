"""The assembler: every format module's tools listed in one order, each tool
routed to its handler, each extension to one reader, read_document's
options and description merged from the formats, and a server module light
enough for every worker child to re-run it."""

import asyncio
import importlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))

import server

_TODAY = (
    "read_document", "write_docx", "write_xlsx", "write_pptx", "write_pdf",
    "convert_document", "edit_pdf", "pdf_to_images", "screenshot_document",
    "images_to_pdf", "edit_image", "preview_document", "analyze_image", "create_chart",
)


def test_the_tools_keep_their_order_and_check_document_follows():
    names = [tool.name for tool in server.TOOLS]
    assert tuple(names[:len(_TODAY)]) == _TODAY
    assert names[len(_TODAY):] == ["check_document"]
    assert len(names) == len(set(names))


def test_every_tool_has_a_handler_that_exists():
    for tool in server.TOOLS:
        if tool.name in ("read_document", "preview_document"):
            continue
        module, function = server.HANDLER_TARGETS[tool.name]
        assert (_HERE / f"{module}.py").is_file(), tool.name
        source = (_HERE / f"{module}.py").read_text()
        assert f"async def {function}(" in source, tool.name


def test_a_tool_routes_through_the_table(monkeypatch):
    fake = types.ModuleType("fake_tool_target")

    async def handle(args):
        return f"handled {args['x']}"

    fake.handle = handle
    monkeypatch.setitem(sys.modules, "fake_tool_target", fake)
    monkeypatch.setitem(server.HANDLER_TARGETS, "fake_tool", ("fake_tool_target", "handle"))
    monkeypatch.setattr(server.TOOL_ARGS, "check", lambda name, args: (args, None))
    out = asyncio.run(server.call_tool("fake_tool", {"x": 7}))
    assert out[0].text == "handled 7"
    unknown = asyncio.run(server.call_tool("no_such_tool", {}))
    assert unknown[0].text == "Unknown tool: no_such_tool"


# Each format's extensions, exactly: a format lane that adds one adds it here.
_READERS = {
    "pdf_tool": {".pdf"},
    "xlsx_tool": {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"},
    "docx_tool": {".docx", ".dotx", ".docm", ".dotm", ".doc"},
    "pptx_tool": {".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".ppt"},
}


def test_each_extension_has_one_reader_and_the_formats_are_today_s():
    owners: dict[str, set[str]] = {}
    for ext, reader in server.READ_DISPATCH.items():
        assert reader.__name__ == "read" and reader.__module__.endswith("_tool"), ext
        owners.setdefault(reader.__module__, set()).add(ext)
    assert owners == _READERS


def test_two_modules_claiming_one_extension_fail_the_assembly(monkeypatch):
    twin = types.SimpleNamespace(__name__="twin_tool", READERS={".pdf": lambda p, o: ""},
                                 READ_PROPERTIES={}, READ_DESCRIPTION="")
    monkeypatch.setattr(server, "FORMAT_MODULES", (*server.FORMAT_MODULES, twin))
    with pytest.raises(RuntimeError, match="two readers for .pdf"):
        server._read_dispatch()


def test_read_options_merge_when_identical_and_fail_when_not(monkeypatch):
    same = types.SimpleNamespace(__name__="same_tool", READERS={},
                                 READ_PROPERTIES={"pages": dict(server.pdf_tool.READ_PROPERTIES["pages"])},
                                 READ_DESCRIPTION="")
    monkeypatch.setattr(server, "FORMAT_MODULES", (*server.FORMAT_MODULES, same))
    assert list(server._read_properties()) == ["path", "pages", "mode", "password", "sheet", "max_rows", "start_cell", "end_cell",
                                               "show_formulas", "start_at", "end_at", "outline_only", "slides",
                                               "detail"]
    assert {"start_at", "end_at", "outline_only", "slides", "detail"} <= set(
        server.READ_DOCUMENT.inputSchema["properties"])
    assert {"start_at", "end_at", "outline_only", "slides", "detail"} <= set(server.READ_OPTIONS)
    other = types.SimpleNamespace(__name__="other_tool", READERS={},
                                  READ_PROPERTIES={"pages": {"type": "integer"}}, READ_DESCRIPTION="")
    monkeypatch.setattr(server, "FORMAT_MODULES", (*server.FORMAT_MODULES, other))
    with pytest.raises(RuntimeError, match="declared differently"):
        server._read_properties()


def test_the_read_description_joins_the_formats_within_budget():
    description = server.READ_DOCUMENT.description
    assert description.startswith(server._READ_HEAD + " For XLSX: ")
    assert description.endswith("For DOCX: every block prints with its address (p12, t2/r3/c1, header0/p0) and "
                                "paragraph id (#1A2B3C4D), which write_docx takes as target; a long document "
                                "reads in windows (start_at), equations as LaTeX, tracked changes as {+inserted+} "
                                "{-deleted-}, then notes and comments.")
    for module in server.FORMAT_MODULES:
        assert len(module.READ_DESCRIPTION) <= server.READ_DESCRIPTION_BUDGET, module.__name__
    assert len(description) <= 2048 - 40


def test_read_document_dispatches_through_the_readers(monkeypatch, tmp_path):
    target = tmp_path / "report.xyz"
    target.write_text("hi")
    seen = {}

    def fake_reader(path, options):
        seen["call"] = (path, options)
        return "read it"

    async def _ident(p, writing=False, **kw):
        return p

    monkeypatch.setattr(server, "_resolve_path", _ident)
    monkeypatch.setitem(server.READ_DISPATCH, ".xyz", fake_reader)
    out = asyncio.run(server._handle_read_document({"path": str(target), "sheet": "S", "max_rows": 9,
                                                    "unrelated": 1}))
    assert out == "read it"
    assert seen["call"] == (str(target), {"sheet": "S", "max_rows": 9})
    (tmp_path / "x.odt").write_text("x")
    unsupported = asyncio.run(server._handle_read_document({"path": str(tmp_path / "x.odt")}))
    assert unsupported == "Error: Unsupported format '.odt'. Supported: .pdf, .docx, .xlsx, .pptx, .doc, .xls, .ppt"


def test_the_xlsx_reader_passes_today_s_arguments(monkeypatch):
    pytest.importorskip("openpyxl")
    import excel
    import xlsx_tool

    calls = []
    monkeypatch.setattr(excel, "read_xlsx", lambda *a, **k: calls.append((a, k)) or "ok")
    assert xlsx_tool.read("/x.xlsx", {"max_rows": "20", "show_formulas": True}) == "ok"
    assert calls == [(("/x.xlsx", None, 20), {"start_cell": None, "end_cell": None, "show_formulas": True})]


def test_the_server_imports_no_document_library():
    """The proxy suite runs server.py in a venv without lxml, and every worker
    child re-runs it before its job: its imports stay light."""
    code = (
        "import importlib.abc, runpy, sys\n"
        "BLOCKED = {'lxml', 'PIL', 'fitz', 'docx', 'openpyxl', 'pptx', 'numpy', 'catalog', 'checks',"
        " 'batch', 'word', 'excel', 'powerpoint', 'pdf', 'images'}\n"
        "class Refuse(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError('refused ' + name)\n"
        "sys.meta_path.insert(0, Refuse())\n"
        "sys.path.insert(0, '.')\n"
        "runpy.run_path('server.py', run_name='__mp_main__')\n"
        "print('light')\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=_HERE, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("light")


def test_the_tool_modules_import_their_format_module_lazily():
    for name in ("docx_tool", "xlsx_tool", "pptx_tool", "pdf_tool"):
        module = importlib.import_module(name)
        source = Path(module.__file__).read_text()
        top = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        assert top == ["from mcp.types import Tool, ToolAnnotations", "from kept_batches import FIX_PROPERTY"], name
    # The one module they share imports nothing but the standard library.
    source = Path(importlib.import_module("kept_batches").__file__).read_text()
    top = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert top <= {"__future__", "json", "secrets", "time", "collections", "dataclasses", "typing"}, top
