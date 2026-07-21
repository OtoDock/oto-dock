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
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import contextlib

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
    each decoded chunk is written straight to ``partial_path`` on disk and
    the file is atomically renamed to ``dest_path`` on the final chunk
    (sha256-verified). Bounded memory: only one chunk is held at a time.
    """
    machine_id: str
    dest_path: Path
    partial_path: Path
    future: asyncio.Future
    hasher: "hashlib._Hash"
    handle: object = None  # opened lazily on the first chunk
    received_bytes: int = 0



class SatelliteFileTransferMixin:
    async def push_file(
        self,
        machine_id: str,
        ref: "PathRef",
        content: bytes,
        *,
        agent_slug: str = "",
        timeout: float = 30.0,
    ) -> bool:
        """Push a file to the satellite and wait for its ack.

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

        Handles ≤ 512KB payloads in a single message; larger files are
        chunked. Returns True on success, False on timeout / error /
        disconnect.
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

        from core.remote.file_sync import MAX_CHUNK_SIZE

        def _base_msg(action: str) -> dict:
            return {
                "type": "file_push",
                "path_kind": ref.kind,
                "agent_slug": agent_slug,
                "action": action,
                "path": ref.value,
            }

        content_hash = f"sha256:{_hashlib.sha256(content).hexdigest()}"
        if len(content) <= MAX_CHUNK_SIZE:
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
                    return ack.get("status") == "ok"
                except asyncio.TimeoutError:
                    return False
                except RuntimeError:
                    # Future rejected by deregister (WS dead).
                    return False
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
        total_chunks = (len(content) + MAX_CHUNK_SIZE - 1) // MAX_CHUNK_SIZE
        offset = 0
        chunk_idx = 0
        while offset < len(content):
            chunk = content[offset:offset + MAX_CHUNK_SIZE]
            is_last = offset + MAX_CHUNK_SIZE >= len(content)
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
            finally:
                if command_id:
                    self._pending_acks.pop(command_id, None)
            offset += MAX_CHUNK_SIZE
            chunk_idx += 1
        return True

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

        Same ``ref.kind`` semantics as ``push_file``. The destination's
        parent dir is created; the caller is responsible for validating that
        ``dest_path`` stays within an allowed root (path traversal).
        """
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        if ref.kind == "agent_tree" and not agent_slug:
            raise ValueError("pull_file_to_path(agent_tree) requires agent_slug")

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(dest) + ".partial")
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_pulls[request_id] = _PullStream(
            machine_id=machine_id,
            dest_path=dest,
            partial_path=partial,
            future=future,
            hasher=hashlib.sha256(),
        )
        try:
            await conn.enqueue_send({
                "type": "file_pull",
                "request_id": request_id,
                "path_kind": ref.kind,
                "agent_slug": agent_slug,
                "path": ref.value,
            })
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, RuntimeError) as e:
            logger.warning(
                "Satellite %s file pull timeout/error: %s", machine_id[:8], e,
            )
            return False
        finally:
            st = self._pending_pulls.pop(request_id, None)
            if st is not None:
                self._cleanup_pull_stream(st)

    def _on_pull_chunk(self, st: "_PullStream", msg: dict) -> None:
        """Apply one ``file_content`` chunk to an in-flight pull. Runs in the
        WS receive loop; 512KB disk writes are sub-ms so no thread offload."""
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
                st.handle = open(st.partial_path, "wb")
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
            self._commit_pull(st, expected_hash)

    def _commit_pull(self, st: "_PullStream", expected_hash: str) -> None:
        """Finalize a completed pull: fsync, verify sha256, atomic rename."""
        with contextlib.suppress(OSError):
            if st.handle is not None:
                st.handle.flush()
                os.fsync(st.handle.fileno())
                st.handle.close()
                st.handle = None
        actual = f"sha256:{st.hasher.hexdigest()}"
        if expected_hash and actual != expected_hash:
            logger.warning("pull hash mismatch for %s", st.dest_path)
            self._cleanup_pull_stream(st)
            if not st.future.done():
                st.future.set_result(False)
            return
        try:
            os.replace(st.partial_path, st.dest_path)
        except OSError as e:
            logger.warning("pull commit failed for %s: %s", st.dest_path, e)
            self._cleanup_pull_stream(st)
            if not st.future.done():
                st.future.set_result(False)
            return
        if not st.future.done():
            st.future.set_result(True)

    def _fail_pull(self, st: "_PullStream", reason: str) -> None:
        logger.warning("file pull failed (%s): %s", st.dest_path, reason)
        self._cleanup_pull_stream(st)
        if not st.future.done():
            st.future.set_result(False)

    def _cleanup_pull_stream(self, st: "_PullStream") -> None:
        """Close the handle (if open) and remove a leftover ``.partial``.
        Idempotent — a committed stream already closed + renamed."""
        h = st.handle
        if h is not None:
            with contextlib.suppress(OSError):
                h.close()
            st.handle = None
        with contextlib.suppress(OSError):
            if st.partial_path.exists():
                st.partial_path.unlink()

    async def _apply_file_changed(self, machine_id: str, msg: dict) -> None:
        """Apply a satellite-side file change to the platform's agent_dir, then
        fan it out to every OTHER satellite running the same agent.

        - Small files (≤ 1 MB) carry ``content_b64`` inline → write directly.
        - Large files (> 1 MB) carry only ``size`` + ``hash`` → pull explicitly,
          then write.
        - Deletes have no body — apply atomically.

        Idempotent if the platform already has the same hash. Errors are
        logged and swallowed — file sync is best-effort during a turn; if
        it fails the user will see stale state until the next session start.

        After applying an AUTHORIZED write/delete, the change is fanned out to
        every OTHER active satellite of the same agent (``workspace_fanout``,
        isolation-filtered) and, for writes that overwrite a different KNOWN
        user's recent edit, a conflict is logged + the overwritten author
        notified. Apply + conflict-detect + fan-out all run UNDER the global
        per-(agent, rel_path) lock so concurrent same-file writers converge on a
        consistent byte sequence (last writer wins, never a torn interleave).
        """
        from core.remote import file_sync as core_file_sync
        import config as _cfg

        agent_slug = msg.get("agent_slug", "")
        rel_path = msg.get("path", "")
        action = msg.get("action", "")
        if not agent_slug or not rel_path or not action:
            return
        session_id = msg.get("session_id", "")

        # Role-aware write-back guard (SECURITY). The satellite→platform write
        # direction must obey the same per-role write matrix as native tools.
        # Resolve the role PROXY-SIDE from the authenticated session — NEVER
        # trust the satellite's payload for identity. Fail-closed. This is the
        # ONLY filesystem-write gate for Codex remote sessions (Codex has no
        # per-tool permission hooks), so without it a Codex editor/viewer agent
        # could write knowledge/ (or shared workspace/) on the satellite disk
        # and have it sync back to the platform.
        from core.session.session_state import get_session_security
        sec = get_session_security(session_id) if session_id else None
        _role = getattr(sec, "role", "") if sec else ""
        _uname = getattr(sec, "username", "") if sec else ""
        # MOUNT identity for the per-user-dir rule (Shared-only human chats
        # blank it); the REAL username above keeps owner config/knowledge
        # curation working. None (a ctx without the property) falls back to
        # username inside can_write_back.
        _mount = getattr(sec, "mount_username", None) if sec else None
        if sec is None or not core_file_sync.can_write_back(
                rel_path, _role, _uname, mount_username=_mount):
            # Engine-internal machinery paths (.claude/.codex/.credentials)
            # are denied for EVERY role by design, and engines rewrite their
            # own runtime state each turn (codex: models_cache.json) — that
            # denial is routine, not a signal. Keep WARNING for everything
            # else: a missing SecurityContext or a role/scope denial on a
            # normal path is exactly the anomaly this log exists to surface.
            if sec is not None and core_file_sync.is_engine_machinery_path(rel_path):
                logger.debug(
                    "write-back skipped (engine machinery): session=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"), rel_path, action,
                )
            else:
                logger.warning(
                    "write-back denied: session=%s role=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"),
                    (_role or "no-ctx"), rel_path, action,
                )
            return

        # Agent identity is part of "never trust the satellite payload": use the
        # session's AUTHENTICATED agent. A mismatch means a buggy/compromised
        # satellite tried to write into a DIFFERENT agent's tree — reject. This
        # also keeps the lock / fan-out / conflict keys honest, since fan-out
        # targets are selected by agent_slug.
        sec_agent = getattr(sec, "agent", "") or ""
        if sec_agent and sec_agent != agent_slug:
            logger.warning(
                "write-back agent mismatch: session=%s sec_agent=%s payload=%s path=%s",
                (session_id[:8] if session_id else "?"), sec_agent, agent_slug, rel_path,
            )
            return
        agent_dir = _cfg.AGENTS_DIR / agent_slug

        # All platform-side writes to this agent file serialize on the global
        # per-(agent, rel_path) lock — across sessions and machines — so
        # pull_through / push_back / this applier / the fan-out never interleave
        # a torn write. agent_slug is guaranteed non-empty past the guard.
        from core.remote import remote_file_flow
        from services.remote import workspace_fanout
        lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)

        conflict_notify: tuple[str, str] | None = None  # (loser_slug, filename)
        try:
            async with lock:
                from storage import (
                    sync_state_store, file_tombstones_store,
                    file_author_store, recover_bin_store,
                )

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
                    await asyncio.to_thread(
                        file_tombstones_store.record, agent_slug, rel_path,
                        time.time(), origin="live-delete",
                    )
                    if pre_bytes is not None:
                        await asyncio.to_thread(
                            recover_bin_store.capture, agent_slug, rel_path,
                            pre_bytes, "deleted",
                        )
                    await asyncio.to_thread(
                        sync_state_store.clear_one, machine_id, agent_slug, rel_path,
                    )
                    await asyncio.to_thread(
                        file_author_store.clear, agent_slug, rel_path,
                    )
                else:
                    new_hash = msg.get("hash", "") or ""
                    # Clobber check: the platform copy changed since THIS machine's
                    # last-converged base → the satellite is overwriting an edit it
                    # never saw. The satellite wins (the live write applies), so the
                    # platform's prior bytes are the loser → strict capture.
                    base_row = await asyncio.to_thread(
                        sync_state_store.get_one, machine_id, agent_slug, rel_path,
                    )
                    base_hash = base_row[0] if base_row else None
                    if (pre_hash is not None and new_hash
                            and new_hash != pre_hash and pre_hash != base_hash):
                        author = await asyncio.to_thread(
                            file_author_store.get, agent_slug, rel_path,
                        )
                        cap_side, cap_reason, notify_user = core_file_sync._divergence_capture(
                            rel_path, lambda _p: author, _uname, platform_wins=False,
                        )
                        if cap_side == "platform" and pre_bytes is not None:
                            entry = await asyncio.to_thread(
                                recover_bin_store.capture, agent_slug, rel_path,
                                pre_bytes, cap_reason,
                            )
                            if entry is not None and notify_user:
                                conflict_notify = (notify_user, entry["original_name"])
                    # Advance base + author to the satellite's just-applied write.
                    base_mtime = await asyncio.to_thread(self._mtime_of, agent_dir, rel_path)
                    await asyncio.to_thread(
                        sync_state_store.record_one, machine_id, agent_slug,
                        rel_path, new_hash, base_mtime,
                    )
                    await asyncio.to_thread(
                        file_author_store.record, agent_slug, rel_path, _uname,
                    )

                # Fan out the applied change to every OTHER satellite of this
                # agent (isolation-filtered inside). Held under the lock on
                # purpose: serializes same-file writers; gather bounds the hold to
                # ~one push timeout. The handler is create_task'd off the receive
                # loop, so this never blocks message receive.
                if action == "delete":
                    await workspace_fanout.fan_out_delete(
                        agent_slug, rel_path, exclude_machine_id=machine_id,
                    )
                elif workspace_fanout.fanout_targets(
                    agent_slug, rel_path, exclude_machine_id=machine_id,
                ):
                    # Only re-read the file from disk when there's somewhere to
                    # send it (the common single-session case skips the read).
                    content = await self._read_workspace_bytes(agent_dir, rel_path)
                    if content is not None:
                        await workspace_fanout.fan_out_write(
                            agent_slug, rel_path, content,
                            exclude_machine_id=machine_id,
                        )
        except Exception:
            logger.exception(
                "_apply_file_changed: failed for %s/%s", agent_slug, rel_path,
            )
            return

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

    async def _capture_pre_overwrite(
        self, agent_dir, rel_path: str,
    ) -> tuple[bytes | None, str | None]:
        """Read + hash the current on-disk bytes of a workspace file BEFORE it is
        overwritten, for conflict detection — but ONLY if it exists and is
        ≤ ``CONFLICT_BACKUP_MAX_BYTES``. Returns ``(bytes, "sha256:<hex>")`` or
        ``(None, None)``. Never reads a large file on the apply path.
        """
        def _read() -> tuple[bytes | None, str | None]:
            try:
                base = Path(agent_dir).resolve()
                dest = (base / rel_path).resolve()
                dest.relative_to(base)
            except (ValueError, OSError):
                return (None, None)
            try:
                if not dest.is_file() or dest.stat().st_size > CONFLICT_BACKUP_MAX_BYTES:
                    return (None, None)
                data = dest.read_bytes()
            except OSError:
                return (None, None)
            return (data, "sha256:" + hashlib.sha256(data).hexdigest())
        return await asyncio.to_thread(_read)

    async def _read_workspace_bytes(self, agent_dir, rel_path: str) -> bytes | None:
        """Re-read the current platform bytes of a workspace file (post-apply) for
        fan-out. Returns None on missing / unreadable / path-traversal."""
        def _read() -> bytes | None:
            try:
                base = Path(agent_dir).resolve()
                dest = (base / rel_path).resolve()
                dest.relative_to(base)
            except (ValueError, OSError):
                return None
            try:
                return dest.read_bytes()
            except OSError:
                return None
        return await asyncio.to_thread(_read)

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

            loser_sub = await asyncio.to_thread(
                database.get_user_sub_by_username, loser_slug,
            )
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
                timeout=180.0,
            )
            if not ok:
                logger.warning(
                    "file_changed pull failed for %s", msg.get("path"),
                )
