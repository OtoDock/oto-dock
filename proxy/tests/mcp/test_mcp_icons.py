"""MCP icons and provenance.

- the manifest's ``author`` / ``author_url`` fields;
- the icon resolver (``services/community/community_icons.py``): the
  installed file, the catalog fetch with its cache, the "no icon" answers,
  the registry-unavailable mark, the air-gap rule;
- ``GET /v1/mcps/{name}/icon.png``: who may call it and what it returns;
- the docker branch of the manifest-hash update signal;
- every bundled ``mcps/custom/*/icon.png`` is a clean 256×256 PNG.
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config  # noqa: E402
from api.mcp import icons as icons_api  # noqa: E402
from auth.providers import UserContext, get_current_user  # noqa: E402
from services.community import community_catalog, community_icons  # noqa: E402
from services.mcp import mcp_manifest_parse  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png(width: int = 256, height: int = 256) -> bytes:
    """A valid PNG of the given size (one grey pixel row repeated)."""
    def chunk(ctype: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


PNG = _png()
REGISTRY = {"mcps": [
    {"name": "github-mcp", "icon_url": "./github-mcp/icon.png"},
    {"name": "home-assistant", "icon_url": "./ha-mcp/icon.png"},
    {"name": "email-server", "icon_url": None},
]}


class _FakeClient:
    """Stands in for httpx.AsyncClient: records GETs, answers from a script."""

    calls: list[tuple[str, dict]] = []
    script: list[SimpleNamespace] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        _FakeClient.calls.append((url, dict(headers or {})))
        if not _FakeClient.script:
            raise AssertionError("unexpected icon fetch")
        answer = _FakeClient.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _response(status: int, content: bytes = b"", etag: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, content=content, headers={"etag": etag} if etag else {})


@pytest.fixture
def resolver(monkeypatch):
    community_icons.clear_cache()
    _FakeClient.calls = []
    _FakeClient.script = []
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", False)
    monkeypatch.setattr(community_icons.httpx, "AsyncClient", _FakeClient)
    registry = AsyncMock(return_value=REGISTRY)
    monkeypatch.setattr(community_catalog, "fetch_registry", registry)
    yield registry
    community_icons.clear_cache()


def _icon(name: str) -> bytes | None:
    return asyncio.run(community_icons.catalog_icon(name))


# ---------------------------------------------------------------------------
# Manifest fields
# ---------------------------------------------------------------------------

def _write_manifest(tmp_path: Path, **extra) -> Path:
    data = {
        "name": "x-mcp", "label": "X", "description": "d", "version": "1.0.0", "category": "community",
        "server": {"runtime": "python", "transport": "stdio", "command": "venv/bin/python", "args": ["server.py"]},
        **extra,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_manifest_author_fields_parse(tmp_path):
    m = mcp_manifest_parse._parse_manifest(
        _write_manifest(tmp_path, author="GitHub", author_url="https://github.com/github/github-mcp-server"))
    assert (m.author, m.author_url) == ("GitHub", "https://github.com/github/github-mcp-server")


def test_manifest_author_fields_default_empty(tmp_path):
    m = mcp_manifest_parse._parse_manifest(_write_manifest(tmp_path))
    assert (m.author, m.author_url) == ("", "")
    # A bad value never blocks the install: it is kept as text for the dashboard to guard.
    m = mcp_manifest_parse._parse_manifest(_write_manifest(tmp_path, author=5, author_url=None))
    assert (m.author, m.author_url) == ("5", "")


# ---------------------------------------------------------------------------
# Resolver building blocks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("icon_url,folder", [
    ("./github-mcp/icon.png", "github-mcp"),
    ("./ha-mcp/icon.png", "ha-mcp"),
    ("./Workspace_MCP/icon.png", "Workspace_MCP"),
    ("../x/icon.png", None),
    ("./a/b/icon.png", None),
    ("./a/logo.png", None),
    ("https://example.com/a/icon.png", None),
    ("", None),
    (None, None),
    (7, None),
])
def test_catalog_folder_rules(icon_url, folder):
    assert community_icons.catalog_folder(icon_url) == folder


def test_png_problem():
    assert community_icons.png_problem(PNG) is None
    assert "not a PNG" in community_icons.png_problem(b"GIF89a" + b"\x00" * 40)
    assert "128x128" in community_icons.png_problem(_png(128, 128))
    assert "bytes" in community_icons.png_problem(b"\x89PNG\r\n\x1a\n" + b"\x00" * (256 * 1024 + 1))


def test_valid_name_follows_the_installer_rule():
    assert community_icons.valid_name("github-mcp")
    assert community_icons.valid_name("Workspace_MCP")
    assert not community_icons.valid_name("../etc")
    assert not community_icons.valid_name(".hidden")
    assert not community_icons.valid_name("a/b")
    assert not community_icons.valid_name("")


def test_installed_icon_path(tmp_path, monkeypatch):
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: SimpleNamespace(mcp_dir=tmp_path) if name == "x" else None)
    assert community_icons.installed_icon_path("x") is None
    (tmp_path / "icon.png").write_bytes(PNG)
    assert community_icons.installed_icon_path("x") == tmp_path / "icon.png"
    assert community_icons.installed_icon_path("other") is None


# ---------------------------------------------------------------------------
# Catalog fetch and cache
# ---------------------------------------------------------------------------

def test_fetches_once_then_serves_the_cache(resolver):
    _FakeClient.script = [_response(200, PNG, etag='"abc"')]
    assert _icon("github-mcp") == PNG
    assert _icon("github-mcp") == PNG
    assert len(_FakeClient.calls) == 1
    url, headers = _FakeClient.calls[0]
    assert url == f"{community_catalog.MCP_RAW_BASE}/github-mcp/icon.png"
    assert "If-None-Match" not in headers
    assert resolver.await_count == 1


def test_folder_comes_from_icon_url_not_the_name(resolver):
    _FakeClient.script = [_response(200, PNG)]
    assert _icon("home-assistant") == PNG
    assert _FakeClient.calls[0][0].endswith("/ha-mcp/icon.png")


def test_stale_entry_revalidates_with_etag(resolver):
    _FakeClient.script = [_response(200, PNG, etag='"v1"'), _response(304)]
    assert _icon("github-mcp") == PNG
    entry = community_icons._cache["github-mcp"]
    entry.fetched_at -= community_icons.ICON_CACHE_TTL_SECONDS + 1
    assert not entry.fresh(time.monotonic())
    assert _icon("github-mcp") == PNG
    assert _FakeClient.calls[1][1] == {"If-None-Match": '"v1"'}
    # The 304 refreshed the entry in place: fresh again, same body, same ETag.
    assert entry.fresh(time.monotonic()) and entry.etag == '"v1"'


def test_no_icon_answers_are_cached(resolver):
    # A registry entry without icon_url, a name the registry does not know,
    # an upstream 404 and an invalid body all cache "no icon" without a retry.
    assert _icon("email-server") is None
    assert _icon("unknown-mcp") is None
    assert _FakeClient.calls == []
    _FakeClient.script = [_response(404)]
    assert _icon("github-mcp") is None
    assert _icon("github-mcp") is None
    assert len(_FakeClient.calls) == 1
    community_icons.clear_cache()
    _FakeClient.script = [_response(200, b"<html>not an image</html>")]
    assert _icon("github-mcp") is None
    assert _icon("github-mcp") is None
    assert len(_FakeClient.calls) == 2
    # One registry read per miss (email-server, unknown-mcp, github-mcp twice
    # across the cache clear); every cached answer skipped the registry.
    assert resolver.await_count == 4


def test_network_error_keeps_the_stale_body_for_a_while(resolver):
    _FakeClient.script = [_response(200, PNG, etag='"v1"')]
    assert _icon("github-mcp") == PNG
    community_icons._cache["github-mcp"].fetched_at -= community_icons.ICON_CACHE_TTL_SECONDS + 1
    _FakeClient.script = [community_icons.httpx.ConnectError("down")]
    assert _icon("github-mcp") == PNG
    assert _icon("github-mcp") == PNG          # no second attempt inside the retry window
    assert len(_FakeClient.calls) == 2


def test_server_error_without_a_body_is_a_short_no(resolver):
    _FakeClient.script = [_response(503)]
    assert _icon("github-mcp") is None
    assert _icon("github-mcp") is None
    assert len(_FakeClient.calls) == 1


def test_registry_failure_marks_the_registry_unavailable(resolver):
    resolver.side_effect = RuntimeError("offline")
    assert _icon("github-mcp") is None
    assert _icon("home-assistant") is None
    assert resolver.await_count == 1           # the second miss never touched the registry
    assert _FakeClient.calls == []


def test_air_gapped_never_goes_outbound(resolver, monkeypatch):
    monkeypatch.setattr(config, "OTODOCK_AIR_GAPPED", True)
    assert _icon("github-mcp") is None
    assert resolver.await_count == 0
    assert _FakeClient.calls == []


def test_concurrent_requests_fetch_once(resolver):
    _FakeClient.script = [_response(200, PNG)]

    async def many():
        return await asyncio.gather(*(community_icons.catalog_icon("github-mcp") for _ in range(3)))

    assert asyncio.run(many()) == [PNG, PNG, PNG]
    assert len(_FakeClient.calls) == 1


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def _user(role: str = "admin", **kw) -> UserContext:
    return UserContext(sub=f"u-{role}", email=f"{role}@x.test", name=role, role=role, **kw)


def _client(user: UserContext | None) -> TestClient:
    app = FastAPI()
    app.include_router(icons_api.router)

    async def _stub():
        return user
    app.dependency_overrides[get_current_user] = _stub
    return TestClient(app)


@pytest.fixture
def no_installed(monkeypatch):
    monkeypatch.setattr(community_icons, "installed_icon_path", lambda name: None)


def test_route_requires_a_dashboard_session(no_installed):
    assert _client(None).get("/v1/mcps/github-mcp/icon.png").status_code == 401
    assert _client(_user(is_api_key=True)).get("/v1/mcps/github-mcp/icon.png").status_code == 403


def test_route_rejects_a_bad_name(no_installed):
    r = _client(_user()).get("/v1/mcps/..%2Fetc/icon.png")
    assert r.status_code == 404
    r = _client(_user()).get("/v1/mcps/.hidden/icon.png")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "private, max-age=300"


def test_route_serves_the_installed_file(tmp_path, monkeypatch):
    (tmp_path / "icon.png").write_bytes(PNG)
    monkeypatch.setattr(community_icons, "installed_icon_path", lambda name: tmp_path / "icon.png")
    catalog = AsyncMock(return_value=None)
    monkeypatch.setattr(community_icons, "catalog_icon", catalog)
    r = _client(_user("member", agent_roles={"a": "manager"})).get("/v1/mcps/memory-mcp/icon.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.content == PNG
    catalog.assert_not_awaited()


def test_route_refuses_a_plain_member(tmp_path, monkeypatch):
    # No surface shows a member an MCP row; the answer would only tell them
    # which MCPs are installed.
    (tmp_path / "icon.png").write_bytes(PNG)
    monkeypatch.setattr(community_icons, "installed_icon_path", lambda name: tmp_path / "icon.png")
    r = _client(_user("member", agent_roles={"a": "editor"})).get("/v1/mcps/memory-mcp/icon.png")
    assert r.status_code == 403


def test_installed_icon_is_checked_like_the_catalogs(tmp_path, monkeypatch):
    # The folder copy arrives unvalidated with the tarball and every row
    # loads it: not a 256x256 PNG or over the size cap = no icon.
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "get_manifest",
                        lambda name: SimpleNamespace(mcp_dir=str(tmp_path)))
    icon = tmp_path / "icon.png"
    icon.write_bytes(PNG)
    assert community_icons.installed_icon_path("x-mcp") == icon
    icon.write_bytes(b"<svg onload=alert(1)>")
    assert community_icons.installed_icon_path("x-mcp") is None
    icon.write_bytes(_png(64, 64))
    assert community_icons.installed_icon_path("x-mcp") is None
    icon.write_bytes(PNG + b"\0" * community_icons.ICON_MAX_BYTES)
    assert community_icons.installed_icon_path("x-mcp") is None
    icon.unlink()
    assert community_icons.installed_icon_path("x-mcp") is None


def test_route_serves_the_catalog_icon_to_creators_and_admins(no_installed, monkeypatch):
    catalog = AsyncMock(return_value=PNG)
    monkeypatch.setattr(community_icons, "catalog_icon", catalog)
    for role in ("admin", "creator"):
        r = _client(_user(role)).get("/v1/mcps/github-mcp/icon.png")
        assert r.status_code == 200 and r.content == PNG
        etag = r.headers["etag"]
        r = _client(_user(role)).get("/v1/mcps/github-mcp/icon.png", headers={"If-None-Match": etag})
        assert r.status_code == 304
        assert r.headers["etag"] == etag
    assert catalog.await_count == 4


def test_route_never_fetches_the_catalog_for_a_manager(no_installed, monkeypatch):
    catalog = AsyncMock(return_value=PNG)
    monkeypatch.setattr(community_icons, "catalog_icon", catalog)
    r = _client(_user("member", agent_roles={"a": "manager"})).get("/v1/mcps/github-mcp/icon.png")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "private, max-age=300"
    catalog.assert_not_awaited()


def test_route_404s_when_the_catalog_has_no_icon(no_installed, monkeypatch):
    monkeypatch.setattr(community_icons, "catalog_icon", AsyncMock(return_value=None))
    r = _client(_user()).get("/v1/mcps/email-server/icon.png")
    assert r.status_code == 404
    # A miss is short-lived: the MCP may get installed in the meantime.
    assert r.headers["cache-control"] == "private, max-age=300"


# ---------------------------------------------------------------------------
# Docker entries join the manifest-hash update signal
# ---------------------------------------------------------------------------

def test_hash_signal_applies_only_to_convergeable_sources():
    applies = community_catalog.manifest_hash_signal_applies
    assert applies({"runtime": "node", "source": "npm:x"})
    assert applies({"runtime": "python", "source": "pypi:x"})
    assert applies({"runtime": "docker", "source": "docker:camoufox + @playwright/mcp@0.0.68"})
    assert applies({"runtime": "none"})
    assert applies({"runtime": "node", "version": ""})          # an older registry without `source`
    assert not applies({"runtime": "python", "source": "git+https://example.com/x.git@v1"})
    assert not applies({"runtime": "remote", "source": "remote:mcp.linear.app"})
    assert not applies({"runtime": "python", "source": "remote:api.postiz.com"})


def test_augment_flags_a_docker_manifest_change_under_the_same_tag():
    entry = {"name": "camoufox", "runtime": "docker", "version": "0.0.74",
             "source": "docker:camoufox", "manifest_hash": "aaaa"}
    out = community_catalog.augment_entry(
        entry, installed_versions={"camoufox": "0.0.74"}, enabled_for_agents={},
        installed_manifest_hashes={"camoufox": "bbbb"})
    assert out["update_available"] is True
    same = community_catalog.augment_entry(
        entry, installed_versions={"camoufox": "0.0.74"}, enabled_for_agents={},
        installed_manifest_hashes={"camoufox": "aaaa"})
    assert same["update_available"] is False
    git_entry = {"name": "telegram-mcp", "runtime": "python", "version": "3.2.21",
                 "source": "git+https://github.com/chigwell/telegram-mcp.git@v3.2.21", "manifest_hash": "aaaa"}
    assert community_catalog.augment_entry(
        git_entry, installed_versions={"telegram-mcp": "3.2.21"}, enabled_for_agents={},
        installed_manifest_hashes={"telegram-mcp": "bbbb"})["update_available"] is False


def test_augment_never_offers_a_docker_install_ahead_of_the_catalog_as_an_update():
    # The image tag is part of the hash, so an install ahead of the catalog
    # always mismatches; converging it would be a downgrade.
    entry = {"name": "video-tools", "runtime": "docker", "version": "0.4.3",
             "source": "docker:video-tools", "manifest_hash": "aaaa"}
    out = community_catalog.augment_entry(
        entry, installed_versions={"video-tools": "0.4.4"}, enabled_for_agents={},
        installed_manifest_hashes={"video-tools": "bbbb"})
    assert out["update_available"] is False
    # A node entry has no version in the catalog: the hash alone decides.
    node = {"name": "n", "runtime": "node", "version": "", "source": "npm:n", "manifest_hash": "aaaa"}
    assert community_catalog.augment_entry(
        node, installed_versions={"n": "1.2.3"}, enabled_for_agents={},
        installed_manifest_hashes={"n": "bbbb"})["update_available"] is True


def test_detect_available_updates_reports_a_docker_manifest_change(tmp_path, monkeypatch):
    from services.mcp import mcp_registry, mcp_updater

    installed = {"name": "camoufox", "version": "0.0.74", "category": "community",
                 "server": {"runtime": "docker", "image": "ghcr.io/otodock/camoufox:0.0.74",
                            "source": "docker:camoufox"}}
    (tmp_path / "manifest.json").write_text(json.dumps(installed), encoding="utf-8")
    manifest = SimpleNamespace(
        name="camoufox", category="community", version="0.0.74", mcp_dir=tmp_path,
        server=SimpleNamespace(runtime="docker", source="docker:camoufox", version_constraint=""),
    )
    monkeypatch.setattr(mcp_registry, "get_all_manifests", lambda: {"camoufox": manifest})
    changed = dict(installed, author="OtoDock")
    catalog = {"mcps": [{"name": "camoufox", "version": "0.0.74", "runtime": "docker",
                         "manifest_hash": community_catalog.normalized_manifest_hash(changed)}]}
    monkeypatch.setattr(community_catalog, "fetch_registry", AsyncMock(return_value=catalog))
    monkeypatch.setattr(community_catalog, "fetch_skills_registry", AsyncMock(return_value={"skills": []}))

    out = asyncio.run(mcp_updater.detect_available_updates())
    assert out["updates"]["camoufox"] == {
        "current": "0.0.74", "latest": "0.0.74", "registry": "catalog",
        "package": "camoufox", "reason": "manifest",
    }

    # Same manifest on both sides: nothing to converge.
    catalog["mcps"][0]["manifest_hash"] = community_catalog.normalized_manifest_hash(installed)
    out = asyncio.run(mcp_updater.detect_available_updates())
    assert "camoufox" not in out["updates"]


# ---------------------------------------------------------------------------
# The bundled icon files
# ---------------------------------------------------------------------------

def test_every_bundled_mcp_ships_a_clean_icon():
    custom = PROXY_DIR.parent / "mcps" / "custom"
    folders = sorted(p.parent for p in custom.glob("*/manifest.json"))
    assert folders, custom
    for folder in folders:
        icon = folder / "icon.png"
        assert icon.is_file(), f"{folder.name} has no icon.png"
        data = icon.read_bytes()
        assert community_icons.png_problem(data) is None, folder.name
        pos, types = 8, []
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            types.append(data[pos + 4:pos + 8])
            pos += 12 + length
        # No text or time chunk: the export's leak scan skips binaries, so a
        # PNG must never carry a generator path.
        assert not {b"tEXt", b"iTXt", b"zTXt", b"tIME"} & set(types), folder.name
