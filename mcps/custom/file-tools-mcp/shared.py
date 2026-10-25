"""Shared utilities for the file-tools MCP server.

Config, path mapping, LibreOffice runs, preview push helpers.
"""

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import signal
import tempfile
import time
import unicodedata
import weakref
from pathlib import Path

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
    """The one container tree this request may touch: the session's own agent
    folder under the mount, or, for a remote machine's file, the session's own
    host-cache folder (the cache is per session). None for anything else."""
    if slug == HOST_CACHE_SEGMENT:
        session_id = _session_id_var.get()
        return f"{MOUNT_AGENTS_DIR}/{HOST_CACHE_SEGMENT}/{session_id}" if session_id else None
    if _SAFE_SLUG_RE.match(slug or ""):
        return f"{MOUNT_AGENTS_DIR}/{slug}"
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

    Falls through with the original path when:
      - the path exists verbatim (fast path — no listdir)
      - the parent dir is missing
      - no normalized match is found (caller's open() raises a clear error)
    """
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return path
    basename = os.path.basename(path)
    if not basename:
        return path
    target_nfc = unicodedata.normalize("NFC", basename)
    try:
        for entry in os.listdir(parent):
            if unicodedata.normalize("NFC", entry) == target_nfc:
                return os.path.join(parent, entry)
    except OSError:
        pass
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

    The answer is confined to the CALLER'S tree: the slug the proxy echoes
    names the agent folder, or the session's own host-cache folder, and the
    resolved path must stay inside it (an answer that names no agent is
    refused: a slug read off the path would confine to whatever the answer
    says). The mount holds every agent's tree, so a prefix check on the
    mount alone would admit another agent's files; the write helpers then
    open the answer without following a link.
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
        rel = agents_rel.lstrip("/")
        allowed = _session_subtree(agent)
        resolved = str(await asyncio.to_thread(Path(MOUNT_AGENTS_DIR + "/" + rel).resolve))
        # The separator matters: a bare prefix test admits a sibling such as
        # /agents/proj-a-evil/x.
        if allowed and (resolved == allowed or resolved.startswith(allowed + "/")):
            return _unicode_match_on_disk(resolved)
        reason = "the path leaves this session's agent tree"

    # Surface the proxy's real verdict (policy reject / not reachable / out of
    # tree) instead of a generic message that masked the actual cause.
    raise ValueError(
        f"Cannot open '{path}': {reason}"
        if reason else
        f"Path could not be resolved: {path}"
    )


def _op_type(op: dict) -> str:
    """Extract operation type — LLMs may use 'op', 'operation', 'action' or
    'type'. An explicit op key wins: next to one, 'type' is a parameter of
    that operation (add_chart's chart type), not the dispatch key."""
    return op.get("op") or op.get("operation") or op.get("action") or op.get("type") or ""


def _normalize_operations(ops) -> tuple[list[dict], int]:
    """Normalize an operations argument to a list of dicts.

    Some LLMs double-encode array parameters as JSON strings. Accept any of:
    - list of dicts (correct shape)
    - list of JSON-encoded strings: ["{\"type\":\"resize\"}", ...]
    - JSON-encoded string of a list: "[{\"type\":\"resize\"}, ...]"
    - JSON-encoded string of a single op: "{\"type\":\"resize\"}"
    - single dict (wrap in list)

    Returns (operations, dropped): malformed items are dropped so other
    operations still run, but the count is reported — a caller that silently
    swallows them leaves the model believing everything was applied.
    """
    if ops is None:
        return [], 0
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except (json.JSONDecodeError, ValueError):
            return [], 1  # the whole blob was unparseable
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        return [], 1
    normalized: list[dict] = []
    dropped = 0
    for op in ops:
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except (json.JSONDecodeError, ValueError):
                dropped += 1
                continue
        if isinstance(op, dict):
            normalized.append(op)
        else:
            dropped += 1
    return normalized, dropped


def _dropped_note(dropped: int) -> str:
    """Result-message suffix reporting operations lost in normalization."""
    if not dropped:
        return ""
    return (
        f"\nWARNING: {dropped} malformed operation item(s) could not be parsed "
        "and were NOT applied (expected objects like {\"type\": \"...\", ...})."
    )


# ---------------------------------------------------------------------------
# The `help` op of write_docx and write_pptx (write_xlsx has its own, built
# from its key catalogue): the tool descriptions stay under the CLI's
# 2048-character cap and name every op; the parameters live here.
# ---------------------------------------------------------------------------

_HELP_OPS = frozenset({"help", "describe", "describe_ops", "catalog", "catalogue",
                       "list_ops", "?"})
_HELP_NAME_KEYS = ("name", "op_name", "operation_name", "topic")
HELP_POINTER = ' ({"op": "help"} lists every operation and its keys)'


def split_help_ops(ops: list[dict]) -> tuple[list[dict], list[dict]]:
    """(the help ops, every other op)."""
    def is_help(op: dict) -> bool:
        return str(_op_type(op)).strip().lower() in _HELP_OPS
    return [op for op in ops if is_help(op)], [op for op in ops if not is_help(op)]


def help_text(tool: str, groups: tuple, notes: str, help_ops: list[dict],
              aliases: dict[str, str] | None = None) -> str:
    """The answer to the help ops of one call: the whole catalogue, or one
    operation's keys when the op names it (an alias the dispatcher accepts
    answers for its operation). ``groups`` is
    ``((group, ((op, keys), ...)), ...)``."""
    entries = {name: keys for _, ops in groups for name, keys in ops}
    aliases = aliases or {}
    answers = []
    for op in help_ops:
        name = next((str(op[k]).strip() for k in _HELP_NAME_KEYS if op.get(k)), "")
        name = aliases.get(name, name)
        if not name:
            lines = [f'{tool} operations: each is {{"op": "<name>", ...keys}}; ? marks '
                     'an optional key. {"op": "help", "name": "<op>"} shows one.']
            for group, ops in groups:
                lines.append(f"\n{group}")
                lines.extend(f"  {op_name}: {keys}" for op_name, keys in ops)
            if notes:
                lines.append("\n" + notes)
            answers.append("\n".join(lines))
        elif name in entries:
            answers.append(f"{name}: {entries[name]}")
        else:
            answers.append(f"No operation named '{name}'. Valid operations: {', '.join(entries)}")
    return "\n\n".join(answers)


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
    parent, name = os.path.split(container_path)
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    for entry in entries:
        if safe_fs.is_partial_of(entry, name):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(parent, entry))


# ---------------------------------------------------------------------------
# Worker-core plumbing (see isolation.py)
# ---------------------------------------------------------------------------

# Parent-side path pre-resolution failures travel into sync worker cores as
# marker strings, so the cores' per-op error containment (errors.append +
# continue with the remaining ops) behaves exactly as the old inline awaits
# did — a bad path in one op must never fail the whole call.
_RESOLVE_ERR_PREFIX = "__RESOLVE_ERROR__:"


async def _resolve_or_mark(path_value: str, *, writing: bool = False) -> str:
    try:
        return await _resolve_path(path_value, writing=writing)
    except Exception as exc:  # noqa: BLE001  # a path that cannot be resolved becomes a marked error for the batch
        return _RESOLVE_ERR_PREFIX + (str(exc) or exc.__class__.__name__)


def _checked_resolved(value):
    """In-core: re-raise the parent's resolve failure at the op's use site."""
    if isinstance(value, str) and value.startswith(_RESOLVE_ERR_PREFIX):
        raise ValueError(value[len(_RESOLVE_ERR_PREFIX):])
    return value


async def _preresolve_image_ops(ops: list) -> None:
    """Canonicalize add_image's `image_path|path|image` aliases (all three
    write modules accept all three) to one pre-resolved `image_path`."""
    for op in ops:
        if _op_type(op) == "add_image":
            src = op.get("image_path") or op.get("path") or op.get("image", "")
            op.pop("path", None)
            op.pop("image", None)
            op["image_path"] = await _resolve_or_mark(src)


_WRITE_OP_ADVICE = (
    "Split the operation list into smaller calls or work on a smaller file"
)


# ---------------------------------------------------------------------------
# LibreOffice runs: one profile per slot, the slots by memory
# ---------------------------------------------------------------------------
#
# Two LibreOffice processes on one user profile cannot run at once (the
# second exits with no output), so each conversion slot has a profile of its
# own and a conversion waits for a free slot. The slots follow the parse
# workers' permits: two on a container of 3 GiB or more or with no memory
# limit, one on a smaller one, where a second soffice beside the warm
# background model and a parse worker would pass the container's memory. A
# conversion writes only into the output directory its caller made for it,
# so two conversions of one file run side by side.

_LO_TIMEOUT_S = 120
# How long a conversion waits for a free slot: past two whole runs, a slot
# that never comes back is an error the caller sees, never a hang.
_LO_SLOT_WAIT_S = 300

# The profile the headless conversions run under: formulas are never
# recalculated on load, links are never updated, macros never run, and a
# linked graphic is never loaded (an HTML image reference, an ODF or OOXML
# linked picture: LibreOffice 7.4 loads them from any path it can read
# unless BlockUntrustedRefererLinks is set; an embedded picture still
# renders). The document under conversion is untrusted input; LibreOffice
# 7.4 is inert on the first three by default and the profile pins them.
_LO_PROFILE_DIR = os.environ.get("FILETOOLS_LO_PROFILE", "/tmp/file-tools-lo-profile")
_LO_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
"""


def _lo_slot_count() -> int:
    from isolation import parse_permits

    return parse_permits()


_lo_free: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Queue]" = weakref.WeakKeyDictionary()
# The runs in flight: a run outlives a caller that stopped waiting.
_lo_runs: set[asyncio.Task] = set()


def _lo_free_slots() -> asyncio.Queue:
    """This loop's free slot numbers (one queue per event loop)."""
    loop = asyncio.get_running_loop()
    free = _lo_free.get(loop)
    if free is None:
        free = _lo_free[loop] = asyncio.Queue()
        for slot in range(_lo_slot_count()):
            free.put_nowait(slot)
    return free


def _libreoffice_profile(slot: int) -> str:
    """The user-installation directory of one slot's conversions, its
    registry seeded with the pins above (written once, rewritten on drift)."""
    profile = os.path.join(_LO_PROFILE_DIR, f"slot-{slot}")
    user = os.path.join(profile, "user")
    os.makedirs(user, exist_ok=True)
    xcu = os.path.join(user, "registrymodifications.xcu")
    try:
        current = open(xcu, encoding="utf-8").read()
    except OSError:
        current = ""
    if current != _LO_REGISTRY:
        with open(xcu, "w", encoding="utf-8") as fh:
            fh.write(_LO_REGISTRY)
    return profile


async def _libreoffice_convert(input_path: str, output_format: str, output_dir: str) -> str:
    """Convert a file with LibreOffice headless. Returns output path. The
    caller passes an ``output_dir`` outside the agent tree (a temp directory)
    and lands the result through ``safe_open_write``: LibreOffice writes by
    name and must never write into the tree itself.

    The run is a task of its own: a caller that stops waiting (the client
    went away) ends it, and the run still ends its processes and waits for
    them before it frees its slot."""
    run = asyncio.ensure_future(_libreoffice_run(input_path, output_format, output_dir))
    _lo_runs.add(run)
    run.add_done_callback(_lo_runs.discard)
    try:
        await asyncio.shield(run)
    except asyncio.CancelledError:
        run.cancel()
        raise
    stem = Path(input_path).stem
    out = Path(output_dir) / f"{stem}.{output_format}"
    if not out.exists():
        raise RuntimeError(f"Conversion produced no output file at {out}")
    return str(out)


async def _libreoffice_run(input_path: str, output_format: str, output_dir: str) -> None:
    free = _lo_free_slots()
    try:
        slot = await asyncio.wait_for(free.get(), _LO_SLOT_WAIT_S)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"LibreOffice is busy: no conversion slot came free within {_LO_SLOT_WAIT_S}s"
        ) from exc
    proc = None
    try:
        # The slot is held: a drift rewrite never lands while a run reads it.
        profile = await asyncio.to_thread(_libreoffice_profile, slot)
        # Its own process group: the wrapper starts oosplash and soffice.bin,
        # and ending the run ends all three.
        proc = await asyncio.create_subprocess_exec(
            "libreoffice", f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--convert-to",
            output_format, "--outdir", output_dir, input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_LO_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"LibreOffice conversion timed out ({_LO_TIMEOUT_S}s)") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"LibreOffice error: {stderr.decode()[:500]}")
    finally:
        # Ending the group and freeing the slot is one task of its own: a
        # cancel that arrives while a lingering group is ended (the person
        # stops a conversion that just timed out) leaves it running, and the
        # slot still comes back once the group is gone.
        cleanup = asyncio.ensure_future(_end_and_free(proc, free, slot))
        _lo_runs.add(cleanup)
        cleanup.add_done_callback(_lo_runs.discard)
        await asyncio.shield(cleanup)


async def _end_and_free(proc, free: asyncio.Queue, slot: int) -> None:
    try:
        if proc is not None and proc.returncode is None:
            await _end_process_group(proc)
    finally:
        free.put_nowait(slot)


async def _end_process_group(proc) -> None:
    """End a run's whole process group and wait, five seconds at most, until
    none of it is left, so the next run on the slot's profile never meets a
    dying instance. The grandchildren are reaped by the image's init."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        await asyncio.sleep(0.05)
    logger.warning("LibreOffice run %s: its processes outlived the 5 s wait", proc.pid)


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
