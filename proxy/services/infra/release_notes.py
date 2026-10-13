"""The release dialog's rule: who is told the install changed release
(features/BACKUP-RESTORE.md "The version in the dashboard").

The dialog itself is the dashboard's, drawn from the curated highlights file
bundled with it (``dashboard/src/release/highlights.json``): the server
decides only whether a person is eligible to be told, and tells an admin
where the release notes are. The rule is pure (``card_for``) and read from
the database by ``card_for_user``:

* no known running version, a ``-dev`` working tree (never refused, never
  recorded, never told) or no ``schema_meta`` row: nothing;
* the person's ``last_seen_version`` equals the running version: nothing;
* no ``last_seen_version`` yet: eligible only when the install was upgraded
  after the person existed (``schema_meta`` says an upgrade happened and the
  person was created before it); a fresh install and a new person are not,
  and the dashboard marks the version seen silently;
* a ``last_seen_version`` that differs: eligible (an upgrade, a downgrade
  with the override, a restore of another version); ``direction`` says which.

An admin is shown whenever eligible (``show_admin``, with ``release_url``);
a member is eligible alike, and the dashboard shows the dialog only when the
running version's highlights carry something for everyone, else marks the
version seen silently.
"""

from __future__ import annotations

from datetime import datetime, timezone

import config
from storage.schema import is_dev_version, parse_version

RELEASES_URL = "https://github.com/OtoDock/oto-dock/releases"


def _parse_when(text) -> datetime | None:
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=timezone.utc)
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def release_url(running: str) -> str:
    """The release page of a release number; the releases list for an rc."""
    run_v = parse_version(running)
    return f"{RELEASES_URL}/tag/v{running}" if run_v and run_v[3] == (0, 0) else RELEASES_URL


def card_for(*, running: str, is_admin: bool, user_created_at, seen: str, meta: dict | None) -> dict:
    """The rule for one person (the module docstring), pure:
    ``{show_admin, eligible, version, previous, direction, seen, release_url}``."""
    run_v = parse_version(running)
    base = {
        "show_admin": False, "eligible": False, "version": running, "previous": "",
        "direction": "up", "seen": seen, "release_url": "",
    }
    if run_v is None or meta is None or is_dev_version(running):
        return base
    if seen == running:
        return base
    if not seen:
        fresh = meta.get("created_by", "") == running and not meta.get("upgraded_from", "")
        created = _parse_when(user_created_at)
        migrated = _parse_when(meta.get("last_migrated_at"))
        if fresh or created is None or migrated is None or created >= migrated:
            return base
    previous = seen or meta.get("upgraded_from", "")
    prev_v = parse_version(previous) if previous else None
    # A downgrade (the override, a restore of an older dump): the change is
    # told, the release's highlights are not.
    downgrade = prev_v is not None and prev_v > run_v
    return {
        **base,
        "show_admin": is_admin,
        "eligible": True,
        "previous": previous,
        "direction": "down" if downgrade else "up",
        "release_url": release_url(running) if is_admin else "",
    }


def card_for_user(sub: str, role: str) -> dict:
    """The rule for a signed-in person, read from the database (sync; run it
    through ``run_db``)."""
    from auth import roles
    from storage import schema
    from storage.identity.db_users import get_user
    from storage.pg import get_conn
    from storage.prefs.user_ui_prefs_store import get_prefs

    user = get_user(sub) or {}
    seen = str(get_prefs(sub).get("last_seen_version") or "")
    with get_conn() as conn:
        meta = schema.read_platform_version(conn)
    return card_for(
        running=config.PINNED_OTODOCK_VERSION, is_admin=roles.is_admin(role),
        user_created_at=user.get("created_at"), seen=seen, meta=meta,
    )
