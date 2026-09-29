"""Chat shares are snapshots (SHARING.md "Chat shares"): what is kept, what
is copied, where it lives, who may read it, and that the copy needs
nothing from the chat once made.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from core.remote import file_sync
from services.sharing import chat_snapshot
from storage import database as task_store
from storage.sharing import share_store

client = TestClient(app)

AGENT = "snap-agent"
OWNER = "snap-owner"
OTHER = "snap-other"
EDITOR = "snap-editor"
ACCOUNT_PW = "owner-pass-123"
ORIGIN = {"Origin": "http://testserver"}


def _user(sub: str = OWNER, role: str = "member", agent_roles: dict[str, str] | None = None,
          agents: tuple[str, ...] = (AGENT,)) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role, agents=list(agents),
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    (tmp_path / AGENT / "users" / "owner" / "workspace" / "generated-ui").mkdir(parents=True)
    for sub, name in ((OWNER, "Owner"), (OTHER, "Other"), (EDITOR, "Editor")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=%s, username=%s WHERE sub=%s",
                     (hash_password(ACCOUNT_PW), "owner", OWNER))
        conn.commit()
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(EDITOR, AGENT, "editor", "test")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    client.cookies.clear()
    _as(_user())
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _chat(owner: str = OWNER, title: str = "Plan review") -> str:
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, owner, AGENT)
    task_store.update_chat(chat_id, title=title)
    task_store.add_chat_message(chat_id, "user", "Show me the plan")
    task_store.add_chat_message(chat_id, "event", event_type="thinking",
                                event_data=json.dumps({"text": "hmm"}))
    task_store.add_chat_message(chat_id, "event", event_type="tool",
                                event_data=json.dumps({"type": "tool", "name": "Read",
                                                       "tool_input": {"file_path": "a.md"}}))
    task_store.add_chat_message(chat_id, "event", event_type="task_spawn",
                                event_data=json.dumps({"type": "task_spawn", "description": "look"}))
    task_store.add_chat_message(chat_id, "event", event_type="bg_command_spawn",
                                event_data=json.dumps({"type": "bg_command_spawn", "command": "ls"}))
    task_store.add_chat_message(chat_id, "assistant", "Here it is, with a chart.")
    # A display_ui artifact: its file lives wherever the token says.
    ui_path = config.AGENTS_DIR / AGENT / "users/owner/workspace/generated-ui/chart-ab12.html"
    ui_path.write_text("<div class='card'>chart</div>")
    task_store.create_media_token("tok-ui-1", str(ui_path), mime="text/html", media_kind="ui",
                                  chat_id=chat_id, agent=AGENT, owner_sub=owner)
    task_store.add_chat_message(chat_id, "event", event_type="ui", event_data=json.dumps(
        {"token": "tok-ui-1", "ui_url": "/v1/ui/tok-ui-1", "title": "Chart", "height": 320}))
    # An inline image and an external one.
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    task_store.add_chat_message(chat_id, "event", event_type="images", event_data=json.dumps(
        {"images": [{"image_data": png, "mime_type": "image/png", "caption": "a"},
                    {"url": "https://example.com/x.jpg", "mime_type": "image/jpeg"}]}))
    # A file behind a media token.
    f_path = config.AGENTS_DIR / AGENT / "users/owner/workspace/report.pdf"
    f_path.write_bytes(b"%PDF-1.4 fake")
    task_store.create_media_token("tok-file-1", str(f_path), mime="application/pdf",
                                  media_kind="file", chat_id=chat_id, agent=AGENT, owner_sub=owner)
    task_store.add_chat_message(chat_id, "event", event_type="file", event_data=json.dumps(
        {"filename": "report.pdf", "download_url": "/v1/media/tok-file-1?download=1"}))
    return chat_id


def _share(chat_id: str, grantee: str = OTHER, **extra) -> dict:
    r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": chat_id,
                                        "grantee": grantee, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ───────────────────────── the snapshot ─────────────────────────────────────


def test_snapshot_keeps_text_and_artifacts_copies_files_and_drops_tools_by_default():
    chat_id = _chat()
    body = _share(chat_id)
    share = share_store.get_share(body["share"]["id"])
    assert share["snapshot_ref"] == f"{AGENT}/shares/users/owner/{share['id']}"
    root = config.AGENTS_DIR / share["snapshot_ref"]
    doc = json.loads((root / "chat.json").read_text())
    kinds = [(m["role"], m.get("event_type", "")) for m in doc["messages"]]
    assert kinds == [("user", ""), ("assistant", ""), ("event", "ui"), ("event", "images"),
                     ("event", "file")]
    assert (root / doc["files"]["tok-ui-1"]).read_text() == "<div class='card'>chart</div>"
    assert (root / doc["files"]["tok-file-1"]).read_bytes().startswith(b"%PDF")
    images = doc["messages"][3]["data"]["images"]
    assert images[0]["token"] == "img-1" and (root / doc["files"]["img-1"]).is_file()
    assert images[1]["url"] == "https://example.com/x.jpg"
    # The whole snapshot sits outside the files API and the sync.
    assert file_sync.is_platform_only_tree("shares/users/owner/x/chat.json")
    assert file_sync.should_sync_to_target("shares/users/owner/x/chat.json", "owner", "manager") is False
    assert client.get(f"/v1/agents/{AGENT}/files/shares/users/owner/{share['id']}/chat.json").status_code in (403, 404)


def test_include_tools_keeps_the_tool_blocks():
    chat_id = _chat()
    body = _share(chat_id, include_tools=True)
    doc = chat_snapshot.load(share_store.get_share(body["share"]["id"]))
    kinds = [m.get("event_type", m["role"]) for m in doc["messages"]]
    # The persisted tool-call kinds ride along: the tool card, the subagent,
    # the background command (the dead pre-phase-6 names never matched a row).
    assert kinds[:6] == ["user", "thinking", "tool", "task_spawn", "bg_command_spawn", "assistant"]


def test_tool_blocks_leave_their_tokens_behind():
    # A synthetic token assembled from its parts, so no secret scanner reads
    # the fixture as a real key.
    jwt = ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJ1c2VyLTEifQ", "c2lnbmF0dXJlLXZhbHVl"))
    text = (f"PROXY_API_KEY={jwt}\nkey otok_AbCdEfGhIjKlMnOpQrSt\n"
            "Authorization: Bearer sk-live-0123456789abcdef\nplain words stay")
    out = chat_snapshot.redact_tokens(text)
    assert jwt not in out and "otok_AbCd" not in out and "sk-live" not in out
    assert out.count("[redacted]") == 3 and "plain words stay" in out
    assert chat_snapshot.redact_tokens("") == ""


def test_only_owner_or_editor_shares_and_task_runs_never():
    chat_id = _chat()
    _as(_user(OTHER, agent_roles={AGENT: "viewer"}))
    # Not the owner and not a member: the chat is not theirs to see.
    r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": chat_id, "grantee": EDITOR})
    assert r.status_code == 404
    # A shared-only agent's chat: editors may share it, viewers may not.
    shared_chat = str(uuid.uuid4())
    task_store.create_chat(shared_chat, f"agent::{AGENT}", AGENT)
    task_store.add_chat_message(shared_chat, "user", "hi")
    _as(_user(OTHER, agent_roles={AGENT: "viewer"}))
    task_store.add_user_agent(OTHER, AGENT, "viewer", "test")
    r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": shared_chat, "grantee": EDITOR})
    assert r.status_code == 403
    _as(_user(EDITOR, agent_roles={AGENT: "editor"}))
    assert client.post("/v1/shares", json={"target_kind": "chat", "target_id": shared_chat,
                                           "grantee": OTHER}).status_code == 200
    # A task run is never shareable.
    task_chat = f"task-{uuid.uuid4()}"
    task_store.create_chat(task_chat, f"task::{OWNER}", AGENT)
    _as(_user())
    r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": task_chat, "grantee": OTHER})
    assert r.status_code in (400, 404)
    # The listing flag says the same.
    listed = client.get(f"/v1/chats?agent={AGENT}").json()["chats"]
    flags = {c["id"]: c["can_share"] for c in listed}
    assert flags[chat_id] is True


# ───────────────────────── reading it ───────────────────────────────────────


def test_grantee_reads_the_snapshot_and_its_copies_after_the_chat_changed():
    chat_id = _chat()
    body = _share(chat_id)
    share_id = body["share"]["id"]
    # The chat moves on; the ui file is deleted; the snapshot does not care.
    task_store.add_chat_message(chat_id, "assistant", "later addition")
    (config.AGENTS_DIR / AGENT / "users/owner/workspace/generated-ui/chart-ab12.html").unlink()
    _as(_user(OTHER, agents=(), agent_roles={}))
    snap = client.get(f"/v1/shares/{share_id}/snapshot").json()
    assert snap["title"] == "Plan review" and snap["shared_by_name"] == "Owner"
    assert [m["role"] for m in snap["messages"]].count("assistant") == 1
    ui = client.get(f"/v1/shares/{share_id}/ui/tok-ui-1")
    assert ui.status_code == 200 and "chart" in ui.text
    assert "sandbox allow-scripts" in ui.headers["content-security-policy"]
    img = client.get(f"/v1/shares/{share_id}/media/img-1")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/png")
    pdf = client.get(f"/v1/shares/{share_id}/media/tok-file-1")
    assert pdf.status_code == 200
    assert client.get(f"/v1/shares/{share_id}/media/nope").status_code == 404
    # "Shared with me" names the page.
    mine = client.get("/v1/shares/mine").json()["shares"]
    assert mine[0]["href"] == f"/shared/{share_id}" and mine[0]["target_kind"] == "chat"
    # A stranger gets the same 404 as a missing share; a revoke closes it.
    _as(_user(EDITOR, agent_roles={AGENT: "editor"}))
    assert client.get(f"/v1/shares/{share_id}/snapshot").status_code == 404
    _as(_user())
    client.patch(f"/v1/shares/{share_id}", json={"revoke": True})
    _as(_user(OTHER, agents=(), agent_roles={}))
    assert client.get(f"/v1/shares/{share_id}/snapshot").status_code == 404


def test_chat_delete_removes_the_copies_and_the_reaper_sweeps_the_rest():
    chat_id = _chat()
    body = _share(chat_id)
    share = share_store.get_share(body["share"]["id"])
    root = config.AGENTS_DIR / share["snapshot_ref"]
    assert root.is_dir()
    assert client.delete(f"/v1/chats/{chat_id}").status_code == 200
    assert not root.exists() and share_store.get_share(share["id"]) is None
    # An orphan directory (no row) and a long-revoked share both go.
    orphan = config.AGENTS_DIR / AGENT / "shares" / "users" / "owner" / str(uuid.uuid4())
    orphan.mkdir(parents=True)
    chat2 = _chat()
    share2 = share_store.get_share(_share(chat2)["share"]["id"])
    share_store.revoke_share(share2["id"])
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE shares SET revoked_at='2020-01-01T00:00:00+00:00' WHERE id=%s",
                     (share2["id"],))
        conn.commit()
    from services.infra import retention
    stats: dict = {}
    retention._pass_share_snapshots(stats, dry_run=False)
    assert stats["share_snapshots_deleted"] == 2
    assert not orphan.exists()
    assert not (config.AGENTS_DIR / share2["snapshot_ref"]).exists()


def test_chat_link_serves_the_snapshot_outside():
    chat_id = _chat()
    r = client.post("/v1/shares", json={"target_kind": "chat", "target_id": chat_id,
                                        "scope": "external", "public": True, "password": ACCOUNT_PW})
    assert r.status_code == 200, r.text
    token = r.json()["link"].rsplit("/s/", 1)[1]
    page = client.get(f"/s/{token}")
    assert page.status_code == 200 and '"kind": "chat"' in page.text
    snap = client.get(f"/s/{token}/snapshot").json()
    assert snap["title"] == "Plan review" and len(snap["messages"]) == 5
    # An anonymous viewer gets the words, never an author's internal id.
    assert all("author_sub" not in m for m in snap["messages"])
    assert client.get(f"/s/{token}/ui/tok-ui-1").status_code == 200
    assert client.get(f"/s/{token}/media/img-1").status_code == 200
    # A link never gets an app's routes for a chat.
    assert client.get(f"/s/{token}/html").status_code == 404
    assert client.get(f"/s/{token}/state").status_code == 404


def test_snapshot_copy_refuses_swapped_symlink(tmp_path):
    # The file behind a media token is swapped for a link after the mint: the
    # copy is refused, the token is left out and the share still builds.
    chat_id = _chat()
    secret = tmp_path / "config.env"
    secret.write_text("JWT_SECRET=proxy-host-secret\n")
    f_path = config.AGENTS_DIR / AGENT / "users/owner/workspace/report.pdf"
    f_path.unlink()
    f_path.symlink_to(secret)
    body = _share(chat_id)
    share = share_store.get_share(body["share"]["id"])
    root = config.AGENTS_DIR / share["snapshot_ref"]
    doc = json.loads((root / "chat.json").read_text())
    assert "tok-file-1" not in doc["files"]
    assert [m.get("event_type", "") for m in doc["messages"]].count("file") == 0
    for p in root.rglob("*"):
        if p.is_file():
            assert b"JWT_SECRET" not in p.read_bytes()
    # The other copies are intact.
    assert (root / doc["files"]["tok-ui-1"]).read_text() == "<div class='card'>chart</div>"
