"""The document pane's downloads (api/media/document_downloads.py): the pane's
proof, the conversions per type, the forced name and type of the media link
they end in, the bounds, the locks and the one-shot token file-tools checks."""

import asyncio
import base64
import os

import httpx
import pytest
from fastapi import FastAPI, Request

AGENT = "test-agent"
DOCX = "test-agent/workspace/Report.docx"


def _fid(rel: str) -> str:
    return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _fresh():
    from api.media import document_downloads
    document_downloads.forget()
    yield
    document_downloads.forget()


class _Rig:
    """The routes of the pane's downloads and the media route, one person who
    may open chat ``c1``, its pushes, and a fake file-tools."""

    def __init__(self, monkeypatch, tmp_path):
        import config
        from api.media import document_downloads, media
        from auth.providers import UserContext, get_current_user
        from services.apps import app_render
        from services.infra import pinned_http
        from services.media import media_pipeline, preview_snapshots
        from storage import database as db

        self.tmp = tmp_path
        self.agents = tmp_path / "agents"
        self.snaps = tmp_path / "snaps"
        self.agents.mkdir()
        self.snaps.mkdir()
        monkeypatch.setattr(config, "AGENTS_DIR", self.agents, raising=False)
        monkeypatch.setattr(config, "PREVIEW_SNAPSHOT_DIR", self.snaps, raising=False)
        monkeypatch.setattr(media_pipeline, "_HOST_CACHE_DIR", tmp_path / "host-cache")
        self.events: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.access = True
        db.create_chat("c1", "alice-sub", AGENT)
        monkeypatch.setattr(db, "get_chat", lambda cid: {"id": cid, "agent": AGENT, "session_id": ""})
        monkeypatch.setattr("core.session.visibility.can_access_chat", lambda u, c: self.access)
        monkeypatch.setattr(db, "get_preview_event_by_file", lambda cid, fid: self.events.get(fid))
        monkeypatch.setattr(db, "get_preview_event_by_snapshot",
                            lambda cid, sid: self.snapshots.get(sid))
        monkeypatch.setattr(preview_snapshots, "snapshot_path",
                            lambda cid, sid: (self.snaps / cid / sid) if (self.snaps / cid / sid).exists() else None)
        monkeypatch.setattr(app_render, "renderer_base_url", lambda: "http://file-tools.test")
        self.calls: list[httpx.Request] = []
        self.answer = lambda req: httpx.Response(200, content=b"%PDF-1.7 converted")
        rig = self

        async def _handler(request: httpx.Request) -> httpx.Response:
            rig.calls.append(request)
            out = rig.answer(request)
            return await out if asyncio.iscoroutine(out) else out

        monkeypatch.setattr(pinned_http, "tool_transport",
                            lambda **kw: {**kw, "transport": httpx.MockTransport(_handler)})
        self.user = UserContext(sub="alice-sub", email="a@t.com", name="Alice", role="creator",
                                agents=[AGENT], agent_roles={AGENT: "manager"})
        app = FastAPI()
        app.include_router(document_downloads.router)
        app.include_router(media.router)

        def _stub(request: Request):
            sub = request.headers.get("x-person")
            if not sub:
                return rig.user
            return UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role="creator",
                               agents=[AGENT], agent_roles={AGENT: "manager"})
        app.dependency_overrides[get_current_user] = _stub
        self.app = app

    def push(self, rel: str, body: bytes = b"PK\x03\x04 docx", *, filename: str | None = None) -> str:
        p = self.agents / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        fid = _fid(rel)
        self.events[fid] = {"file_id": fid, "filename": filename or rel.rsplit("/", 1)[-1]}
        return fid

    def version(self, fid: str, sid: str, body: bytes, *, filename: str = "Report.docx") -> None:
        (self.snaps / "c1").mkdir(exist_ok=True)
        (self.snaps / "c1" / sid).write_bytes(body)
        self.snapshots[sid] = {"file_id": fid, "filename": filename, "snapshot_id": sid}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")

    async def ask(self, fid: str, fmt: str = "pdf", snapshot_id: str = "", client=None,
                  person: str = "") -> httpx.Response:
        body = {"chat_id": "c1", "file_id": fid, "snapshot_id": snapshot_id, "format": fmt}
        headers = {"x-person": person} if person else {}
        if client is not None:
            return await client.post("/v1/documents/download", json=body, headers=headers)
        async with self.client() as c:
            return await c.post("/v1/documents/download", json=body, headers=headers)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    return _Rig(monkeypatch, tmp_path)


# ── what each type offers ───────────────────────────────────────────────────


def test_the_conversions_per_type():
    from api.media.document_downloads import targets_for
    assert targets_for("a.docx") == ("pdf",)
    assert targets_for("b.ODT") == ("pdf",)
    assert targets_for("c.xlsx") == ("pdf", "csv")
    assert targets_for("d.xls") == ("pdf", "csv")
    assert targets_for("e.pptx") == ("pdf",)
    assert targets_for("f.pdf") == ()
    assert targets_for("g.csv") == ()
    assert targets_for("") == ()


def test_the_name_a_download_takes():
    from api.media.document_downloads import download_name
    assert download_name("Report.docx", "pdf") == "Report.pdf"
    assert download_name("Αναφορά 2026.xlsx", "csv") == "Αναφορά 2026.csv"
    assert download_name('a"b\nc.docx', "pdf") == 'a"bc.pdf'
    assert download_name("../../x.docx", "pdf") == "x.pdf"
    assert download_name("..\\..\\evil.docx", "pdf") == "evil.pdf"
    assert download_name(" .docx", "pdf") == "document.pdf"
    assert download_name("Deck.pptx", "") == "Deck.pptx"
    long = download_name("é" * 300 + ".docx", "pdf")
    assert long.endswith(".pdf") and len(long.encode()) <= 200


# ── a conversion, end to end through the media route ────────────────────────


@pytest.mark.asyncio
async def test_a_word_file_downloads_as_pdf_through_a_media_link(rig):
    fid = rig.push(DOCX)
    async with rig.client() as c:
        r = await rig.ask(fid, client=c)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "Report.pdf"
        assert body["download_url"].startswith("/v1/media/")
        call = rig.calls[0]
        assert call.url.path == "/convert"
        assert dict(call.url.params) == {"ext": "docx", "format": "pdf"}
        assert call.content == b"PK\x03\x04 docx"
        served = await c.get(body["download_url"] + "&fn=..%2Fevil.html")
    assert served.status_code == 200
    assert served.content == b"%PDF-1.7 converted"
    assert served.headers["content-type"] == "application/pdf"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cache-control"] == "no-store"
    disposition = served.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "Report.pdf" in disposition
    assert "evil" not in disposition


@pytest.mark.asyncio
async def test_file_tools_is_in_use_before_it_is_woken_and_until_the_conversion_ends(rig, monkeypatch):
    """No sweep can put the tool to sleep between its wake and the call."""
    from types import SimpleNamespace

    from services.apps import app_render
    from services.containers import door, state
    state.reset_for_tests()
    monkeypatch.setattr(app_render, "_renderer_tool", lambda: SimpleNamespace(name="file-tools"))
    seen: list[bool] = []

    async def ready(manifest, **_kw):
        seen.append(state.in_use(manifest.name))
        return ""
    monkeypatch.setattr(door, "ready", ready)
    fid = rig.push(DOCX)
    async with rig.client() as c:
        r = await rig.ask(fid, client=c)
    assert r.status_code == 200, r.text
    assert seen == [True]
    assert not state.in_use("file-tools")


@pytest.mark.asyncio
async def test_the_row_is_a_converted_cache_owned_link_that_expires(rig):
    from datetime import datetime

    from storage import database as db
    fid = rig.push(DOCX)
    r = await rig.ask(fid)
    token = r.json()["download_url"].split("/v1/media/")[1].split("?")[0]
    row = db.get_media_token(token)
    assert row["media_kind"] == "converted"
    assert row["mime"] == "application/pdf"
    assert row["chat_id"] == "c1" and row["agent"] == AGENT
    assert row["cache_owned"] == 1
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(datetime.fromisoformat(row["expires_at"]).tzinfo)
    assert row["abs_path"].endswith("/converted/" + token + "/Report.pdf")


@pytest.mark.asyncio
async def test_a_greek_name_gets_a_clean_header(rig):
    fid = rig.push("test-agent/workspace/Αναφορά.xlsx", filename="Αναφορά.xlsx")
    rig.answer = lambda req: httpx.Response(200, content="Όνομα,1\n".encode())
    async with rig.client() as c:
        r = await rig.ask(fid, "csv", client=c)
        served = await c.get(r.json()["download_url"])
    assert served.headers["content-type"] == "text/csv; charset=utf-8"
    assert "filename*=utf-8''%CE%91" in served.headers["content-disposition"]
    assert "\n" not in served.headers["content-disposition"]


@pytest.mark.asyncio
async def test_a_version_downloads_as_it_is_and_as_pdf(rig):
    fid = rig.push(DOCX, b"PK live")
    rig.version(fid, "v1abc", b"PK version one")
    async with rig.client() as c:
        plain = await rig.ask(fid, "", snapshot_id="v1abc", client=c)
        assert plain.status_code == 200, plain.text
        assert plain.json()["filename"] == "Report.docx"
        assert not rig.calls
        served = await c.get(plain.json()["download_url"])
        assert served.content == b"PK version one"
        assert served.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        converted = await rig.ask(fid, "pdf", snapshot_id="v1abc", client=c)
    assert converted.status_code == 200
    assert rig.calls[0].content == b"PK version one"


# ── the proof ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_person_without_the_chat_gets_nothing(rig):
    fid = rig.push(DOCX)
    rig.version(fid, "v1abc", b"PK v1")
    rig.access = False
    assert (await rig.ask(fid)).status_code == 403
    assert (await rig.ask(fid, "", snapshot_id="v1abc")).status_code == 403
    assert not rig.calls


@pytest.mark.asyncio
async def test_a_link_is_served_only_to_who_may_open_the_chat(rig):
    fid = rig.push(DOCX)
    async with rig.client() as c:
        url = (await rig.ask(fid, client=c)).json()["download_url"]
        rig.access = False
        assert (await c.get(url)).status_code == 404


@pytest.mark.asyncio
async def test_a_file_the_pane_does_not_hold_is_404(rig):
    rig.push(DOCX)
    other = _fid("test-agent/workspace/secret.docx")
    (rig.agents / "test-agent/workspace/secret.docx").write_bytes(b"PK secret")
    assert (await rig.ask(other)).status_code == 404
    rig.events[_fid(DOCX)] = None
    assert (await rig.ask(_fid(DOCX))).status_code == 404
    assert not rig.calls


@pytest.mark.asyncio
async def test_an_event_naming_a_path_outside_the_tree_is_refused(rig):
    (rig.tmp / "outside.docx").write_bytes(b"PK outside")
    fid = _fid("../outside.docx")
    rig.events[fid] = {"file_id": fid, "filename": "outside.docx"}
    assert (await rig.ask(fid)).status_code == 403
    assert not rig.calls


@pytest.mark.asyncio
async def test_a_version_of_another_file_is_404(rig):
    fid = rig.push(DOCX)
    other = rig.push("test-agent/workspace/Other.docx")
    rig.version(other, "v9xyz", b"PK other")
    assert (await rig.ask(fid, "", snapshot_id="v9xyz")).status_code == 404


@pytest.mark.asyncio
async def test_a_link_swapped_in_after_the_gate_is_404(rig, monkeypatch):
    from api.media import document_downloads, wopi
    fid = rig.push(DOCX)
    (rig.tmp / "elsewhere.docx").write_bytes(b"PK elsewhere")
    real = wopi.live_document

    def _swap(*a, **kw):
        out = real(*a, **kw)
        target = rig.agents / DOCX
        target.unlink()
        os.symlink(rig.tmp / "elsewhere.docx", target)
        return out
    monkeypatch.setattr(document_downloads.wopi, "live_document", _swap)
    assert (await rig.ask(fid)).status_code == 404
    assert not rig.calls


# ── what the route refuses ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_target_the_type_does_not_offer_is_400(rig):
    fid = rig.push(DOCX)
    pdf = rig.push("test-agent/workspace/Scan.pdf", b"%PDF-1.4")
    assert (await rig.ask(fid, "csv")).status_code == 400
    assert (await rig.ask(pdf, "pdf")).status_code == 400
    assert (await rig.ask(fid, "")).status_code == 400
    assert not rig.calls


@pytest.mark.asyncio
async def test_a_file_over_the_cap_is_413_before_any_call(rig, monkeypatch):
    from api.media import document_downloads
    monkeypatch.setattr(document_downloads, "MAX_INPUT_BYTES", 8)
    fid = rig.push(DOCX, b"PK" + b"x" * 20)
    r = await rig.ask(fid)
    assert r.status_code == 413
    assert not rig.calls


@pytest.mark.asyncio
async def test_an_answer_over_the_cap_leaves_no_link(rig, monkeypatch):
    from api.media import document_downloads
    from storage import database as db
    monkeypatch.setattr(document_downloads, "MAX_ANSWER_BYTES", 4)
    fid = rig.push(DOCX)
    r = await rig.ask(fid)
    assert r.status_code == 413
    root = document_downloads.converted_root()
    assert not any(root.iterdir())
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media_tokens").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_file_tools_refusals_become_sentences(rig):
    fid = rig.push(DOCX)
    rig.answer = lambda req: httpx.Response(404)
    r = await rig.ask(fid)
    assert r.json()["detail"] == "This install needs an update to convert documents. Ask an admin."
    assert r.status_code == 503
    rig.answer = lambda req: httpx.Response(422, json={"reason": "the engine said no"})
    r = await rig.ask(fid)
    assert r.status_code == 502 and r.json()["detail"] == "The PDF could not be made from this file."

    def _slow(req):
        raise httpx.ReadTimeout("slow", request=req)
    rig.answer = _slow
    r = await rig.ask(fid)
    assert r.status_code == 504 and "took too long" in r.json()["detail"]


@pytest.mark.asyncio
async def test_no_file_tools_is_503(rig, monkeypatch):
    from services.apps import app_render
    monkeypatch.setattr(app_render, "renderer_base_url", lambda: "")
    fid = rig.push(DOCX)
    r = await rig.ask(fid)
    assert r.status_code == 503 and "not available" in r.json()["detail"]


# ── the locks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_download_per_person_at_a_time(rig):
    fid = rig.push(DOCX)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def _held(req):
        started.set()
        await gate.wait()
        return httpx.Response(200, content=b"%PDF")
    rig.answer = _held
    async with rig.client() as c:
        first = asyncio.create_task(rig.ask(fid, client=c))
        await started.wait()
        second = await rig.ask(fid, client=c)
        assert second.status_code == 429
        gate.set()
        assert (await first).status_code == 200


@pytest.mark.asyncio
async def test_a_failed_download_frees_the_person(rig):
    fid = rig.push(DOCX)

    def _timeout(req):
        raise httpx.ReadTimeout("slow", request=req)
    for answer in (_timeout, lambda req: httpx.Response(422, json={})):
        rig.answer = answer
        assert (await rig.ask(fid)).status_code in (502, 504)
    rig.answer = lambda req: httpx.Response(200, content=b"%PDF")
    assert (await rig.ask(fid)).status_code == 200


@pytest.mark.asyncio
async def test_a_cancelled_download_frees_the_person(rig):
    from api.media import document_downloads
    fid = rig.push(DOCX)
    started = asyncio.Event()

    async def _never(req):
        started.set()
        await asyncio.Event().wait()
    rig.answer = _never
    async with rig.client() as c:
        task = asyncio.create_task(rig.ask(fid, client=c))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "alice-sub" not in document_downloads._busy
    assert document_downloads._running == 0


@pytest.mark.asyncio
async def test_a_third_download_across_the_install_waits_then_is_503(rig, monkeypatch):
    from api.media import document_downloads
    monkeypatch.setattr(document_downloads, "SLOT_WAIT_S", 0.3)
    fid = rig.push(DOCX)
    gate = asyncio.Event()
    both = asyncio.Event()
    started: list[int] = []

    async def _held(req):
        started.append(1)
        if len(started) == 2:
            both.set()
        await gate.wait()
        return httpx.Response(200, content=b"%PDF")
    rig.answer = _held
    async with rig.client() as c:
        tasks = [asyncio.create_task(rig.ask(fid, client=c, person=p)) for p in ("p0", "p1")]
        await both.wait()
        third = await rig.ask(fid, client=c, person="p2")
        assert third.status_code == 503 and "busy" in third.json()["detail"]
        gate.set()
        assert [ (await t).status_code for t in tasks] == [200, 200]


@pytest.mark.asyncio
async def test_a_persons_next_download_removes_their_previous_one(rig):
    from api.media import document_downloads
    fid = rig.push(DOCX)
    first = (await rig.ask(fid)).json()["download_url"]
    second = (await rig.ask(fid)).json()["download_url"]
    folders = [p.name for p in document_downloads.converted_root().iterdir()]
    assert folders == [second.split("/v1/media/")[1].split("?")[0]]
    async with rig.client() as c:
        assert (await c.get(first)).status_code == 404


# ── the one-shot token file-tools checks ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_conversion_token_works_once(rig):
    seen: list[str] = []
    fid = rig.push(DOCX)

    def _capture(req):
        seen.append(req.headers["authorization"].split(" ", 1)[1])
        return httpx.Response(200, content=b"%PDF")
    rig.answer = _capture
    assert (await rig.ask(fid)).status_code == 200
    async with rig.client() as c:
        verify = "/v1/internal/convert/verify"
        assert (await c.get(verify)).status_code == 401
        assert (await c.get(verify, headers={"Authorization": "Bearer nope"})).status_code == 401
        auth = {"Authorization": f"Bearer {seen[0]}"}
        assert (await c.get(verify, headers=auth)).status_code == 204
        assert (await c.get(verify, headers=auth)).status_code == 401


@pytest.mark.asyncio
async def test_an_old_conversion_token_is_refused(rig, monkeypatch):
    from api.media import document_downloads
    token = document_downloads._mint_token()
    key = document_downloads._token_key(token)
    document_downloads._tokens[key] -= document_downloads.TOKEN_TTL_S + 1
    async with rig.client() as c:
        r = await c.get("/v1/internal/convert/verify", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# ── the listing ─────────────────────────────────────────────────────────────


def test_the_listing_says_what_each_document_offers(temp_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api.media import wopi
    from auth.providers import UserContext, get_current_user
    from storage import database as db
    rows = [
        {"id": 1, "chat_id": "c1", "generation": 1, "turn": 1,
         "data": {"file_id": "a", "filename": "Budget.xlsx", "download_url": "/v1/media/x"}},
        {"id": 2, "chat_id": "c1", "generation": 2, "turn": 1,
         "data": {"file_id": "b", "filename": "Scan.pdf", "download_url": "/v1/media/y"}},
    ]
    monkeypatch.setattr(db, "get_chat", lambda cid: {"id": cid, "agent": AGENT, "session_id": ""})
    monkeypatch.setattr("core.session.visibility.can_access_chat", lambda u, c: True)
    monkeypatch.setattr(db, "get_chat_preview_listing_rows", lambda ids: rows)
    user = UserContext(sub="s", email="a@t.com", name="A", role="creator",
                       agents=[AGENT], agent_roles={AGENT: "manager"})

    async def _stub():
        return user
    app = FastAPI()
    app.include_router(wopi.router)
    app.dependency_overrides[get_current_user] = _stub
    docs = TestClient(app).get("/v1/documents/chat-documents?chat_id=c1").json()["documents"]
    assert {d["file_id"]: d["downloads"] for d in docs} == {"a": ["pdf", "csv"], "b": []}


def test_the_gate_reads_off_the_loop(rig, monkeypatch):
    from fastapi.testclient import TestClient

    from storage import database as db
    fid = rig.push(DOCX)
    on_loop: list[bool] = []
    real = db.get_preview_event_by_file

    def _guarded(*a):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*a)
    monkeypatch.setattr(db, "get_preview_event_by_file", _guarded)
    r = TestClient(rig.app).post("/v1/documents/download",
                                 json={"chat_id": "c1", "file_id": fid, "format": "pdf"})
    assert r.status_code == 200, r.text
    assert on_loop and not any(on_loop)


@pytest.mark.asyncio
async def test_file_tools_that_do_not_answer_is_a_sentence(rig):
    fid = rig.push(DOCX)

    def _down(req):
        raise httpx.ConnectError("refused", request=req)
    rig.answer = _down
    r = await rig.ask(fid)
    assert r.status_code == 503
    assert r.json()["detail"] == "The PDF could not be made right now. Try again in a moment."


# ── the expiry is the media sweep's ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_media_sweep_removes_an_expired_download_and_its_folder(rig):
    import time

    from api.media import document_downloads
    from services.media import media_pipeline
    from storage import database as db
    fid = rig.push(DOCX)
    url = (await rig.ask(fid)).json()["download_url"]
    token = url.split("/v1/media/")[1].split("?")[0]
    folder = document_downloads.converted_root() / token
    with db.get_conn() as conn:
        conn.execute("UPDATE media_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE token=%s", (token,))
        conn.commit()
    import startup_sweeps
    await startup_sweeps._sweep_media()
    assert db.get_media_token(token) is None
    assert folder.is_dir() and not any(folder.iterdir())
    media_pipeline.sweep_host_media_cache()
    assert folder.is_dir()  # a young folder may be a conversion still writing
    old = time.time() - 600
    os.utime(folder, (old, old))
    media_pipeline.sweep_host_media_cache()
    assert not folder.exists()


def test_the_media_sweep_takes_a_folder_a_cut_conversion_left(rig):
    import time

    from api.media import document_downloads
    from services.media import media_pipeline
    root = document_downloads.converted_root()
    left = root / "cut"
    left.mkdir(parents=True)
    (left / "Report.pdf").write_bytes(b"%PDF partial")
    fresh = root / "fresh"
    fresh.mkdir()
    (fresh / "Deck.pdf").write_bytes(b"%PDF")
    old = time.time() - 7300
    os.utime(left, (old, old))
    media_pipeline.sweep_host_media_cache()
    assert not left.exists()
    assert (fresh / "Deck.pdf").exists()
