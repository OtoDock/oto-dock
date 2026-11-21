"""When the platform was last alive, and how it stopped.

Three ``platform_settings`` keys. ``platform_alive_at`` is written every
``BEAT_S`` while the server runs and once more at its clean stop, so a boot
knows since when the platform was down (a one-time task that came due in
that window fires at boot: ``services/scheduler/scheduler.py``). A database
with no key, or with one older than ``MAX_DOWNTIME``, is a boot that takes
nothing up, and that boot says so in the store
(``storage/chat/cut_records.py``, ``CAUSE_NOT_TAKEN_UP``), since the stamp it
writes reads as caught up to the next one. ``platform_clean_stop`` is set by
the graceful shutdown and cleared at boot: without it the last process
ended with no stop of its own. ``platform_held_at`` is when a drain began to
hold new work, so what it held counts as due in the downtime.

Synchronous: the boot calls these directly, everything else on the DB
executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from storage import database as task_store

logger = logging.getLogger("otodock")

ALIVE_AT = "platform_alive_at"
CLEAN_STOP = "platform_clean_stop"
HELD_AT = "platform_held_at"

#: How often the server says it is alive.
BEAT_S = 60.0
#: A fire this long before the last sign of life may have been missed too.
_SLACK = timedelta(minutes=5)
#: A longer downtime takes nothing up again: no chat is continued, no run
#: reopened, no fire caught up. ``scripts/restore.sh`` removes the keys, so a
#: restore reads the same however old its dump is.
MAX_DOWNTIME = timedelta(hours=24)


@dataclass(frozen=True)
class Boot:
    """What the last process left: ``stamped``, whether a record-keeping
    server ran on this database (read by the tests alone; the product decides
    on ``down_since``, through ``catches_up``), whether it stopped by its own
    shutdown, and since when
    the platform was down (None on an unstamped database, or after a
    downtime longer than ``MAX_DOWNTIME``)."""

    stamped: bool = False
    clean_stop: bool = False
    down_since: datetime | None = None

    @property
    def catches_up(self) -> bool:
        """The boot takes up what the last process left. False on a first
        start, after a restore and after a downtime past ``MAX_DOWNTIME``: a
        chat gets the card and a run fails with its one report."""
        return self.down_since is not None


_boot = Boot()


def _parse(value: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def read_at_boot() -> Boot:
    """Read what the last process left. Nothing is written:
    ``stamp_at_boot`` follows once the boot's answer is in the store, so a
    boot that fails before that reads the same marks next time."""
    global _boot
    now = datetime.now(timezone.utc)
    alive = _parse(task_store.get_platform_setting(ALIVE_AT))
    held = _parse(task_store.get_platform_setting(HELD_AT))
    clean = task_store.get_platform_setting(CLEAN_STOP) == "1"
    down_since = None
    if alive is not None:
        down_since = alive - _SLACK
        if held is not None and held < down_since:
            down_since = held
        if now - down_since > MAX_DOWNTIME:
            logger.warning("The platform was last alive at %s: nothing that came due since "
                           "is caught up", alive.isoformat())
            down_since = None
    _boot = Boot(stamped=alive is not None, clean_stop=clean, down_since=down_since)
    return _boot


def stamp_at_boot() -> None:
    """Mark this process alive: what the last one left has been read once."""
    task_store.set_platform_setting(CLEAN_STOP, "")
    task_store.set_platform_setting(HELD_AT, "")
    beat()


def boot() -> Boot:
    """What ``read_at_boot`` found."""
    return _boot


def past_max_downtime(when: str) -> bool:
    """``when`` (an ISO time) lies further back than ``MAX_DOWNTIME``."""
    at = _parse(when)
    return at is not None and datetime.now(timezone.utc) - at > MAX_DOWNTIME


def beat() -> None:
    task_store.set_platform_setting(ALIVE_AT, datetime.now(timezone.utc).isoformat())


def note_held() -> None:
    """A drain holds new work from now."""
    task_store.set_platform_setting(HELD_AT, datetime.now(timezone.utc).isoformat())


def note_resumed() -> None:
    task_store.set_platform_setting(HELD_AT, "")


def note_clean_stop() -> None:
    """The graceful shutdown, while the database is still there."""
    beat()
    task_store.set_platform_setting(CLEAN_STOP, "1")
