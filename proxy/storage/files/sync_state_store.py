"""Versioned file-sync base state — the ``base`` of the 3-way merge.

One row per ``(machine_id, agent_slug, rel_path)``: the hash the platform last
**converged** with that satellite for that file. ``core/remote/file_sync.py::diff_manifests``
reads it to tell who changed since the last sync (platform / satellite / neither),
which is what turns "proxy-always-wins" into "newest-version-wins".

A **cache + change-attribution hint, never a delete-authority**:
it is reconciled from every manifest, so a stale or missing row degrades to
"first sync", never to data loss. ``base_mtime`` is the PLATFORM file's mtime
(platform clock) — a re-hash-cache hint, NOT a merge input (the merge attributes
by hash, not time).

All functions are synchronous (called via ``asyncio.to_thread`` from async code).
The pooled connection commits on clean context exit (see ``storage/pg.py``).
"""

import logging
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger("claude-proxy.sync-state")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_for_machine_agent(
    machine_id: str, agent_slug: str,
) -> dict[str, tuple[str, float]]:
    """Return ``{rel_path: (base_hash, base_mtime)}`` for one (machine, agent)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rel_path, base_hash, base_mtime FROM sync_state "
            "WHERE machine_id=%s AND agent_slug=%s",
            (machine_id, agent_slug),
        ).fetchall()
    return {r["rel_path"]: (r["base_hash"], r["base_mtime"]) for r in rows}


_UPSERT = """
    INSERT INTO sync_state
        (machine_id, agent_slug, rel_path, base_hash, base_mtime, updated_at)
    VALUES (%s,%s,%s,%s,%s,%s)
    ON CONFLICT (machine_id, agent_slug, rel_path)
    DO UPDATE SET base_hash=EXCLUDED.base_hash,
                  base_mtime=EXCLUDED.base_mtime,
                  updated_at=EXCLUDED.updated_at
"""


def get_one(
    machine_id: str, agent_slug: str, rel_path: str,
) -> tuple[str, float] | None:
    """Return ``(base_hash, base_mtime)`` for one file, or ``None`` — used by the
    live write-back path to detect a clobber (platform changed since this base)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT base_hash, base_mtime FROM sync_state "
            "WHERE machine_id=%s AND agent_slug=%s AND rel_path=%s",
            (machine_id, agent_slug, rel_path),
        ).fetchone()
    return (row["base_hash"], row["base_mtime"]) if row is not None else None


def record_one(
    machine_id: str, agent_slug: str, rel_path: str,
    base_hash: str, base_mtime: float,
) -> None:
    """Upsert a single converged base — used by the live write-back path so the
    next session-start merge sees the file as in-sync (no phantom conflict)."""
    with get_conn() as conn:
        conn.execute(
            _UPSERT,
            (machine_id, agent_slug, rel_path, base_hash, base_mtime, _now_iso()),
        )


def record_synced_many(
    machine_id: str, agent_slug: str,
    rows: list[tuple[str, str, float]],
) -> None:
    """Atomically upsert many converged bases.

    ``rows`` = ``[(rel_path, base_hash, base_mtime), ...]``. Called from the
    session-start merge **after** the per-file durable ack, so base only ever
    advances to a hash we know both sides hold.
    """
    if not rows:
        return
    now = _now_iso()
    params = [
        (machine_id, agent_slug, rp, bh, bm, now) for (rp, bh, bm) in rows
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, params)


def agents_for_machine(machine_id: str) -> set[str]:
    """Distinct ``agent_slug``s this machine has converged at least one file for —
    the set to re-sync on reconnect (catch up deletes/drift without a session)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_slug FROM sync_state WHERE machine_id=%s",
            (machine_id,),
        ).fetchall()
    return {r["agent_slug"] for r in rows}


def clear_one(machine_id: str, agent_slug: str, rel_path: str) -> None:
    """Drop the base for a single file — it went absent on both sides (converged
    to deleted), so there is nothing left to attribute changes against."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sync_state "
            "WHERE machine_id=%s AND agent_slug=%s AND rel_path=%s",
            (machine_id, agent_slug, rel_path),
        )
