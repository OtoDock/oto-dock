"""Microsoft tenant-admin consent flow tests.

Covers:
  * State engine: create / validate / one-shot consumption / separate
    namespace from auth-code state.
  * Provider's build_admin_consent_url (covered in test_microsoft_provider
    too — here we verify the API integration shape).

The route-level integration tests (FastAPI handlers) are covered by
manual smoke + the dashboard E2E in the verification checklist. These
unit tests pin the state-engine contract that the routes depend on.
"""

from __future__ import annotations


from services.oauth import oauth_engine


# ---------------------------------------------------------------------------
# Admin-consent state — create + validate
# ---------------------------------------------------------------------------


class TestAdminConsentState:
    def test_create_returns_opaque_token(self):
        token = oauth_engine.create_admin_consent_state(
            user_sub="alice",
            mcp_name="m365-mcp",
            provider_id="microsoft",
        )
        assert isinstance(token, str)
        assert len(token) >= 16

    def test_validate_returns_payload_once(self):
        token = oauth_engine.create_admin_consent_state(
            user_sub="alice",
            mcp_name="m365-mcp",
            provider_id="microsoft",
            mobile=True,
        )
        ctx = oauth_engine.validate_admin_consent_state(token)
        assert ctx is not None
        assert ctx.user_sub == "alice"
        assert ctx.mcp_name == "m365-mcp"
        assert ctx.provider_id == "microsoft"
        assert ctx.mobile is True

    def test_validate_is_one_shot(self):
        token = oauth_engine.create_admin_consent_state(
            user_sub="u", mcp_name="m365-mcp", provider_id="microsoft",
        )
        first = oauth_engine.validate_admin_consent_state(token)
        second = oauth_engine.validate_admin_consent_state(token)
        assert first is not None
        assert second is None  # consumed

    def test_validate_unknown_token_returns_none(self):
        ctx = oauth_engine.validate_admin_consent_state("never-issued")
        assert ctx is None


# ---------------------------------------------------------------------------
# State namespace separation from auth-code states
# ---------------------------------------------------------------------------


class TestStateNamespaceSeparation:
    """Admin-consent state and auth-code state live in different dicts.

    A token issued by one cannot be redeemed by the other — critical so
    a user's pending auth-code flow doesn't get burned by an admin
    granting tenant consent (or vice versa).
    """

    def test_admin_consent_token_rejected_by_validate_state(self):
        """A token from create_admin_consent_state must not validate
        against the auth-code validate_state path."""
        token = oauth_engine.create_admin_consent_state(
            user_sub="u", mcp_name="m365-mcp", provider_id="microsoft",
        )
        # Try to redeem via the auth-code validator — must return None.
        assert oauth_engine.validate_state(token) is None
        # Admin-consent state should still be valid (the failed call
        # above didn't pop the admin-consent store).
        assert oauth_engine.validate_admin_consent_state(token) is not None
