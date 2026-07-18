"""Tests for core.events.artifact_events — the single source of truth that maps a
permission-queue artifact item to its dashboard WS frame, shared by the -p pump
and the interactive drainer."""

from core.events.artifact_events import (
    artifact_event_from_perm_item,
    ARTIFACT_EVENT_TYPES,
)


def test_images_passes_gallery_through():
    item = {"event_type": "images", "images": [{"image_data": "b64", "mime_type": "image/png", "caption": "Chart: Sales"}]}
    assert artifact_event_from_perm_item(item) == {
        "type": "images",
        "images": [{"image_data": "b64", "mime_type": "image/png", "caption": "Chart: Sales"}],
    }


def test_image_generating_defaults():
    assert artifact_event_from_perm_item({"event_type": "image_generating"}) == {
        "type": "image_generating", "prompt_preview": "", "model": "",
    }


def test_image_gen_failed_and_media_failed_are_removals():
    assert artifact_event_from_perm_item({"event_type": "image_gen_failed"}) == {"type": "image_gen_failed"}
    assert artifact_event_from_perm_item({"event_type": "media_failed", "error": "bad codec"}) == {
        "type": "media_failed", "error": "bad codec",
    }


def test_url_and_file():
    assert artifact_event_from_perm_item(
        {"event_type": "url", "url": "https://x", "title": "X", "description": "d"}
    ) == {"type": "url", "url": "https://x", "title": "X", "description": "d"}
    assert artifact_event_from_perm_item(
        {"event_type": "file", "filename": "a.pdf", "download_url": "/d/1"}
    ) == {"type": "file", "filename": "a.pdf", "download_url": "/d/1", "description": ""}


def test_video_audio_carry_src_fields():
    item = {"event_type": "video", "src_kind": "token", "token": "tok", "media_url": "/m/tok", "mime": "video/mp4"}
    out = artifact_event_from_perm_item(item)
    assert out["type"] == "video" and out["src_kind"] == "token"
    assert out["token"] == "tok" and out["media_url"] == "/m/tok" and out["mime"] == "video/mp4"
    # All player keys present (the renderer reads them unconditionally).
    for k in ("url", "token", "media_url", "mime", "caption", "title", "poster"):
        assert k in out


def test_media_processing_placeholder():
    assert artifact_event_from_perm_item({"event_type": "media_processing", "media_kind": "audio"}) == {
        "type": "media_processing", "media_kind": "audio", "caption": "",
    }


def test_document_preview():
    item = {"event_type": "document_preview", "wopi_url": "w", "filename": "f.docx",
            "file_id": "fid", "download_url": "/d",
            "snapshot_id": "abc123", "generation": 1700000000000}
    assert artifact_event_from_perm_item(item) == {
        "type": "document_preview", "wopi_url": "w", "filename": "f.docx",
        "file_id": "fid", "download_url": "/d",
        "snapshot_id": "abc123", "generation": 1700000000000,
    }
    # Pre-snapshot pushes (no chat, copy failure) default to empty identity.
    out = artifact_event_from_perm_item(
        {"event_type": "document_preview", "wopi_url": "w", "filename": "f",
         "file_id": "fid", "download_url": "/d"})
    assert out["snapshot_id"] == "" and out["generation"] == 0


def test_ui_carries_every_field():
    # The frame is json.dumps-persisted verbatim — a dropped key is silently
    # lost on reload/reconnect, so every field must ride along.
    item = {"event_type": "ui", "token": "tok", "ui_url": "/v1/ui/tok",
            "title": "Tip calc", "height": 420, "path": "workspace/generated-ui/a.html"}
    assert artifact_event_from_perm_item(item) == {
        "type": "ui", "token": "tok", "ui_url": "/v1/ui/tok",
        "title": "Tip calc", "height": 420, "path": "workspace/generated-ui/a.html",
    }
    # Auto-height artifacts have no height hint — None passes through.
    out = artifact_event_from_perm_item({"event_type": "ui", "token": "t", "ui_url": "/v1/ui/t"})
    assert out["height"] is None and out["title"] == "" and out["path"] == ""


def test_blocking_and_unknown_return_none():
    for et in ("permission_prompt", "plan_review", "question", "tool_result", "mode_restored", "", "nonsense"):
        assert artifact_event_from_perm_item({"event_type": et}) is None


def test_artifact_event_types_set_matches_mapper():
    # Every type in the set maps to a frame; nothing outside it does.
    for et in ARTIFACT_EVENT_TYPES:
        minimal = {"event_type": et}
        # Some require fields; build the minimal viable item per type.
        if et == "images":
            minimal["images"] = []
        elif et == "url":
            minimal.update(url="u", title="t")
        elif et == "file":
            minimal.update(filename="f", download_url="d")
        elif et == "document_preview":
            minimal.update(wopi_url="w", filename="f", file_id="i", download_url="d")
        elif et == "ui":
            minimal.update(token="t", ui_url="/v1/ui/t")
        assert artifact_event_from_perm_item(minimal) is not None, et
