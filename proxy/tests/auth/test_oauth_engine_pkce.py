"""PKCE flow plumbing in oauth_engine.

Verifier round-trip via state, S256 challenge derivation, manifest opt-in
gating, and end-to-end exchange-code passthrough.
"""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import patch, MagicMock


from services.oauth import oauth_engine


def _make_manifest(flow: str = "authorization_code"):
    """Build a minimal manifest stub the engine reads `flows[0]` from."""
    m = MagicMock()
    m.credentials.oauth = {
        "provider_id": "linear",
        "flows": [flow],
        "authorization_url": "https://x/auth",
        "token_url": "https://x/token",
    }
    return m


def test_pkce_verifier_set_on_state_when_manifest_opts_in():
    """When manifest declares authorization_code_pkce, create_state mints
    a verifier and stores it on the state."""
    with patch(
        "services.mcp.mcp_registry.get_manifest",
        return_value=_make_manifest("authorization_code_pkce"),
    ):
        token = oauth_engine.create_state(
            user_sub="alice",
            mcp_name="linear-mcp",
            provider_id="linear",
            redirect_uri="https://x/cb",
        )
    state = oauth_engine.validate_state(token)
    assert state is not None
    assert state.code_verifier, "PKCE flow should populate code_verifier"
    # Verifier is base64url-encoded 48 bytes → ~64 chars without padding
    assert 43 <= len(state.code_verifier) <= 128


def test_no_pkce_verifier_when_flow_is_authorization_code():
    """Plain authorization_code flow leaves code_verifier empty."""
    with patch(
        "services.mcp.mcp_registry.get_manifest",
        return_value=_make_manifest("authorization_code"),
    ):
        token = oauth_engine.create_state(
            user_sub="alice",
            mcp_name="linear-mcp",
            provider_id="linear",
        )
    state = oauth_engine.validate_state(token)
    assert state is not None
    assert state.code_verifier == ""


def test_pkce_challenge_is_s256_of_verifier():
    """The challenge sent to the provider must be SHA256(verifier) base64url."""
    with patch(
        "services.mcp.mcp_registry.get_manifest",
        return_value=_make_manifest("authorization_code_pkce"),
    ):
        token = oauth_engine.create_state(
            user_sub="alice",
            mcp_name="linear-mcp",
            provider_id="linear",
        )
    state = oauth_engine.validate_state(token)
    assert state is not None

    expected_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(state.code_verifier.encode("ascii")).digest(),
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert state.extra.get("code_challenge") == expected_challenge
    assert state.extra.get("code_challenge_method") == "S256"


def test_pkce_verifier_recovered_only_once_via_one_shot_state():
    """State token is one-shot, so the verifier is also one-shot — second
    validate_state returns None (CSRF + replay defense)."""
    with patch(
        "services.mcp.mcp_registry.get_manifest",
        return_value=_make_manifest("authorization_code_pkce"),
    ):
        token = oauth_engine.create_state(
            user_sub="u",
            mcp_name="linear-mcp",
            provider_id="linear",
        )
    first = oauth_engine.validate_state(token)
    second = oauth_engine.validate_state(token)
    assert first is not None and first.code_verifier
    assert second is None


def test_pkce_verifier_is_unique_per_state():
    """Two state tokens should NEVER share the same verifier."""
    with patch(
        "services.mcp.mcp_registry.get_manifest",
        return_value=_make_manifest("authorization_code_pkce"),
    ):
        t1 = oauth_engine.create_state(
            user_sub="a", mcp_name="linear-mcp", provider_id="linear",
        )
        t2 = oauth_engine.create_state(
            user_sub="b", mcp_name="linear-mcp", provider_id="linear",
        )
    s1 = oauth_engine.validate_state(t1)
    s2 = oauth_engine.validate_state(t2)
    assert s1 is not None and s2 is not None
    assert s1.code_verifier != s2.code_verifier
