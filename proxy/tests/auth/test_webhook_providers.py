"""WebhookProvider tests.

Covers the GenericWebhookProvider implementations (HMAC variants, URL
handshakes, event-id extraction, payload normalization) AND the
event_normalizer pure functions used by the dispatcher + dynamic_context.

These are pure-logic tests — no DB, no httpx, no fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
import time

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ───────────────────────────────────────────────────────────────────────
# event_normalizer.walk_path
# ───────────────────────────────────────────────────────────────────────


def test_walk_path_body_dot_path():
    from services.webhooks.event_normalizer import walk_path
    body = {"a": {"b": {"c": "deep"}}}
    assert walk_path(body=body, headers={}, path="body.a.b.c") == "deep"


def test_walk_path_header_case_insensitive():
    from services.webhooks.event_normalizer import walk_path
    assert walk_path(
        body={}, headers={"X-Sig": "abc"}, path="headers.x-sig",
    ) == "abc"
    assert walk_path(
        body={}, headers={"X-SIG": "ABC"}, path="headers.X-Sig",
    ) == "ABC"


def test_walk_path_array_index():
    from services.webhooks.event_normalizer import walk_path
    body = {"arr": [{"k": "a"}, {"k": "b"}, {"k": "c"}]}
    assert walk_path(body=body, headers={}, path="body.arr.1.k") == "b"


def test_walk_path_composite_plus():
    from services.webhooks.event_normalizer import walk_path
    assert walk_path(
        body={"a": "x"}, headers={"X-B": "y"},
        path="body.a+headers.X-B",
    ) == "x+y"


def test_walk_path_empty_returns_empty():
    from services.webhooks.event_normalizer import walk_path
    assert walk_path(body={}, headers={}, path="") == ""
    assert walk_path(body={"a": None}, headers={}, path="body.a") == ""
    assert walk_path(body={}, headers={}, path="body.missing.key") == ""


def test_walk_path_container_value_returns_empty():
    """Path resolving to a dict/list returns empty (don't dump JSON)."""
    from services.webhooks.event_normalizer import walk_path
    assert walk_path(body={"a": {"b": 1}}, headers={}, path="body.a") == ""


# ───────────────────────────────────────────────────────────────────────
# event_normalizer.normalize_event
# ───────────────────────────────────────────────────────────────────────


def test_normalize_event_github_pull_request():
    from services.webhooks.event_normalizer import normalize_event
    body = {
        "action": "opened",
        "pull_request": {"number": 42, "title": "Add foo",
                          "html_url": "https://github.com/octocat/hello/pull/42"},
        "repository": {"full_name": "octocat/hello",
                        "html_url": "https://github.com/octocat/hello"},
        "sender": {"id": 1, "login": "octocat",
                    "html_url": "https://github.com/octocat"},
    }
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "abc-123"}
    manifest = {
        "event_type_path": "headers.X-GitHub-Event",
        "actor": {"id_path": "body.sender.id",
                  "name_path": "body.sender.login",
                  "url_path": "body.sender.html_url"},
        "subject": {"type_path": "body.action",
                    "id_path": "body.pull_request.number",
                    "title_path": "body.pull_request.title",
                    "url_path": "body.pull_request.html_url"},
        "target": {"type": "repository",
                   "id_path": "body.repository.full_name",
                   "url_path": "body.repository.html_url"},
    }
    event = normalize_event(
        body=body, headers=headers,
        manifest_block=manifest, vendor_event_id="abc-123",
    )
    assert event.event_type == "pull_request"
    assert event.vendor_event_id == "abc-123"
    assert event.actor["name"] == "octocat"
    assert event.actor["id"] == "1"
    assert event.subject["type"] == "opened"
    assert event.subject["id"] == "42"
    assert event.subject["title"] == "Add foo"
    assert event.target["type"] == "repository"
    assert event.target["id"] == "octocat/hello"


def test_normalize_event_missing_paths_render_empty():
    from services.webhooks.event_normalizer import normalize_event
    manifest = {
        "event_type_path": "body.event.type",
        "actor": {"email_path": "body.event.user.email"},
        "subject": {"id_path": "body.event.ts"},
    }
    event = normalize_event(
        body={"event": {"type": "message", "ts": "1700.0001"}},
        headers={},
        manifest_block=manifest,
    )
    assert event.event_type == "message"
    assert event.subject["id"] == "1700.0001"
    assert event.actor.get("email", "") == ""  # path didn't resolve
    assert event.target == {}


# ───────────────────────────────────────────────────────────────────────
# event_normalizer.match_event_filter
# ───────────────────────────────────────────────────────────────────────


def test_match_event_filter_empty_matches_all():
    from auth.webhook_providers.base import NormalizedEvent
    from services.webhooks.event_normalizer import match_event_filter
    ev = NormalizedEvent(event_type="anything")
    assert match_event_filter(event=ev, event_filter={}) is True


def test_match_event_filter_exact_string():
    from auth.webhook_providers.base import NormalizedEvent
    from services.webhooks.event_normalizer import match_event_filter
    ev = NormalizedEvent(event_type="pull_request")
    assert match_event_filter(
        event=ev, event_filter={"event_type": "pull_request"},
    ) is True
    assert match_event_filter(
        event=ev, event_filter={"event_type": "issues"},
    ) is False


def test_match_event_filter_list_any_of():
    from auth.webhook_providers.base import NormalizedEvent
    from services.webhooks.event_normalizer import match_event_filter
    ev = NormalizedEvent(event_type="pull_request",
                         subject={"type": "opened"})
    assert match_event_filter(
        event=ev,
        event_filter={"subject.type": ["opened", "reopened"]},
    ) is True
    assert match_event_filter(
        event=ev,
        event_filter={"subject.type": ["closed", "merged"]},
    ) is False


def test_match_event_filter_dot_path():
    from auth.webhook_providers.base import NormalizedEvent
    from services.webhooks.event_normalizer import match_event_filter
    ev = NormalizedEvent(
        event_type="pull_request",
        target={"id": "octocat/hello", "type": "repository"},
    )
    assert match_event_filter(
        event=ev,
        event_filter={"target.id": "octocat/hello"},
    ) is True


def test_match_event_filter_multiple_keys_all_must_match():
    from auth.webhook_providers.base import NormalizedEvent
    from services.webhooks.event_normalizer import match_event_filter
    ev = NormalizedEvent(event_type="pull_request",
                         subject={"type": "opened"})
    assert match_event_filter(
        event=ev,
        event_filter={"event_type": "pull_request", "subject.type": "opened"},
    ) is True
    assert match_event_filter(
        event=ev,
        event_filter={"event_type": "pull_request", "subject.type": "closed"},
    ) is False


# ───────────────────────────────────────────────────────────────────────
# GenericWebhookProvider.verify_signature
# ───────────────────────────────────────────────────────────────────────


def _hmac_hex(secret: str, payload: str, alg=hashlib.sha256) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), alg).hexdigest()


def test_verify_signature_github_style_ok():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    secret = "test-secret"
    body = b'{"action":"opened"}'
    sig = _hmac_hex(secret, body.decode())
    result = p.verify_signature(
        raw_body=body,
        headers={"x-hub-signature-256": f"sha256={sig}"},
        signing_secret=secret,
        manifest_sig_block={
            "algorithm": "hmac-sha256",
            "header": "X-Hub-Signature-256",
            "prefix": "sha256=",
            "per_subscription_secret": True,
            "max_age_seconds": 0,
        },
    )
    assert result.ok
    assert result.reason == ""


def test_verify_signature_mismatch():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    secret = "test-secret"
    body = b'{"a":1}'
    sig = _hmac_hex(secret, body.decode())
    result = p.verify_signature(
        raw_body=body,
        headers={"x-hub-signature-256": f"sha256={sig}"},
        signing_secret="wrong-secret",
        manifest_sig_block={
            "algorithm": "hmac-sha256",
            "header": "X-Hub-Signature-256",
            "prefix": "sha256=",
        },
    )
    assert not result.ok
    assert result.reason == "signature_mismatch"


def test_verify_signature_missing_header():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    result = p.verify_signature(
        raw_body=b'{}',
        headers={},  # no signature header
        signing_secret="x",
        manifest_sig_block={"algorithm": "hmac-sha256",
                            "header": "X-Sig"},
    )
    assert not result.ok
    assert result.reason == "missing_header"


def test_verify_signature_unsupported_algorithm():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="x")
    result = p.verify_signature(
        raw_body=b'',
        headers={},
        signing_secret="x",
        manifest_sig_block={"algorithm": "md5", "header": "X-Sig"},
    )
    assert not result.ok
    assert result.reason == "unsupported_algorithm"


def test_verify_signature_slack_style_with_timestamp():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="slack")
    secret = "slack-secret"
    ts = str(int(time.time()))
    body = b'{"event": "test"}'
    signed = f"v0:{ts}:{body.decode()}"
    sig = _hmac_hex(secret, signed)
    result = p.verify_signature(
        raw_body=body,
        headers={"x-slack-signature": f"v0={sig}",
                 "x-slack-request-timestamp": ts},
        signing_secret=secret,
        manifest_sig_block={
            "algorithm": "hmac-sha256",
            "header": "X-Slack-Signature",
            "version_prefix": "v0=",
            "timestamp_header": "X-Slack-Request-Timestamp",
            "timestamp_format": "unix",
            "signed_payload_template": "v0:{timestamp}:{body}",
            "max_age_seconds": 300,
        },
    )
    assert result.ok


def test_verify_signature_timestamp_too_old():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="slack")
    secret = "s"
    old_ts = str(int(time.time()) - 3600)  # 1 hour ago
    body = b'{}'
    signed = f"v0:{old_ts}:{body.decode()}"
    sig = _hmac_hex(secret, signed)
    result = p.verify_signature(
        raw_body=body,
        headers={"x-slack-signature": f"v0={sig}",
                 "x-slack-request-timestamp": old_ts},
        signing_secret=secret,
        manifest_sig_block={
            "algorithm": "hmac-sha256",
            "header": "X-Slack-Signature",
            "version_prefix": "v0=",
            "timestamp_header": "X-Slack-Request-Timestamp",
            "signed_payload_template": "v0:{timestamp}:{body}",
            "max_age_seconds": 300,
        },
    )
    assert not result.ok
    assert result.reason == "timestamp_too_old"


def test_verify_signature_empty_secret_rejected():
    """An empty signing_secret would let anyone forge requests — reject."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="x")
    result = p.verify_signature(
        raw_body=b'{}',
        headers={"x-sig": "anything"},
        signing_secret="",
        manifest_sig_block={"algorithm": "hmac-sha256", "header": "X-Sig"},
    )
    assert not result.ok


# ───────────────────────────────────────────────────────────────────────
# GenericWebhookProvider.handle_url_verification
# ───────────────────────────────────────────────────────────────────────


def test_handshake_slack_challenge():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="slack")

    async def go():
        return await p.handle_url_verification(
            request_body={"challenge": "xyz"},
            query_params={},
            manifest_uv_block={
                "kind": "slack_challenge",
                "request_field": "challenge",
                "request_source": "body",
                "response_field": "challenge",
                "response_content_type": "application/json",
            },
            signing_secret="",
        )

    r = asyncio.run(go())
    assert r is not None
    status, body, headers = r
    assert status == 200
    assert "xyz" in body
    assert headers["content-type"] == "application/json"


def test_handshake_ms_graph_validation_token():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="microsoft")

    async def go():
        return await p.handle_url_verification(
            request_body={},
            query_params={"validationToken": "ms-token-123"},
            manifest_uv_block={
                "kind": "ms_graph_validation_token",
                "request_field": "validationToken",
                "request_source": "query",
                "response_field": "plain_text",
                "response_content_type": "text/plain",
            },
            signing_secret="",
        )

    r = asyncio.run(go())
    assert r is not None
    status, body, headers = r
    assert status == 200
    assert body == "ms-token-123"
    assert headers["content-type"] == "text/plain"


def test_handshake_zoom_endpoint_validation():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="zoom")
    secret = "zsecret"
    plain = "plaintoken"

    async def go():
        return await p.handle_url_verification(
            request_body={"event": "endpoint.url_validation",
                          "payload": {"plainToken": plain}},
            query_params={},
            manifest_uv_block={
                "kind": "zoom_endpoint_validation",
                "request_field": "plainToken",
                "request_source": "body",
                "response_field": "encryptedToken",
                "response_content_type": "application/json",
            },
            signing_secret=secret,
        )

    r = asyncio.run(go())
    assert r is not None
    status, body, _ = r
    assert status == 200
    # Body contains both plainToken and the HMAC-SHA256(secret, plain) hex.
    expected = _hmac_hex(secret, plain)
    assert plain in body
    assert expected in body


def test_handshake_none_returns_none():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")

    async def go():
        return await p.handle_url_verification(
            request_body={"action": "opened"},
            query_params={},
            manifest_uv_block={"kind": "none"},
            signing_secret="",
        )

    assert asyncio.run(go()) is None


# ───────────────────────────────────────────────────────────────────────
# extract_event_id
# ───────────────────────────────────────────────────────────────────────


def test_extract_event_id_header():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    eid = p.extract_event_id(
        body={}, headers={"x-github-delivery": "abc-123"},
        manifest_block={"event_id_field": "headers.X-GitHub-Delivery"},
    )
    assert eid == "abc-123"


def test_extract_event_id_missing_field_empty():
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    eid = p.extract_event_id(
        body={}, headers={},
        manifest_block={},  # no event_id_field declared
    )
    assert eid == ""


# ───────────────────────────────────────────────────────────────────────
# webhook_providers registry
# ───────────────────────────────────────────────────────────────────────


def test_registry_hardcoded_github():
    from auth import webhook_providers
    p = webhook_providers.get_provider("github")
    assert p.provider_id == "github"


def test_registry_unknown_provider_raises():
    from auth import webhook_providers
    with pytest.raises(KeyError):
        webhook_providers.get_provider("nonexistent-provider-xyz")


def test_registry_lists_seeded_providers():
    from auth import webhook_providers
    ids = webhook_providers.list_provider_ids()
    for p in ("github", "slack", "linear", "microsoft", "zoom"):
        assert p in ids


# ───────────────────────────────────────────────────────────────────────
# Linear signature verification
# ───────────────────────────────────────────────────────────────────────


_LINEAR_SIG_BLOCK = {
    "algorithm": "hmac-sha256",
    "header": "Linear-Signature",
    "prefix": "",
    "version_prefix": "",
    "timestamp_header": "",
    "per_subscription_secret": True,
    "secret_credential_key": "",
}


def test_linear_signature_verify_positive():
    """Linear-Signature is raw hex HMAC-SHA256 of body, no prefix."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    secret = "linear-test-secret-12345"
    body = b'{"action":"create","type":"Issue","data":{"id":"abc"}}'
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    p = GenericWebhookProvider(provider_id="linear")
    result = p.verify_signature(
        raw_body=body,
        headers={"linear-signature": expected},
        signing_secret=secret,
        manifest_sig_block=_LINEAR_SIG_BLOCK,
    )
    assert result.ok, f"reason={result.reason}"


def test_linear_signature_verify_negative_wrong_secret():
    from auth.webhook_providers.generic import GenericWebhookProvider
    secret = "linear-test-secret-12345"
    body = b'{"action":"create"}'
    bad_sig = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    p = GenericWebhookProvider(provider_id="linear")
    result = p.verify_signature(
        raw_body=body,
        headers={"linear-signature": bad_sig},
        signing_secret=secret,
        manifest_sig_block=_LINEAR_SIG_BLOCK,
    )
    assert not result.ok
    assert result.reason == "signature_mismatch"


def test_linear_event_id_extracted_from_header():
    """Linear-Delivery is the dedup key."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="linear")
    event_id = p.extract_event_id(
        body={},
        headers={"linear-delivery": "0e5b4f1c-7a89-4d2c-b6e7-1a2b3c4d5e6f"},
        manifest_block={"event_id_field": "headers.Linear-Delivery"},
    )
    assert event_id == "0e5b4f1c-7a89-4d2c-b6e7-1a2b3c4d5e6f"


# ───────────────────────────────────────────────────────────────────────
# Slack signature + URL verification handshake
# ───────────────────────────────────────────────────────────────────────


_SLACK_SIG_BLOCK = {
    "algorithm": "hmac-sha256",
    "header": "X-Slack-Signature",
    "prefix": "",
    "version_prefix": "v0=",
    "timestamp_header": "X-Slack-Request-Timestamp",
    "timestamp_format": "unix",
    "signed_payload_template": "v0:{timestamp}:{body}",
    "max_age_seconds": 300,
    "per_subscription_secret": False,
    "secret_credential_key": "SLACK_SIGNING_SECRET",
}


def test_slack_signature_verify_positive():
    """Slack: HMAC-SHA256 over `v0:{ts}:{body}`, header value is `v0=<hex>`."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    secret = "slack-test-signing-secret"
    body = b'{"token":"abc","challenge":"xyz","type":"url_verification"}'
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body.decode()}"
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    header_value = f"v0={digest}"
    p = GenericWebhookProvider(provider_id="slack")
    result = p.verify_signature(
        raw_body=body,
        headers={
            "x-slack-signature": header_value,
            "x-slack-request-timestamp": ts,
        },
        signing_secret=secret,
        manifest_sig_block=_SLACK_SIG_BLOCK,
    )
    assert result.ok, f"reason={result.reason}"


def test_slack_signature_replay_too_old():
    """Slack: timestamp older than 5 min must be rejected."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    secret = "slack-test-signing-secret"
    body = b'{"event":{"type":"app_mention"}}'
    old_ts = str(int(time.time()) - 600)  # 10 minutes ago
    base = f"v0:{old_ts}:{body.decode()}"
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    p = GenericWebhookProvider(provider_id="slack")
    result = p.verify_signature(
        raw_body=body,
        headers={
            "x-slack-signature": f"v0={digest}",
            "x-slack-request-timestamp": old_ts,
        },
        signing_secret=secret,
        manifest_sig_block=_SLACK_SIG_BLOCK,
    )
    assert not result.ok
    assert result.reason == "timestamp_too_old"


def test_slack_url_verification_handshake_echoes_challenge():
    """Slack POSTs `{type, challenge}`; provider echoes `{"challenge": value}`."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="slack")
    uv_block = {
        "kind": "slack_challenge",
        "request_field": "challenge",
        "request_source": "body",
        "response_field": "challenge",
        "response_content_type": "application/json",
    }
    result = asyncio.run(p.handle_url_verification(
        request_body={
            "type": "url_verification",
            "token": "abc",
            "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P",
        },
        query_params={},
        manifest_uv_block=uv_block,
        signing_secret="",
    ))
    assert result is not None
    status, body, headers = result
    assert status == 200
    assert "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P" in body
    assert headers.get("content-type") == "application/json"


def test_slack_url_verification_returns_none_on_normal_event():
    """Slack: non-handshake POSTs (regular events) → handler returns None."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="slack")
    result = asyncio.run(p.handle_url_verification(
        request_body={"event": {"type": "app_mention"}},
        query_params={},
        manifest_uv_block={
            "kind": "slack_challenge",
            "request_field": "challenge",
            "request_source": "body",
            "response_field": "challenge",
            "response_content_type": "application/json",
        },
        signing_secret="",
    ))
    assert result is None


# ───────────────────────────────────────────────────────────────────────
# Zoom signature + endpoint validation handshake
# ───────────────────────────────────────────────────────────────────────


_ZOOM_SIG_BLOCK = {
    "algorithm": "hmac-sha256",
    "header": "x-zm-signature",
    "prefix": "",
    "version_prefix": "v0=",
    "timestamp_header": "x-zm-request-timestamp",
    "timestamp_format": "unix",
    "signed_payload_template": "v0:{timestamp}:{body}",
    "max_age_seconds": 300,
    "per_subscription_secret": False,
    "secret_credential_key": "ZOOM_WEBHOOK_SECRET_TOKEN",
}


def test_zoom_signature_verify_positive():
    """Zoom signing matches Slack pattern: HMAC over v0:{ts}:{body}, header v0={hex}."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    secret = "zoom-test-secret"
    body = b'{"event":"meeting.started","payload":{"object":{"id":"12345"}}}'
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body.decode()}"
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    p = GenericWebhookProvider(provider_id="zoom")
    result = p.verify_signature(
        raw_body=body,
        headers={
            "x-zm-signature": f"v0={digest}",
            "x-zm-request-timestamp": ts,
        },
        signing_secret=secret,
        manifest_sig_block=_ZOOM_SIG_BLOCK,
    )
    assert result.ok, f"reason={result.reason}"


def test_zoom_endpoint_validation_handshake_returns_encrypted_token():
    """Zoom POSTs `endpoint.url_validation` with plainToken; respond with
    {plainToken, encryptedToken = HMAC-SHA256(secret, plainToken).hex()}."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="zoom")
    secret = "zoom-test-secret"
    plain = "qgg8vlvZRS6UYooatFL8Aw"
    expected_encrypted = hmac.new(
        secret.encode(), plain.encode(), hashlib.sha256,
    ).hexdigest()
    uv_block = {
        "kind": "zoom_endpoint_validation",
        "request_field": "plainToken",
        "request_source": "body",
        "response_field": "encryptedToken",
        "response_content_type": "application/json",
    }
    result = asyncio.run(p.handle_url_verification(
        request_body={
            "event": "endpoint.url_validation",
            "payload": {"plainToken": plain},
            "event_ts": 1654503849680,
        },
        query_params={},
        manifest_uv_block=uv_block,
        signing_secret=secret,
    ))
    assert result is not None
    status, body, headers = result
    assert status == 200
    assert plain in body
    assert expected_encrypted in body
    assert headers.get("content-type") == "application/json"


# ───────────────────────────────────────────────────────────────────────
# Microsoft clientState echo + batched normalization
# ───────────────────────────────────────────────────────────────────────


def test_microsoft_client_state_verify_positive():
    """All items in value[] must carry the same clientState as the row's secret."""
    import json as _json
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    secret = "row-clientstate-secret"
    body = _json.dumps({
        "value": [
            {"id": "ev1", "clientState": secret, "changeType": "created"},
            {"id": "ev2", "clientState": secret, "changeType": "updated"},
        ]
    }).encode()
    result = p.verify_signature(
        raw_body=body,
        headers={},
        signing_secret=secret,
        manifest_sig_block={"algorithm": "client_state_echo"},
    )
    assert result.ok, f"reason={result.reason}"


def test_microsoft_client_state_verify_negative_one_mismatch_fails_all():
    """A single item with a wrong clientState fails the entire request."""
    import json as _json
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    secret = "row-clientstate-secret"
    body = _json.dumps({
        "value": [
            {"id": "ev1", "clientState": secret},
            {"id": "ev2", "clientState": "stolen-or-stale"},
        ]
    }).encode()
    result = p.verify_signature(
        raw_body=body,
        headers={},
        signing_secret=secret,
        manifest_sig_block={"algorithm": "client_state_echo"},
    )
    assert not result.ok
    assert result.reason == "client_state_mismatch"


def test_microsoft_client_state_verify_negative_empty_value_array():
    """Empty value[] is rejected — there's nothing to verify."""
    import json as _json
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    body = _json.dumps({"value": []}).encode()
    result = p.verify_signature(
        raw_body=body,
        headers={},
        signing_secret="anything",
        manifest_sig_block={"algorithm": "client_state_echo"},
    )
    assert not result.ok
    assert result.reason == "no_value_array"


def test_microsoft_client_state_verify_negative_missing_secret():
    """Empty signing_secret must be rejected — an attacker forging empty
    clientState would otherwise pass."""
    import json as _json
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    body = _json.dumps({
        "value": [{"id": "ev1", "clientState": ""}]
    }).encode()
    result = p.verify_signature(
        raw_body=body,
        headers={},
        signing_secret="",
        manifest_sig_block={"algorithm": "client_state_echo"},
    )
    assert not result.ok
    assert result.reason == "missing_secret"


def test_microsoft_normalize_payload_batch_yields_one_event_per_item():
    """body.value[] with N items → N NormalizedEvents."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    body = {
        "value": [
            {
                "id": "delivery-1",
                "subscriptionId": "sub-uuid",
                "clientState": "x",
                "changeType": "created",
                "resource": "users/u1/messages/m1",
                "resourceData": {
                    "@odata.type": "#Microsoft.Graph.Message",
                    "id": "m1",
                },
            },
            {
                "id": "delivery-2",
                "subscriptionId": "sub-uuid",
                "clientState": "x",
                "changeType": "updated",
                "resource": "users/u1/messages/m2",
                "resourceData": {
                    "@odata.type": "#Microsoft.Graph.Message",
                    "id": "m2",
                },
            },
        ]
    }
    manifest = {
        "payload_normalization": {
            "event_type_path": "body.changeType",
            "subject": {
                "type_path": "body.resourceData.@odata.type",
                "id_path": "body.resourceData.id",
            },
            "target": {"type": "resource", "id_path": "body.resource"},
        }
    }
    events = p.normalize_payload_batch(body=body, headers={}, manifest_block=manifest)
    assert len(events) == 2
    assert events[0].vendor_event_id == "delivery-1"
    assert events[0].event_type == "created"
    assert events[0].subject["id"] == "m1"
    assert events[1].vendor_event_id == "delivery-2"
    assert events[1].event_type == "updated"
    assert events[1].subject["id"] == "m2"


def test_microsoft_event_id_uses_resource_data_id_plus_change():
    """MS Graph has no top-level per-notification id — the changed item's id
    is in resourceData.id. The dedup key must be {resourceData.id}:{changeType}
    so distinct items (and created-vs-updated of one item) don't collide. The
    old {subscriptionId}:{idx} fallback collapsed every single-item delivery to
    {subId}:0, wrongly deduping everything after the first within the ring."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    body = {
        "value": [
            {"subscriptionId": "sub-uuid", "clientState": "x",
             "changeType": "created", "resource": "Users/u/Events/EV1",
             "resourceData": {"id": "EV1"}},
            {"subscriptionId": "sub-uuid", "clientState": "x",
             "changeType": "updated", "resource": "Users/u/Events/EV1",
             "resourceData": {"id": "EV1"}},
            {"subscriptionId": "sub-uuid", "clientState": "x",
             "changeType": "created", "resource": "Users/u/Events/EV2",
             "resourceData": {"id": "EV2"}},
        ]
    }
    events = p.normalize_payload_batch(
        body=body, headers={}, manifest_block={"payload_normalization": {}},
    )
    ids = [e.vendor_event_id for e in events]
    # All three distinct — created/updated of EV1 differ, EV2 differs.
    assert ids == ["EV1:created", "EV1:updated", "EV2:created"]
    assert len(set(ids)) == 3


def test_microsoft_normalize_payload_batch_missing_id_uses_fallback():
    """Items with neither top-level id nor resourceData.id fall back to the
    subscriptionId:idx composite (last resort — at least unique within batch)."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    body = {
        "value": [
            {"subscriptionId": "sub-uuid", "clientState": "x", "changeType": "created"},
        ]
    }
    events = p.normalize_payload_batch(
        body=body, headers={}, manifest_block={"payload_normalization": {}},
    )
    assert len(events) == 1
    assert events[0].vendor_event_id == "sub-uuid:0"


_MS_CATALOG = [
    {"key": "mail_inbox", "label": "Mail", "resource_contains": "/messages"},
    {"key": "calendar_events", "label": "Cal", "resource_contains": "/events"},
    {"key": "drive_root", "label": "Drive", "resource_contains": "/drive"},
    {"key": "contacts", "label": "Contacts", "resource_contains": "/contacts"},
]


def test_microsoft_batch_canonicalizes_event_type_via_resource_contains():
    """Graph items carry changeType ("created"/"updated") as the raw type,
    but the subscription gate + trigger event_filters speak catalog keys
    ("calendar_events"). Entries with `resource_contains` map each item to
    its key per item — the request-level match mechanism can't see item
    context in a batched body."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    manifest = {
        "event_catalog": _MS_CATALOG,
        "payload_normalization": {"event_type_path": "body.changeType"},
    }
    body = {"value": [
        # Graph mixes casing in echoed resource strings — match must be
        # case-insensitive.
        {"id": "d1", "changeType": "created",
         "resource": "Users/abc/Events/AAA="},
        {"id": "d2", "changeType": "updated",
         "resource": "Users/abc/mailFolders('Inbox')/Messages/M1"},
        {"id": "d3", "changeType": "updated",
         "resource": "users/abc/drive/root"},
        {"id": "d4", "changeType": "created",
         "resource": "Users/abc/Contacts/C1"},
    ]}
    events = p.normalize_payload_batch(body=body, headers={}, manifest_block=manifest)
    assert [e.event_type for e in events] == [
        "calendar_events", "mail_inbox", "drive_root", "contacts",
    ]


def test_microsoft_batch_keeps_raw_type_when_no_resource_match():
    """Unknown resource (or missing resource) keeps the raw changeType —
    the dispatcher's selected-events gate then ignores it (conservative:
    never invent a catalog key)."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    manifest = {
        "event_catalog": _MS_CATALOG,
        "payload_normalization": {"event_type_path": "body.changeType"},
    }
    body = {"value": [
        {"id": "d1", "changeType": "created",
         "resource": "users/abc/todo/lists/L1"},   # not in the catalog
        {"id": "d2", "changeType": "updated"},      # no resource at all
    ]}
    events = p.normalize_payload_batch(body=body, headers={}, manifest_block=manifest)
    assert [e.event_type for e in events] == ["created", "updated"]


def test_microsoft_url_verification_handshake_echoes_validation_token_plain_text():
    """MS Graph GETs / POSTs with ?validationToken=xyz — reply with the
    raw token as text/plain."""
    from auth.webhook_providers.microsoft import MicrosoftWebhookProvider
    p = MicrosoftWebhookProvider()
    uv_block = {
        "kind": "ms_graph_validation_token",
        "request_field": "validationToken",
        "request_source": "query",
        "response_field": "plain_text",
        "response_content_type": "text/plain",
    }
    result = asyncio.run(p.handle_url_verification(
        request_body={},
        query_params={"validationToken": "abc-123-opaque"},
        manifest_uv_block=uv_block,
        signing_secret="",
    ))
    assert result is not None
    status, body, headers = result
    assert status == 200
    assert body == "abc-123-opaque"
    assert headers.get("content-type") == "text/plain"


# ───────────────────────────────────────────────────────────────────────
# Generic provider rejects `client_state_echo` (forces subclass override)
# ───────────────────────────────────────────────────────────────────────


def test_generic_provider_rejects_client_state_echo_algorithm():
    """The generic provider doesn't know how to verify clientState — only the
    Microsoft override does. Manifest authors who declare client_state_echo
    must also have a Python subclass registered for that provider_id."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="hypothetical")
    result = p.verify_signature(
        raw_body=b"{}",
        headers={"x-sig": "abc"},
        signing_secret="secret",
        manifest_sig_block={
            "algorithm": "client_state_echo",
            "header": "X-Sig",
        },
    )
    assert not result.ok
    assert result.reason == "unsupported_algorithm"


# ───────────────────────────────────────────────────────────────────────
# normalize_payload_batch default (single-event vendors)
# ───────────────────────────────────────────────────────────────────────


def test_normalize_payload_batch_default_wraps_singular_in_list():
    """Single-event vendors (GitHub, Slack, Linear, Zoom) inherit the
    ABC default: batch returns a 1-element list wrapping normalize_payload."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    p = GenericWebhookProvider(provider_id="github")
    body = {"action": "opened", "pull_request": {"number": 42}}
    manifest = {
        "payload_normalization": {
            "event_type_path": "headers.X-GitHub-Event",
            "subject": {"id_path": "body.pull_request.number"},
        },
        "event_id_field": "headers.X-GitHub-Delivery",
    }
    events = p.normalize_payload_batch(
        body=body,
        headers={"x-github-event": "pull_request", "x-github-delivery": "d-1"},
        manifest_block=manifest,
    )
    assert len(events) == 1
    assert events[0].event_type == "pull_request"
    assert events[0].vendor_event_id == "d-1"


# ───────────────────────────────────────────────────────────────────────
# _resolve_signing_secret — platform-wide secrets read the app_credential
# bundle (where the admin form stores them), not the MCP name
# ───────────────────────────────────────────────────────────────────────


def test_platform_signing_secret_reads_app_credential_slug(monkeypatch):
    """Slack's signing secret is saved by the admin form under the
    manifest's `app_credential` slug (slack-oauth-app) — the dispatcher
    must read THAT bundle (mcp-name fallback for oauth-less manifests)."""
    from types import SimpleNamespace
    from services.mcp import mcp_registry
    from services.webhooks import webhook_dispatcher
    from storage import credential_store

    manifest = SimpleNamespace(credentials=SimpleNamespace(
        oauth={"app_credential": "slack-oauth-app"},
    ))
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: manifest)

    bundles = {
        "slack-oauth-app": {"SLACK_SIGNING_SECRET": "shhh-form-stored"},
        "slack-mcp": {},
    }
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: bundles.get(slug, {}),
    )

    secret = webhook_dispatcher._resolve_signing_secret(
        webhooks_block={"signature": {
            "per_subscription_secret": False,
            "secret_credential_key": "SLACK_SIGNING_SECRET",
        }},
        row={"id": "sub-1", "mcp_name": "slack-mcp"},
    )
    assert secret == "shhh-form-stored"


def test_platform_signing_secret_falls_back_to_mcp_name(monkeypatch):
    """Manifests without an oauth block (or with the secret stored under
    the legacy mcp-name bundle) still resolve."""
    from types import SimpleNamespace
    from services.mcp import mcp_registry
    from services.webhooks import webhook_dispatcher
    from storage import credential_store

    manifest = SimpleNamespace(credentials=SimpleNamespace(oauth={}))
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda name: manifest)
    monkeypatch.setattr(
        credential_store, "get_infra_credentials",
        lambda slug: {"K": "legacy"} if slug == "some-mcp" else {},
    )

    secret = webhook_dispatcher._resolve_signing_secret(
        webhooks_block={"signature": {
            "per_subscription_secret": False,
            "secret_credential_key": "K",
        }},
        row={"id": "sub-2", "mcp_name": "some-mcp"},
    )
    assert secret == "legacy"
