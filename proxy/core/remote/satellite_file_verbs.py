"""The satellite connection's file transfer verbs: push a file, stat one, and the
pull stream with its staged commit. ``SatelliteFileTransferMixin`` keeps the
file-changed lane and has this mixin as its base.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from core.remote.satellite_push_run import (
    FAILED,
    SENT,
    _push_read_chunk,
    _push_reap_answers,
    _push_send_frame,
    _PushRun,
)
from services.infra import safe_fs

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
    )
    from core.remote.satellite_host import SatelliteManagerHost as _Host
    from services.path_policy_v2 import PathRef
else:
    _Host = object

logger = logging.getLogger("otodock.satellite")


# A push's chunk on a connection measured slow (512 KiB, MAX_CHUNK_SIZE,
# otherwise): more of the credit's bytes are acked sooner, and an ack waits
# behind less.
PUSH_SLOW_CHUNK_BYTES = 128 * 1024


def push_ceiling_s(size: int) -> float:
    """The longest a push of ``size`` bytes may run however it moves: 16 KiB/s
    at the least, 10 minutes for anything smaller."""
    return max(600.0, size / (16 * 1024))


def _staging_name(name: str) -> str:
    """A pull's own staging name beside ``name``: hidden, random, and
    ending in ``.partial`` (both manifests skip it, retention reaps an
    orphan). The name part is cut to 200 bytes so the whole fits in 255."""
    base = os.fsdecode(os.fsencode(name)[:200])
    return f".{base}.{secrets.token_hex(8)}.partial"


def _in_agents_tree(dest: Path) -> bool:
    """An agent's own tree (an agent folder never starts with a dot; the
    host cache does)."""
    import config
    try:
        rel = safe_fs.rel_under(dest, config.AGENTS_DIR)
    except safe_fs.EscapeRefused:
        return False
    return bool(rel) and not rel.startswith(".")


def _close_opened_root(job: "asyncio.Future[tuple[int, str]]") -> None:
    """Close the handle a pull's root-open produced after its caller left."""
    if job.cancelled() or job.exception() is not None:
        return
    with contextlib.suppress(OSError):
        os.close(job.result()[0])


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
    handle: "BinaryIO | None" = None  # opened lazily on the first chunk
    received_bytes: int = 0
    # The caller's own cap on the bytes, judged chunk by chunk (0 = the
    # sync's MAX_FILE_SIZE).
    max_bytes: int = 0
    # The final chunk's sha256; the commit (off the loop) compares against it.
    expected_hash: str = ""
    # The sha256 of the bytes the commit renamed into place.
    committed_hash: str = ""
    # The connection the pull went out on (a replacement rejects it by
    # identity), when it started and when its last chunk came (monotonic),
    # and the chunk count the satellite announced (its ceiling).
    conn: object = None
    started: float = 0.0
    first_chunk_at: float = 0.0
    last_chunk_at: float = 0.0
    total_chunks: int = 0
    # The satellite ended the stream itself (its final chunk or its error):
    # every other end tells a 0.5.137 satellite to stop producing.
    ended: bool = False
    # The caller left before the commit ran (its path lock went with it):
    # the job cleans up instead of renaming over a write made since.
    abandoned: bool = False
    # The destination is a platform copy the sync hashes (not a private
    # snapshot, nor the host cache under the agents root): only then is its
    # verified hash worth keeping.
    in_agents_tree: bool = False
    # The caller's ``progress_cb(bytes_received, bytes_expected)``, called in
    # the receive loop after each written chunk: synchronous and quick.
    progress_cb: "Callable[[int, int], object] | None" = None


def _is_descriptor_path(source: object) -> bool:
    """``source`` is the path ``safe_fs.fd_path`` gives for an open descriptor."""
    text = os.fspath(source) if isinstance(source, (str, os.PathLike)) else ""
    for prefix in ("/proc/self/fd/", "/dev/fd/"):
        if isinstance(text, str) and text.startswith(prefix) and text[len(prefix):].isdigit():
            return True
    return False


class SatelliteFileVerbsMixin(_Host):
    async def push_file(
        self,
        machine_id: str,
        ref: "PathRef",
        source: "bytes | Path",
        *,
        agent_slug: str = "",
        stall_s: float | None = None,
        progress_cb=None,
        content_hash: str | None = None,
        expect: "dict | str | None" = None,
        stale_out: dict | None = None,
        frame_fields: dict | None = None,
    ) -> bool:
        """Push a file to the satellite and wait for its ack.

        ``source`` is either the file's bytes (small payloads already in
        memory) or the ``Path`` of a descriptor the caller opened and keeps
        open (``safe_fs.fd_path``) — the streaming mode: chunks are read one
        at a time, so memory stays O(chunk) regardless of file size. Any
        other path is refused (``ValueError``): a name checked earlier and
        opened here could lead elsewhere since, so the caller opens the file
        where it judged it and hands over that descriptor. ``push_file``
        picks the inline fast path internally for small files either way.

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

        Every frame carries a ``command_id`` (every satellite from 0.5.76
        acks each one after applying it) and waits for the connection's
        bulk credit before it is enqueued, so the bytes ahead of the
        protocol's pong stay bounded on a slow uplink. A frame's credit
        comes back with its ack. The push fails only when the transfer
        stops moving: no ack for ``stall_s`` (``PUSH_STALL_S``, 90 s) while
        nothing else moves on the connection either, or past
        ``push_ceiling_s(size)``. Chunks are 512 KiB, 128 KiB on a
        connection measured slow (decided once per push, so the
        ``total_chunks`` it sends stays true).

        ``progress_cb(bytes_sent, bytes_total)`` — optional, sync or async;
        invoked after each acked chunk and once terminally with
        ``(size, size)``. Fully fenced: a raising/broken callback never
        aborts the transfer.

        ``content_hash`` (``sha256:<hex>``) pins the content: it is the
        frame's ``hash`` (the inline write, the last chunk) and the source
        is not hashed again. The satellite verifies the bytes it received
        against it and refuses a mismatch (the push answers False), so a
        caller that records a merge base of that hash never records one of
        other bytes.

        ``expect`` is what the platform knows of the machine's copy of an
        agent-tree path: its ``{size, mtime_ns}`` (the manifest, a pull, a
        previous push) or ``"live"`` (nothing). A 0.5.150 satellite leaves a
        file that does not match, or one written within its live window,
        alone and answers ``stale``: the push returns False and
        ``stale_out`` (when given) takes the ack's ``reason`` and the file's
        ``size`` and ``mtime_ns``. An older satellite ignores the key. The
        committed stat a 0.5.150 ``ok`` ack carries is recorded as the
        machine's copy (``remote_file_flow.record_pushed_stat``).

        Returns True on success, False on a refusal, a stall, a dropped or
        replaced connection, the cap, or a source that vanished or changed
        size.
        """
        import hashlib as _hashlib

        import config
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        if ref.kind == "agent_tree" and not agent_slug:
            raise ValueError("push_file(agent_tree) requires agent_slug")
        if not isinstance(source, (bytes, bytearray)) and not _is_descriptor_path(source):
            raise ValueError("push_file streams only from an open descriptor's path (safe_fs.fd_path)")

        from core.remote import file_sync
        from core.remote.file_sync import MAX_CHUNK_SIZE

        # Resolve (size, content_hash) without ever holding a big file in
        # memory: Path sources are stat'd + stream-hashed off the event loop.
        from_path = not isinstance(source, (bytes, bytearray))
        if from_path:
            path = Path(cast("Path", source))
            try:
                st = await asyncio.to_thread(os.stat, path)
                size = st.st_size
                if content_hash is None:
                    content_hash = await asyncio.to_thread(file_sync._hash_file, path)
            except OSError as e:
                logger.warning("push_file: cannot read %s: %s", path, e)
                return False
        else:
            size = len(cast("bytes", source))
            if content_hash is None:
                content_hash = f"sha256:{_hashlib.sha256(cast('bytes', source)).hexdigest()}"

        # Never send a satellite a file above the sync cap it applies (the
        # transfer would only burn bandwidth and fail at commit).
        cap = file_sync.MAX_FILE_SIZE
        if size > cap:
            logger.warning(
                "push_file: %s is %.1f MB > %d MB cap for machine %s — skipped",
                ref.value, size / 1024 / 1024, cap // 1024 // 1024, machine_id[:8],
            )
            return False

        # The stat and the hash took time: the machine may have reconnected
        # meanwhile, and a push on the replaced connection would fail for
        # nothing while the new one is healthy.
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        credit = conn.bulk_credit
        # A credit set below one chunk paces nothing unless the frames are
        # smaller: the slow chunk then, the inline write included.
        chunk_size = MAX_CHUNK_SIZE
        if credit.is_slow() or credit.cap < MAX_CHUNK_SIZE:
            chunk_size = min(MAX_CHUNK_SIZE, PUSH_SLOW_CHUNK_BYTES)
        inline = size <= chunk_size
        total_chunks = 1 if inline else (size + chunk_size - 1) // chunk_size
        stall = float(stall_s if stall_s is not None else getattr(config, "PUSH_STALL_S", 90.0))
        started = time.monotonic()
        ceiling_at = started + push_ceiling_s(size)

        # total_chunks and the hash are captured at start: a Path source that
        # SHRINKS mid-push (short read) aborts immediately — never send a
        # truncated stream under a stale total_chunks. A same-size content
        # mutation is caught by the satellite's final-chunk sha256 verify
        # against the pre-computed hash (error ack → False → retried by the
        # next sync cycle).
        fh = None
        if from_path and not inline:
            try:
                # Unbuffered: sequential chunk reads need no readahead layer,
                # and a buffered reader could mask a mid-push truncation by
                # serving pre-buffered bytes.
                fh = await asyncio.to_thread(open, path, "rb", 0)
            except OSError as e:
                logger.warning("push_file: cannot open %s: %s", path, e)
                return False

        run = _PushRun(
            mgr=cast("SatelliteConnectionManager", self), machine_id=machine_id, conn=conn,
            credit=credit, ref=ref,
            agent_slug=agent_slug, expect=expect, stale_out=stale_out,
            progress_cb=progress_cb, source=source, from_path=from_path,
            path=path if from_path else None, size=size, content_hash=content_hash,
            fh=fh, chunk_size=chunk_size, inline=inline, total_chunks=total_chunks,
            stall=stall, started=started, ceiling_at=ceiling_at, last_progress=started,
            frame_fields=frame_fields,
        )
        try:
            while True:
                if await _push_reap_answers(run) == FAILED:
                    return False
                if run.idx >= total_chunks and not run.outstanding:
                    break
                if await _push_read_chunk(run) == FAILED:
                    return False
                verdict = await _push_send_frame(run)
                if verdict == FAILED:
                    return False
                if verdict == SENT:
                    continue
                now = time.monotonic()
                progress = max(run.last_progress, conn.last_transfer_at)
                deadline = min(progress + stall, ceiling_at)
                if now >= deadline:
                    run.failure = (f"no answer for {stall:.0f} s" if deadline < ceiling_at
                                   else f"past its {ceiling_at - started:.0f} s ceiling")
                    return False
                # Any answer wakes the push: the oldest one moves it on, a
                # later one first shows a lost frame.
                waits = {fut for _c, fut, _t, _e in run.outstanding}
                if run.taking is not None:
                    waits.add(run.taking)
                await asyncio.wait(waits, timeout=deadline - now,
                                   return_when=asyncio.FIRST_COMPLETED)
            await run.notify(size, size)
            took = time.monotonic() - started
            logger.log(
                logging.INFO if total_chunks > 1 else logging.DEBUG,
                "push to %s %s: %.1f MB in %.0f s (%.0f kB/s), chunk %d KiB, credit %d KiB",
                machine_id[:8], ref.value, size / 1e6, took, size / 1e3 / max(took, 1e-3),
                chunk_size // 1024, credit.cap // 1024,
            )
            return True
        finally:
            if run.taking is not None:
                credit.abandon(run.taking_n, run.taking)
            if run.ready:
                credit.release(run.taking_n, acked=False)
            for cid, fut, token, _end in run.outstanding:
                token.cancelled = True
                self._pending_acks.pop(cid, None)
                if not fut.done():
                    fut.cancel()
            if run.failure:
                logger.warning(
                    "push to %s %s failed with %.1f of %.1f MB acked: %s",
                    machine_id[:8], ref.value, run.acked_end / 1e6, size / 1e6, run.failure,
                )
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
        """Cheap file metadata probe on the satellite.

        Returns ``{"exists": bool, "size": int, "mtime_ns": int}`` — both
        stat values are SATELLITE-clock/filesystem facts, so comparing two
        probes (or a probe against a value recorded at pull time) never
        involves cross-host clock skew. Returns ``None`` on timeout, policy
        reject or disconnect. ``None`` means "don't trust the cache":
        callers fall back to a full pull, never to serving stale bytes.
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
        stall_s: float | None = None,
        max_bytes: int | None = None,
        request_fields: dict | None = None,
        progress_cb: "Callable[[int, int], object] | None" = None,
        hash_out: dict | None = None,
    ) -> bool:
        """Stream a file from the satellite to ``dest_path`` (bounded memory).

        The satellite chunks the file into ``file_content`` messages; each
        decoded chunk is written straight to a staging file of the pull's own
        beside the destination (``_staging_name``) and the file is atomically
        renamed into place on the final chunk (sha256-verified). Returns True on success; False on a stall,
        read-denied / not-found / hash-mismatch / size-cap / disconnect.

        The pull fails only when the transfer stops moving: no chunk for
        ``stall_s`` (``PULL_STALL_S``, 90 s) while nothing else moves on the
        connection either, or past ``max(10 min, the file's size at
        16 KiB/s)`` from its first chunk, or 10 minutes from its start while
        no chunk came.

        ``max_bytes`` fails the pull at the chunk that would pass it, before
        those bytes are written (a caller that read a size from a manifest
        bounds what may land even when the file grew since).

        ``progress_cb(bytes_received, bytes_expected)`` (``max_bytes``, else
        0: unknown) runs in the receive loop after each written chunk, so it
        must be synchronous and quick; one that raises is logged and the
        pull goes on.

        ``hash_out`` (when given) takes ``hash`` and ``size``, the sha256 and
        the length of the bytes the pull committed, on success only: the bytes
        the machine sent.

        Same ``ref.kind`` semantics as ``push_file``. A destination in the
        agents tree is reached beneath the agents root with no component
        followed (its missing parents created there), so a link planted at
        any parent refuses the pull; a destination elsewhere (a
        platform-owned cache) opens beneath its own parent. The caller is
        still responsible for authorizing ``dest_path``.
        """
        import config
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        if ref.kind == "agent_tree" and not agent_slug:
            raise ValueError("pull_file_to_path(agent_tree) requires agent_slug")

        dest = Path(dest_path)
        staging = _staging_name(dest.name)
        partial = dest.parent / staging
        open_job = asyncio.ensure_future(asyncio.to_thread(self._open_pull_root, dest))
        try:
            rootfd, dest_rel = await asyncio.shield(open_job)
        except asyncio.CancelledError:
            # The thread runs on whatever the caller does: wait for its mkdir
            # (a caller that removes its folder after the cancel must not see
            # it come back) and close the handle it opens.
            open_job.add_done_callback(_close_opened_root)
            with contextlib.suppress(BaseException):
                await asyncio.wait({open_job})
            raise
        except OSError as e:
            logger.warning(
                "Satellite %s file pull refused (%s): %s", machine_id[:8], dest, e,
            )
            return False
        # The root opened off the loop: the connection may have been replaced
        # meanwhile, and the pull goes out on the one that will answer.
        conn = self._connections.get(machine_id)
        if conn is None:
            os.close(rootfd)
            return False
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        started = time.monotonic()
        self._pending_pulls[request_id] = _PullStream(
            machine_id=machine_id,
            dest_path=dest,
            partial_path=partial,
            future=future,
            hasher=hashlib.sha256(),
            rootfd=rootfd,
            dest_rel=dest_rel,
            partial_rel=f"{dest_rel.rpartition('/')[0]}/{staging}".lstrip("/"),
            conn=conn,
            started=started,
            max_bytes=max(0, max_bytes or 0),
            in_agents_tree=_in_agents_tree(dest),
            progress_cb=progress_cb,
        )
        stall = float(stall_s if stall_s is not None else getattr(config, "PULL_STALL_S", 90.0))
        try:
            # ``request_fields`` replace the path a ``file_pull`` names (a
            # session move's pull names its scope and the file's place in it).
            await conn.enqueue_send({
                "request_id": request_id,
                **(request_fields or {"type": "file_pull", "path_kind": ref.kind,
                                      "agent_slug": agent_slug, "path": ref.value}),
            })
            # True means every byte is on disk (the final chunk arrived);
            # False is a failed or refused transfer.
            received = await self._await_pull(self._pending_pulls[request_id], conn, stall)
        except (asyncio.TimeoutError, RuntimeError) as e:
            logger.warning(
                "Satellite %s file pull of %s failed: %s", machine_id[:8], ref.value, e,
            )
            st = self._pending_pulls.pop(request_id, None)
            if st is not None:
                self._cleanup_pull_stream(st)
                self._cancel_satellite_pull(machine_id, st, request_id)
            return False
        except BaseException:
            st = self._pending_pulls.pop(request_id, None)
            if st is not None:
                self._cleanup_pull_stream(st)
                self._cancel_satellite_pull(machine_id, st, request_id)
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
            self._cancel_satellite_pull(machine_id, st, request_id)
            return False
        from core import file_commit
        # The commit runs whatever the caller does: a caller cancelled while
        # the job waits for a pool worker would otherwise leave the .partial
        # and the root descriptor behind. A job that finds its caller gone
        # cleans up instead of renaming: the caller's path lock went with it,
        # and a platform write may have landed meanwhile. The caller waits
        # for the job either way (the open job's shape): its own cleanup, a
        # temp folder removed, never races a rename into it.
        commit = asyncio.ensure_future(file_commit.run(self._commit_pull_sync, st))
        try:
            ok = await asyncio.shield(commit)
        except asyncio.CancelledError:
            st.abandoned = True
            await asyncio.wait({commit})
            raise
        if ok:
            took = time.monotonic() - started
            logger.log(
                logging.INFO if st.total_chunks > 1 else logging.DEBUG,
                "pull from %s %s: %.1f MB in %.0f s (%.0f kB/s)",
                machine_id[:8], ref.value, st.received_bytes / 1e6, took,
                st.received_bytes / 1e3 / max(took, 1e-3),
            )
            if hash_out is not None:
                hash_out.update(hash=st.committed_hash, size=st.received_bytes)
        return ok

    def _cancel_satellite_pull(self, machine_id: str, st: "_PullStream", request_id: str) -> None:
        """The proxy gave up on a pull the satellite is still producing (a
        stall, its own refusal, a caller that left): the satellite on the
        same connection stops it."""
        if st.ended or self._connections.get(machine_id) is not st.conn:
            return
        cast("SatelliteConnection", st.conn).enqueue_send_nowait(
            {"type": "file_pull_cancel", "request_id": request_id})

    @staticmethod
    async def _await_pull(st: "_PullStream", conn, stall: float) -> bool:
        """The pull's answer, or ``asyncio.TimeoutError`` once it stops
        moving (see ``pull_file_to_path``)."""
        from core.remote.file_sync import MAX_CHUNK_SIZE
        while True:
            now = time.monotonic()
            progress = max(st.started, st.last_chunk_at,
                           getattr(conn, "last_transfer_at", 0.0))
            # The ceiling counts from the first chunk, else from the start:
            # the satellite interleaves its pulls, so a pull that never
            # starts (a file the machine cannot open) ends at the floor.
            if st.first_chunk_at:
                ceiling = max(600.0, st.total_chunks * MAX_CHUNK_SIZE / (16 * 1024))
                ceiling_at = st.first_chunk_at + ceiling
            else:
                ceiling = 600.0
                ceiling_at = st.started + ceiling
            deadline = min(progress + stall, ceiling_at)
            if now >= deadline:
                if deadline < ceiling_at:
                    raise asyncio.TimeoutError(f"no chunk for {stall:.0f} s")
                raise asyncio.TimeoutError(f"past its {ceiling:.0f} s ceiling")
            done, _ = await asyncio.wait({st.future}, timeout=deadline - now)
            if done:
                return st.future.result()

    def _on_pull_chunk(self, st: "_PullStream", msg: dict) -> None:
        """Apply one ``file_content`` chunk to an in-flight pull. Runs in the
        WS receive loop: a 512KB write is sub-ms; the commit is not and runs
        elsewhere (``pull_file_to_path``)."""
        if st.future.done():
            return
        err = msg.get("error")
        if err:
            st.ended = True
            self._fail_pull(st, str(err))
            return
        b64 = msg.get("content_b64", "")
        try:
            block = base64.b64decode(b64) if b64 else b""
        except (ValueError, TypeError) as e:
            self._fail_pull(st, f"bad base64: {e}")
            return
        from core.remote.file_sync import MAX_FILE_SIZE
        if st.received_bytes + len(block) > MAX_FILE_SIZE:
            self._fail_pull(st, "pulled file exceeds MAX_FILE_SIZE")
            return
        if st.max_bytes and st.received_bytes + len(block) > st.max_bytes:
            self._fail_pull(st, f"pulled file exceeds the caller's {st.max_bytes} bytes")
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
        if st.progress_cb is not None:
            try:
                st.progress_cb(st.received_bytes, st.max_bytes)
            except Exception:  # noqa: BLE001  # a caller's progress callback must never abort the pull
                logger.debug("pull progress_cb failed", exc_info=True)
        st.last_chunk_at = time.monotonic()
        if not st.first_chunk_at:
            st.first_chunk_at = st.last_chunk_at
        total = int(msg.get("total_chunks", 0) or 0)
        st.total_chunks = max(st.total_chunks, total)
        expected_hash = msg.get("hash") or ""
        chunk_index = int(msg.get("chunk_index", 0) or 0)
        is_last = bool(expected_hash) or (total and chunk_index >= total - 1)
        if is_last:
            st.ended = True
            st.expected_hash = expected_hash
            if not st.future.done():
                st.future.set_result(True)

    def _commit_pull_sync(self, st: "_PullStream") -> bool:
        """Finalize a received pull: flush, fsync, verify sha256, atomic
        rename, prime the hash cache. Runs on the file-commit executor; it
        never touches the future or the pending map. On any failure the
        partial is removed and the answer is False."""
        if st.abandoned:
            self._cleanup_pull_stream(st)
            return False
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
        st.committed_hash = actual
        # The sha256 of these bytes was just verified — prime the manifest
        # hash cache so the next sync cycle never re-hashes a big pull.
        if st.in_agents_tree:
            from core.remote.file_sync import prime_hash_cache
            prime_hash_cache(st.dest_path, actual)
        return True

    def _fail_pull(self, st: "_PullStream", reason: str) -> None:
        # A path the machine does not have (a lazy pull of an output no tool
        # has written yet) is routine; the satellite's answer says so verbatim.
        level = logging.DEBUG if reason == "File not found" else logging.WARNING
        logger.log(level, "file pull failed (%s): %s", st.dest_path, reason)
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
        """The partial's write descriptor beneath the root: a new file at
        the pull's own random staging name, so nothing planted there (a
        link, a hard link to another file) is ever written into, and two
        pulls into one destination never share one."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        return safe_fs.open_beneath(st.rootfd, st.partial_rel, flags, 0o644)

    @staticmethod
    def _close_pull_root(st: "_PullStream") -> None:
        if st.rootfd >= 0:
            with contextlib.suppress(OSError):
                os.close(st.rootfd)
            st.rootfd = -1
