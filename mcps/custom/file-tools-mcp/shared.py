"""Shared utilities for the file-tools MCP server.

Config, path mapping, preview push helpers.
"""

import asyncio
import base64
import contextlib
import contextvars
import errno
import hashlib
import logging
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import BinaryIO

import httpx

import safe_fs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
# `/agents` is the canonical mount point inside file-tools containers (declared
# in docker-compose.yml). Hardcoded — no longer a config knob; the env var
# `MOUNT_AGENTS_DIR` is gone in v2.
MOUNT_AGENTS_DIR = "/agents"
# The pseudo-agent under the mount where the proxy caches a remote machine's
# files per session (``<mount>/.remote-host-cache/<session id>/...``).
HOST_CACHE_SEGMENT = ".remote-host-cache"
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
MCP_PORT = int(os.environ.get("MCP_PORT", "8932"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("file-tools")

# ---------------------------------------------------------------------------
# Per-request session binding (session_id + auth) via contextvars
# ---------------------------------------------------------------------------
#
# file-tools is a SHARED container serving every session of the install, so it
# can't hold a session-scoped credential in its env the way a per-session stdio
# MCP does. Instead the proxy injects, per session, a `?session_id=` URL param
# AND an `Authorization: Bearer <session-JWT>` header (see the platform's
# build_session_mcp_config + per-layer swap of the OTO_SESSION_JWT sentinel).
# We bind BOTH per request via contextvars.
#
# Why contextvars (not a module global): the streamable-HTTP transport runs in
# STATELESS mode (server.py: StreamableHTTPSessionManager(stateless=True)), so
# each request gets its own task group spawned AFTER the ASGI handler sets these
# — the values propagate to the tool handler and never bleed across concurrent
# sessions. A global would race two sessions onto one value; a ContextVar
# isolates per request and, crucially, fails CLOSED on any propagation gap
# (empty default → a clean "not session-bound" error, never another session).
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "file_tools_session_id", default=""
)
# Full `Authorization` header value the client sent (e.g. "Bearer eyJ..."),
# forwarded verbatim on every proxy callback. Empty when none was sent.
_auth_header_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "file_tools_auth", default=""
)


def set_request_context(session_id: str, auth_header: str) -> None:
    """Bind the in-flight request's session_id + Authorization header.

    Called at the transport boundary (server.py handle_sse / mcp_asgi_app)
    BEFORE the MCP SDK dispatches the tool handler.
    """
    _session_id_var.set(session_id or "")
    _auth_header_var.set(auth_header or "")


def _current_session() -> tuple[str, str]:
    """``(session_id, authorization_header)`` for the in-flight request."""
    return _session_id_var.get(), _auth_header_var.get()

# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------
#
# Inbound (LLM → file-tools): paths arrive as sandbox-virtual or agents-relative
# strings. We always ask the proxy's /v1/hooks/resolve-path to translate them
# to a canonical agents-relative path, then prepend `/agents/` for the mounted
# container view. The old direct host→container translation (HOST_AGENTS_DIR
# replacement) is gone — it broke remote-satellite sessions where the host path
# doesn't exist platform-side.
#
# Outbound (file-tools → proxy hooks): we post agents-relative paths to
# /v1/hooks/file*, which the hooks then translate per-target (local sandbox,
# remote satellite, plain host). See `_to_agents_relative` below.

# For satellite-homed sessions the resolve/file/file-written hooks perform a
# FULL synchronous file transfer over the satellite's WebSocket (lazy pull in,
# push-back out) before replying — over a WAN/tunnel link a few MB takes tens
# of seconds. The read budget must cover that transfer (the proxy's own pull
# budget is 180 s); connect stays short because the proxy itself is always
# platform-local to this container.
HOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=150.0, write=30.0, pool=5.0)


def _to_agents_relative(container_path: str) -> str:
    """Strip the `/agents/` mount prefix to produce an agents-relative path
    suitable for posting to /v1/hooks/file, /v1/hooks/document-preview, and
    /v1/hooks/file-written.

        /agents/personal-assistant/users/<user>/workspace/foo.docx
            → personal-assistant/users/<user>/workspace/foo.docx
    """
    if container_path.startswith(MOUNT_AGENTS_DIR + "/"):
        return container_path[len(MOUNT_AGENTS_DIR) + 1:]
    if container_path == MOUNT_AGENTS_DIR:
        return ""
    return container_path  # already agents-relative or out-of-tree


async def _resolve_via_proxy(path: str, writing: bool = False) -> tuple[str | None, str, str]:
    """Ask the proxy to translate a path to an agents-relative path.

    Returns ``(agents_relative, agent, "")`` on success (the agents-relative
    path is usable directly as a container path under MOUNT_AGENTS_DIR;
    ``agent`` is the slug the proxy echoes for the session, ``""`` from a
    proxy that does not send it). On failure returns ``(None, "", reason)``
    where ``reason`` carries the proxy's REAL verdict: a 403 policy reject (e.g. "outside the OS user's home directory;
    enable full filesystem access") or a 404 not-reachable, so the caller can
    surface it instead of a generic "within the agents directory" error that
    masked every cause (Issue C).

    ``writing`` marks a WRITE target (output/save path): the proxy then
    tolerates a missing file — on remote sessions a not-yet-existing output
    resolves to the platform creation path instead of failing the lazy pull.

    Async on purpose: this container serves EVERY session; a blocking
    httpx.post here (a remote pull can take minutes) stalls the shared
    event loop for all of them.
    """
    session_id, auth = _current_session()
    logger.info("_resolve_via_proxy: path=%s, session_id=%s, PROXY_URL=%s", path, session_id[:12] if session_id else '(empty)', PROXY_URL)
    if not session_id or not PROXY_URL or not auth:
        logger.warning("_resolve_via_proxy: skipping — session_id=%s, PROXY_URL=%s, auth=%s", 'empty' if not session_id else 'set', 'empty' if not PROXY_URL else 'set', 'empty' if not auth else 'set')
        return None, "", "file-tools is not session-bound (missing session_id/PROXY_URL/auth)"
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/resolve-path",
                json={"session_id": session_id, "path": path, "writing": writing},
                headers={"Authorization": auth},
            )
        if resp.status_code == 200:
            data = resp.json()
            agents_rel = data.get("agents_relative", "")
            if agents_rel:
                return agents_rel, str(data.get("agent") or ""), ""
            return None, "", (
                "proxy resolved the path but returned no agents-relative "
                "mapping (it is outside the synced agent tree)"
            )
        # Surface the proxy's real reason (403 policy reject / 404 not reachable).
        detail = ""
        try:
            detail = str(resp.json().get("detail", "")).strip()
        except (ValueError, AttributeError):
            detail = (resp.text or "")[:200].strip()
        return None, "", f"proxy resolve-path {resp.status_code}: {detail or '(no detail)'}"
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        logger.debug("resolve-path failed for '%s': %s", path, e)
        return None, "", f"resolve-path request failed: {e}"


def _session_subtree(slug: str) -> str | None:
    """The one tree this request may touch, as a rel under the mount: the
    session's own agent folder, or, for a remote machine's file, the
    session's own host-cache folder (the cache is per session). None for
    anything else."""
    if slug == HOST_CACHE_SEGMENT:
        session_id = _session_id_var.get()
        if not _SAFE_SLUG_RE.match(session_id or ""):
            return None
        return f"{HOST_CACHE_SEGMENT}/{session_id}"
    if _SAFE_SLUG_RE.match(slug or ""):
        return slug
    return None


def _answer_in_tree(agents_rel: str, agent: str) -> str | None:
    """The container path of the server's answer when its TEXT stays in the
    session's own tree, else None. Nothing on disk is consulted: the answer
    is link-free when given, every part must be a plain name (a ``..``, a
    ``.`` or an empty part is refused, never folded), and the open that
    follows refuses any link met on the way."""
    subtree = _session_subtree(agent)
    if subtree is None:
        return None
    try:
        rel = safe_fs.rel_under(MOUNT_AGENTS_DIR + "/" + agents_rel.lstrip("/"), MOUNT_AGENTS_DIR)
    except OSError:
        return None
    # The separator matters: a bare prefix test admits a sibling such as
    # proj-a-evil/x.
    if rel == subtree or rel.startswith(subtree + "/"):
        return MOUNT_AGENTS_DIR + "/" + rel
    return None


def _unicode_match_on_disk(path: str) -> str:
    """If ``path`` doesn't exist verbatim but a Unicode-normalized variant
    of its basename exists in the parent dir, return the matching on-disk
    path.

    Linux filesystems do byte-exact lookups, but LLM/JSON transports often
    normalize Unicode to NFC when echoing tool-result paths back, while
    files written by Google Drive / macOS / Slack / etc. may use NFD
    (decomposed form like ``ι`` + combining ``U+0301`` instead of precomposed
    ``ί``). Without this fallback, every non-ASCII filename round-tripped
    through a tool result becomes unreadable.

    Looked at beneath the write root with no link followed: whether the name
    is there is ``kind_beneath``'s answer, and the parent is listed through
    its own handle. Blocking: run it in a thread.

    Falls through with the original path when:
      - something sits at the path verbatim (fast path — no listing)
      - the path is not beneath the root, or its folder cannot be opened
        without a link (the strict open then answers)
      - no normalized match is found (caller's open() raises a clear error)
    """
    root = _write_root()
    try:
        rel = safe_fs.rel_under(path, root)
    except OSError:
        return path
    parent_rel, _, basename = rel.rpartition("/")
    if not basename or safe_fs.kind_beneath(root, rel).kind != "missing":
        return path
    target_nfc = unicodedata.normalize("NFC", basename)
    try:
        fd = safe_fs.open_dir_beneath(root, parent_rel)
    except OSError:
        return path
    try:
        entries = os.listdir(fd)
    except OSError:
        return path
    finally:
        os.close(fd)
    for entry in entries:
        if unicodedata.normalize("NFC", entry) == target_nfc:
            return path.rstrip("/").rpartition("/")[0] + "/" + entry
    return path


async def _resolve_path(path: str, writing: bool = False) -> str:
    """Resolve a tool-input path to a container-local path, validate it.

    Always uses the proxy's resolve-path API: the LLM's path string may be
    sandbox-virtual (`/users/alice/workspace/foo`), agents-relative
    (`personal-assistant/users/alice/...`), or already container-absolute
    (`/agents/...`) — the proxy normalizes them all to agents-relative.

    Pass ``writing=True`` for WRITE targets (output/save paths) so a
    not-yet-existing file resolves to its creation path on remote sessions
    instead of failing the lazy pull. Read-modify-write targets (e.g.
    ``write_docx`` on an existing doc) also use ``writing=True`` — the proxy
    still pulls the current satellite bytes first when the file exists.

    After prefix translation, falls back to a Unicode-normalized lookup in
    the parent dir if the exact path doesn't exist on disk (see
    ``_unicode_match_on_disk``).

    The answer is confined to the CALLER'S tree on its text: the slug the
    proxy echoes names the agent folder, or the session's own host-cache
    folder, and the answer must be that folder or below it, every part a
    plain name (an answer that names no agent is refused: a slug read off
    the path would confine to whatever the answer says). The mount holds
    every agent's tree, so a prefix check on the mount alone would admit
    another agent's files. The answer is never resolved again: the server
    answered the place every link led to, and the readers and writers open
    it beneath the mount without following a link, so a link put on the
    way afterwards is refused, never followed.
    """
    # A container-absolute path goes through the proxy like every other
    # form: the mount holds EVERY agent's tree, so the prefix alone says
    # nothing about this session's rights on the file (another agent, another
    # user's folder, a caller's config). Hand the proxy the agents-relative
    # form and let the session's path policy decide.
    if path == MOUNT_AGENTS_DIR:
        path = ""
    elif path.startswith(MOUNT_AGENTS_DIR + "/"):
        path = path[len(MOUNT_AGENTS_DIR) + 1:]

    agents_rel, agent, reason = await _resolve_via_proxy(path, writing=writing)
    if agents_rel and not agent:
        reason = "the proxy answered without the session's agent"
    elif agents_rel:
        container = _answer_in_tree(agents_rel, agent)
        if container is not None:
            return await asyncio.to_thread(_unicode_match_on_disk, container)
        reason = "the path leaves this session's agent tree"

    # Surface the proxy's real verdict (policy reject / not reachable / out of
    # tree) instead of a generic message that masked the actual cause.
    raise ValueError(
        f"Cannot open '{path}': {reason}"
        if reason else
        f"Path could not be resolved: {path}"
    )


# ---------------------------------------------------------------------------
# Writing an output: beneath the mount, no link followed
# ---------------------------------------------------------------------------
#
# A resolved output path was judged by the proxy and confined above, but the
# name can be swapped for a link before the write lands. Every write sink
# therefore hands its bytes to ``safe_open_write``: a temp created beside the
# name inside the parent's own handle and renamed onto it there (``safe_fs``,
# the proxy helper's copy), so a link at the name is replaced, never written
# through, and a link at any component refuses the write. The cores run in
# spawn children with no session state; the root is read at call time.

def _write_root() -> str:
    """The root every output opens beneath: the mount. ``FILETOOLS_WRITE_ROOT``
    points the suite at a temporary tree (a spawn child inherits it)."""
    return os.environ.get("FILETOOLS_WRITE_ROOT") or MOUNT_AGENTS_DIR


def _rel_beneath_root(container_path: str) -> tuple[str, str]:
    root = _write_root()
    try:
        return root, safe_fs.rel_under(container_path, root)
    except OSError:
        raise PermissionError(
            f"Cannot write '{_to_agents_relative(container_path)}': outside the writable tree"
        ) from None


@contextlib.contextmanager
def safe_open_write(container_path: str):
    """A binary file to write the output at ``container_path`` into; on a
    clean exit the bytes replace the name atomically beneath the mount with
    no component followed (a failure leaves no temp behind)."""
    root, rel = _rel_beneath_root(container_path)
    try:
        with safe_fs.atomic_writer(root, rel, mkdirs=True) as fh:
            yield fh
    except safe_fs.SafeFsError as exc:
        raise PermissionError(
            f"Cannot write '{_to_agents_relative(container_path)}': {exc.strerror}"
        ) from None


# ---------------------------------------------------------------------------
# Reading an input: the answer opened beneath the mount, no link followed
# ---------------------------------------------------------------------------
#
# A person's file is the server's answer, link-free when given; between the
# answer and the open a folder on the way can be swapped for a link. Every
# reader of a person's path opens it here, beneath the write root with no
# component followed; a pipe or a device is refused at once. The tool's own
# files outside the mount (its temp copies and renders) open plainly: that
# is decided on the path's text alone (``rel_under`` refuses it), never by
# looking at the disk.

def _read_place(path: str | os.PathLike[str]) -> tuple[str, str] | None:
    """``(root, rel)`` for a path beneath the write root; None for a
    normalized absolute path outside it (the tool's own file). Anything
    else (a dot part, a relative path) raises ``SafeFsError``."""
    root = _write_root()
    try:
        return root, safe_fs.rel_under(path, root)
    except safe_fs.EscapeRefused:
        text = os.fspath(path)
        if os.path.isabs(text) and os.path.normpath(text) == text:
            return None
        raise


def _read_refused(path: str | os.PathLike[str], exc: safe_fs.SafeFsError) -> PermissionError:
    return PermissionError(f"Cannot read '{_to_agents_relative(os.fspath(path))}': {exc.strerror}")


def safe_open_read(path: str | os.PathLike[str]) -> BinaryIO:
    """``path`` opened for reading as a binary file: a person's file beneath
    the mount with no component followed through a link (refused with
    "Cannot read '<path>': <why>"), the tool's own temp outside it plainly.
    A missing file stays ``FileNotFoundError``. Blocking: a thread or a
    worker reads it."""
    try:
        place = _read_place(path)
        if place is None:
            return open(path, "rb")
        return safe_fs.open_file_for_read(*place)
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None


@contextlib.contextmanager
def reading(source: str | os.PathLike[str] | BinaryIO):
    """``source`` as a binary file, for a reader handed either: a path is
    opened with ``safe_open_read`` and closed on exit; a file the caller
    holds (one handle read several times) is rewound and left open."""
    if isinstance(source, (str, os.PathLike)):
        with safe_open_read(source) as fh:
            yield fh
        return
    source.seek(0)
    yield source


def safe_read_fd(path: str | os.PathLike[str]) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` of ``path`` opened as ``safe_open_read`` does, for a
    library that needs a name: hand it ``safe_fs.fd_path(fd)``. The caller
    closes ``fd``."""
    try:
        place = _read_place(path)
        if place is None:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                return fd, os.fstat(fd)
            except BaseException:
                os.close(fd)
                raise
        return safe_fs.open_regular_for_read(*place)
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None


def safe_kind(path: str | os.PathLike[str]) -> safe_fs.NameKind:
    """What sits at ``path`` with no link followed (``kind_beneath``
    beneath the mount; the tool's own file outside it by its ``lstat``):
    the status check in front of a read."""
    try:
        place = _read_place(path)
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None
    if place is not None:
        return safe_fs.kind_beneath(*place)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return safe_fs.NameKind("missing", None)
    except OSError:
        return safe_fs.NameKind("other", None)
    if stat.S_ISREG(st.st_mode):
        return safe_fs.NameKind("file", st)
    return safe_fs.NameKind("dir" if stat.S_ISDIR(st.st_mode) else "other", st)


def private_copy(path: str | os.PathLike[str], folder: str, max_size: int) -> str:
    """A copy of ``path`` read through ``safe_open_read`` at
    ``folder/<its name>`` (a folder of the call's own), refused past
    ``max_size`` bytes at the open and while copying
    (``safe_fs.FileTooLarge``): what a program the tool starts reads instead
    of the person's file."""
    target = os.path.join(folder, os.path.basename(os.fspath(path)))
    with safe_open_read(path) as src:
        size = os.fstat(src.fileno()).st_size
        if size > max_size:
            raise safe_fs.FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(path))
        try:
            with open(target, "xb") as dst:
                safe_fs.copy_fd(src, dst, max_size=max_size)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
    return target


def sibling_path(caller_path: str, suffix: str) -> str:
    """The caller's own path string with the suffix of its LAST part changed
    to ``suffix`` (appended when that part has none, as ``Path.suffix``
    reads one): a converted copy's or a default output's name, asked of the
    server as a write in the form the caller gave. Both ``/`` and ``\\``
    end a part, so a dot in a folder's name (a Windows folder's too) is
    never cut."""
    text = caller_path.rstrip("/\\")
    cut = max(text.rfind("/"), text.rfind("\\")) + 1
    head, name = text[:cut], text[cut:]
    dot = name.rfind(".")
    stem = name[:dot] if 0 < dot < len(name) - 1 else name
    return head + stem + suffix


def safe_mkdirs(container_dir: str) -> None:
    """Create ``container_dir`` and its missing parents beneath the mount, none
    of them reached through a link."""
    root, rel = _rel_beneath_root(container_dir)
    if not rel:
        return
    try:
        safe_fs.mkdirs_beneath(root, rel)
    except safe_fs.SafeFsError as exc:
        raise PermissionError(
            f"Cannot create '{_to_agents_relative(container_dir)}': {exc.strerror}"
        ) from None


def worker_temp_path(container_path: str, suffix: str) -> str:
    """The one temp in the system temp dir a worker writing ``container_path``
    may use for a library that writes by name outside the tree: named after
    the output, so the parent's ``cleanup_partials`` finds it after a kill
    the worker's own ``finally`` never saw. Two workers on one output share
    the name, as they share the output."""
    digest = hashlib.sha256(container_path.encode("utf-8")).hexdigest()[:24]
    return os.path.join(tempfile.gettempdir(), f"file-tools-{digest}{suffix}")


def cleanup_partials(container_path: str) -> None:
    """Remove the temps a killed or failed worker left beside
    ``container_path`` (``.<name>.<hex>.partial``, the name cut to safe_fs's
    temp length) and its temp in the system temp dir; best-effort."""
    with contextlib.suppress(OSError):
        os.unlink(worker_temp_path(container_path, ".pdf"))
    root = _write_root()
    try:
        parent_rel, _, name = safe_fs.rel_under(container_path, root).rpartition("/")
        fd = safe_fs.open_dir_beneath(root, parent_rel)
    except OSError:
        return
    try:
        # Listed and removed through the folder's own handle: a link put in
        # place of the folder is never followed.
        for entry in os.listdir(fd):
            if name and safe_fs.is_partial_of(entry, name):
                with contextlib.suppress(OSError):
                    os.unlink(entry, dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Worker-core plumbing (see isolation.py)
# ---------------------------------------------------------------------------

_WRITE_OP_ADVICE = (
    "Split the operation list into smaller calls or work on a smaller file"
)


# ---------------------------------------------------------------------------
# Preview push helpers
# ---------------------------------------------------------------------------


async def _notify_file_written(file_path: str) -> bool:
    """Tell the proxy that we just finished writing a file.

    For remote agent sessions, the proxy uses this signal to push the file
    from its platform-side cache back to the satellite so the agent CLI and
    downstream MCPs on the satellite see the updated content. On a local
    session the proxy pushes nothing (it answers `local: true`) and, like
    on a remote one, announces the change with `file_updated` unless the
    file still holds the bytes its newest push's document knows.
    Fire-and-forget — the tool's success is independent of sync success.
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return False
    agents_rel = _to_agents_relative(file_path)
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/file-written",
                json={"session_id": session_id, "path": agents_rel},
                headers={"Authorization": auth},
            )
            if resp.status_code == 200:
                return bool(resp.json().get("ok"))
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("file-written notify failed (non-fatal): %s", exc)
    return False


async def _push_preview(file_path: str, filename: str | None = None):
    """Push a document preview event to the dashboard via proxy hook.

    Also notifies the proxy that the file was written first, so a remote
    session's machine holds the new bytes before the preview, and every
    open view of the file hears of the change.
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    # Flush any platform-cache write back to the remote satellite before
    # the preview loads — otherwise the dashboard's download link could
    # race with the sync and serve stale bytes.
    await _notify_file_written(file_path)
    agents_rel = _to_agents_relative(file_path)
    fname = filename or Path(file_path).name
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/document-preview",
                json={
                    "session_id": session_id,
                    "file_path": agents_rel,
                    "filename": fname,
                },
                headers={"Authorization": auth},
            )
    except httpx.HTTPError as exc:
        logger.warning("Preview push failed (non-fatal): %s", exc)


# Dashboard previews never need print resolution — a multi-MB base64 body
# just slows the hook hop (same payload discipline as screenshot_document).
_PREVIEW_MAX_BYTES = int(1.5 * 1024 * 1024)
_PREVIEW_LONG_EDGE = 2000


async def _push_image_preview(
    image_bytes: bytes, mime: str, caption: str = ""
):
    """Push an inline image preview to the dashboard."""
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    if len(image_bytes) > _PREVIEW_MAX_BYTES:
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((_PREVIEW_LONG_EDGE, _PREVIEW_LONG_EDGE))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            image_bytes, mime = buf.getvalue(), "image/jpeg"
        except Exception:  # noqa: BLE001  # Pillow on the tool's image raises anything from a decoder
            pass  # best-effort — an oversized preview beats none
    b64 = base64.b64encode(image_bytes).decode()
    # Posts a 1-item gallery — the unified /v1/hooks/images endpoint renders
    # single images identically to the old /v1/hooks/image flow.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/images",
                json={
                    "session_id": session_id,
                    "images": [{
                        "image_data": b64,
                        "mime_type": mime,
                        "caption": caption,
                    }],
                },
                headers={"Authorization": auth},
            )
    except httpx.HTTPError as exc:
        logger.warning("Image preview push failed (non-fatal): %s", exc)
