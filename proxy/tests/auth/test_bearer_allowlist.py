"""Bearer-allowlist tests — storage + matcher + manifest validator
+ runtime injector.

Storage tests hit the real PG (entries are namespaced by random provider
ids so they don't collide). Matcher + validator + injector are pure
functions exercised with synthetic inputs.
"""

import uuid
import pytest

from storage.identity import bearer_allowlist
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

    def test_bearer_required_with_sse_transport_rejected(self):
        # The credential gateway confines every forward to the one declared
        # path; an SSE endpoint event names another one.
        with pytest.raises(ValueError, match="streamable-HTTP") as info:
            mcp_registry._validate_oauth_services(
                {
                    "provider_id": "slack",
                    "flows": ["authorization_code"],
                    "bearer_required": True,
                    "proposed_hosts": ["mcp.slack.com"],
                },
                "slack-mcp",
                {"transport": "sse"},
            )
        assert "declared path" in str(info.value)

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


# The runtime side (the gateway entry a bearer manifest gets, the refusal of
# an unapproved host) is tests/mcp/test_gateway_entry.py.


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

    def test_postiz_api_postiz_com_seeded(self):
        """postiz-mcp is a remote bearer-required MCP at api.postiz.com —
        the user's Postiz API key rides as the bearer to the hosted MCP."""
        entries = bearer_allowlist.list_allowed()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in entries}
        assert ("postiz", "api.postiz.com") in hosts

    def test_github_hosted_server_seeded(self):
        """GitHub's hosted MCP server, reached through the credential
        gateway with the person's own token."""
        entries = bearer_allowlist.list_allowed()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in entries}
        assert ("github", "api.githubcopilot.com") in hosts

    def test_a_deleted_default_stays_deleted_across_the_seed(self):
        """The seed inserts each default once (the ledger remembers it), so
        an admin's deletion survives a restart; "Restore defaults" is the
        way back."""
        from storage.pg import get_conn
        row = [e for e in bearer_allowlist.list_allowed()
               if (e["provider_id"], e["host_pattern"]) == ("github", "api.githubcopilot.com")][0]
        assert bearer_allowlist.delete_allowed(row["id"])
        with get_conn() as conn:
            bearer_allowlist.seed_defaults(conn)
            conn.commit()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in bearer_allowlist.list_allowed()}
        assert ("github", "api.githubcopilot.com") not in hosts
        bearer_allowlist.restore_defaults()
        hosts = {(e["provider_id"], e["host_pattern"]) for e in bearer_allowlist.list_allowed()}
        assert ("github", "api.githubcopilot.com") in hosts

    def test_every_change_moves_the_generation(self):
        g0 = bearer_allowlist.generation()
        row = bearer_allowlist.add_allowed(f"gen-{uuid.uuid4().hex[:6]}", "h.example.com", "test")
        g1 = bearer_allowlist.generation()
        assert g1 > g0
        bearer_allowlist.delete_allowed(row)
        assert bearer_allowlist.generation() > g1
