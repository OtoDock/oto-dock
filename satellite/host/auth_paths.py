"""Central allowlist for paths the proxy is allowed to ask the
satellite to write to or read from.

A compromised proxy could in principle craft a ``file_push`` with
``agent_slug=".."`` or an absolute-looking ``path`` to escape the
agent directory. The per-call checks in ``file_sync.apply_file_push``
already block traversal AFTER joining, but they don't validate the
slug itself. This module centralizes those rules in one place so
every receive-side handler can call the same primitive.

Authorized roots are determined at startup from ``SatelliteConfig``:
  * ``agents_dir``   — agent workspaces, user dirs, config, knowledge
  * ``mcps_dir``     — MCP packages (also written via sync_mcps, not file_push)

Anything outside these roots is rejected. The check uses path
resolution (``resolve()``) so symlinks can't escape either.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger("satellite.auth-paths")


# Agent slugs are plain identifiers — no path separators, no leading dot,
# no '..'. Reject anything else to keep ``agents_dir / slug`` from
# escaping the agents root.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


def is_safe_slug(slug: str) -> bool:
    """A safe slug is non-empty, no path separators, no traversal segments."""
    if not slug:
        return False
    if not _SLUG_RE.match(slug):
        return False
    return True


def is_path_under_root(path: Path, root: Path) -> bool:
    """Return True iff `path.resolve()` lives under `root.resolve()`.

    Handles symlinks (they're followed by ``resolve()`` before comparison).
    On Windows, also applies ``os.path.normcase`` to both sides since NTFS
    is case-insensitive but ``Path.relative_to``'s string compare is not —
    ``C:\\users\\…`` and ``C:\\Users\\…`` would otherwise be treated as
    distinct. Unix paths stay case-sensitive (real distinction on ext4/xfs).
    """
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        if sys.platform == "win32":
            resolved_path = Path(os.path.normcase(str(resolved_path)))
            resolved_root = Path(os.path.normcase(str(resolved_root)))
        resolved_path.relative_to(resolved_root)
        return True
    except (ValueError, OSError):
        return False


def is_authorized_relative_path(rel_path: str) -> bool:
    """Sanity check for a relative path string: not absolute, no traversal
    segments, no NUL bytes. The CALLER still has to join with a root and
    re-check via ``is_path_under_root`` after resolution.
    """
    if not rel_path:
        return False
    if "\0" in rel_path:
        return False
    p = Path(rel_path)
    if p.is_absolute():
        return False
    if any(part == ".." for part in p.parts):
        return False
    return True


def assert_agent_path_safe(
    rel_path: str, agent_slug: str, agents_dir: Path,
) -> Path:
    """Validate + resolve an (agent_slug, rel_path) tuple to an absolute
    path under agents_dir/agent_slug. Raises ``ValueError`` with a short
    machine-readable reason on rejection.

    Caller is responsible for logging the rejection alongside ``machine_id``
    for audit context. Used by ``session_manager.file_push`` and
    ``session_manager.file_pull``.
    """
    if not is_safe_slug(agent_slug):
        raise ValueError(f"unsafe agent_slug: {agent_slug!r}")
    if not is_authorized_relative_path(rel_path):
        raise ValueError(f"unsafe rel_path: {rel_path!r}")

    agent_dir = (agents_dir / agent_slug).resolve()
    if not is_path_under_root(agent_dir, agents_dir):
        # Belt-and-suspenders: even though is_safe_slug() should prevent
        # this, double-check post-join in case the slug RE drifts.
        raise ValueError(f"agent_dir escapes agents_dir: {agent_dir}")

    target = (agent_dir / rel_path).resolve()
    if not is_path_under_root(target, agent_dir):
        raise ValueError(f"target escapes agent_dir: {target}")
    return target
