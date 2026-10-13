"""Pictures and files an MCP tool returns, kept in the workspace and shown in
the chat.

A manifest names the tools whose results matter to the person
(``result_files: {"tools": [...], "subdir": "maps"}``, MCP-FRAMEWORK.md
"Result files"). For such a tool each picture (an MCP ``image`` block) and
each embedded resource blob of the result is saved under ``subdir`` of the
session's workspace and shown in the chat the way ``display_images`` and
``send_file`` show theirs, with the result's text as the caption; the agent
gets one line naming the paths. Nothing else is parsed: base64 inside a
server's own JSON text stays the server's business.

The three engines hand the result over in three places, and each calls
``keep`` once per result: the Claude CLI's PostToolUse hook
(``/v1/hooks/tool-result``, whose answer carries the line back to the
agent; the CLI hands a hook pictures only, it saves any other blob itself),
the Codex app-server's completed ``mcpToolCall`` item (the pump's
TOOL_RESULT branch; the agent is not told) and the Direct LLM loop (the line
joins the tool's text; an Anthropic model also sees the pictures).

Every check is here: the opt-in, the caps, the decode check of a picture,
the folder the session's own write rights admit, the file name (the tool's,
never the model's text). ``keep`` never raises to its caller.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ws import wire_events as wire

logger = logging.getLogger("otodock.result-files")

# One file's limit: the figure image-search-mcp's ``save_image`` downloads
# with (its own process; the number is shared, not the constant).
MAX_FILE_BYTES = 15 * 1024 * 1024
# A result's limits: past either, the remaining blocks reach the model only.
MAX_RESULT_BYTES = 30 * 1024 * 1024
MAX_FILES_PER_RESULT = 8
# A picture decoded whole for its chat copy (the pasted-photo rule).
MAX_PICTURE_PIXELS = 50_000_000
# The chat copies ride inline in the chat's history (as display-mcp's do):
# the longest side display-mcp keeps, a small picture as it came (a map's
# text stays sharp), an animation as it came up to its limit, a large copy
# as JPEG, and at most this much of a result's copies in the chat.
CHAT_COPY_MAX_DIM = 1568
CHAT_COPY_AS_IS_BYTES = 1536 * 1024
CHAT_COPY_ANIMATION_BYTES = 3 * 1024 * 1024
CHAT_COPY_PNG_BYTES = 2 * 1024 * 1024
CHAT_INLINE_BUDGET = 8 * 1024 * 1024
# How long ``keep`` waits for a paired machine to take the files; the push
# runs on after that, and a file it could not deliver reaches the machine at
# its next sync (the platform copy is the newer).
PUSH_WAIT_S = 4.0
CAPTION_MAX = 300
LINE_MAX = 2000
# One result decodes at a time per pair: a 50 MP picture is ~200 MB decoded.
_DECODES = asyncio.Semaphore(2)

#: Decoded picture format → (mime, extension). Nothing else is a picture.
PICTURE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "MPO": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}
# MCP content block kinds (the MCP schema's, not the platform's frames).
_TEXT = "text"
_IMAGE = "image"
_RESOURCE = "resource"
_BINARY_KINDS = (_IMAGE, _RESOURCE)
_IMAGE_MIME_PREFIX = "image/"
_MCP_TOOL_PREFIX = "mcp__"


def _split_mcp_tool(tool_name: str) -> tuple[str, str] | None:
    """``(server key, tool)`` of ``mcp__<server key>__<tool>``, the name every
    engine gives an MCP tool: the key ends at the first ``__`` after its
    first character, both halves non-empty. A loop, not a regex: a regex
    with two open-ended groups backtracks over a long run of underscores."""
    if not tool_name.startswith(_MCP_TOOL_PREFIX):
        return None
    rest = tool_name[len(_MCP_TOOL_PREFIX):]
    cut = rest.find("__", 1)
    if cut < 1 or cut + 2 >= len(rest):
        return None
    return rest[:cut], rest[cut + 2:]

# The texts the Claude CLI adds to an MCP result it hands the model (CLI
# 2.1.289): the pointer after an image (``[Image: source: <config dir>/…/
# tool-results/mcp-<server>-blob-….png]``) and the line before a resource's
# picture (``[Resource from <server> at <uri>]``). The CLI's, not the
# server's: never a caption.
_CLI_TEXT_RE = re.compile(
    r"^\[(Image: source: [^\]\n]*tool-results[/\\][^\]\n]*"
    r"|Resource from [^\]\n]* at [^\]\n]*)\]$")
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_TOOL_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_WS_RE = re.compile(r"\s+")
_NAME_TRIES = 20

# Push tasks still running past ``PUSH_WAIT_S`` (held so they are not
# collected mid-flight).
_PUSHES: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Blob:
    """One binary block of a tool result, engine-neutral. ``data`` is the
    base64 text as delivered, empty for a block the hook script left behind
    (``skipped_bytes`` says how large it was)."""
    kind: str               # "image" | "resource"
    mime: str
    data: str
    name_hint: str = ""
    skipped_bytes: int = 0


@dataclass
class Kept:
    """What ``keep`` did: the line for the agent, the chat copies of the
    pictures (``{"base64", "media_type"}``, for a model that sees them) and
    the saved paths as the session names them."""
    line: str
    vision: list[dict] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)


@dataclass
class _Outcome:
    blob: Blob
    picture: bool = False
    rel: str = ""                 # agents-root relative, when written
    shown_as: str = ""            # the session's own path form
    chat_b64: str = ""
    chat_mime: str = ""
    reason: str = ""              # why it was not kept, or not written
    not_in_chat: str = ""         # why a kept picture is not in the chat
    in_chat: bool = False         # a gallery or card for it reached the chat


# ---------------------------------------------------------------------------
# The engines' shapes
# ---------------------------------------------------------------------------

def blobs_of(blocks) -> list[Blob]:
    """The binary blocks of a tool result, in order. Takes the MCP shapes
    (``{"type": "image", "data", "mimeType"}``, ``{"type": "resource",
    "resource": {"uri", "mimeType", "blob"}}``), the Claude CLI's
    (``{"type": "image", "source": {"type": "base64", "media_type",
    "data"}}``) and the hook script's stand-in for a block it did not send
    (``{"type": …, "skipped_bytes": n}``). A text resource, a link or any
    other block is not a file."""
    out: list[Blob] = []
    for b in blocks or []:
        if not isinstance(b, dict) or b.get("type") not in _BINARY_KINDS:
            continue
        kind = b["type"]
        skipped = b.get("skipped_bytes")
        if isinstance(skipped, int) and skipped > 0:
            out.append(Blob(kind=kind, mime=str(b.get("mime") or ""), data="",
                            skipped_bytes=skipped))
        elif kind == _IMAGE:
            src = b.get("source")
            if isinstance(src, dict) and src.get("type") == "base64":
                data, mime = src.get("data"), src.get("media_type")
            else:
                data, mime = b.get("data"), b.get("mimeType")
            if isinstance(data, str) and data:
                out.append(Blob(kind=kind, mime=str(mime or ""), data=data))
        else:
            res = b.get("resource")
            data = res.get("blob") if isinstance(res, dict) else None
            if isinstance(data, str) and data:
                hint = res.get("name") if isinstance(res.get("name"), str) else ""
                out.append(Blob(kind=kind, mime=str(res.get("mimeType") or ""),
                                data=data, name_hint=hint or str(res.get("uri") or "")))
    return out


def text_of(blocks) -> str:
    """The result's own text: its text blocks, the Claude CLI's own lines
    left out."""
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == _TEXT:
            t = b.get(_TEXT)
            if isinstance(t, str) and t and not _CLI_TEXT_RE.match(t.strip()):
                parts.append(t)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------

def opted_in(tool_name: str):
    """``(manifest, tool)`` when ``mcp__<server key>__<tool>`` belongs to an
    MCP whose ``result_files`` names the tool, else None. The server key is
    the manifest's ``server_name`` (Codex's sanitised form of it too); the
    manifest the key names decides, never another MCP's list."""
    named = _split_mcp_tool(tool_name or "")
    if named is None:
        return None
    key, tool = named
    from core.layers.codex.tool_names import sanitized_server_name
    from services.mcp import mcp_registry
    exact = sanitized = None
    for m in mcp_registry.get_all_manifests().values():
        server = m.server_name or m.name
        if server == key:
            exact = m
            break
        if sanitized is None and sanitized_server_name(server) == key:
            sanitized = m
    manifest = exact or sanitized
    decl = getattr(manifest, "result_files", None) if manifest is not None else None
    if decl is None or tool not in decl.tools:
        return None
    return manifest, tool


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------

async def keep(session_id: str, tool_name: str, blobs: list[Blob], text: str = "", *,
               is_error: bool = False, wait_for_push: bool = True) -> Kept | None:
    """Save and show the binary blocks of one result of ``tool_name``.
    None when the tool is not opted in, the call failed, the result holds
    no binary block, or the session is unknown: the caller then changes
    nothing. ``wait_for_push`` holds the answer up to ``PUSH_WAIT_S`` while
    a paired machine takes the files (the agent reads the line next); a
    caller with no line to answer lets the push run on."""
    if not blobs or is_error:
        return None
    hit = opted_in(tool_name)
    if hit is None:
        return None
    manifest, tool = hit
    from core.session.session_state import get_session_security
    from services.mcp.mcp_manifest_parse import result_files_subdir_ok
    ctx = get_session_security(session_id)
    subdir = manifest.result_files.subdir
    if ctx is None or not result_files_subdir_ok(subdir):
        return None
    try:
        async with _DECODES:
            outcomes = await asyncio.to_thread(_save, ctx, subdir, tool, blobs)
        await _push(session_id, ctx, outcomes, wait=wait_for_push)
        await _show(session_id, ctx, outcomes, text)
        return _kept(outcomes)
    except Exception:
        logger.exception("result files of %s in %s failed", tool_name, session_id[:8])
        return None


async def keep_blocks(session_id: str, tool_name: str, blocks, text: str = "", *,
                      is_error: bool = False) -> Kept | None:
    """``keep`` for a caller holding the raw content blocks and no line to
    answer (the pump on a Codex result): the push to a paired machine runs
    on without holding the stream."""
    return await keep(session_id, tool_name, blobs_of(blocks), text,
                      is_error=is_error, wait_for_push=False)


def _save(ctx, subdir: str, tool: str, blobs: list[Blob]) -> list[_Outcome]:
    """Decode, check, copy for the chat and write (a worker thread). Each
    block gets an outcome; a block past a cap or failing the check is left
    to the model."""
    import config
    from services.mcp.mcp_output_relocation import session_workspace_dir, session_workspace_shown
    agents_root = Path(os.path.realpath(config.AGENTS_DIR))
    dest_dir = agents_root / safe_rel(session_workspace_dir(ctx), config.AGENTS_DIR) / subdir
    shown_dir = f"{session_workspace_shown(ctx)}/{subdir}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tool_part = _TOOL_UNSAFE.sub("_", tool)[:64] or "tool"
    no_write = _write_refusal(ctx, dest_dir / f"{tool_part}.bin")
    folder_made = False
    outcomes: list[_Outcome] = []
    total = kept = inline = 0
    for blob in blobs:
        o = _Outcome(blob=blob)
        outcomes.append(o)
        size = blob.skipped_bytes or _decoded_size(blob.data)
        if kept >= MAX_FILES_PER_RESULT:
            o.reason = f"over the {MAX_FILES_PER_RESULT} files a result may keep"
            continue
        if size > MAX_FILE_BYTES:
            o.reason = f"{_mb(size)}, over the {_mb(MAX_FILE_BYTES)} limit"
            continue
        if total + size > MAX_RESULT_BYTES:
            o.reason = f"over the {_mb(MAX_RESULT_BYTES)} a result may keep"
            continue
        raw = _decode(blob.data)
        if raw is None:
            o.reason = "not valid base64"
            continue
        checked = _check_picture(raw) if (
            blob.kind == _IMAGE or blob.mime.lower().startswith(_IMAGE_MIME_PREFIX)) else None
        if isinstance(checked, tuple):
            ext = checked[1]
            o.picture = True
            o.chat_b64, o.chat_mime = _chat_copy(raw, checked[0])
            if inline + len(o.chat_b64) > CHAT_INLINE_BUDGET:
                o.chat_b64 = o.chat_mime = ""
                o.not_in_chat = "past what one result shows in the chat"
            inline += len(o.chat_b64)
            tail = ""
        elif blob.kind == _IMAGE:
            o.reason = checked or "not a png, jpeg, webp or gif picture"
            continue
        else:
            # A blob, an image/* one Pillow does not keep (svg, tiff) too.
            hint = _name_hint(blob.name_hint)
            stem, suffix = os.path.splitext(hint)
            ext = suffix if _EXT_RE.match(suffix) else _ext_for(blob.mime)
            tail = f"-{stem}" if stem else ""
        total += len(raw)
        kept += 1
        if not no_write and not folder_made:
            no_write = _make_folder(agents_root, dest_dir)
            folder_made = True
        if no_write:
            o.reason = no_write
            continue
        written, why = _write_new(agents_root, dest_dir, tool_part, stamp, tail, ext, raw)
        if not written:
            o.reason = why
            continue
        o.rel = safe_rel(dest_dir / written, agents_root)
        o.shown_as = f"{shown_dir}/{written}"
    return outcomes


def _write_refusal(ctx, probe: Path) -> str:
    """Why this session may not write its workspace folder, or ""."""
    from auth.path_policy import check_host_path_access
    if getattr(ctx, "read_only", False):
        return "a check's judge keeps no files"
    if not check_host_path_access(probe, ctx, writing=True).allowed:
        return "this session cannot write its workspace"
    return ""


def _decoded_size(data: str) -> int:
    n = len(data)
    return n * 3 // 4 - data[-2:].count("=") if n else 0


def _decode(data: str) -> bytes | None:
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        return None


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _check_picture(raw: bytes):
    """``(mime, ext)`` of a picture this platform keeps, or the reason it is
    not one: the decoded format decides, never the declared mime."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as img:
            fmt = (img.format or "").upper()
            if fmt not in PICTURE_FORMATS:
                return "not a png, jpeg, webp or gif picture"
            w, h = img.size
            if w * h > MAX_PICTURE_PIXELS:
                return f"{w}x{h} is too many pixels"
            img.verify()
    except Exception:
        return "not a png, jpeg, webp or gif picture"
    return PICTURE_FORMATS[fmt]


def _chat_copy(raw: bytes, mime: str) -> tuple[str, str]:
    """The picture as the chat carries it (base64 inline, as display-mcp's
    ``_resize_and_encode`` does): small ones and short animations as they
    came, else the first frame with its longest side cut to 1568, PNG with
    transparency (JPEG when that is still large), JPEG q85 without."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as img:
        w, h = img.size
        animated = bool(getattr(img, "is_animated", False))
        if animated and len(raw) <= CHAT_COPY_ANIMATION_BYTES:
            return base64.b64encode(raw).decode("ascii"), mime
        if not animated and max(w, h) <= CHAT_COPY_MAX_DIM and len(raw) <= CHAT_COPY_AS_IS_BYTES:
            return base64.b64encode(raw).decode("ascii"), mime
        img.seek(0)
        frame = ImageOps.exif_transpose(img)
        if max(frame.size) > CHAT_COPY_MAX_DIM:
            frame.thumbnail((CHAT_COPY_MAX_DIM, CHAT_COPY_MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        if frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info:
            frame.convert("RGBA").save(buf, format="PNG", optimize=True)
            if buf.tell() <= CHAT_COPY_PNG_BYTES:
                return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png"
            buf = io.BytesIO()
        frame.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _name_hint(value: str) -> str:
    """A resource's own name for its file: the last segment of its uri
    path (or its ``name``), cut to safe characters, no leading dot, at most
    80 characters with its extension kept."""
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.netloc:
        path = parts.path if parts.path.strip("/") else parts.netloc
    else:
        path = value
    last = ""
    for seg in re.split(r"[/\\]", unquote(path)):
        if seg.strip():
            last = seg.strip()
    last = _NAME_UNSAFE.sub("_", last).lstrip("._-")
    if len(last) > 80:
        suffix = os.path.splitext(last)[1]
        suffix = suffix if _EXT_RE.match(suffix) else ""
        last = last[:80 - len(suffix)] + suffix
    return last


def _ext_for(mime: str) -> str:
    ext = mimetypes.guess_extension((mime or "").split(";")[0].strip().lower()) or ""
    return ext if _EXT_RE.match(ext) else ".bin"


def safe_rel(path: Path, root: Path) -> str:
    from services.infra import safe_fs
    return safe_fs.rel_under(str(path), str(root))


def _make_folder(root: Path, dest_dir: Path) -> str:
    """Make the result's folder once; "" or the reason no file goes there."""
    from services.infra import safe_fs
    rel_dir = safe_rel(dest_dir, root)
    try:
        safe_fs.mkdirs_beneath(root, rel_dir)
    except OSError as e:
        logger.warning("result files folder %s not made: %s", rel_dir, e)
        return f"the folder {dest_dir.name} could not be made"
    return ""


def _write_new(root: Path, dest_dir: Path, tool: str, stamp: str, tail: str,
               ext: str, raw: bytes) -> tuple[str, str]:
    """Write ``raw`` into ``dest_dir`` as ``<tool>-<stamp>[-n]<tail><ext>``,
    never over an existing name (probed first, the write itself exclusive).
    ``(name, "")``, or ``("", reason)``."""
    from services.infra import safe_fs
    rel_dir = safe_rel(dest_dir, root)
    for n in range(1, _NAME_TRIES + 1):
        name = f"{tool}-{stamp}{'' if n == 1 else f'-{n}'}{tail}{ext}"
        try:
            safe_fs.lstat_beneath(root, f"{rel_dir}/{name}")
            continue
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            safe_fs.atomic_write_beneath(root, f"{rel_dir}/{name}", raw, exclusive=True)
            return name, ""
        except FileExistsError:
            continue
        except OSError as e:
            logger.warning("result file %s/%s not written: %s", rel_dir, name, e)
            return "", "the file could not be written"
    return "", "no free file name"


async def _push(session_id: str, ctx, outcomes: list[_Outcome], *, wait: bool) -> None:
    """A paired machine's session gets the files in its synced folder."""
    rels = [o.rel for o in outcomes if o.rel]
    if not rels:
        return
    from core.remote import remote_file_flow
    if not remote_file_flow.is_remote_session(session_id):
        return
    prefix = f"{ctx.agent}/"
    tasks = []
    for rel in rels:
        if not rel.startswith(prefix):
            continue
        task = asyncio.create_task(remote_file_flow.push_back(session_id, rel[len(prefix):]))
        _PUSHES.add(task)
        task.add_done_callback(_PUSHES.discard)
        tasks.append(task)
    if tasks and wait:
        await asyncio.wait(tasks, timeout=PUSH_WAIT_S)


async def _show(session_id: str, ctx, outcomes: list[_Outcome], text: str) -> None:
    """One gallery for the result's pictures, one card per other file, on
    the queue of the pump that renders the session (a meeting
    participant's goes to the meeting's)."""
    from core.session.session_state import get_meeting_session_info, get_permission_queue
    from services.sharing.chat_snapshot import redact_tokens
    info = get_meeting_session_info(session_id)
    queue = get_permission_queue(info["pump_session_id"] if info else session_id)
    clean = redact_tokens(text)
    caption, link = _caption(clean), _link(clean)
    pictures = [o for o in outcomes if o.picture and o.chat_b64]
    if pictures:
        await queue.put({"event_type": wire.IMAGES, "images": [{
            "url": "", "image_data": o.chat_b64, "mime_type": o.chat_mime,
            "caption": caption if i == 0 else "",
            "attribution": f"Saved as {o.shown_as}" if o.shown_as else "",
            "link_url": link, "download_url": "",
        } for i, o in enumerate(pictures)]})
        for o in pictures:
            o.in_chat = True
    for o in outcomes:
        if o.picture or not o.rel:
            continue
        card = await _file_card(session_id, ctx, o, caption, info)
        if card is not None:
            await queue.put(card)
            o.in_chat = True
        else:
            o.not_in_chat = "this session's client shows no files"


async def _file_card(session_id: str, ctx, o: _Outcome, caption: str, info) -> dict | None:
    """The card ``/v1/hooks/file`` puts in the chat: a durable media token
    for the saved file when the session's adapter serves downloads."""
    import secrets
    import config
    from adapters import get_adapter, get_session_adapter
    from core.session import session_kind
    from storage import database as task_store
    from storage.pg import run_db
    adapter = get_adapter(session_kind.DASHBOARD.name) if info else get_session_adapter(session_id)
    if adapter is None:
        return None
    path = Path(os.path.realpath(config.AGENTS_DIR)) / o.rel
    download_url = ""
    if getattr(adapter, "serves_file_downloads", False):
        chat_id = (info or {}).get("parent_chat_id") or ""
        if not chat_id:
            chat = await run_db(task_store.get_chat_by_session, session_id)
            chat_id = chat["id"] if chat else ""
        token = secrets.token_urlsafe(32)
        await run_db(
            task_store.create_media_token, token, str(path),
            media_kind="file", chat_id=chat_id or None, session_id=session_id,
            cache_owned=False, expires_at="", agent=ctx.agent,
        )
        download_url = f"/v1/media/{token}?download=1"
    return await adapter.handle_file_display(session_id, path, path.name, caption, download_url)


def _caption(text: str) -> str:
    one = _WS_RE.sub(" ", text or "").strip()
    return one if len(one) <= CAPTION_MAX else one[:CAPTION_MAX - 1].rstrip() + "…"


def _link(text: str) -> str:
    m = _URL_RE.search(text or "")
    if not m:
        return ""
    url = m.group(0).rstrip(".,;:!?)]}")
    return url if len(url) <= 2048 else ""


def _kept(outcomes: list[_Outcome]) -> Kept:
    """The line the agent reads: what was saved where and shown, and why
    any other block of the result was not."""
    kept_outcomes = [o for o in outcomes if o.shown_as]
    saved = [o.shown_as for o in kept_outcomes]
    shown = sum(1 for o in kept_outcomes if o.in_chat)
    vision = [{"base64": o.chat_b64, "media_type": o.chat_mime}
              for o in outcomes if o.picture and o.chat_b64]
    parts = []
    if saved:
        one = len(saved) == 1
        # "showed" only for what reached the chat: a block line below says
        # why the rest did not.
        if shown == len(saved):
            tail = (" and showed it" if one else " and showed them") + " to the user in the chat."
        elif shown:
            tail = f" and showed {shown} of them to the user in the chat."
        else:
            tail = "."
        parts.append("The platform saved this result's "
                     + ("file as " if one else f"{len(saved)} files as ")
                     + ", ".join(saved) + tail)
    for i, o in enumerate(outcomes, 1):
        what = "picture" if o.picture or o.blob.kind == _IMAGE else "file"
        if o.reason and o.picture and o.chat_b64:
            parts.append(f"Block {i} ({what}) is shown in the chat but not saved: {o.reason}.")
        elif o.reason:
            parts.append(f"Block {i} ({what}) is not kept: {o.reason}.")
        elif o.not_in_chat:
            parts.append(f"Block {i} ({what}) is saved but not shown: {o.not_in_chat}.")
    line = " ".join(parts)
    if len(line) > LINE_MAX:
        line = line[:LINE_MAX - 1] + "…"
    return Kept(line=line, vision=vision, saved=saved)
