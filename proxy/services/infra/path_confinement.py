"""Path confinement in the shape static analysis recognizes.

Every request-derived path the proxy touches is already confined with
pathlib (``resolve()`` then ``is_relative_to``). Code scanning does not model
that pair as a barrier — the pair it models is ``os.path.realpath`` /
``os.path.normpath`` followed by ``str.startswith`` — so the same check
written that way is what closes a path-injection finding for good instead
of dismissing it again after every line shift. Route the FINAL path through
one of these right before the filesystem call and let the returned value be
the one that flows on: the check is on the string the scanner tracks.
"""

import os
from pathlib import Path

import config


class PathOutsideRoot(ValueError):
    """The candidate path does not lie inside the root it must stay under."""


def resolve_under(path: str | Path, root: str | Path) -> Path:
    """``path`` with symlinks followed, guaranteed inside ``root`` (the root
    itself allowed). Both sides are canonicalized, so a symlink anywhere in
    the chain is judged by where it points, not by where it sits."""
    root_real = os.path.realpath(root).rstrip(os.sep) + os.sep
    real = os.path.realpath(path) + os.sep
    if not real.startswith(root_real):
        raise PathOutsideRoot(f"{path} is outside {root}")
    return Path(real)


def normalize_rel_path(raw: str) -> str:
    """``raw`` as a clean relative path: the leading and trailing slashes
    stripped (the sandbox-virtual forms clients send — ``/workspace/x`` —
    name a relative path of the tree), refused (``PathOutsideRoot``) when
    the result is empty, carries a NUL, or has a segment that is empty,
    ``.`` or ``..``. Lexical, no filesystem, backslashes untouched (a Linux
    name may carry one; a guard that takes a NAME keeps its own
    no-separator rule). Unlike ``join_under`` it refuses an interior ``..``
    instead of collapsing it. A caller for which an absolute form is an
    error refuses the leading slash BEFORE calling; a caller whose value
    must be canonical asks ``normalize_rel_path(x) == x``; every caller
    joins the value returned here, never the raw one."""
    if not isinstance(raw, str) or "\x00" in raw:
        raise PathOutsideRoot(f"{raw!r} is not a relative path")
    norm = raw.strip("/")
    if not norm:
        raise PathOutsideRoot(f"{raw!r} names no path")
    if any(seg in ("", ".", "..") for seg in norm.split("/")):
        raise PathOutsideRoot(f"{raw!r} has an empty or dot segment")
    return norm


def join_under(root: str | Path, *parts: str) -> Path:
    """``root/parts...`` without touching the filesystem, guaranteed to stay
    lexically BELOW ``root``: a ``..``, absolute or empty segment that would
    land on or above the root is refused (an interior ``a/../b`` collapses
    to ``b`` — ``normalize_rel_path`` is the guard that refuses it).
    Symlinks are not followed — an agent root that is itself a symlink
    keeps working, as everywhere else."""
    root_norm = os.path.normpath(root)
    prefix = root_norm if root_norm.endswith(os.sep) else root_norm + os.sep
    joined = os.path.normpath(os.path.join(root_norm, *parts))
    if not joined.startswith(prefix):
        raise PathOutsideRoot(f"{os.sep.join(parts)} would leave {root}")
    return Path(joined)


def safe_agent_dir(agent: str) -> Path:
    """``config.get_agent_dir`` for a request-supplied name: the same
    ``AGENTS_DIR / agent`` join, refused when the name would leave the
    agents tree (the check ``config.is_safe_agent_name`` makes, in barrier
    shape)."""
    return join_under(config.AGENTS_DIR, agent)
