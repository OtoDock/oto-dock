"""The bundled tools whose environment is not built, read from memory.

The boot scans the bundled MCPs before the server listens
(``mcp_venv_bootstrap.scan_bundled_venvs``) and builds nothing: an image
carries every environment, and an install from source builds them with its
setup script (``scripts/dev-setup.sh``, which runs ``python -m
services.mcp.mcp_venv_bootstrap --build``). An MCP the scan found stale is
handed to no session (``mcp_session_filter.session_exclusion_reason``) until
a boot's scan reads it built: the script runs with OtoDock stopped. The
health parts read the names, and admins are told once per start
(``notice``, ``mcp_venv_bootstrap.tell_admins``).

A stdlib leaf: the session filter and the health answer import it at module
level, and nothing here touches the disk.
"""

from __future__ import annotations

_stale: frozenset[str] = frozenset()
_notice = ""


def note_stale(names: frozenset[str] | set[str], notice: str = "") -> None:
    """What the boot's scan found stale, in place of what an earlier scan
    found, and the sentence that names it to admins
    (``mcp_venv_bootstrap.build_notice``)."""
    global _stale, _notice
    _stale = frozenset(names)
    _notice = notice if _stale else ""


def stale_names() -> frozenset[str]:
    return _stale


def notice() -> str:
    """What admins are told of the stale MCPs, "" when none is."""
    return _notice


def stale_reason(label: str) -> str:
    """The words a session is built without such an MCP with: a sentence an
    agent can repeat to a person."""
    return (f"{label} needs its environment built. An admin stops OtoDock, runs `scripts/dev-setup.sh` on the "
            "server and starts OtoDock again.")


def reset_for_tests() -> None:
    global _stale, _notice
    _stale, _notice = frozenset(), ""
