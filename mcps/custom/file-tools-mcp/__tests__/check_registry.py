"""The check registry swapped for one test: the tests that register checks
of their own, or count every check module loaded, use these fixtures, so
the order the suite runs in never changes which checks the others see."""

import contextlib
import sys
from pathlib import Path

import checks

MCP_DIR = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def swapped(start: dict | None = None, *, every_module_loaded: bool = True):
    """The test's own registry content (empty, or a copy of ``start``) and
    loaded set (every check module, so none registers by import; else a
    copy of the real one). The swap is made in place: the real objects stay
    bound, so a test that also monkeypatches them (whose undo runs after
    this) still ends on the real ones. Afterwards their content comes back,
    and every check module the test imported for the first time registers
    its checks there."""
    registry, loaded = checks._REGISTRY, checks._LOADED
    saved_registry, saved_loaded = dict(registry), set(loaded)
    initial = dict(start) if start is not None else {}
    imported = set(sys.modules)
    registry.clear()
    registry.update(initial)
    loaded.clear()
    loaded.update(checks.check_module_names(MCP_DIR) if every_module_loaded else saved_loaded)
    try:
        yield registry
    finally:
        checks._REGISTRY, checks._LOADED = registry, loaded
        registry.clear()
        registry.update(saved_registry)
        loaded.clear()
        loaded.update(saved_loaded)
        # A check module imported for the first time registered into the
        # swapped content: it registers again into the real one.
        for name in checks.check_module_names(MCP_DIR):
            if name in sys.modules and name not in imported:
                sys.modules[name].register_all()
