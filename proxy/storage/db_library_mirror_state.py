"""Shared-library mirror merge base — ``library_mirror_state`` rows.

One row per (consumer, source, source-root-relative file): the content hash
the consumer's mirror and the source last CONVERGED on, plus the size/mtime
quick-check pair. Written by ``services/knowledge/library_projector`` after
every projection/adoption; read at the start of every reconcile pass. The
projector is the only writer. Semantics (why this exists) are documented on
the table DDL in ``storage/schema.py`` and in KNOWLEDGE-LIBRARIES.md.

All functions are synchronous (called via ``asyncio.to_thread``).
"""

from datetime import datetime, timezone

from storage.pg import get_conn

_UPSERT = (
    "INSERT INTO library_mirror_state "
    "(consumer_agent, source_agent, sub_rel, base_hash, base_size, base_mtime, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (consumer_agent, source_agent, sub_rel) DO UPDATE SET "
    "base_hash = EXCLUDED.base_hash, base_size = EXCLUDED.base_size, "
    "base_mtime = EXCLUDED.base_mtime, updated_at = EXCLUDED.updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_map(consumer_agent: str, source_agent: str) -> dict[str, dict]:
    """``{sub_rel: {base_hash, base_size, base_mtime}}`` for every file the
    consumer's mirror of ``source_agent`` has converged on (all libraries of
    that source — the caller filters by subtree)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sub_rel, base_hash, base_size, base_mtime "
            "FROM library_mirror_state "
            "WHERE consumer_agent = %s AND source_agent = %s",
            (consumer_agent, source_agent),
        ).fetchall()
    return {
        r["sub_rel"]: {
            "base_hash": r["base_hash"],
            "base_size": int(r["base_size"] or 0),
            "base_mtime": float(r["base_mtime"] or 0.0),
        }
        for r in rows
    }


def get_one(consumer_agent: str, source_agent: str, sub_rel: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT base_hash, base_size, base_mtime FROM library_mirror_state "
            "WHERE consumer_agent = %s AND source_agent = %s AND sub_rel = %s",
            (consumer_agent, source_agent, sub_rel),
        ).fetchone()
    if row is None:
        return None
    return {
        "base_hash": row["base_hash"],
        "base_size": int(row["base_size"] or 0),
        "base_mtime": float(row["base_mtime"] or 0.0),
    }


def upsert_many(rows: list[tuple[str, str, str, str, int, float]]) -> None:
    """``rows`` = ``[(consumer, source, sub_rel, base_hash, base_size, base_mtime), …]``
    — one statement, one transaction."""
    if not rows:
        return
    now = _now()
    params = [(c, s, r, h, int(sz), float(mt), now) for (c, s, r, h, sz, mt) in rows]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, params)


def upsert(consumer_agent: str, source_agent: str, sub_rel: str,
           base_hash: str, base_size: int, base_mtime: float) -> None:
    upsert_many([(consumer_agent, source_agent, sub_rel, base_hash, base_size, base_mtime)])


def delete_many(keys: list[tuple[str, str, str]]) -> None:
    """``keys`` = ``[(consumer, source, sub_rel), …]``."""
    if not keys:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "DELETE FROM library_mirror_state "
                "WHERE consumer_agent = %s AND source_agent = %s AND sub_rel = %s",
                keys,
            )


def delete_rel_all_consumers(source_agent: str, sub_rel: str) -> int:
    """Drop the base for one source file across EVERY consumer (the file is
    gone at the source — any surviving mirror copy heals by deletion)."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM library_mirror_state WHERE source_agent = %s AND sub_rel = %s",
            (source_agent, sub_rel),
        )
        return cur.rowcount or 0


def delete_consumer_subtree(consumer_agent: str, source_agent: str,
                            subdir: str = "") -> int:
    """Drop every base row of one consumer for one library subtree
    (``subdir`` '' = every library of that source) — attachment teardown."""
    with get_conn() as conn:
        if subdir:
            cur = conn.execute(
                "DELETE FROM library_mirror_state "
                "WHERE consumer_agent = %s AND source_agent = %s "
                "AND (sub_rel = %s OR sub_rel LIKE %s)",
                (consumer_agent, source_agent, subdir, subdir + "/%"),
            )
        else:
            cur = conn.execute(
                "DELETE FROM library_mirror_state "
                "WHERE consumer_agent = %s AND source_agent = %s",
                (consumer_agent, source_agent),
            )
        return cur.rowcount or 0


def delete_source(source_agent: str) -> int:
    """Source-agent deletion / un-promote of everything: drop all rows."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM library_mirror_state WHERE source_agent = %s",
            (source_agent,),
        )
        return cur.rowcount or 0
