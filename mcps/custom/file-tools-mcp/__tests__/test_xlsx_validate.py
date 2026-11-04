"""Every write_xlsx operation leaves a valid package: each catalogue
example (after its setup) is saved, opened again and passes
ooxml.validate.validate_package with no error."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import checks
from excel import CATALOG, contract_fixtures, handle_write_xlsx
from ooxml.validate import validate_package

_EXAMPLES = [(spec.name, number, [*spec.setup, example])
             for spec in CATALOG.ops.values() for number, example in enumerate(spec.examples)]


@pytest.mark.parametrize(("name", "number", "ops"), _EXAMPLES, ids=[f"{n}-{i}" for n, i, _ in _EXAMPLES])
def test_every_example_saves_a_valid_package(tmp_path, monkeypatch, name, number, ops):
    fixtures = contract_fixtures(str(tmp_path))

    async def resolve(value, writing=False, **kw):
        return fixtures.get(value, os.path.join(str(tmp_path), os.path.basename(value)))

    async def _none(path, filename=None):
        return None

    async def missing(path, *, out_dir, **kw):
        raise FileNotFoundError("libreoffice")

    monkeypatch.setattr("shared._resolve_path", resolve)
    monkeypatch.setattr("shared._push_preview", _none)
    monkeypatch.setattr("shared._notify_file_written", _none)
    monkeypatch.setattr("render_engine.recalc", missing)
    book = tmp_path / "book.xlsx"
    out = asyncio.run(handle_write_xlsx({"path": str(book), "operations": ops}))
    assert out.startswith("Workbook saved"), out
    with checks.CheckContext(str(book), "xlsx") as ctx:
        errors = [f for f in validate_package(ctx) if f.severity is checks.Severity.ERROR]
    assert errors == [], [f"{f.check} {f.location}: {f.message}" for f in errors]
