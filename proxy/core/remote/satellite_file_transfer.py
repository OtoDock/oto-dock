"""Satellite file push / pull / shared-workspace conflict handling (mixin).

The proxy side of the satellite file protocol: streaming push (windowed
file_content chunks) and pull (sha256-verified atomic rename), plus multi-user
shared-workspace conflict detection + recoverable backups on the live
write-back path. Mixed into SatelliteConnectionManager; split out of
satellite_connection.py. `PUSH_WINDOW_CHUNKS` stays in satellite_connection
(monkeypatched by tests) and is imported lazily in push_file.
"""

import asyncio
import base64
from collections import OrderedDict
import hashlib
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import contextlib

from services.infra import safe_fs

if TYPE_CHECKING:
    from services.path_policy_v2 import PathRef

logger = logging.getLogger("claude-proxy.satellite")


# --- Multi-user shared-workspace conflict detection (versioned-sync) ---

# Only files at or under this size are conflict-tracked AND get a recoverable
# byte backup on the live write-back path — we never read/hash a large file on
# the per-turn apply path. Larger files still fan out + propagate; they just
# aren't captured live (an idle satellite still captures them up to the
# recover-bin cap at session-start).
CONFLICT_BACKUP_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class _PullStream:
    """In-flight streaming file pull (proxy side).

    The satellite sends the file as a sequence of ``file_content`` chunks;
    each decoded chunk is written straight to ``partial_path`` on disk and,
    after the final chunk, the commit (flush, fsync, the atomic rename to
    ``dest_path``, sha256-verified) runs on the file-commit executor, off
    the receive loop. Bounded memory: only one chunk is held at a time.

    The partial and the destination are opened beneath ``rootfd`` by their
    names under it (``partial_rel``, ``dest_rel``), never by path: no
    component is followed. The handle is closed with the stream.
    """
    machine_id: str
    dest_path: Path
    partial_path: Path
    future: asyncio.Future
    hasher: "hashlib._Hash"
    rootfd: int = -1
    dest_rel: str = ""
    partial_rel: str = ""
    handle: object = None  # opened lazily on the first chunk
    received_bytes: int = 0
    # The final chunk's sha256; the commit (off the loop) compares against it.
    expected_hash: str = ""


@dataclass
class _AdmittedFrame:
    """A ``file_changed`` frame past admission: the authenticated context and
    the fields the applier keys on, all from that context or the frame."""
    sec: object
    agent_slug: str
    rel_path: str
    action: str
    session_id: str
    msg: dict


@dataclass
class _FileChangedLane:
    """One machine's applier lane: the frames in flight (queued or running)
    and the semaphore that bounds how many run at once. A task keeps the
    record it started on, so a replaced connection's appliers finish on
    their own lane and never touch a fresh one's count."""
    sem: asyncio.Semaphore
    inflight: int = 0
    last_shed_warning: float | None = None
    shed_since_warning: int = 0


# When a machine last pushed a change of an agent's tree (monotonic), by
# (machine_id, agent_slug): a platform-side judge (CHECKS.md ``judge_on:
# platform``) waits until the post-turn frames go quiet. Stamped only for
# admitted frames (the agent is the session's authenticated one), bounded at
# ``SAT_FILE_CHANGED_STAMP_MAX`` entries, and evicted with a machine's
# connection unless the machine sits in the reconnect grace (the satellite
# replays its frames on reconnect and the judge must keep waiting).
LAST_FILE_CHANGED: OrderedDict[tuple[str, str], float] = OrderedDict()


def last_file_changed_at(machine_id: str, agent_slug: str) -> float:
    return LAST_FILE_CHANGED.get((machine_id, agent_slug), 0.0)


def _stamp_file_changed(machine_id: str, agent_slug: str) -> None:
    import config
    key = (machine_id, agent_slug)
    if key in LAST_FILE_CHANGED:
        del LAST_FILE_CHANGED[key]
    LAST_FILE_CHANGED[key] = time.monotonic()
    while len(LAST_FILE_CHANGED) > config.SAT_FILE_CHANGED_STAMP_MAX:
        LAST_FILE_CHANGED.popitem(last=False)


def evict_file_changed_stamps(machine_id: str) -> None:
    for key in [k for k in LAST_FILE_CHANGED if k[0] == machine_id]:
        del LAST_FILE_CHANGED[key]


def _admit_file_changed(machine_id: str, msg: dict) -> "_AdmittedFrame | None":
    """The gate every ``file_changed`` frame passes before it touches any
    state: the frame names an agent, a path and an action; the claimed
    session has a registered SecurityContext; a context bound to a machine
    is bound to THIS machine; and the context's agent is the frame's. A
    frame that fails costs a dictionary lookup and a log line: no stamp, no
    query, no task. Identity is never read off the payload.
    """
    agent_slug = msg.get("agent_slug", "")
    rel_path = msg.get("path", "")
    action = msg.get("action", "")
    if not agent_slug or not rel_path or not action:
        return None
    session_id = msg.get("session_id", "")

    from core.remote import file_sync as core_file_sync
    from core.session.session_state import get_session_security
    sec = get_session_security(session_id) if session_id else None
    # Bind the claimed session to the SENDING machine (same rule as
    # transcript_lines / pty_inject_result): a compromised satellite must
    # not inherit another machine's session role by quoting its session id.
    # Positive mismatch only: task/meeting/phone contexts carry no machine
    # id on their placement and keep their normal path.
    _sess_target = sec.placement.machine_id if sec else ""
    if sec is not None and _sess_target and _sess_target != machine_id:
        logger.warning(
            "file_changed: session %s is bound to machine %s but the "
            "frame came from %s; dropping the claimed role",
            str(session_id)[:8], str(_sess_target)[:8], str(machine_id)[:8],
        )
        sec = None
    if sec is None:
        # Engine-internal machinery paths (.claude/.codex/.credentials) with
        # NO registered context are the engine's last write landing after
        # the session's close (Claude stamps `.claude/.last-cleanup` on exit,
        # and a mode-switch close unregisters the context a few ms before):
        # said at info so the close race stays visible
        # without a WARNING. A missing context on a NORMAL path is exactly
        # the anomaly the WARNING exists to surface.
        if core_file_sync.is_engine_machinery_path(rel_path):
            logger.info(
                "write-back skipped (engine machinery, no session context): "
                "session=%s path=%s action=%s",
                (session_id[:8] if session_id else "?"), rel_path, action,
            )
        else:
            logger.warning(
                "write-back denied: session=%s role=no-ctx path=%s action=%s",
                (session_id[:8] if session_id else "?"), rel_path, action,
            )
        return None
    # Agent identity is part of "never trust the satellite payload": the
    # session's AUTHENTICATED agent must be the frame's. A mismatch means a
    # buggy/compromised satellite tried to write into a DIFFERENT agent's
    # tree: reject. This also keeps the lock / fan-out / conflict / stamp
    # keys honest, since fan-out targets are selected by agent_slug.
    sec_agent = getattr(sec, "agent", "") or ""
    if sec_agent != agent_slug:
        logger.warning(
            "write-back agent mismatch: session=%s sec_agent=%s payload=%s path=%s",
            (session_id[:8] if session_id else "?"), sec_agent, agent_slug, rel_path,
        )
        return None
    return _AdmittedFrame(sec=sec, agent_slug=agent_slug, rel_path=rel_path,
                          action=action, session_id=session_id, msg=msg)


class SatelliteFileTransferMixin:
    async def push_file(
        self,
        machine_id: str,
        ref: "PathRef",
        source: "bytes | Path",
        *,
        agent_slug: str = "",
        timeout: float = 30.0,
        progress_cb=None,
    ) -> bool:
        """Push a file to the satellite and wait for its ack.

        ``source`` is either the file's bytes (small payloads already in
        memory) or a filesystem ``Path`` — the streaming mode: chunks are
        read from disk per window, so memory stays O(window) regardless of
        file size. Callers should pass a Path whenever the bytes live on
        disk; ``push_file`` picks the inline fast path internally for small
        files either way.

        ``ref.kind == "agent_tree"`` — writes under the agent's tree at
        ``{satellite_agents_dir}/{agent_slug}/{ref.value}``. The
        ``agent_slug`` kwarg is REQUIRED in this mode. Used by all
        existing callers (push_back, mcp_output_relocation, uploads, etc.).

        ``ref.kind == "satellite_host"`` — writes to ``ref.value`` (an
        absolute path on the satellite's filesystem). Used by
        Docker MCP push-back for satellite-host paths (e.g.
        ``/home/alice/Desktop/foo.png``). ``agent_slug`` is ignored;
        the satellite re-validates ``..`` / NUL defensively before
        writing.

        ``progress_cb(bytes_sent, bytes_total)`` — optional, sync or async;
        invoked after each acked window boundary and once terminally with
        ``(size, size)``. Fully fenced: a raising/broken callback never
        aborts the transfer.

        Handles ≤ 512KB payloads in a single message; larger files are
        chunked. Returns True on success, False on timeout / error /
        disconnect / cap-exceeded / source-vanished.
        """
        # PUSH_WINDOW_CHUNKS stays in satellite_connection (monkeypatched by
        # tests) — read it live each call.
        from core.remote.satellite_connection import PUSH_WINDOW_CHUNKS
        import base64 as _b64
        import hashlib as _hashlib
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        if ref.kind == "agent_tree" and not agent_slug:
            raise ValueError("push_file(agent_tree) requires agent_slug")

        from core.remote import file_sync
        from core.remote.file_sync import MAX_CHUNK_SIZE

        async def _notify(sent: int, total: int) -> None:
            if progress_cb is None:
                return
            try:
                res = progress_cb(sent, total)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.debug("push_file progress_cb failed", exc_info=True)

        # Resolve (size, content_hash) without ever holding a big file in
        # memory: Path sources are stat'd + stream-hashed off the event loop.
        from_path = not isinstance(source, (bytes, bytearray))
        if from_path:
            path = Path(source)
            try:
                st = await asyncio.to_thread(os.stat, path)
                size = st.st_size
                content_hash = await asyncio.to_thread(file_sync._hash_file, path)
            except OSError as e:
                logger.warning("push_file: cannot read %s: %s", path, e)
                return False
        else:
            size = len(source)
            content_hash = f"sha256:{_hashlib.sha256(source).hexdigest()}"

        # Never send a satellite a file above what it accepts: the config cap
        # for 0.5.103+, the legacy 100MB for older machines (their
        # apply_file_push hard-rejects above it — the transfer would only
        # burn bandwidth and fail at commit).
        cap = self.effective_sync_cap(machine_id)
        if size > cap:
            logger.warning(
                "push_file: %s is %.1f MB > %d MB cap for machine %s — skipped",
                ref.value, size / 1024 / 1024, cap // 1024 // 1024, machine_id[:8],
            )
            return False

        def _base_msg(action: str) -> dict:
            return {
                "type": "file_push",
                "path_kind": ref.kind,
                "agent_slug": agent_slug,
                "action": action,
                "path": ref.value,
            }

        if size <= MAX_CHUNK_SIZE:
            if from_path:
                try:
                    content = await asyncio.to_thread(path.read_bytes)
                except OSError as e:
                    logger.warning("push_file: cannot read %s: %s", path, e)
                    return False
            else:
                content = bytes(source)
            command_id = str(uuid.uuid4())
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending_acks[command_id] = (machine_id, future)
            try:
                msg = _base_msg("write")
                msg["command_id"] = command_id
                msg["content_b64"] = _b64.b64encode(content).decode()
                msg["hash"] = content_hash
                await conn.enqueue_send(msg, bulk=True)
                try:
                    ack = await asyncio.wait_for(future, timeout=timeout)
                    ok = ack.get("status") == "ok"
                except asyncio.TimeoutError:
                    return False
                except RuntimeError:
                    # Future rejected by deregister (WS dead).
                    return False
                if ok:
                    await _notify(size, size)
                return ok
            finally:
                self._pending_acks.pop(command_id, None)

        # Chunked path — send write_chunk frames on the BULK lane in bounded
        # windows of PUSH_WINDOW_CHUNKS. A command_id is attached to the last
        # chunk of each window (and to the final chunk); we await that ack
        # before sending the next window, so at most one window is in flight.
        # The satellite commits + sha256-verifies only on the final chunk
        # (non-empty hash); intermediate window-boundary chunks just append and
        # ack "ok". A non-ok / timed-out / WS-dropped window aborts the whole
        # transfer (returns False) instead of blasting the remaining chunks.
        #
        # total_chunks and the hash are captured at start: a Path source that
        # SHRINKS mid-push (short read) aborts immediately — never send a
        # truncated stream under a stale total_chunks. A same-size content
        # mutation is caught by the satellite's final-chunk sha256 verify
        # against the pre-computed hash (error ack → False → retried by the
        # next sync cycle).
        total_chunks = (size + MAX_CHUNK_SIZE - 1) // MAX_CHUNK_SIZE
        fh = None
        if from_path:
            try:
                # Unbuffered: sequential 512KB reads need no readahead layer,
                # and a buffered reader could mask a mid-push truncation by
                # serving pre-buffered bytes.
                fh = await asyncio.to_thread(open, path, "rb", 0)
            except OSError as e:
                logger.warning("push_file: cannot open %s: %s", path, e)
                return False

        def _read_exact(n: int) -> bytes:
            # Retry mid-file partial reads (raw IO may return short); only a
            # true EOF — the file shrank — yields fewer than n bytes.
            buf = b""
            while len(buf) < n:
                block = fh.read(n - len(buf))
                if not block:
                    break
                buf += block
            return buf

        try:
            offset = 0
            chunk_idx = 0
            while offset < size:
                expected = min(MAX_CHUNK_SIZE, size - offset)
                if from_path:
                    chunk = await asyncio.to_thread(_read_exact, expected)
                else:
                    chunk = source[offset:offset + MAX_CHUNK_SIZE]
                if len(chunk) != expected:
                    logger.warning(
                        "push_file: %s changed size mid-push (expected %d-byte "
                        "chunk, got %d) — aborted", ref.value, expected, len(chunk),
                    )
                    return False
                is_last = offset + MAX_CHUNK_SIZE >= size
                # Flush (await an ack) at every window boundary and at the final chunk.
                is_flush = is_last or ((chunk_idx + 1) % PUSH_WINDOW_CHUNKS == 0)
                command_id = str(uuid.uuid4()) if is_flush else ""
                future: asyncio.Future | None = None
                if command_id:
                    future = asyncio.get_event_loop().create_future()
                    self._pending_acks[command_id] = (machine_id, future)
                try:
                    msg = _base_msg("write_chunk")
                    msg["chunk_index"] = chunk_idx
                    msg["total_chunks"] = total_chunks
                    msg["content_b64"] = _b64.b64encode(chunk).decode()
                    msg["hash"] = content_hash if is_last else ""
                    if command_id:
                        msg["command_id"] = command_id
                    await conn.enqueue_send(msg, bulk=True)
                    if command_id:
                        try:
                            ack = await asyncio.wait_for(future, timeout=timeout)
                        except asyncio.TimeoutError:
                            return False
                        except RuntimeError:
                            # Future rejected by deregister (WS dead).
                            return False
                        if ack.get("status") != "ok":
                            return False  # early abort — stop sending the rest
                        await _notify(min(offset + len(chunk), size), size)
                finally:
                    if command_id:
                        self._pending_acks.pop(command_id, None)
                offset += MAX_CHUNK_SIZE
                chunk_idx += 1
            return True
        finally:
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()

    async def stat_file(
        self,
        machine_id: str,
        ref: "PathRef",
        *,
        agent_slug: str = "",
        timeout: float = 10.0,
    ) -> dict | None:
        """Cheap file metadata probe on the satellite (0.5.95+).

        Returns ``{"exists": bool, "size": int, "mtime_ns": int}`` — both
        stat values are SATELLITE-clock/filesystem facts, so comparing two
        probes (or a probe against a value recorded at pull time) never
        involves cross-host clock skew. Returns ``None`` on timeout, policy
        reject, disconnect, or an old satellite (callers must gate on
        ``satellite_supports_file_stat`` — an ungated send to an old
        satellite is silently dropped and burns the whole timeout).
        ``None`` means "don't trust the cache": callers fall back to a full
        pull, never to serving stale bytes.
        """
        try:
            ack = await self.send_command(
                machine_id,
                {
                    "type": "file_stat",
                    "path_kind": ref.kind,
                    "agent_slug": agent_slug,
                    "path": ref.value,
                },
                timeout=timeout,
            )
        except RuntimeError:
            return None
        if not isinstance(ack, dict) or ack.get("status") != "ok":
            return None
        return {
            "exists": bool(ack.get("exists")),
            "size": int(ack.get("size", 0) or 0),
            "mtime_ns": int(ack.get("mtime_ns", 0) or 0),
        }

    async def pull_file_to_path(
        self,
        machine_id: str,
        ref: "PathRef",
        dest_path,
        *,
        agent_slug: str = "",
        timeout: float = 180.0,
    ) -> bool:
        """Stream a file from the satellite to ``dest_path`` (bounded memory).

        The satellite chunks the file into ``file_content`` messages; each
        decoded chunk is written straight to ``dest_path + '.partial'`` and
        the file is atomically renamed into place on the final chunk
        (sha256-verified). Returns True on success; False on timeout /
        read-denied / not-found / hash-mismatch / size-cap / disconnect.

        Same ``ref.kind`` semantics as ``push_file``. A destination in the
        agents tree is reached beneath the agents root with no component
        followed (its missing parents created there), so a link planted at
        any parent refuses the pull; a destination elsewhere (a
        platform-owned cache) opens beneath its own parent. The caller is
        still responsible for authorizing ``dest_path``.
        """
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        if ref.kind == "agent_tree" and not agent_slug:
            raise ValueError("pull_file_to_path(agent_tree) requires agent_slug")

        dest = Path(dest_path)
        partial = Path(str(dest) + ".partial")
        try:
            rootfd, dest_rel = await asyncio.to_thread(self._open_pull_root, dest)
        except OSError as e:
            logger.warning(
                "Satellite %s file pull refused (%s): %s", machine_id[:8], dest, e,
            )
            return False
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_pulls[request_id] = _PullStream(
            machine_id=machine_id,
            dest_path=dest,
            partial_path=partial,
            future=future,
            hasher=hashlib.sha256(),
            rootfd=rootfd,
            dest_rel=dest_rel,
            partial_rel=dest_rel + ".partial",
        )
        try:
            await conn.enqueue_send({
                "type": "file_pull",
                "request_id": request_id,
                "path_kind": ref.kind,
                "agent_slug": agent_slug,
                "path": ref.value,
            })
            # True means every byte is on disk (the final chunk arrived);
            # False is a failed or refused transfer.
            received = await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, RuntimeError) as e:
            logger.warning(
                "Satellite %s file pull timeout/error: %s", machine_id[:8], e,
            )
            st = self._pending_pulls.pop(request_id, None)
            if st is not None:
                self._cleanup_pull_stream(st)
            return False
        except BaseException:
            st = self._pending_pulls.pop(request_id, None)
            if st is not None:
                self._cleanup_pull_stream(st)
            raise
        # The stream leaves the pending map BEFORE the commit, so a deregister
        # or a late chunk can no longer touch it; the commit (flush, fsync,
        # verify, rename) then runs on the file-commit executor, never on the
        # receive loop. Callers still return only after the rename.
        st = self._pending_pulls.pop(request_id, None)
        if st is None:
            return False
        if not received:
            self._cleanup_pull_stream(st)
            return False
        from core import file_commit
        return await file_commit.run(self._commit_pull_sync, st)

    def _on_pull_chunk(self, st: "_PullStream", msg: dict) -> None:
        """Apply one ``file_content`` chunk to an in-flight pull. Runs in the
        WS receive loop: a 512KB write is sub-ms; the commit is not and runs
        elsewhere (``pull_file_to_path``)."""
        if st.future.done():
            return
        err = msg.get("error")
        if err:
            self._fail_pull(st, str(err))
            return
        b64 = msg.get("content_b64", "")
        try:
            block = base64.b64decode(b64) if b64 else b""
        except Exception as e:
            self._fail_pull(st, f"bad base64: {e}")
            return
        from core.remote.file_sync import MAX_FILE_SIZE
        if st.received_bytes + len(block) > MAX_FILE_SIZE:
            self._fail_pull(st, "pulled file exceeds MAX_FILE_SIZE")
            return
        if st.handle is None:
            try:
                st.handle = os.fdopen(self._open_partial(st), "wb")
            except OSError as e:
                self._fail_pull(st, f"cannot open partial: {e}")
                return
        try:
            st.handle.write(block)
        except OSError as e:
            self._fail_pull(st, f"write failed: {e}")
            return
        st.hasher.update(block)
        st.received_bytes += len(block)
        total = int(msg.get("total_chunks", 0) or 0)
        expected_hash = msg.get("hash") or ""
        chunk_index = int(msg.get("chunk_index", 0) or 0)
        is_last = bool(expected_hash) or (total and chunk_index >= total - 1)
        if is_last:
            st.expected_hash = expected_hash
            if not st.future.done():
                st.future.set_result(True)

    def _commit_pull_sync(self, st: "_PullStream") -> bool:
        """Finalize a received pull: flush, fsync, verify sha256, atomic
        rename, prime the hash cache. Runs on the file-commit executor; it
        never touches the future or the pending map. On any failure the
        partial is removed and the answer is False."""
        try:
            if st.handle is not None:
                st.handle.flush()
                os.fsync(st.handle.fileno())
                st.handle.close()
                st.handle = None
            actual = f"sha256:{st.hasher.hexdigest()}"
            if st.expected_hash and actual != st.expected_hash:
                logger.warning("pull hash mismatch for %s", st.dest_path)
                self._cleanup_pull_stream(st)
                return False
            # A rename replaces a link planted at the destination as a name,
            # never through it.
            safe_fs.rename_beneath(st.rootfd, st.partial_rel, st.dest_rel, replace=True)
        except OSError as e:
            logger.warning("pull commit failed for %s: %s", st.dest_path, e)
            self._cleanup_pull_stream(st)
            return False
        self._close_pull_root(st)
        # The sha256 of these bytes was just verified — prime the manifest
        # hash cache so the next sync cycle never re-hashes a big pull.
        from core.remote.file_sync import prime_hash_cache
        prime_hash_cache(st.dest_path, actual)
        return True

    def _fail_pull(self, st: "_PullStream", reason: str) -> None:
        logger.warning("file pull failed (%s): %s", st.dest_path, reason)
        self._cleanup_pull_stream(st)
        if not st.future.done():
            st.future.set_result(False)

    def _cleanup_pull_stream(self, st: "_PullStream") -> None:
        """Close the handle (if open), remove a leftover ``.partial`` and
        close the root. Idempotent: a committed stream already closed,
        renamed and released its root."""
        h = st.handle
        if h is not None:
            with contextlib.suppress(OSError):
                h.close()
            st.handle = None
        if st.rootfd >= 0:
            with contextlib.suppress(OSError):
                safe_fs.unlink_beneath(st.rootfd, st.partial_rel, missing_ok=True)
            self._close_pull_root(st)

    @staticmethod
    def _open_pull_root(dest: Path) -> tuple[int, str]:
        """The directory handle a pull's partial and destination open
        beneath, and the destination's name under it: the agents root for a
        destination in the agents tree (every missing parent created there,
        no component followed), else the destination's own parent, a
        platform-owned cache. The caller closes the handle."""
        import config
        agents_root = Path(config.AGENTS_DIR)
        try:
            rel = safe_fs.rel_under(dest, agents_root)
        except safe_fs.EscapeRefused:
            rel = ""
        if not rel:
            dest.parent.mkdir(parents=True, exist_ok=True)
            return safe_fs.open_dir_beneath(dest.parent), dest.name
        rootfd = safe_fs.open_dir_beneath(agents_root)
        try:
            parent = rel.rpartition("/")[0]
            if parent:
                safe_fs.mkdirs_beneath(rootfd, parent)
        except BaseException:
            os.close(rootfd)
            raise
        return rootfd, rel

    @staticmethod
    def _open_partial(st: "_PullStream") -> int:
        """The partial's write descriptor beneath the root. A link planted
        at the staging name goes (its target stays) and the bytes land in a
        fresh file."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            return safe_fs.open_beneath(st.rootfd, st.partial_rel, flags, 0o644)
        except safe_fs.SymlinkRefused:
            safe_fs.unlink_beneath(st.rootfd, st.partial_rel, missing_ok=True)
            return safe_fs.open_beneath(st.rootfd, st.partial_rel, flags | os.O_EXCL, 0o644)

    @staticmethod
    def _close_pull_root(st: "_PullStream") -> None:
        if st.rootfd >= 0:
            with contextlib.suppress(OSError):
                os.close(st.rootfd)
            st.rootfd = -1

    def _fc_lane(self, machine_id: str) -> _FileChangedLane:
        import config
        lane = self._fc_lanes.get(machine_id)
        if lane is None:
            lane = self._fc_lanes[machine_id] = _FileChangedLane(
                sem=asyncio.Semaphore(config.SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE))
        return lane

    def _reset_file_changed_lane(self, machine_id: str) -> None:
        """A fresh lane for a (re)connecting or leaving machine: appliers still
        running hold the old record and finish on it."""
        self._fc_lanes.pop(machine_id, None)

    def _dispatch_file_changed(self, machine_id: str, msg: dict) -> bool:
        """Admit, bound and schedule one ``file_changed`` frame from the WS
        receive loop. False when the frame was dropped or shed.

        Bounds: at most ``SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE`` frames
        of one machine queued or running (0 = no shedding; past it the frame
        is shed with a rate-limited WARNING: file sync is best-effort and the
        next merge reconciles), ``SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE``
        running per machine inside ``SAT_FILE_CHANGED_CONCURRENCY_GLOBAL``
        running across machines (the per-machine semaphore is taken first,
        so one machine holds at most its own share of the global slots).
        """
        import config
        frame = _admit_file_changed(machine_id, msg)
        if frame is None:
            return False
        lane = self._fc_lane(machine_id)
        # Every admitted frame stamps the quiet window, shed ones included: a
        # platform-side judge must not read a turn as quiet while its mirror
        # is incomplete.
        _stamp_file_changed(machine_id, frame.agent_slug)
        ceiling = config.SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE
        if ceiling > 0 and lane.inflight >= ceiling:
            lane.shed_since_warning += 1
            now = time.monotonic()
            if lane.last_shed_warning is None or now - lane.last_shed_warning >= 10.0:
                logger.warning(
                    "file_changed: machine %s has %d frame(s) in flight; %d frame(s) "
                    "shed (the next merge reconciles them)",
                    machine_id[:8], lane.inflight, lane.shed_since_warning,
                )
                lane.last_shed_warning = now
                lane.shed_since_warning = 0
            return False
        lane.inflight += 1
        asyncio.create_task(self._run_applier(machine_id, lane, frame))
        return True

    async def _run_applier(self, machine_id: str, lane: _FileChangedLane,
                           frame: _AdmittedFrame) -> None:
        try:
            async with lane.sem:
                async with self._fc_global_sem:
                    await self._apply_admitted(machine_id, frame)
        except Exception:
            logger.exception(
                "_apply_file_changed: failed for %s/%s", frame.agent_slug, frame.rel_path,
            )
        finally:
            lane.inflight -= 1
            # A long-queued frame keeps the judge's quiet window open until
            # it is applied.
            _stamp_file_changed(machine_id, frame.agent_slug)

    async def _apply_file_changed(self, machine_id: str, msg: dict) -> None:
        """Admit and apply one ``file_changed`` frame: the direct entry (the
        receive loop goes through ``_dispatch_file_changed``, which admits
        once and runs the body under the lanes)."""
        frame = _admit_file_changed(machine_id, msg)
        if frame is None:
            return
        _stamp_file_changed(machine_id, frame.agent_slug)
        await self._apply_admitted(machine_id, frame)

    async def _apply_admitted(self, machine_id: str, frame: _AdmittedFrame) -> None:
        """Apply an admitted satellite-side file change to the platform's
        agent_dir, then fan it out to every OTHER satellite running the same
        agent.

        - Small files (≤ 1 MB) carry ``content_b64`` inline → write directly.
        - Large files (> 1 MB) carry only ``size`` + ``hash`` → pull explicitly,
          then write.
        - Deletes have no body — apply atomically.

        Idempotent if the platform already has the same hash. Errors are
        logged and swallowed — file sync is best-effort during a turn; if
        it fails the user will see stale state until the next session start.

        The role gate (``can_write_back``) runs here, after admission; a
        knowledge-mirror path costs one query for the consumer's RW pairs.
        Apply + conflict-detect run UNDER the global per-(agent, rel_path)
        lock so concurrent same-file writers converge on a consistent byte
        sequence (last writer wins, never a torn interleave). The fan-out
        runs AFTER the lock is released, under the per-path fan-out lock
        taken inside it, with the applied bytes (an inline write) or the
        file's path (a pulled one) as its source: same-path fan-outs stay in
        apply order, a slow target no longer holds the next writer of the
        path back, and ``fan_out_write`` hashes the very descriptor it
        pushes, so a later same-path write cannot change what the recorded
        merge base names. Every store call runs on the DB lane (``run_db``).
        """
        from core.remote import file_sync as core_file_sync
        import config as _cfg
        from storage.pg import run_db

        sec = frame.sec
        agent_slug, rel_path, action = frame.agent_slug, frame.rel_path, frame.action
        session_id, msg = frame.session_id, frame.msg

        # Role-aware write-back guard (SECURITY). The satellite→platform write
        # direction must obey the same per-role write matrix as native tools.
        # The role is the AUTHENTICATED session's, NEVER the payload's.
        # Fail-closed. This is the ONLY filesystem-write gate for Codex remote
        # sessions (Codex has no per-tool permission hooks).
        _role = getattr(sec, "role", "") or ""
        _uname = getattr(sec, "username", "") or ""
        # MOUNT identity for the per-user-dir rule (Shared-only human chats
        # blank it); the REAL username above keeps owner config/knowledge
        # curation working. None (a ctx without the property) falls back to
        # username inside can_write_back.
        _mount = getattr(sec, "mount_username", None)
        # Knowledge-library mirror paths need the consumer's RW-attachment
        # (source, subdir) pairs for can_write_back's segment-wise subtree
        # rule (one indexed SELECT, only for mirror paths — plain writes
        # never pay it, and a frame that failed admission never reached it).
        _lib_src = core_file_sync.library_mirror_source(rel_path)
        _writable_libs: frozenset[tuple[str, str]] | None = None
        if _lib_src is not None:
            from storage.knowledge import db_knowledge_libraries
            _writable_libs = await run_db(
                db_knowledge_libraries.writable_pairs_for, agent_slug)
        if not core_file_sync.can_write_back(
                rel_path, _role, _uname, mount_username=_mount,
                writable_libraries=_writable_libs,
                knowledge_rw=bool(getattr(sec, "knowledge_rw", False))):
            # Engine-internal machinery paths (.claude/.codex/.credentials)
            # are denied for EVERY role by design, and engines rewrite their
            # own runtime state each turn (codex: models_cache.json) — that
            # denial is routine, not a signal. Keep WARNING for everything
            # else: a role/scope denial on a NORMAL path is exactly the
            # anomaly this log exists to surface.
            if core_file_sync.is_engine_machinery_path(rel_path):
                logger.debug(
                    "write-back skipped (engine machinery): session=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"), rel_path, action,
                )
            else:
                logger.warning(
                    "write-back denied: session=%s role=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"), _role, rel_path, action,
                )
            return

        # Satellite-side delete inside a library mirror. The satellite only
        # sends ``action=delete`` for a live watcher event (never inferred
        # from absence), so on a WRITABLE attachment it is the agent's
        # explicit intent — route it through the projector's explicit-delete
        # channel (capture at the source, delete at the source, propagate to
        # every mirror; 2026-09-03). A read-only mirror keeps the old rule:
        # ignored, the projector heals the file back from the source.
        # Applying the delete locally would just churn a tombstone against
        # the heal. Content writes above already passed the RW gate.
        if _lib_src is not None and action == "delete":
            _parts = rel_path.split("/")
            _sub_rel = "/".join(_parts[3:]) if len(_parts) >= 4 else ""
            _propagated = False
            if _sub_rel:
                from services.knowledge import library_projector
                try:
                    _propagated = await library_projector.propagate_mirror_delete(
                        agent_slug, _lib_src, _sub_rel)
                except Exception:
                    logger.exception(
                        "library-mirror delete propagation failed: %s/%s",
                        agent_slug, rel_path)
            if _propagated:
                logger.info(
                    "library-mirror delete propagated to source %s: %s/%s",
                    _lib_src, agent_slug, rel_path,
                )
            else:
                logger.info(
                    "library-mirror delete ignored (heals from source): %s/%s",
                    agent_slug, rel_path,
                )
            return
        agent_dir = _cfg.AGENTS_DIR / agent_slug

        # Sync-delta telemetry (alert-only, post-gate): count authorized
        # satellite-authored writes in a rolling window — a per-turn flood
        # usually means a build tree the sync-ignore table doesn't cover.
        if action == "write":
            try:
                from core.remote import sync_delta_alerts
                _sz = msg.get("size") or (
                    len(msg.get("content_b64") or "") * 3 // 4)
                _crossed = sync_delta_alerts.record_turn_write(
                    machine_id, agent_slug, rel_path, int(_sz or 0))
                if _crossed is not None:
                    asyncio.ensure_future(sync_delta_alerts.maybe_alert(
                        machine_id, agent_slug, source="per-turn",
                        new_files=_crossed[0], new_bytes=_crossed[1],
                        subtree_counts=_crossed[2]))
            except Exception:
                logger.debug("sync-delta turn counter failed", exc_info=True)

        # All platform-side writes to this agent file serialize on the global
        # per-(agent, rel_path) lock — across sessions and machines — so
        # pull_through / push_back / this applier / the fan-out never interleave
        # a torn write. agent_slug is guaranteed non-empty past admission.
        from core.remote import remote_file_flow
        from services.remote import workspace_fanout
        conflict_notify: tuple[str, str] | None = None  # (loser_slug, filename)
        fanout_source: bytes | Path | None = None
        fanout_lock = None
        lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        try:
            async with lock:
                from storage.files import sync_state_store
                from storage.files import file_tombstones_store
                from storage.files import file_author_store
                from storage.files import recover_bin_store

                # Pre-capture the to-be-removed/overwritten bytes. Size-gated, so a
                # large file is never read on the apply path (None → not captured).
                pre_bytes, pre_hash = await self._capture_pre_overwrite(
                    agent_dir, rel_path,
                )

                await self._apply_file_changed_inner(
                    agent_dir, msg, machine_id, agent_slug, action,
                )

                # Keep the versioned-sync state current under this same lock:
                # so the next session-start merge sees no phantom conflict, deletes
                # propagate to idle satellites (tombstone), and a genuine cross-user
                # live overwrite captures the loser + notifies them.
                if action == "delete":
                    await run_db(
                        file_tombstones_store.record, agent_slug, rel_path,
                        time.time(), origin="live-delete",
                    )
                    if pre_bytes is not None:
                        await run_db(
                            recover_bin_store.capture, agent_slug, rel_path,
                            pre_bytes, "deleted",
                        )
                    await run_db(
                        sync_state_store.clear_one, machine_id, agent_slug, rel_path,
                    )
                    await run_db(file_author_store.clear, agent_slug, rel_path)
                else:
                    new_hash = msg.get("hash", "") or ""
                    # Clobber check: the platform copy changed since THIS machine's
                    # last-converged base → the satellite is overwriting an edit it
                    # never saw. The satellite wins (the live write applies), so the
                    # platform's prior bytes are the loser → strict capture.
                    base_row = await run_db(
                        sync_state_store.get_one, machine_id, agent_slug, rel_path,
                    )
                    base_hash = base_row[0] if base_row else None
                    if (pre_hash is not None and new_hash
                            and new_hash != pre_hash and pre_hash != base_hash):
                        author = await run_db(file_author_store.get, agent_slug, rel_path)
                        cap_side, cap_reason, notify_user = core_file_sync._divergence_capture(
                            rel_path, lambda _p: author, _uname, platform_wins=False,
                        )
                        if cap_side == "platform" and pre_bytes is not None:
                            entry = await run_db(
                                recover_bin_store.capture, agent_slug, rel_path,
                                pre_bytes, cap_reason,
                            )
                            if entry is not None and notify_user:
                                conflict_notify = (notify_user, entry["original_name"])
                    # Advance base + author to the satellite's just-applied write.
                    base_mtime = await asyncio.to_thread(self._mtime_of, agent_dir, rel_path)
                    await run_db(
                        sync_state_store.record_one, machine_id, agent_slug,
                        rel_path, new_hash, base_mtime,
                    )
                    await run_db(file_author_store.record, agent_slug, rel_path, _uname)

                # Fan out the applied change to every OTHER satellite of this
                # agent (isolation-filtered inside). The one store read a
                # personal path needs runs on the DB lane, never here.
                if action == "delete":
                    await workspace_fanout.fan_out_delete(
                        agent_slug, rel_path, exclude_machine_id=machine_id,
                    )
                elif workspace_fanout.fanout_targets(
                    agent_slug, rel_path, exclude_machine_id=machine_id,
                    shared_only=await workspace_fanout.shared_only_of(agent_slug, rel_path),
                ):
                    # The fan-out lock is taken here (inside the path lock, so
                    # fan-outs of this path keep apply order) and the push runs
                    # after the release: the applied bytes for an inline write,
                    # the file's path for a pulled one (push_file streams per
                    # window, so memory stays O(window) even for a 1GB file).
                    if "content_b64" in msg:
                        fanout_source = await asyncio.to_thread(
                            base64.b64decode, msg.get("content_b64") or "")
                    else:
                        fanout_source = await self._workspace_path_checked(agent_dir, rel_path)
                    if fanout_source is not None:
                        fanout_lock = await remote_file_flow.acquire_fanout_lock(
                            agent_slug, rel_path)
                        await fanout_lock.acquire()
        except Exception:
            logger.exception(
                "_apply_file_changed: failed for %s/%s", agent_slug, rel_path,
            )
            if fanout_lock is not None and fanout_lock.locked():
                fanout_lock.release()
            return

        if fanout_lock is not None:
            try:
                await workspace_fanout.fan_out_write(
                    agent_slug, rel_path, fanout_source, exclude_machine_id=machine_id,
                )
            except Exception:
                logger.exception(
                    "_apply_file_changed: fan-out failed for %s/%s", agent_slug, rel_path,
                )
            finally:
                fanout_lock.release()

        if conflict_notify is not None:
            # Notify after releasing the lock — it doesn't need it.
            await self._notify_live_conflict(agent_slug, *conflict_notify)

        # Refresh any open dashboard workspace view — the live write/delete just
        # changed the platform tree. Best-effort, outside the
        # lock (it doesn't need it). Both actions notify: a write adds/updates the
        # file, a delete makes it vanish from the refetched tree. NOT excluding the
        # satellite's user — that user is the one watching the dashboard for the
        # file to appear/disappear.
        from services.notifications import notification_manager
        await notification_manager.broadcast_file_updated(
            agent_slug, rel_path, source="disk",
        )

        # Knowledge-library projection (best-effort, post-lock): a write into
        # a promoted source's knowledge/ updates every consumer mirror; an
        # authorized RW mirror write flows back to its source. Both no-op in
        # one SELECT when the agent has no library rows.
        from services.knowledge import library_projector
        if _lib_src is not None:
            _parts = rel_path.split("/")
            if len(_parts) >= 4:
                asyncio.create_task(library_projector.propagate_mirror_write(
                    agent_slug, _lib_src, "/".join(_parts[3:]),
                ))
        elif rel_path.startswith("knowledge/"):
            asyncio.create_task(library_projector.propagate_source_write(
                agent_slug, rel_path[len("knowledge/"):],
                deleted=(action == "delete"),
            ))

    async def _capture_pre_overwrite(
        self, agent_dir, rel_path: str,
    ) -> tuple[bytes | None, str | None]:
        """Read + hash the current on-disk bytes of a workspace file BEFORE it is
        overwritten, for conflict detection — but ONLY if it exists and is
        ≤ ``CONFLICT_BACKUP_MAX_BYTES``. Returns ``(bytes, "sha256:<hex>")`` or
        ``(None, None)``. Never reads a large file on the apply path. The read
        opens beneath the agents root with no component followed: a link at
        the name or on the way is refused, never read through.
        """
        def _read() -> tuple[bytes | None, str | None]:
            agent = Path(agent_dir)
            try:
                data = safe_fs.read_bytes_beneath(
                    agent.parent, f"{agent.name}/{rel_path}",
                    max_size=CONFLICT_BACKUP_MAX_BYTES,
                )
            except OSError:
                return (None, None)
            return (data, "sha256:" + hashlib.sha256(data).hexdigest())
        return await asyncio.to_thread(_read)

    async def _workspace_path_checked(self, agent_dir, rel_path: str) -> Path | None:
        """Resolve a workspace file's on-disk Path (post-apply) for streaming
        fan-out. Returns None on missing / not-a-file / path-traversal."""
        def _check() -> Path | None:
            try:
                base = Path(agent_dir).resolve()
                dest = (base / rel_path).resolve()
                dest.relative_to(base)
            except (ValueError, OSError):
                return None
            try:
                return dest if dest.is_file() else None
            except OSError:
                return None
        return await asyncio.to_thread(_check)

    def _mtime_of(self, agent_dir, rel_path: str) -> float:
        """Current mtime of a platform file (epoch seconds), or 0.0 — used to stamp
        the merge base after a live write-back."""
        try:
            return (Path(agent_dir) / rel_path).stat().st_mtime
        except OSError:
            return 0.0

    async def _notify_live_conflict(
        self, agent_slug: str, loser_slug: str, filename: str,
    ) -> None:
        """Notify a user whose edit just lost a live cross-user conflict — their
        version is in the workspace Recover bin (no download link; the dashboard
        deep-links to the Recover button). Best-effort — never raises."""
        try:
            from storage import database
            from services.notifications import notification_manager

            from storage.pg import run_db
            loser_sub = await run_db(database.get_user_sub_by_username, loser_slug)
            if not loser_sub:
                return  # prior writer no longer maps to a user → nobody to notify
            await notification_manager.fire_notification(
                title="Recover your file",
                body=(
                    f"Your version of “{filename}” was replaced by a newer edit "
                    f"from another user, but is recoverable."
                ),
                severity="info", scope="user", target=loser_sub,
                source="file_conflict", agent_slug=agent_slug,
            )
        except Exception:
            logger.exception(
                "live conflict notify failed for %s/%s", agent_slug, filename,
            )

    async def _apply_file_changed_inner(
        self, agent_dir, msg: dict, machine_id: str, agent_slug: str, action: str,
    ) -> None:
        """Inner half of _apply_file_changed (lock already held if applicable)."""
        from core.remote import file_sync as core_file_sync

        if action == "delete":
            await asyncio.to_thread(
                core_file_sync.apply_incoming_file,
                agent_dir, msg["path"], "delete", None,
            )
            return

        # Routing: an inline write carries the content key (possibly "" — a
        # ZERO-BYTE file is a real write, not an absence); a large-file
        # notification carries no content key but a size, and is pulled.
        if "content_b64" in msg:
            await asyncio.to_thread(
                core_file_sync.apply_incoming_file,
                agent_dir, msg["path"], "write", msg.get("content_b64") or "",
            )
            return

        if msg.get("size", 0) > 0:
            # Large file — stream the body straight to disk (chunked pull),
            # never holding the whole file in memory.
            from services.path_policy_v2 import PathRef
            dest = (Path(agent_dir) / msg["path"]).resolve()
            try:
                dest.relative_to(Path(agent_dir).resolve())
            except ValueError:
                logger.warning(
                    "file_changed pull traversal blocked: %s", msg["path"],
                )
                return
            ok = await self.pull_file_to_path(
                machine_id,
                PathRef("agent_tree", msg["path"]),
                dest,
                agent_slug=agent_slug,
                timeout=core_file_sync.pull_timeout_for_size(
                    int(msg.get("size", 0) or 0)
                ),
            )
            if not ok:
                logger.warning(
                    "file_changed pull failed for %s", msg.get("path"),
                )
