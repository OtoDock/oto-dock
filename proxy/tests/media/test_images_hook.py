"""Tests for the /v1/hooks/images endpoint + /v1/images/temp/{token}
endpoint pair that backs image-search-mcp's reverse-image-search flow.

Hook validation covers the exactly-one-of-(url|image_data) per item
contract; the temp-URL pair covers create + serve + TTL + scope reject.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from core.session import session_state


client = TestClient(app)


# ───────────────────────── /v1/hooks/images ──────────────────────────────────


def _post_hook_images(payload: dict, session_id: str = "sess-1") -> object:
    """POST with the master API key. verify_session_match accepts master+match.

    The endpoint takes the session_id from the body, not the header. We mock
    verify_session_match to a no-op so the call doesn't need a real session.
    """
    with patch("api.hooks.hooks.verify_session_match"):
        return client.post(
            "/v1/hooks/images",
            json=payload,
            headers={"Authorization": "Bearer dummy"},
        )


def test_hook_images_accepts_url_only_item():
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [
            {"url": "https://cdn.example.com/photo.jpg", "caption": "test"},
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_hook_images_accepts_image_data_only_item():
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [
            {"image_data": "AAAA", "mime_type": "image/png", "caption": "test"},
        ],
    })
    assert resp.status_code == 200


def test_hook_images_rejects_item_with_both_url_and_image_data():
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [
            {"url": "https://x", "image_data": "AAAA", "mime_type": "image/png"},
        ],
    })
    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]


def test_hook_images_rejects_item_with_neither_url_nor_image_data():
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [{"caption": "nope"}],
    })
    assert resp.status_code == 400


def test_hook_images_rejects_empty_images_list():
    resp = _post_hook_images({"session_id": "sess-1", "images": []})
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_hook_images_accepts_link_url_and_attribution_fields():
    """Reverse-image-search flow uses link_url + attribution on each card."""
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [
            {"url": "https://cdn.example.com/sweater.jpg",
             "caption": "Cozy wool sweater",
             "attribution": "$45 — H&M",
             "link_url": "https://hm.com/product/123"},
        ],
    })
    assert resp.status_code == 200


def test_hook_images_accepts_multiple_items():
    resp = _post_hook_images({
        "session_id": "sess-1",
        "images": [
            {"url": "https://a"},
            {"url": "https://b"},
            {"url": "https://c"},
            {"url": "https://d"},
        ],
    })
    assert resp.status_code == 200


# ───────────────────────── /v1/images/temp/* ─────────────────────────────────


@pytest.fixture
def tmp_agent_image(tmp_path, monkeypatch):
    """Set up a fake agent_dir with one image file, plus a SecurityContext.

    Returns (abs_path_to_image, agent_name) for the test to use.
    """
    agent = "test-agent"
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / agent / "users" / "alice" / "workspace"
    agent_dir.mkdir(parents=True)
    img_path = agent_dir / "test.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    # path_policy caches AGENTS_DIR at import (_AGENTS_DIR); B3 now routes the
    # temp-image path through _check_read_path, so patch the module constant too
    # (same convention as test_oauth_token_protection / test_satellite_host_paths).
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    # The endpoint reads ctx.agent to compute agent_dir — set up the session.
    ctx = SecurityContext(
        role="manager", username="alice", agent=agent,
        is_admin_agent=False,
    )
    session_state.set_session_security("sess-temp-1", ctx)
    yield str(img_path), agent
    # cleanup
    session_state._session_security.pop("sess-temp-1", None)


def _post_temp_url(abs_path: str, session_id: str = "sess-temp-1", ttl: int = 60):
    with patch("api.media.images.verify_session_match"):
        # DASHBOARD_PUBLIC_URL must be set to mint a URL
        with patch.object(config, "DASHBOARD_PUBLIC_URL", "https://platform.example"):
            return client.post(
                "/v1/images/temp",
                json={"session_id": session_id, "abs_path": abs_path,
                      "ttl_seconds": ttl},
                headers={"Authorization": "Bearer dummy"},
            )


def test_temp_url_happy_path(tmp_agent_image):
    abs_path, _agent = tmp_agent_image
    resp = _post_temp_url(abs_path, ttl=60)
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://platform.example/v1/images/temp/")
    assert body["expires_in"] == 60

    # Extract token and GET the served file
    token = body["url"].rsplit("/", 1)[-1]
    serve = client.get(f"/v1/images/temp/{token}")
    assert serve.status_code == 200
    assert serve.content == b"\xff\xd8\xff\xe0fake-jpeg"


def test_temp_url_rejects_path_outside_agent_dir(tmp_agent_image):
    """Path escape attempt — /etc/passwd should be rejected.

    _classify_and_pull now denies out-of-tree host paths at the gate (returns
    None → 404 here) rather than resolving them and leaning on this endpoint's
    own relative_to(agent_dir) check (403). Either way the path is refused;
    accept both so the earlier, cleaner rejection isn't treated as a regression.
    """
    resp = _post_temp_url("/etc/passwd")
    assert resp.status_code in (403, 404)


def test_temp_url_accepts_sandbox_virtual_path(tmp_agent_image):
    """A sandbox-virtual path (what stdio MCPs running in bwrap naturally
    produce) must translate to the host path under the agent_dir before
    the scope check — otherwise reverse image search 403s every time the
    agent passes its own IMAGE_WORKSPACE-rooted path.
    """
    # The file lives at <agent_dir>/users/alice/workspace/test.jpg in the
    # fixture. The MCP would pass it as the sandbox-virtual form:
    sandbox_virtual = "/users/alice/workspace/test.jpg"
    resp = _post_temp_url(sandbox_virtual, ttl=60)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["url"].rsplit("/", 1)[-1]
    serve = client.get(f"/v1/images/temp/{token}")
    assert serve.status_code == 200
    assert serve.content == b"\xff\xd8\xff\xe0fake-jpeg"


def test_temp_url_rejects_cross_user_file(tmp_agent_image):
    """A session must NOT read ANOTHER user's file via the
    temp-image hook. The local form-2 resolver previously skipped the security
    context, so alice could pass `<agent>/users/bob/...` (under the agent dir,
    so the endpoint's own under-agent-dir check passed) and read bob's file.
    `_classify_and_pull` now re-imposes cross-user RBAC on the resolved path.
    """
    _abs, agent = tmp_agent_image
    bob_ws = config.AGENTS_DIR / agent / "users" / "bob" / "workspace"
    bob_ws.mkdir(parents=True, exist_ok=True)
    (bob_ws / "secret.jpg").write_bytes(b"\xff\xd8secret")
    # alice's session (the fixture) asks for bob's file — both path forms.
    for path in (
        "/users/bob/workspace/secret.jpg",               # sandbox-virtual
        f"{agent}/users/bob/workspace/secret.jpg",       # agents-relative (form 2)
    ):
        resp = _post_temp_url(path)
        assert resp.status_code in (403, 404), f"{path} -> {resp.status_code}"


def test_temp_url_rejects_missing_file(tmp_agent_image):
    _abs_path, _agent = tmp_agent_image
    # A path inside the agent_dir that doesn't exist
    nonexistent = str(Path(_abs_path).parent / "ghost.jpg")
    resp = _post_temp_url(nonexistent)
    assert resp.status_code == 404


def test_temp_url_clamps_ttl_above_max(tmp_agent_image):
    abs_path, _ = tmp_agent_image
    resp = _post_temp_url(abs_path, ttl=9999)
    assert resp.status_code == 200
    assert resp.json()["expires_in"] == 600   # MAX_TTL_SECONDS


def test_temp_url_clamps_ttl_below_min(tmp_agent_image):
    abs_path, _ = tmp_agent_image
    resp = _post_temp_url(abs_path, ttl=1)
    assert resp.status_code == 200
    assert resp.json()["expires_in"] == 30    # MIN_TTL_SECONDS


def test_temp_url_returns_410_after_expiry(tmp_agent_image):
    """A second GET after the TTL passes should 410. We force-expire by
    mutating the registry directly so we don't have to sleep."""
    from api.media import images
    abs_path, _ = tmp_agent_image
    resp = _post_temp_url(abs_path, ttl=60)
    token = resp.json()["url"].rsplit("/", 1)[-1]

    # Force-expire
    path, _exp = images._temp_image_tokens[token]
    images._temp_image_tokens[token] = (path, time.monotonic() - 1)

    serve = client.get(f"/v1/images/temp/{token}")
    assert serve.status_code == 410
    # Token also evicted from the registry
    assert token not in images._temp_image_tokens


def test_temp_url_returns_404_for_unknown_token():
    serve = client.get("/v1/images/temp/totally-fake-token-xyz")
    assert serve.status_code == 404


def test_temp_url_requires_dashboard_public_url(tmp_agent_image):
    """Without DASHBOARD_PUBLIC_URL configured we can't mint a public URL."""
    abs_path, _ = tmp_agent_image
    with patch("api.media.images.verify_session_match"):
        with patch.object(config, "DASHBOARD_PUBLIC_URL", ""):
            resp = client.post(
                "/v1/images/temp",
                json={"session_id": "sess-temp-1", "abs_path": abs_path,
                      "ttl_seconds": 60},
                headers={"Authorization": "Bearer dummy"},
            )
    assert resp.status_code == 500
    assert "DASHBOARD_PUBLIC_URL" in resp.json()["detail"]
