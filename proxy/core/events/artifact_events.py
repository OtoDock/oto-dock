"""Permission-queue artifact item → dashboard WS frame (single source of truth).

Display-MCP and file-tools artifacts (galleries, charts, URLs, files, media,
Collabora previews) reach the proxy as items on a session's **permission
queue** (``api/hooks/hooks.py`` → ``core/session_state.get_permission_queue``), keyed
by ``event_type``. Two consumers turn those items into the WS frames the
dashboard renders (``lib/messageBlocks.ts::eventToBlock``):

* the ``-p``/pump path — ``core/events/stream_pump.py::_handle_perm_event`` (which also
  buffers the frame into ``_turn_blocks``/live-state for DB persistence + reconnect);
* the **interactive** path — ``ws/dashboard.py::_attach_pty_viewer`` drainer
  callback (there is no pump; floating windows).

This module is the one place the **frame shape** is defined, so the two paths
can never drift. It is pure (no I/O, no state) and unit-testable; callers own
their own buffering / forwarding / placeholder-replacement around it.

Since core-seams phase 9 it is also the one place the **behaviour per kind**
is defined: ``KINDS`` carries a row per artifact kind — whether the frame is
a renderable block or a removal signal, which placeholder it evicts, the
identity a later push replaces by, whether the pump defers it to the turn's
flush, persists it, and whether a share snapshot copies it — and every set
below derives from the rows. The pump's one artifact arm, the interactive
drainer, the snapshot and the dashboard mirror
(``dashboard/src/lib/kinds/artifact.ts``, bound by
``tests/core/test_kinds.py``) read the rows; nothing compares a kind's word.
"""

from __future__ import annotations

from dataclasses import dataclass

from ws import wire_events as wire


@dataclass(frozen=True)
class ArtifactKind:
    """One artifact kind and the behaviour the consumers apply to it."""
    name: str
    block: bool              # a renderable block appended to the turn and live lists; False = a removal signal
    placeholder: bool        # a transient skeleton the real artifact replaces
    evicts: str | None       # the placeholder kind this one removes (the latest of it, both lists)
    identity: str | None     # the wire field a later push of the same artifact replaces by (when non-empty)
    deferred: bool           # buffered by the pump to the turn's flush, replaced in place meanwhile
    saved: bool              # the pump persists the block at the turn's save (= the catalogue's persisted)
    shareable: bool          # a share snapshot copies it (bytes it can serve without a session)


# ``saved``: ``image_generating`` is persisted though no history renderer
# draws the row (only the live-state rebuild has an arm) while its media
# twin is dropped at save — kept as the catalogue says until the operator
# decides (core-seams phase 9, "Put to the operator"). ``shareable``: a
# ``document_preview`` is a session-bound Collabora URL — the snapshot has
# no bytes to copy and the share host no renderer for it — so it is
# unshareable by design, not by omission.
KINDS: dict[str, ArtifactKind] = {k.name: k for k in (
    ArtifactKind(wire.IMAGES, block=True, placeholder=False, evicts=wire.IMAGE_GENERATING, identity=None,
                 deferred=False, saved=True, shareable=True),
    ArtifactKind(wire.IMAGE_GENERATING, block=True, placeholder=True, evicts=None, identity=None,
                 deferred=False, saved=True, shareable=False),
    ArtifactKind(wire.IMAGE_GEN_FAILED, block=False, placeholder=False, evicts=wire.IMAGE_GENERATING,
                 identity=None, deferred=False, saved=False, shareable=False),
    ArtifactKind(wire.URL, block=True, placeholder=False, evicts=None, identity=None,
                 deferred=False, saved=True, shareable=True),
    ArtifactKind(wire.FILE, block=True, placeholder=False, evicts=None, identity=None,
                 deferred=False, saved=True, shareable=True),
    ArtifactKind(wire.VIDEO, block=True, placeholder=False, evicts=wire.MEDIA_PROCESSING, identity=None,
                 deferred=False, saved=True, shareable=True),
    ArtifactKind(wire.AUDIO, block=True, placeholder=False, evicts=wire.MEDIA_PROCESSING, identity=None,
                 deferred=False, saved=True, shareable=True),
    ArtifactKind(wire.MEDIA_PROCESSING, block=True, placeholder=True, evicts=None, identity=None,
                 deferred=False, saved=False, shareable=False),
    ArtifactKind(wire.MEDIA_FAILED, block=False, placeholder=False, evicts=wire.MEDIA_PROCESSING,
                 identity=None, deferred=False, saved=False, shareable=False),
    ArtifactKind(wire.DOCUMENT_PREVIEW, block=True, placeholder=False, evicts=None, identity="file_id",
                 deferred=True, saved=True, shareable=False),
    ArtifactKind(wire.UI, block=True, placeholder=False, evicts=None, identity="path",
                 deferred=False, saved=True, shareable=True),
)}

# event_types that carry a renderable display/file-tools artifact (as opposed to
# the blocking prompts — permission_prompt / plan_review / question — and
# tool_result, which each surface handles itself).
ARTIFACT_EVENT_TYPES = frozenset(KINDS)

# The kinds that render as a block in a message (the removal signals do not).
BLOCK_KINDS = frozenset(k.name for k in KINDS.values() if k.block)

# The REPLAYABLE subset: final renderables the interactive drainer persists as
# chat_messages event rows (interactive_session.persist_drained_artifact), so a
# later open can rebuild both the rich DB history and the PiP replay-on-open.
# Placeholders (image_generating / media_processing) and their failure/removal
# twins are transient by design, so persisting them would freeze a skeleton
# into history: a block that is not a placeholder.
REPLAYABLE_ARTIFACT_EVENT_TYPES = frozenset(k.name for k in KINDS.values() if k.block and not k.placeholder)

# The kinds the pump persists at the turn's save.
SAVED = frozenset(k.name for k in KINDS.values() if k.saved)

# The kinds a share snapshot copies (services/sharing/chat_snapshot.py).
SHAREABLE = frozenset(k.name for k in KINDS.values() if k.shareable)


#: Event fields a live frame carries and a stored row never does: the
#: document preview's WOPI token (a card from history mints its own).
LIVE_ONLY_KEYS = ("access_token", "access_token_ttl")


def for_viewer(event: dict, pusher: str, viewer: str) -> dict:
    """``event`` as one viewer's connection gets it: the live-only fields
    (the pushing session's edit-capable WOPI token) go only to the person
    whose session pushed it; every other viewer of the chat gets the frame
    without them, and their pane mints its own role-gated token. No known
    pusher keeps the token from everyone."""
    if not any(k in event for k in LIVE_ONLY_KEYS) or (pusher and viewer == pusher):
        return event
    return {k: v for k, v in event.items() if k not in LIVE_ONLY_KEYS}


def kind_of(event_type: str | None) -> ArtifactKind | None:
    """The row for an event type, ``None`` for anything that is not an
    artifact kind (a text or tool block, a blocking prompt) — total, so a
    loop over every turn block can ask it."""
    return KINDS.get(event_type or "")


def artifact_event_from_perm_item(perm_data: dict) -> dict | None:
    """Map a permission-queue artifact item to its dashboard WS ``event`` dict.

    Returns the ``{"type": ...}`` frame for a display/file-tools artifact, or
    ``None`` for anything that is not a renderable artifact (blocking prompts,
    tool_result, unknown types) — the caller skips those.
    """
    et = perm_data.get("event_type", "")
    if et == "images":
        return {"type": wire.IMAGES, "images": perm_data["images"]}
    if et == "image_generating":
        return {
            "type": wire.IMAGE_GENERATING,
            "prompt_preview": perm_data.get("prompt_preview", ""),
            "model": perm_data.get("model", ""),
        }
    if et == "image_gen_failed":
        return {"type": wire.IMAGE_GEN_FAILED}
    if et == "url":
        return {
            "type": wire.URL,
            "url": perm_data["url"],
            "title": perm_data["title"],
            "description": perm_data.get("description", ""),
        }
    if et == "file":
        return {
            "type": wire.FILE,
            "filename": perm_data["filename"],
            "download_url": perm_data["download_url"],
            "description": perm_data.get("description", ""),
        }
    if et in ("video", "audio"):
        return {
            "type": et,
            "src_kind": perm_data.get("src_kind", "url"),
            "url": perm_data.get("url", ""),
            "token": perm_data.get("token", ""),
            "media_url": perm_data.get("media_url", ""),
            "mime": perm_data.get("mime", ""),
            "caption": perm_data.get("caption", ""),
            "title": perm_data.get("title", ""),
            "poster": perm_data.get("poster", ""),
        }
    if et == "media_processing":
        return {
            "type": wire.MEDIA_PROCESSING,
            "media_kind": perm_data.get("media_kind", "video"),
            "caption": perm_data.get("caption", ""),
        }
    if et == "media_failed":
        return {"type": wire.MEDIA_FAILED, "error": perm_data.get("error", "")}
    if et == "document_preview":
        return {
            "type": wire.DOCUMENT_PREVIEW,
            "wopi_url": perm_data["wopi_url"],
            # The WOPI token rides the live frame only (LIVE_ONLY_KEYS).
            "access_token": perm_data.get("access_token", ""),
            "access_token_ttl": perm_data.get("access_token_ttl", 0),
            "filename": perm_data["filename"],
            "file_id": perm_data["file_id"],
            "download_url": perm_data["download_url"],
            # Version-pinned snapshot identity: the pane opens this push's
            # version through /v1/documents/snapshot-wopi-url ("" = no copy,
            # the version reads "no longer available").
            "snapshot_id": perm_data.get("snapshot_id", ""),
            "generation": perm_data.get("generation", 0),
            # The push's number among the file's pushes in this chat (the
            # card's "version N"; the documents listing is the authority).
            "version": perm_data.get("version", 0),
        }
    if et == "ui":
        # Every field rides along: this dict is json.dumps-persisted verbatim,
        # so a dropped key is silently lost on reload/reconnect.
        return {
            "type": wire.UI,
            "token": perm_data["token"],
            "ui_url": perm_data["ui_url"],
            "title": perm_data.get("title", ""),
            "height": perm_data.get("height"),
            "path": perm_data.get("path", ""),
        }
    return None
