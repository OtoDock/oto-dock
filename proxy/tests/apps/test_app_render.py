"""The rendered check (services/apps/app_render.py, APPS.md "Deploy
pipeline"): the verdict matrix, the pictures kept beside the releases, the
render token and the check instance living exactly as long as the job, the
fallback when no renderer answers, and the deploy that refuses a page
which fails when loaded while it keeps one that merely warns.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

import config
from auth import render_principal as rp
from services.apps import app_deploy, app_render, app_supervisor, releases
from storage import database as task_store

AGENT = "render-job-agent"
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg").decode()


def _raw(**over) -> dict:
    base = {"ready": True, "frame": True, "banner": "", "console": [], "errors": [],
            "failed_requests": [], "responses": [], "csp": [], "ms": 900,
            "pages": [{"width": w, "theme": "light", "jpeg_b64": JPEG, "bytes": 22} for w in (390, 820, 1280)]
                     + [{"width": 1280, "theme": "dark", "jpeg_b64": JPEG, "bytes": 22}]}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    rp._live.clear()
    app_render._probe.update({"at": 0.0, "ok": False, "reason": "", "base": ""})
    yield agents_root / AGENT
    rp._live.clear()
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


@pytest.fixture
def renderer(monkeypatch):
    """A renderer that answers what the test puts in ``answers``; records
    the token it was called with."""
    calls: list[dict] = []
    answers: list[dict] = []

    async def available():
        return True, ""

    async def call(base, token, path, *, timeout_s):
        calls.append({"token": token, "path": path, "live": rp.verify(token) is not None})
        return answers.pop(0) if answers else _raw()
    monkeypatch.setattr(app_render, "renderer_available", available)
    app_render._probe["base"] = "http://file-tools:8932"
    monkeypatch.setattr(app_render, "_call_renderer", call)
    return {"calls": calls, "answers": answers}


def _static_app(agent_dir: Path, slug: str = "board", html: str = "<p>hi</p>") -> tuple[dict, Path]:
    src = agent_dir / "workspace" / "apps" / slug
    (src / "client").mkdir(parents=True)
    (src / "app.json").write_text(json.dumps({"title": slug.title()}))
    (src / "client" / "index.html").write_text(html)
    row = task_store.upsert_app(AGENT, "", None, slug, title=slug.title(),
                                rel_path=f"workspace/apps/{slug}", kind="folder")
    return row, src


def test_a_clean_render_keeps_its_pictures_and_frees_everything(renderer, _tree):
    row, src = _static_app(_tree)
    report = asyncio.run(app_render.render_working_tree(row, src, approved=True))
    assert report.status == "ok", report
    assert [im["name"] for im in report.images] == ["phone", "tablet", "desktop", "desktop-dark"]
    checks = releases.app_release_dir(row) / app_render.CHECKS_DIRNAME
    assert (checks / "phone.jpg").read_bytes().startswith(b"\xff\xd8")
    assert json.loads((checks / "report.json").read_text())["status"] == "ok"
    assert "jpeg_b64" not in json.loads((checks / "report.json").read_text())["images"][0]
    # The renderer was called with a live token naming the scratch copy.
    call = renderer["calls"][0]
    assert call["live"] is True and call["path"].startswith(f"/apps/{row['id']}?render=")
    # Everything the job held is gone: the token, the check instance, the copy.
    assert rp._live == {}
    assert app_supervisor.get(row["id"], "check") is None
    assert not list(releases.app_release_dir(row).glob("check-*"))
    assert "Rendered clean" in report.summary()


def test_two_renders_at_once_queue_for_the_one_renderer(renderer, _tree, monkeypatch):
    """file-tools renders one page at a time (a second call is a 503 that
    would read as "no renderer" and skip the check): two jobs at once wait
    their turn instead of overlapping."""
    one, src_one = _static_app(_tree, "one")
    two, src_two = _static_app(_tree, "two")
    busy = {"now": 0, "max": 0}

    async def call(base, token, path, *, timeout_s):
        busy["now"] += 1
        busy["max"] = max(busy["max"], busy["now"])
        await asyncio.sleep(0.2)
        busy["now"] -= 1
        return _raw()

    monkeypatch.setattr(app_render, "_call_renderer", call)
    # A lock of this test's own: contention binds one to its loop.
    monkeypatch.setattr(app_render, "_renderer_slot", asyncio.Lock(), raising=False)

    async def both():
        return await asyncio.gather(app_render.render_working_tree(one, src_one, approved=True),
                                    app_render.render_working_tree(two, src_two, approved=True))

    reports = asyncio.run(both())
    assert [r.status for r in reports] == ["ok", "ok"] and busy["max"] == 1


def test_the_verdict_matrix(renderer, _tree):
    row, src = _static_app(_tree)
    cases = [
        (_raw(ready=False, frame=False), "hard", "frame never loaded"),
        (_raw(ready=False, banner="Starting Board…"), "hard", "never became ready"),
        (_raw(errors=["TypeError: x is not a function"]), "hard", "uncaught error"),
        (_raw(csp=[{"directive": "script-src", "text": "Refused to load the script"}]), "hard", "script-src"),
        (_raw(csp=[{"directive": "img-src", "text": "Refused to load the image"}]), "soft", "img-src"),
        (_raw(responses=[{"url": "/v1/apps/x/client/abc/app.js", "status": 404, "server": ""}]), "hard", "answered 404"),
        (_raw(responses=[{"url": "/v1/apps/x/api/state", "status": 500, "server": ""}]), "hard", "API answered 500"),
        # The app's own API with a /client/ path is the API, not an asset.
        (_raw(responses=[{"url": "/v1/apps/x/api/client/42", "status": 404, "server": ""}]), "soft", "API answered 404"),
        (_raw(responses=[{"url": "/v1/apps/x/api/state", "status": 503, "server": "starting"}]), "ok", ""),
        (_raw(console=[{"level": "error", "text": "boom"}]), "soft", "console.error: boom"),
        (_raw(failed_requests=[{"url": "https://cdn.example/x.js", "reason": "blocked"}]), "soft", "request failed"),
        # A page wider than the phone scrolls sideways: measured, not seen.
        (_raw(pages=[{"width": 390, "theme": "light", "jpeg_b64": JPEG, "bytes": 22, "overflow": 140},
                     {"width": 1280, "theme": "light", "jpeg_b64": JPEG, "bytes": 22, "overflow": 6}]),
         "soft", "runs 140 px past the edge at phone width"),
    ]
    for raw, status, words in cases:
        renderer["answers"].append(raw)
        if not raw["ready"]:
            renderer["answers"].append(raw)   # the one retry
        report = asyncio.run(app_render.render_working_tree(row, src, approved=True))
        assert report.status == status, (raw, report.hard, report.soft)
        assert words in report.summary() or words in " ".join(report.hard + report.soft), (words, report)


def test_an_unapproved_manifest_renders_the_static_page_only(renderer, _tree):
    row, src = _static_app(_tree)
    renderer["answers"] += [_raw(ready=False, banner="Board is waiting for approval.")] * 2
    report = asyncio.run(app_render.render_working_tree(row, src, approved=False))
    assert report.status == "partial" and report.egress is False
    assert "before approval" in report.summary()


def test_no_renderer_means_unavailable_and_nothing_refused(monkeypatch, _tree):
    row, src = _static_app(_tree)

    async def down():
        return False, "file-tools did not answer"
    monkeypatch.setattr(app_render, "renderer_available", down)
    called = []
    monkeypatch.setattr(app_render, "_call_renderer", lambda *a, **k: called.append(1))
    report = asyncio.run(app_render.render_working_tree(row, src, approved=True))
    assert report.status == "unavailable" and "did not answer" in report.reason
    assert not called and rp._live == {}
    # The deploy goes ahead on the static checks alone.
    out = asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "board", src, "workspace/apps/board"))
    assert out["status"] == "ok" and out["render"]["status"] == "unavailable", out


def test_a_deploy_refuses_a_page_that_fails_and_keeps_one_that_warns(renderer, _tree):
    row, src = _static_app(_tree)
    renderer["answers"] += [_raw(errors=["ReferenceError: boot is not defined"])]
    out = asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "board", src, "workspace/apps/board"))
    assert out["status"] == "refused" and out["render"]["status"] == "hard", out
    assert "boot is not defined" in out["render"]["hard"][0]
    # The release copy went with the refusal and nothing serves.
    assert releases.release_numbers(task_store.get_app(row["id"])) == []
    renderer["answers"] += [_raw(console=[{"level": "warning", "text": "slow"}])]
    out = asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "board", src, "workspace/apps/board"))
    assert out["status"] == "ok" and out["release"] == 1 and out["render"]["status"] == "soft", out
    assert renderer["calls"][-1]["path"].startswith(f"/apps/{row['id']}?render=")
    # The rendered copy was release 1 itself, found by its hash.
    sha = renderer["calls"][-1]["path"].split("render=")[1]
    assert releases.find_release_by_sha(task_store.get_app(row["id"]), sha).name == "1"
    # A refused release that changed the manifest leaves the live one's on
    # the row: release 1 keeps its approval, nothing waits on the card.
    live = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(live)
    (src / "app.json").write_text(json.dumps({"title": "Board", "egress": ["api.example.com"]}))
    renderer["answers"] += [_raw(errors=["TypeError: nope"])]
    out = asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "board", src, "workspace/apps/board"))
    assert out["status"] == "refused", out
    after = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(after) and not after.get("egress")
    assert after["release_sha256"] == live["release_sha256"] and not int(after["pending_release"] or 0)


def test_the_live_release_renders_on_scratch_data(renderer, _tree):
    row, src = _static_app(_tree)
    asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "board", src, "workspace/apps/board"))
    row = task_store.get_app(row["id"])
    report = asyncio.run(app_render.render_live(row, approved=True))
    assert report.status == "ok"
    sha = renderer["calls"][-1]["path"].split("render=")[1]
    assert sha == row["release_sha256"]
    fresh = task_store.upsert_app(AGENT, "", None, "empty", title="Empty",
                                  rel_path="workspace/apps/empty", kind="folder")
    report = asyncio.run(app_render.render_live(fresh, approved=True))
    assert report.status == "unavailable" and "no release" in report.reason


def test_a_stale_check_copy_is_swept(renderer, _tree):
    import os
    import time
    row, src = _static_app(_tree)
    base = releases.app_release_dir(row)
    base.mkdir(parents=True, exist_ok=True)
    stale = base / "check-deadbeef"
    stale.mkdir()
    old = time.time() - 2 * app_render.STALE_CHECK_S
    os.utime(stale, (old, old))
    asyncio.run(app_render.render_working_tree(row, src, approved=True))
    assert not stale.exists()
