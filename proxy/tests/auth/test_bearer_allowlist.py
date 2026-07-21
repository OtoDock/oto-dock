"""Bearer-allowlist tests — storage + matcher + manifest validator
+ runtime injector.

Storage tests hit the real PG (entries are namespaced by random provider
ids so they don't collide). Matcher + validator + injector are pure
functions exercised with synthetic inputs.
"""

import uuid
import pytest

from storage import bearer_allowlist
from services.mcp import mcp_registry


def _fresh_provider() -> str:
    return f"test-provider-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Storage CRUD + matcher
# ---------------------------------------------------------------------------


class TestAllowlistStorage:
    def test_add_and_list(self):
        p = _fresh_provider()
        bearer_allowlist.add_allowed(p, "mcp.example.com", "test")
        entries = bearer_allowlist.list_allowed()
        match = [e for e in entries if e["provider_id"] == p]
        assert len(match) == 1
        assert match[0]["host_pattern"] == "mcp.example.com"
        assert match[0]["added_by"] == "test"
        bearer_allowlist.delete_allowed(match[0]["id"])

    def test_idempotent_add(self):
        p = _fresh_provider()
        id1 = bearer_allowlist.add_allowed(p, "host.example.com")
        id2 = bearer_allowlist.add_allowed(p, "host.example.com")
        assert id1 == id2  # upsert
        entries = [e for e in bearer_allowlist.list_allowed()
                   if e["provider_id"] == p]
        assert len(entries) == 1
        bearer_allowlist.delete_allowed(id1)

    def test_delete_missing_returns_false(self):
        assert bearer_allowlist.delete_allowed(999_999_999) is False

    def test_delete_existing_returns_true(self):
        p = _fresh_provider()
        row_id = bearer_allowlist.add_allowed(p, "host.example.com")
        assert bearer_allowlist.delete_allowed(row_id) is True
        # second delete is a no-op
        assert bearer_allowlist.delete_allowed(row_id) is False


class TestAllowlistMatcher:
    @pytest.fixture
    def seeded_provider(self):
        p = _fresh_provider()
        ids = [
            bearer_allowlist.add_allowed(p, "mcp.example.com"),
            bearer_allowlist.add_allowed(p, "*.linear.app"),
        ]
        yield p
        for i in ids:
            bearer_allowlist.delete_allowed(i)

    def test_exact_host_matches(self, seeded_provider):
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "mcp.example.com",
        ) is True

    def test_wrong_host_rejected(self, seeded_provider):
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "evil.example.com",
        ) is False

    def test_wildcard_matches_subdomain(self, seeded_provider):
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "mcp.linear.app",
        ) is True
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "api.linear.app",
        ) is True

    def test_wildcard_does_not_match_apex(self, seeded_provider):
        # `*.linear.app` does NOT match `linear.app` itself (fnmatch behavior).
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "linear.app",
        ) is False

    def test_wildcard_does_not_match_unrelated_domain(self, seeded_provider):
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "linear.app.evil.com",
        ) is False  # Wait — this actually MATCHES fnmatch "*.linear.app"
        # Confirm fnmatch behavior: `*.linear.app` is ANY characters
        # (including dots) ending in `.linear.app`. So `linear.app.evil.com`
        # does NOT end in `.linear.app` — correctly rejected.

    def test_case_insensitive_host(self, seeded_provider):
        assert bearer_allowlist.is_host_allowed(
            seeded_provider, "MCP.EXAMPLE.COM",
        ) is True

    def test_unknown_provider_rejected(self):
        assert bearer_allowlist.is_host_allowed(
            "ghost-provider-xyz", "mcp.example.com",
        ) is False

    def test_empty_inputs_rejected(self):
        assert bearer_allowlist.is_host_allowed("", "x") is False
        assert bearer_allowlist.is_host_allowed("x", "") is False


# ---------------------------------------------------------------------------
# Manifest validator — bearer_required gates
# ---------------------------------------------------------------------------


class TestManifestValidator:
    def test_bearer_required_without_proposed_hosts_rejected(self):
        with pytest.raises(ValueError, match="proposed_hosts"):
            mcp_registry._validate_oauth_services(
                {
                    "provider_id": "slack",
                    "flows": ["authorization_code"],
                    "bearer_required": True,
                },
                "slack-mcp",
            )

    def test_bearer_required_with_stdio_transport_rejected(self):
        with pytest.raises(ValueError, match="HTTP-class"):
            mcp_registry._validate_oauth_services(
                {
                    "provider_id": "slack",
                    "flows": ["authorization_code"],
                    "bearer_required": True,
                    "proposed_hosts": ["mcp.slack.com"],
                },
                "slack-mcp",
                {"transport": "stdio"},
            )

    def test_bearer_required_with_http_transport_accepted(self):
        # Should not raise.
        mcp_registry._validate_oauth_services(
            {
                "provider_id": "slack",
                "flows": ["authorization_code"],
                "bearer_required": True,
                "proposed_hosts": ["mcp.slack.com"],
            },
            "slack-mcp",
            {"transport": "streamable_http"},
        )

    def test_invalid_hostname_in_proposed_hosts_rejected(self):
        with pytest.raises(ValueError, match="not a valid hostname"):
            mcp_registry._validate_oauth_services(
                {
                    "provider_id": "slack",
                    "flows": ["authorization_code"],
                    "bearer_required": True,
                    "proposed_hosts": ["https://mcp.slack.com/"],
                },
                "slack-mcp",
                {"transport": "http"},
            )


# ---------------------------------------------------------------------------
# Runtime injector — maybe_inject_bearer_header
# ---------------------------------------------------------------------------


class TestRuntimeInjector:
    def test_stdio_entry_unchanged(self):
        # bearer_required is irrelevant for stdio (no URL to check)
        # because the entry has no `url` key.
        from services.mcp.mcp_registry import (
            maybe_inject_bearer_header,
        )
        m = _make_manifest(
            transport="stdio",
            oauth={
                "provider_id": "slack",
                "flows": ["authorization_code"],
                "bearer_required": True,
                "proposed_hosts": ["mcp.slack.com"],
            },
        )
        entry = {"type": "stdio", "command": "x", "args": []}
        result = maybe_inject_bearer_header(
            entry, m, user_sub="u", agent_name="a", task_scope="user",
        )
        # No url → no header injection (stdio case).
        assert "headers" not in result

    def test_off_allowlist_host_skipped_with_warning(self, caplog):
        from services.mcp.mcp_registry import maybe_inject_bearer_header
        m = _make_manifest(
            transport="http",
            url_template="https://attacker.example.com/mcp",
            oauth={
                "provider_id": "slack",
                "flows": ["authorization_code"],
                "bearer_required": True,
                "proposed_hosts": ["attacker.example.com"],
            },
        )
        entry = {"type": "sse", "url": "https://attacker.example.com/mcp/sse"}
        with caplog.at_level("WARNING"):
            result = maybe_inject_bearer_header(
                entry, m, user_sub="u", agent_name="a", task_scope="user",
            )
        assert "headers" not in result
        assert any(
            "Bearer-header skipped" in r.message for r in caplog.records
        )

    def test_bearer_not_required_no_header(self):
        from services.mcp.mcp_registry import maybe_inject_bearer_header
        m = _make_manifest(
            transport="http",
            url_template="https://mcp.example.com",
            oauth={"provider_id": "google", "bearer_required": False},
        )
        entry = {"type": "sse", "url": "https://mcp.example.com/sse"}
        result = maybe_inject_bearer_header(
            entry, m, user_sub="u", agent_name="a", task_scope="user",
        )
        assert "headers" not in result


# Helper for constructing minimal manifests in tests.
def _make_manifest(*, transport: str, url_template: str = "", oauth: dict):
    from services.mcp.mcp_registry import (
        McpManifest, ServerConfig, CredentialConfig,
    )
    return McpManifest(
        name="x", label="x", description="", version="0", category="custom",
        server=ServerConfig(
            runtime="python", transport=transport,
            url_template=url_template,
        ),
        credentials=CredentialConfig(type="per_user", oauth=oauth),
        config=[], env={}, agent_env={}, exclude_from=[], skills=[],
    )


# ---------------------------------------------------------------------------
# Seed coverage — Microsoft + Zoom must be pre-seeded
# ---------------------------------------------------------------------------


@pytest.fixture
def _seeded_schema(temp_db):
    """Re-run init_schema's seed loop AFTER conftest.temp_db's TRUNCATE
    so the vendor-official hosts (microsoft/localhost, zoom/mcp.zoom.us) are
    present. Function-scoped + dependent on temp_db to enforce ordering.
    Idempotent — ON CONFLICT DO NOTHING means re-runs are safe.
    """
    from storage import schema
    from storage.pg import get_conn
    with get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
    return temp_db


@pytest.mark.usefixtures("_seeded_schema")
class TestVendorHostSeeds:
    """init_schema seeds the vendor-official hosts. The fixture above
    re-runs the seed loop after temp_db's TRUNCATE so these tests are
    independent of DB state.
    """

    def test_microsoft_localhost_seeded(self):
        """m365-mcp is a Docker container; bearer is forwarded to the
        local container at http://localhost:${port}/mcp — NOT directly
        to graph.microsoft.com (the container makes Graph calls itself
        with the forwarded token)."""
        entries = bearer_allowlist.list_allowed()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in entries}
        assert ("microsoft", "localhost") in hosts

    def test_zoom_mcp_zoom_us_seeded(self):
        """zoom-mcp is a remote bearer-required MCP at mcp.zoom.us."""
        entries = bearer_allowlist.list_allowed()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in entries}
        assert ("zoom", "mcp.zoom.us") in hosts
