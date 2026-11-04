"""The real-engine tests' own smoke: where the office mark runs, LibreOffice
is on the path and every module of the folder imports."""

import importlib
import shutil
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))


@pytest.mark.office
def test_libreoffice_is_on_the_path():
    assert shutil.which("libreoffice") or shutil.which("soffice")


def _modules() -> list[str]:
    names = [p.stem for p in _HERE.glob("*.py")]
    names += [".".join(p.relative_to(_HERE).with_suffix("").parts).removesuffix(".__init__")
              for p in (_HERE / "formats").glob("**/*.py")]
    return sorted(names)


@pytest.mark.office
@pytest.mark.parametrize("module", _modules())
def test_every_module_imports(module):
    importlib.import_module(module)
