"""Suite-wide fixtures.

Handler tests run worker cores INLINE (in-process): a real spawn costs ~1s
per call and, worse, a fresh child re-imports the modules and never sees
monkeypatched module attributes (budgets, byte caps, _resolve_path stubs).
Inline mode keeps those seams testable and the suite fast.

The real spawn path is covered explicitly: test_isolation.py and the
per-core spawn smokes in test_worker_cores.py delenv the flag.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolation_inline(monkeypatch):
    monkeypatch.setenv("FILETOOLS_ISOLATION_INLINE", "1")


@pytest.fixture(autouse=True)
def _write_root(monkeypatch, tmp_path):
    """Every output opens beneath the write root (the mount in the
    container); the suite writes under ``tmp_path``, and a spawn child
    inherits the variable."""
    monkeypatch.setenv("FILETOOLS_WRITE_ROOT", str(tmp_path))
