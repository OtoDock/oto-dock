"""Tests for the manifest `credentials.oauth` block validator
(`services/mcp_registry._validate_oauth_services`).

Strict validation protects against community MCPs shipping garbage scope
arrays that silently grant nothing (broken integration) or grant the
wrong scopes (security issue). Every test here is a contract guarantee
for community MCP authors.

An earlier revision was Google-specific. The validator was later generalized:
every oauth block must now declare ``provider_id``, and per-provider scope
regexes apply (Google's still strict; generic providers only check non-empty
strings).

The tests below stay Google-flavored — a tiny ``_validate`` wrapper
auto-injects ``provider_id: "google"`` so the test bodies stay focused
on the per-field semantics they exercise. Provider-id-specific tests
live in ``TestProviderId``.
"""

import pytest

from services.mcp.mcp_registry import _validate_oauth_services as _strict_validate


def _validate_oauth_services(raw, mcp_name):
    """Inject ``provider_id`` + ``flows`` for compact test fixtures.

    Most tests assert one specific field's validation rule and don't
    bother constructing a fully-valid wrapper. Tests that want to assert
    the provider-id-missing or flows-missing failure modes call
    ``_strict_validate`` directly.
    """
    if isinstance(raw, dict):
        if "provider_id" not in raw and "provider" not in raw:
            raw = {**raw, "provider_id": "google"}
        if "flows" not in raw:
            raw = {**raw, "flows": ["authorization_code"]}
    return _strict_validate(raw, mcp_name)


def _raw_validate(raw, mcp_name, server_raw=None):
    """Auto-inject ``flows`` for tests that already supply ``provider_id``.

    Lets tests focus on the field they're exercising without restating the
    full required minimum. Use ``_strict_validate`` directly when the
    intent is to assert a missing-required-field failure mode.
    """
    if isinstance(raw, dict) and "flows" not in raw:
        raw = {**raw, "flows": ["authorization_code"]}
    return _strict_validate(raw, mcp_name, server_raw)


# ═══════════════════════════════════════════════════════════════════════════
# Happy path — validator returns None and does not raise
# ═══════════════════════════════════════════════════════════════════════════


class TestValidOAuthBlock:
    def test_returns_none_when_omitted(self):
        assert _validate_oauth_services(None, "any") is None

    def test_minimal_valid_block_with_only_base_scopes(self):
        _validate_oauth_services({
            "base_scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        }, "any")

    def test_minimal_valid_block_with_one_service(self):
        _validate_oauth_services({
            "services": [
                {
                    "key": "gmail",
                    "label": "Gmail",
                    "description": "Read emails",
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                }
            ],
        }, "any")

    def test_full_google_workspace_block(self):
        """Round-trip a representative subset of the actual google-workspace manifest."""
        _validate_oauth_services({
            "base_scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
            "services": [
                {
                    "key": "gmail", "label": "Gmail",
                    "description": "Read and send emails",
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.send",
                    ],
                },
                {
                    "key": "drive", "label": "Drive",
                    "description": "Read and manage files",
                    "scopes": [
                        "https://www.googleapis.com/auth/drive",
                        "https://www.googleapis.com/auth/drive.file",
                    ],
                },
            ],
        }, "google-workspace")

    def test_empty_scopes_list_allowed(self):
        """OAuth-login-only services (no API scope) are allowed by the schema."""
        _validate_oauth_services({
            "services": [
                {
                    "key": "loginonly",
                    "label": "Login only",
                    "description": "OAuth identity, no API scopes",
                    "scopes": [],
                }
            ],
        }, "any")

    def test_extra_unknown_keys_ignored(self):
        """Validator only enforces required fields; extras don't raise."""
        _validate_oauth_services({
            "provider_id": "google",
            "base_scopes": ["openid"],
            "services": [
                {
                    "key": "gmail", "label": "Gmail", "description": "x",
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                    "future_field": "ignored",
                }
            ],
        }, "any")


# ═══════════════════════════════════════════════════════════════════════════
# Validator failures — must raise ValueError
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidOAuthBlock:
    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_oauth_services("nope", "x")

    def test_base_scopes_not_list(self):
        with pytest.raises(ValueError, match="base_scopes"):
            _validate_oauth_services({"base_scopes": "openid"}, "x")

    def test_base_scopes_empty_list(self):
        with pytest.raises(ValueError, match="base_scopes"):
            _validate_oauth_services({"base_scopes": []}, "x")

    def test_base_scope_non_string(self):
        with pytest.raises(ValueError, match=r"base_scopes\[0\]"):
            _validate_oauth_services({"base_scopes": [123]}, "x")

    def test_base_scope_empty_string(self):
        with pytest.raises(ValueError, match=r"base_scopes\[0\]"):
            _validate_oauth_services({"base_scopes": [""]}, "x")

    def test_base_scope_invalid_url(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"base_scopes": ["not-a-url"]}, "x")

    def test_base_scope_wrong_domain(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services(
                {"base_scopes": ["https://api.slack.com/scopes/chat:write"]}, "x"
            )

    def test_services_not_list(self):
        with pytest.raises(ValueError, match="services"):
            _validate_oauth_services({"services": "gmail"}, "x")

    def test_services_empty_list(self):
        with pytest.raises(ValueError, match="services"):
            _validate_oauth_services({"services": []}, "x")

    def test_service_entry_not_dict(self):
        with pytest.raises(ValueError, match=r"services\[0\]"):
            _validate_oauth_services({"services": ["gmail"]}, "x")

    def test_service_missing_key(self):
        with pytest.raises(ValueError, match=r"services\[0\].key"):
            _validate_oauth_services({"services": [
                {"label": "Gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_empty_key(self):
        with pytest.raises(ValueError, match=r"services\[0\].key"):
            _validate_oauth_services({"services": [
                {"key": "", "label": "Gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_missing_label(self):
        with pytest.raises(ValueError, match=r"services\[0\].label"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_empty_label(self):
        with pytest.raises(ValueError, match=r"services\[0\].label"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_missing_description(self):
        with pytest.raises(ValueError, match=r"services\[0\].description"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "scopes": []}
            ]}, "x")

    def test_service_missing_scopes_field(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x"}
            ]}, "x")

    def test_service_scopes_not_list(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": "gmail.readonly"}
            ]}, "x")

    def test_service_scope_non_string(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes\[0\]"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": [42]}
            ]}, "x")

    def test_service_scope_empty_string(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes\[0\]"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": [""]}
            ]}, "x")

    def test_service_scope_invalid_url(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": ["readonly"]}
            ]}, "x")

    def test_service_scope_wrong_domain(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"services": [
                {"key": "x", "label": "X", "description": "x",
                 "scopes": ["https://example.com/scope/foo"]}
            ]}, "x")

    def test_duplicate_service_keys(self):
        with pytest.raises(ValueError, match="duplicates"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x", "scopes": []},
                {"key": "gmail", "label": "Mail2", "description": "y", "scopes": []},
            ]}, "x")


# ═══════════════════════════════════════════════════════════════════════════
# Boundary cases that should pass — openid as base, "openid" inside services
# ═══════════════════════════════════════════════════════════════════════════


class TestBoundaryAllowed:
    def test_openid_scope_in_base(self):
        _validate_oauth_services({"base_scopes": ["openid"]}, "x")

    def test_openid_scope_in_service(self):
        _validate_oauth_services({"services": [
            {"key": "x", "label": "X", "description": "x", "scopes": ["openid"]}
        ]}, "x")


# ═══════════════════════════════════════════════════════════════════════════
# Provider_id, generic provider acceptance, bearer_required,
# capabilities, token_format, refresh.
# ═══════════════════════════════════════════════════════════════════════════


class TestProviderId:
    def test_missing_provider_id_raises(self):
        with pytest.raises(ValueError, match="provider_id"):
            _raw_validate({"base_scopes": ["openid"]}, "x")

    def test_legacy_provider_field_rejected(self):
        with pytest.raises(ValueError, match="provider_id"):
            _raw_validate(
                {"provider": "google", "base_scopes": ["openid"]}, "x",
            )

    def test_unknown_provider_skips_scope_url_regex(self):
        """A provider_id without a hardcoded scope regex (generic
        providers) is accepted with arbitrary non-empty scope strings."""
        _raw_validate({
            "provider_id": "linear",
            "authorization_url": "https://linear.app/oauth/authorize",
            "token_url": "https://api.linear.app/oauth/token",
            "services": [{
                "key": "issues", "label": "Issues", "description": "Read issues",
                "scopes": ["read", "write"],
            }],
        }, "linear-mcp")

    def test_google_provider_still_enforces_strict_scopes(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _raw_validate({
                "provider_id": "google",
                "services": [{
                    "key": "x", "label": "X", "description": "x",
                    "scopes": ["read"],
                }],
            }, "x")


class TestBearerRequired:
    def test_bearer_required_requires_proposed_hosts(self):
        with pytest.raises(ValueError, match="proposed_hosts"):
            _raw_validate(
                {"provider_id": "slack", "bearer_required": True},
                "slack-mcp",
            )

    def test_bearer_required_validates_hostname_format(self):
        with pytest.raises(ValueError, match="not a valid hostname"):
            _raw_validate(
                {
                    "provider_id": "slack",
                    "bearer_required": True,
                    "proposed_hosts": ["https://mcp.slack.com"],
                },
                "slack-mcp",
            )

    def test_bearer_required_cross_checks_transport(self):
        """When server.transport is stdio, bearer_required=true must reject."""
        with pytest.raises(ValueError, match="HTTP-class"):
            _raw_validate(
                {
                    "provider_id": "slack",
                    "bearer_required": True,
                    "proposed_hosts": ["mcp.slack.com"],
                },
                "slack-mcp",
                {"transport": "stdio"},
            )

    def test_bearer_required_accepts_http_transport(self):
        _raw_validate(
            {
                "provider_id": "slack",
                "bearer_required": True,
                "proposed_hosts": ["mcp.slack.com"],
            },
            "slack-mcp",
            {"transport": "streamable_http"},
        )

    def test_bearer_required_accepts_wildcard_host(self):
        _raw_validate(
            {
                "provider_id": "linear",
                "bearer_required": True,
                "proposed_hosts": ["*.linear.app"],
            },
            "linear-mcp",
            {"transport": "http"},
        )


class TestOptionalFields:
    def test_token_format_must_be_object(self):
        with pytest.raises(ValueError, match="token_format"):
            _raw_validate(
                {"provider_id": "google", "token_format": "workspace_mcp"},
                "x",
            )

    def test_token_format_schema_required_when_object(self):
        with pytest.raises(ValueError, match="token_format.schema"):
            _raw_validate(
                {"provider_id": "google", "token_format": {"schema": ""}},
                "x",
            )

    def test_refresh_strategy_rejects_unknown(self):
        with pytest.raises(ValueError, match="strategy"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "refresh": {"strategy": "aggressive"},
                },
                "x",
            )

    def test_refresh_min_remaining_must_be_positive(self):
        with pytest.raises(ValueError, match="min_remaining_seconds"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "refresh": {"strategy": "lazy", "min_remaining_seconds": -1},
                },
                "x",
            )

    def test_boolean_fields_type_checked(self):
        with pytest.raises(ValueError, match="supports_multi_account"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "supports_multi_account": "yes",
                },
                "x",
            )

    def test_capabilities_must_be_list_of_strings(self):
        with pytest.raises(ValueError, match="capabilities"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "services": [{
                        "key": "x", "label": "X", "description": "x",
                        "scopes": [],
                        "capabilities": "posts_as_other",
                    }],
                },
                "x",
            )

    def test_capabilities_accepted(self):
        _raw_validate(
            {
                "provider_id": "google",
                "services": [{
                    "key": "x", "label": "X", "description": "x",
                    "scopes": [],
                    "capabilities": ["posts_as_other_identity"],
                }],
            },
            "x",
        )

    def test_service_requires_admin_consent_type_checked(self):
        with pytest.raises(ValueError, match="requires_admin_consent"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "services": [{
                        "key": "x", "label": "X", "description": "x",
                        "scopes": [],
                        "requires_admin_consent": "yes",
                    }],
                },
                "x",
            )


# ═══════════════════════════════════════════════════════════════════════════
# credentials.oauth.authorization_server — the MCP server names its own
# authorization server; the install registers itself there.
# ═══════════════════════════════════════════════════════════════════════════


def _server_block(url="https://mcp.notion.com/mcp", transport="streamable_http"):
    return {"transport": transport, "url_template": url, "source": "remote:mcp.notion.com"}


def _as_block(**over):
    raw = {
        "provider_id": "notion-hosted",
        "flows": ["authorization_code_pkce"],
        "authorization_server": {"registration": "dynamic", "confidential": False},
        "bearer_required": True,
        "proposed_hosts": ["mcp.notion.com"],
    }
    raw.update(over)
    return raw


class TestAuthorizationServerBlock:
    def test_minimal_block_accepted_without_services(self):
        """A server that names its own scopes needs no services list."""
        _strict_validate(_as_block(), "notion-hosted-mcp", _server_block())

    def test_block_accepted_with_services_and_identity(self):
        raw = _as_block(
            authorization_server={
                "registration": "dynamic", "confidential": True,
                "issuer": "https://mcp.notion.com", "client_name": "OtoDock",
                "scopes": ["default"],
                "identity": {"label_field": "workspace_id", "display_field": "email_domain"},
            },
            services=[{"key": "default", "label": "Workspace", "description": "d",
                       "scopes": ["default"]}],
        )
        _strict_validate(raw, "notion-hosted-mcp", _server_block())

    def test_block_must_be_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _strict_validate(_as_block(authorization_server="dynamic"), "m", _server_block())

    @pytest.mark.parametrize("field", ["confidential", "accepts_app_tokens"])
    def test_the_boolean_fields(self, field):
        _strict_validate(_as_block(authorization_server={field: True}), "m", _server_block())
        with pytest.raises(ValueError, match=f"{field} must be a boolean"):
            _strict_validate(_as_block(authorization_server={field: "yes"}), "m", _server_block())

    @pytest.mark.parametrize("flows", [["authorization_code"], ["authorization_code_pkce", "personal_access_token"]])
    def test_flows_must_be_exactly_pkce(self, flows):
        with pytest.raises(ValueError, match="authorization_code_pkce"):
            _strict_validate(_as_block(flows=flows), "m", _server_block())

    def test_app_flow_urls_may_stay_beside_the_block(self):
        """An admin's own app keeps today's flow; the registered client
        discovers its endpoints and ignores these."""
        _strict_validate(_as_block(authorization_url="https://x/auth", token_url="https://x/token",
                                   revoke_url="https://x/revoke", app_credential="x-app"),
                         "m", _server_block())

    @pytest.mark.parametrize("key,value", [
        ("device_authorization_url", "https://x/d"),
        ("app_credential_variants", {"device_code": "x"}), ("authorize_params", {"owner": "user"}),
        ("env_injection", ["TOKEN"]), ("mcp_env_injection", ["TOKEN"]),
        ("git_credential_helper", {"host": "h", "helper": "x"}),
    ])
    def test_excluded_keys_refused(self, key, value):
        with pytest.raises(ValueError, match=f"credentials.oauth.{key} cannot be declared beside"):
            _strict_validate(_as_block(**{key: value}), "m", _server_block())

    def test_registration_mode_must_be_dynamic(self):
        with pytest.raises(ValueError, match="registration='metadata_document' is not supported"):
            _strict_validate(_as_block(authorization_server={"registration": "metadata_document"}), "m", _server_block())

    def test_confidential_is_a_boolean(self):
        with pytest.raises(ValueError, match="confidential must be a boolean"):
            _strict_validate(_as_block(authorization_server={"confidential": "yes"}), "m", _server_block())

    @pytest.mark.parametrize("issuer", ["http://mcp.notion.com", "https://mcp.notion.com/?x=1",
                                        "https://mcp.notion.com/#f", "mcp.notion.com"])
    def test_issuer_must_be_clean_https(self, issuer):
        with pytest.raises(ValueError, match="issuer must be an https URL"):
            _strict_validate(_as_block(authorization_server={"issuer": issuer}), "m", _server_block())

    def test_scopes_and_identity_shapes(self):
        with pytest.raises(ValueError, match="scopes must be a list of"):
            _strict_validate(_as_block(authorization_server={"scopes": ["read", ""]}), "m", _server_block())
        with pytest.raises(ValueError, match="identity must be a non-empty object"):
            _strict_validate(_as_block(authorization_server={"identity": {}}), "m", _server_block())
        with pytest.raises(ValueError, match="identity.email is not a known field"):
            _strict_validate(_as_block(authorization_server={"identity": {"email": "e"}}), "m", _server_block())
        with pytest.raises(ValueError, match="identity.label_field must be a non-empty string"):
            _strict_validate(_as_block(authorization_server={"identity": {"label_field": ""}}), "m", _server_block())

    @pytest.mark.parametrize("pid", ["google", "slack", "microsoft", "zoom", "facebook"])
    def test_hardcoded_provider_cannot_carry_the_block(self, pid):
        with pytest.raises(ValueError, match="own Python class"):
            _strict_validate(_as_block(provider_id=pid), "m", _server_block())

    def test_bearer_required_is_required(self):
        raw = _as_block()
        raw.pop("bearer_required")
        raw.pop("proposed_hosts")
        with pytest.raises(ValueError, match="requires bearer_required=true"):
            _strict_validate(raw, "m", _server_block())

    def test_url_template_must_be_https_with_a_literal_host(self):
        with pytest.raises(ValueError, match="https server.url_template with a literal host"):
            _strict_validate(_as_block(), "m", _server_block(url="http://mcp.notion.com/mcp"))
        with pytest.raises(ValueError, match="https server.url_template with a literal host"):
            _strict_validate(_as_block(proposed_hosts=["localhost"]), "m",
                             _server_block(url="https://${docker_mcp_host}:8935/mcp"))

    def test_url_template_host_must_match_proposed_hosts(self):
        with pytest.raises(ValueError, match="proposed_hosts must match the server.url_template host 'mcp.linear.app'"):
            _strict_validate(_as_block(), "m", _server_block(url="https://mcp.linear.app/mcp"))
        # a wildcard pattern and a case difference both match
        _strict_validate(_as_block(proposed_hosts=["*.linear.app"]), "m",
                         _server_block(url="https://MCP.linear.app/mcp"))

    def test_userinfo_url_must_be_https_beside_the_block(self):
        with pytest.raises(ValueError, match="userinfo_url must be an https URL"):
            _strict_validate(_as_block(userinfo_url="http://mcp.notion.com/me"), "m", _server_block())
        _strict_validate(_as_block(userinfo_url="https://mcp.notion.com/me"), "m", _server_block())

    def test_transport_must_be_http_class(self):
        with pytest.raises(ValueError, match="HTTP-class"):
            _strict_validate(_as_block(), "m", _server_block(transport="stdio"))

    def test_manifests_without_the_block_keep_their_rules(self):
        """github-mcp's templated host with proposed_hosts localhost stays valid."""
        _raw_validate(
            {"provider_id": "github", "bearer_required": True, "proposed_hosts": ["localhost"]},
            "github-mcp",
            {"transport": "streamable_http", "url_template": "http://${docker_mcp_host}:8935/mcp"},
        )
