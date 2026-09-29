"""Chat snapshots (SHARING.md "Chat shares"): what a shared chat looks like
at the moment it was shared.

A chat share is a copy, never a live view: the user and assistant text,
the chat's artifacts (``display_ui`` pages), its displayed images, video,
audio and files, and — only when asked — the tool-call blocks. Everything
is copied under ``<agent dir>/shares/users/<creator>/<share id>/`` (the
creator's quota bucket; outside the files API, the sync and the
blueprints), so the copy serves after the chat's own files and media
tokens are gone, and ``chat.json`` maps each token to its copy. Every
artifact is located through its media token, never by guessing a folder.
Synchronous helpers, called off the loop.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config
from core.events import artifact_events
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, join_under
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from ws import wire_events as wire
from core import layout

logger = logging.getLogger("claude-proxy.shares")

SNAPSHOT_DIRNAME = "shares"
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
MAX_CHAT_SHARES_PER_USER = 50
# The artifact kinds a snapshot carries — the table's ``shareable`` rows
# (core/events/artifact_events.py): the ones whose bytes the copier can
# serve without a session. A document preview is not among them (a
# session-bound Collabora URL; no bytes, no renderer on the share host).
ARTIFACT_EVENTS = artifact_events.SHAREABLE
# The persisted kinds that ARE tool-call blocks (a tool card with its folded
# input and result, a subagent, a background command) plus the thinking rows
# — the ``include_tools`` copy. Every name is a row kind the pump writes.
TOOL_EVENTS = frozenset({wire.PERSISTED_TOOL, wire.THINKING, wire.TASK_SPAWN, wire.BG_COMMAND_SPAWN})
_INERT_INLINE = ("audio/", "video/", "image/")

# Credentials a tool call may have printed (an env dump carries the
# session's PROXY_API_KEY, a JWT acting as the chat's owner): a shared
# copy keeps the call, never the token.
_TOKEN_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"   # a JWT
    r"|otok_[A-Za-z0-9_-]{16,}"                                    # a platform API key
    r"|(?<=[Bb]earer )[A-Za-z0-9._~+/=-]{16,}"                      # any bearer value
)


def redact_tokens(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", text or "")


class SnapshotTooLarge(Exception):
    """The chat's files exceed what one snapshot may hold."""


def bucket_root(agent_dir: Path, username: str) -> Path:
    return agent_dir / SNAPSHOT_DIRNAME / layout.USERS / username


def snapshot_ref(agent: str, username: str, share_id: str) -> str:
    """The value stored on the share row: ``<agent>/shares/users/<u>/<id>``
    (the agent is part of it, so the copy is found after the chat is gone)."""
    return f"{agent}/{SNAPSHOT_DIRNAME}/{layout.USERS}/{username}/{share_id}"


def snapshot_path(share: dict) -> Path | None:
    """The snapshot's folder from the share row's ``snapshot_ref``, joined
    below the agents dir — an empty, absolute or traversing value is None."""
    try:
        return join_under(config.AGENTS_DIR, share.get("snapshot_ref") or "")
    except PathOutsideRoot:
        return None


def _token_of_media_url(url: str) -> str:
    """``/v1/media/<token>?download=1`` → the token."""
    if not url or "/v1/media/" not in url:
        return ""
    return url.split("/v1/media/", 1)[1].split("?", 1)[0].split("/", 1)[0]


class _Copier:
    """Writes the snapshot's copies beneath the agents root: every source is
    the file its media token names, opened with no symlink followed (the
    token was checked at mint on a file the agent keeps writing under), and
    every destination is written through ``safe_fs`` so a name planted in
    the snapshot folder is replaced, never written through."""

    def __init__(self, agent: str, username: str, share_id: str) -> None:
        self.root_rel = snapshot_ref(agent, username, share_id)
        self.total = 0
        self.files: dict[str, str] = {}

    def _account(self, n: int) -> None:
        self.total += n
        if self.total > MAX_SNAPSHOT_BYTES:
            raise SnapshotTooLarge()

    def copy_token(self, token: str, sub: str, suffix_hint: str = "") -> bool:
        """Copy the file a media token names into ``sub/``; False when the
        token is unknown, its file is gone or it cannot be opened safely
        (the block then renders as text)."""
        if not token or token in self.files:
            return token in self.files
        info = task_store.get_media_token(token)
        if not info:
            return False
        from api.media.media import MediaUnserveable, locate_media_row
        try:
            root, src_rel = locate_media_row(info)
        except MediaUnserveable:
            return False
        leaf = src_rel.rsplit("/", 1)[-1]
        suffix = Path(leaf).suffix or suffix_hint \
            or mimetypes.guess_extension(info.get("mime") or "") or ""
        rel = f"{sub}/{token}{suffix}"
        try:
            copied = safe_fs.copy_file_beneath(
                root, src_rel, config.AGENTS_DIR, f"{self.root_rel}/{rel}",
                max_size=MAX_SNAPSHOT_BYTES - self.total, mkdirs=True,
            )
        except safe_fs.FileTooLarge:
            raise SnapshotTooLarge()
        except OSError as exc:
            logger.warning("share snapshot skipped media token %s: %s",
                           token[:8], type(exc).__name__)
            return False
        self._account(copied)
        self.files[token] = rel
        return True

    def write_inline(self, key: str, data: bytes, mime: str) -> None:
        self._account(len(data))
        suffix = mimetypes.guess_extension(mime or "") or ".bin"
        rel = f"media/{key}{suffix}"
        safe_fs.atomic_write_beneath(
            config.AGENTS_DIR, f"{self.root_rel}/{rel}", data, mkdirs=True, fsync=False,
        )
        self.files[key] = rel


def _snapshot_block(row: dict, copier: _Copier, counter: list[int]) -> dict | None:
    """One artifact event, rewritten to reference the snapshot's copies."""
    et = row.get("event_type") or ""
    try:
        data = json.loads(row.get("event_data") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict = {"role": "event", "event_type": et, "created_at": row.get("created_at") or ""}
    if et == wire.UI:
        token = str(data.get("token") or "")
        if not copier.copy_token(token, "ui", ".html"):
            return None
        out["data"] = {"token": token, "title": data.get("title") or "", "height": data.get("height")}
    elif et in (wire.VIDEO, wire.AUDIO):
        token = str(data.get("token") or "")
        block = {"caption": data.get("caption") or "", "title": data.get("title") or "",
                 "mime": data.get("mime") or ""}
        if token and copier.copy_token(token, "media"):
            block["token"] = token
        elif data.get("url") and (data.get("src_kind") or "url") == "url":
            block["url"] = data["url"]
        else:
            return None
        out["data"] = block
    elif et == wire.FILE:
        token = _token_of_media_url(str(data.get("download_url") or ""))
        if not copier.copy_token(token, "media"):
            return None
        out["data"] = {"token": token, "filename": data.get("filename") or "",
                       "description": data.get("description") or ""}
    elif et == wire.IMAGES:
        items = []
        for item in data.get("images") or []:
            if not isinstance(item, dict):
                continue
            mime = item.get("mime_type") or "image/png"
            if item.get("image_data"):
                counter[0] += 1
                key = f"img-{counter[0]}"
                try:
                    raw = base64.b64decode(str(item["image_data"]), validate=False)
                except (ValueError, TypeError):
                    continue
                copier.write_inline(key, raw, mime)
                items.append({"token": key, "mime_type": mime, "caption": item.get("caption") or ""})
            elif item.get("url"):
                items.append({"url": item["url"], "mime_type": mime, "caption": item.get("caption") or ""})
        if not items:
            return None
        out["data"] = {"images": items}
    elif et == wire.URL:
        out["data"] = {"url": data.get("url") or "", "title": data.get("title") or "",
                       "description": data.get("description") or ""}
    else:
        return None
    return out


def build(chat: dict, share_id: str, creator_username: str, *, include_tools: bool = False) -> str:
    """Write the snapshot and return its ``snapshot_ref``. Raises
    SnapshotTooLarge past the byte cap (the caller undoes the share)."""
    agent = chat["agent"]
    agent_dir = config.get_agent_dir(agent)
    root = bucket_root(agent_dir, creator_username) / share_id
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    copier = _Copier(agent, creator_username, share_id)
    counter = [0]
    messages: list[dict] = []
    try:
        for row in task_store.get_chat_messages(chat["id"], limit=5000):
            role = row.get("role") or ""
            et = row.get("event_type") or ""
            if role in ("user", "assistant") and not et:
                if not (row.get("content") or "").strip():
                    continue
                messages.append({"role": role, "content": row.get("content") or "",
                                 "created_at": row.get("created_at") or "",
                                 "author_sub": row.get("author_sub") or ""})
            elif role == "event" and et in ARTIFACT_EVENTS:
                block = _snapshot_block(row, copier, counter)
                if block:
                    messages.append(block)
            elif include_tools and role == "event" and et in TOOL_EVENTS:
                messages.append({"role": "event", "event_type": et,
                                 "event_data": redact_tokens(row.get("event_data") or ""),
                                 "created_at": row.get("created_at") or ""})
        doc = {
            "version": 1,
            "chat_id": chat["id"],
            "title": chat.get("title") or "",
            "agent": agent,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "include_tools": bool(include_tools),
            "messages": messages,
            "files": copier.files,
        }
        text = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        copier._account(len(text))
        safe_fs.atomic_write_beneath(
            config.AGENTS_DIR, f"{copier.root_rel}/chat.json", text, fsync=False,
        )
    except SnapshotTooLarge:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return snapshot_ref(agent, creator_username, share_id)


def load(share: dict) -> dict | None:
    root = snapshot_path(share)
    if root is None:
        return None
    path = root / "chat.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def file_path(share: dict, token: str) -> Path | None:
    """The copy a token names inside this snapshot, or None. The map, not
    the token, decides the path, and the result must stay under the
    snapshot directory."""
    root = snapshot_path(share)
    doc = load(share)
    if root is None or not doc:
        return None
    rel = (doc.get("files") or {}).get(token or "")
    if not isinstance(rel, str) or not rel:
        return None
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path


def inline_ok(mime: str) -> bool:
    """The inert-type allowlist of ``/v1/media``: everything else downloads."""
    return (mime.startswith(_INERT_INLINE) and mime != "image/svg+xml") or mime == "application/pdf"


def remove(share: dict) -> None:
    root = snapshot_path(share)
    if root is not None and root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


def remove_user_snapshots(username: str) -> int:
    """Every snapshot a deleted user made, across the agents."""
    if not username:
        return 0
    n = 0
    agents_dir = Path(config.AGENTS_DIR)
    if not agents_dir.is_dir():
        return 0
    for agent_dir in agent_dirs(agents_dir):
        root = bucket_root(agent_dir, username)
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
            n += 1
    return n


def sweep_orphans(live_ids: set[str]) -> int:
    """Snapshot directories with no share row (the retention sweep)."""
    n = 0
    agents_dir = Path(config.AGENTS_DIR)
    if not agents_dir.is_dir():
        return 0
    for agent_dir in agent_dirs(agents_dir):
        users = agent_dir / SNAPSHOT_DIRNAME / layout.USERS
        if not users.is_dir():
            continue
        for bucket in users.iterdir():
            if not bucket.is_dir():
                continue
            for d in bucket.iterdir():
                if d.is_dir() and d.name not in live_ids:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
    return n
