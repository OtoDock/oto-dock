"""Manifest validator tests for `credentials.webhooks`.

Pure validator tests (no DB, no httpx). Mirrors `test_manifest_costs.py`
+ `test_manifest_oauth_scopes.py` patterns. Exercises the
``_validate_webhooks_block`` function in mcp_registry.
"""

from __future__ import annotations

import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


def _minimal_valid_block():
    """A minimal block that passes the validator. Tests mutate this."""
    return {
        "available": True,
        "provider_id": "github",
        "signature": {
            "algorithm": "hmac-sha256",
            "header": "X-Hub-Signature-256",
            "per_subscription_secret": True,
        },
        "url_verification": {"kind": "none"},
        "registration": {
            "mode": "auto",
            "create": {
                "method": "POST",
                "url_template": "https://api.example.com/hooks",
                "expected_status": [201],
            },
        },
        "event_catalog": [
            {"key": "push", "label": "Commits pushed", "required_scopes": ["repo"]},
        ],
        "payload_normalization": {"event_type_path": "headers.X-Event"},
        "vendor_target_spec": {"kind": "free_text", "label": "Target"},
    }


def test_validator_none_passes():
    """Manifest without a webhooks block is valid (block is optional)."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    _validate_webhooks_block(None, "test-mcp")


def test_validator_available_false_skips_deeper_check():
    """`available=false` short-circuits — incomplete blocks are OK."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    _validate_webhooks_block(
        {"available": False, "provider_id": "x"},  # missing signature etc.
        "test-mcp",
    )


def test_validator_minimal_valid_block_passes():
    from services.mcp.mcp_registry import _validate_webhooks_block
    _validate_webhooks_block(_minimal_valid_block(), "test-mcp")


def test_validator_not_an_object_raises():
    from services.mcp.mcp_registry import _validate_webhooks_block
    with pytest.raises(ValueError, match="must be an object"):
        _validate_webhooks_block([], "test-mcp")


def test_validator_missing_provider_id():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["provider_id"] = ""
    with pytest.raises(ValueError, match="provider_id"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_missing_signature_block():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    del block["signature"]
    with pytest.raises(ValueError, match="signature"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_unsupported_algorithm():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["algorithm"] = "md5"
    with pytest.raises(ValueError, match="algorithm"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_signature_header_required():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["header"] = ""
    with pytest.raises(ValueError, match="header"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_per_subscription_secret_false_requires_credential_key():
    """When platform-wide secret, manifest MUST declare secret_credential_key."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["per_subscription_secret"] = False
    block["signature"]["secret_credential_key"] = ""
    with pytest.raises(ValueError, match="secret_credential_key"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_unknown_url_verification_kind():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["url_verification"]["kind"] = "magic-shake"
    with pytest.raises(ValueError, match="url_verification.kind"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_url_verification_kind_slack_requires_fields():
    """kind=slack_challenge needs request_field + response_field etc."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["url_verification"] = {"kind": "slack_challenge"}  # missing all sub-fields
    with pytest.raises(ValueError, match="request_field"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_registration_mode_invalid():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["registration"]["mode"] = "yolo"
    with pytest.raises(ValueError, match="registration.mode"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_registration_mode_manual_requires_instructions_url():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["registration"] = {"mode": "manual"}
    with pytest.raises(ValueError, match="manual_instructions_url"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_must_be_non_empty():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = []
    with pytest.raises(ValueError, match="event_catalog"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_entry_missing_key():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = [{"label": "No key"}]
    with pytest.raises(ValueError, match=r"event_catalog\[0\].key"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_duplicate_key_rejected():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = [
        {"key": "push", "label": "Push"},
        {"key": "push", "label": "Push again"},
    ]
    with pytest.raises(ValueError, match="duplicates"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_resource_contains_accepted():
    """Optional per-entry `resource_contains` (MS-Graph-style per-item
    canonicalization) — a non-empty string passes."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = [
        {"key": "calendar_events", "label": "Calendar",
         "resource_contains": "/events"},
    ]
    _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_resource_contains_must_be_string():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    for bad in ("", 42, ["/events"]):
        block["event_catalog"] = [
            {"key": "calendar_events", "label": "Calendar",
             "resource_contains": bad},
        ]
        with pytest.raises(ValueError, match="resource_contains"):
            _validate_webhooks_block(block, "test-mcp")


def test_validator_payload_normalization_event_type_path_required():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["payload_normalization"] = {}
    with pytest.raises(ValueError, match="event_type_path"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_vendor_target_spec_kind_invalid():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["vendor_target_spec"] = {"kind": "magic", "label": "x"}
    with pytest.raises(ValueError, match="vendor_target_spec.kind"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_vendor_target_spec_remote_list_requires_endpoint():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["vendor_target_spec"] = {"kind": "remote_list", "label": "x"}
    with pytest.raises(ValueError, match="list_endpoint"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_vendor_target_spec_static_list_requires_options():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["vendor_target_spec"] = {"kind": "static_list", "label": "x"}
    with pytest.raises(ValueError, match="static_options"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_renew_block_validates_when_present():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["registration"]["renew"] = {
        "method": "PATCH",
        "url_template": "https://api.example.com/sub/${vendor_subscription_id}",
        "expected_status": [200],
        "renew_before_seconds": -1,
    }
    with pytest.raises(ValueError, match="renew_before_seconds"):
        _validate_webhooks_block(block, "test-mcp")


def _community_manifest(name: str) -> dict:
    """Load a community MCP's manifest, skipping when it isn't on disk —
    community MCPs ship from the separate community-mcps repo and are
    gitignored here, so a fresh checkout legitimately lacks them."""
    import json
    from pathlib import Path

    manifest_path = (
        Path(_proxy_root).parent / "mcps" / "community" / name / "manifest.json"
    )
    if not manifest_path.is_file():
        pytest.skip(f"{name} manifest not present in this checkout")
    return json.loads(manifest_path.read_text())


def test_validator_github_manifest_passes():
    """Sanity: the shipped github-mcp manifest's webhooks block validates."""
    from services.mcp.mcp_registry import _validate_webhooks_block

    webhooks = _community_manifest("github-mcp")["credentials"]["webhooks"]
    _validate_webhooks_block(webhooks, "github-mcp")


# ───────────────────────────────────────────────────────────────────────
# Shipped vendor manifests
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["linear-mcp", "slack-mcp", "m365-mcp", "zoom-mcp"])
def test_validator_phase_3_5_vendor_manifests_pass(name):
    """Each shipped vendor manifest's webhooks block validates."""
    from services.mcp.mcp_registry import _validate_webhooks_block

    webhooks = _community_manifest(name)["credentials"]["webhooks"]
    _validate_webhooks_block(webhooks, name)


# ───────────────────────────────────────────────────────────────────────
# `client_state_echo` algorithm + relaxed header rule
# ───────────────────────────────────────────────────────────────────────


def test_validator_client_state_echo_algorithm_allowed():
    """The new ``client_state_echo`` algorithm is a valid choice."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["algorithm"] = "client_state_echo"
    # client_state_echo doesn't read any header — empty header is OK
    block["signature"]["header"] = ""
    _validate_webhooks_block(block, "test-mcp")


def test_validator_client_state_echo_allows_empty_header():
    """client_state_echo relaxes the non-empty-header rule (verification
    is in the subclass override; manifest header is ignored)."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["algorithm"] = "client_state_echo"
    block["signature"]["header"] = ""
    # Should NOT raise even though header is empty.
    _validate_webhooks_block(block, "test-mcp")


def test_validator_hmac_algorithms_still_require_header():
    """The relaxation is scoped to client_state_echo — HMAC variants still
    require a non-empty header."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["algorithm"] = "hmac-sha256"
    block["signature"]["header"] = ""
    with pytest.raises(ValueError, match="header must be a non-empty string"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_unknown_algorithm_rejected():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["signature"]["algorithm"] = "not-a-real-algo"
    with pytest.raises(ValueError, match="algorithm"):
        _validate_webhooks_block(block, "test-mcp")


# ───────────────────────────────────────────────────────────────────────
# `success_path` field on registration.create
# ───────────────────────────────────────────────────────────────────────


def test_validator_success_path_optional_string_allowed():
    """success_path is optional; when declared it must be a string."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["registration"]["create"]["success_path"] = "data.webhookCreate.success"
    _validate_webhooks_block(block, "test-mcp")


def test_validator_success_path_non_string_rejected():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["registration"]["create"]["success_path"] = ["not", "a", "string"]
    with pytest.raises(ValueError, match="success_path"):
        _validate_webhooks_block(block, "test-mcp")


# --- E1 additions: match / delivery / admin_only / workspace_id_path /
# --- enrichment / account_extra_key / reserved 'relay' ----------------------

def test_validator_reserved_relay_provider_id():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["provider_id"] = "relay"
    with pytest.raises(ValueError, match="reserved"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_match_block_happy():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = [
        {
            "key": "message.channels", "label": "Channel messages",
            "match": {
                "event_type": "message",
                "conditions": {"body.event.channel_type": ["channel"]},
            },
        },
        {
            "key": "message.im", "label": "DMs",
            "match": {
                "event_type": "message",
                "conditions": {"body.event.channel_type": "im"},
            },
        },
        {"key": "reaction_added", "label": "Reactions"},
    ]
    _validate_webhooks_block(block, "test-mcp")


def test_validator_match_missing_event_type():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"][0]["match"] = {"conditions": {"body.x": "y"}}
    with pytest.raises(ValueError, match="match.event_type"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_match_condition_bad_prefix():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"][0]["match"] = {
        "event_type": "message", "conditions": {"event.channel_type": "im"},
    }
    with pytest.raises(ValueError, match="must start with 'body.'"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_match_condition_bad_value():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"][0]["match"] = {
        "event_type": "message", "conditions": {"body.x": []},
    }
    with pytest.raises(ValueError, match="non-empty list"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_delivery_and_admin_only():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"][0]["delivery"] = "bot"
    block["event_catalog"][0]["admin_only"] = True
    _validate_webhooks_block(block, "test-mcp")
    block["event_catalog"][0]["delivery"] = "robot"
    with pytest.raises(ValueError, match="delivery"):
        _validate_webhooks_block(block, "test-mcp")
    block["event_catalog"][0]["delivery"] = "user"
    block["event_catalog"][0]["admin_only"] = "yes"
    with pytest.raises(ValueError, match="admin_only"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_workspace_id_path():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["workspace_id_path"] = "body.team_id"
    _validate_webhooks_block(block, "test-mcp")
    block["workspace_id_path"] = "team_id"
    with pytest.raises(ValueError, match="workspace_id_path"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_account_extra_key():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["vendor_target_spec"]["account_extra_key"] = "team_id"
    _validate_webhooks_block(block, "test-mcp")
    block["vendor_target_spec"]["account_extra_key"] = 42
    with pytest.raises(ValueError, match="account_extra_key"):
        _validate_webhooks_block(block, "test-mcp")


def _enrichment_block():
    return {
        "lookups": [
            {
                "source_field": "actor.id",
                "request": {
                    "method": "GET",
                    "url_template": "https://slack.com/api/users.info?user=${value}",
                    "expected_status": [200],
                },
                "outputs": {
                    "actor.name": [
                        "body.user.profile.display_name", "body.user.real_name",
                    ],
                },
                "ttl_seconds": 900,
            },
        ],
    }


def test_validator_enrichment_happy():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["enrichment"] = _enrichment_block()
    _validate_webhooks_block(block, "test-mcp")


def test_validator_enrichment_bad_source_field():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["enrichment"] = _enrichment_block()
    block["enrichment"]["lookups"][0]["source_field"] = "event.user"
    with pytest.raises(ValueError, match="source_field"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_enrichment_bad_outputs():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["enrichment"] = _enrichment_block()
    block["enrichment"]["lookups"][0]["outputs"] = {"name": "body.user.name"}
    with pytest.raises(ValueError, match="outputs"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_enrichment_bad_ttl():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["enrichment"] = _enrichment_block()
    block["enrichment"]["lookups"][0]["ttl_seconds"] = 0
    with pytest.raises(ValueError, match="ttl_seconds"):
        _validate_webhooks_block(block, "test-mcp")


# --- verification_token_capture kind (notion-class in-band setup) -----------------

def _capture_uv_block():
    return {
        "kind": "verification_token_capture",
        "request_field": "verification_token",
        "request_source": "body",
        "response_field": "ok",
        "response_content_type": "application/json",
    }


def test_validator_capture_kind_valid():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()  # per_subscription_secret=True already
    block["url_verification"] = _capture_uv_block()
    _validate_webhooks_block(block, "test-mcp")


def test_validator_capture_kind_requires_body_source():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["url_verification"] = {**_capture_uv_block(), "request_source": "query"}
    with pytest.raises(ValueError, match="request_source"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_capture_kind_requires_per_subscription_secret():
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["url_verification"] = _capture_uv_block()
    block["signature"]["per_subscription_secret"] = False
    block["signature"]["secret_credential_key"] = "X_SECRET"
    with pytest.raises(ValueError, match="per_subscription_secret"):
        _validate_webhooks_block(block, "test-mcp")


def test_validator_notion_shaped_block_valid():
    """The real notion shape: ts-less sha256= signature + capture handshake
    + workspace_id_path + plain catalog keys."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["provider_id"] = "notion"
    block["signature"] = {
        "algorithm": "hmac-sha256",
        "header": "X-Notion-Signature",
        "prefix": "sha256=",
        "per_subscription_secret": True,
    }
    block["url_verification"] = _capture_uv_block()
    block["registration"] = {
        "mode": "manual",
        "manual_instructions_url": "https://developers.notion.com/reference/webhooks",
    }
    block["event_catalog"] = [
        {"key": "comment.created", "label": "Comments added"},
        {"key": "page.content_updated", "label": "Page edits"},
    ]
    block["payload_normalization"] = {
        "event_type_path": "body.type",
        "actor": {"id_path": "body.authors.0.id"},
    }
    block["event_id_field"] = "body.id"
    block["workspace_id_path"] = "body.workspace_id"
    _validate_webhooks_block(block, "test-mcp")


def test_validator_event_catalog_vendor_create_fields():
    """Optional per-entry vendor_create_fields — non-empty {str: str} object."""
    from services.mcp.mcp_registry import _validate_webhooks_block
    block = _minimal_valid_block()
    block["event_catalog"] = [
        {"key": "drive_root", "label": "Drive",
         "vendor_create_fields": {"changeType": "updated"}},
    ]
    _validate_webhooks_block(block, "test-mcp")
    for bad in ({}, "updated", {"changeType": 42}, {"": "x"}):
        block["event_catalog"] = [
            {"key": "drive_root", "label": "Drive", "vendor_create_fields": bad},
        ]
        with pytest.raises(ValueError, match="vendor_create_fields"):
            _validate_webhooks_block(block, "test-mcp")
