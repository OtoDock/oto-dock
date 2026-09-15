"""Cross-agent file transfer — the delegation-mcp ``send_files`` backend.

Sandboxes cannot see each other by design: the platform tree is the
authority on both sides, and this module is the ONLY cross-agent file
path. The gates mirror ``spawn_authz.authorize_spawn`` step for step
(same order, same error voice) with the roster edge as the consent — no
per-transfer approval. The copy mechanics mirror ``api/media/uploads.py``:
containment on resolved paths, per-file cap ``config.MAX_UPLOAD_SIZE_BYTES``,
``.partial`` + ``os.replace`` atomicity (``.partial`` is sync/fan-out
invisible by construction), conflict renames — mailbox semantics, an
existing inbox file is never overwritten.

send_files is the PASSIVE half of the delegation pair: it never spawns a
turn on the target (autonomy stays with explicit ``delegate``); the
target hears about the drop from the file-inbox context block at its
next session start (``storage/files/db_file_transfers``).

Remote sources (2026-09-05): satellite → platform workspace sync runs at
turn boundaries, so for a session executing on a remote machine the
endpoint calls ``prefetch_remote_sources`` first — every requested path is
read through from the satellite (``remote_file_flow.pull_through``, the
display/file hooks' path) so a file written or modified in the SAME turn
is copied with its current bytes. Local sessions never enter that step.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

import config
from auth.providers import UserContext
from core.session.visibility import available_scopes_for
from storage.agents import agent_store
from storage.files import db_file_transfers
from storage.mcp import mcp_store
from storage import remote_store
from storage import database as task_store
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.delegation")

# Per-call and per-creator ceilings. Admin-set values ride
# ``mcp_config_values['delegation-mcp']`` (manifest declares the same
# defaults so the admin config editor shows them).
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_PER_DAY = 50
_NOTE_CAP = 500


def _config_int(key: str, default: int) -> int:
    raw = mcp_store.get_mcp_config_values("delegation-mcp").get(key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def validate_rel_dir(value: str) -> str | None:
    """Safe relative directory (dest_dir) — same rules as the MCP client's
    output_dir check. Returns an error message or None."""
    if value.startswith(("/", "\\")):
        return "must be a relative path"
    parts = Path(value).parts
    if ".." in parts:
        return "must not contain '..'"
    if any(p.startswith(".") for p in parts):
        return "must not contain hidden directories"
    return None


class MissingSourcePath(HTTPException):
    """404 for a requested path that is neither a file nor a directory in
    the platform copy of the caller's tree. Same status + detail every
    caller always got; ``raw`` lets the endpoint re-word it for a remote
    session whose satellite could not provide the path either."""

    def __init__(self, raw: str):
        super().__init__(
            status_code=404,
            detail=f"No such file or directory in your workspace: '{raw}'",
        )
        self.raw = raw


def validate_send_path(raw: str) -> str:
    """Normalize one ``paths`` entry to a workspace-relative posix path, or
    raise the 400s ``perform_send_files`` always raised (empty, absolute,
    ``..``). Shared with the remote prefetch so nothing the copy would
    refuse ever reaches ``pull_through`` (which mkdirs the parent chain)."""
    rel = (raw or "").strip().strip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="Empty path in `paths`.")
    if raw.strip().startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid path '{raw}': paths are relative to your "
                   "workspace and must not contain '..'.",
        )
    # Path() collapses `./` and doubled slashes — the form `src_root / rel`
    # resolves to anyway, and the form the satellite manifest reports.
    return Path(rel).as_posix()


@dataclass
class SendFilesAuthz:
    """The resolved authorization for one cross-agent file transfer."""

    created_by: str          # attribution: real user sub, or the source agent slug
    acting_sub: str | None   # the real user; None for service callers
    source_agent: str        # token-authoritative sending agent
    target_agent: str
    source_root: Path        # the caller's own readable tree (platform side)
    dest_root: Path          # target inbox root: …/workspace/inbox/<source>/
    dest_scope: str          # final scope after clamping to the target's mode
    scope_note: str          # non-empty when the requested scope was clamped
    owner_sub: str           # dest-tree owner sub ('' for agent scope)


def authorize_send_files(
    user: UserContext,
    *,
    target_agent: str,
    requested_scope: str,
    source_agent: str | None = None,
    x_agent_name: str | None = None,
) -> SendFilesAuthz:
    """Authorize one transfer; raises HTTPException on denial.

    Mirrors ``spawn_authz.authorize_spawn``: kill-switch first, then
    source/roster/access, then scope clamping to the target's mode, then
    identity + role for the FINAL scope, and the per-creator quota last —
    all before any file is touched.
    """
    # 1. Platform kill-switch (same row as spawn).
    state = mcp_store.get_mcp_state("delegation-mcp")
    if not state or not state.get("enabled"):
        raise HTTPException(
            status_code=403,
            detail="Delegation is disabled on this platform (delegation-mcp is turned off).",
        )

    if requested_scope not in ("user", "agent"):
        raise HTTPException(status_code=400, detail=f"Invalid scope: {requested_scope!r}")

    target_row = agent_store.get_agent(target_agent)
    if not target_row:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {target_agent}")

    # 2. Sending agent — token-authoritative. Files move between agent
    #    trees, so a caller with no agent identity has no source tree.
    source = user.agent or x_agent_name or source_agent or ""
    if not source:
        raise HTTPException(
            status_code=400,
            detail="send_files needs an agent session — a dashboard or "
                   "service caller has no source workspace.",
        )
    if not config.is_safe_agent_name(source) or not agent_store.get_agent(source):
        raise HTTPException(status_code=404, detail=f"Unknown source agent: {source}")
    # When the source claim did NOT come from the session token (dashboard
    # cookie or service caller passing a header/body value), the source tree
    # it selects for reading must be one the caller can access themselves —
    # otherwise any user could exfiltrate an arbitrary agent's workspace by
    # naming it as `source_agent` (the roster edge only proves the AGENTS
    # may exchange files, not that this USER may read the source).
    if not user.agent and user.acting_sub is not None \
            and not user.can_access_agent(source):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have access to source agent '{source}'.",
        )
    if source == target_agent:
        raise HTTPException(
            status_code=400,
            detail=f"'{target_agent}' is the calling agent — the files are "
                   "already in your own tree.",
        )
    allowed = agent_store.get_delegation_targets(source)
    if target_agent not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{source}' cannot send files to '{target_agent}' — "
                   "not in its delegation targets.",
        )
    # A real user must additionally have access to the target agent; service
    # callers are covered by the roster (agent-to-agent policy).
    acting = user.acting_sub
    if acting is not None and not user.can_access_agent(target_agent):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have access to agent '{target_agent}'.",
        )

    # Source tree = what the calling session can already read itself
    # (its own mount) — the proxy copy never exceeds the caller's reach.
    src_username = ""
    if requested_scope == "user":
        if acting is None:
            raise HTTPException(
                status_code=400,
                detail="User-scoped send_files needs a user-backed session.",
            )
        src_username = task_store.get_username_by_sub(acting) or ""
        if not src_username:
            raise HTTPException(status_code=400, detail="User has no username configured")
    src_agent_dir = config.get_agent_dir(source)
    source_root = (
        src_agent_dir / "users" / src_username / "workspace"
        if requested_scope == "user" else src_agent_dir / "workspace"
    )

    # 3. Clamp the destination scope to what the target's mode offers
    #    (spawn parity: silent clamp with a note beats a wasted turn).
    avail = available_scopes_for(
        bool(target_row.get("collaborative", True)),
        target_row.get("default_scope") or "user",
    )
    dest_scope, scope_note = requested_scope, ""
    if requested_scope not in avail:
        clamped = avail[0]
        if clamped == "user" and acting is None:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{target_agent}' only offers user-scoped sessions "
                       "and this caller has no user identity.",
            )
        dest_scope = clamped
        scope_note = (
            f"Note: scope '{requested_scope}' is not offered by "
            f"'{target_agent}' — the files landed in its '{dest_scope}' tree."
        )

    # 4. Identity for the FINAL scope.
    created_by = acting if acting is not None else source

    # 5. Shared-workspace drops from a real user are gated at the editor
    #    tier — the same bar as agent-scoped workers (viewers are read-only).
    if dest_scope == "agent" and acting is not None and not user.can_edit_agent(target_agent):
        raise HTTPException(
            status_code=403,
            detail="Sending into the shared workspace requires editor, "
                   "manager, or admin role for the target agent.",
        )

    # 6. Destination tree owner. User scope lands in the SAME user's tree
    #    on the target (reads follow the user; the write stays inbox-only).
    owner_sub = ""
    dest_username = ""
    if dest_scope == "user":
        owner_sub = acting or ""
        dest_username = task_store.get_username_by_sub(acting or "") or ""
        if not dest_username:
            raise HTTPException(status_code=400, detail="User has no username configured")
    tgt_agent_dir = config.get_agent_dir(target_agent)
    dest_base = (
        tgt_agent_dir / "users" / dest_username / "workspace"
        if dest_scope == "user" else tgt_agent_dir / "workspace"
    )

    # 7. Per-creator quota — policy "no" before any file is touched.
    per_day = _config_int("SEND_FILES_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)
    recent = db_file_transfers.count_recent_by_creator(created_by, hours=24)
    if recent + 1 > per_day:
        raise HTTPException(
            status_code=403,
            detail=f"send_files limit reached: {recent} transfer(s) in the "
                   f"last 24h (max {per_day}). Ask an admin to raise "
                   "SEND_FILES_MAX_PER_DAY for delegation-mcp.",
        )

    return SendFilesAuthz(
        created_by=created_by,
        acting_sub=acting,
        source_agent=source,
        target_agent=target_agent,
        source_root=source_root,
        dest_root=dest_base / "inbox" / source,
        dest_scope=dest_scope,
        scope_note=scope_note,
        owner_sub=owner_sub,
    )


@dataclass
class TransferResult:
    transfer_id: str
    landed: list[str]        # target-agent-relative rel paths (fan-out keys)
    total_bytes: int
    skipped: list[str]       # symlinks etc. — reported, never silent


def _resolve_conflict(target: Path) -> Path:
    """Append _1, _2, … if the target exists (mailbox: never overwrite)."""
    if not target.exists():
        return target
    stem, ext, parent = target.stem, target.suffix, target.parent
    for i in range(1, 100):
        candidate = parent / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail=f"Too many inbox files named '{target.name}'.")


def perform_send_files(
    authz: SendFilesAuthz,
    *,
    paths: list[str],
    dest_dir: str = "",
    note: str = "",
) -> TransferResult:
    """Validate + expand ``paths``, copy into the target inbox, record the
    transfer row. Synchronous (call via ``asyncio.to_thread``); the caller
    schedules satellite fan-out for ``result.landed`` afterwards."""
    max_files = _config_int("SEND_FILES_MAX_FILES", DEFAULT_MAX_FILES)
    size_cap = config.MAX_UPLOAD_SIZE_BYTES
    cap_mb = size_cap // (1024 * 1024)

    src_root = authz.source_root.resolve()
    if not src_root.is_dir():
        raise HTTPException(
            status_code=404,
            detail="Your workspace directory does not exist yet — nothing to send.",
        )

    dest_base = authz.dest_root
    clean_dest_dir = ""
    if dest_dir:
        err = validate_rel_dir(dest_dir)
        if err:
            raise HTTPException(status_code=400, detail=f"Invalid dest_dir '{dest_dir}': {err}")
        clean_dest_dir = str(Path(dest_dir))
        dest_base = dest_base / clean_dest_dir

    def _contained(p: Path) -> Path | None:
        """The resolved path when it stays inside the source tree, else None."""
        try:
            resolved = p.resolve()
        except OSError:
            return None
        if resolved == src_root or str(resolved).startswith(str(src_root) + os.sep):
            return resolved
        return None

    # Expand paths → (resolved source file, dest path relative to dest_base).
    picked: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    for raw in paths:
        rel = validate_send_path(raw)
        src = src_root / rel
        if src.is_symlink():
            skipped.append(f"{rel} (symlink)")
            continue
        if src.is_dir():
            # Directories copy recursively, structure preserved under the
            # directory's own name. Symlinks and torn `.partial` files skip.
            for f in sorted(src.rglob("*")):
                if f.is_symlink():
                    skipped.append(f"{f.relative_to(src_root)} (symlink)")
                    continue
                if not f.is_file() or f.name.endswith(".partial"):
                    continue
                resolved = _contained(f)
                if resolved is None:
                    skipped.append(f"{f.relative_to(src_root)} (outside workspace)")
                    continue
                picked.append((resolved, Path(src.name) / f.relative_to(src)))
        elif src.is_file():
            resolved = _contained(src)
            if resolved is None:
                raise HTTPException(status_code=400, detail=f"Path '{raw}' escapes your workspace.")
            picked.append((resolved, Path(src.name)))
        else:
            raise MissingSourcePath(raw)
        if len(picked) > max_files:
            raise HTTPException(
                status_code=413,
                detail=f"Too many files (max {max_files} per call). Send an "
                       "archive, or split the transfer.",
            )

    if not picked:
        detail = "Nothing to send."
        if skipped:
            detail = "Nothing to send — skipped: " + ", ".join(skipped[:10])
        raise HTTPException(status_code=400, detail=detail)

    total_bytes = 0
    for src, _ in picked:
        st = src.stat()
        if st.st_size > size_cap:
            raise HTTPException(
                status_code=413,
                detail=f"'{src.name}' is {st.st_size // (1024 * 1024)} MB — "
                       f"the per-file cap is {cap_mb} MB.",
            )
        total_bytes += st.st_size

    # Copy: `.partial` + fsync + atomic rename per file. A quota/disk stop
    # mid-batch leaves the already-landed files in place (mailbox — partial
    # delivery is real delivery) and says exactly where it stopped.
    tgt_agent_dir = config.get_agent_dir(authz.target_agent).resolve()
    # The containment anchor must NOT itself resolve through workspace-level
    # symlinks (resolving authz.dest_root would follow a planted link and
    # move the goalposts with it): rebuild it from the RESOLVED agent root —
    # platform-owned, outside the sandbox's write reach — plus the literal
    # scope path.
    dest_root_resolved = tgt_agent_dir / authz.dest_root.relative_to(
        config.get_agent_dir(authz.target_agent),
    )
    landed: list[str] = []
    for src, rel_dest in picked:
        dest = _resolve_conflict(dest_base / rel_dest)
        tmp = dest.with_name(dest.name + ".partial")
        try:
            # Both trees are written directly by sandboxed sessions (their
            # own bind mounts), so a path component may have become a
            # symlink since authorization. Re-anchor the write on the
            # RESOLVED parent and require it inside the transfer root —
            # the same resolved-containment bar as api/media/uploads.py —
            # so a symlinked inbox can't redirect this proxy-privileged
            # copy outside the target workspace. Resolved BEFORE mkdir
            # (resolve() handles the non-existent tail lexically), so not
            # even empty directories land outside.
            real_parent = dest.parent.resolve()
            if real_parent != dest_root_resolved and \
                    not real_parent.is_relative_to(dest_root_resolved):
                raise HTTPException(
                    status_code=400,
                    detail="Destination path escapes the target workspace "
                           "(symlinked component) — transfer refused.",
                )
            dest = real_parent / dest.name
            tmp = dest.with_name(dest.name + ".partial")
            real_parent.mkdir(parents=True, exist_ok=True)
            # Mirror-image guard for the source: open without following a
            # final-component symlink, then verify the file actually opened
            # still lives inside the sender's workspace (a directory
            # component swapped mid-race would otherwise leak whatever the
            # proxy can read into the target inbox).
            src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
            with open(src_fd, "rb") as fin, open(tmp, "wb") as fout:
                real_src = Path(os.path.realpath(f"/proc/self/fd/{fin.fileno()}"))
                if real_src != src_root and \
                        not real_src.is_relative_to(src_root):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Path '{rel_dest}' escapes your workspace.",
                    )
                shutil.copyfileobj(fin, fout, 1024 * 1024)
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(tmp, dest)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno in (errno.EDQUOT, errno.ENOSPC):
                raise HTTPException(
                    status_code=507,
                    detail=f"Not enough storage in '{authz.target_agent}'s "
                           f"{authz.dest_scope} bucket — stopped after "
                           f"{len(landed)} of {len(picked)} file(s).",
                )
            logger.error("send_files copy failed: %s → %s: %s", src, dest, e)
            raise HTTPException(status_code=500, detail="File copy failed.")
        landed.append(str(dest.relative_to(tgt_agent_dir)))

    transfer_id = db_file_transfers.record_transfer(
        source_agent=authz.source_agent,
        target_agent=authz.target_agent,
        scope=authz.dest_scope,
        owner_sub=authz.owner_sub,
        dest_dir=clean_dest_dir,
        file_count=len(landed),
        total_bytes=total_bytes,
        note=(note or "").strip()[:_NOTE_CAP],
        created_by=authz.created_by,
    )
    logger.info(
        f"send_files: transfer={transfer_id} {authz.source_agent}→"
        f"{authz.target_agent} scope={authz.dest_scope} files={len(landed)} "
        f"bytes={total_bytes} by={authz.created_by} "
        f"dest=inbox/{authz.source_agent}/{clean_dest_dir or ''} "
        f"skipped={len(skipped)}"
    )
    return TransferResult(
        transfer_id=transfer_id,
        landed=landed,
        total_bytes=total_bytes,
        skipped=skipped,
    )


# ───────────────────────────────────────────────────────────────────────────
# Remote sources — read-through before the copy (2026-09-05)
# ───────────────────────────────────────────────────────────────────────────


async def prefetch_remote_sources(
    session_id: str,
    authz: SendFilesAuthz,
    paths: list[str],
    *,
    max_files: int | None = None,
) -> list[str]:
    """Bring the requested paths up to date from the caller's satellite
    BEFORE ``perform_send_files`` reads the platform tree.

    Satellite → platform workspace sync runs at turn boundaries, so a file
    written on the remote machine and sent in the same turn is absent (404)
    or stale (silently old bytes) platform-side until the turn ends. For a
    remote session every path is read through ``remote_file_flow``:

    - a cheap ``file_stat`` probe first (0.5.95+; ``exists`` False = absent
      OR a directory) so a typo never mkdirs a junk chain and a directory
      never costs a failed pull; then
    - ``pull_through`` — the display/file hooks' path: revalidation fast
      path, pull under the per-(agent, path) write lock, platform-mirror
      fallback. Called for EVERY existing file (a copy that arrived via the
      turn-end sync has no pull-stat record, so the current bytes are
      pulled); and
    - for a directory (or a path the probe does not know as a file): ONE
      ``request_manifest`` per call, the entries under it pulled one by
      one, stopping after ``max_files + 1`` pulls across the call — past
      that ``perform`` 413s anyway.

    Returns the raw paths that resolved to nothing on either side (for the
    endpoint's remote-aware 404 — ``perform`` still decides). Local sessions
    return at once; invalid paths are left for ``perform``'s 400 and never
    reach the satellite; every probe / pull / manifest failure degrades to
    today's platform-tree behaviour. Deletions inside a directory this turn
    still ride the turn-end scan (the manifest drives pulls, not deletes).
    """
    from core.remote import remote_file_flow

    if not session_id or not remote_file_flow.is_remote_session(session_id):
        return []
    try:
        prefix = authz.source_root.relative_to(
            config.get_agent_dir(authz.source_agent),
        ).as_posix()
    except ValueError:
        return []

    unavailable: list[str] = []
    manifest: list[str] | None = None
    manifest_failed = False
    budget: int | None = None
    pulled: list[str] = []

    async def _pull(remote_rel: str) -> Path | None:
        try:
            got = await remote_file_flow.pull_through(session_id, remote_rel)
            if got is not None:
                pulled.append(remote_rel)
            return got
        except Exception:
            logger.warning(
                "send_files prefetch: pull failed for %s", remote_rel, exc_info=True,
            )
            return None

    async def _probe(remote_rel: str) -> dict | None:
        try:
            return await remote_file_flow.stat_probe(session_id, remote_rel)
        except Exception:
            logger.warning(
                "send_files prefetch: probe failed for %s", remote_rel, exc_info=True,
            )
            return None

    async def _files_under(remote_rel: str) -> list[str] | None:
        nonlocal manifest, manifest_failed, budget
        if manifest is None and not manifest_failed:
            try:
                manifest = await remote_file_flow.list_remote_files(session_id, prefix)
            except Exception:
                logger.warning("send_files prefetch: manifest failed", exc_info=True)
                manifest = None
            if manifest is None:
                manifest_failed = True
            if budget is None:
                cap = max_files
                if cap is None:
                    cap = await asyncio.to_thread(
                        _config_int, "SEND_FILES_MAX_FILES", DEFAULT_MAX_FILES,
                    )
                budget = cap + 1
        if manifest is None:
            return None
        want = remote_rel.rstrip("/") + "/"
        return [p for p in manifest if p.startswith(want)]

    for raw in paths:
        try:
            rel = validate_send_path(raw)
        except HTTPException:
            continue  # perform raises the same 400 — nothing reaches the satellite
        remote_rel = prefix if rel == "." else f"{prefix}/{rel}"
        # A platform-side directory can't be a satellite file — straight to
        # the manifest. Anything else: probe, then pull when it is (or may
        # be) a file over there.
        if not (authz.source_root / rel).is_dir():
            probe = await _probe(remote_rel)
            if probe is None or probe.get("exists"):
                if await _pull(remote_rel) is not None:
                    continue
        under = await _files_under(remote_rel)
        if not under:
            # Nothing the satellite could name — a directory the platform
            # still holds copies as today; a path absent on both sides
            # gets the remote-aware 404 from the endpoint.
            unavailable.append(raw)
            continue
        for entry in under:
            if budget is not None and budget <= 0:
                break
            if budget is not None:
                budget -= 1
            await _pull(entry)
    logger.info(
        "send_files prefetch: session=%s agent=%s paths=%d read through=%d "
        "manifest=%s unavailable=%s",
        session_id[:8], authz.source_agent, len(paths), len(pulled),
        "failed" if manifest_failed else ("yes" if manifest is not None else "no"),
        unavailable or "-",
    )
    return unavailable


async def remote_source_label(session_id: str) -> str:
    """Human label of the remote machine behind ``session_id`` for error
    text: the live connection's cached name, else the machine row (read off
    the loop), else the id prefix."""
    from core.remote import remote_file_flow
    machine_id = remote_file_flow.remote_machine_id(session_id)
    if not machine_id:
        return "for this session"
    name = ""
    try:
        from core.remote.satellite_connection import get_connection_manager
        conn = get_connection_manager().get_connection(machine_id)
        name = getattr(conn, "name", "") if conn is not None else ""
    except Exception:
        name = ""
    if not name:
        try:
            row = await run_db(remote_store.get_remote_machine, machine_id)
        except Exception:
            row = None
        name = (row or {}).get("name") or ""
    return f"'{name}'" if name else machine_id[:8]
