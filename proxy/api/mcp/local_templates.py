"""Local agent-template API — agents creating agents.

An agent authors a community-agent template folder somewhere it can write
and hands the path to these endpoints, which run it through the exact
pipeline that installs catalog templates. Driven by ``agent-creator-mcp``;
the dashboard has no UI for this yet.

Authorization has two independent layers, and the second is the real one:

1. The MCP shows its tools only when ``OTO_PLATFORM_ROLE`` is
   ``admin``/``creator``. Cosmetic — env is baked at session spawn.
2. Every endpoint here requires a session-backed platform creator/admin,
   resolved live from the users table by the auth layer, and resolves the
   caller-supplied path through the session's own path policy.

The template folder is COPIED into a private snapshot before it is parsed:
the authoring agent owns that directory and could otherwise swap a
subdirectory for a symlink between the check and the read.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api._guards import require_creator_or_admin
from auth import roles
from auth.providers import UserContext, get_current_user, require_auth, require_user

logger = logging.getLogger("otodock.local-templates")
router = APIRouter(dependencies=[Depends(require_user)])

# The parser reads every context file into memory with no cap of its own,
# so these are load-bearing, not belt-and-braces.
_MAX_FILES = 300
_MAX_FILE_BYTES = 1 * 1024 * 1024
_MAX_TOTAL_BYTES = 20 * 1024 * 1024


class LocalTemplateBody(BaseModel):
    path: str


class InstallLocalTemplateBody(BaseModel):
    path: str
    target_slug: str | None = None


_CREATOR_DETAIL = "Creating agents requires a platform creator or admin account"


@dataclass(frozen=True)
class _Folder:
    """A caller's template or package folder, authorized: ``rel`` is its
    agent-tree path (what a remote machine lists), ``platform`` the
    platform's copy, which need not exist (a remote session's folder may
    reach the platform only at the end of the turn)."""

    raw: str
    rel: str
    platform: Path
    agent: str


def _resolve_template_dir(user: UserContext, raw_path: str) -> _Folder:
    """Resolve a caller-supplied template path against the session's policy.

    Requires a real session principal: the path policy is built from the
    session's security context, and the RBAC re-imposition keys on the
    session's own username. A master-key or user-less principal has neither.
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(400, "path is required")
    if not user.session_id or not user.acting_sub:
        raise HTTPException(
            403,
            "This endpoint is callable only from an agent session with a "
            "signed-in user",
        )

    from auth.path_policy import check_host_path_access, enforce_agent_tree_rbac
    from core.session import session_state
    from services import path_policy_v2

    ctx = session_state.get_session_security(user.session_id)
    if ctx is None:
        raise HTTPException(
            409, "Session security context unavailable — retry in a moment",
        )

    policy_ctx = path_policy_v2.context_from_security(ctx)
    resolution = path_policy_v2.resolve_path_for_session(policy_ctx, raw_path.strip())
    if not resolution.allowed:
        raise HTTPException(403, resolution.error or "access denied")
    ref = getattr(resolution, "path_ref", None)
    if ref is None or ref.kind != "agent_tree":
        # A satellite-host path lives on the remote machine's disk; the proxy
        # cannot read it. Authoring must happen inside the synced agent tree.
        raise HTTPException(
            400,
            "The template folder must live inside the agent's workspace "
            "(e.g. /workspace/... or /users/<you>/workspace/...), not "
            "elsewhere on the machine's filesystem",
        )
    decision = enforce_agent_tree_rbac(resolution, ctx, writing=False)
    if not decision.allowed:
        raise HTTPException(403, decision.reason or "access denied")

    # ``access_path`` is the sandbox-virtual form on local sessions; the
    # agent-tree-relative slug on ``path_ref`` is what maps to proxy-side
    # disk, and it is the same value the RBAC check above authorized.
    import config
    agent_root = config.get_agent_dir(ctx.agent).resolve()
    resolved = (agent_root / ref.value).resolve()
    # Judged again where it resolves NOW: a symlink swapped in since the
    # check above must not lead into another person's tree.
    if not resolved.is_relative_to(agent_root) or not check_host_path_access(resolved, ctx).allowed:
        raise HTTPException(403, "access denied")
    return _Folder(
        raw=raw_path.strip(), rel=ref.value.strip("/"), platform=resolved, agent=ctx.agent,
    )


# A remote session's folder is read from its machine, four pulls at a time.
# One 60 s deadline covers the listing and the pulls: the MCP clients wait
# 120 s for a call, so a read that ends in time leaves the install or the
# package check the other half.
_REMOTE_PULLS = 4
_REMOTE_READ_BUDGET_S = 60.0
# The longest name this platform's filesystems hold, in UTF-8 bytes.
_MAX_NAME_BYTES = 255


class _MachineRefused(Exception):
    """A path the machine listed that the snapshot may not hold."""


def _clip(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _skipped_name(parts: list[str], skip_dirs: frozenset[str]) -> str | None:
    """The local snapshot's skip rules on one listed path (``parts`` relative
    to the folder, the file name last): the name of what is left out, a
    folder or the file itself, or None to keep the file."""
    for i, name in enumerate(parts[:-1]):
        if name.startswith(".") or name == "node_modules" or name in skip_dirs or (
                name == "data" and _is_app_dir(Path(*parts[:i]))):
            return "/".join(parts[:i + 1])
    if parts[-1].startswith("."):
        return "/".join(parts)
    return None


def _plan_remote_snapshot(
    folder: _Folder, entries: list[dict], *, skip_dirs: frozenset[str],
    skipped: list[str] | None, what: str,
) -> list[tuple[str, str]]:
    """``(agent-tree path, path inside the folder)`` for every listed file
    the snapshot takes, the listing judged before a byte moves: the caps on
    the listed sizes (and again on what lands), and by name every file the
    snapshot could not hold as listed (listed twice, also the folder of
    another listed file, a name too long for this platform)."""
    from core.remote.file_sync import is_canonical_rel_path
    prefix = folder.rel + "/"
    out: list[tuple[str, str]] = []
    total = 0
    left_out_seen = set(skipped or ())
    kept: set[str] = set()
    kept_dirs: set[str] = set()
    for entry in entries:
        path = entry["path"]
        # Canonical first: a ``..`` is refused, never skipped as a dot-name.
        if not is_canonical_rel_path(path) or not path.startswith(prefix):
            raise _MachineRefused(path)
        inner = path[len(prefix):]
        parts = inner.split("/")
        left_out = _skipped_name(parts, skip_dirs)
        if left_out is not None:
            if skipped is not None and left_out not in left_out_seen:
                left_out_seen.add(left_out)
                skipped.append(left_out)
            continue
        try:
            name_bytes = [len(part.encode()) for part in parts]
        except UnicodeEncodeError as exc:
            raise _MachineRefused(path) from exc
        if max(name_bytes) > _MAX_NAME_BYTES:
            raise HTTPException(
                400,
                f"A name in {_clip(inner)} is longer than {_MAX_NAME_BYTES} bytes, "
                "more than this platform holds: shorten it",
            )
        if inner in kept:
            raise HTTPException(
                400, f"The machine listed {_clip(inner)} twice in the {what} folder",
            )
        dirs = ["/".join(parts[:i]) for i in range(1, len(parts))]
        clash = inner if inner in kept_dirs else next((d for d in dirs if d in kept), None)
        if clash is not None:
            raise HTTPException(
                400,
                f"The machine listed {_clip(clash)} as both a file and a folder "
                f"in the {what} folder",
            )
        kept.add(inner)
        kept_dirs.update(dirs)
        if len(out) >= _MAX_FILES:
            raise HTTPException(
                400, f"The {what} folder has too many files (max {_MAX_FILES})",
            )
        _check_file_size(inner, entry["size"])
        total += entry["size"]
        _check_total_size(total, what)
        out.append((path, inner))
    return out


def _file_too_large(rel: str) -> HTTPException:
    return HTTPException(
        400, f"{rel} is too large (max {_MAX_FILE_BYTES // 1024} KB per file)",
    )


def _folder_too_large(what: str) -> HTTPException:
    return HTTPException(
        400,
        f"The {what} folder is too large "
        f"(max {_MAX_TOTAL_BYTES // (1024 * 1024)} MB total)",
    )


def _check_file_size(rel: str, size: int) -> None:
    if size > _MAX_FILE_BYTES:
        raise _file_too_large(rel)


def _check_total_size(total: int, what: str) -> None:
    if total > _MAX_TOTAL_BYTES:
        raise _folder_too_large(what)


def _held_elsewhere(session_id: str, folder: _Folder, machine: str) -> HTTPException | None:
    """The 409 when the session's machine runs it for another agent than
    the folder's: no retry changes that answer, unlike a 503."""
    from core.remote import remote_file_flow
    held = remote_file_flow.remote_session_agent(session_id)
    if held and held != folder.agent:
        return HTTPException(409, f"This session runs {held} on {machine}, not {folder.agent}")
    return None


async def _failed_pull(
    session_id: str, folder: _Folder, path: str, inner: str, cap: int, what: str,
) -> HTTPException:
    """Why a pull under ``cap`` failed: a file the machine holds past the
    cap is too large (a retry would fail the same way), anything else a
    503 the agent retries."""
    from core.remote import remote_file_flow
    from services.delegation.file_transfer import remote_source_label
    machine = await remote_source_label(session_id)
    held = _held_elsewhere(session_id, folder, machine)
    if held is not None:
        return held
    probe = await remote_file_flow.stat_probe(session_id, path)
    size = probe.get("size") if probe and probe.get("exists") else None
    if isinstance(size, int) and size > cap:
        return _file_too_large(inner) if size > _MAX_FILE_BYTES else _folder_too_large(what)
    return HTTPException(503, f"{inner} could not be read from {machine}, retry in a moment")


async def _pull_remote_snapshot(
    session_id: str, folder: _Folder, files: list[tuple[str, str]], what: str,
) -> Path:
    """Pull ``files`` from the session's machine into a private temp dir.
    The bytes land nowhere else: the platform copy arrives through the
    end-of-turn sync, gated as always. Each pull is capped by what may still
    land (a file at most 1 MiB, the folder 20 MiB counted from what landed,
    whatever the listing said); a file past its cap fails the read with a
    400, any other failed pull with a 503 (the agent retries) rather than
    serving older bytes. The first failure ends the read: the workers still
    running are cancelled and awaited before the temp dir goes, so none
    writes into it afterwards."""
    from core.remote import remote_file_flow

    dest = Path(tempfile.mkdtemp(prefix="oto-local-template-"))
    queue = list(files)
    landed = 0

    async def _worker() -> None:
        nonlocal landed
        while queue:
            path, inner = queue.pop(0)
            cap = min(_MAX_FILE_BYTES, _MAX_TOTAL_BYTES - landed)
            if cap <= 0:
                # Never passed on: a cap of 0 is no cap to the pull.
                raise _folder_too_large(what)
            target = dest / inner
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("A template file could not be stored: %s", exc)
                reason = ("The name is too long." if exc.errno == errno.ENAMETOOLONG
                          else "The server log has the details.")
                raise HTTPException(400, f"{_clip(inner)} cannot be stored here. {reason}") from exc
            if not await remote_file_flow.pull_into(
                    session_id, path, target, agent=folder.agent, max_bytes=cap):
                raise await _failed_pull(session_id, folder, path, inner, cap, what)
            st = target.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise HTTPException(400, f"Only regular files are allowed: {inner}")
            _check_file_size(inner, st.st_size)
            landed += st.st_size
            _check_total_size(landed, what)

    tasks: list[asyncio.Task[None]] = []
    try:
        tasks = [
            asyncio.create_task(_worker()) for _ in range(min(_REMOTE_PULLS, len(files)))
        ]
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task.done():
                task.result()
        return dest
    except BaseException as err:
        for task in tasks:
            task.cancel()
        # Every worker ends before the rmtree, even when the caller is
        # cancelled again meanwhile; that cancel is raised after it.
        interrupted = False
        while not all(task.done() for task in tasks):
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                interrupted = True
        for task in tasks:
            if not task.cancelled():
                task.exception()
        shutil.rmtree(dest, ignore_errors=True)
        if interrupted:
            raise asyncio.CancelledError from err
        raise


async def _snapshot_folder(
    user: UserContext, folder: _Folder, *, skip_dirs: frozenset[str] = frozenset(),
    skipped: list[str] | None = None, what: str = "template",
) -> Path:
    """The private copy the checks read. For a session on a remote machine
    the machine's folder: what the agent wrote this turn reaches the
    platform only at the turn's end, so the platform copy may be missing or
    old. The platform copy only when the machine holds nothing there (a
    folder only the platform has); a machine that does not answer, or not
    in time, is a 503, never a silent read of older bytes."""
    from core.remote import remote_file_flow
    from services.delegation.file_transfer import remote_source_label

    machine = ""
    if user.session_id and remote_file_flow.is_remote_session(user.session_id):
        session_id = user.session_id
        try:
            async with asyncio.timeout(_REMOTE_READ_BUDGET_S):
                entries = await remote_file_flow.list_remote_entries(
                    session_id, folder.rel, agent=folder.agent,
                )
                if entries:
                    try:
                        files = _plan_remote_snapshot(
                            folder, entries, skip_dirs=skip_dirs, skipped=skipped, what=what,
                        )
                    except _MachineRefused as exc:
                        path = str(exc)
                        logger.warning("local %s read: the machine listed %r", what, path)
                        inner = path.removeprefix(folder.rel + "/")
                        raise HTTPException(
                            400,
                            f"The machine listed an invalid path in the {what} folder: "
                            f"{_clip(repr(inner))}",
                        ) from exc
                    return await _pull_remote_snapshot(session_id, folder, files, what)
        except TimeoutError as err:
            machine = await remote_source_label(session_id)
            raise HTTPException(
                503,
                f"Reading the {what} folder from {machine} took too long, "
                "retry in a moment",
            ) from err
        machine = await remote_source_label(session_id)
        if entries is None:
            raise _held_elsewhere(session_id, folder, machine) or HTTPException(
                503, f"{machine} did not list the {what} folder, retry in a moment",
            )
        if not folder.platform.is_dir() and any(
                part.startswith(".") for part in folder.rel.split("/")):
            # The machine never lists a folder under a dot-folder, and the
            # sync never carries one: the author has to move it.
            raise HTTPException(
                400,
                f"Not a directory: {folder.raw} (a folder inside a folder whose "
                f"name starts with '.' is not read from {machine}; move it)",
            )
    if not folder.platform.is_dir():
        where = (
            f" (not on the platform, and {machine} holds no file there that it "
            "reads: generated, dependency and dot folders are not read)"
        ) if machine else ""
        raise HTTPException(400, f"Not a directory: {folder.raw}{where}")
    return await asyncio.to_thread(
        _snapshot_template_dir, folder.platform, skip_dirs=skip_dirs, skipped=skipped,
        what=what,
    )


def _snapshot_template_dir(
    src: Path, *, skip_dirs: frozenset[str] = frozenset(),
    skipped: list[str] | None = None, what: str = "template",
) -> Path:
    """Copy ``src`` into a private temp dir, rejecting anything unexpected.

    Only regular files and real directories are copied — no symlinks, no
    devices — so the parser (which globs and follows symlinked dirs) can
    never reach outside what was validated here. The folder is the
    session's own and may change while it is read: the walk holds each
    directory open (``os.fwalk``) and opens every file relative to it with
    ``O_NOFOLLOW``, judging what it opened (``fstat``), never a name it
    checked before. The caller deletes the snapshot.

    ``skip_dirs`` adds directory names left out besides the dotfiles and
    ``node_modules``; ``skipped`` collects the relative names of everything
    left out, so a check that must refuse them (an MCP package's ``.env``
    or ``venv``) sees them without the copy holding them.
    """
    dest = Path(tempfile.mkdtemp(prefix="oto-local-template-"))
    files = 0
    total = 0
    try:
        for root, dirnames, filenames, dirfd in os.fwalk(src, follow_symlinks=False):
            rel_root = Path(root).relative_to(src)
            # Skip what a release never takes (dotfiles, node_modules, an
            # app's data/) — an author's `bun install` beside the app would
            # otherwise fill the file cap; reject symlinked dirs outright.
            for name in list(dirnames):
                if name.startswith(".") or name == "node_modules" or name in skip_dirs or (
                        name == "data" and _is_app_dir(rel_root)):
                    dirnames.remove(name)
                    if skipped is not None:
                        skipped.append(str(rel_root / name))
                elif stat.S_ISLNK(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode):
                    raise HTTPException(
                        400,
                        f"Symlinks are not allowed in a {what} folder: "
                        f"{rel_root / name}",
                    )
            (dest / rel_root).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                if name.startswith("."):
                    if skipped is not None:
                        skipped.append(str(rel_root / name))
                    continue
                rel = rel_root / name
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        raise HTTPException(
                            400,
                            f"Symlinks are not allowed in a {what} folder: {rel}",
                        ) from e
                    raise HTTPException(400, f"{rel} cannot be read") from e
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise HTTPException(
                            400, f"Only regular files are allowed: {rel}",
                        )
                    files += 1
                    if files > _MAX_FILES:
                        raise HTTPException(
                            400, f"The {what} folder has too many files (max {_MAX_FILES})",
                        )
                    with os.fdopen(os.dup(fd), "rb") as fh:
                        data = fh.read(_MAX_FILE_BYTES + 1)
                finally:
                    os.close(fd)
                if len(data) > _MAX_FILE_BYTES:
                    raise HTTPException(
                        400,
                        f"{rel} is too large "
                        f"(max {_MAX_FILE_BYTES // 1024} KB per file)",
                    )
                total += len(data)
                if total > _MAX_TOTAL_BYTES:
                    raise HTTPException(
                        400,
                        f"The {what} folder is too large "
                        f"(max {_MAX_TOTAL_BYTES // (1024 * 1024)} MB total)",
                    )
                (dest / rel).write_bytes(data)
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# POST /v1/mcps/local-package/validate — an MCP package an agent authored
# ---------------------------------------------------------------------------

# A built package's dependency tree is never part of what ships; left out of
# the snapshot (and reported) instead of counting against the file cap.
_PACKAGE_SKIP_DIRS = frozenset({"venv"})


def _require_package_author(user: UserContext | None) -> UserContext:
    """A session principal with a signed-in person at the editor tier of the
    session's agent: the audience of the authoring skill (``mcps-mcp``
    lists its check tool for the same tier). The path resolver below needs
    the session either way."""
    user = require_auth(user)
    if not user.session_id or not user.acting_sub or not user.agent:
        raise HTTPException(
            403,
            "This endpoint is callable only from an agent session with a "
            "signed-in user",
        )
    if not roles.can_edit(user.acting_role(user.agent)):
        raise HTTPException(
            403, "Authoring an MCP package takes the editor role or above on this agent",
        )
    return user


@router.post("/v1/mcps/local-package/validate")
async def validate_local_package(
    body: LocalTemplateBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Check an MCP package folder the way the admin installer will, without
    installing it (``community_installer.check_package``). The folder is
    resolved through the session's own path policy and snapshotted (no
    symlink, the byte caps), so nothing outside the session's tree is read.
    Problems come back as ``ok: false`` plus ``errors`` and ``warnings``
    (the agent self-corrects from them); 4xx is reserved for authorization
    and path problems."""
    from services.community import community_installer

    u = _require_package_author(user)
    folder = _resolve_template_dir(u, body.path)
    skipped: list[str] = []
    snapshot = await _snapshot_folder(
        u, folder, skip_dirs=_PACKAGE_SKIP_DIRS, skipped=skipped, what="package",
    )
    try:
        return await asyncio.to_thread(
            community_installer.check_package, snapshot, skipped=skipped,
        )
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


def _is_app_dir(rel_root: Path) -> bool:
    """``apps/<slug>`` or ``user-apps/<slug>`` inside the template."""
    parts = rel_root.parts
    return len(parts) == 2 and parts[0] in ("apps", "user-apps")


def _validate_apps(template) -> list[str]:
    """Each app folder through the deploy's own validator and static
    checks, before its agent exists. An ``mcp_tool`` action names an MCP the
    agent will only have after the install, so that one refusal is
    tolerated when the template requires the MCP; everything else the
    deploy would refuse is reported here, with the file and the line."""
    from services.apps import app_deploy, app_lint, releases
    errors: list[str] = []
    required = {m.name for m in template.mcps}
    for item in template.apps:
        label = f"{'user-apps' if item.visibility == 'user' else 'apps'}/{item.slug}"
        required |= set(item.requires_mcps)
        doc = json.loads((item.dir / "app.json").read_text(encoding="utf-8"))
        try:
            app_deploy.validate_app_json(doc, template.slug, item.visibility != "user", item.dir)
        except app_deploy.DeployError as exc:
            m = _MCP_UNAVAILABLE_RE.search(str(exc))
            if not (m and m.group(1) in required):
                errors.append(f"{label}: {exc}")
                continue
        try:
            files = releases.walk_tree(item.dir)
        except releases.ReleaseInvalid as exc:
            errors.append(f"{label}: {exc.reason}")
            continue
        for f in app_lint.lint_tree(item.dir, files, app_lint.declared_from_doc(doc)):
            if f.severity == "fail":
                errors.append(f"{label}: {f.file}:{f.line} {f.message}")
    return errors


_MCP_UNAVAILABLE_RE = re.compile(r"MCP '([^']+)' is not available to this agent")


async def _load_snapshot(user: UserContext, raw_path: str):
    """Resolve → snapshot → parse. Returns ``(template, snapshot_dir)``.

    The caller owns the snapshot dir and must delete it.
    """
    from services.community.community_agent_template_store import (
        TemplateValidationError,
        load_template_from_dir,
    )

    folder = _resolve_template_dir(user, raw_path)
    snapshot = await _snapshot_folder(user, folder)
    try:
        template = await asyncio.to_thread(load_template_from_dir, snapshot)
    except TemplateValidationError as exc:
        shutil.rmtree(snapshot, ignore_errors=True)
        # The parser names files by their snapshot path; the author knows
        # them by their place in the folder.
        problems = [p.replace(f"{snapshot}{os.sep}", "") for p in exc.problems]
        raise HTTPException(400, detail={"error": "invalid_template", "message": "; ".join(problems),
                                         "problems": problems}) from exc
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return template, snapshot


# ---------------------------------------------------------------------------
# GET /v1/agents/local-template/building-blocks
# ---------------------------------------------------------------------------

@router.get("/v1/agents/local-template/building-blocks")
async def local_template_building_blocks(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """MCPs, skill packages and taken slugs an authoring agent can draw on.

    Projects explicit fields only — a manifest carries config field
    definitions and env templates that have no business in an agent's
    context.
    """
    require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    from services.community import community_catalog
    from services.mcp import mcp_registry
    from services.mcp.mcp_registry import _first_sentence
    from storage.agents import agent_store

    mcps: list[dict] = []
    skill_packages: list[dict] = []
    installed_names: set[str] = set()
    for name, manifest in mcp_registry.get_all_manifests().items():
        installed_names.add(name)
        entry = {
            "name": name,
            "label": manifest.label or name,
            "description": _first_sentence(manifest.description or ""),
            "category": manifest.category,
            "installed": True,
            "skills": [s.id for s in manifest.skills],
        }
        if manifest.category == "skill":
            skill_packages.append(entry)
        else:
            mcps.append(entry)

    catalog_error = ""
    try:
        registry = await community_catalog.fetch_registry()
        for entry in registry.get("mcps", []):
            name = entry.get("name") or ""
            if not name or name in installed_names:
                continue
            mcps.append({
                "name": name,
                "label": entry.get("label") or name,
                "description": _first_sentence(entry.get("description") or ""),
                "category": "community",
                "installed": False,
                "skills": [],
            })
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RuntimeError) as exc:
        # Detail goes to the log only — exception text can carry paths/URLs
        # the response has no business echoing.
        catalog_error = "community MCP catalog unreachable"
        logger.warning("building-blocks: MCP catalog unreachable: %s", exc)

    try:
        skills_registry = await community_catalog.fetch_skills_registry()
        # The fetch answers an empty catalog flagged unreachable rather than
        # raising when it has no cached copy either.
        if skills_registry.get("catalog_unreachable") and not catalog_error:
            catalog_error = "community skills catalog unreachable"
        for entry in skills_registry.get("skills", []):
            name = entry.get("name") or ""
            if not name or name in installed_names:
                continue
            skill_packages.append({
                "name": name,
                "label": entry.get("label") or name,
                "description": _first_sentence(entry.get("description") or ""),
                "category": "skill",
                "installed": False,
                "skills": [],
            })
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RuntimeError) as exc:
        if not catalog_error:
            catalog_error = "community skills catalog unreachable"
        logger.warning("building-blocks: skills catalog unreachable: %s", exc)

    slugs = [a["slug"] for a in await asyncio.to_thread(agent_store.get_all_agents)]
    return {
        "mcps": sorted(mcps, key=lambda m: (not m["installed"], m["name"])),
        "skill_packages": sorted(
            skill_packages, key=lambda s: (not s["installed"], s["name"]),
        ),
        # The reserved ones too: a template naming one cannot be installed.
        "agent_slugs": sorted({*slugs, *agent_store.RESERVED_AGENT_SLUGS}),
        "check_examples": await asyncio.to_thread(_check_examples),
        "catalog_error": catalog_error,
    }


def _check_examples() -> list[dict]:
    """The checks-mcp's example folders: a template's ``checks/<name>/`` is
    one of these shapes, so an authoring agent copies the closest."""
    import config
    root = config.MCPS_DIR / "custom" / "checks-mcp" / "examples"
    out: list[dict] = []
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            doc = json.loads((d / "check.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        out.append({
            "name": d.name,
            "description": str(doc.get("description") or ""),
            "sections": [s for s in ("schema", "script", "handler", "judge") if doc.get(s)],
            "files": sorted(p.name for p in d.iterdir() if p.is_file()),
        })
    return out


# ---------------------------------------------------------------------------
# POST /v1/agents/local-template/validate
# ---------------------------------------------------------------------------

@router.post("/v1/agents/local-template/validate")
async def validate_local_template(
    body: LocalTemplateBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Dry-run a template folder. No side effects — validate as often as needed.

    Validation problems come back as ``ok: false`` + ``errors`` (the agent
    self-corrects from them); 4xx is reserved for authorization and path
    problems, which the agent cannot fix by editing files.
    """
    u = require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    from services.community import community_agent_installer as installer
    from storage.agents import agent_store

    try:
        template, snapshot = await _load_snapshot(u, body.path)
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("error") == "invalid_template":
            return {"ok": False, "errors": detail.get("problems") or [detail["message"]]}
        raise

    try:
        errors: list[str] = []
        mcp_plan: dict[str, list[str]] = {"ready": [], "needs_install": [], "needs_admin_request": []}
        skill_plan: dict[str, list[str]] = {"ready": [], "missing": [], "admin_required": []}

        try:
            await installer._preflight_check_mcps(template.mcps)
        except HTTPException as exc:
            detail = exc.detail
            missing: list = detail.get("missing", []) if isinstance(detail, dict) else []
            errors.append(
                "These MCPs are not installed and not in any catalog: "
                + ", ".join(str(m) for m in missing),
            )
        else:
            from services.mcp import mcp_registry
            installed = set(mcp_registry.get_all_manifests().keys())
            for req in template.mcps:
                if req.name in installed:
                    mcp_plan["ready"].append(req.name)
                elif u.is_admin:
                    mcp_plan["needs_install"].append(req.name)
                else:
                    mcp_plan["needs_admin_request"].append(req.name)

        try:
            await installer._preflight_check_skill_packages(
                template.skill_packages, u.role,
            )
        except HTTPException as exc:
            detail = exc.detail
            kind = detail.get("error") if isinstance(detail, dict) else ""
            names: list = detail.get("missing", []) if isinstance(detail, dict) else []
            if kind == "skills_require_admin":
                skill_plan["admin_required"] = [str(n) for n in names]
                errors.append(
                    "These skill packages must be installed by a platform "
                    "admin first: " + ", ".join(str(n) for n in names),
                )
            else:
                skill_plan["missing"] = [str(n) for n in names]
                errors.append(
                    "These skill packages are not in any catalog: "
                    + ", ".join(str(n) for n in names),
                )
        else:
            skill_plan["ready"] = [p.name for p in template.skill_packages]

        errors.extend(await asyncio.to_thread(_validate_apps, template))

        slug = agent_store.sanitize_slug(template.slug)
        slug_available = bool(slug) and agent_store.reserved_slug_refusal(slug) is None \
            and not await asyncio.to_thread(agent_store.agent_exists, slug)
        suggested = ""
        if slug and not slug_available:
            suggested = await asyncio.to_thread(installer._propose_free_slug, slug)

        return {
            "ok": not errors,
            "errors": errors,
            "slug": slug,
            "slug_available": slug_available,
            "suggested_slug": suggested,
            "mcp_plan": mcp_plan,
            "skill_plan": skill_plan,
            "seeds": {
                "tasks": len(template.tasks),
                "triggers": len(template.triggers),
                "notifications": len(template.notifications),
                "dashboards": len(template.dashboards),
                "context_files": len(template.context_files),
                "has_setup": template.setup_md is not None,
                "has_user_setup": template.user_setup_md is not None,
                "apps": [
                    {"slug": a.slug, "title": a.title, "visibility": a.visibility,
                     "owner_approval": a.owner_approval}
                    for a in template.apps
                ],
                "checks": [
                    {"name": c.name, "mandatory": c.mandatory} for c in template.checks
                ],
            },
        }
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


# ---------------------------------------------------------------------------
# POST /v1/agents/install-from-local-template
# ---------------------------------------------------------------------------

@router.post("/v1/agents/install-from-local-template")
async def install_from_local_template(
    body: InstallLocalTemplateBody,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Create a new agent from a locally authored template folder.

    The caller becomes the new agent's manager. Platform admins get MCPs
    installed inline; creators get the admin-approval request queue — the
    same split the catalog install path uses.
    """
    u = require_creator_or_admin(user, detail=_CREATOR_DETAIL)
    from services.community import community_agent_installer

    template, snapshot = await _load_snapshot(u, body.path)
    try:
        result = await community_agent_installer.install_from_extracted_template(
            template=template,
            target_slug=body.target_slug or template.slug,
            installer_user_sub=u.acting_sub,
            installer_role=u.role,
            source_label=f"local:{template.slug}",
            # Provenance: keeps a locally authored agent from matching a
            # catalog entry that happens to share its slug in Browse.
            template_ref=f"local:{template.slug}",
            # Attaching every future platform user is an admin decision.
            allow_default_for_new_users=u.is_admin,
        )
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)

    result["dashboard_path"] = f"/agents/{result['agent_slug']}"
    result.pop("agent", None)
    return result
