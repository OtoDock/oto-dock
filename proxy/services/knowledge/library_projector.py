"""Shared-knowledge-library projector — keeps consumer mirrors true.

A promoted library — a subtree (``subdir``, '' = whole folder) of the
source agent's ``knowledge/`` — is mirrored into every attached consumer
at ``agents/<consumer>/knowledge/shared/<source>/<subdir>/``: the mirror
path is the SOURCE-ROOT-RELATIVE path under the slug segment, so
mirror→source mapping is identity (strip ``shared/<slug>/``) and the
whole-folder share keeps the exact pre-subdir layout. Mirrors are REAL
files on the platform host — that is what makes every non-sandbox
surface (dashboard tree, prompt workspace tree, satellite manifests and
fan-out) work without special cases. This module is the only writer of
mirror content and the only mirror→source bridge.

Merge base (2026-09-03): every converged (consumer, file) pair is
remembered in ``library_mirror_state`` (``storage/db_library_mirror_state``)
— the content hash + size/mtime the mirror and the source last agreed on,
the ``sync_state`` idea applied to libraries. It is what turns a bare
"the mirror lacks this file" into one of three things:

- no base row → the mirror never had it → project it (first sync);
- base row and the source still equals the base → the consumer DELETED it
  → for a WRITABLE attachment that is a deliberate delete: the source copy
  is captured to the recover-bin ("deleted"), removed at the source and
  from every other mirror; read-only attachments heal as before;
- base row but the source moved on since → the source wins, the mirror is
  healed (a stale delete never beats a newer edit).

And the mirror-holds/source-lacks case, which used to re-adopt a file into
the source from any writable mirror (undoing a source-side delete the
moment another consumer still held a copy), now reads the base too: a
base row means the SOURCE deleted it → the mirror copy is removed (kept in
the recover-bin only if it diverged from the base); no base row means a
genuinely new mirror file → adopted (RW) or captured+removed (RO).

Guards (the satellite delete-attribution incident class stays closed):
- MASS-DELETE GUARD — one reconcile pass never propagates more deletes
  from one consumer's mirror than ``max(MASS_DELETE_MIN,
  MASS_DELETE_FRACTION × tracked files)`` of that library: a wiped or
  restored consumer directory heals like before (WARNING logged).
- a deliberate delete requires HASH equality between the source and the
  base (never the quick-check alone);
- teardown drops the consumer's base rows before touching files;
- an absent/stale base row degrades to first-projection behaviour (heal),
  never to a delete.

Direction rules otherwise unchanged:
- source → mirrors: always, including deletions (the source is truth).
- mirror → source: only for WRITABLE attachments — creates/modifies
  discovered by comparison, EXPLICIT deletes through
  :func:`propagate_mirror_delete` (dashboard file API, satellite live
  deletes on RW mirrors), and reconcile-detected deliberate deletes above.
- read-only attachments: a locally edited mirror file (newer mtime,
  different bytes) is captured to the consumer's recover-bin
  ("conflict") and healed from the source.
- mirror content OUTSIDE every attached library subtree (possible only
  via direct-bind sandbox writes that bypass the hook) is captured the
  same way and removed — the projector owns the mirror namespace.

Freshness: mirrors carry the source file's mtime (``os.utime`` after
every projection); an equal size AND equal mtime pair is treated as
identical without re-hashing (rsync quick-check — the base row records
the hash for later attribution). When both sides changed, the newer mtime
wins — knowledge folders are git-versioned at both ends, which is the
deep-history safety net, so the projector does not attempt three-way
content merges.

Timing: the 5-minute sweep (:func:`reconcile_loop`) is the backstop;
:func:`schedule_reconcile_for_agent` is kicked at every chat/task turn
end so an agent's own sandbox delete or write lands before it reports.

Exclusions (never projected, in either direction) are keyed on the
SOURCE-ROOT-RELATIVE path: top-level ``memory/`` (agent memory) and
``shared/`` itself (no transitive nesting) — a subdir library's internal
folder that happens to be named ``memory`` is legitimate content;
``.git`` and ``.credentials`` at ANY depth (segment rule, matching
file_sync's ``.credentials`` push rule — a git clone inside a knowledge
folder must never fan its object store out to consumers); plus
``.partial`` temporaries.
"""

import asyncio
import errno
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

import config
from storage import db_knowledge_libraries, db_library_mirror_state, recover_bin_store
from storage.db_knowledge_libraries import subtree_covers

logger = logging.getLogger("claude-proxy.knowledge-libraries")

SHARED_SUBDIR = "shared"
_EXCLUDED_TOP = frozenset({"memory", SHARED_SUBDIR})
_EXCLUDED_SEGMENTS = frozenset({".git", ".credentials"})
RECONCILE_INTERVAL_S = 300
# Turn-end kick debounce per source (a burst of turn ends on one library
# collapses into one reconcile every 10 s at most).
RECONCILE_KICK_DEBOUNCE_S = 10.0
# Mass-delete guard: a single pass propagates at most max(MIN, FRACTION ×
# tracked files) deliberate deletes from one consumer's mirror of one
# library; more than that is treated as a wiped mirror and healed instead.
MASS_DELETE_MIN = 25
MASS_DELETE_FRACTION = 0.5

# Serializes work per source agent — overlapping reconciles would race
# their .partial files and double-fan-out.
_locks: dict[str, asyncio.Lock] = {}
# Turn-end kick bookkeeping (per source).
_kick_last: dict[str, float] = {}
_kick_pending: set[str] = set()


def _lock(source_agent: str) -> asyncio.Lock:
    return _locks.setdefault(source_agent, asyncio.Lock())


def source_knowledge_dir(source_agent: str) -> Path:
    return config.get_agent_dir(source_agent) / "knowledge"


def mirror_dir(consumer_agent: str, source_agent: str) -> Path:
    """The consumer's mirror root for one SOURCE (slug segment). Library
    subtrees of that source live below it at their own ``subdir``."""
    return (config.get_agent_dir(consumer_agent) / "knowledge"
            / SHARED_SUBDIR / source_agent)


def is_excluded_rel(rel: str) -> bool:
    """True for library-internal paths that must never be projected.
    ``rel`` is SOURCE-ROOT-relative (``<subdir>/<lib rel>`` for a subdir
    library) — so only the true top-level ``memory``/``shared`` are
    excluded, never a nested folder of a subdir library."""
    parts = rel.split("/")
    if not parts or parts[0] in _EXCLUDED_TOP:
        return True
    if not _EXCLUDED_SEGMENTS.isdisjoint(parts):
        return True
    return parts[-1].endswith(".partial")


def parse_library_rel(rel_path: str) -> tuple[str, str] | None:
    """Split an agent-tree rel path into ``(source_slug, sub_rel)`` when it
    lies inside the mirror namespace (``knowledge/shared/<src>/…``), else
    None. ``sub_rel`` is SOURCE-ROOT-relative (identity mapping — it
    includes the library subdir); resolving WHICH library covers it is the
    store's ``attachment_covering`` / ``library_covering``.
    """
    parts = rel_path.split("/")
    if len(parts) >= 4 and parts[0] == "knowledge" and parts[1] == SHARED_SUBDIR:
        return parts[2], "/".join(parts[3:])
    return None


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_rel(root: Path, *, prune_top: bool = True) -> dict[str, os.stat_result]:
    """Relative file paths under ``root`` (excluding projector exclusions).

    ``prune_top=False`` for a SUBDIR library walk: the top-level
    ``memory``/``shared`` reservation applies at the source ROOT only —
    a subdir library's own first-level folder with one of those names is
    legitimate content. Segment exclusions prune at every level always."""
    out: dict[str, os.stat_result] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        # Segment exclusions prune at EVERY level (nested .git/.credentials
        # never even get walked); top-level exclusions prune at the root.
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_SEGMENTS]
        if rel_dir == ".":
            if prune_top:
                dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_TOP]
            rel_dir = ""
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel.endswith(".partial"):
                continue
            p = Path(dirpath) / name
            # Defensive: symlinked files are rejected at promote and never
            # projected — a link crossing knowledge subtrees would leak
            # content past the library boundary.
            if p.is_symlink():
                continue
            try:
                out[rel] = p.stat()
            except OSError:
                continue
    return out


def _copy_file(src: Path, dest: Path) -> str:
    """`.partial` + fsync + atomic replace; carries the source mtime so the
    freshness comparison stays meaningful on the copy. Returns the sha256
    of the copied bytes (hashed while streaming — no second read) so the
    caller can record the merge base."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dest)
        st = src.stat()
        os.utime(dest, (st.st_atime, st.st_mtime))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return h.hexdigest()


def _quick_same(a: os.stat_result, b: os.stat_result) -> bool:
    """rsync-style quick check: equal size AND equal whole-second mtime."""
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def _matches_base(st: os.stat_result, base: dict | None) -> bool:
    return (base is not None and st.st_size == base["base_size"]
            and int(st.st_mtime) == int(base["base_mtime"]))


async def _fan_out(agent_slug: str, rel_path: str, path: Path | None) -> None:
    """Best-effort satellite propagation of one mirror/source change."""
    from services.remote import workspace_fanout
    try:
        if path is None:
            await workspace_fanout.fan_out_delete(
                agent_slug, rel_path, include_idle=True)
        else:
            if workspace_fanout.has_fanout_candidates(
                    agent_slug, rel_path, include_idle=True):
                await workspace_fanout.fan_out_write(
                    agent_slug, rel_path, path, include_idle=True)
    except Exception:
        logger.exception("library fan-out failed: %s %s", agent_slug, rel_path)


def _capture_and_log(agent_slug: str, rel_path: str, path: Path, reason: str) -> None:
    try:
        content = path.read_bytes()
    except OSError:
        return
    try:
        recover_bin_store.capture(agent_slug, rel_path, content, reason)
    except Exception:
        logger.exception("library recover-bin capture failed: %s/%s",
                         agent_slug, rel_path)


class _BaseBook:
    """Merge-base bookkeeping for one reconcile: rows loaded up front, the
    pass's upserts/drops collected in memory and flushed in two statements
    at the end (a steady-state sweep writes nothing)."""

    def __init__(self, source_agent: str) -> None:
        self.source = source_agent
        self._loaded: dict[str, dict[str, dict]] = {}
        self._upserts: dict[tuple[str, str], tuple[str, int, float]] = {}
        self._drops: set[tuple[str, str]] = set()

    def load(self, consumer: str) -> None:
        if consumer not in self._loaded:
            self._loaded[consumer] = db_library_mirror_state.get_map(consumer, self.source)

    def get(self, consumer: str, sub_rel: str) -> dict | None:
        key = (consumer, sub_rel)
        if key in self._drops:
            return None
        up = self._upserts.get(key)
        if up is not None:
            return {"base_hash": up[0], "base_size": up[1], "base_mtime": up[2]}
        return self._loaded.get(consumer, {}).get(sub_rel)

    def tracked(self, consumer: str, subdir: str) -> int:
        rows = self._loaded.get(consumer, {})
        return sum(1 for r in rows if subtree_covers(subdir, r))

    def set(self, consumer: str, sub_rel: str, base_hash: str,
            size: int, mtime: float) -> None:
        key = (consumer, sub_rel)
        self._drops.discard(key)
        self._upserts[key] = (base_hash, int(size), float(mtime))

    def drop(self, consumer: str, sub_rel: str) -> None:
        key = (consumer, sub_rel)
        self._upserts.pop(key, None)
        self._drops.add(key)

    def flush(self) -> None:
        if self._upserts:
            db_library_mirror_state.upsert_many([
                (c, self.source, r, h, sz, mt)
                for (c, r), (h, sz, mt) in self._upserts.items()
            ])
        if self._drops:
            db_library_mirror_state.delete_many(
                [(c, self.source, r) for (c, r) in self._drops])
        self._upserts.clear()
        self._drops.clear()


def _sync_pair_files(src_file: Path, mir_file: Path, *, writable: bool,
                     consumer: str, source: str, sub_rel: str,
                     base: dict | None = None,
                     deliberate_delete: bool = False,
                     other_mirrors: list[tuple[str, Path]] = (),
                     book: "_BaseBook | None" = None,
                     ) -> tuple[list[tuple[str, str, Path | None]], bool]:
    """Reconcile ONE file pair. ``sub_rel`` is SOURCE-ROOT-relative.
    Returns (fanout jobs, source_changed).

    ``base`` is this consumer's merge base for the file (None = never
    converged). ``deliberate_delete`` is decided by the caller (RW
    attachment, base present, source hash == base hash, mass-delete guard
    passed): the source-only file is then removed at the source and from
    ``other_mirrors`` (``(consumer_slug, mirror_path)`` pairs). ``book``
    receives the base updates; None = no bookkeeping (targeted paths).

    The library's ``bulletin/`` subtree gets NO special-casing since 1.5:
    a writable attachment covers bulletin authorship like any other
    library content (operator decision 2026-08-28); read-only mirrors
    keep the capture+heal behavior for every path, bulletins included.

    Jobs are ``(agent_slug, rel_path, path-or-None)`` — path None = delete.
    """
    jobs: list[tuple[str, str, Path | None]] = []
    adopt = writable
    src_exists, mir_exists = src_file.is_file(), mir_file.is_file()
    mirror_rel = f"knowledge/{SHARED_SUBDIR}/{source}/{sub_rel}"
    source_rel = f"knowledge/{sub_rel}"

    def _set(h: str, st: os.stat_result) -> None:
        if book is not None:
            book.set(consumer, sub_rel, h, st.st_size, st.st_mtime)

    def _drop(c: str = consumer) -> None:
        if book is not None:
            book.drop(c, sub_rel)

    if src_exists and not mir_exists:
        if deliberate_delete:
            # The consumer removed a file it had converged on and the source
            # has not changed since → propagate: capture the source bytes,
            # delete at the source, remove every other mirror's copy (kept
            # in ITS recover-bin only if it diverged from the source).
            src_st = src_file.stat()
            _capture_and_log(source, source_rel, src_file, "deleted")
            src_file.unlink(missing_ok=True)
            jobs.append((source, source_rel, None))
            _drop()
            for oc, om in other_mirrors:
                if om.is_file():
                    try:
                        if not _quick_same(om.stat(), src_st):
                            _capture_and_log(
                                oc, f"knowledge/{SHARED_SUBDIR}/{source}/{sub_rel}",
                                om, "conflict")
                        om.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("library mirror delete failed: %s %s", oc, sub_rel)
                        continue
                    jobs.append((oc, f"knowledge/{SHARED_SUBDIR}/{source}/{sub_rel}", None))
                _drop(oc)
            logger.info("library delete propagated from mirror %s → source %s: %s",
                        consumer, source, sub_rel)
            return jobs, True
        h = _copy_file(src_file, mir_file)
        _set(h, mir_file.stat())
        jobs.append((consumer, mirror_rel, mir_file))
        return jobs, False
    if mir_exists and not src_exists:
        if base is not None:
            # The mirror converged on this file before and the source no
            # longer has it → the SOURCE deleted it: heal by deletion, never
            # re-adopt. A locally edited copy goes to the recover-bin.
            try:
                if not _matches_base(mir_file.stat(), base):
                    _capture_and_log(consumer, mirror_rel, mir_file, "conflict")
            except OSError:
                pass
            mir_file.unlink(missing_ok=True)
            jobs.append((consumer, mirror_rel, None))
            _drop()
            return jobs, False
        if adopt:
            # New file authored in an RW mirror → adopt into the source.
            h = _copy_file(mir_file, src_file)
            _set(h, src_file.stat())
            jobs.append((source, source_rel, src_file))
            return jobs, True
        # RO mirror holds a file the source doesn't → capture + heal (the
        # source may have deleted it, or someone smuggled a file in).
        _capture_and_log(consumer, mirror_rel, mir_file, "conflict")
        mir_file.unlink(missing_ok=True)
        jobs.append((consumer, mirror_rel, None))
        _drop()
        return jobs, False
    if not src_exists and not mir_exists:
        if base is not None:
            _drop()
        return jobs, False

    src_st, mir_st = src_file.stat(), mir_file.stat()
    if _quick_same(src_st, mir_st):
        # Converged pair. Record the base once (hash) when it is missing or
        # stale; steady state costs a stat per side, no reads.
        if not _matches_base(src_st, base):
            _set(_hash_file(src_file), src_st)
        return jobs, False
    if src_st.st_size == mir_st.st_size:
        src_hash = _hash_file(src_file)
        if src_hash == _hash_file(mir_file):
            if int(src_st.st_mtime) != int(mir_st.st_mtime):
                os.utime(mir_file, (src_st.st_atime, src_st.st_mtime))
            _set(src_hash, src_st)
            return jobs, False

    mirror_is_newer = mir_st.st_mtime > src_st.st_mtime
    if mirror_is_newer and adopt:
        h = _copy_file(mir_file, src_file)
        _set(h, src_file.stat())
        jobs.append((source, source_rel, src_file))
        return jobs, True
    if mirror_is_newer:
        # Local edit of a read-only mirror: revert-to-trash, then heal.
        _capture_and_log(consumer, mirror_rel, mir_file, "conflict")
    h = _copy_file(src_file, mir_file)
    _set(h, mir_file.stat())
    jobs.append((consumer, mirror_rel, mir_file))
    return jobs, False


async def reconcile_source(source_agent: str) -> None:
    """Full sweep of one source's libraries: heal every consumer mirror,
    adopt RW additions, propagate source deletions AND deliberate RW-mirror
    deletions (merge-base attributed, mass-delete guarded), and remove
    mirror strays outside every attached subtree. Safe to call anytime."""
    attachments = await asyncio.to_thread(
        db_knowledge_libraries.consumers_of, source_agent)
    if not attachments:
        return
    async with _lock(source_agent):
        src_root = source_knowledge_dir(source_agent)
        all_jobs: list[tuple[str, str, Path | None]] = []
        source_changed = False
        book = _BaseBook(source_agent)

        # consumer → its attached (subdir, writable) rows for this source;
        # subdir → every consumer of that library (for cross-mirror deletes).
        by_consumer: dict[str, list[tuple[str, bool]]] = {}
        lib_consumers: dict[str, list[str]] = {}
        for att in attachments:
            by_consumer.setdefault(att["consumer_agent"], []).append(
                (att["subdir"] or "", bool(att["writable"])))
            lib_consumers.setdefault(att["subdir"] or "", []).append(att["consumer_agent"])

        def _sync_library(consumer: str, subdir: str, writable: bool) -> None:
            nonlocal source_changed
            lib_src_root = src_root / subdir if subdir else src_root
            lib_mir_root = mirror_dir(consumer, source_agent) / subdir \
                if subdir else mirror_dir(consumer, source_agent)
            src_map = _walk_rel(lib_src_root, prune_top=not subdir)
            mir_map = _walk_rel(lib_mir_root, prune_top=not subdir)
            rels = sorted(set(src_map) | set(mir_map))

            def _sub(lib_rel: str) -> str:
                return f"{subdir}/{lib_rel}" if subdir else lib_rel

            # Phase 1 — classify deliberate deletes (RW only): source-only
            # files this mirror had converged on, with the source still
            # byte-identical to that base.
            deliberate: set[str] = set()
            if writable:
                for lib_rel in rels:
                    if lib_rel in mir_map or lib_rel not in src_map:
                        continue
                    sub_rel = _sub(lib_rel)
                    if is_excluded_rel(sub_rel):
                        continue
                    base = book.get(consumer, sub_rel)
                    if base is None or src_map[lib_rel].st_size != base["base_size"]:
                        continue
                    try:
                        if _hash_file(lib_src_root / lib_rel) == base["base_hash"]:
                            deliberate.add(lib_rel)
                    except OSError:
                        continue
                if deliberate:
                    tracked = book.tracked(consumer, subdir)
                    limit = max(MASS_DELETE_MIN, int(tracked * MASS_DELETE_FRACTION))
                    if len(deliberate) > limit:
                        logger.warning(
                            "library mirror wipe suspected: %s → %s %r — %d of %d "
                            "tracked files missing from the mirror; refusing to "
                            "propagate the deletes (healing from the source instead)",
                            consumer, source_agent, subdir or "/", len(deliberate), tracked)
                        deliberate = set()

            # Phase 2 — per-pair sync.
            others = [c for c in lib_consumers.get(subdir, []) if c != consumer]
            for lib_rel in rels:
                sub_rel = _sub(lib_rel)
                if is_excluded_rel(sub_rel):
                    continue
                is_del = lib_rel in deliberate
                other_mirrors = [
                    (oc, (mirror_dir(oc, source_agent) / subdir if subdir
                          else mirror_dir(oc, source_agent)) / lib_rel)
                    for oc in others
                ] if is_del else []
                try:
                    jobs, changed = _sync_pair_files(
                        lib_src_root / lib_rel, lib_mir_root / lib_rel,
                        writable=writable, consumer=consumer,
                        source=source_agent, sub_rel=sub_rel,
                        base=book.get(consumer, sub_rel),
                        deliberate_delete=is_del,
                        other_mirrors=other_mirrors, book=book)
                    all_jobs.extend(jobs)
                    source_changed = source_changed or changed
                except OSError as e:
                    if e.errno in (errno.EDQUOT, errno.ENOSPC):
                        logger.warning(
                            "library projection out of space: %s→%s %s",
                            source_agent, consumer, sub_rel)
                        return
                    logger.exception("library projection failed: %s→%s %s",
                                     source_agent, consumer, sub_rel)

        def _sweep_strays(consumer: str, libs: list[tuple[str, bool]]) -> None:
            """Mirror files outside EVERY attached subtree of this source
            are not any library's content — capture + remove (only a
            hook-bypassing direct-bind write can create them)."""
            mir_root = mirror_dir(consumer, source_agent)
            covered = [s for s, _w in libs]
            for sub_rel in _walk_rel(mir_root, prune_top=False):
                if is_excluded_rel(sub_rel):
                    continue  # never projected either way — leave alone
                if any(subtree_covers(s, sub_rel) for s in covered):
                    continue
                mir_file = mir_root / sub_rel
                mirror_rel = f"knowledge/{SHARED_SUBDIR}/{source_agent}/{sub_rel}"
                _capture_and_log(consumer, mirror_rel, mir_file, "conflict")
                mir_file.unlink(missing_ok=True)
                book.drop(consumer, sub_rel)
                all_jobs.append((consumer, mirror_rel, None))

        def _one_pass() -> None:
            for consumer, libs in by_consumer.items():
                book.load(consumer)
                for subdir, writable in libs:
                    _sync_library(consumer, subdir, writable)
                _sweep_strays(consumer, libs)

        await asyncio.to_thread(_one_pass)
        if source_changed:
            # RW adoption/deletion changed the source — re-run consumers so
            # every OTHER mirror picks the change up in the same pass.
            await asyncio.to_thread(_one_pass)
        try:
            await asyncio.to_thread(book.flush)
        except Exception:
            logger.exception("library merge-base flush failed: %s", source_agent)
        for slug, rel, path in all_jobs:
            await _fan_out(slug, rel, path)


async def propagate_source_write(source_agent: str, knowledge_rel: str, *,
                                 deleted: bool = False) -> None:
    """Targeted single-file source→mirrors projection.

    ``knowledge_rel`` is relative to the source's ``knowledge/`` dir.
    Call after any platform-side write/delete of a promoted source's
    knowledge file (dashboard API, satellite applier, Collabora).
    No-op for non-promoted agents, paths outside every library subtree,
    and excluded paths.
    """
    if is_excluded_rel(knowledge_rel):
        return
    lib = await asyncio.to_thread(
        db_knowledge_libraries.library_covering, source_agent, knowledge_rel)
    if lib is None:
        return
    attachments = await asyncio.to_thread(
        db_knowledge_libraries.consumers_of, source_agent, lib["subdir"])
    if not attachments:
        return
    async with _lock(source_agent):
        src_file = source_knowledge_dir(source_agent) / knowledge_rel
        base_rows: list[tuple[str, str, str, str, int, float]] = []
        for att in attachments:
            consumer = att["consumer_agent"]
            mir_file = mirror_dir(consumer, source_agent) / knowledge_rel
            mirror_rel = f"knowledge/{SHARED_SUBDIR}/{source_agent}/{knowledge_rel}"
            try:
                if deleted or not src_file.is_file():
                    if mir_file.is_file():
                        await asyncio.to_thread(mir_file.unlink)
                        await _fan_out(consumer, mirror_rel, None)
                else:
                    h = await asyncio.to_thread(_copy_file, src_file, mir_file)
                    st = mir_file.stat()
                    base_rows.append((consumer, source_agent, knowledge_rel,
                                      h, st.st_size, st.st_mtime))
                    await _fan_out(consumer, mirror_rel, mir_file)
            except OSError as e:
                if e.errno in (errno.EDQUOT, errno.ENOSPC):
                    logger.warning("library projection out of space: %s→%s %s",
                                   source_agent, consumer, knowledge_rel)
                    continue
                logger.exception("library projection failed: %s→%s %s",
                                 source_agent, consumer, knowledge_rel)
        try:
            if deleted or not src_file.is_file():
                await asyncio.to_thread(
                    db_library_mirror_state.delete_rel_all_consumers,
                    source_agent, knowledge_rel)
            elif base_rows:
                await asyncio.to_thread(db_library_mirror_state.upsert_many, base_rows)
        except Exception:
            logger.exception("library merge-base update failed: %s %s",
                             source_agent, knowledge_rel)


async def propagate_mirror_write(consumer_agent: str, source_agent: str,
                                 sub_rel: str) -> bool:
    """Targeted RW mirror→source propagation of one created/modified file.

    ``sub_rel`` is SOURCE-ROOT-relative (the mirror path below the slug).
    Returns False (and does nothing) when no attached library of this
    consumer covers the path, or the covering attachment is read-only —
    callers surface that as their own permission error.
    Raises OSError(EDQUOT/ENOSPC) for the caller to translate to 507.
    """
    if is_excluded_rel(sub_rel):
        return False
    att = await asyncio.to_thread(
        db_knowledge_libraries.attachment_covering,
        source_agent, consumer_agent, sub_rel)
    if att is None or not att["writable"]:
        return False
    async with _lock(source_agent):
        mir_file = mirror_dir(consumer_agent, source_agent) / sub_rel
        if not mir_file.is_file():
            return False
        src_file = source_knowledge_dir(source_agent) / sub_rel
        h = await asyncio.to_thread(_copy_file, mir_file, src_file)
        try:
            st = src_file.stat()
            await asyncio.to_thread(
                db_library_mirror_state.upsert, consumer_agent, source_agent,
                sub_rel, h, st.st_size, st.st_mtime)
        except Exception:
            logger.exception("library merge-base update failed: %s %s",
                             consumer_agent, sub_rel)
        await _fan_out(source_agent, f"knowledge/{sub_rel}", src_file)
    # Other mirrors pick the change up outside the source lock.
    await propagate_source_write(source_agent, sub_rel)
    return True


async def propagate_mirror_delete(consumer_agent: str, source_agent: str,
                                  sub_rel: str) -> bool:
    """EXPLICIT delete from an RW mirror (dashboard file API, satellite
    live delete): capture the source bytes ("deleted"), delete at the
    source, propagate to every mirror. Returns False for read-only /
    uncovered paths (the caller keeps its own behaviour)."""
    if is_excluded_rel(sub_rel):
        return False
    att = await asyncio.to_thread(
        db_knowledge_libraries.attachment_covering,
        source_agent, consumer_agent, sub_rel)
    if att is None or not att["writable"]:
        return False
    async with _lock(source_agent):
        src_file = source_knowledge_dir(source_agent) / sub_rel
        if src_file.is_file():
            await asyncio.to_thread(
                _capture_and_log, source_agent, f"knowledge/{sub_rel}",
                src_file, "deleted")
            await asyncio.to_thread(src_file.unlink)
            await _fan_out(source_agent, f"knowledge/{sub_rel}", None)
    await propagate_source_write(source_agent, sub_rel, deleted=True)
    return True


async def attach_setup(source_agent: str, consumer_agent: str) -> None:
    """Initial projection after an attach (mirror materializes now, mounts
    at the consumer's next session spawn)."""
    await reconcile_source(source_agent)


def _rm_empty_parents(leaf: Path, stop: Path) -> None:
    """Best-effort: rmdir now-empty dirs from ``leaf`` up to (and incl.)
    ``stop`` — a torn-down subdir library shouldn't leave a husk of empty
    parents in the consumer's mirror namespace."""
    cur = leaf
    while True:
        try:
            cur.rmdir()
        except OSError:
            return
        if cur == stop:
            return
        cur = cur.parent


async def detach_teardown(source_agent: str, consumer_agent: str,
                          subdir: str = "") -> None:
    """Remove ONE library's mirror subtree after detach (files are
    projector-owned). A sibling library of the same source keeps its
    subtree — only the detached ``subdir`` goes. The consumer's merge-base
    rows for the subtree go FIRST, so nothing can ever read the removal
    as a set of deliberate deletes."""
    try:
        await asyncio.to_thread(
            db_library_mirror_state.delete_consumer_subtree,
            consumer_agent, source_agent, subdir)
    except Exception:
        logger.exception("library merge-base teardown failed: %s/%s",
                         consumer_agent, source_agent)
    mir_root = mirror_dir(consumer_agent, source_agent)
    lib_root = mir_root / subdir if subdir else mir_root
    rels = await asyncio.to_thread(
        lambda: sorted(_walk_rel(lib_root, prune_top=not subdir)))
    await asyncio.to_thread(
        lambda: shutil.rmtree(lib_root, ignore_errors=True))
    if subdir:
        await asyncio.to_thread(_rm_empty_parents, lib_root.parent, mir_root)
    for lib_rel in rels:
        sub_rel = f"{subdir}/{lib_rel}" if subdir else lib_rel
        await _fan_out(
            consumer_agent,
            f"knowledge/{SHARED_SUBDIR}/{source_agent}/{sub_rel}", None)


async def teardown_library(source_agent: str, subdir: str,
                           consumers: list[str]) -> None:
    """Un-promote of ONE library: remove its subtree from every consumer
    (the rows are already gone — un-promote returns the consumer list)."""
    for consumer in consumers:
        await detach_teardown(source_agent, consumer, subdir)


async def teardown_source(source_agent: str,
                          consumers: list[str] | None = None) -> None:
    """Source-agent deletion: remove the WHOLE slug mirror from every
    consumer, across all of the source's libraries.

    ``consumers`` is passed when the rows are already gone (agent deletion
    cascades them)."""
    if consumers is None:
        consumers = sorted({
            a["consumer_agent"] for a in await asyncio.to_thread(
                db_knowledge_libraries.consumers_of, source_agent)})
    for consumer in dict.fromkeys(consumers):
        await detach_teardown(source_agent, consumer)
    try:
        await asyncio.to_thread(db_library_mirror_state.delete_source, source_agent)
    except Exception:
        logger.exception("library merge-base teardown failed: %s", source_agent)


# ── Turn-end kick ───────────────────────────────────────────────────────────

def _sources_touched_by(agent_slug: str) -> list[str]:
    """Sources whose libraries this agent can change from its sandbox: the
    sources of its WRITABLE attachments, plus itself when promoted."""
    out = {
        a["source_agent"]
        for a in db_knowledge_libraries.attachments_for_consumer(agent_slug)
        if a.get("writable")
    }
    if db_knowledge_libraries.is_promoted(agent_slug):
        out.add(agent_slug)
    return sorted(out)


async def _kick_source(source_agent: str) -> None:
    if source_agent in _kick_pending:
        return  # a queued run will cover this change too
    _kick_pending.add(source_agent)
    try:
        wait = RECONCILE_KICK_DEBOUNCE_S - (
            time.monotonic() - _kick_last.get(source_agent, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _kick_last[source_agent] = time.monotonic()
    finally:
        _kick_pending.discard(source_agent)
    await reconcile_source(source_agent)


async def _reconcile_for_agent(agent_slug: str) -> None:
    try:
        sources = await asyncio.to_thread(_sources_touched_by, agent_slug)
        for source in sources:
            await _kick_source(source)
    except Exception:
        logger.exception("library turn-end reconcile failed: %s", agent_slug)


def schedule_reconcile_for_agent(agent_slug: str) -> None:
    """Fire-and-forget turn-end kick: reconcile every library ``agent_slug``
    can change from its sandbox (RW attachments as a consumer, its own
    libraries as a source), debounced per source. Sync-callable from the
    event-loop thread; a no-op without a running loop or library rows."""
    if not agent_slug:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_reconcile_for_agent(agent_slug))


async def reconcile_loop() -> None:
    """Periodic sweep — catches direct-bind sandbox writes (which pass no
    proxy chokepoint) and heals any divergence. Cheap when no libraries
    exist (one SELECT)."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_S)
        try:
            libraries = await asyncio.to_thread(
                db_knowledge_libraries.list_libraries)
            for source in sorted({l["source_agent"] for l in libraries}):
                await reconcile_source(source)
        except Exception:
            logger.exception("library reconcile sweep failed")
