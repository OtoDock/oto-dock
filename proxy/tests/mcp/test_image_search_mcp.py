"""Unit tests for image-search-mcp server helpers.

The MCP code lives outside the proxy's import path; ``load_mcp_server``
imports it by file location so we can exercise the helpers directly — pure
functions (path resolution, SerpAPI response normalization, cost-rule
matching) don't need an MCP runtime.
"""

from __future__ import annotations

import pytest

from tests._paths import CUSTOM_MCPS, load_mcp_server

MCP_DIR = CUSTOM_MCPS / "image-search-mcp"


# ───────────────────────── _resolve_image_path ──────────────────────────────


def _import_server_with_env(monkeypatch, **env):
    """Re-import the server module after setting env vars (module-level
    constants snapshot at import time)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_mcp_server(MCP_DIR)


def test_resolve_image_path_relative(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    abs_path, err = s._resolve_image_path("uploads/photos/cat.jpg")
    assert err is None
    assert abs_path == "/users/alice/workspace/uploads/photos/cat.jpg"


def test_resolve_image_path_absolute_under_users(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    abs_path, err = s._resolve_image_path("/users/alice/workspace/foo.png")
    assert err is None
    assert abs_path == "/users/alice/workspace/foo.png"


def test_resolve_image_path_absolute_under_workspace(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    abs_path, err = s._resolve_image_path("/workspace/uploads/x.jpg")
    assert err is None
    assert abs_path == "/workspace/uploads/x.jpg"


def test_resolve_image_path_passes_through_absolute(monkeypatch):
    # Absolute paths come pre-translated from the framework's
    # stdio interceptor (or are bwrap-mapped on local). The MCP just
    # normalizes + parent-traversal-guards. Out-of-tree admission is
    # the framework's job, not the MCP's.
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    abs_path, err = s._resolve_image_path("/etc/hosts")
    assert err is None
    assert abs_path == "/etc/hosts"


def test_resolve_image_path_rejects_parent_traversal_relative(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    _, err = s._resolve_image_path("../../../etc/passwd")
    assert err is not None
    assert "parent-traversal" in err


def test_resolve_image_path_parent_traversal_absolute_normalized(monkeypatch):
    # Absolute parent-traversal collapses via normpath; the
    # resulting normalized path is returned (the framework's policy
    # rejects it on user-paired allow_full_fs=false; on full-FS it's
    # admitted as a genuine satellite-host path).
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    abs_path, err = s._resolve_image_path(
        "/users/alice/workspace/../../alice/workspace/foo.jpg",
    )
    # normpath collapses to /users/alice/workspace/foo.jpg
    assert err is None
    assert abs_path == "/users/alice/workspace/foo.jpg"


def test_resolve_image_path_relative_requires_env(monkeypatch):
    # Empty IMAGE_WORKSPACE only blocks RELATIVE paths (where
    # we need an anchor); absolute paths still pass through.
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="")
    _, err = s._resolve_image_path("foo.jpg")
    assert err is not None
    assert "IMAGE_WORKSPACE" in err


def test_resolve_image_path_requires_value(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    _, err = s._resolve_image_path("")
    assert err is not None
    assert "required" in err


# ───────────────────────── _resolve_save_path ──────────────────────────────


def test_resolve_save_path_empty_autoname(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    path, err = s._resolve_save_path("", "jpg")
    assert err is None
    assert path.startswith("/users/alice/workspace/images/saved_")
    assert path.endswith(".jpg")


def test_resolve_save_path_relative_joined(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    path, err = s._resolve_save_path("photos/cat.png", "png")
    assert err is None
    assert path == "/users/alice/workspace/photos/cat.png"


def test_resolve_save_path_absolute_inside_workspace(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    path, err = s._resolve_save_path("/users/alice/workspace/folder/file.jpg", "jpg")
    assert err is None
    assert path == "/users/alice/workspace/folder/file.jpg"


def test_resolve_save_path_absolute_outside_reanchored(monkeypatch):
    """Out-of-scope absolute paths get re-anchored under the default subdir
    using the basename — accidental writes to /etc become safe writes
    under workspace/images/."""
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    path, err = s._resolve_save_path("/etc/passwd", "jpg")
    assert err is None
    assert path.startswith("/users/alice/workspace/images/")
    assert path.endswith("passwd")


def test_resolve_save_path_rejects_relative_parent_traversal(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/users/alice/workspace")
    _, err = s._resolve_save_path("../../../etc/passwd", "jpg")
    assert err is not None
    assert "parent-traversal" in err


# ───────────────────────── _normalize_serpapi_lens ──────────────────────────


def test_normalize_lens_products_mode_extracts_price(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    data = {
        "shopping_results": [
            {"title": "Cozy sweater",
             "link": "https://hm.com/p/1",
             "source": "H&M",
             "thumbnail": "https://hm.com/t/1.jpg",
             "price": {"value": "$45.00", "extracted_value": 45.00, "currency": "USD"}},
        ]
    }
    matches = s._normalize_serpapi_lens(data, mode="products", count=10)
    assert len(matches) == 1
    m = matches[0]
    assert m["title"] == "Cozy sweater"
    assert m["link"] == "https://hm.com/p/1"
    assert m["source_name"] == "H&M"
    assert m["thumbnail_url"] == "https://hm.com/t/1.jpg"
    assert m["price"] == "$45.00"
    assert m["currency"] == "USD"


def test_normalize_lens_visual_matches_no_price(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    data = {
        "visual_matches": [
            {"title": "Similar image",
             "link": "https://blog.example.com/post",
             "source": "blog.example.com",
             "thumbnail": "https://blog.example.com/t.jpg"},
        ]
    }
    matches = s._normalize_serpapi_lens(data, mode="visual_matches", count=10)
    assert len(matches) == 1
    assert matches[0]["price"] == ""
    assert matches[0]["currency"] == ""


def test_normalize_lens_all_mode_unions_arrays_and_dedupes(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    data = {
        "shopping_results": [
            {"title": "A", "link": "https://x.com/1", "source": "X"},
        ],
        "visual_matches": [
            {"title": "B", "link": "https://y.com/2", "source": "Y"},
            {"title": "A-dupe", "link": "https://x.com/1", "source": "X"},  # dup link
        ],
    }
    matches = s._normalize_serpapi_lens(data, mode="all", count=10)
    links = [m["link"] for m in matches]
    assert links.count("https://x.com/1") == 1  # deduped


def test_normalize_lens_respects_count_cap(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    data = {
        "visual_matches": [
            {"title": f"m{i}", "link": f"https://e.com/{i}", "source": "e"}
            for i in range(20)
        ]
    }
    matches = s._normalize_serpapi_lens(data, mode="all", count=5)
    assert len(matches) == 5


# ───────────────────────── login-redirect heuristic ─────────────────────────


def test_looks_like_login_redirect_positive_cases(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    assert s._looks_like_login_redirect("https://authentik.example.com/if/flow/x")
    assert s._looks_like_login_redirect("https://auth.example.com/login?next=/img")
    assert s._looks_like_login_redirect("https://example.com/oauth2/start")
    assert s._looks_like_login_redirect("https://example.com/sso/saml")


def test_looks_like_login_redirect_negative_cases(monkeypatch):
    s = _import_server_with_env(monkeypatch, IMAGE_WORKSPACE="/workspace")
    assert not s._looks_like_login_redirect("https://cdn.example.com/photo.jpg")
    assert not s._looks_like_login_redirect("")
    assert not s._looks_like_login_redirect("https://example.com/api/data")


# ───────────────────────── graceful no-config ───────────────────────────────


@pytest.mark.asyncio
async def test_find_images_no_keys_returns_clear_error(monkeypatch):
    """With zero provider keys, find_images returns an admin-actionable error."""
    s = _import_server_with_env(
        monkeypatch,
        IMAGE_WORKSPACE="/workspace",
        UNSPLASH_ACCESS_KEY="",
        PEXELS_API_KEY="",
        SERPAPI_KEY="",
    )
    result = await s._tool_find_images({"query": "cats", "prefer_provider": "all"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_find_images_google_prefer_needs_serpapi_key(monkeypatch):
    """prefer_provider='google' now requires SERPAPI_KEY (the Google Custom
    Search route was retired by Google's deprecation of 'search entire web').
    """
    s = _import_server_with_env(
        monkeypatch,
        IMAGE_WORKSPACE="/workspace",
        UNSPLASH_ACCESS_KEY="dummy",  # stock configured, but user wants google
        PEXELS_API_KEY="",
        SERPAPI_KEY="",
    )
    result = await s._tool_find_images({"query": "cats", "prefer_provider": "google"})
    assert "error" in result
    assert "SERPAPI_KEY" in result["error"]


@pytest.mark.asyncio
async def test_search_by_image_no_serpapi_key_returns_clear_error(monkeypatch):
    s = _import_server_with_env(
        monkeypatch,
        IMAGE_WORKSPACE="/workspace",
        SERPAPI_KEY="",
    )
    result = await s._tool_search_by_image({"image_path": "/workspace/foo.jpg"})
    assert "error" in result
    assert "SERPAPI_KEY" in result["error"]
