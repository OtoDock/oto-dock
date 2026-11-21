"""What the platform knows of each Docker tool's container while it runs: its
lock, its use, whether the platform put it to sleep and why its last wake
failed. In memory: a proxy start begins with every tool used at that moment.

The lock is the one every compose run on a tool's project takes (a start, a
stop, a recreate, a wake, a sleep), from a thread: two runs on one project
collide, and a sleep must never stop a container a wake is starting. The use
counts are kept by the door (``door.py``) on the event loop.
"""

from __future__ import annotations

import functools
import re
import threading
import time
from dataclasses import dataclass, field


@dataclass
class ToolState:
    """One tool's container as the platform knows it."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    # Requests the door forwards to it now, and sessions bound to it.
    in_flight: int = 0
    bound: set[str] = field(default_factory=set)
    # When its last request ended (monotonic); a proxy start counts as use.
    last_use: float = field(default_factory=time.monotonic)
    # The platform stopped it for want of use, and nothing started it since.
    asleep: bool = False
    # Why its last start failed (``NO_IMAGE`` or ``FAILED``), "" when it did
    # not, and compose's own words for the admin.
    failed: str = ""
    last_error: str = ""
    # A wake runs now.
    waking: bool = False
    # Since when (monotonic) the sweep keeps it awake because it says it is
    # working (``idle._still_working``); 0.0 when it does not.
    held_since: float = 0.0


NO_IMAGE = "image"
FAILED = "start"
# What compose prints when the image is not on this host and ``--pull never``
# forbids fetching it.
_NO_IMAGE_MARKS = ("no such image", "pull access denied", "image not found", "manifest unknown")
# Compose's progress lines ("Container <name>  Creating"): no cause.
_PROGRESS_LINE = re.compile(r"^\W*(Container|Network|Volume|Image)\s+\S+\s+\w+\s*$", re.IGNORECASE)

_states: dict[str, ToolState] = {}
_states_lock = threading.Lock()


def of(name: str) -> ToolState:
    with _states_lock:
        st = _states.get(name)
        if st is None:
            st = _states[name] = ToolState()
        return st


def known() -> dict[str, ToolState]:
    with _states_lock:
        return dict(_states)


def locked(fn):
    """A compose run on one tool's project, under that tool's lock (the
    first argument is its manifest)."""
    @functools.wraps(fn)
    def run(manifest, *args, **kwargs):
        with of(manifest.name).lock:
            return fn(manifest, *args, **kwargs)
    return run


def started(name: str) -> None:
    """Its container was started by anyone: awake, its last wake's failure
    gone, used now (so a start by hand is not slept at the next tick)."""
    st = of(name)
    st.asleep = False
    st.failed = ""
    st.last_error = ""
    st.last_use = time.monotonic()
    st.held_since = 0.0


def failed_start(name: str, error: str) -> None:
    """A start of its container failed, whoever tried it (a boot, Start,
    an update, a wake): its row reads Did not start until a start works."""
    st = of(name)
    st.last_error = (error or "")[-2000:]
    st.failed = NO_IMAGE if any(mark in st.last_error.lower() for mark in _NO_IMAGE_MARKS) else FAILED


def plain_cause(name: str) -> str:
    """One plain cause of its last failed start for its row: the missing
    image's own sentence, else compose's last line of error, once."""
    st = of(name)
    if st.failed == NO_IMAGE:
        return "Its image is not on this server: press Start to fetch it."
    if not st.failed:
        return ""
    lines = [ln.strip() for ln in st.last_error.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _PROGRESS_LINE.match(ln)]
    return lines[-1][:300] if lines else ""


def removed(name: str) -> None:
    """Its container was taken down (the tool turned off or deleted)."""
    st = of(name)
    st.asleep = False
    st.failed = ""
    st.held_since = 0.0


def in_use(name: str) -> bool:
    st = of(name)
    return st.in_flight > 0 or bool(st.bound)


def reset_for_tests() -> None:
    with _states_lock:
        _states.clear()
