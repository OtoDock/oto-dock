"""An agent's folder as the root of the server's own file work in it.

One rule for every reader, writer and remover of the server's that works in
an agent's folder: the folder may itself be a link the install's operator
made, and that one link is followed once, here (:func:`agent_root`). Nothing
beneath it is ever followed: the ``safe_fs`` helpers take the root and a
rel and refuse a link at any part below the root. A caller that holds only a
path asks :func:`beneath_agent` for both; :func:`agent_holding` names the
agent a path belongs to.
"""

from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path

import config as app_config


def agent_root(agent_name: str) -> Path:
    """The agent's real folder: its folder in the agents folder, resolved
    once (an agent folder that is itself a link is the operator's honest
    layout)."""
    return Path(os.path.realpath(app_config.get_agent_dir(agent_name)))


def agent_holding(path: str | os.PathLike[str]) -> str:
    """The agent whose folder holds ``path``, "" for none: by the agents
    folder (as named or at its real place) or, for an agent folder that is
    itself a link, by that folder's real place. Judged on the text, with one
    listing of the agents folder only for a path that is not under it. A
    listing that fails raises its ``OSError``: "could not look" is never
    read as "no agent holds it". An agents folder that is not there holds
    nothing, and an entry that cannot be read is passed over."""
    from services.infra import safe_fs
    text = os.fspath(path)
    agents = app_config.AGENTS_DIR
    try:
        first = safe_fs.rel_under(text, agents).split("/", 1)[0]
        return first if app_config.is_safe_agent_name(first) else ""
    except OSError:
        pass
    try:
        entries = os.scandir(agents)
    except FileNotFoundError:
        return ""
    with entries:
        for entry in entries:
            with contextlib.suppress(OSError):
                if not entry.is_symlink() or not app_config.is_safe_agent_name(entry.name):
                    continue
                safe_fs.rel_under(text, os.path.realpath(entry.path))
                return entry.name
    return ""


def beneath_agent(path: str | os.PathLike[str], agent_name: str = "") -> tuple[Path, str]:
    """The root and the rel of ``path`` for the ``safe_fs`` helpers: the
    agent's real folder (:func:`agent_root`) and ``path``'s parts below it,
    judged on the text (the agent's folder as named or at its real place).
    With ``agent_name`` the path must lie in that agent's folder, without it
    in the folder of the agent that holds it (:func:`agent_holding`). A path
    in no agent's folder, or in another one's, raises ``EscapeRefused``."""
    from services.infra import safe_fs
    agent = agent_name or agent_holding(path)
    if not agent:
        raise safe_fs.EscapeRefused(errno.EXDEV, "path is in no agent's folder", os.fspath(path))
    text = os.fspath(path)
    try:
        return agent_root(agent), safe_fs.rel_under(text, app_config.get_agent_dir(agent))
    except safe_fs.EscapeRefused:
        # The agents folder itself a link: a path joined at its real place.
        real = os.path.join(os.path.realpath(app_config.AGENTS_DIR), agent)
        return agent_root(agent), safe_fs.rel_under(text, real)
