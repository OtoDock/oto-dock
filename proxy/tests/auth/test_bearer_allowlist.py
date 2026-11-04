"""Bearer-allowlist tests — storage + matcher + manifest validator
+ runtime injector.

Storage tests hit the real PG (entries are namespaced by random provider
ids so they don't collide). Matcher + validator + injector are pure
functions exercised with synthetic inputs.
"""

import uuid

import pytest

from services.mcp import mcp_registry
from storage.identity import bearer_allowlist


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

    def test_only_an_mcps_row_counts_for_mcp_row_allows(self, seeded_provider):
        """What waits for an Allow ignores an admin's rows: only a row an MCP
        owns covers a host there, wildcards and case as the gateway reads them."""
        assert not bearer_allowlist.mcp_row_allows(seeded_provider, "mcp.example.com")
        bearer_allowlist.set_mcp_hosts("matcher-test-mcp", approved={(seeded_provider, "*.vendor.example"): "catalog"},
                                       live={(seeded_provider, "*.vendor.example")}, by="catalog")
        try:
            assert bearer_allowlist.mcp_row_allows(seeded_provider, "API.vendor.example")
            assert not bearer_allowlist.mcp_row_allows(seeded_provider, "vendor.example")
            assert not bearer_allowlist.mcp_row_allows("another-provider", "api.vendor.example")
        finally:
            bearer_allowlist.forget_mcp("matcher-test-mcp")

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
# No predefined list, and the removal of an admin's rows
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_schema(temp_db):
    """Re-run init_schema AFTER conftest.temp_db's TRUNCATE, as a boot does
    on an existing database. Function-scoped and dependent on temp_db to
    enforce ordering."""
    from storage import schema
    from storage.pg import get_conn
    with get_conn() as conn:
        schema.init_schema(conn)
        conn.commit()
    return temp_db


@pytest.mark.usefixtures("_fresh_schema")
class TestNoPredefinedList:
    """The boot inserts no row: every address belongs to an installed MCP's
    manifest or to an admin, and a boot leaves an existing row alone."""

    def test_a_boot_inserts_no_row(self):
        assert bearer_allowlist.list_allowed() == []

    def test_a_boot_keeps_an_existing_row(self):
        from storage import schema
        from storage.pg import get_conn
        row_id = bearer_allowlist.add_allowed("slack", "mcp.slack.com", "system")
        with get_conn() as conn:
            schema.init_schema(conn)
            conn.commit()
        assert [(r["id"], r["source"]) for r in bearer_allowlist.list_allowed()] == [(row_id, "admin")]

    def test_removing_the_admins_rows_keeps_every_mcp_row(self):
        custom_id = bearer_allowlist.add_allowed("custom-prov", "custom.example.com")
        bearer_allowlist.add_allowed("slack", "mcp.slack.com", "system")
        bearer_allowlist.set_mcp_hosts("restore-test-mcp", approved={("custom-prov", "custom.example.com"): "catalog"},
                                       live={("custom-prov", "custom.example.com")}, by="catalog")
        try:
            g0 = bearer_allowlist.generation()
            rows = bearer_allowlist.remove_admin_rows()
            assert bearer_allowlist.generation() > g0
            assert not any(r["id"] == custom_id for r in rows)
            assert [(r["provider_id"], r["host_pattern"], r["source"]) for r in rows] \
                == [("custom-prov", "custom.example.com", "restore-test-mcp")]
            assert bearer_allowlist.remove_admin_rows() == rows  # twice: nothing more goes
            assert not bearer_allowlist.is_host_allowed("slack", "mcp.slack.com")
        finally:
            bearer_allowlist.forget_mcp("restore-test-mcp")

    def test_every_change_moves_the_generation(self):
        g0 = bearer_allowlist.generation()
        row = bearer_allowlist.add_allowed(f"gen-{uuid.uuid4().hex[:6]}", "h.example.com", "test")
        g1 = bearer_allowlist.generation()
        assert g1 > g0
        bearer_allowlist.delete_allowed(row)
        assert bearer_allowlist.generation() > g1
