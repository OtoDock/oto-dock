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
