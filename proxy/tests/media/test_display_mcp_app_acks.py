"""display-mcp's app acks for the rendered check (APPS.md "Deploy pipeline").

`check_app`, `deploy_app` and `screenshot_app` hand the agent the verdict
in words AND the pictures the platform's browser took, as image content;
a step refused by the render alone is worded without a findings list.
The hook is patched: these are the MCP's words, not the proxy's job.
"""

from __future__ import annotations

import base64
import sys

import pytest
from mcp.types import ImageContent, TextContent

from tests._paths import CUSTOM_MCPS

MCP_DIR = CUSTOM_MCPS / "display-mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import app_tools  # noqa: E402

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 not really a jpeg").decode()


def _render(status="ok", **over):
    base = {
        "status": status, "summary": f"summary for {status}", "errors": [], "csp": [],
        "failed_requests": [], "hard": [], "soft": [],
        "images": [
            {"name": "phone", "width": 390, "theme": "light", "bytes": 10, "jpeg_b64": JPEG},
            {"name": "desktop-dark", "width": 1280, "theme": "dark", "bytes": 10, "jpeg_b64": JPEG},
        ],
    }
    base.update(over)
    return base


def _patch(monkeypatch, answer: dict):
    calls: list[tuple[str, dict, float]] = []

    async def fake(path, payload, *, read=60.0):
        calls.append((path, payload, read))
        return answer, ""

    monkeypatch.setattr(app_tools, "_post_hook", fake)
    return calls


def _texts(parts):
    return "\n".join(p.text for p in parts if isinstance(p, TextContent))


def _images(parts):
    return [p for p in parts if isinstance(p, ImageContent)]


@pytest.mark.asyncio
async def test_check_attaches_the_pictures_and_names_them(monkeypatch):
    calls = _patch(monkeypatch, {"status": "ok", "files": 3, "manifest": {"actions": 1},
                                 "server": "up", "render": _render()})
    parts = await app_tools._handle_deploy_hook("check", {"slug": "board"})
    text = _texts(parts)
    assert "'board' checks out" in text
    assert "summary for ok" in text
    assert "phone (390px), desktop-dark (1280px)" in text
    imgs = _images(parts)
    assert len(imgs) == 2 and imgs[0].mimeType == "image/jpeg" and imgs[0].data == JPEG
    # The rendering hooks get the long read timeout; the others keep 60 s.
    assert calls[0][2] == 240.0


@pytest.mark.asyncio
async def test_check_refused_by_the_render_alone_is_worded(monkeypatch):
    _patch(monkeypatch, {"status": "refused", "findings": [], "problems": 0, "warnings": 0,
                         "render": _render("hard", hard=["the page threw: x is not defined"],
                                           errors=[{"message": "x is not defined", "url": "index.html:3"}])})
    parts = await app_tools._handle_deploy_hook("check", {"slug": "board"})
    text = _texts(parts)
    assert "'board' is NOT ready: summary for hard" in text
    assert "check_app again" in text
    assert "Page errors:" in text and "x is not defined" in text
    assert len(_images(parts)) == 2


@pytest.mark.asyncio
async def test_deploy_refused_by_findings_still_carries_the_render(monkeypatch):
    _patch(monkeypatch, {
        "status": "refused", "problems": 1, "warnings": 0,
        "findings": [{"severity": "fail", "file": "client/index.html", "line": 9,
                      "rule": "raw-fetch", "message": "use otodock.fetch"}],
        "render": _render("soft", soft=["console error: boom"])})
    parts = await app_tools._handle_deploy_hook("deploy", {"slug": "board"})
    text = _texts(parts)
    assert "1 problem(s)" in text and "raw-fetch" in text
    assert "summary for soft" in text


@pytest.mark.asyncio
async def test_a_render_refusal_with_warnings_says_nothing_was_deployed(monkeypatch):
    _patch(monkeypatch, {
        "status": "refused", "problems": 0, "warnings": 1,
        "findings": [{"severity": "warn", "file": "server/index.ts", "line": 1,
                      "rule": "no-health", "message": "answer /_health"}],
        "render": _render("hard", hard=["the page threw"])})
    text = _texts(await app_tools._handle_deploy_hook("deploy", {"slug": "board"}))
    assert text.startswith("'board' is NOT ready: summary for hard") and "Nothing was deployed" in text
    assert "Worth fixing" in text and "no-health" in text


@pytest.mark.asyncio
async def test_pin_app_on_a_folder_words_the_deploy_it_ran(monkeypatch):
    """A folder pin runs the deploy pipeline: its answer carries no
    ``approval`` and must never read as a live single-file pin."""
    calls = _patch(monkeypatch, {"status": "pending approval", "release": 2, "app_id": "a1",
                                 "manifest_changed": True, "live_release": 1, "superseded": 0,
                                 "render": _render("partial")})
    parts = await app_tools._handle_pin_app({"slug": "board"})
    text = _texts(parts)
    assert "WAITING FOR APPROVAL" in text and "Viewers keep release 1" in text
    assert "No actions declared" not in text and calls[0][2] == 240.0
    assert len(_images(parts)) == 2
    _patch(monkeypatch, {"status": "refused", "findings": [], "app_id": "a1",
                         "render": _render("hard", hard=["the page threw"])})
    text = _texts(await app_tools._handle_pin_app({"slug": "board"}))
    assert "'board' is NOT ready" in text and "pin_app again" in text
    # A single-file pin keeps its own words.
    _patch(monkeypatch, {"status": "ok", "app_id": "a2", "path": "/workspace/apps/one.html",
                         "scope": "shared", "pin_scope": "standing", "release": 1,
                         "approval": "none"})
    assert "No actions declared" in _texts(await app_tools._handle_pin_app({"slug": "one", "html": "<p>x</p>"}))


@pytest.mark.asyncio
async def test_import_waits_for_the_render(monkeypatch):
    calls = _patch(monkeypatch, {"status": "refused", "findings": [], "slug": "board",
                                 "render": _render("hard", hard=["x"])})
    text = _texts(await app_tools._handle_bundle("import", {"path": "b.otoapp"}))
    assert "'board' is NOT ready" in text and "import_app again" in text and calls[0][2] == 240.0


@pytest.mark.asyncio
async def test_a_hook_that_does_not_answer_in_time_says_so(monkeypatch):
    """A read timeout's own text is empty: the agent read "Error (deploy): "
    while the deploy went on finishing on the platform."""
    import httpx

    class _Slow:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(app_tools.httpx, "AsyncClient", _Slow)
    data, err = await app_tools._post_hook("/v1/hooks/apps/deploy", {}, read=240.0)
    assert data is None and "no answer within 240 s" in err and "deploy_status" in err


@pytest.mark.asyncio
async def test_deploy_without_a_renderer_says_so_and_attaches_nothing(monkeypatch):
    _patch(monkeypatch, {"status": "ok", "release": 2, "screens": 1, "app_id": "a1",
                         "scope": "shared",
                         "render": {"status": "unavailable", "reason": "file-tools is down",
                                    "summary": "The rendered check was not available (file-tools is down)",
                                    "images": []}})
    parts = await app_tools._handle_deploy_hook("deploy", {"slug": "board"})
    text = _texts(parts)
    assert "Deployed 'board'" in text and "was not available" in text
    assert _images(parts) == []


@pytest.mark.asyncio
async def test_screenshot_sends_the_source_and_words_it(monkeypatch):
    calls = _patch(monkeypatch, {"status": "ok", "app_id": "a1", "slug": "board",
                                 "source": "live", "render": _render()})
    parts = await app_tools._handle_deploy_hook("screenshot", {"slug": "board", "source": "live"})
    assert calls[0][0] == "/v1/hooks/apps/screenshot"
    assert calls[0][1]["source"] == "live" and calls[0][2] == 240.0
    assert _texts(parts).startswith("'board', the live release: summary for ok")
    assert len(_images(parts)) == 2
    calls.clear()
    await app_tools._handle_deploy_hook("screenshot", {"slug": "board"})
    assert calls[0][1]["source"] == "working"


@pytest.mark.asyncio
async def test_a_release_that_lowers_the_switch_is_named_in_the_replies(monkeypatch):
    # The deploy answer carries the flag with its reason; the tool says what
    # the person's click will do, never that the deploy did it.
    _patch(monkeypatch, {"status": "pending approval", "release": 3, "app_id": "a1",
                         "live_release": 2, "lowers_approval": True,
                         "reason": "turns off approval for every deploy"})
    text = _texts(await app_tools._handle_deploy_hook("deploy", {"slug": "board"}))
    assert "turns approval off for every later deploy" in text
    assert "a person approves it on the card" in text
    _patch(monkeypatch, {"status": "ok", "release": 2, "pending_release": 3, "deploy_state": "pending",
                         "server": "up", "manifest_approved": True, "deploy_requires_approval": True,
                         "lowers_approval": True})
    text = _texts(await app_tools._handle_deploy_hook("status", {"slug": "board"}))
    assert "turns approval off for every later deploy" in text
    # Read as a boolean: only the flag itself is the flag.
    _patch(monkeypatch, {"status": "ok", "release": 2, "pending_release": 3, "deploy_state": "pending",
                         "server": "up", "manifest_approved": True, "lowers_approval": "yes"})
    text = _texts(await app_tools._handle_deploy_hook("status", {"slug": "board"}))
    assert "turns approval off" not in text


@pytest.mark.asyncio
async def test_the_preview_reply_says_it_starts_without_secret_values(monkeypatch):
    _patch(monkeypatch, {"status": "ok", "server": "up", "url": "/apps/a1?preview=1"})
    text = _texts(await app_tools._handle_deploy_hook("preview", {"slug": "board"}))
    assert "no secret values" in text
    assert "403" in text


@pytest.mark.asyncio
async def test_status_keeps_the_short_timeout(monkeypatch):
    calls = _patch(monkeypatch, {"status": "ok", "release": 1, "deploy_state": "live",
                                 "server": "up", "manifest_approved": True})
    await app_tools._handle_deploy_hook("status", {"slug": "board"})
    assert calls[0][2] == 60.0


def test_screenshot_app_is_a_tool_with_a_handler():
    names = {t.name for t in app_tools.APP_TOOLS}
    assert "screenshot_app" in names and "screenshot_app" in app_tools.HANDLERS


@pytest.mark.asyncio
async def test_a_tunnel_refusal_names_the_satellite_not_a_missing_renderer(monkeypatch):
    """A remote session's satellite carries its own hook allowlist; an older
    one answers 403 path-not-allowlisted, which three agents read as "no
    renderer on this install" and skipped the pictures. The tool says what
    it is and where the pictures still come from."""
    async def fake(path, payload, *, read=60.0):
        return None, 'HTTP 403 — {"error": "path-not-allowlisted"}'
    monkeypatch.setattr(app_tools, "_post_hook", fake)
    parts = await app_tools._handle_deploy_hook("screenshot", {"slug": "board"})
    text = _texts(parts)
    assert "satellite on this machine is older" in text
    assert "deploy_app and check_app still render" in text
    assert "not available on this install" not in text
    # Any other transport error keeps its own words.
    async def other(path, payload, *, read=60.0):
        return None, "HTTP 500 — boom"
    monkeypatch.setattr(app_tools, "_post_hook", other)
    assert "HTTP 500 — boom" in _texts(await app_tools._handle_deploy_hook("screenshot", {"slug": "board"}))


def test_the_skills_the_always_loaded_skill_names_are_shipped():
    """display-tools rides every prompt and sends the agent to a skill by
    id; an id the manifest no longer ships fails the Skill call (the
    miniapp-authoring skill was renamed app-authoring)."""
    import json
    import re
    manifest = json.loads((MCP_DIR / "manifest.json").read_text())
    shipped = {s["id"] for s in manifest["skills"]}
    text = (MCP_DIR / "skills" / "display-tools.md").read_text()
    named = set(re.findall(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?:\*\*)? skill\b", text))
    assert named and named <= shipped, named - shipped


@pytest.mark.asyncio
async def test_list_apps_prints_placed_apps_apart_and_a_placed_only_list(monkeypatch):
    """An app a share placed in this agent rides ``placed`` (SHARING.md
    "Agents use a placed app"): named by its home agent and slug with its
    id, how it got here, the role this session acts at and the calls it
    answers; an agent holding only placed apps still reads them."""
    placed = [{"id": "p-1", "agent": "head", "slug": "register", "title": "Register",
               "from_agent_name": "Head", "kind": "folder",
               "placement": {"kind": "agent", "share_id": "sh-1", "role_cap": "editor"},
               "role": "editor", "actions_approved": True, "hidden_for_me": False,
               "exports": {"methods": {"status": {"description": "s"},
                                       "update-project": {"description": "w", "min_role": "editor"}}}},
              {"id": "p-2", "agent": "head", "slug": "notes", "title": "Notes",
               "from_agent_name": "Head", "kind": "file",
               "placement": {"kind": "person", "share_id": "sh-2", "role_cap": "viewer"},
               "role": "agent", "actions_approved": False, "hidden_for_me": True,
               "exports": {"methods": {}}}]
    _patch(monkeypatch, {"apps": [], "placed": placed})
    text = _texts(await app_tools._handle_app_hook("list", {}))
    assert "No pinned apps of this agent's own." in text
    assert "- register of agent head [placed by an agent share at editor, as editor, server app]" in text
    assert "id p-1; calls: status, update-project (editor and up) → POST $PROXY_URL/v1/apps/p-1/api/<method>" in text
    assert "- notes of agent head [placed by your own accepted share at viewer, with no person" in text
    assert "exports no method" in text and "waits for approval on its home agent" in text
    assert "hidden by the user in this agent" in text
    _patch(monkeypatch, {"apps": [{"id": "a", "slug": "mine", "title": "Mine", "scope": "shared",
                                   "path": "/workspace/apps/mine.html", "actions": [],
                                   "actions_approved": True}], "placed": []})
    text = _texts(await app_tools._handle_app_hook("list", {}))
    assert text.startswith("Pinned apps:") and "Placed here" not in text
    _patch(monkeypatch, {"apps": [], "placed": []})
    assert _texts(await app_tools._handle_app_hook("list", {})) == "No pinned apps in your scope."


@pytest.mark.asyncio
async def test_describe_app_prints_the_placement_and_binds_only_over_an_edge(monkeypatch):
    base = {"app_id": "p-1", "agent": "head", "slug": "register", "title": "Register", "approved": True,
            "exports": {"methods": {"status": {"description": "s"},
                                    "update-project": {"description": "w", "min_role": "editor"}},
                        "snapshots": {}, "events": {}}}
    call = "POST $PROXY_URL/v1/apps/p-1/api/<method> with your session token"
    _patch(monkeypatch, {**base, "binding": None, "call": call,
                         "placement": {"kind": "agent", "share_id": "sh", "role_cap": "editor", "role": "viewer"}})
    text = _texts(await app_tools._handle_describe({"agent": "head", "slug": "register"}))
    assert "Bind with" not in text and "No binding reaches it" in text
    assert "Placed in this agent by an agent share at editor: this session calls its exported methods as viewer" in text
    assert call in text and "[editor and up, judged at your role here as the share gives it]" in text
    assert "bindings/<name>" not in text
    assert "delegation edge" not in text and "found through the placement alone" in text
    _patch(monkeypatch, {**base, "binding": {"agent": "head", "app": "register"}})
    text = _texts(await app_tools._handle_describe({"agent": "head", "slug": "register"}))
    assert "Bind with" in text and "Placed in this agent" not in text and "[editor and up, on head]" in text


@pytest.mark.asyncio
async def test_open_app_sends_the_home_agent_of_a_placed_app(monkeypatch):
    calls = _patch(monkeypatch, {"status": "opened", "app_id": "p-1", "screens": 1})
    await app_tools._handle_live_hook("open", {"slug": "register", "agent": " Head "})
    assert calls[-1][0] == "/v1/hooks/apps/open" and calls[-1][1]["agent"] == "head"
    await app_tools._handle_live_hook("open", {"slug": "register"})
    assert "agent" not in calls[-1][1]
    schema = next(t for t in app_tools.APP_TOOLS if t.name == "open_app").inputSchema
    assert "agent" in schema["properties"]
