"""One file push in flight (``satellite_file_transfer.push_file``): the run
that holds what the loop's locals held, the frame builder and the ack
callback as its methods, and the loop's three stages, each answering the
verdict the loop acts on. ``push_file`` keeps the entry checks, the chunk
sizing (the names the tests patch on its module) and the ``try/finally``.
``satellite_connection`` imports the mixin, so its exception and its token
are imported inside the stages that use them, never at module level.
"""

import asyncio
import inspect
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

if TYPE_CHECKING:
    from core.remote.satellite_connection import (
        SatelliteConnection,
        SatelliteConnectionManager,
        _BulkCredit,
    )
    from services.path_policy_v2 import PathRef

logger = logging.getLogger("otodock.satellite")

# A stage's verdict. FAILED: the push is over; ``run.failure`` names the
# cause the ``finally`` warns about, and stays empty where the stage logged
# its own line (a stale ack, a source that vanished or changed size). SENT:
# a frame went out, the loop goes round without waiting. IDLE: nothing ended
# the push and nothing went out, so the loop may wait.
FAILED = "failed"
SENT = "sent"
IDLE = "idle"


@dataclass
class _PushRun:
    """One push in flight: the connection it started on and its credit, the
    source facts, the chunking decided once (so the ``total_chunks`` it
    sends stays true), the clocks, and the loop's state: the frames on their
    way, the credit's ticket, the chunk read and not yet sent, the failure
    the ``finally`` warns about."""
    mgr: "SatelliteConnectionManager"
    machine_id: str
    conn: "SatelliteConnection"
    credit: "_BulkCredit"
    ref: "PathRef"
    agent_slug: str
    expect: "dict | str | None"
    stale_out: dict | None
    progress_cb: Callable | None
    source: "bytes | Path"
    from_path: bool
    path: Path | None
    size: int
    content_hash: str
    fh: BinaryIO | None
    chunk_size: int
    inline: bool
    total_chunks: int
    stall: float
    started: float
    ceiling_at: float
    last_progress: float
    # (command_id, future, token, end offset) per frame on its way.
    outstanding: deque = field(default_factory=deque)
    taking: asyncio.Future | None = None   # a queued ticket of the credit
    taking_n: int = 0
    ready: bool = False   # the credit for ``chunk`` is held, the frame not yet sent
    chunk: bytes = b""
    idx: int = 0
    offset: int = 0
    acked_end: int = 0
    failure: str = ""
    # The frame's own fields instead of a ``file_push``'s (a session move's
    # push names its scope, never a path): every frame then carries the chunk
    # fields, the hash on the last.
    frame_fields: dict | None = None

    async def notify(self, sent: int, total: int) -> None:
        """The caller's progress callback, sync or async, fenced: it never
        aborts the transfer."""
        if self.progress_cb is None:
            return
        try:
            res = self.progress_cb(sent, total)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001  # a caller's progress callback must never abort the transfer
            logger.debug("push_file progress_cb failed", exc_info=True)

    def read_exact(self, n: int) -> bytes:
        """The next ``n`` bytes of ``fh`` (a thread's work: ``asyncio.to_thread``)."""
        # Retry mid-file partial reads (raw IO may return short); only a
        # true EOF — the file shrank — yields fewer than n bytes.
        buf = b""
        while len(buf) < n:
            block = cast("BinaryIO", self.fh).read(n - len(buf))
            if not block:
                break
            buf += block
        return buf

    def frame(self, idx: int, chunk: bytes) -> dict:
        """The ``file_push`` frame of chunk ``idx``: the inline write, or a
        ``write_chunk`` with the hash on the last one."""
        import base64 as _b64
        if self.frame_fields is not None:
            last = idx == self.total_chunks - 1
            return {**self.frame_fields, "content_b64": _b64.b64encode(chunk).decode(),
                    "chunk_index": idx, "total_chunks": self.total_chunks,
                    "hash": self.content_hash if last else ""}
        msg: dict = {
            "type": "file_push",
            "path_kind": self.ref.kind,
            "agent_slug": self.agent_slug,
            "path": self.ref.value,
            "content_b64": _b64.b64encode(chunk).decode(),
        }
        if self.inline:
            msg["action"] = "write"
            msg["hash"] = self.content_hash
        else:
            msg["action"] = "write_chunk"
            msg["chunk_index"] = idx
            msg["total_chunks"] = self.total_chunks
            msg["hash"] = self.content_hash if idx == self.total_chunks - 1 else ""
        if self.expect is not None:
            msg["expect"] = self.expect
        return msg

    def on_answer(self, fut: asyncio.Future, n: int) -> None:
        """The done callback of a frame's ack future, ``n`` the frame's bytes
        bound at the send (a late-bound read would release the wrong credit)."""
        from core.remote.remote_file_flow import PUSH_ACK_STALE
        # The credit comes back exactly once per frame, however its
        # wait ended. A stale answer is the satellite's, in time.
        answered = not fut.cancelled() and fut.exception() is None
        if answered:
            self.conn.last_transfer_at = time.monotonic()
        self.credit.release(
            n, acked=answered and (fut.result() or {}).get("status") in ("ok", PUSH_ACK_STALE),
        )


async def _push_reap_answers(run: _PushRun) -> str:
    """The oldest answers, in their order: a future that dropped or failed
    ends the push, a ``stale`` ack reports the machine's copy (no failure:
    the caller's own outcome), a refusal is the failure, an ok ack moves the
    acked end and the clock, records the last frame's committed stat and
    notifies the progress. Then a later answer with the oldest unanswered
    means that frame was lost. FAILED, or IDLE to go on."""
    from core.remote.remote_file_flow import PUSH_ACK_STALE, record_committed_stat
    while run.outstanding and run.outstanding[0][1].done():
        cid, fut, _token, end = run.outstanding.popleft()
        run.mgr._pending_acks.pop(cid, None)
        if fut.cancelled() or fut.exception() is not None:
            run.failure = ("the connection dropped" if fut.cancelled()
                           else str(fut.exception()))
            return FAILED
        ack = fut.result() or {}
        if ack.get("status") == PUSH_ACK_STALE:
            reason = "changed" if ack.get("reason") == "changed" else "live"
            if run.stale_out is not None:
                run.stale_out.update(reason=reason, size=ack.get("size"),
                                     mtime_ns=ack.get("mtime_ns"))
            logger.info(
                "push to %s %s not applied: the machine's copy %s",
                run.machine_id[:8], run.ref.value,
                "changed since the platform saw it" if reason == "changed"
                else "is being written",
            )
            return FAILED
        if ack.get("status") != "ok":
            run.failure = f"the satellite refused it ({ack.get('error') or 'error'})"
            return FAILED
        run.last_progress = time.monotonic()
        run.acked_end = end
        if end >= run.size and run.ref.kind == "agent_tree":
            record_committed_stat(run.machine_id, run.agent_slug, run.ref.value, ack)
        if end < run.size:
            await run.notify(end, run.size)
    if run.outstanding and any(f.done() for _c, f, _t, _e in list(run.outstanding)[1:]):
        # A satellite answers its frames in arrival order: a
        # later answer with the oldest unanswered means that
        # frame was lost.
        run.failure = "a frame was never answered"
        return FAILED
    return IDLE


async def _push_read_chunk(run: _PushRun) -> str:
    """The next chunk, when one is left and no ticket is pending: its bytes
    (a source that vanished or changed size ends the push, logged, no
    failure of the push's own), then the credit's ticket: granted at once
    the chunk is ``ready``, queued it is ``taking``, a credit that is gone
    is the failure. FAILED, or IDLE to go on."""
    from core.remote.satellite_connection import CreditFailed
    if run.idx < run.total_chunks and run.taking is None and not run.ready:
        expected = run.size if run.inline else min(run.chunk_size, run.size - run.offset)
        if run.inline:
            if run.from_path:
                try:
                    run.chunk = await asyncio.to_thread(cast("Path", run.path).read_bytes)
                except OSError as e:
                    logger.warning("push_file: cannot read %s: %s", run.path, e)
                    return FAILED
            else:
                run.chunk = bytes(run.source)
        elif run.from_path:
            try:
                run.chunk = await asyncio.to_thread(run.read_exact, expected)
            except OSError as e:
                logger.warning("push_file: cannot read %s: %s", run.path, e)
                return FAILED
        else:
            run.chunk = bytes(cast("bytes", run.source)[run.offset:run.offset + expected])
        if len(run.chunk) != expected:
            logger.warning(
                "push_file: %s changed size mid-push (expected %d-byte "
                "chunk, got %d) — aborted", run.ref.value, expected, len(run.chunk),
            )
            return FAILED
        run.taking_n = len(run.chunk)
        try:
            run.taking = run.credit.reserve(run.taking_n)
        except CreditFailed as e:
            run.failure = str(e)
            return FAILED
        run.ready = run.taking is None
    return IDLE


async def _push_send_frame(run: _PushRun) -> str:
    """A granted ticket makes the chunk ``ready`` (a ticket that dropped or
    failed ends the push); a ready chunk goes out as one frame on the
    connection the push started on (one replaced or gone is the failure),
    its ack future releasing the credit through ``on_answer`` with the
    frame's bytes bound now. SENT after a frame, else IDLE."""
    from core.remote.satellite_connection import _XferToken
    if run.taking is not None and run.taking.done():
        granted = run.taking
        run.taking = None
        if granted.cancelled() or granted.exception() is not None:
            run.failure = ("the connection dropped" if granted.cancelled()
                           else str(granted.exception()))
            return FAILED
        run.ready = True
    if run.ready:
        current = run.mgr._connections.get(run.machine_id)
        if current is not run.conn:
            run.failure = ("the connection dropped" if current is None
                           else "the connection was replaced")
            return FAILED
        run.ready = False
        cid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        fut.add_done_callback(lambda f, n=run.taking_n: run.on_answer(f, n))  # type: ignore[misc]  # mypy infers no type for a lambda with a default argument (checker limitation)
        run.mgr._pending_acks[cid] = (run.machine_id, fut, run.conn)
        token = _XferToken()
        msg = run.frame(run.idx, run.chunk)
        msg["command_id"] = cid
        msg["_xfer"] = token
        run.offset += len(run.chunk)
        run.idx += 1
        run.outstanding.append((cid, fut, token, run.offset))
        run.chunk = b""
        await run.conn.enqueue_send(msg, bulk=True)
        return SENT
    return IDLE
