"""The working-files window — what a person only showed an agent once.

A pass of the daily retention sweep (``services/infra/retention.py`` calls
:func:`run_pass`). Inside a workspace, shared and personal, only what the
platform itself put there for a moment ages, each file by its own age:

- ``uploads/photos/**`` and ``uploads/files/**`` (what a chat's attachments
  land in): parked in the Recover bin for its seven days, with a tombstone. A
  file the bin cannot take stays.
- ``.screenshots/**`` (the browser tool's, never synced): deleted after its
  own bound, whatever the window.

Everything else in a workspace is the person's work and never ages: a file
placed elsewhere under ``uploads/``, and ``inbox/``, where another agent's file
may be the only copy (the sender can clean up, or be removed). External
callers' trees are the caller-data pass's. Nothing here follows a link: the
walks and the deletes open beneath the agents root (``safe_fs``).

An unset value is the default. A value outside the range or not a number (a
forced setting, a typo: the settings route clamps its own) keeps everything,
never the default or the floor: a forced ``0`` meant as off must not delete.

The pass waits ``GRACE_DAYS`` from the first real sweep that reaches it, so an
upgraded install's admin can read the setting before anything goes.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

import config
from core import layout
from services.infra import file_bookkeeping, safe_fs
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from storage.files import db_file_pins, recover_bin_store

logger = logging.getLogger("otodock")

DEFAULT_DAYS = 180
SCREENSHOTS_DEFAULT_DAYS = 30
MIN_DAYS = 7
MAX_DAYS = 3650
GRACE_DAYS = 30

ENABLED_KEY = "working_files_retention_enabled"
DAYS_KEY = "working_files_retention_days"
SCREENSHOTS_DAYS_KEY = "screenshots_retention_days"
# Written once by the first real sweep that reaches the pass; never on the card.
STARTED_KEY = "working_files_retention_started_at"

STAT_KEYS = (
    "working_uploads_binned", "working_screenshots_deleted", "working_files_bytes", "working_uploads_deferred", "working_uploads_too_large",
    "working_files_kept", "working_files_errors",
)
UPLOADS = ("uploads/photos", "uploads/files")
SCREENSHOTS = ".screenshots"


def enabled() -> bool:
    """Unset means ON; only an explicit '0' disables."""
    return task_store.get_platform_setting(ENABLED_KEY) != "0"


def days_from(raw: str | None, default: int, key: str = DAYS_KEY) -> int:
    """The days a stored value means, 0 for none: unset is ``default``, a
    value outside ``MIN_DAYS``..``MAX_DAYS`` or not a number keeps everything."""
    if not raw:
        return default
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = -1
    if MIN_DAYS <= days <= MAX_DAYS:
        return days
    logger.warning("%s %r is not a number of days from %d to %d: nothing ages out by it",
                   key, raw, MIN_DAYS, MAX_DAYS)
    return 0


def window_days() -> int:
    return days_from(task_store.get_platform_setting(DAYS_KEY), DEFAULT_DAYS)


def screenshot_days() -> int:
    return days_from(task_store.get_platform_setting(SCREENSHOTS_DAYS_KEY),
                     SCREENSHOTS_DEFAULT_DAYS, SCREENSHOTS_DAYS_KEY)


def _parse_stamp(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _grace_end(now: datetime, dry_run: bool) -> datetime:
    """When the pass may start removing files. The first real sweep starts
    the clock; a dry run reads it and never writes one. An unreadable stamp
    is treated as none: the pass waits again, the safe way round."""
    started = _parse_stamp(task_store.get_platform_setting(STARTED_KEY) or "")
    if started is None:
        started = now
        if not dry_run:
            task_store.set_platform_setting(STARTED_KEY, now.isoformat())
    return started + timedelta(days=GRACE_DAYS)


def _scopes(agent: str) -> list[str]:
    """The workspaces of one agent, agent-relative: the shared one and each
    person's. Users are listed beneath the agents root, links left out."""
    scopes = [layout.WORKSPACE]
    try:
        walk = safe_fs.walk_beneath(config.AGENTS_DIR, f"{agent}/{layout.USERS}")
        try:
            top = next(walk)
            scopes += [f"{layout.USERS}/{u}/{layout.WORKSPACE}" for u in top.dirs]
            top.dirs[:] = []
        finally:
            walk.close()
    except OSError:
        logger.debug("working files: no users folder listed for %s", agent, exc_info=True)
    return scopes


def _files_of(agent: str, rel: str, stats: dict) -> Iterator[tuple[str, float, int]]:
    """``(agent-relative path, age stamp, size)`` of each regular file under
    ``rel``, newest stamp of mtime and ctime. Links are neither followed nor
    listed; a missing folder, or a link standing in for one, yields nothing;
    a folder that cannot be read is counted and skipped."""
    top = f"{agent}/{rel}"

    def _unreadable(err: OSError) -> None:
        stats["working_files_errors"] += 1
        logger.warning("working files: a folder of %s could not be read: %r", agent, err)

    try:
        for step in safe_fs.walk_beneath(config.AGENTS_DIR, top, onerror=_unreadable):
            base = step.rel[len(agent) + 1:]
            for name in step.files:
                try:
                    st = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield f"{base}/{name}", max(st.st_mtime, st.st_ctime), st.st_size
    except (FileNotFoundError, safe_fs.SymlinkRefused):
        return
    except OSError as err:
        _unreadable(err)


def _encodable(rel: str) -> bool:
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _note(agent: str, rel: str, size: int, stamp: float, what: str, stats: dict, dry_run: bool) -> None:
    stats["working_files_bytes"] += size
    if dry_run:
        return
    logger.info("retention: %s %s/%r (%d bytes, last touched %s)", what, agent, rel, size,
                datetime.fromtimestamp(stamp, timezone.utc).date().isoformat())


def _retire(agent: str, agent_dir: Path, rel: str, stamp: float, cutoff: float, stats: dict) -> None:
    """One aged upload through the bookkeeping, per-file failures counted."""
    try:
        outcome, size = file_bookkeeping.retire_aged_file_sync(agent, agent_dir, rel, cutoff=cutoff)
    except (OSError, ValueError, psycopg.Error) as err:
        stats["working_files_errors"] += 1
        logger.warning("retention: %s/%r was not aged out: %r", agent, rel, err)
        return
    if outcome == file_bookkeeping.AGED_GONE:
        stats["working_uploads_binned"] += 1
        _note(agent, rel, size, stamp, "aged out", stats, dry_run=False)
    elif outcome == file_bookkeeping.AGED_TOO_LARGE:
        stats["working_uploads_too_large"] += 1
        logger.info("retention: %s/%r stays: at %d bytes it cannot go to the Recover bin",
                    agent, rel, size)
    elif outcome == file_bookkeeping.AGED_NO_ROOM:
        stats["working_uploads_deferred"] += 1
    elif outcome == file_bookkeeping.AGED_KEPT:
        stats["working_files_kept"] += 1
        logger.warning("retention: %s/%r stays: its copy did not reach the Recover bin", agent, rel)


def _age_uploads(agent: str, agent_dir: Path, scope: str, cutoff: float,
                 pinned: set[str], stats: dict, dry_run: bool) -> None:
    budget = recover_bin_store.room_left(agent) if dry_run else None
    for folder in UPLOADS:
        aged = [f for f in _files_of(agent, f"{scope}/{folder}", stats) if f[1] < cutoff]
        # Oldest first: the bin's room goes to what has waited longest.
        for rel, stamp, size in sorted(aged, key=lambda f: f[1]):
            if rel in pinned:
                continue
            if not _encodable(rel):
                stats["working_files_errors"] += 1
                logger.warning("retention: %s/%r stays: its name is not UTF-8", agent, rel)
                continue
            if not dry_run:
                _retire(agent, agent_dir, rel, stamp, cutoff, stats)
                continue
            if size > config.RECOVER_BIN_MAX_BYTES:
                stats["working_uploads_too_large"] += 1
            elif budget is not None and size > budget:
                stats["working_uploads_deferred"] += 1
            else:
                if budget is not None:
                    budget -= size
                stats["working_uploads_binned"] += 1
                _note(agent, rel, size, stamp, "aged out", stats, dry_run=True)


def _age_screenshots(agent: str, scope: str, cutoff: float, pinned: set[str],
                     stats: dict, dry_run: bool) -> None:
    for rel, stamp, size in _files_of(agent, f"{scope}/{SCREENSHOTS}", stats):
        if stamp >= cutoff or rel in pinned:
            continue
        if not dry_run:
            try:
                safe_fs.unlink_beneath(config.AGENTS_DIR, f"{agent}/{rel}")
            except OSError as err:
                stats["working_files_errors"] += 1
                logger.warning("retention: %s/%r was not removed: %r", agent, rel, err)
                continue
        stats["working_screenshots_deleted"] += 1
        _note(agent, rel, size, stamp, "aged out a screenshot,", stats, dry_run)


def run_pass(stats: dict, dry_run: bool) -> None:
    """The pass. Reads its own settings; a dry run counts what would go, even
    during the grace, and writes nothing."""
    for key in STAT_KEYS:
        stats.setdefault(key, 0)
    stats.setdefault("working_files_grace_until", "")
    if not enabled():
        return
    window, screenshots = window_days(), screenshot_days()
    if not window and not screenshots:
        return
    now = datetime.now(timezone.utc)
    grace_end = _grace_end(now, dry_run)
    if now < grace_end:
        stats["working_files_grace_until"] = grace_end.isoformat()
        if not dry_run:
            return
    window_cutoff = (now - timedelta(days=window)).timestamp()
    screenshots_cutoff = (now - timedelta(days=screenshots)).timestamp()
    for agent_dir in sorted(agent_dirs(Path(config.AGENTS_DIR))):
        agent = agent_dir.name
        pinned = db_file_pins.pinned_paths(agent)
        for scope in _scopes(agent):
            if window:
                _age_uploads(agent, agent_dir, scope, window_cutoff, pinned, stats, dry_run)
            if screenshots:
                _age_screenshots(agent, scope, screenshots_cutoff, pinned, stats, dry_run)
