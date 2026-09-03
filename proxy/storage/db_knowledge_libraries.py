"""Shared knowledge libraries — promote/attach rows.

A ``knowledge_libraries`` row marks a SUBTREE of the source agent's
``knowledge/`` folder as an installation-wide library — ``subdir = ''``
is the whole-folder share, a non-empty ``subdir`` shares just that
subtree. Subtrees of one agent are disjoint (validated at promote), so
any knowledge path belongs to at most one library.
``knowledge_library_attachments`` rows are the consumers, keyed
``(source_agent, subdir, consumer_agent)``. Deliberately UNCACHED
(delegation-targets precedent): rows are read per call, so
promote/attach changes need no invalidation hook — they take effect
wherever the next read happens (mounts and prompts still bake at
session spawn).

The attachment's ``writable`` flag is the single source of truth for
every RO/RW decision: sandbox bind disposition, the satellite write-back
subtree rule, and mirror→source propagation in the projector.

No FKs by design — un-promote stays a plain delete; attachment rows are
deleted alongside it in the same transaction.

All functions are synchronous (called via asyncio.to_thread from async
code).
"""

import logging
from datetime import datetime, timezone

from storage.pg import get_conn

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bulletin_rel(subdir: str, name: str) -> str:
    """SOURCE-ROOT-relative path of a library's bulletin file:
    ``<subtree root>/bulletin/<library name>.md``. The folder AND filename
    both carry the library's display name convention — reserved by
    convention, documented in KNOWLEDGE-LIBRARIES.md. '' when the library
    has no (valid) name."""
    if not name:
        return ""
    return f"{subdir}/bulletin/{name}.md" if subdir else f"bulletin/{name}.md"


def subtree_covers(subdir: str, rel: str) -> bool:
    """Segment-wise: is source-root-relative ``rel`` inside the library
    subtree ``subdir``? The root library ('') covers everything. Matching
    is by whole path segments — a library ``marketing`` never covers a
    sibling ``marketing-extra`` — and exact-case (promote-time
    disjointness is what case-folds, for case-insensitive satellites)."""
    if not subdir:
        return True
    sub_parts = subdir.split("/")
    rel_parts = rel.split("/") if rel else []
    return rel_parts[: len(sub_parts)] == sub_parts


def promote(source_agent: str, *, created_by: str, name: str = "",
            subdir: str = "") -> bool:
    """Share one library — a subtree (``subdir``) of the agent's knowledge
    folder, '' for the whole folder.

    ``name`` is the library's human label. It never reaches a mirror path
    (the mirror stays ``knowledge/shared/<source_agent>/<subdir>/``) but it
    DOES drive the bulletin filename, so the API validates it as a
    filename before calling here. Re-promoting an existing (source,
    subdir) updates the label, so renaming works without detach/reattach.

    Disjointness/path validation is the API layer's job (it owns the
    request cycle and the filesystem checks); this store enforces only
    the key shape.

    Returns True if newly promoted, False if it already was.
    """
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO knowledge_libraries "
            "(source_agent, subdir, created_by, created_at, name) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (source_agent, subdir) DO UPDATE SET name = EXCLUDED.name "
            # rowcount can't tell insert from update once DO UPDATE is in
            # play (both report 1); xmax is 0 only on a fresh insert.
            "RETURNING (xmax = 0) AS inserted",
            (source_agent, subdir, created_by, _now(), name),
        ).fetchone()
        return bool(row["inserted"]) if row else False


def unpromote(source_agent: str, subdir: str = "") -> list[str]:
    """Un-share ONE library; detaches its consumers in the same
    transaction (no FK cascade — the delete pair is explicit).

    Returns the consumer slugs that were detached (the caller tears down
    their mirror subtrees).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "DELETE FROM knowledge_library_attachments "
            "WHERE source_agent = %s AND subdir = %s "
            "RETURNING consumer_agent",
            (source_agent, subdir),
        ).fetchall()
        conn.execute(
            "DELETE FROM knowledge_libraries "
            "WHERE source_agent = %s AND subdir = %s",
            (source_agent, subdir),
        )
    return [r["consumer_agent"] for r in rows]


def is_promoted(source_agent: str, subdir: str | None = None) -> bool:
    """Any library of this agent (``subdir=None``), or the specific one."""
    with get_conn() as conn:
        if subdir is None:
            row = conn.execute(
                "SELECT 1 FROM knowledge_libraries WHERE source_agent = %s "
                "LIMIT 1",
                (source_agent,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM knowledge_libraries "
                "WHERE source_agent = %s AND subdir = %s",
                (source_agent, subdir),
            ).fetchone()
    return row is not None


def libraries_of(source_agent: str) -> list[dict]:
    """This agent's own shared libraries: ``{subdir, name, created_by,
    created_at}`` rows, root share first then by subdir."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT subdir, name, created_by, created_at "
            "FROM knowledge_libraries WHERE source_agent = %s "
            "ORDER BY subdir",
            (source_agent,),
        ).fetchall()
    return [dict(r) for r in rows]


def library_name(source_agent: str, subdir: str = "") -> str:
    """The library's display label, or '' when unshared / never labelled."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM knowledge_libraries "
            "WHERE source_agent = %s AND subdir = %s",
            (source_agent, subdir),
        ).fetchone()
    return (row["name"] if row else "") or ""


def list_libraries() -> list[dict]:
    """Every library with its consumer count (newest first)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT l.source_agent, l.subdir, l.created_by, l.created_at, "
            "       l.name, COUNT(a.consumer_agent) AS consumers "
            "FROM knowledge_libraries l "
            "LEFT JOIN knowledge_library_attachments a "
            "       ON a.source_agent = l.source_agent AND a.subdir = l.subdir "
            "GROUP BY l.source_agent, l.subdir, l.created_by, l.created_at, "
            "         l.name "
            "ORDER BY l.created_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def attach(source_agent: str, consumer_agent: str, *,
           writable: bool, created_by: str, subdir: str = "") -> bool:
    """Attach a library to a consumer. Requires the library row to exist
    (enforced here, not by FK, so un-promote stays a plain delete).

    Returns True if newly attached; False if the triple already existed
    (use :func:`set_writable` to change the flag — re-attach never
    silently flips it, mirroring the observe-edge rule).
    """
    with get_conn() as conn:
        lib = conn.execute(
            "SELECT 1 FROM knowledge_libraries "
            "WHERE source_agent = %s AND subdir = %s",
            (source_agent, subdir),
        ).fetchone()
        if lib is None:
            label = f"{source_agent}/{subdir}" if subdir else source_agent
            raise ValueError(f"'{label}' is not a shared knowledge library")
        cur = conn.execute(
            "INSERT INTO knowledge_library_attachments "
            "(source_agent, subdir, consumer_agent, writable, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_agent, subdir, consumer_agent) DO NOTHING",
            (source_agent, subdir, consumer_agent, writable, created_by, _now()),
        )
        return cur.rowcount > 0


def detach(source_agent: str, consumer_agent: str, subdir: str = "") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM knowledge_library_attachments "
            "WHERE source_agent = %s AND subdir = %s AND consumer_agent = %s",
            (source_agent, subdir, consumer_agent),
        )
        return cur.rowcount > 0


def set_writable(source_agent: str, consumer_agent: str, writable: bool,
                 subdir: str = "") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE knowledge_library_attachments SET writable = %s "
            "WHERE source_agent = %s AND subdir = %s AND consumer_agent = %s",
            (writable, source_agent, subdir, consumer_agent),
        )
        return cur.rowcount > 0


def attachments_for_consumer(consumer_agent: str) -> list[dict]:
    """The consumer's attachments — drives its mounts, prompt and gates.

    Each row: ``{source_agent, subdir, writable, name}``, ordered by
    (source slug, subdir) so mount/prompt rendering is deterministic.
    ``name`` is the library's display label (empty when never set); the
    write gates key on ``(source_agent, subdir)``, which is also the
    mirror's folder path under ``knowledge/shared/``.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.source_agent, a.subdir, a.writable, "
            "       COALESCE(l.name, '') AS name "
            "FROM knowledge_library_attachments a "
            "LEFT JOIN knowledge_libraries l "
            "       ON l.source_agent = a.source_agent AND l.subdir = a.subdir "
            "WHERE a.consumer_agent = %s ORDER BY a.source_agent, a.subdir",
            (consumer_agent,),
        ).fetchall()
    return [dict(r) for r in rows]


def consumers_of(source_agent: str, subdir: str | None = None) -> list[dict]:
    """Consumers of one library (``subdir`` given) or of every library of
    the source (``subdir=None``): ``{subdir, consumer_agent, writable}``."""
    with get_conn() as conn:
        if subdir is None:
            rows = conn.execute(
                "SELECT subdir, consumer_agent, writable "
                "FROM knowledge_library_attachments "
                "WHERE source_agent = %s ORDER BY subdir, consumer_agent",
                (source_agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT subdir, consumer_agent, writable "
                "FROM knowledge_library_attachments "
                "WHERE source_agent = %s AND subdir = %s "
                "ORDER BY consumer_agent",
                (source_agent, subdir),
            ).fetchall()
    return [dict(r) for r in rows]


def writable_pairs_for(consumer_agent: str) -> frozenset[tuple[str, str]]:
    """``(source_agent, subdir)`` pairs the consumer is RW-attached to —
    the shape ``can_write_back``'s subtree rule consumes."""
    return frozenset(
        (a["source_agent"], a["subdir"])
        for a in attachments_for_consumer(consumer_agent)
        if a["writable"]
    )


def writable_attachment(source_agent: str, consumer_agent: str,
                        subdir: str = "") -> bool | None:
    """The exact (library, consumer) pair's writable flag, or None when not
    attached. For path-based resolution use :func:`attachment_covering`."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT writable FROM knowledge_library_attachments "
            "WHERE source_agent = %s AND subdir = %s AND consumer_agent = %s",
            (source_agent, subdir, consumer_agent),
        ).fetchone()
    return bool(row["writable"]) if row else None


def attachment_covering(source_agent: str, consumer_agent: str,
                        sub_rel: str) -> dict | None:
    """The consumer's attachment whose library subtree contains the
    source-root-relative path ``sub_rel`` (segment-wise), or None.
    Disjointness guarantees at most one match."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT subdir, writable FROM knowledge_library_attachments "
            "WHERE source_agent = %s AND consumer_agent = %s",
            (source_agent, consumer_agent),
        ).fetchall()
    for r in rows:
        if subtree_covers(r["subdir"], sub_rel):
            return dict(r)
    return None


def library_covering(source_agent: str, sub_rel: str) -> dict | None:
    """The source's library whose subtree contains the source-root-relative
    path ``sub_rel``, or None: ``{subdir, name}``."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT subdir, name FROM knowledge_libraries "
            "WHERE source_agent = %s",
            (source_agent,),
        ).fetchall()
    for r in rows:
        if subtree_covers(r["subdir"], sub_rel):
            return dict(r)
    return None
