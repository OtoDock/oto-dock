"""Filesystem anchors shared across the test suite.

Each anchor is computed once, here, so individual test modules never hardcode
``__file__``-relative parent counts (``Path(__file__).parent.parent`` and
friends). That means a test can live at any depth under ``tests/`` — moving it
between subfolders never changes which directory it points at.

Import the anchor you need instead of recomputing it::

    from tests._paths import PROXY_DIR, REPO_ROOT, CUSTOM_MCPS

This module lives directly in ``tests/`` and is the one place whose depth is
fixed by contract, so the computation below stays valid regardless of where the
importing test module sits.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

# tests/_paths.py -> parent = tests/ -> parent.parent = proxy/
PROXY_DIR = Path(__file__).resolve().parent.parent
# proxy/ -> repository root (contains proxy/, mcps/, satellite/, ...)
REPO_ROOT = PROXY_DIR.parent
# Bundled MCP server sources.
CUSTOM_MCPS = REPO_ROOT / "mcps" / "custom"


def load_mcp_server(mcp_dir: Path):
    """Import ``<mcp_dir>/server.py`` under a module key unique to that MCP.

    Every custom MCP names its entry module ``server.py``. Loading them via
    ``sys.path.insert`` + ``import server`` gives them all the same
    ``sys.modules["server"]`` key — under xdist, test files for two different
    MCPs interleave on one worker and a re-import resolves to whichever MCP
    dir was promoted to ``sys.path[0]`` most recently (the whole victim file
    then fails, but only for some worker schedules). Loading by explicit file
    location under a per-MCP key removes both the key collision and the
    path-order dependence. Each call executes a fresh module, so module-level
    constants re-read the current env (the reload-with-env test pattern).

    A multi-file MCP imports its siblings by bare name (``python server.py``
    from its folder puts the folder on ``sys.path[0]``), so the folder sits
    on ``sys.path`` only WHILE ``server.py`` executes — then it comes off,
    since another MCP's generic sibling names (``shared``, ``screen``) must
    not shadow a later import from a folder left at the front. The sibling
    stays cached in ``sys.modules`` under its own (unique) name.
    """
    name = f"{mcp_dir.name.replace('-', '_')}_server"
    spec = importlib.util.spec_from_file_location(name, mcp_dir / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(mcp_dir))
    try:
        spec.loader.exec_module(mod)
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(mcp_dir))
    return mod
