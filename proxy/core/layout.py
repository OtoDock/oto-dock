"""The agent tree — every folder name spelled once, the per-user tree, the
sandbox-virtual roots and the one rule between the two anchors.

One shape, two anchors. On the platform host the tree hangs under
``AGENTS_DIR/<slug>`` and a local session sees it through bwrap as the
sandbox-virtual roots (``/workspace``, ``/users/<u>``, ``/knowledge``,
``/config``); on a satellite the same tree hangs under the machine's
``agents_dir/<slug>``, nothing is mounted, and the virtual roots are
translated to the machine's paths by ``host_of_virtual`` — the satellite's
rule, which lives here and runs there as a byte copy
(``satellite/_vendored/layout.py``, hash-checked at boot).

The tree::

    <agent dir>/
      workspace/            the agent's shared workspace
      knowledge/            the reference library (``knowledge/.credentials``: the agent-scope credentials dir; no token file lives there, tokens are delivered per session)
      config/               the agent's behaviour (owner-tier; ``config/context``: its docs)
      users/<u>/            one person's private tree, the ``/users/<u>`` mount:
        workspace/          - created for every user, RW
        context/            - created for every user, RW
        .credentials/       - the person's credentials dir, RW when present; no token file lives there (delivered per session, a leftover masked and swept at boot)
        <engine config dir> - ``.claude`` / ``.codex``: the ENGINE's word, never spelled here

A session's WORKING ROOT is ``users/<u>`` for a user-scoped mount and
``workspace`` for an agent-scoped one (``scope_root``); its default save
folder is that root's workspace (``scope_workspace``). An external caller's
tree (``externals/<channel>/<id>``, ``core/session/external_identity.py``)
has the per-user SHAPE — its subdir names are these constants — but its
root is that module's, never a ``users/`` join.

Standard library only: the satellite runs the vendored copy with no proxy
on its path, and ``config.py`` imports nothing from this module — every
question takes the agent dir it works under. A frozen spelling: every name
here is on disk in every installed agent tree and in every stored path;
the ``V_*`` roots are agent-visible (prompts, tool descriptions, the
``OTO_*`` env, the mount table). Core-seams phase 10.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------

USERS = "users"
WORKSPACE = "workspace"
KNOWLEDGE = "knowledge"
CONFIG = "config"
CONTEXT = "context"
CREDENTIALS_DIR = ".credentials"

#: The synced agent tree's top level — what ``file_sync`` carries, what a
#: virtual path may begin with.
HEADS = (USERS, WORKSPACE, KNOWLEDGE, CONFIG)
#: The heads a person's path may name (``config`` is owner-tier, hidden
#: from the files API below the owner tier and never a check's input).
FILE_HEADS = (WORKSPACE, KNOWLEDGE, USERS)
#: A user's tree: the two subdirs created for every user, writable.
USER_SUBDIRS = (WORKSPACE, CONTEXT)

#: The sandbox-virtual roots of the four heads.
V_USERS = "/users"
V_WORKSPACE = "/workspace"
V_KNOWLEDGE = "/knowledge"
V_CONFIG = "/config"
TREE_ROOTS = (V_USERS, V_WORKSPACE, V_KNOWLEDGE, V_CONFIG)


# ---------------------------------------------------------------------------
# Relative and virtual spellings
# ---------------------------------------------------------------------------

def user_rel(username: str) -> str:
    """``users/<u>`` — a person's tree, agent-relative."""
    return f"{USERS}/{username}"


def scope_root(username: str) -> str:
    """The agent-relative root a session works in and keeps its scope config
    dir under: ``users/<u>`` for a user-scoped mount, else the shared
    ``workspace``. ONE rule for the satellite's ``cwd_relative``, the
    credential fan-out target and the adopt path."""
    return user_rel(username) if username else WORKSPACE


def scope_workspace(username: str) -> str:
    """The session's default save folder, agent-relative:
    ``users/<u>/workspace`` for a user-scoped mount, else ``workspace``."""
    return f"{user_rel(username)}/{WORKSPACE}" if username else WORKSPACE


def virtual_user_root(username: str) -> str:
    """``/users/<u>`` — the person's tree as the session sees it."""
    return f"{V_USERS}/{username}"


def virtual_workspace(username: str) -> str:
    """``/users/<u>/workspace`` for a user-scoped mount, else ``/workspace``
    — the ``workspace`` path role and the prompt's default folder."""
    return f"{virtual_user_root(username)}/{WORKSPACE}" if username else V_WORKSPACE


# ---------------------------------------------------------------------------
# Host compositions (the anchor is an argument: the platform's agent dir or
# a satellite's)
# ---------------------------------------------------------------------------

def user_dir(agent_dir: Path, username: str) -> Path:
    """``<agent dir>/users/<u>``."""
    return agent_dir / USERS / username


def workspace_dir(agent_dir: Path, username: str) -> Path:
    """The session's default save folder on disk (``scope_workspace``)."""
    return user_dir(agent_dir, username) / WORKSPACE if username else agent_dir / WORKSPACE


def context_dir(agent_dir: Path, username: str) -> Path:
    """``<agent dir>/users/<u>/context`` — the person's docs and memory."""
    return user_dir(agent_dir, username) / CONTEXT


def state_dir(agent_dir: Path, username: str, name: str) -> Path:
    """An engine's config dir at the session's scope root:
    ``users/<u>/<name>`` for a user-scoped mount, else ``workspace/<name>``
    (``name`` is the engine's declared dir — its word, not this module's)."""
    return (user_dir(agent_dir, username) if username else agent_dir / WORKSPACE) / name


# ---------------------------------------------------------------------------
# Questions about an agent-relative path
# ---------------------------------------------------------------------------

def head_of(rel: str) -> str:
    """The head an agent-relative path begins with, or ``""``. Exactly the
    first ``/``-separated segment: a bare ``users`` is the head ``users``,
    a leading slash is not stripped (a virtual path is not agent-relative)."""
    first = rel.split("/", 1)[0]
    return first if first in HEADS else ""


def is_personal(rel: str) -> bool:
    """The path lies inside somebody's tree (``users/<…>``) — the four
    ``startswith`` sites' rule: a bare ``users`` is not personal."""
    return rel.startswith(USERS + "/")


def user_of(rel: str) -> str:
    """The person whose tree an agent-relative path names (``users/<u>[/…]``),
    else ``""``. A leading slash is tolerated — the satellite's
    ``cwd_relative`` reader always stripped one — and a missing or empty
    second segment answers ``""``."""
    parts = rel.lstrip("/").split("/")
    if len(parts) >= 2 and parts[0] == USERS:
        return parts[1]
    return ""


def under(path: str, root: str) -> bool:
    """The one boundary rule of a virtual root: ``path`` is the root or lies
    below it (``/workspace`` and ``/workspace/x``, never ``/workspaces``)."""
    return path == root or path.startswith(root + "/")


# ---------------------------------------------------------------------------
# The satellite's translation
# ---------------------------------------------------------------------------

def host_of_virtual(
    value: str, agent_dir: Path, username: str, state_dirs: tuple = (),
) -> str:
    """Translate one sandbox-virtual path to a machine path under
    ``agent_dir`` — the satellite's rule (no bwrap there: the roots are
    real directories), applied to every env value, prompt path and MCP
    config the proxy ships in virtual form.

    ``/users/…``, ``/workspace…``, ``/knowledge…``, ``/config…`` become
    ``<agent_dir><value>`` by string concatenation (forward slashes kept on
    Windows — the paths the MCPs and the CLIs get today); ``/<state dir>…``
    for each name in ``state_dirs`` (the engines' config dirs, ``.claude`` /
    ``.codex``, handed in by the caller) becomes the scope's
    ``users/<u>/<name>…`` or ``workspace/<name>…``. Anything else — a URL,
    an opaque value, an absolute path outside the tree — passes through
    unchanged.
    """
    p = value
    base = str(agent_dir).rstrip("/")
    for name in state_dirs:
        root = "/" + name
        if under(p, root):
            suffix = p[len(root):]  # "" or "/x"
            if username:
                return f"{base}/{USERS}/{username}/{name}{suffix}"
            return f"{base}/{WORKSPACE}/{name}{suffix}"
    for root in TREE_ROOTS:
        if under(p, root):
            return f"{base}{p}"
    return p


def self_hash() -> str:
    """SHA256 of this module's source — used by the satellite drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""
