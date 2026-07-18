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
"""

from __future__ import annotations

# event_types that carry a renderable display/file-tools artifact (as opposed to
# the blocking prompts — permission_prompt / plan_review / question — and
# tool_result, which each surface handles itself).
ARTIFACT_EVENT_TYPES = frozenset({
    "images", "image_generating", "image_gen_failed",
    "url", "file",
    "video", "audio", "media_processing", "media_failed",
    "document_preview",
    "ui",
})

# The REPLAYABLE subset: final renderables the interactive drainer persists as
# chat_messages event rows (interactive_session.persist_drained_artifact), so a
# later open can rebuild both the rich DB history and the PiP replay-on-open.
# Placeholders (image_generating / media_processing) and their failure/removal
# twins are transient by design — the pump's _save_turn_blocks drops
# media_processing the same way — so persisting them would freeze a skeleton
# into history.
REPLAYABLE_ARTIFACT_EVENT_TYPES = frozenset({
    "images", "url", "file", "video", "audio", "document_preview", "ui",
})


def artifact_event_from_perm_item(perm_data: dict) -> dict | None:
    """Map a permission-queue artifact item to its dashboard WS ``event`` dict.

    Returns the ``{"type": ...}`` frame for a display/file-tools artifact, or
    ``None`` for anything that is not a renderable artifact (blocking prompts,
    tool_result, unknown types) — the caller skips those.
    """
    et = perm_data.get("event_type", "")
    if et == "images":
        return {"type": "images", "images": perm_data["images"]}
    if et == "image_generating":
        return {
            "type": "image_generating",
            "prompt_preview": perm_data.get("prompt_preview", ""),
            "model": perm_data.get("model", ""),
        }
    if et == "image_gen_failed":
        return {"type": "image_gen_failed"}
    if et == "url":
        return {
            "type": "url",
            "url": perm_data["url"],
            "title": perm_data["title"],
            "description": perm_data.get("description", ""),
        }
    if et == "file":
        return {
            "type": "file",
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
            "type": "media_processing",
            "media_kind": perm_data.get("media_kind", "video"),
            "caption": perm_data.get("caption", ""),
        }
    if et == "media_failed":
        return {"type": "media_failed", "error": perm_data.get("error", "")}
    if et == "document_preview":
        return {
            "type": "document_preview",
            "wopi_url": perm_data["wopi_url"],
            "filename": perm_data["filename"],
            "file_id": perm_data["file_id"],
            "download_url": perm_data["download_url"],
            # Version-pinned snapshot identity: the dashboard renders a
            # superseded block from ITS OWN snapshot via
            # /v1/documents/snapshot-wopi-url ("" = no snapshot → chip).
            "snapshot_id": perm_data.get("snapshot_id", ""),
            "generation": perm_data.get("generation", 0),
        }
    if et == "ui":
        # Every field rides along: this dict is json.dumps-persisted verbatim,
        # so a dropped key is silently lost on reload/reconnect.
        return {
            "type": "ui",
            "token": perm_data["token"],
            "ui_url": perm_data["ui_url"],
            "title": perm_data.get("title", ""),
            "height": perm_data.get("height"),
            "path": perm_data.get("path", ""),
        }
    return None
