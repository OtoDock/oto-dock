"""Tests for the manifest-driven OAuth scope builder used at OAuth start time.

The builder lives in ``services.mcp.mcp_registry.build_oauth_scopes`` (generalized
to any provider's manifest). An earlier revision had a Google-specific
``auth.google_oauth.build_scopes`` — this test module adapts so the
existing test bodies (focused on the scope-union contract) still apply.

Validates the contract:
  - Reads scope definitions from the named MCP's manifest
    (`credentials.oauth.base_scopes` + `credentials.oauth.services[].scopes`).
  - Returns deduplicated list preserving declaration order (base first,
    then per-requested-service).
  - Unknown service keys are silently dropped (caller validates separately).
  - Missing manifest or oauth block returns empty list (defensive).
"""

import pytest

from services.mcp import mcp_registry
from services.mcp.mcp_registry import (
    CredentialConfig,
    McpManifest,
    ServerConfig,
    build_oauth_scopes as _build,
)


def build_scopes(services):
    """Adapter for compact fixtures: workspace-mcp is the only OAuth MCP,
    so this keeps the old test bodies unchanged."""
    return _build("google-workspace", services)


GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]
CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]
BASE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def _make_manifest(oauth_dict: dict | None) -> McpManifest:
    """Construct a minimal McpManifest with the given oauth block."""
    return McpManifest(
        name="google-workspace",
        label="Google Workspace",
        description="test",
        version="0.0.1",
        category="community",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(oauth=oauth_dict),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
    )


@pytest.fixture
def patched_manifest(monkeypatch):
    """Helper to install a synthetic google-workspace manifest into the registry."""
    def _install(oauth_dict):
        manifest = _make_manifest(oauth_dict)
        monkeypatch.setattr(mcp_registry, "_manifests", {"google-workspace": manifest})
        return manifest
    return _install


# ═══════════════════════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════════════════════


def test_returns_base_only_when_no_services_requested(patched_manifest):
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "gmail", "label": "G", "description": "x", "scopes": GMAIL_SCOPES},
    ]})
    assert build_scopes([]) == BASE


def test_returns_base_plus_one_service(patched_manifest):
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "gmail", "label": "G", "description": "x", "scopes": GMAIL_SCOPES},
    ]})
    result = build_scopes(["gmail"])
    assert result == BASE + GMAIL_SCOPES


def test_returns_union_of_multiple_services(patched_manifest):
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "gmail", "label": "G", "description": "x", "scopes": GMAIL_SCOPES},
        {"key": "drive", "label": "D", "description": "x", "scopes": DRIVE_SCOPES},
        {"key": "calendar", "label": "C", "description": "x", "scopes": CALENDAR_SCOPES},
    ]})
    result = build_scopes(["gmail", "drive", "calendar"])
    # Order: base scopes first, then per-service in request order
    assert result == BASE + GMAIL_SCOPES + DRIVE_SCOPES + CALENDAR_SCOPES


def test_deduplicates_overlapping_scopes(patched_manifest):
    """When two services share a scope (e.g. docs+sheets both need drive.readonly),
    it appears once in the result."""
    drive_ro = "https://www.googleapis.com/auth/drive.readonly"
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "docs", "label": "D", "description": "x",
         "scopes": ["https://www.googleapis.com/auth/documents", drive_ro]},
        {"key": "sheets", "label": "S", "description": "x",
         "scopes": ["https://www.googleapis.com/auth/spreadsheets", drive_ro]},
    ]})
    result = build_scopes(["docs", "sheets"])
    # drive.readonly must appear exactly once
    assert result.count(drive_ro) == 1
    # All other scopes still present
    assert "https://www.googleapis.com/auth/documents" in result
    assert "https://www.googleapis.com/auth/spreadsheets" in result


def test_preserves_request_order_across_services(patched_manifest):
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "gmail", "label": "G", "description": "x", "scopes": GMAIL_SCOPES},
        {"key": "drive", "label": "D", "description": "x", "scopes": DRIVE_SCOPES},
    ]})
    # Request drive first, gmail second
    result = build_scopes(["drive", "gmail"])
    # Base always first; then drive scopes; then gmail scopes
    assert result == BASE + DRIVE_SCOPES + GMAIL_SCOPES


def test_unknown_service_silently_dropped(patched_manifest):
    """API-level validation rejects unknown services before build_scopes is
    called — this just verifies build_scopes is defensive enough that an
    unknown key doesn't crash."""
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "gmail", "label": "G", "description": "x", "scopes": GMAIL_SCOPES},
    ]})
    result = build_scopes(["gmail", "nonexistent"])
    assert result == BASE + GMAIL_SCOPES


# ═══════════════════════════════════════════════════════════════════════════
# Defensive paths — missing manifest / missing oauth block
# ═══════════════════════════════════════════════════════════════════════════


def test_returns_empty_when_manifest_missing(monkeypatch):
    monkeypatch.setattr(mcp_registry, "_manifests", {})
    assert build_scopes(["gmail"]) == []


def test_returns_empty_when_oauth_block_missing(patched_manifest):
    patched_manifest(None)  # CredentialConfig with oauth=None
    assert build_scopes(["gmail"]) == []


def test_returns_empty_when_base_and_services_both_missing(patched_manifest):
    patched_manifest({})  # oauth block exists but no fields
    assert build_scopes(["gmail"]) == []


def test_returns_only_base_when_services_array_omitted(patched_manifest):
    patched_manifest({"base_scopes": BASE})
    # No service entries declared — only base scopes available
    assert build_scopes(["gmail"]) == BASE


def test_handles_service_with_empty_scopes_array(patched_manifest):
    """OAuth-login-only services (no API scopes) should not contribute scopes."""
    patched_manifest({"base_scopes": BASE, "services": [
        {"key": "loginonly", "label": "L", "description": "x", "scopes": []},
    ]})
    assert build_scopes(["loginonly"]) == BASE
