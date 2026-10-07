"""Worker results carry files: the delegation MCP's ``attach_result_files``
backend (PROJECTS.md "Result files").

A delegated worker names files of its own workspace while its run is live.
The proxy resolves the run from the caller's session token alone, computes
the destination from the run (the delegating chat's workspace, by the same
who-wakes rule the delivery uses), copies the files into
``inbox/<worker agent>/`` through ``safe_fs`` (no symlink followed on either
side, never an overwrite), records one row per file on the run, and the
delivery names the rows when the result is handed back. The worker never
reads the other tree and needs no roster edge: the result text already
flows that way.

Copied at attach, not at delivery: the satellite ends a turn before its
file-change scan and most run endings close the session before delivery,
so a copy at delivery would read a remote worker's stale platform bytes.
At attach the session is live and the send_files read-through applies.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

import config
from auth import roles
from core import layout
from core.session.visibility import SCOPE_AGENT, SCOPE_USER
from services.delegation import file_transfer
from services.delegation.file_transfer import SendFilesAuthz, validate_send_path
from services.infra import safe_fs
from services.infra.path_confinement import join_under
from services.scheduler import task_kinds
from services.scheduler.delivery_identity import _wake_visibility, resolve_delivery_person
from storage import database as task_store
from storage.automation import db_result_files, run_status
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.delegation")

# Rows a run may hold across its attach calls (every status counts), three
# calls' worth of the per-call cap; admin-set values ride
# ``mcp_config_values['delegation-mcp']`` like the send_files caps.
DEFAULT_MAX_PER_RUN = 60

NO_RUN = ("This session has no running delegated run: the attachment is not "
          "part of any result. Name the file's path in your reply instead.")
TWO_RUNS = "Two runs are live on this session; attach again in a moment."
NO_CALLBACK = "This run reports to no one; nothing to attach to."

# One lock per run, held by the attaches in flight and the delivery's read;
# the entry leaves when the last holder lets go (a plain "forget when not
# locked" would drop a lock a waiter is still about to take).
_locks: dict[str, tuple[asyncio.Lock, int]] = {}


class AttachRefused(Exception):
    """The attach answers one sentence and copies nothing."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@contextlib.asynccontextmanager
async def run_lock(run_id: str):
    """Two attaches of one run in one turn, and the delivery's read of the
    rows, are serialized: the caps, the refusal replacement and the
    named-once rule check and then write."""
    lock, holders = _locks.get(run_id) or (asyncio.Lock(), 0)
    _locks[run_id] = (lock, holders + 1)
    try:
        async with lock:
            yield
    finally:
        lock, holders = _locks[run_id]
        if holders <= 1:
            _locks.pop(run_id, None)
        else:
            _locks[run_id] = (lock, holders - 1)


def max_per_run() -> int:
    return file_transfer._config_int("RESULT_FILES_MAX_PER_RUN", DEFAULT_MAX_PER_RUN)


def max_per_call() -> int:
    return file_transfer._config_int("SEND_FILES_MAX_FILES", file_transfer.DEFAULT_MAX_FILES)


# ---------------------------------------------------------------------------
# The run and the destination
# ---------------------------------------------------------------------------


def resolve_run(session_id: str, agent: str) -> tuple[dict, dict]:
    """The RUNNING delegate run of the caller's session on its agent and its
    dynamic task row. The run row carries its session id before the turn
    runs, so the session keys it (a chat may hold two RUNNING rows for a
    moment between rounds, and a warmup may rewrite a chat's session).
    Synchronous: call it on the DB executor."""
    runs = [
        r for r in task_store.list_runs(limit=5, session_id=session_id,
                                        status=run_status.RUNNING)
        if r.get("task_type") == task_kinds.DELEGATE and r.get("agent") == agent
    ]
    if not runs:
        raise AttachRefused(409, NO_RUN)
    if len(runs) > 1:
        raise AttachRefused(409, TWO_RUNS)
    run = runs[0]
    task_row = task_store.get_dynamic_task(run.get("task_id") or "")
    # The delivery's own skip rule (``_deliver_task_result``).
    if not task_row or not task_row.get("on_complete_agent") or not task_row.get("on_complete_prompt"):
        raise AttachRefused(409, NO_CALLBACK)
    return run, task_row


@dataclass(frozen=True)
class ResultAuthz:
    """The resolved authorization for one run's attaches."""

    run_id: str
    worker_agent: str
    target_agent: str
    created_by: str
    person: str                 # "" when the result wakes nobody
    src_mount_username: str
    dest_mount_username: str
    source_root: Path           # the worker's workspace on the platform
    dest_workspace: Path        # the delegating chat's workspace
    dest_root: Path             # dest_workspace/inbox/<worker agent>
    dest_scope: str
    owner_sub: str              # the person when the destination is their tree
    same_tree: bool

    def as_send_files_authz(self) -> SendFilesAuthz:
        """What ``prefetch_remote_sources`` reads (the source side)."""
        return SendFilesAuthz(
            created_by=self.created_by, acting_sub=self.person or None,
            source_agent=self.worker_agent, target_agent=self.target_agent,
            source_root=self.source_root, dest_root=self.dest_root,
            dest_scope=self.dest_scope, scope_note="", owner_sub=self.owner_sub,
        )


def _source_mount_username(session_id: str, worker_agent: str, task_row: dict) -> str:
    """The worker's mount: the live security context (authoritative during
    the run: the real creator in ``username``, the mount scope in
    ``session_scope``), else the run identity re-resolved from the task row.
    Synchronous: call it on the DB executor."""
    from core.session.session_state import get_session_security
    ctx = get_session_security(session_id)
    if ctx is not None and ctx.agent == worker_agent:
        return ctx.username if ctx.session_scope == SCOPE_USER else ""
    from core.config.task_config_builder import resolve_task_identity
    scope = task_row.get("scope") if task_row.get("scope") is not None else SCOPE_USER
    return resolve_task_identity(worker_agent, scope, task_row.get("created_by")).username


async def authorize(run: dict, task_row: dict, session_id: str) -> ResultAuthz:
    """The source tree (the worker's mount) and the destination tree (the
    delegating chat's, by the delivery's who-wakes rule and its standing
    gate), and whether they are one tree. Raises ``AttachRefused``."""
    worker_agent = run.get("agent") or ""
    target_agent = task_row.get("on_complete_agent") or ""
    created_by = task_row.get("created_by") or ""
    scope = task_row.get("scope") if task_row.get("scope") is not None else SCOPE_USER
    src_mount = await run_db(_source_mount_username, session_id, worker_agent, task_row)
    identity = await resolve_delivery_person(
        task_row.get("on_complete_chat_id") or None,
        task_row.get("on_complete_session_id") or "",
        created_by, scope, target_agent,
    )
    if identity.refused:
        raise AttachRefused(
            409, f"The person this result reports to no longer holds agent "
                 f"'{target_agent}'; nothing was copied.")
    username = ""
    if identity.person:
        username = await run_db(task_store.get_username_by_sub, identity.person) or ""
        if not username:
            raise AttachRefused(
                409, "The person this result reports to has no username; nothing was copied.")
    vis = await run_db(_wake_visibility, target_agent, username, identity.role,
                       identity.person or None)
    if identity.person and vis.mount_scope == SCOPE_AGENT \
            and not roles.can_write_workspace(identity.standing):
        # The chat's mount is the agent's shared workspace (the agent became
        # Shared-only since the spawn): a drop there is a shared-workspace
        # write, gated at the workspace tier as a send_files drop is.
        raise AttachRefused(
            409, f"The person this result reports to may not write agent "
                 f"'{target_agent}'s shared workspace; nothing was copied.")
    dest_mount = vis.mount_username
    dest_workspace = layout.workspace_dir(config.get_agent_dir(target_agent), dest_mount)
    return ResultAuthz(
        run_id=run["id"], worker_agent=worker_agent, target_agent=target_agent,
        created_by=created_by, person=identity.person,
        src_mount_username=src_mount, dest_mount_username=dest_mount,
        source_root=layout.workspace_dir(config.get_agent_dir(worker_agent), src_mount),
        dest_workspace=dest_workspace,
        dest_root=dest_workspace / "inbox" / worker_agent,
        dest_scope=vis.mount_scope,
        owner_sub=identity.person if vis.mount_scope == SCOPE_USER else "",
        same_tree=(worker_agent, src_mount) == (target_agent, dest_mount),
    )


def _rows_held(run_id: str, paths: list[str]) -> int:
    """The run's rows that stay after a call naming ``paths``: every row but
    the skipped ones at or under a named path, which the call replaces."""
    return (db_result_files.count_for_run(run_id)
            - db_result_files.count_skipped_under(run_id, paths))


def check_run_cap(run_id: str, raw_paths: list[str]) -> None:
    """Before any satellite read: a run at its cap pulls nothing (a retry of
    a refused path still passes, since its row is replaced)."""
    cleaned: list[str] = []
    for raw in raw_paths:
        try:
            cleaned.append(validate_send_path(raw))
        except HTTPException:
            continue
    cap = max_per_run()
    if _rows_held(run_id, cleaned) >= cap:
        raise AttachRefused(413, f"This run has attached its cap of {cap} files.")


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------


@dataclass
class AttachResult:
    landed: list[dict]      # {path, landed_path, ws_path, bytes}
    named: list[dict]       # the same shape, files already in the delegator's tree
    skipped: list[dict]     # {path, reason}
    max_files: int
    max_per_run: int
    attached: int           # rows the run holds after this call


@dataclass
class _Pick:
    path: str        # the file as the worker names it (workspace-relative)
    src_rel: str     # agent-tree-relative on the worker's side
    dest_rel: str    # relative to the inbox root (basename, or dir/…)
    size: int


def _skip(skipped: list[dict], path: str, reason: str) -> None:
    skipped.append({"path": path, "reason": reason})


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _classify(src_fd: int, scope_ws: str, raw_paths: list[str], *, cap: int,
              unavailable: list[str], remote_label: str,
              ) -> tuple[list[_Pick], list[dict], list[str]]:
    """Validate and expand the named paths through ``safe_fs`` (a link
    anywhere on a path is one skipped row carrying the worker's own text,
    never a listing). Returns the picks, the skipped rows and the validated
    paths named."""
    from core.remote.file_sync import is_engine_machinery_path
    picks: list[_Pick] = []
    skipped: list[dict] = []
    named: list[str] = []
    for raw in raw_paths:
        try:
            rel = validate_send_path(raw)
        except HTTPException as e:
            raise AttachRefused(400, str(e.detail)) from None
        named.append(rel)
        if is_engine_machinery_path(rel) or rel.endswith(".partial"):
            _skip(skipped, rel, "engine state is not a deliverable")
            continue
        src_rel = f"{scope_ws}/{rel}"
        try:
            st = safe_fs.lstat_beneath(src_fd, src_rel)
        except safe_fs.SymlinkRefused:
            _skip(skipped, rel, "symlink")
            continue
        except FileNotFoundError:
            if raw in unavailable and remote_label:
                _skip(skipped, rel, "not in your workspace, and the remote machine "
                                    f"{remote_label} could not provide it")
            else:
                _skip(skipped, rel, "not in your workspace")
            continue
        except OSError:
            _skip(skipped, rel, "not in your workspace")
            continue
        if stat.S_ISLNK(st.st_mode):
            _skip(skipped, rel, "symlink")
        elif stat.S_ISREG(st.st_mode):
            picks.append(_Pick(rel, src_rel, Path(rel).name, st.st_size))
        elif stat.S_ISDIR(st.st_mode):
            _walk_dir(src_fd, rel, src_rel, picks, skipped, cap=cap)
        else:
            _skip(skipped, rel, "not a regular file")
        if len(picks) + len(skipped) > cap:
            break  # over the per-call cap: the call is refused whole
    return picks, skipped, named


def _walk_dir(src_fd: int, rel: str, src_rel: str, picks: list[_Pick], skipped: list[dict],
              *, cap: int) -> None:
    """Expand a named directory through ``safe_fs.walk_beneath``: links and
    non-regular entries are skipped rows by name, a subdirectory that cannot
    be entered is one too, engine state and torn files are left out, and the
    walk stops once the per-call cap is passed (the call is refused whole)."""
    from core.remote.file_sync import is_engine_machinery_path
    dir_name = Path(rel).name
    budget = cap + 1 - len(picks) - len(skipped)

    def _unreadable(err: OSError) -> None:
        nonlocal budget
        failed = str(err.filename or "")
        _skip(skipped, _under(rel, src_rel, failed) if failed.startswith(src_rel + "/") else rel,
              "not readable")
        budget -= 1

    try:
        for step in safe_fs.walk_beneath(src_fd, src_rel, onerror=_unreadable):
            for name in step.symlinks:
                _skip(skipped, _under(rel, src_rel, step.path(name)), "symlink")
                budget -= 1
            for name in step.other:
                _skip(skipped, _under(rel, src_rel, step.path(name)), "not a regular file")
                budget -= 1
            for name in step.files:
                entry_src = step.path(name)
                entry = _under(rel, src_rel, entry_src)
                if name.endswith(".partial") or is_engine_machinery_path(entry):
                    continue
                try:
                    fst = safe_fs.lstat_beneath(src_fd, entry_src)
                except OSError:
                    _skip(skipped, entry, "vanished")
                    budget -= 1
                    continue
                picks.append(_Pick(entry, entry_src,
                                   f"{dir_name}/{entry_src[len(src_rel) + 1:]}", fst.st_size))
                budget -= 1
                if budget <= 0:
                    break
            if budget <= 0:
                break
    except safe_fs.SymlinkRefused:
        _skip(skipped, rel, "symlink")
    except OSError:
        _skip(skipped, rel, "not readable")


def _under(rel: str, src_rel: str, entry_src: str) -> str:
    """The worker-relative spelling of an entry found under a directory."""
    return f"{rel}/{entry_src[len(src_rel) + 1:]}"


def attach(authz: ResultAuthz, *, paths: list[str], dest_dir: str = "",
           unavailable: list[str] | None = None, remote_label: str = "") -> AttachResult:
    """Classify and expand ``paths``, apply the caps, copy per file best
    effort (or name them in the same tree), record the rows. Synchronous:
    call it in a worker thread, under ``run_lock``."""
    cap = max_per_call()
    per_run = max_per_run()
    size_cap = config.MAX_UPLOAD_SIZE_BYTES
    cap_mb = size_cap // (1024 * 1024)
    agent_dir = config.get_agent_dir(authz.target_agent)
    dest_root = authz.dest_root
    if dest_dir:
        err = file_transfer.validate_rel_dir(dest_dir)
        if err:
            raise AttachRefused(400, f"Invalid dest_dir '{dest_dir}': {err}")
        clean = file_transfer._clean_rel(dest_dir)
        if clean:
            dest_root = join_under(dest_root, clean)
    scope_ws = layout.scope_workspace(authz.src_mount_username)
    try:
        src_root_cm = safe_fs.open_root(config.AGENTS_DIR, authz.worker_agent)
        src_fd = src_root_cm.__enter__()
    except OSError:
        raise AttachRefused(409, "The worker's tree could not be opened; nothing was copied.") from None
    try:
        picks, skipped, named_paths = _classify(
            src_fd, scope_ws, paths, cap=cap, unavailable=unavailable or [],
            remote_label=remote_label)
        total = len(picks) + len(skipped)
        if total > cap:
            raise AttachRefused(
                413, f"Too many files ({total}, max {cap} per call): split the "
                     "attach or send an archive.")
        existing = _rows_held(authz.run_id, named_paths)
        if existing + total > per_run:
            raise AttachRefused(
                413, f"This run would hold {existing + total} attached files; the cap "
                     f"is {per_run} per run.")
        landed: list[dict] = []
        named: list[dict] = []
        if authz.same_tree:
            already = db_result_files.named_paths(authz.run_id)
            for pick in picks:
                try:
                    fd, st = safe_fs.open_regular_for_read(src_fd, pick.src_rel)
                except safe_fs.SafeFsError:
                    _skip(skipped, pick.path, "symlink")
                    continue
                except OSError:
                    _skip(skipped, pick.path, "vanished")
                    continue
                os.close(fd)
                if pick.src_rel in already:
                    continue
                already.add(pick.src_rel)
                named.append({"path": pick.path, "landed_path": pick.src_rel,
                              "ws_path": pick.path, "bytes": st.st_size})
        elif picks:
            try:
                dst_root_cm = safe_fs.open_root(config.AGENTS_DIR, authz.target_agent)
                dst_fd = dst_root_cm.__enter__()
            except OSError:
                raise AttachRefused(
                    409, "The delegating agent's tree could not be opened; nothing was copied.",
                ) from None
            try:
                inbox_rel = dest_root.relative_to(agent_dir).as_posix()
                ws_prefix = dest_root.relative_to(authz.dest_workspace).as_posix()
                _copy_all(src_fd, dst_fd, picks, inbox_rel, ws_prefix, landed, skipped,
                          size_cap=size_cap, cap_mb=cap_mb)
            finally:
                dst_root_cm.__exit__(None, None, None)
        db_result_files.drop_skipped_under(authz.run_id, named_paths)
        rows = (
            [{**r, "status": db_result_files.LANDED} for r in landed]
            + [{**r, "status": db_result_files.NAMED} for r in named]
            + [{"path": s["path"], "status": db_result_files.SKIPPED, "reason": s["reason"]}
               for s in skipped]
        )
        db_result_files.record(authz.run_id, authz.target_agent, rows)
        attached = db_result_files.count_for_run(authz.run_id)
    finally:
        src_root_cm.__exit__(None, None, None)
    logger.info(
        "result files: run=%s %s→%s landed=%d named=%d skipped=%d same_tree=%s dest=%s",
        authz.run_id, authz.worker_agent, authz.target_agent, len(landed), len(named),
        len(skipped), authz.same_tree, dest_root.relative_to(agent_dir).as_posix(),
    )
    return AttachResult(landed=landed, named=named, skipped=skipped, max_files=cap,
                        max_per_run=per_run, attached=attached)


def _copy_all(src_fd: int, dst_fd: int, picks: list[_Pick], inbox_rel: str, ws_prefix: str,
              landed: list[dict], skipped: list[dict], *, size_cap: int, cap_mb: int) -> None:
    """One copy per pick, best effort: the free name is chosen first and
    written exclusively; a full bucket ends the batch."""
    out_of_room = False
    for pick in picks:
        if out_of_room:
            _skip(skipped, pick.path, "no room in the target's bucket")
            continue
        parent_rel, ws_dir, name = inbox_rel, ws_prefix, pick.dest_rel
        if "/" in pick.dest_rel:
            sub, name = pick.dest_rel.rsplit("/", 1)
            parent_rel, ws_dir = f"{inbox_rel}/{sub}", f"{ws_prefix}/{sub}"
        try:
            safe_fs.mkdirs_beneath(dst_fd, parent_rel)
        except OSError:
            _skip(skipped, pick.path, "the destination folder could not be made")
            continue
        copied: int | None = None
        for _attempt in range(4):
            try:
                free = file_transfer.free_name(dst_fd, parent_rel, name)
                if free is None:
                    _skip(skipped, pick.path, "too many files of that name")
                    break
                dst_rel = f"{parent_rel}/{free}"
                copied = safe_fs.copy_file_beneath(
                    src_fd, pick.src_rel, dst_fd, dst_rel, max_size=size_cap, exclusive=True)
            except FileExistsError:
                continue  # a race on the name: the next free one
            except safe_fs.FileTooLarge:
                _skip(skipped, pick.path, f"over the {cap_mb} MB cap")
            except safe_fs.NotRegularFile:
                _skip(skipped, pick.path, "not a regular file")
            except (safe_fs.SymlinkRefused, safe_fs.EscapeRefused) as e:
                _skip(skipped, pick.path, "the destination path is a link"
                      if str(e.filename or "").startswith(inbox_rel) else "symlink")
            except FileNotFoundError:
                _skip(skipped, pick.path, "vanished")
            except OSError as e:
                if e.errno in (errno.EDQUOT, errno.ENOSPC):
                    out_of_room = True
                    _skip(skipped, pick.path, "no room in the target's bucket")
                else:
                    logger.error("result files copy failed: %s → %s: %s", pick.src_rel, dst_rel, e)
                    _skip(skipped, pick.path, "copy failed")
            else:
                landed.append({"path": pick.path, "landed_path": dst_rel,
                               "ws_path": f"{ws_dir}/{free}", "bytes": copied})
            break
        else:
            _skip(skipped, pick.path, "too many files of that name")


# ---------------------------------------------------------------------------
# The delivery
# ---------------------------------------------------------------------------


def delivery_lists(run_id: str) -> tuple[list[dict], list[dict], str]:
    """What the result names: ``files`` (the landed and named rows as
    ``{path, bytes}``, agent-tree-relative), ``files_skipped`` (``{path,
    reason}``) and the bracketed note for the callback prompt (the paths
    relative to the delegating chat's workspace). Empty when the run holds
    no rows. Synchronous: call it on the DB executor."""
    if not run_id:
        return [], [], ""
    rows = db_result_files.list_for_run(run_id)
    files = [{"path": r["landed_path"], "bytes": int(r["bytes"] or 0)}
             for r in rows if r["status"] in (db_result_files.LANDED, db_result_files.NAMED)]
    skipped = [{"path": r["path"], "reason": r["reason"]}
               for r in rows if r["status"] == db_result_files.SKIPPED]
    if not files and not skipped:
        return [], [], ""
    parts: list[str] = []
    if files:
        where = ("already in your workspace"
                 if all(r["status"] == db_result_files.NAMED for r in rows
                        if r["status"] != db_result_files.SKIPPED)
                 else "relative to your workspace")
        items = ", ".join(f"{r['ws_path']} ({_size_text(int(r['bytes'] or 0))})"
                          for r in rows if r["status"] != db_result_files.SKIPPED)
        parts.append(f"Files the worker attached, {where}: {items}.")
    if skipped:
        parts.append("Not attached: " + ", ".join(
            f"{s['path']} ({s['reason']})" for s in skipped) + ".")
    return files, skipped, "[" + " ".join(parts) + "]"
