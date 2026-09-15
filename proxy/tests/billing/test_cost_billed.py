"""``subscription_pool.session_cost_billed`` — the credential kind behind the
chat's cost display. A subscription (oauth) or a local model (local_endpoint)
answers False (the chat hides the cost); an API key or the hosted relay
answers True; anything the pool cannot resolve answers True (show — today's
behaviour). Recording is not this function's business and never consults it.
"""

from unittest.mock import patch

import pytest


def _reset(sp):
    sp._session_subscriptions.clear()
    sp._session_binding_ctx.clear()
    sp._session_scope_keys.clear()


class TestSessionCostBilled:
    @pytest.mark.parametrize("auth_type,billed", [
        ("oauth", False),
        ("local_endpoint", False),
        ("api_key", True),
        ("relay", True),
        ("some-future-kind", True),  # unknown type keeps showing
    ])
    @patch("services.engines.subscription_pool.subscription_store")
    def test_auth_type_decides(self, mock_store, auth_type, billed):
        from services.engines import subscription_pool as sp
        _reset(sp)
        sp._session_subscriptions["sess-x"] = "sub-x"
        mock_store.get_subscription.return_value = {"id": "sub-x", "auth_type": auth_type}
        try:
            assert sp.session_cost_billed("sess-x") is billed
            mock_store.get_subscription.assert_called_once_with("sub-x")
        finally:
            _reset(sp)

    @patch("services.engines.subscription_pool.subscription_store")
    def test_unbound_session_is_billed(self, mock_store):
        # No live binding and no persisted row: pool-external credentials —
        # the kind is unknowable, the cost stays visible.
        from services.engines import subscription_pool as sp
        _reset(sp)
        mock_store.get_session_binding.return_value = None
        assert sp.session_cost_billed("sess-nobody") is True
        mock_store.get_subscription.assert_not_called()

    @patch("services.engines.subscription_pool.subscription_store")
    def test_reads_through_the_persisted_binding(self, mock_store):
        # A session that outlived a proxy restart (satellite, re-adopted chat)
        # only has the persisted row — same read-through as attribution.
        from services.engines import subscription_pool as sp
        _reset(sp)
        mock_store.get_session_binding.return_value = {"subscription_id": "sub-p"}
        mock_store.get_subscription.return_value = {"id": "sub-p", "auth_type": "oauth"}
        assert sp.session_cost_billed("sess-restarted") is False

    @patch("services.engines.subscription_pool.subscription_store")
    def test_missing_row_or_store_error_is_billed(self, mock_store):
        from services.engines import subscription_pool as sp
        _reset(sp)
        sp._session_subscriptions["sess-x"] = "sub-gone"
        try:
            mock_store.get_subscription.return_value = None
            assert sp.session_cost_billed("sess-x") is True
            mock_store.get_subscription.side_effect = RuntimeError("db down")
            assert sp.session_cost_billed("sess-x") is True
        finally:
            _reset(sp)

    def test_real_store_roundtrip(self, temp_db):
        """Bind → hidden; release deletes the binding → shown again (which is
        why the flag must ride the persisted turn row, not a chat-level read)."""
        from services.engines import subscription_pool as sp
        from storage.billing import subscription_store as store
        _reset(sp)
        sub = store.add_subscription(
            layer="direct-llm", provider="ollama", auth_type="local_endpoint",
            owner_sub="user-admin", label="local",
            credential_data={"endpoint_url": "http://localhost:11434"},
        )
        try:
            sp.bind_session("sess-local", sub["id"], layer="direct-llm", user_sub="user-admin")
            assert sp.session_cost_billed("sess-local") is False
            sp.release_subscription("sess-local")
            assert sp.session_cost_billed("sess-local") is True
        finally:
            _reset(sp)
