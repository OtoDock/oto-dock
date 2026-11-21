"""Post-tool relocation for MCPs that write to shared output directories.

Problem: some MCPs (camoufox's Playwright, future similar) write files to a
shared per-MCP output directory. For local sandboxed agents that dir is
bwrap-mounted into every sandbox — so any agent could see every other agent's
outputs (privacy) and the dir accumulates forever.

Solution: declare `outputs` in the MCP's manifest:
    {
      "source": "${mcp_dir}/screenshots",
      "destination_template": "${workspace_dir}/.screenshots",
      "after_tools": ["*"],
      "keep_recent": 15
    }

After each tool call from an MCP with `outputs`:
1. Determine which file(s) the tool just produced — **precisely**, by parsing
   the tool's result text for the basename(s) it reported (move-by-filename).
   This isolates concurrent multi-user sessions (each moves only its own file)
   and leaves no orphans. If the result text isn't available (e.g. the Codex
   app-server / interactive paths run no PostToolUse hook), fall back to the
   `mtime > tool_start` scan of the source dir — no worse than before.
2. Move them into the per-session **workspace** under `destination_template`
   (a HIDDEN `.screenshots/` — kept out of the platform↔satellite file-sync so
   the `keep_recent` cap never gets resurrected).
3. Local (T1): the file lands in the bwrap-mounted workspace; the agent reads it
   at `<workspace>/.screenshots/<name>`. Containerised (T2): the source is a
   named volume the proxy can't see, so the file is pulled out with `docker cp`.
   Remote: the moved file is also pushed to the satellite workspace.
4. `keep_recent` trims the dest dir to the newest N (ephemeral browser captures
   are already base64-inlined into chat history on display, so the on-disk copy
   is a convenience).

The tool's container writes the source and the session writes the
destination, so every read, write and removal here goes through
``services.infra.safe_fs``: the destination lies inside the session's
workspace and is reached from ``AGENTS_DIR`` with no link at any part, the
source inside the tool's own folder; only regular files are taken, a copy
never lands a link, and the container's archive is read by the server,
bounded in bytes and time.
"""

from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, BinaryIO, cast

from core import layout
from services.infra import safe_fs

if TYPE_CHECKING:
    from core.placement import PlacementCapabilities

logger = logging.getLogger("otodock.mcp-output-relocation")

# A media basename the tool reported producing, in the tool's own form
# `<source folder>/<name>` (camoufox's
# `screenshots/page-2026-06-27T05-11-40-812Z.png`). The character class excludes
# `/`, so a result string can never steer this into a path traversal — the match
# is always a bare basename.
_OUTPUT_NAME = r"([\w.:\-]+\.(?:png|jpe?g|webp|gif))"
# A result's text can carry a page's own words: a named file counts only when
# it was written since the call started, less the file clock's coarse step.
_NAMED_SLACK_S = 1.0
# The most one copy out of a tool may carry (a container's archive, or one
# file of the tool's folder), and how long the container's copy may run.
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_DOCKER_CP_TIMEOUT_S = 60.0
_CHUNK = 1024 * 1024


@dataclass
class _SessionToolStart:
    """Per-(session, mcp) tool-call start times — the mtime cutoff the
    move-by-filename FALLBACK uses to tell new files from pre-existing ones."""
    started_at: dict[str, float] = field(default_factory=dict)


_state: dict[str, _SessionToolStart] = {}


def record_tool_start(session_id: str, mcp_name: str) -> None:
    """Mark when a tool call from an MCP started (mtime cutoff for the fallback)."""
    st = _state.setdefault(session_id, _SessionToolStart())
    st.started_at[mcp_name] = time.time()


def _resolve_template(
    template: str, *, mcp_dir: Path, workspace_dir: Path, session_id: str,
) -> Path:
    """Substitute ${mcp_dir}, ${workspace_dir}, ${session_id}."""
    s = template.replace("${mcp_dir}", str(mcp_dir))
    s = s.replace("${workspace_dir}", str(workspace_dir))
    s = s.replace("${session_id}", session_id)
    return Path(s)


def _session_workspace(
    session_id: str, agent_name: str, username: str, external_home: str = "",
) -> Path:
    """Derive the workspace dir for a session.

    External caller with a private tree → <external_home>/workspace.
    User-scoped sessions → users/{username}/workspace.
    Agent-scoped (no username) → workspace.
    """
    import config
    if external_home:
        return Path(external_home) / layout.WORKSPACE
    if username:
        return layout.user_dir(config.AGENTS_DIR / agent_name, username) / layout.WORKSPACE
    return config.AGENTS_DIR / agent_name / layout.WORKSPACE


def session_workspace_dir(ctx) -> Path:
    """The host folder of a session's workspace, as its ``workspace`` path
    role resolves it: the MOUNT decides, so a Shared-only human chat (a
    username, an agent-scope mount) works in the agent's ``workspace``."""
    from core.session.external_identity import external_home_of
    return _session_workspace("", ctx.agent, ctx.mount_username, external_home_of(ctx))


def session_workspace_shown(ctx) -> str:
    """The same folder as the session itself names it: sandbox-virtual
    (``/users/<u>/workspace``, ``/workspace``, ``/caller/workspace``) on the
    platform, the machine's own path on a paired machine (no sandbox there
    maps the virtual roots for a shell command)."""
    from core.session.external_identity import SANDBOX_HOME, external_home_of
    if external_home_of(ctx):
        return f"{SANDBOX_HOME}/{layout.WORKSPACE}"
    virtual = layout.virtual_workspace(ctx.mount_username)
    placement = getattr(ctx, "placement", None)
    agents_dir = getattr(placement, "agents_dir", "") if placement is not None else ""
    if agents_dir and cast("PlacementCapabilities", placement).needs_path_translation:
        agent_dir = agents_dir.replace("\\", "/").rstrip("/") + "/" + ctx.agent
        return layout.host_of_virtual(virtual, agent_dir, ctx.mount_username)
    return virtual


def _parse_output_names(result_text: str | None, folder: str) -> list[str]:
    """Extract the basename(s) the tool reported producing in its own form
    ``<folder>/<name>``, deduped + sanitized.

    Returns [] when no result text is available (→ caller uses the mtime fallback).
    """
    if not result_text or not folder:
        return []
    names: list[str] = []
    for m in re.findall(re.escape(folder) + "/" + _OUTPUT_NAME, result_text):
        name = os.path.basename(m)  # defensive; the regex already excludes '/'
        if name and name not in names and "/" not in name and ".." not in name:
            names.append(name)
    return names


def _mcp_container_name(manifest) -> str:
    """The namespaced container name compose gives this Docker MCP — the same
    string `compose_rewrite` writes as `container_name`."""
    import config
    return f"otodock-{config.INSTALL_ID}-mcp-{manifest.name}"


def _container_source_dir(out) -> str:
    """In-container path of the output dir (T2 `docker cp` source). The compose
    mounts `${mcp_dir}/<basename>` at `/<basename>` (camoufox: `/screenshots`)."""
    return "/" + Path(out.source).name


def _drain(stream: BinaryIO, sink: BinaryIO) -> bool:
    """Copy ``stream`` into ``sink`` to its end: False past the byte cap or
    the time bound, with the rest unread."""
    deadline = time.monotonic() + _DOCKER_CP_TIMEOUT_S
    fd, total = stream.fileno(), 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return False
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            return True
        total += len(chunk)
        if total > _MAX_OUTPUT_BYTES:
            return False
        sink.write(chunk)


def _docker_cp(container: str, src: str, sink: BinaryIO) -> bool:
    """`docker cp <container>:<src> -` through the (T2) socket-proxy (its
    CONTAINERS+POST grant: HEAD-stat + GET-archive): the archive goes into
    ``sink`` for the server to read, never onto a path the copy chooses.
    Bounded in bytes and time. Best-effort."""
    from core.config import deployment
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                ["docker", "cp", f"{container}:{src}", "-"],
                env={**os.environ, **deployment.docker_subprocess_env()},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
            )
            stdout = cast("BinaryIO", proc.stdout)
            try:
                whole = _drain(stdout, sink)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                stdout.close()
            if not whole:
                logger.warning("docker cp %s stopped: over %d bytes or %d s, nothing taken",
                               src, _MAX_OUTPUT_BYTES, _DOCKER_CP_TIMEOUT_S)
                return False
            if proc.returncode != 0:
                err.seek(0)
                logger.warning("docker cp %s failed: %s", src,
                               err.read()[-200:].decode(errors="replace").strip())
                return False
            return True
    except OSError as e:
        logger.warning("docker cp %s error: %s", src, e)
        return False


def _relocate_local(
    mcp_dir: Path, src_rel: str, names: list[str], dest_rel: str, start_ts: float,
) -> list[Path]:
    """T1: copy the named files (or, fallback, files newer than tool-start) out of
    the tool's own output folder into the per-session workspace dest, then
    remove them there. Only the regular files of the folder's first level,
    each reached with no link at any part; the copy lands a regular file,
    never a link (a rename would move a link the tool swapped in as one)."""
    import config
    cutoff = start_ts - _NAMED_SLACK_S if names else start_ts
    try:
        walk = safe_fs.walk_beneath(mcp_dir, src_rel)
        step = next(walk)
    except (OSError, StopIteration):
        return []
    try:
        taken = []
        for name in step.files:
            if names and name not in names:
                continue
            try:
                if os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_mtime >= cutoff:
                    taken.append(name)
            except OSError:
                continue
    finally:
        walk.close()
    moved: list[Path] = []
    for name in taken:
        try:
            safe_fs.copy_file_beneath(mcp_dir, f"{src_rel}/{name}", config.AGENTS_DIR, f"{dest_rel}/{name}",
                                      max_size=_MAX_OUTPUT_BYTES, mkdirs=True)
            safe_fs.unlink_beneath(mcp_dir, f"{src_rel}/{name}", missing_ok=True)
            moved.append(config.AGENTS_DIR / dest_rel / name)
        except OSError as e:
            logger.warning("relocate failed for %s: %s", name, e)
    return moved


def _chunks(reader: IO[bytes]) -> Iterator[bytes]:
    while chunk := reader.read(_CHUNK):
        yield chunk


def _from_archive(container: str, src: str, dest_rel: str,
                  accept: Callable[[tarfile.TarInfo], str | None]) -> list[Path]:
    """The members of the container's archive of ``src`` that ``accept``
    names (a regular file, by a plain name), each written into the
    destination by the server. Links, folders and any other member are
    never written."""
    import config
    moved: list[Path] = []
    with tempfile.TemporaryFile() as archive:
        if not _docker_cp(container, src, archive):
            return moved
        archive.seek(0)
        try:
            with tarfile.open(fileobj=archive, mode="r:") as tar:
                for member in tar:
                    # A name is one part (the write refuses "." and ".." itself).
                    name = accept(member) if member.isreg() else None
                    if not name:
                        continue
                    reader = tar.extractfile(member)
                    if reader is None:
                        continue
                    try:
                        with reader:
                            safe_fs.atomic_write_beneath(config.AGENTS_DIR, f"{dest_rel}/{name}",
                                                         _chunks(reader), mkdirs=True)
                        moved.append(config.AGENTS_DIR / dest_rel / name)
                    except OSError as e:
                        logger.warning("relocate failed for %s: %s", name, e)
        except (tarfile.TarError, OSError) as e:
            logger.warning("docker cp %s: unreadable archive: %s", src, e)
    return moved


def _relocate_via_docker(
    manifest, out, names: list[str], dest_rel: str, start_ts: float,
) -> list[Path]:
    """T2: the source is a named volume the proxy can't see — pull the file(s) out
    of the container with `docker cp`. Precise per-name; whole-dir fallback when
    the result text didn't name them. The archive's members are in Docker's
    layout: the named copy's one member is ``<name>``, the folder's are
    ``./`` and ``./<name>`` (deeper ones are left)."""
    container = _mcp_container_name(manifest)
    csrc = _container_source_dir(out)
    if names:
        cutoff = int(start_ts - _NAMED_SLACK_S)

        def named(name: str) -> Callable[[tarfile.TarInfo], str | None]:
            return lambda m: name if m.name == name and m.mtime >= cutoff else None

        moved: list[Path] = []
        for name in names:
            moved += _from_archive(container, f"{csrc}/{name}", dest_rel, named(name))
        return moved

    def first_level(member: tarfile.TarInfo) -> str | None:
        parts = member.name.rstrip("/").split("/")
        if parts[:1] == ["."]:
            parts = parts[1:]
        return parts[0] if len(parts) == 1 and member.mtime >= int(start_ts) else None

    return _from_archive(container, f"{csrc}/.", dest_rel, first_level)


def _prune_recent(dest_rel: str, keep: int) -> None:
    """Keep only the `keep` newest files in the (relocation-dedicated) dest dir;
    delete older. Safe — a displayed image is already base64-inlined into chat
    history, so the on-disk copy is just a convenience. Regular files only,
    removed through the handle the listing held: a link there is neither
    counted nor removed, and a dest reached through a link is left alone."""
    import config
    if keep <= 0:
        return
    try:
        walk = safe_fs.walk_beneath(config.AGENTS_DIR, dest_rel)
        step = next(walk)
    except (OSError, StopIteration):
        return
    try:
        dated = []
        for name in step.files:
            try:
                dated.append((os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_mtime, name))
            except OSError:
                continue
        dated.sort(reverse=True)
        for _mtime, name in dated[keep:]:
            try:
                safe_fs.unlink_beneath(step.dirfd, name, missing_ok=True)
            except OSError as e:
                logger.debug("keep_recent prune failed for %s: %s", name, e)
    finally:
        walk.close()


def _rels(out, manifest, workspace: Path, session_id: str) -> tuple[str, str] | None:
    """``(source rel below the tool's folder, dest rel below AGENTS_DIR)`` of
    one ``outputs`` entry, or None (logged) when the destination is not
    inside the session's workspace or the source not inside the tool's own
    folder. Judged on the text: the opens that follow refuse any link."""
    import config
    dest_dir = _resolve_template(
        out.destination_template,
        mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
    )
    source_dir = _resolve_template(
        out.source,
        mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
    )
    try:
        src_rel = safe_fs.rel_under(source_dir, manifest.mcp_dir)
        dest_rel = safe_fs.rel_under(dest_dir, config.AGENTS_DIR)
    except OSError:
        src_rel = dest_rel = ""
    if _below(dest_dir, workspace) and src_rel:
        return src_rel, dest_rel
    logger.warning(_OUTSIDE, getattr(manifest, "name", "?"))
    return None


_OUTSIDE = "%s: an outputs entry names a folder outside the session's workspace or the tool's own folder, skipped"


def _below(path: Path, root: Path) -> bool:
    """Whether ``path`` lies strictly below ``root``, judged on the text."""
    try:
        return bool(safe_fs.rel_under(path, root))
    except OSError:
        return False


def relocate_for_tool(
    session_id: str, mcp_name: str, tool_name: str, result_text: str | None = None,
) -> list[Path]:
    """Move the file(s) the MCP just produced into the per-session workspace dest.

    `result_text` (the tool's result, when available) enables the precise
    move-by-filename path; without it the source dir is mtime-scanned. Returns the
    destination paths written (local moves; for remote, push them with
    ``push_relocated_to_satellite``, or call ``relocate_and_push_for_tool``).
    """
    from core.config import deployment
    from core.session.session_state import get_session_security
    from services.mcp import mcp_registry

    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or not manifest.outputs:
        return []
    ctx = get_session_security(session_id)
    if ctx is None:
        return []

    st = _state.get(session_id)
    start_ts = st.started_at.get(mcp_name, 0.0) if st else 0.0
    workspace = session_workspace_dir(ctx)
    is_t2 = deployment.current_mode() == deployment.MANAGED_SOCKPROX

    moved: list[Path] = []
    for out in manifest.outputs:
        if not (("*" in out.after_tools) or (tool_name in out.after_tools)):
            continue
        rels = _rels(out, manifest, workspace, session_id)
        if rels is None:
            continue
        src_rel, dest_rel = rels
        names = _parse_output_names(result_text, Path(out.source).name)
        if is_t2:
            moved += _relocate_via_docker(manifest, out, names, dest_rel, start_ts)
        else:
            moved += _relocate_local(manifest.mcp_dir, src_rel, names, dest_rel, start_ts)
        if out.keep_recent:
            _prune_recent(dest_rel, out.keep_recent)
    return moved


async def push_relocated_to_satellite(
    session_id: str, moved_paths: list[Path],
) -> None:
    """Push relocated files to the satellite for remote sessions.

    No-op for local sessions. For remote sessions, each file's bytes are sent via
    ``cm.push_file()`` so the agent CLI on the satellite can read it at the same
    agent-relative path (needed for ``display_images`` on the satellite). Best-effort:
    failed pushes are logged, not raised. The satellite's copy is add-only and
    excluded from the bidirectional sync, so it's bounded on the satellite side by
    its own periodic ``.screenshots`` sweep (mirrors the platform keep-cap — see
    ``satellite/transport/ws_client.py::_sweep_satellite_screenshots``).
    """
    if not moved_paths:
        return
    from core.remote import remote_file_flow
    if not remote_file_flow.is_remote_session(session_id):
        return

    info = remote_file_flow._get_remote_session_info(session_id)
    if info is None or not getattr(info, "files_synced", True):
        return
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()

    import asyncio

    import config as _cfg
    from core.remote.remote_file_cache import _open_platform_copy
    from services.path_policy_v2 import PathRef

    for dst in moved_paths:
        try:
            rel_path = safe_fs.rel_under(dst, _cfg.AGENTS_DIR / info.agent_name)
        except OSError:
            logger.warning("Skipping push (path outside agent_dir): %s", dst)
            continue
        # The file as written, reached with no link at any part: a swap
        # after the move refuses the push instead of sending another file.
        try:
            fd, _st = await asyncio.to_thread(_open_platform_copy, info.agent_name, rel_path)
        except OSError as e:
            logger.warning("push_relocated_to_satellite: %s not pushed: %s", rel_path, type(e).__name__)
            continue
        try:
            # Pass the descriptor's PATH — push_file streams from disk
            # (unreadable → False with its own warning log).
            ok = await cm.push_file(
                info.machine_id,
                PathRef("agent_tree", rel_path),
                Path(safe_fs.fd_path(fd)),
                agent_slug=info.agent_name,
            )
        finally:
            os.close(fd)
        if not ok:
            logger.warning("push_relocated_to_satellite: push failed for %s", rel_path)


async def relocate_and_push_for_tool(
    session_id: str, mcp_name: str, tool_name: str, result_text: str | None = None,
) -> list[Path]:
    """Async wrapper: relocate + push to satellite if remote. The move (a
    rename, or ``docker cp`` for up to 60 s on a container install) runs on a
    lane of its own (``file_commit.run_in``), never on the event loop and
    never in front of an upload's commits."""
    from core import file_commit
    moved = await file_commit.run_in("tool-outputs", relocate_for_tool, session_id, mcp_name,
                                     tool_name, result_text=result_text)
    if moved:
        await push_relocated_to_satellite(session_id, moved)
    return moved


def cleanup_session(
    session_id: str, agent_name: str, username: str, external_home: str = "",
) -> None:
    """Purge per-session subdirs declared with `gc_after: "session_close"`.

    Modern outputs (camoufox) bound their dest with `keep_recent` instead and set
    no `gc_after`, so they skip this. The per-session `/{session_id}` is retained
    below as a HARD GUARD — it ensures this can only ever target a (legacy,
    per-session) subdir, never `rmtree` the flat dest dir itself.
    """
    from services.mcp import mcp_registry

    _state.pop(session_id, None)
    workspace = (
        _session_workspace(session_id, agent_name, username, external_home)
        if external_home else _session_workspace(session_id, agent_name, username)
    )
    folders = []
    for _name, manifest in mcp_registry.get_all_manifests().items():
        for out in manifest.outputs:
            if out.gc_after != "session_close":
                continue
            dest_base = _resolve_template(
                out.destination_template,
                mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
            )
            if not _below(dest_base, workspace):
                logger.warning(_OUTSIDE, getattr(manifest, "name", "?"))
                continue
            folders.append(dest_base / session_id)  # GUARD: never the flat dest itself
    if folders:
        # Off the event loop; the next start of the id waits for it.
        from core import file_commit
        file_commit.behind(_remove_folders, folders, name="session_outputs_removal", key=session_id)


def _remove_folders(folders: list[Path]) -> None:
    """Each folder removed from ``AGENTS_DIR`` with no link at any part, and
    none followed inside it; one that is not there, or not reached plainly,
    is left (logged) and the rest go on."""
    import config
    for folder in folders:
        try:
            safe_fs.rmtree_beneath(config.AGENTS_DIR, safe_fs.rel_under(folder, config.AGENTS_DIR),
                                   missing_ok=True)
        except OSError as e:
            logger.warning("session outputs folder %s not removed: %s", folder.name, type(e).__name__)
