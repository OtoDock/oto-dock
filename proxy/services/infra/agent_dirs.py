"""The agent directories under ``AGENTS_DIR``, and nothing else that lives
there.

An agent slug never starts with a dot; the dot entries of the agents
directory hold platform state, the offboarding archive
(``.offboarded/<username>/<agent>/``) among them. A sweep that walked them
as agents would read a departed person's archived tree as live agent data
whenever their username spelled one of the folder names it looks for
(``users``, ``app-releases``), and could delete inside it.
"""

from __future__ import annotations

from pathlib import Path


def agent_dirs(agents_dir: Path) -> list[Path]:
    """Every agent directory under ``agents_dir``, unsorted; none when the
    directory is missing."""
    if not agents_dir.is_dir():
        return []
    return [p for p in agents_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
