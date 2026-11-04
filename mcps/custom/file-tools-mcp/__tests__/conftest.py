"""Suite-wide fixtures.

Handler tests run worker cores INLINE (in-process): a real spawn costs ~1s
per call and, worse, a fresh child re-imports the modules and never sees
monkeypatched module attributes (budgets, byte caps, _resolve_path stubs).
Inline mode keeps those seams testable and the suite fast.

The real spawn path is covered explicitly: test_isolation.py and the
per-core spawn smokes in test_worker_cores.py delenv the flag.

Tests marked ``office`` run real LibreOffice; neither the dev box nor CI has
it, so they are skipped there and run in the image's test runner.
"""

import shutil

import pytest

_OFFICE_BINARIES = ("libreoffice", "soffice")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "office: runs real LibreOffice (skipped where it is not installed)")


def pytest_collection_modifyitems(config, items):
    if any(shutil.which(name) for name in _OFFICE_BINARIES):
        return
    skip = pytest.mark.skip(reason="LibreOffice is not installed here; the image's test runner runs these")
    for item in items:
        if "office" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolation_inline(monkeypatch):
    monkeypatch.setenv("FILETOOLS_ISOLATION_INLINE", "1")


@pytest.fixture(autouse=True)
def _write_root(monkeypatch, tmp_path):
    """Every output opens beneath the write root (the mount in the
    container); the suite writes under ``tmp_path``, and a spawn child
    inherits the variable."""
    monkeypatch.setenv("FILETOOLS_WRITE_ROOT", str(tmp_path))


@pytest.fixture()
def check_registry():
    """An empty check registry for one test, every check module counted as
    loaded; the real one comes back after it (see check_registry.py)."""
    import check_registry as swap

    with swap.swapped() as registry:
        yield registry


@pytest.fixture()
def check_registry_copy():
    """A copy of the real check registry for one test, which may add to it."""
    import check_registry as swap

    import checks

    with swap.swapped(checks._REGISTRY, every_module_loaded=False) as registry:
        yield registry
