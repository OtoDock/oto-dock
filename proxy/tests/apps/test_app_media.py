"""An app's media links: ``files.media`` and the
cookie-less route that serves what it names.

Load-bearing: a link is made on a person's own page alone, for a file every
gate of ``files.read`` lets them read; every request on it is judged again
by the same functions, so a person, an approval, a folder or a file that
changed ends it at the next request; only an inert kind is ever answered,
by its name and by its bytes; the address never reaches a log line.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_media, app_proxy, apps as apps_api
from app import app
from auth import token_holder
from auth.providers import UserContext, get_current_user
from core import log_queue
from services.apps import (
    app_supervisor,
    app_tokens,
    call_rate,
    catalog,
    manifest as _mf,
    media_links,
)
from storage import database as task_store
from storage.pg import get_conn

client = TestClient(app)
AGENT = "media-agent"
MEDIA = [{"id": "m", "label": "Show", "type": "platform", "method": "files.media"}]
BOTH = [*MEDIA, {"id": "r", "label": "Read", "type": "platform", "method": "files.read"}]

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4096
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 512
PAGE = b"<!doctype html><html><script>1</script></html>"


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None, **extra) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles, **extra)


ALICE = _user()
BOB = _user("bob-sub", {AGENT: "viewer"})


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean():
    _as(ALICE)
    apps_api._fire_rate.clear()
    call_rate.reset()
    media_links.reset()
    app_media._places.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    call_rate.reset()
    media_links.reset()
    app_media._places.clear()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    for sub in ("users/alice/workspace", "workspace/media", "knowledge"):
        (agents_root / AGENT / sub).mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    media = agents_root / AGENT / "workspace" / "media"
    (media / "clip.mp4").write_bytes(MP4)
    (media / "shot.png").write_bytes(PNG)
    (media / "song.mp3").write_bytes(MP3)
    return agents_root / AGENT


def _row(slug: str = "studio", actions: list[dict] | None = None, *, files: dict | None = None,
         username: str = "", owner: str | None = None, approve: bool = True) -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    blocks = {"files": task_store.canonical_actions_json(files or {"read": ["workspace/media"]})}
    actions_json, err = _mf.validate_actions(actions or MEDIA, AGENT, shared=not username)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json=actions_json, blocks=blocks)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _ask(row: dict, path: str = "workspace/media/clip.mp4", method: str = "files.media", **args) -> dict:
    r = client.post(f"/v1/apps/{row['id']}/catalog/{method}", json={"args": {"path": path, **args}})
    assert r.status_code == 200, r.text
    return r.json()


def _link(row: dict, path: str = "workspace/media/clip.mp4") -> str:
    out = _ask(row, path)
    assert out["ok"], out
    return out["result"]["url"]


def _get(url: str, **headers):
    """The frame's request: no cookie, no signed-in caller."""
    _as(None)
    try:
        return client.get(url, headers=headers)
    finally:
        _as(ALICE)


class _Clock:
    """``media_links``' clock, moved by the test."""

    def __init__(self):
        import time
        self._time = time
        self.offset = 0.0

    def monotonic(self) -> float:
        return self._time.monotonic() + self.offset

    def time(self) -> float:
        return self._time.time() + self.offset


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(media_links, "time", c)
    return c


# ── the mint and the answer ────────────────────────────────────────────────


def test_a_page_gets_a_link_and_the_frame_plays_it_with_no_cookie(tree):
    row = _row()
    out = _ask(row)["result"]
    assert out["kind"] == "video" and out["mime"] == "video/mp4" and out["size"] == len(MP4)
    assert out["path"] == "workspace/media/clip.mp4" and out["expires_in"] == int(media_links.LEASE_S)
    assert re.fullmatch(rf"/v1/apps/{row['id']}/client/media\?v=[0-9a-f]{{12}}&t=[A-Za-z0-9_-]{{43}}", out["url"])
    r = _get(out["url"])
    assert r.status_code == 200 and r.content == MP4
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["x-accel-buffering"] == "no" and r.headers["accept-ranges"] == "bytes"
    assert "set-cookie" not in r.headers


def test_ranges_head_a_bad_range_and_a_preflight_all_carry_the_routes_headers(tree):
    url = _link(_row())
    part = _get(url, Range="bytes=4-7")
    assert part.status_code == 206 and part.content == b"ftyp"
    assert part.headers["content-range"] == f"bytes 4-7/{len(MP4)}"
    _as(None)
    head = client.head(url)
    bad = client.get(url, headers={"Range": f"bytes={len(MP4) + 5}-"})
    pre = client.options(url)
    _as(ALICE)
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) == len(MP4)
    assert bad.status_code == 416 and pre.status_code == 204
    assert "range" in pre.headers["access-control-allow-headers"]
    for answer in (part, head, bad, pre):
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in answer.headers["content-security-policy"]
        assert answer.headers["access-control-allow-origin"] == "*"
    # A changed file is answered whole, not as a stale part.
    etag = part.headers["etag"]
    assert _get(url, Range="bytes=0-3", **{"If-Range": etag}).status_code == 206
    assert _get(url, Range="bytes=0-3", **{"If-Range": '"another"'}).status_code == 200


def test_a_batch_answers_a_link_or_a_reason_for_each_file(tree):
    row = _row()
    r = client.post(f"/v1/apps/{row['id']}/catalog/files.media", json={"args": {"paths": [
        "workspace/media/clip.mp4", "workspace/media/shot.png", "workspace/media/none.png", "workspace/x.png"]}})
    links = r.json()["result"]["links"]
    assert [bool(e.get("url")) for e in links] == [True, True, False, False]
    assert links[1]["kind"] == "image" and links[2]["error"] == "not a file"
    assert "outside the app's declared" in links[3]["error"]
    assert _get(links[1]["url"]).content == PNG
    too_many = client.post(f"/v1/apps/{row['id']}/catalog/files.media",
                           json={"args": {"paths": ["workspace/media/shot.png"] * 33}}).json()
    assert too_many["ok"] is False and "1 to 32" in too_many["reason"]
    assert catalog.call_cost("files.media", {"paths": ["a"] * 9}) == 3.0
    assert catalog.call_cost("files.media", {"path": "a"}) == 1.0 == catalog.call_cost("files.read", {"paths": [1] * 9})


# ── one gate for the read and the link ─────────────────────────────────────


@pytest.mark.parametrize("method", ["files.read", "files.media"])
def test_every_gate_of_a_read_holds_for_a_link(tree, method):
    row = _row(actions=BOTH)
    ws = tree / "workspace"
    (ws / "private.png").write_bytes(PNG)
    (ws / "media" / "out.png").symlink_to(ws / "private.png")          # a link beneath the folder
    (ws / "media" / ".oauth").mkdir()
    (ws / "media" / ".oauth" / "t.png").write_bytes(PNG)
    for path, word in (("workspace/private.png", "outside the app's declared"),
                       ("workspace/media/out.png", "resolves outside"),
                       ("workspace/media/../private.png", ""),
                       ("workspace/media/.oauth/t.png", ""),
                       ("", "path is required")):
        out = _ask(row, path, method)
        assert out["ok"] is False and word in out["reason"], (path, out)
    # A share admits to the app, never to the agent's files.
    from storage.sharing import share_store
    task_store.upsert_user("carol-sub", "carol-sub@test.com", "Carol", "member")
    share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                      grantee_sub="carol-sub", created_by="alice-sub")
    _as(_user("carol-sub", {}))
    out = _ask(row, "workspace/media/shot.png", method)
    assert out["ok"] is False and "not available" in out["reason"]
    # An outside person reaches no platform method.
    with pytest.raises(PermissionError, match="outside people"):
        catalog.run_method(method, AGENT, row, SimpleNamespace(is_external=True, sub="x"),
                           {"path": "workspace/media/shot.png"})


def test_an_undeclared_or_unapproved_method_and_a_floor_refuse_the_ask(tree):
    assert client.post(f"/v1/apps/{_row('reads', BOTH[1:])['id']}/catalog/files.media",
                       json={"args": {"path": "workspace/media/clip.mp4"}}).status_code == 404
    assert client.post(f"/v1/apps/{_row('raw', approve=False)['id']}/catalog/files.media",
                       json={"args": {"path": "workspace/media/clip.mp4"}}).status_code == 409
    floored = _row("floored", [{**MEDIA[0], "min_role": "editor"}])
    _as(BOB)
    assert client.post(f"/v1/apps/{floored['id']}/catalog/files.media",
                       json={"args": {"path": "workspace/media/clip.mp4"}}).status_code == 403


# ── inert kinds, by the name and by the bytes ──────────────────────────────


@pytest.mark.parametrize("name,data", [
    ("a.mp4", MP4), ("a.m4v", MP4), ("a.m4a", MP4), ("a.webm", b"\x1a\x45\xdf\xa3rest"),
    ("a.weba", b"\x1a\x45\xdf\xa3rest"), ("a.mp3", MP3), ("b.mp3", b"ID3\x04rest"),
    ("a.wav", b"RIFF\x00\x00\x00\x00WAVEfmt "), ("a.ogg", b"OggS\x00"), ("a.opus", b"OggS\x00"),
    ("a.flac", b"fLaC\x00"), ("a.jpg", b"\xff\xd8\xff\xe0"), ("a.JPEG", b"\xff\xd8\xff\xe0"),
    ("a.png", PNG), ("a.gif", b"GIF89a"), ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
    ("a.avif", b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1avifmiaf"),
])
def test_what_a_browser_plays_or_draws_gets_a_link(tree, name, data):
    (tree / "workspace" / "media" / name).write_bytes(data)
    kind = media_links.kind_of(name)
    assert kind is not None and kind.check(data)
    out = _ask(_row(), f"workspace/media/{name}")
    assert out["ok"] and out["result"]["mime"] == kind.mime, out


@pytest.mark.parametrize("name,data,word", [
    ("page.html", PAGE, "not a video"), ("draw.svg", b"<svg onload='x'/>", "not a video"),
    ("doc.pdf", b"%PDF-1.7", "not a video"), ("list.m3u8", b"#EXTM3U", "not a video"),
    ("code.js", b"alert(1)", "not a video"), ("noext", MP4, "not a video"),
    ("clip.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ", "not a video"),
    ("page.mp4", PAGE, "not what its name says"),                      # a page file under a media name
    ("page.png", PAGE, "not what its name says"),
    ("text.mp3", "﻿hello".encode("utf-16"), "not what its name says"),   # FF FE reads as Layer I
    ("quick.mp4", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ", "not what its name says"),
    ("fake.avif", MP4, "not what its name says"),
    ("empty.png", b"", "not what its name says"),
])
def test_anything_else_gets_none_whatever_its_name_says(tree, name, data, word):
    (tree / "workspace" / "media" / name).write_bytes(data)
    out = _ask(_row(), f"workspace/media/{name}")
    assert out["ok"] is False and word in out["reason"], out


def test_a_pipe_and_a_folder_under_a_media_name_are_refused_without_a_wait(tree):
    os.mkfifo(tree / "workspace" / "media" / "pipe.mp4")
    (tree / "workspace" / "media" / "dir.png").mkdir()
    for name in ("pipe.mp4", "dir.png"):
        assert _ask(_row(), f"workspace/media/{name}")["ok"] is False


# ── only a person's own page ───────────────────────────────────────────────


def test_only_a_persons_own_page_gets_a_link(tree):
    row = _row()
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(AGENT),
                                   data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    viewer = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    body = {"args": {"path": "workspace/media/clip.mp4"}}
    url = f"/v1/apps/{row['id']}/platform/files.media"
    # The app's server, alone and with a person's claim, and the claim as the bearer.
    for headers in ({"Authorization": f"Bearer {inst.token}"},
                    {"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": viewer},
                    {"Authorization": f"Bearer {viewer}"}):
        r = client.post(url, json=body, headers=headers)
        assert r.status_code == 403 and "own page" in r.json()["detail"], r.text
    assert not media_links._links
    # A bearer, the render and the app itself, on the page's own route.
    for other in (_user(is_api_key=True), _user(render_app=row["id"]), catalog.app_principal(row)):
        with pytest.raises(PermissionError, match="own page"):
            catalog.run_method("files.media", AGENT, row, other, body["args"],
                               path_gate=lambda *a, **k: pytest.fail("the gate ran"))


def test_the_render_route_list_never_offers_the_method():
    from auth import render_principal
    assert not any("files.media" in rx.replace("\\", "") for _m, rx in render_principal._ALLOW)


# ── the lease ──────────────────────────────────────────────────────────────


def test_a_fresh_lease_is_told_whole_on_a_machine_that_started_minutes_ago(monkeypatch):
    # A lease read back as ``(now + LEASE_S) - now`` falls just under its
    # length where the sum crosses a power of two: a young clock does that often.
    for now in (3976.042499992593, 450.3, 1000.7, 1_207_688.25):
        monkeypatch.setattr(media_links.time, "monotonic", lambda now=now: now)
        media_links.reset()
        link, lease = media_links.issue("app-1", "alice-sub", "/x/clip.mp4", "workspace/media/clip.mp4", "video")
        assert lease == int(media_links.LEASE_S), now
        assert link.lease_until == pytest.approx(now + media_links.LEASE_S)


def test_the_lease_ends_the_same_address_comes_back_and_thirty_minutes_is_the_end(tree, clock):
    row = _row()
    url = _link(row)
    assert _get(url).status_code == 200
    clock.offset += media_links.LEASE_S + 1
    assert _get(url).status_code == 404                       # lapsed: nothing is answered
    assert _link(row) == url and _get(url).status_code == 200  # the page asked again: the same address
    # A page that keeps asking keeps the one address, until thirty minutes from its birth.
    asked = 0
    while clock.offset < media_links.MAX_LIFE_S - media_links.LEASE_S:
        clock.offset += 60
        assert _link(row) == url
        asked += 1
    assert asked > 20
    clock.offset += 60
    fresh = _link(row)
    assert fresh != url                                        # near its end an address is not given again
    assert _get(url).status_code == 200                        # and lives out the lease it had
    old = media_links._links[url.split("t=")[1]]
    assert old.lease_until <= old.born + media_links.MAX_LIFE_S
    clock.offset += media_links.LEASE_S + 1
    assert _get(url).status_code == 404 and _link(row) == fresh


def test_a_file_rewritten_under_its_name_answers_a_new_address(tree):
    """A browser keeps the picture it drew for an address: the address
    follows the file, so the page's next ask loads the new version."""
    row = _row()
    shot = tree / "workspace" / "media" / "shot.png"
    first = _link(row, "workspace/media/shot.png")
    assert _link(row, "workspace/media/shot.png") == first     # unchanged: the same address
    shot.write_bytes(PNG + b"brighter")                         # rewritten in place, another size
    second = _link(row, "workspace/media/shot.png")
    assert second != first
    assert _get(first).content == PNG + b"brighter"             # the old address lives out its lease
    replaced = shot.with_name("shot.tmp")
    replaced.write_bytes(PNG + b"brighter")                     # replaced whole, the same size
    os.utime(replaced, ns=(1, 1))
    replaced.replace(shot)
    third = _link(row, "workspace/media/shot.png")
    assert third not in (first, second)
    assert _get(third).content == PNG + b"brighter"
    shot.write_bytes(PNG + b"BRIGHTER")                         # in place: the same inode and size
    os.utime(shot, ns=(2, 2))                                   # a new time alone
    fourth = _link(row, "workspace/media/shot.png")
    assert fourth not in (first, second, third)
    assert len(media_links._links) == 1                         # one link, one secret, its lease shared
    assert {u.split("t=")[1] for u in (first, second, third, fourth)} == set(media_links._links)


def test_at_the_apps_bound_a_new_link_takes_a_lapsed_place_or_the_askers_own_never_another_persons(
        tree, monkeypatch, clock):
    row = _row()
    media = tree / "workspace" / "media"
    for i in range(3):
        (media / f"p{i}.png").write_bytes(PNG)
    monkeypatch.setattr(media_links, "MAX_PER_APP", 2)
    _as(BOB)
    b0, b1 = _link(row, "workspace/media/p0.png"), _link(row, "workspace/media/p1.png")
    _as(ALICE)
    out = _ask(row, "workspace/media/p2.png")                  # full of another person's live links
    assert out["ok"] is False and "try again in a moment" in out["reason"]
    assert _get(b0).status_code == 200 and _get(b1).status_code == 200 and len(media_links._links) == 2
    clock.offset += media_links.LEASE_S + 1                    # Bob's page stopped asking: both lapsed
    mine = _link(row, "workspace/media/p2.png")                # a lapsed link makes room
    newer = _link(row, "workspace/media/p0.png")               # and the other one
    assert len(media_links._links) == 2 and _get(b0).status_code == 404 and _get(b1).status_code == 404
    assert _get(mine).status_code == 200 and _get(newer).status_code == 200
    newest = _link(row, "workspace/media/p1.png")              # full, nothing lapsed: my own oldest goes
    assert _get(mine).status_code == 404 and _get(newer).status_code == 200 and _get(newest).status_code == 200


def test_an_agent_folder_an_operator_moved_behind_a_link_still_plays(tree, tmp_path):
    moved = tmp_path / "moved" / AGENT
    moved.parent.mkdir()
    tree.rename(moved)
    tree.symlink_to(moved)                                     # the agents folder now holds a link to it
    (tmp_path / "outside.png").write_bytes(PNG)
    (moved / "workspace" / "media" / "out.png").symlink_to(tmp_path / "outside.png")
    row = _row()
    url = _link(row)                                           # the one link the gate itself follows
    played = _get(url)
    assert played.status_code == 200 and played.content == MP4
    out = _ask(row, "workspace/media/out.png")                 # a link beneath it is refused as ever
    assert out["ok"] is False


def test_the_kind_is_the_resolved_files_not_the_one_its_alias_is_named_for(tree):
    row = _row()
    media = tree / "workspace" / "media"
    (media / "alias.m4a").symlink_to(media / "clip.mp4")       # an audio name that leads to a video
    out = _ask(row, "workspace/media/alias.m4a")["result"]
    assert out["kind"] == "video" and out["mime"] == "video/mp4"
    played = _get(out["url"])
    assert played.status_code == 200 and played.headers["content-type"] == "video/mp4"


def test_a_full_table_finds_a_lapsed_link_past_its_oldest_ones_at_most_once_a_while(tree, monkeypatch, clock):
    row = _row()
    media = tree / "workspace" / "media"
    for i in range(4):
        (media / f"p{i}.png").write_bytes(PNG)
    monkeypatch.setattr(media_links, "MAX_TOTAL", 4)
    monkeypatch.setattr(media_links, "_OLDEST", 1)             # the bound looks at the one oldest link first
    monkeypatch.setattr(media_links, "_SWEEP_MAX", 1)          # and a read of the whole table drops one link
    monkeypatch.setattr(media_links, "SCAN_EVERY_S", 10.0)
    video = _link(row)
    _link(row, "workspace/media/shot.png")
    song = media_links.find(_link(row, "workspace/media/song.mp3").split("t=")[1], row["id"])
    assert song is not None
    media_links.enter(song)                                    # an answer on the song began, and still streams
    _link(row, "workspace/media/p3.png")
    clock.offset += media_links.LEASE_S + 1                    # every page stopped asking
    assert _link(row) == video                                 # the oldest link is asked for again: it is live
    assert _ask(row, "workspace/media/p0.png")["ok"] is True   # full: a lapsed link further back makes room
    assert len(media_links._links) == 4
    again = _ask(row, "workspace/media/p1.png")                # full again, and the table was just read
    assert again["ok"] is False and "try again in a moment" in again["reason"]
    clock.offset += 10.0
    assert _ask(row, "workspace/media/p1.png")["ok"] is True   # the next read finds the other lapsed picture
    clock.offset += 10.0
    still = _ask(row, "workspace/media/p2.png")                # the one lapsed link left has an answer in flight
    assert still["ok"] is False and song.secret in media_links._links
    media_links.leave(song)                                    # the answer ended
    clock.offset += 10.0
    assert _ask(row, "workspace/media/p2.png")["ok"] is True   # the next read finds the song's link
    assert len(media_links._links) == 4 and song.secret not in media_links._links
    assert _get(video).status_code == 200                      # a live link was never the one to go


def test_two_people_and_two_files_never_share_an_address(tree):
    row = _row()
    mine = _link(row)
    assert _link(row, "workspace/media/shot.png") != mine
    _as(BOB)
    theirs = _link(row)
    assert theirs != mine and _link(row) == theirs


def test_the_table_drops_lapsed_links_first_then_pictures_and_never_one_in_flight(tree, monkeypatch, clock):
    row = _row()
    media = tree / "workspace" / "media"
    for i in range(4):
        (media / f"p{i}.png").write_bytes(PNG)
    monkeypatch.setattr(media_links, "MAX_PER_VIEWER", 3)
    video = _link(row)
    p0, p1 = _link(row, "workspace/media/p0.png"), _link(row, "workspace/media/p1.png")
    p2 = _link(row, "workspace/media/p2.png")                  # over the cap: the oldest picture goes
    assert _get(p0).status_code == 404 and _get(video).status_code == 200 and _get(p1).status_code == 200
    held = media_links.find(p1.split("t=")[1], row["id"])
    assert held is not None
    media_links.enter(held)
    assert _get(p2).status_code == 200                         # p2 was used last: p1 is the one to drop
    _link(row, "workspace/media/p3.png")                       # but p1 has an answer in flight: p2 goes instead
    assert _get(p2).status_code == 404
    assert _get(p1).status_code == 200 and _get(video).status_code == 200
    media_links.leave(held)
    # All in flight and nothing to drop: the ask is refused, the table is not grown.
    with monkeypatch.context() as m:
        m.setattr(media_links, "_victim", lambda held, now: None)
        out = _ask(row, "workspace/media/p0.png")
    assert out["ok"] is False and "try again in a moment" in out["reason"]
    assert len(media_links._links) == 3
    monkeypatch.setattr(media_links, "MAX_PER_VIEWER", 9)
    monkeypatch.setattr(media_links, "MAX_TOTAL", 3)
    out = _ask(row, "workspace/media/p0.png")                  # full, and no link of anyone has lapsed
    assert out["ok"] is False and len(media_links._links) == 3
    clock.offset += media_links.LEASE_S + 1                    # everything lapsed
    assert _ask(row, "workspace/media/p0.png")["ok"] and len(media_links._links) == 3
    monkeypatch.setattr(media_links, "MAX_PER_APP", 1)
    assert _ask(row, "workspace/media/p1.png")["ok"] and len(media_links._links) == 3


def test_signing_out_everywhere_and_an_unpin_drop_the_links_at_once(tree):
    row = _row()
    url, bobs = _link(row), None
    _as(BOB)
    bobs = _link(row)
    token_holder.forget("alice-sub")
    assert _get(url).status_code == 404 and _get(bobs).status_code == 200
    assert media_links.drop_app(row["id"]) == 1 and not media_links._links


# ── every request is judged again ──────────────────────────────────────────


def _sql(statement: str, *params) -> None:
    with get_conn() as conn:
        conn.execute(statement, params)
        conn.commit()


def test_a_member_removed_from_the_agent_loses_the_file_at_the_next_request(tree):
    row = _row()
    _as(BOB)
    url = _link(row)
    assert _get(url).status_code == 200
    _sql("DELETE FROM user_agents WHERE sub = %s AND agent = %s", "bob-sub", AGENT)
    r = _get(url)
    assert r.status_code == 404 and r.json() == {"detail": "Not found"} and MP4 not in r.content
    assert r.headers["x-content-type-options"] == "nosniff"
    # The refused request holds nothing: the viewer's place and the link's count are given back.
    assert not app_media._places and all(link.in_flight == 0 for link in media_links._links.values())


def test_a_person_who_is_gone_or_whose_sign_in_ended_after_the_link_is_refused(tree, clock):
    row = _row()
    _sql("UPDATE users SET created_at = (now() - interval '1 hour')::text WHERE sub = %s", "alice-sub")
    clock.offset = -60.0                                       # the page last asked a minute ago
    url = _link(row)
    clock.offset = 0.0
    assert _get(url).status_code == 200
    # Another process ended every sign-in thirty seconds ago: after the link's last ask, before now.
    _sql("UPDATE users SET token_epoch_at = (now() - interval '30 seconds')::text WHERE sub = %s", "alice-sub")
    assert _get(url).status_code == 404
    _sql("UPDATE users SET token_epoch_at = NULL, must_change_password = TRUE WHERE sub = %s", "alice-sub")
    assert _get(url).status_code == 404                       # held at the password gate
    _sql("UPDATE users SET must_change_password = FALSE WHERE sub = %s", "alice-sub")
    assert _get(url).status_code == 200
    task_store.delete_user("alice-sub")
    assert _get(url).status_code == 404


def test_an_approval_a_floor_a_folder_and_an_app_that_changed_end_the_link(tree):
    row = _row()
    url = _link(row)
    stale, err = _mf.validate_actions([{**MEDIA[0], "label": "Show more"}], AGENT, shared=True)
    assert stale is not None, err
    task_store.upsert_app(AGENT, "", None, "studio", actions_json=stale)
    assert _get(url).status_code == 404                       # the manifest changed: not approved
    fresh = task_store.get_app(row["id"])
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(fresh), "alice-sub")
    assert _get(url).status_code == 200
    _row(files={"read": ["workspace/other"]})                 # the folder left the declaration
    assert _get(url).status_code == 404
    row = _row()
    assert _get(url).status_code == 200
    floored, _ = _mf.validate_actions([{**MEDIA[0], "min_role": "manager"}], AGENT, shared=True)
    task_store.upsert_app(AGENT, "", None, "studio", actions_json=floored)
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(task_store.get_app(row["id"])), "alice-sub")
    assert _get(url).status_code == 200                       # the manager still clears it
    _as(BOB)
    assert client.post(f"/v1/apps/{row['id']}/catalog/files.media",
                       json={"args": {"path": "workspace/media/clip.mp4"}}).status_code == 403
    _sql("UPDATE user_agents SET agent_role = 'viewer' WHERE sub = %s AND agent = %s", "alice-sub", AGENT)
    assert _get(url).status_code == 404                       # below the floor now
    _sql("UPDATE user_agents SET agent_role = 'manager' WHERE sub = %s AND agent = %s", "alice-sub", AGENT)
    task_store.set_app_hidden(row["id"], True)
    assert _get(url).status_code == 404                       # unpinned


def test_a_file_that_is_gone_replaced_or_became_another_ends_the_link(tree):
    row = _row()
    media = tree / "workspace" / "media"
    (media / "real.png").write_bytes(PNG)
    (media / "other.png").write_bytes(PNG + b"other")
    (media / "alias.png").symlink_to(media / "real.png")       # a link that stays inside the folder
    alias = _link(row, "workspace/media/alias.png")
    assert _get(alias).content == PNG
    (media / "alias.png").unlink()
    (media / "alias.png").symlink_to(media / "other.png")      # the name now leads to another file
    assert _get(alias).status_code == 404
    url = _link(row)
    (media / "clip.mp4").write_bytes(PAGE)                     # a page put under the media name
    assert _get(url).status_code == 404
    (media / "clip.mp4").write_bytes(MP4 + b"new")
    assert _get(url).content == MP4 + b"new"                   # the open is per request
    (media / "clip.mp4").unlink()
    assert _get(url).status_code == 404


def test_another_apps_address_an_unknown_secret_and_a_dormant_app_answer_nothing(tree, monkeypatch):
    row, other = _row(), _row("other")
    url = _link(row)
    secret = url.split("t=")[1]
    assert _get(f"/v1/apps/{other['id']}/client/media?t={secret}").status_code == 404
    assert _get(f"/v1/apps/{row['id']}/client/media?t={secret[:-2]}xx").status_code == 404
    assert _get(f"/v1/apps/{row['id']}/client/media").status_code == 404
    # The client-files routes never take the address for one of theirs.
    assert _get(f"/v1/apps/{row['id']}/client/media/?t={secret}").status_code in (401, 404)
    # A dormant app answers nobody, an admin included (the access rule lets an admin in first).
    task_store.upsert_user("root-sub", "root-sub@test.com", "Root", "admin")
    _as(UserContext(sub="root-sub", email="root-sub@test.com", name="root", role="admin", agents=[]))
    admins = _link(row, "workspace/media/shot.png")
    assert _get(admins).status_code == 200
    monkeypatch.setattr(app_media.db_apps, "personal_row_dormant", lambda r: True)
    assert _get(admins).status_code == 404 and _get(url).status_code == 404


def test_only_a_media_element_is_answered_where_the_browser_says_what_asks(tree):
    url = _link(_row())
    for dest in ("video", "audio", "image", "IMAGE"):
        assert _get(url, **{"Sec-Fetch-Dest": dest}).status_code == 200, dest
    for dest in ("document", "iframe", "embed", "object", "empty", "script", "style", "worker", ""):
        assert _get(url, **{"Sec-Fetch-Dest": dest}).status_code == 404, dest


def test_a_request_for_several_ranges_is_refused(tree):
    row = _row()
    url = _link(row)
    assert _get(url, Range="bytes=0-9").status_code == 206     # what a media element asks
    several = _get(url, Range="bytes=0-9, 100-109")
    assert several.status_code == 404 and several.json() == {"detail": "Not found"}
    assert several.headers["x-content-type-options"] == "nosniff"
    assert not app_media._places and app_media._waiting == 0   # refused before any place


def test_requests_and_answers_in_flight_are_bounded_per_viewer(tree, monkeypatch):
    row = _row()
    url = _link(row)
    monkeypatch.setattr(app_media, "REQUEST_BURST", 2.0)
    monkeypatch.setattr(app_media, "REQUESTS_PER_S", 0.001)
    codes = [_get(url).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert _get(url).headers["retry-after"] == "1"
    call_rate.reset()
    monkeypatch.setattr(app_media, "IN_FLIGHT_PER_VIEWER", 1)
    monkeypatch.setattr(app_media, "IN_FLIGHT_WAIT_S", 0.05)
    held = media_links.find(url.split("t=")[1], row["id"])
    assert held is not None
    place = app_media._places[(row["id"], held.sub)] = call_rate.Gate(1)
    place.busy = 1                                             # one answer is still streaming
    started = time.monotonic()
    refused = _get(url)
    assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
    assert time.monotonic() - started < 2.0                    # after the short wait, not a long one
    assert not place.waiters and held.in_flight == 0           # the wait ran out: the line is left
    place.leave()
    assert _get(url).status_code == 200
    assert not app_media._places and held.in_flight == 0


def test_a_request_past_the_viewers_places_waits_in_line_for_one(tree, monkeypatch):
    row = _row()
    link = media_links.find(_link(row).split("t=")[1], row["id"])
    assert link is not None
    monkeypatch.setattr(app_media, "IN_FLIGHT_PER_VIEWER", 1)

    async def run() -> None:
        assert await app_media._enter(link)
        second = asyncio.ensure_future(app_media._enter(link))
        await asyncio.sleep(0.01)
        assert not second.done() and link.in_flight == 1       # it waits, and holds nothing yet
        app_media._leave(link)                                 # the first answer ends
        assert await second and link.in_flight == 1
        monkeypatch.setattr(app_media, "WAITING_PER_VIEWER", 0)
        assert not await asyncio.wait_for(app_media._enter(link), 0.5)   # a full line refuses at once
        app_media._leave(link)
        assert not app_media._places and link.in_flight == 0

    asyncio.run(run())


def test_a_viewers_gate_left_idle_by_a_wait_that_ran_out_is_dropped(tree, monkeypatch):
    row = _row()
    link = media_links.find(_link(row).split("t=")[1], row["id"])
    assert link is not None
    monkeypatch.setattr(app_media, "IN_FLIGHT_WAIT_S", 0.0)    # the wait's time is up as it begins

    async def run() -> None:
        place = app_media._places[(row["id"], link.sub)] = call_rate.Gate(1)
        place.busy = 1                                         # the viewer's one answer is streaming
        waiting = asyncio.ensure_future(app_media._enter(link))
        await asyncio.sleep(0)                                 # it stands in line
        place.leave()                                          # the answer ends in the same turn: the place is handed over
        assert not await waiting                               # too late for this request, which gives the place on
        assert not app_media._places and link.in_flight == 0   # nobody is left: the gate goes

    asyncio.run(run())


def test_media_requests_that_wait_are_bounded_across_the_platform(tree, monkeypatch):
    row = _row()
    url = _link(row)
    counted: list[int] = []
    judge = app_media._judge
    monkeypatch.setattr(app_media, "_judge", lambda link: (counted.append(app_media._waiting), judge(link))[1])
    assert _get(url).status_code == 200
    assert counted == [1] and app_media._waiting == 0          # counted until its answer starts
    monkeypatch.setattr(app_media, "WAITING_PLATFORM", 2)
    monkeypatch.setattr(app_media, "_waiting", 2)              # two requests of other people wait
    refused = _get(url)
    assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
    assert app_media._waiting == 2 and len(counted) == 1       # refused before any place or gate
    monkeypatch.setattr(app_media, "_waiting", 1)
    assert _get(url).status_code == 200 and app_media._waiting == 1


# ── an answer that runs long ───────────────────────────────────────────────


def _stream(response, method: str = "GET") -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "headers": [], "path": "/", "query_string": b""}
    asyncio.run(response(scope, receive, send))
    return sent


def _response(tree, row, size: int = 400_000):
    path = tree / "workspace" / "media" / "long.mp4"
    path.write_bytes(MP4 + b"\x00" * size)
    url = _link(row, "workspace/media/long.mp4")
    link = media_links.find(url.split("t=")[1], row["id"])
    assert link is not None
    media_links.enter(link)
    fd = os.open(path, os.O_RDONLY)
    return link, fd, app_media.JudgedFileResponse(fd, os.fstat(fd), link=link, media_type="video/mp4")


def test_an_answer_that_runs_long_is_judged_again_and_ends_where_it_is_refused(tree, monkeypatch):
    row = _row()
    link, fd, response = _response(tree, row)
    monkeypatch.setattr(app_media, "RECHECK_S", 0.0)
    judged: list[int] = []

    def judge(seen):
        judged.append(1)
        if len(judged) == 1:
            again = os.open(tree / "workspace" / "media" / "long.mp4", os.O_RDONLY)
            return media_links.MediaFile(again, os.fstat(again), seen.rel, seen.kind, "video/mp4")
        return None                                            # the person was removed meanwhile

    monkeypatch.setattr(app_media, "_judge", judge)
    sent = _stream(response)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert sent[0]["type"] == "http.response.start" and len(judged) == 2
    assert len(bodies) == 1 and bodies[0].get("more_body") is True   # cut after the chunk judged fine
    assert sum(len(b.get("body", b"")) for b in bodies) < 400_000
    assert (b"x-content-type-options", b"nosniff") in sent[0]["headers"]
    with pytest.raises(OSError):
        os.fstat(fd)                                           # the descriptor is released
    assert link.in_flight == 0 and not app_media._places


def test_an_answer_ends_when_its_reader_went_away(tree):
    row = _row()
    link, fd, response = _response(tree, row, size=2_000_000)  # about thirty chunks
    key = (row["id"], link.sub)
    place = app_media._places[key] = call_rate.Gate(app_media.IN_FLIGHT_PER_VIEWER)
    place.busy = 1                                             # the place this answer holds
    sent: list[dict] = []

    async def scene() -> None:
        gone = asyncio.Event()
        asked = 0

        async def receive():
            nonlocal asked
            asked += 1
            if asked == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body":
                gone.set()                                     # the reader leaves after the first chunk

        scope = {"type": "http", "method": "GET", "headers": [], "path": "/", "query_string": b""}
        await response(scope, receive, send)

    asyncio.run(scene())
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert 1 <= len(bodies) <= 2                               # one more chunk at most, never the whole file
    assert not app_media._places and link.in_flight == 0       # the place is given back
    with pytest.raises(OSError):
        os.fstat(fd)                                           # and the descriptor is closed


def test_an_answer_that_began_in_the_lease_runs_on_after_the_lease_and_the_life(tree, clock, monkeypatch):
    """The lease bounds when a request begins, not how long its answer runs
    (decision 42 says so): the answer is judged as the person at every
    re-check, never against the lease."""
    row = _row()
    link, _fd, response = _response(tree, row, size=400_000)
    monkeypatch.setattr(app_media, "RECHECK_S", 0.0)
    clock.offset += media_links.MAX_LIFE_S + media_links.LEASE_S + 60
    assert media_links.find(link.secret, row["id"]) is None    # no new request begins on the address
    sent = _stream(response)
    body = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    assert body == os.path.getsize(tree / "workspace" / "media" / "long.mp4")


def _open_files() -> int:
    gc.collect()
    return len(os.listdir("/proc/self/fd"))


def test_no_refusal_and_no_recheck_leaves_a_file_open(tree, monkeypatch):
    row = _row()
    media = tree / "workspace" / "media"
    (media / "page.mp4").write_bytes(PAGE)                     # a kind its bytes refuse
    (media / "real.png").write_bytes(PNG)
    (media / "other.png").write_bytes(PNG + b"other")
    (media / "alias.png").symlink_to(media / "real.png")
    alias = _link(row, "workspace/media/alias.png")
    (media / "alias.png").unlink()
    (media / "alias.png").symlink_to(media / "other.png")      # the name now leads to another file
    monkeypatch.setattr(app_media, "RECHECK_S", 0.0)           # a long answer is judged again at every chunk

    def each_sort(times: int) -> None:
        for _ in range(times):
            assert _ask(row, "workspace/media/page.mp4")["ok"] is False
            assert _get(alias).status_code == 404
        _link_, _fd, response = _response(tree, row)
        sent = _stream(response)                               # judged by the real gate, which opens the file
        assert sum(len(m.get("body", b"")) for m in sent[1:]) == len(MP4) + 400_000

    each_sort(1)                                               # whatever the first use opens for good is open now
    before = _open_files()
    each_sort(20)
    assert _open_files() == before


def test_a_file_that_shrank_under_the_read_ends_quietly_and_a_whole_answer_is_whole(tree, monkeypatch):
    row = _row()
    link, _fd, response = _response(tree, row)
    sent = _stream(response)
    assert sum(len(m.get("body", b"")) for m in sent[1:]) == len(MP4) + 400_000
    assert _stream(_response(tree, row)[2], "HEAD")[-1].get("body", b"") == b"" and link.in_flight == 0

    async def short(self, scope, receive, send):
        raise RuntimeError("Response content shorter than Content-Length")

    async def broken(self, scope, receive, send):
        raise RuntimeError("something else")

    link, _fd, response = _response(tree, row)
    with monkeypatch.context() as m:
        m.setattr(app_media.FdFileResponse, "__call__", short)
        asyncio.run(response({"type": "http"}, None, None))    # no error reaches the server
    assert link.in_flight == 0
    link, _fd, response = _response(tree, row)
    with monkeypatch.context() as m:
        m.setattr(app_media.FdFileResponse, "__call__", broken)
        with pytest.raises(RuntimeError, match="something else"):
            asyncio.run(response({"type": "http"}, None, None))
    assert link.in_flight == 0


# ── the address stays out of the log ───────────────────────────────────────


def test_the_secret_reaches_no_log_line(tree, caplog):
    row = _row()
    with caplog.at_level(logging.DEBUG):
        url = _link(row)
        secret = url.split("t=")[1]
        assert _get(url).status_code == 200
        _sql("DELETE FROM user_agents WHERE sub = %s AND agent = %s", "alice-sub", AGENT)
        assert _get(url).status_code == 404
    assert len(secret) >= 43 and secret not in caplog.text
    assert any("media link refused" in r.getMessage() for r in caplog.records)
    line = f'192.168.1.9:5 - "GET {url} HTTP/1.1" 206'
    assert secret not in log_queue.redact(line) and "&t=[redacted]" in log_queue.redact(line)
    assert secret not in log_queue.redact(f"https://x.example/a?u=1&t={secret}&v=2")


def test_the_bucket_is_the_one_every_app_route_counts_with():
    assert app_proxy._take is call_rate.take and app_proxy._buckets is call_rate._buckets
