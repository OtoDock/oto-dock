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


def test_a_persons_photos_are_counted_never_copied():
    chat_id = _chat()
    meta = {"images": [{"name": "a.jpg", "path": "users/owner/workspace/uploads/photos/a.jpg"},
                       {"name": "b.jpg", "path": "users/owner/workspace/uploads/photos/b.jpg"}]}
    task_store.add_chat_message(chat_id, "user", "", event_data=json.dumps(meta))
    task_store.add_chat_message(chat_id, "user", "and this file", event_data=json.dumps(
        {"files": [{"name": "x.pdf", "path": "users/owner/workspace/uploads/files/x.pdf"}]}))
    body = _share(chat_id)
    share = share_store.get_share(body["share"]["id"])
    doc = json.loads((config.AGENTS_DIR / share["snapshot_ref"] / "chat.json").read_text())
    photo_only, with_file = doc["messages"][-2:]
    # A message of photos alone keeps its place, with the count.
    assert photo_only == {"role": "user", "content": "", "created_at": photo_only["created_at"],
                          "author_sub": "", "attachments": {"photos": 2, "files": 0}}
    assert with_file["content"] == "and this file"
    assert with_file["attachments"] == {"photos": 0, "files": 1}
    # The first row carried nothing: no count.
    assert "attachments" not in doc["messages"][0]
    assert not any("uploads" in rel for rel in doc["files"].values())


def test_a_settings_card_stays_as_a_placeholder_with_and_without_tools():
    """The copy keeps a setting's card as its label, never a value, whether
    or not the tools are included."""
    from ws import wire_events as wire
    chat_id = _chat()
    task_store.add_chat_message(chat_id, "event", event_type=wire.SETTINGS_CARD,
                                event_data=json.dumps(wire.settings_card_block(
                                    "user.general.wake-word", "Wake word", 1)))
    for grantee, include_tools in ((OTHER, False), (EDITOR, True)):
        body = _share(chat_id, grantee=grantee, include_tools=include_tools)
        doc = chat_snapshot.load(share_store.get_share(body["share"]["id"]))
        cards = [m for m in doc["messages"] if m.get("event_type") == wire.SETTINGS_CARD]
        assert len(cards) == 1
        # The artifact blocks' shape: the renderers read ``data.label``.
        assert cards[0]["data"] == {"setting": "user.general.wake-word", "label": "Wake word", "title": ""}
        assert "event_data" not in cards[0] and "value" not in json.dumps(cards[0])


def test_an_action_card_stays_as_a_placeholder_naming_its_title():
    """The copy keeps an action card as its title alone: no button, no
    note, nothing a reader of the copy could press."""
    from ws import wire_events as wire
    chat_id = _chat()
    task_store.add_chat_message(chat_id, "event", event_type=wire.ACTION_CARD,
                                event_data=json.dumps(wire.action_card_block(
                                    "card-1", "Move Alpha to Eng", "Places Alpha at the Team level.", "a note",
                                    [{"id": "go", "label": "Move", "kind": "do"}], [])))
    body = _share(chat_id, grantee=OTHER)
    doc = chat_snapshot.load(share_store.get_share(body["share"]["id"]))
    (card,) = [m for m in doc["messages"] if m.get("event_type") == wire.ACTION_CARD]
    assert card["data"] == {"setting": "", "label": "", "title": "Move Alpha to Eng"}
    assert "Move" not in json.dumps(card["data"]).replace("Move Alpha", "") and "note" not in card["data"]


def test_a_list_card_stays_as_a_placeholder_naming_its_title_and_kind():
    """The copy keeps a list card as its title and kind: no row of the list
    (none was ever in the row), and never the person it was drawn for."""
    from ws import wire_events as wire
    chat_id = _chat()
    task_store.add_chat_message(chat_id, "event", event_type=wire.LIST_CARD,
                                event_data=json.dumps(wire.list_card_block(
                                    "tasks", {"scope": "mine"}, "Your scheduled tasks", "user-admin")))
    body = _share(chat_id, grantee=OTHER)
    doc = chat_snapshot.load(share_store.get_share(body["share"]["id"]))
    (card,) = [m for m in doc["messages"] if m.get("event_type") == wire.LIST_CARD]
    assert card["data"] == {"setting": "", "label": "", "title": "Your scheduled tasks", "kind": "tasks"}
    assert "user-admin" not in json.dumps(card) and "filters" not in card["data"]


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
    # Nobody signed in: the frame's own sign-in page, never a JSON refusal.
    app.dependency_overrides.pop(get_current_user, None)
    anon = client.get(f"/v1/shares/{share_id}/ui/tok-ui-1")
    assert anon.status_code == 401 and anon.headers["content-type"].startswith("text/html")
    assert "Sign in to OtoDock" in anon.text
    _as(_user(OTHER, agents=(), agent_roles={}))
    img = client.get(f"/v1/shares/{share_id}/media/img-1")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/png")
    pdf = client.get(f"/v1/shares/{share_id}/media/tok-file-1")
    assert pdf.status_code == 200
    assert client.get(f"/v1/shares/{share_id}/media/nope").status_code == 404
    # The section names the page; a chat needs no decision.
    items = client.get("/v1/shares/inbox").json()["items"]
    assert [(i["href"], i["kind"], i["actions"]) for i in items] == \
        [(f"/shared/{share_id}", "chat", ["open", "hide"])]
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


def test_the_daily_sweep_removes_a_revoked_chat_share_s_copy_then_its_row():
    """Thirty days after a revoke the sweep removes the copy (its own pass,
    which finds the folder through the row) and then the row."""
    from datetime import datetime, timedelta, timezone

    from services.infra import retention
    from services.infra.retention import LiveSnapshot
    from storage.pg import get_conn
    chat_id = _chat()
    share = share_store.get_share(_share(chat_id)["share"]["id"])
    root = config.AGENTS_DIR / share["snapshot_ref"]
    assert root.is_dir()
    share_store.revoke_share(share["id"])
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE shares SET revoked_at=%s WHERE id=%s", (old, share["id"]))
        conn.commit()
    from unittest.mock import patch

    from services.sharing import chat_snapshot
    # The orphan sweep would take the copy in either order: with it off,
    # only the revoked-row path, which needs the row, can remove it.
    with patch.object(chat_snapshot, "sweep_orphans", return_value=0):
        stats = retention._run_sweep_sync(30, False, LiveSnapshot(), False)
    assert stats["share_snapshots_deleted"] >= 1 and stats["shares_deleted"] == 1
    assert not root.exists() and share_store.get_share(share["id"]) is None


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
    # An anonymous viewer gets the words, never an author's internal id nor
    # a row's.
    assert all("author_sub" not in m and "id" not in m for m in snap["messages"])
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


# ───────────────────────── reporting a reply from the copy ──────────────────


def _rows_of(chat_id: str) -> list[dict]:
    from storage.pg import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT id, role, event_type FROM chat_messages WHERE chat_id = %s "
                            "ORDER BY id", (chat_id,)).fetchall()


def _reports() -> list[dict]:
    from storage.pg import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reply_reports ORDER BY id").fetchall()


@pytest.fixture
def fired(monkeypatch):
    from services.notifications import notification_manager as nm
    calls: list[dict] = []

    async def fake_fire(**kw):
        # The share's own notice to its reader is not a report's.
        if kw.get("source") == "report":
            calls.append(kw)
        return [{"id": "d"}]
    monkeypatch.setattr(nm, "fire_notification", fake_fire)
    return calls


def test_the_copy_keeps_the_agents_row_ids_only():
    chat_id = _chat()
    rows = {(r["role"], r["event_type"] or ""): r["id"] for r in _rows_of(chat_id)}
    share = share_store.get_share(_share(chat_id, include_tools=True)["share"]["id"])
    doc = json.loads((config.AGENTS_DIR / share["snapshot_ref"] / "chat.json").read_text())
    ids = {(m["role"], m.get("event_type", "")): m.get("id") for m in doc["messages"]}
    assert ids[("user", "")] is None and ids[("event", "thinking")] is None
    for kind in (("assistant", ""), ("event", "tool"), ("event", "ui"), ("event", "images"),
                 ("event", "file")):
        assert ids[kind] == rows[kind]


def _report_from(share_id: str, message_id: int, reason: str = "harmful"):
    return client.post(f"/v1/shares/{share_id}/reports",
                       json={"message_id": message_id, "reason": reason, "note": ""})


def test_a_reader_of_the_copy_reports_the_chats_reply(fired):
    chat_id = _chat()
    share_id = _share(chat_id)["share"]["id"]
    rows = _rows_of(chat_id)
    answer = next(r["id"] for r in rows if r["role"] == "assistant")
    first = next(r["id"] for r in rows if r["role"] != "user")
    _as(_user(OTHER, agents=(), agent_roles={}))
    r = _report_from(share_id, answer)
    # The answer is the copy's own row; the stored report names the live
    # reply's first row, which a copy made without tools does not carry.
    assert r.status_code == 200 and r.json() == {"status": "reported", "message_id": answer}
    [row] = _reports()
    assert (row["reporter_sub"], row["chat_id"], row["message_id"]) == (OTHER, chat_id, first)
    assert row["reply_text"] == "Here it is, with a chart." and row["chat_title"] == "Plan review"
    assert fired[0]["href"] == f"/chat/{AGENT}/{chat_id}?message={first}"


def test_the_copys_words_when_the_row_was_written_again(fired):
    chat_id = _chat()
    share_id = _share(chat_id)["share"]["id"]
    answer = next(r["id"] for r in _rows_of(chat_id) if r["role"] == "assistant")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE id = %s", (answer,))
        conn.commit()
    _as(_user(OTHER, agents=(), agent_roles={}))
    r = _report_from(share_id, answer)
    assert r.status_code == 200 and r.json()["message_id"] == answer
    [row] = _reports()
    assert (row["chat_id"], row["message_id"], row["reply_text"]) == (
        chat_id, answer, "Here it is, with a chart.")
    assert fired[0]["href"] == f"/chat/{AGENT}/{chat_id}"


def test_a_text_less_block_of_the_copy_is_described(fired):
    chat_id = _chat()
    share_id = _share(chat_id)["share"]["id"]
    ui = next(r["id"] for r in _rows_of(chat_id) if r["event_type"] == "ui")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE id = %s", (ui,))
        conn.commit()
    _as(_user(OTHER, agents=(), agent_roles={}))
    assert _report_from(share_id, ui).status_code == 200
    # From that block to the next person's message: every part it showed.
    assert _reports()[0]["reply_text"] == (
        "Showed the app \u201cChart\u201d.\nShowed 2 images.\nShowed the file report.pdf.")


def test_only_the_shares_readers_report_from_it_and_only_its_blocks(fired):
    chat_id = _chat()
    share_id = _share(chat_id)["share"]["id"]
    rows = _rows_of(chat_id)
    answer = next(r["id"] for r in rows if r["role"] == "assistant")
    person = next(r["id"] for r in rows if r["role"] == "user")
    _as(_user(EDITOR, agent_roles={AGENT: "editor"}))
    assert _report_from(share_id, answer).status_code == 404
    _as(UserContext(sub=OTHER, email="o@test.com", name=OTHER, role="member", is_api_key=True))
    assert _report_from(share_id, answer).status_code == 403
    _as(_user(OTHER, agents=(), agent_roles={}))
    r = _report_from(share_id, person)
    assert r.status_code == 404 and r.json()["detail"] == "That message is not in this shared chat."
    assert _report_from(share_id, answer, reason="nope").status_code == 422
    _as(_user())
    client.patch(f"/v1/shares/{share_id}", json={"revoke": True})
    _as(_user(OTHER, agents=(), agent_roles={}))
    assert _report_from(share_id, answer).status_code == 404
    assert not _reports() and not fired


def test_a_copys_reply_whose_first_row_is_gone_keeps_the_whole_reply(fired):
    chat_id = _chat()
    share_id = _share(chat_id, include_tools=True)["share"]["id"]
    tool = next(r["id"] for r in _rows_of(chat_id) if r["event_type"] == "tool")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE id = %s", (tool,))
        conn.commit()
    _as(_user(OTHER, agents=(), agent_roles={}))
    assert _report_from(share_id, tool).status_code == 200
    # The run the flag names starts at the tool; its text is the reply's,
    # not "Used Read." alone.
    [row] = _reports()
    assert (row["message_id"], row["reply_text"]) == (tool, "Here it is, with a chart.")


def test_a_copys_text_less_reply_is_described_part_by_part():
    from api.agents.reply_reports import copy_reply_text
    messages = [
        {"role": "user", "content": "Q"},
        {"id": 5, "role": "event", "event_type": "tool", "event_data": json.dumps({"name": "Bash"})},
        {"role": "event", "event_type": "thinking", "event_data": json.dumps({"text": "x"})},
        {"id": 7, "role": "event", "event_type": "images", "data": {"images": [{"token": "a"}]}},
        {"role": "user", "content": "Next"},
        {"id": 9, "role": "assistant", "content": "Not this one."},
    ]
    assert copy_reply_text(messages, 1) == "Used Bash.\nShowed an image."
