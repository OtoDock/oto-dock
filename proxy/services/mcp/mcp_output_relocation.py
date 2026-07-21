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
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("claude-proxy.mcp-output-relocation")

# A media basename the tool reported producing (e.g. camoufox's
# `screenshots/page-2026-06-27T05-11-40-812Z.png`). The character class excludes
# `/`, so a result string can never steer this into a path traversal — the match
# is always a bare basename.
_OUTPUT_NAME_RE = re.compile(r"([\w.:\-]+\.(?:png|jpe?g|webp|gif))")


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


def _session_workspace(session_id: str, agent_name: str, username: str) -> Path:
    """Derive the workspace dir for a session.

    User-scoped sessions → users/{username}/workspace.
    Agent-scoped (no username) → workspace.
    """
    import config
    if username:
        return config.AGENTS_DIR / agent_name / "users" / username / "workspace"
    return config.AGENTS_DIR / agent_name / "workspace"


def _parse_output_names(result_text: str | None) -> list[str]:
    """Extract the basename(s) the tool reported producing, deduped + sanitized.

    Returns [] when no result text is available (→ caller uses the mtime fallback).
    """
    if not result_text:
        return []
    names: list[str] = []
    for m in _OUTPUT_NAME_RE.findall(result_text):
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


def _docker_cp(container: str, src: str, dst: Path) -> bool:
    """`docker cp <container>:<src> <dst>` through the (T2) socket-proxy. Allowed
    by its CONTAINERS+POST grant (HEAD-stat + GET-archive). Best-effort."""
    from core.config import deployment
    try:
        r = subprocess.run(
            ["docker", "cp", f"{container}:{src}", str(dst)],
            env={**os.environ, **deployment.docker_subprocess_env()},
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            logger.warning("docker cp %s failed: %s", src, (r.stderr or "").strip()[:200])
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("docker cp %s error: %s", src, e)
        return False


def _relocate_local(
    source_dir: Path, names: list[str], dest_dir: Path, start_ts: float,
) -> list[Path]:
    """T1: move the named files (or, fallback, files newer than tool-start) out of
    the shared host source dir into the per-session workspace dest."""
    if not source_dir.is_dir():
        return []
    if names:
        srcs = [source_dir / n for n in names]
        srcs = [p for p in srcs if p.parent == source_dir and p.is_file()]
    else:
        srcs = []
        for p in source_dir.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime >= start_ts:
                    srcs.append(p)
            except OSError:
                continue
    if not srcs:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for src in srcs:
        try:
            dst = dest_dir / src.name
            shutil.move(str(src), str(dst))
            moved.append(dst)
        except (OSError, shutil.Error) as e:
            logger.warning("relocate failed for %s: %s", src, e)
    return moved


def _relocate_via_docker(
    manifest, out, names: list[str], dest_dir: Path, start_ts: float,
) -> list[Path]:
    """T2: the source is a named volume the proxy can't see — pull the file(s) out
    of the container with `docker cp`. Precise per-name; whole-dir fallback when
    the result text didn't name them."""
    container = _mcp_container_name(manifest)
    csrc = _container_source_dir(out)
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    if names:
        for name in names:
            dst = dest_dir / name
            if _docker_cp(container, f"{csrc}/{name}", dst):
                moved.append(dst)
        return moved
    # Fallback: copy the whole dir, then keep the files newer than tool-start.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if not _docker_cp(container, f"{csrc}/.", tmp_path):
            return []
        for src in tmp_path.iterdir():
            try:
                if src.is_file() and src.stat().st_mtime >= start_ts:
                    dst = dest_dir / src.name
                    shutil.move(str(src), str(dst))
                    moved.append(dst)
            except OSError:
                continue
    return moved


def _prune_recent(dest_dir: Path, keep: int) -> None:
    """Keep only the `keep` newest files in the (relocation-dedicated) dest dir;
    delete older. Safe — a displayed image is already base64-inlined into chat
    history, so the on-disk copy is just a convenience."""
    if keep <= 0 or not dest_dir.is_dir():
        return
    try:
        files = sorted(
            (p for p in dest_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return
    for p in files[keep:]:
        try:
            p.unlink(missing_ok=True)  # already-gone file is fine
        except OSError as e:
            logger.debug("keep_recent prune failed for %s: %s", p, e)


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
    workspace = _session_workspace(session_id, ctx.agent, ctx.username)
    names = _parse_output_names(result_text)
    is_t2 = deployment.current_mode() == deployment.MANAGED_SOCKPROX

    moved: list[Path] = []
    for out in manifest.outputs:
        if not (("*" in out.after_tools) or (tool_name in out.after_tools)):
            continue
        dest_dir = _resolve_template(
            out.destination_template,
            mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
        )
        if is_t2:
            moved += _relocate_via_docker(manifest, out, names, dest_dir, start_ts)
        else:
            source_dir = _resolve_template(
                out.source,
                mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
            )
            moved += _relocate_local(source_dir, names, dest_dir, start_ts)
        if out.keep_recent:
            _prune_recent(dest_dir, out.keep_recent)
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
    if info is None:
        return
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()

    import config as _cfg
    agent_dir = (_cfg.AGENTS_DIR / info.agent_name).resolve()

    for dst in moved_paths:
        try:
            rel_path = str(dst.resolve().relative_to(agent_dir))
        except ValueError:
            logger.warning("Skipping push (path outside agent_dir): %s", dst)
            continue
        try:
            content = dst.read_bytes()
        except OSError as e:
            logger.warning("Cannot read %s for push: %s", dst, e)
            continue
        from services.path_policy_v2 import PathRef
        ok = await cm.push_file(
            info.machine_id,
            PathRef("agent_tree", rel_path),
            content,
            agent_slug=info.agent_name,
        )
        if not ok:
            logger.warning("push_relocated_to_satellite: push failed for %s", rel_path)


async def relocate_and_push_for_tool(
    session_id: str, mcp_name: str, tool_name: str, result_text: str | None = None,
) -> list[Path]:
    """Async wrapper: relocate (sync) + push to satellite if remote."""
    moved = relocate_for_tool(session_id, mcp_name, tool_name, result_text=result_text)
    if moved:
        await push_relocated_to_satellite(session_id, moved)
    return moved


def cleanup_session(session_id: str, agent_name: str, username: str) -> None:
    """Purge per-session subdirs declared with `gc_after: "session_close"`.

    Modern outputs (camoufox) bound their dest with `keep_recent` instead and set
    no `gc_after`, so they skip this. The per-session `/{session_id}` is retained
    below as a HARD GUARD — it ensures this can only ever target a (legacy,
    per-session) subdir, never `rmtree` the flat dest dir itself.
    """
    from services.mcp import mcp_registry

    _state.pop(session_id, None)
    workspace = _session_workspace(session_id, agent_name, username)
    for _name, manifest in mcp_registry.get_all_manifests().items():
        for out in manifest.outputs:
            if out.gc_after != "session_close":
                continue
            dest_base = _resolve_template(
                out.destination_template,
                mcp_dir=manifest.mcp_dir, workspace_dir=workspace, session_id=session_id,
            )
            dest_dir = dest_base / session_id  # GUARD: never the flat dest itself
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
