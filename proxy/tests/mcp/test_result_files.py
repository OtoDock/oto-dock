"""``services/mcp/result_files.py`` — an opted-in MCP tool's pictures and
file blobs: the opt-in by server key, the engines' block shapes, the caps,
the decode check, the destination per session scope and write right, the
names, the chat events, the line, and the push to a paired machine."""

from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

import config
from auth.path_policy import SecurityContext
from core.session import session_state
from services.mcp import result_files as rf
from services.mcp.mcp_manifest_types import ResultFilesDecl
from ws import wire_events as wire

AGENT = "rf-agent"


def _png(w=64, h=40, color=(200, 30, 30), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format=fmt)
    return buf.getvalue()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _image(raw: bytes, mime="image/png") -> dict:
    return {"type": "image", "data": _b64(raw), "mimeType": mime}


def _manifest(name, tools, subdir="maps", server_name=""):
    return SimpleNamespace(name=name, server_name=server_name or name,
                           result_files=ResultFilesDecl(tools=list(tools), subdir=subdir))


@pytest.fixture
def manifests(monkeypatch):
    found = {
        "maps-mcp": _manifest("maps-mcp", ["static_map", "drive_file"]),
        "shots": _manifest("shots", ["other"], subdir="shots"),
        "plain": SimpleNamespace(name="plain", server_name="plain", result_files=None),
    }
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: found)
    return found


def _ctx(*, username="alice", scope="user", role="manager", **kw) -> SecurityContext:
    return SecurityContext(role=role, username=username, agent=AGENT, is_admin_agent=False,
                           session_scope=scope, **kw)


@pytest.fixture
def session(monkeypatch):
    """Register a context under a fresh id; the test may replace it."""
    sid = "rf-sess-1"

    def _register(ctx):
        monkeypatch.setitem(session_state._session_security, sid, ctx)
        return sid

    _register(_ctx())
    yield sid, _register
    for key in (sid, "meeting-rf"):
        session_state._permission_emitters.pop(key, None)


def _drain(sid):
    q = session_state.get_permission_queue(sid)
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _agent_dir():
    return config.AGENTS_DIR / AGENT


# --- the opt-in ---------------------------------------------------------------

def test_the_server_key_picks_the_manifest_whose_list_decides(manifests):
    m, tool = rf.opted_in("mcp__maps-mcp__static_map")
    assert m.name == "maps-mcp" and tool == "static_map"
    # Codex's sanitised server key names the same MCP.
    assert rf.opted_in("mcp__maps_mcp__static_map")[0].name == "maps-mcp"
    # Another MCP's list never opts a tool in.
    assert rf.opted_in("mcp__shots__static_map") is None
    assert rf.opted_in("mcp__maps-mcp__other") is None
    assert rf.opted_in("mcp__plain__static_map") is None
    assert rf.opted_in("Bash") is None


def test_the_tool_name_splits_at_the_first_double_underscore():
    """The key ends at the first ``__`` after its first character, both
    halves non-empty; a long run of underscores costs one pass."""
    assert rf._split_mcp_tool("mcp__a__b__c") == ("a", "b__c")
    assert rf._split_mcp_tool("mcp__a___b") == ("a", "_b")
    assert rf._split_mcp_tool("mcp____x") is None
    assert rf._split_mcp_tool("mcp__a__") is None
    assert rf._split_mcp_tool("mcp__" + "_" * 100_000) == ("_", "_" * 99_997)
    assert rf._split_mcp_tool("Bash") is None
    assert rf.opted_in("mcp__maps-mcp") is None


# --- the shapes ---------------------------------------------------------------

def test_the_three_engine_shapes_read_as_blobs():
    raw = _png()
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(raw)}},
        _image(raw),
        {"type": "resource", "resource": {"uri": "gdrive://a/b.pdf", "mimeType": "application/pdf",
                                          "blob": _b64(b"%PDF-1.4")}},
        {"type": "resource", "resource": {"uri": "file:///x.txt", "mimeType": "text/plain", "text": "hi"}},
        {"type": "image", "mime": "image/png", "skipped_bytes": 99},
        {"type": "text", "text": "caption"},
        "junk",
    ]
    blobs = rf.blobs_of(blocks)
    assert [(b.kind, b.mime) for b in blobs] == [
        ("image", "image/png"), ("image", "image/png"), ("resource", "application/pdf"),
        ("image", "image/png")]
    assert blobs[2].name_hint == "gdrive://a/b.pdf"
    assert blobs[3].data == "" and blobs[3].skipped_bytes == 99


def test_the_cli_image_pointer_is_no_caption():
    blocks = [
        {"type": "text", "text": "[Image: source: /users/a/.claude/projects/-users-a/s/tool-results/"
                                 "mcp-maps-blob-1-x.png]"},
        {"type": "text", "text": "[Image: source: C:\\Users\\a\\.claude\\projects\\p\\s\\tool-results\\"
                                 "mcp-maps-blob-1-x.png, original 2481x3508, displayed at 1414x2000.]"},
        {"type": "text", "text": "Route: 12 km https://maps.example/r"},
    ]
    assert rf.text_of(blocks) == "Route: 12 km https://maps.example/r"


@pytest.mark.parametrize("text, caption", [
    # @cablate/mcp-google-map's maps_static_map: the byte count goes, the size stays.
    ("Map generated (67917 bytes, 640x480)", "Map generated (640x480)"),
    ("Map generated (67,917 bytes, 640 × 480)", "Map generated (640 × 480)"),
    ("Map generated (67917 bytes)", "Map generated"),
    ("Map generated\n(67917 bytes,\n640x480)  ", "Map generated (640x480)"),
    ("(67917 bytes)", ""),
    # Any other parenthesis is the tool's own text.
    ("Report (2 bytes of data) ready", "Report (2 bytes of data) ready"),
    ("Tile (640x480)", "Tile (640x480)"),
    ("A (12 bytes, 640x480) then more", "A (12 bytes, 640x480) then more"),
    ("Took (12 Bytes)", "Took (12 Bytes)"),
    ("Saved (1 byte)", "Saved (1 byte)"),
])
def test_a_trailing_byte_count_leaves_the_caption(text, caption):
    assert rf._caption(text) == caption


def test_a_long_digit_run_costs_the_caption_nothing():
    text = "x (" + "1" * 200_000 + " bytes"
    assert rf._caption(text).startswith("x (111")


# --- keep ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_kept_picture_is_saved_shown_and_named(manifests, session):
    sid, _ = session
    raw = _png()
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(raw)]),
                         "Route from A to B: 12 km. https://www.google.com/maps/dir/?api=1&o=A).")
    folder = _agent_dir() / "users" / "alice" / "workspace" / "maps"
    files = sorted(folder.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == raw
    assert files[0].name.startswith("static_map-") and files[0].suffix == ".png"
    shown = f"/users/alice/workspace/maps/{files[0].name}"
    assert kept.saved == [shown]
    assert shown in kept.line and "showed it to the user" in kept.line
    assert kept.vision and kept.vision[0]["media_type"] == "image/png"
    (event,) = _drain(sid)
    assert event["event_type"] == wire.IMAGES
    (item,) = event["images"]
    assert base64.b64decode(item["image_data"]) == raw  # small: as it came
    assert item["caption"].startswith("Route from A to B")
    assert item["link_url"] == "https://www.google.com/maps/dir/?api=1&o=A"
    assert item["attribution"] == f"Saved as {shown}"


@pytest.mark.asyncio
async def test_a_tool_not_opted_in_changes_nothing(manifests, session):
    sid, _ = session
    assert await rf.keep(sid, "mcp__maps-mcp__other", rf.blobs_of([_image(_png())])) is None
    assert await rf.keep(sid, "mcp__plain__static_map", rf.blobs_of([_image(_png())])) is None
    assert not (_agent_dir() / "users" / "alice" / "workspace" / "maps").exists()
    assert _drain(sid) == []


@pytest.mark.asyncio
async def test_twenty_pictures_keep_eight_with_distinct_names(manifests, session):
    sid, _ = session
    raw = _png()
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(raw)] * 20))
    files = sorted((_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir())
    assert len(files) == rf.MAX_FILES_PER_RESULT == 8
    assert len({f.name for f in files}) == 8
    assert len(kept.saved) == 8
    assert kept.line.startswith("The platform saved this result's 8 files as ")
    assert "and showed them to the user in the chat." in kept.line
    assert kept.line.count("not kept: over the 8 files a result may keep") == 12
    (event,) = _drain(sid)
    assert len(event["images"]) == 8
    assert event["images"][1]["caption"] == ""


@pytest.mark.asyncio
async def test_size_limits_and_the_decode_check(manifests, session):
    sid, _ = session
    big = "A" * ((rf.MAX_FILE_BYTES + 1024) * 4 // 3)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("a.txt", "not a picture")
    jpeg = _png(fmt="JPEG")
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([
        {"type": "image", "data": big, "mimeType": "image/png"},
        _image(zip_buf.getvalue()),
        {"type": "image", "data": "!!not base64!!", "mimeType": "image/png"},
        _image(jpeg, mime="image/png"),
        {"type": "image", "mime": "image/png", "skipped_bytes": 40 * 1024 * 1024},
    ]))
    files = sorted((_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir())
    # Only the JPEG declared png is kept, under its decoded format.
    assert [f.suffix for f in files] == [".jpg"] and files[0].read_bytes() == jpeg
    assert "Block 1 (picture) is not kept: 15 MB, over the 15 MB limit." in kept.line
    assert "Block 2 (picture) is not kept: not a png, jpeg, webp or gif picture." in kept.line
    assert "Block 3 (picture) is not kept: not valid base64." in kept.line
    assert "Block 5 (picture) is not kept: 40 MB, over the 15 MB limit." in kept.line


@pytest.mark.asyncio
async def test_too_many_pixels_is_refused_before_a_decode(manifests, session, monkeypatch):
    sid, _ = session
    monkeypatch.setattr(rf, "MAX_PICTURE_PIXELS", 100)
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png(20, 20))]))
    assert "20x20 is too many pixels" in kept.line
    assert kept.saved == []


@pytest.mark.asyncio
async def test_a_large_picture_reaches_the_chat_cut_to_1568(manifests, session):
    sid, _ = session
    raw = _png(3000, 1000)
    await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(raw)]))
    (event,) = _drain(sid)
    copy = Image.open(io.BytesIO(base64.b64decode(event["images"][0]["image_data"])))
    assert max(copy.size) == rf.CHAT_COPY_MAX_DIM
    # The workspace keeps the original.
    (f,) = (_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir()
    assert f.read_bytes() == raw


@pytest.mark.asyncio
async def test_the_mount_decides_the_folder(manifests, session):
    sid, register = session
    # A Shared-only human chat works in the shared workspace.
    register(_ctx(username="alice", scope="agent"))
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert kept.saved[0].startswith("/workspace/maps/static_map-")
    assert len(list((_agent_dir() / "workspace" / "maps").iterdir())) == 1
    # An agent-scope task run too (it runs as the agent's manager).
    register(_ctx(username="", scope="agent", role="manager"))
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png(30))]))
    assert kept.saved[0].startswith("/workspace/maps/")
    assert not (_agent_dir() / "users").exists()


@pytest.mark.asyncio
async def test_a_session_that_cannot_write_shows_the_picture_only(manifests, session):
    sid, register = session
    register(_ctx(username="vic", scope="agent", role="viewer"))
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert kept.saved == []
    assert "is shown in the chat but not saved: this session cannot write its workspace" in kept.line
    assert not (_agent_dir() / "workspace" / "maps").exists()
    (event,) = _drain(sid)
    assert event["images"][0]["attribution"] == ""


@pytest.mark.asyncio
async def test_a_resource_blob_is_a_file_with_a_safe_name(manifests, session, monkeypatch, temp_db):
    sid, _ = session
    cards = []

    class _Adapter:
        serves_file_downloads = False

        async def handle_file_display(self, session_id, path, filename, description, download_url=""):
            cards.append((path, filename, description, download_url))
            return {"event_type": wire.FILE, "filename": filename,
                    "download_url": download_url, "description": description}

    import adapters
    monkeypatch.setattr(adapters, "get_session_adapter", lambda _sid: _Adapter())
    kept = await rf.keep(sid, "mcp__maps-mcp__drive_file", rf.blobs_of([{
        "type": "resource", "resource": {
            "uri": "gdrive://files/abc/../../Quarterly report Q3.pdf",
            "mimeType": "application/pdf", "blob": _b64(b"%PDF-1.4 x")}}]),
        "Downloaded the report.")
    (f,) = (_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir()
    assert f.name.startswith("drive_file-") and f.name.endswith("-Quarterly_report_Q3.pdf")
    assert f.read_bytes() == b"%PDF-1.4 x"
    assert kept.vision == [] and len(kept.saved) == 1
    assert "and showed it to the user in the chat." in kept.line
    (event,) = _drain(sid)
    assert event["event_type"] == wire.FILE and event["filename"] == f.name
    assert cards[0][2] == "Downloaded the report."


@pytest.mark.parametrize("hint,expected", [
    ("gdrive://x/../../a b.pdf", "a_b.pdf"),
    ("..\\..\\C:\\evil\\x.exe", "x.exe"),
    ("/abs/path/.hidden", "hidden"),
    ("..", ""),
    ("", ""),
    ("x://logo.svg", "logo.svg"),
    ("https://h/p/" + "n" * 120 + ".docx", "n" * 75 + ".docx"),
])
def test_a_resource_name_keeps_only_its_last_safe_segment(hint, expected):
    assert rf._name_hint(hint) == expected


@pytest.mark.asyncio
async def test_a_meeting_participant_shows_in_the_meeting(manifests, session):
    sid, _ = session
    session_state.set_meeting_session_info(sid, "parent-1", "meeting-rf", AGENT, "chat-p")
    try:
        await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
        assert [e["event_type"] for e in _drain("meeting-rf")] == [wire.IMAGES]
        assert _drain(sid) == []
    finally:
        session_state.cleanup_meeting_session_info(sid)


@pytest.mark.asyncio
async def test_a_remote_session_pushes_and_an_offline_machine_does_not_hold_it(
        manifests, session, monkeypatch):
    sid, _ = session
    from core.remote import remote_file_flow
    pushed = []

    async def _push_back(session_id, rel):
        pushed.append(rel)
        return False  # offline: the platform copy stays ahead

    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda _sid: True)
    monkeypatch.setattr(remote_file_flow, "push_back", _push_back)
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert len(kept.saved) == 1
    assert pushed and pushed[0].startswith("users/alice/workspace/maps/static_map-")
    assert [e["event_type"] for e in _drain(sid)] == [wire.IMAGES]


@pytest.mark.asyncio
async def test_a_slow_push_does_not_hold_the_answer_past_the_wait(manifests, session, monkeypatch):
    sid, _ = session
    from core.remote import remote_file_flow
    monkeypatch.setattr(rf, "PUSH_WAIT_S", 0.05)

    async def _slow(session_id, rel):
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda _sid: True)
    monkeypatch.setattr(remote_file_flow, "push_back", _slow)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert kept.saved and loop.time() - t0 < 2
    for task in list(rf._PUSHES):
        task.cancel()


@pytest.mark.asyncio
async def test_an_unknown_session_changes_nothing(manifests):
    assert await rf.keep("rf-nobody", "mcp__maps-mcp__static_map",
                         rf.blobs_of([_image(_png())])) is None


# --- the hook route -----------------------------------------------------------

def _post_hook(body):
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from app import app
    with patch("api.hooks.lifecycle.verify_session_match_async", new_callable=AsyncMock):
        return TestClient(app).post("/v1/hooks/tool-result", json=body,
                                    headers={"Authorization": "Bearer dummy"})


def test_the_route_asks_for_the_files_of_an_opted_in_tool(manifests, session):
    sid, _ = session
    base = {"session_id": sid, "tool_use_id": "tu1", "summary": "ok", "result_content": "r"}
    assert _post_hook({**base, "tool_name": "mcp__maps-mcp__static_map"}).json() == {
        "status": "ok", "result_files": True}
    assert _post_hook({**base, "tool_name": "mcp__maps-mcp__other"}).json() == {"status": "ok"}
    assert _post_hook({**base, "tool_name": "Bash"}).json() == {"status": "ok"}
    # Each forward rendered its pill once.
    assert [e["event_type"] for e in _drain(sid)] == [wire.ITEM_TOOL_RESULT] * 3


def test_the_files_request_keeps_them_and_answers_the_line(manifests, session):
    sid, _ = session
    raw = _png()
    resp = _post_hook({
        "session_id": sid, "tool_name": "mcp__maps-mcp__static_map", "tool_use_id": "tu1",
        "summary": "", "result_content": "Route: 12 km", "files_only": True,
        "result_blocks": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": _b64(raw)}}],
    })
    line = resp.json()["additional_context"]
    assert line.startswith("The platform saved this result's file as /users/alice/workspace/maps/")
    # No second pill: only the gallery.
    assert [e["event_type"] for e in _drain(sid)] == [wire.IMAGES]


def test_a_files_request_for_a_tool_not_kept_does_nothing(manifests, session):
    sid, _ = session
    resp = _post_hook({
        "session_id": sid, "tool_name": "mcp__maps-mcp__other", "summary": "",
        "files_only": True, "result_blocks": [_image(_png())],
    })
    assert resp.json() == {"status": "ok", "additional_context": ""}
    assert _drain(sid) == []


# --- the audit's cases ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_judge_run_keeps_no_file(manifests, session):
    sid, register = session
    register(_ctx(read_only=True))
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert kept.saved == [] and "a check's judge keeps no files" in kept.line
    assert not (_agent_dir() / "users" / "alice" / "workspace" / "maps").exists()


@pytest.mark.asyncio
async def test_a_failed_call_keeps_nothing(manifests, session):
    sid, _ = session
    assert await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]),
                         is_error=True) is None
    assert _drain(sid) == []


@pytest.mark.asyncio
async def test_the_chat_carries_a_bounded_share_of_a_result(manifests, session, monkeypatch):
    sid, _ = session
    raw = _png()
    monkeypatch.setattr(rf, "CHAT_INLINE_BUDGET", len(_b64(raw)) * 2)
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(raw)] * 3))
    assert len(kept.saved) == 3
    assert "3 files as " in kept.line and "and showed 2 of them to the user in the chat." in kept.line
    assert "Block 3 (picture) is saved but not shown: past what one result shows in the chat." \
        in kept.line
    (event,) = _drain(sid)
    assert len(event["images"]) == 2


@pytest.mark.asyncio
async def test_a_paired_machine_session_reads_its_own_path(manifests, session):
    from core import placement
    sid, register = session
    register(_ctx(placement=placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, machine_id="m1",
        agents_dir="/home/dave/.oto-dock/agents", home_dir="/home/dave")))
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())]))
    assert kept.saved[0].startswith(
        f"/home/dave/.oto-dock/agents/{AGENT}/users/alice/workspace/maps/static_map-")
    # The platform copy is written where the sync takes it from.
    assert len(list((_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir())) == 1


@pytest.mark.asyncio
async def test_an_image_blob_pillow_does_not_keep_is_saved_as_a_file(manifests, session, monkeypatch):
    sid, _ = session
    import adapters
    monkeypatch.setattr(adapters, "get_session_adapter", lambda _sid: None)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'
    kept = await rf.keep(sid, "mcp__maps-mcp__drive_file", rf.blobs_of([
        {"type": "resource", "resource": {"uri": "x://logo.svg", "mimeType": "image/svg+xml",
                                          "blob": _b64(svg)}},
        {"type": "image", "data": _b64(svg), "mimeType": "image/svg+xml"},
    ]))
    (f,) = (_agent_dir() / "users" / "alice" / "workspace" / "maps").iterdir()
    assert f.name.endswith("-logo.svg") and f.read_bytes() == svg
    # An image block must be a picture.
    assert "Block 2 (picture) is not kept: not a png, jpeg, webp or gif picture." in kept.line
    # No adapter showed the file: the line says so instead of "showed it".
    assert "showed" not in kept.line
    assert "Block 1 (file) is saved but not shown: this session's client shows no files." in kept.line
    assert _drain(sid) == []


@pytest.mark.asyncio
async def test_a_folder_that_cannot_be_made_is_one_reason(manifests, session):
    sid, _ = session
    ws = _agent_dir() / "users" / "alice" / "workspace"
    ws.mkdir(parents=True)
    (ws / "maps").write_text("a file where the folder goes")
    kept = await rf.keep(sid, "mcp__maps-mcp__static_map", rf.blobs_of([_image(_png())] * 3))
    assert kept.saved == []
    assert kept.line.count("the folder maps could not be made") == 3
    assert (ws / "maps").read_text() == "a file where the folder goes"
