"""PostgreSQL-backed Workspace Recover Bin store.

One row per file that was removed or overwritten **involuntarily** — a sync
reconcile when an offline satellite reconnects (proxy-wins), or a proxy-wins
overwrite of a satellite's divergent copy — or **deleted via the dashboard**.
The captured pre-change bytes live on disk under
``RECOVER_BIN_DIR/<agent_slug>/<entry_id>``; this table holds the metadata + the
recovery scope so a restore can be authorization-gated:

* ``scope='user'``  → a ``users/<slug>/...`` file; only its owner (or an admin)
  may list / restore it. ``owner_sub`` is the resolved user_sub.
* ``scope='shared'`` → a ``workspace/`` / ``knowledge/`` / ``config/`` file (or a
  ``users/<slug>/`` file whose slug no longer maps to a user); only a manager of
  the agent (or an admin) may list / restore it.

Entries expire after 7 days (reaped by ``app.py``). **No version history** — this
is a deletion/overwrite safety net, not a backup system.

Bytes live on disk rather than in a BYTEA column because a recover-bin entry can
be up to ``RECOVER_BIN_MAX_BYTES`` (100 MB) — too large to want inside Postgres.

All functions are synchronous (called via ``asyncio.to_thread`` from async code).
The pooled connection commits on clean context exit (see ``storage/pg.py``), so
no explicit ``conn.commit()`` is needed.
"""

import contextlib
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.recover-bin")

# How long a captured entry stays recoverable before the reaper drops it.
RECOVER_BIN_TTL_DAYS = 7

# The reason a file landed in the bin. Mirrored by the table's inline CHECK.
#   ``deleted``  — pre-delete bytes of a removed file (dashboard delete, a
#                  tombstone-driven satellite delete, or an applied live delete).
#   ``conflict`` — the losing side of a genuine cross-user concurrent edit on a
#                  shared file (the winner is live; the loser stays recoverable).
VALID_REASONS = ("deleted", "conflict")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_path(agent_slug: str, entry_id: str) -> Path:
    """On-disk location of an entry's captured bytes (flat per agent)."""
    return config.RECOVER_BIN_DIR / agent_slug / entry_id


def remove_agent_files(agent_slug: str) -> None:
    """Unlink the whole on-disk recover-bin tree for an agent.

    Called from the agent-delete endpoint after the metadata rows are removed in
    ``agent_store.delete_agent`` — so a deleted agent leaves nothing recoverable
    behind. Best-effort; lives outside the agent folder (under
    ``RECOVER_BIN_DIR/<slug>/``) so it isn't covered by the agent-dir rmtree.
    """
    import shutil

    d = config.RECOVER_BIN_DIR / agent_slug
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _resolve_scope_owner(rel_path: str) -> tuple[str, str]:
    """Derive ``(scope, owner_sub)`` from a virtual ``rel_path``.

    * ``users/<slug>/...`` → ``('user', <resolved user_sub>)`` — or
      ``('shared', '')`` when the slug no longer maps to a user, so the entry is
      never orphaned (a manager/admin can still recover it).
    * everything else (``workspace/``, ``knowledge/``, ``config/``) → ``('shared', '')``.

    The scope is derived server-side here, never trusted from a client.
    """
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "users" and parts[1]:
        # Lazy import: storage.database pulls in heavier deps; importing it at
        # module load risks a cycle (mirrors storage/remote_store.py).
        from storage import database
        sub = database.get_user_sub_by_username(parts[1])
        if sub:
            return ("user", sub)
    return ("shared", "")


_NON_RECOVERABLE_SEGMENTS = frozenset({".claude", ".codex", ".credentials", ".config"})


def _is_recoverable_path(rel_path: str) -> bool:
    """True only for real CONTENT worth recovering.

    Excludes session state + secrets — ``.claude`` / ``.codex`` (CLI session
    state + config the platform rewrites every session), ``.config`` (machine
    config), ``.credentials`` (OAuth tokens). Those are regenerated every sync
    and are never the user's work — capturing them would flood the bin AND leak
    credentials. The agent ``config/`` folder (prompt + context) is NOT excluded
    — it's manager-curated content, recoverable like ``knowledge/``.
    """
    parts = rel_path.split("/")
    if not parts:
        return False
    return not any(seg in _NON_RECOVERABLE_SEGMENTS for seg in parts)


def _already_binned(agent_slug: str, rel_path: str, file_hash: str) -> bool:
    """True if this exact version (same path + bytes) is already binned and
    unexpired — makes capture idempotent. Collapses the dashboard-delete +
    reconnect-reconcile double-capture of one file, and stops a
    perpetually-divergent file from re-binning (and re-notifying) every sync.
    """
    now_iso = _now().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM recover_bin WHERE agent_slug=%s AND rel_path=%s "
            "AND file_hash=%s AND expires_at > %s LIMIT 1",
            (agent_slug, rel_path, file_hash, now_iso),
        ).fetchone()
    return row is not None


def restore_tier(rel_path: str) -> str:
    """The permission tier required to restore a path — mirrors who may WRITE it.

    ``user`` → personal ``users/<slug>/`` (the owner); ``editor`` → shared
    ``workspace/`` (editor + manager); ``manager`` → ``knowledge/`` + ``config/``
    (manager only). Anything else falls back to ``manager`` (most restrictive).
    """
    parts = rel_path.split("/")
    if parts and parts[0] == "users":
        return "user"
    if parts and parts[0] == "workspace":
        return "editor"
    return "manager"


def can_restore(
    entry: dict, requester_sub: str,
    can_edit: bool, can_manage: bool, is_admin: bool,
) -> bool:
    """Whether the requester may list / restore / discard this entry.

    Mirrors the write-permission tier of the path (you can recover what you
    could have written). Admins count as manager-tier for the SHARED scopes
    (workspace/knowledge/config — admins manage agents; ``can_edit`` /
    ``can_manage`` already include them, ``is_admin`` is a defensive belt for
    callers passing per-agent-only flags) but get NO bypass for personal
    entries: a ``users/<slug>/`` file is restorable by its owner alone. Used
    by ``list_for`` AND the restore/discard endpoints so the gate is
    identical at both layers.
    """
    manager_tier = can_manage or is_admin
    tier = restore_tier(entry.get("rel_path", ""))
    if tier == "manager":
        return manager_tier
    if tier == "editor":
        return can_edit or is_admin
    # tier == "user": the owner restores — nobody else, admins included; an
    # orphaned personal file (slug no longer maps to a user → no owner_sub)
    # falls back to manager-tier so it is never left unrecoverable.
    owner = entry.get("owner_sub") or ""
    return (owner == requester_sub) if owner else manager_tier


def _enforce_agent_cap(agent_slug: str, incoming: int) -> None:
    """Evict this agent's oldest recover-bin entries until ``incoming`` more
    bytes fit under ``config.RECOVER_BIN_AGENT_MAX_BYTES`` (0 = unlimited).

    The recover-bin sits OUTSIDE the agent quota tree (sibling of agents/), so
    without this aggregate cap an overwrite loop on an over-quota agent could
    grow it without bound and fill the backing filesystem. Eviction is
    oldest-first, atop the 7-day TTL.
    """
    cap = config.RECOVER_BIN_AGENT_MAX_BYTES
    if cap <= 0:
        return
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT entry_id, size FROM recover_bin "
            "WHERE agent_slug=%s ORDER BY binned_at ASC",
            (agent_slug,),
        ).fetchall()]
    total = sum(int(r["size"] or 0) for r in rows)
    evicted = 0
    for r in rows:
        if total + incoming <= cap:
            break
        delete(r["entry_id"])
        total -= int(r["size"] or 0)
        evicted += 1
    if evicted:
        logger.info(
            "recover-bin: evicted %d oldest entries for %s to stay under cap %d bytes",
            evicted, agent_slug, cap,
        )


def capture(
    agent_slug: str,
    rel_path: str,
    content: bytes,
    reason: str,
    ttl_days: int = RECOVER_BIN_TTL_DAYS,
) -> dict | None:
    """Back up the pre-change bytes of a removed/overwritten file.

    Returns the entry metadata, or ``None`` when nothing was binned — empty
    content, content over ``RECOVER_BIN_MAX_BYTES``, or a disk/DB failure.
    Binning is a best-effort safety net, NEVER a gate: the caller proceeds with
    the delete/overwrite regardless of the return value.
    """
    if reason not in VALID_REASONS:
        raise ValueError(f"recover-bin: invalid reason {reason!r}")
    if not content:
        return None  # nothing to recover (empty / zero-byte file)
    if not _is_recoverable_path(rel_path):
        return None  # session state / generated config / secrets — never binned
    if len(content) > config.RECOVER_BIN_MAX_BYTES:
        logger.info(
            "recover-bin: not binning %s/%s (%d bytes > cap %d)",
            agent_slug, rel_path, len(content), config.RECOVER_BIN_MAX_BYTES,
        )
        return None

    file_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    if _already_binned(agent_slug, rel_path, file_hash):
        return None  # idempotent — this exact version is already recoverable

    # Make room under the per-agent cap before writing (evict oldest-first).
    _enforce_agent_cap(agent_slug, len(content))

    scope, owner_sub = _resolve_scope_owner(rel_path)
    original_name = rel_path.rsplit("/", 1)[-1]
    entry_id = uuid.uuid4().hex
    now = _now()
    binned_at = now.isoformat()
    expires_at = (now + timedelta(days=ttl_days)).isoformat()

    # Write bytes FIRST so a committed row always points at recoverable content.
    # On any disk failure, bail out — never insert a row with missing bytes.
    dest = _entry_path(agent_slug, entry_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError:
        logger.exception("recover-bin: failed writing bytes for %s/%s",
                         agent_slug, rel_path)
        return None

    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO recover_bin
                       (entry_id, agent_slug, rel_path, original_name, reason,
                        scope, owner_sub, binned_at, file_hash, size, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (entry_id, agent_slug, rel_path, original_name, reason,
                 scope, owner_sub, binned_at, file_hash, len(content), expires_at),
            )
    except Exception:
        # Roll back the on-disk bytes so we don't leak an orphan file.
        logger.exception("recover-bin: insert failed for %s/%s",
                         agent_slug, rel_path)
        with contextlib.suppress(OSError):
            dest.unlink()
        return None

    return {
        "entry_id": entry_id,
        "agent_slug": agent_slug,
        "rel_path": rel_path,
        "original_name": original_name,
        "reason": reason,
        "scope": scope,
        "owner_sub": owner_sub,
        "binned_at": binned_at,
        "file_hash": file_hash,
        "size": len(content),
        "expires_at": expires_at,
    }


def list_for(
    agent_slug: str,
    requester_sub: str,
    can_edit: bool,
    can_manage: bool,
    is_admin: bool,
) -> list[dict]:
    """Return the non-expired entries the requester may restore, newest first.

    The restore tier mirrors the WRITE permission of each path: a member sees
    only their own ``users/<self>/`` files; an editor additionally sees the
    shared ``workspace/``; a manager additionally sees ``knowledge/`` + ``config/``;
    an admin sees everything. No cross-user leak — a member never sees another
    user's personal files.
    """
    now_iso = _now().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT entry_id, agent_slug, rel_path, original_name, reason,
                      scope, owner_sub, binned_at, file_hash, size, expires_at
                 FROM recover_bin
                WHERE agent_slug=%s AND expires_at > %s
                ORDER BY binned_at DESC""",
            (agent_slug, now_iso),
        ).fetchall()

    out: list[dict] = []
    for row in rows:
        d = dict(row)
        if can_restore(d, requester_sub, can_edit, can_manage, is_admin):
            out.append(d)
    return out


def get(entry_id: str) -> dict | None:
    """Return an entry's metadata row, or ``None`` if it doesn't exist."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recover_bin WHERE entry_id=%s", (entry_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def read_bytes(entry: dict) -> bytes | None:
    """Read an entry's captured bytes, or ``None`` if the file is gone.

    The metadata row can outlive its bytes (manual cleanup, partial disk loss);
    callers treat ``None`` as "unrecoverable" and skip the restore.
    """
    try:
        return _entry_path(entry["agent_slug"], entry["entry_id"]).read_bytes()
    except OSError:
        return None


def delete(entry_id: str) -> None:
    """Remove an entry's DB row and on-disk bytes (idempotent)."""
    entry = get(entry_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM recover_bin WHERE entry_id=%s", (entry_id,))
    if entry is not None:
        with contextlib.suppress(OSError):
            _entry_path(entry["agent_slug"], entry_id).unlink()


def delete_expired() -> int:
    """Drop entries past their TTL — both DB rows and on-disk bytes.

    ``expires_at`` and the comparison value are both
    ``datetime.now(utc).isoformat()`` (fixed ``+00:00`` offset), so the
    lexicographic TEXT comparison matches the chronological one — the same
    ISO-string convention the rest of the schema uses. Returns rows removed.
    """
    now_iso = _now().isoformat()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT entry_id, agent_slug FROM recover_bin WHERE expires_at <= %s",
            (now_iso,),
        ).fetchall()]
        if rows:
            conn.execute(
                "DELETE FROM recover_bin WHERE expires_at <= %s", (now_iso,),
            )

    touched_dirs = set()
    for row in rows:
        path = _entry_path(row["agent_slug"], row["entry_id"])
        with contextlib.suppress(OSError):
            path.unlink()
        touched_dirs.add(path.parent)
    # Prune now-empty per-agent dirs (rmdir only succeeds when empty).
    for d in touched_dirs:
        with contextlib.suppress(OSError):
            d.rmdir()
    return len(rows)
