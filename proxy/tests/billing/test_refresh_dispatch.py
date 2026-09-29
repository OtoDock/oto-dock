"""The rotation chokepoint after phase 3c-ii: the vendor call is the ENGINE's
(``ExecutionLayer.refresh_oauth``), the policy around it is the pool's.

Pinned here: the pool persists the adapter's record with the platform's own
keys carried across the rotation; a failure is classified from the
adapter's status and body exactly as before; and an engine that cannot
refresh — unregistered, no login, or an adapter that raises — is a TRANSIENT
failure, never a dead grant (an OAuth row can sit on any layer today, and no
migration removes it).
"""

import time
from unittest.mock import patch

from core.execution_layer import OAuthRefresh
from services.engines import subscription_pool as pool

_REFRESH = "core.layers.cli.layer.CLIExecutionLayer.refresh_oauth"


def _row(sub_id="sub-1", layer="claude-code-cli", provider="anthropic"):
    return {"id": sub_id, "layer": layer, "provider": provider, "auth_type": "oauth"}


def _clean():
    pool._session_subscriptions.clear()
    pool._refresh_backoff.clear()
    pool._auth_fail_streaks.clear()
    pool._throttled_until.clear()


class TestPersistedRecord:
    def setup_method(self):
        _clean()

    def _refresh(self, stored, out):
        writes = []
        with patch.object(pool.subscription_store, "get_credential_data", return_value=stored), \
             patch.object(pool.subscription_store, "update_credential_data",
                          side_effect=lambda _sid, cred: writes.append(cred)), \
             patch(_REFRESH, return_value=out):
            got = pool._refresh_oauth_token(_row(), "r1")
        return got, writes

    def test_platform_keys_survive_the_rotation(self):
        # The adapter knows the vendor shape; the grant expiry, the health
        # stamps and the account uuid are the platform's and carry forward.
        stored = {"oauth_token": {
            "accessToken": "old", "refreshToken": "r1", "expiresAt": 1,
            "refreshTokenExpiresAt": 123_456, "healthAlerts": {"72h": {"expiry": 1, "at": 2}},
            "accountUuid": "acct-uuid",
        }, "other": "kept"}
        out = OAuthRefresh(oauth_token={"accessToken": "new", "refreshToken": "r2",
                                        "expiresAt": 999, "subscriptionType": "max"})
        got, writes = self._refresh(stored, out)
        assert got == ("new", False)
        assert len(writes) == 1
        tok = writes[0]["oauth_token"]
        assert tok["accessToken"] == "new" and tok["refreshToken"] == "r2"
        assert tok["subscriptionType"] == "max"
        assert tok["refreshTokenExpiresAt"] == 123_456          # carried
        assert tok["healthAlerts"] == {"72h": {"expiry": 1, "at": 2}}
        assert tok["accountUuid"] == "acct-uuid"                 # was dropped before 3c-ii
        assert writes[0]["other"] == "kept"

    def test_a_reported_grant_lifetime_recomputes_the_expiry(self):
        stored = {"oauth_token": {"accessToken": "old", "refreshTokenExpiresAt": 5}}
        out = OAuthRefresh(oauth_token={"accessToken": "new", "refreshToken": "r2", "expiresAt": 9},
                           refresh_token_expires_in=3600)
        _, writes = self._refresh(stored, out)
        assert abs(writes[0]["oauth_token"]["refreshTokenExpiresAt"] - (time.time() + 3600) * 1000) < 5000

    def test_extra_keys_replace_their_stored_twins(self):
        # Codex: the login blob's tokens follow the rotation.
        stored = {"oauth_token": {"accessToken": "old"}, "codex_auth_blob": {"tokens": {"access_token": "old"}}}
        out = OAuthRefresh(oauth_token={"accessToken": "new", "refreshToken": "r2", "expiresAt": 9},
                           extra={"codex_auth_blob": {"tokens": {"access_token": "new"}}})
        with patch.object(pool.subscription_store, "get_credential_data", return_value=stored), \
             patch.object(pool.subscription_store, "update_credential_data") as upd, \
             patch("core.layers.codex.layer.CodexCLIExecutionLayer.refresh_oauth", return_value=out):
            assert pool._refresh_oauth_token(_row(layer="codex-cli", provider="openai"), "r1") == ("new", False)
        assert upd.call_args.args[1]["codex_auth_blob"] == {"tokens": {"access_token": "new"}}


class TestFailures:
    def setup_method(self):
        _clean()

    def _fail(self, out, row=None):
        with patch.object(pool.subscription_store, "get_credential_data",
                          return_value={"oauth_token": {"accessToken": "old"}}), \
             patch.object(pool.subscription_store, "update_credential_data") as upd, \
             patch(_REFRESH, return_value=out):
            got = pool._refresh_oauth_token(row or _row(), "r1")
        upd.assert_not_called()
        return got

    def test_invalid_grant_is_terminal(self):
        assert self._fail(OAuthRefresh(status=400, body={"error": "invalid_grant"})) == (None, True)
        assert self._fail(OAuthRefresh(status=401, body={"error": {"type": "invalid_grant"}})) == (None, True)

    def test_everything_else_is_transient(self):
        assert self._fail(OAuthRefresh(status=500, body=None)) == (None, False)
        assert self._fail(OAuthRefresh(status=400, body={"error": "invalid_scope"})) == (None, False)
        assert self._fail(OAuthRefresh(error="connection reset")) == (None, False)

    def test_a_401_with_a_structured_error_counts_toward_the_streak(self):
        self._fail(OAuthRefresh(status=401, body={"error": {"type": "invalid_request_error"}}))
        assert pool._auth_fail_streaks["sub-1"][1] == 1
        self._fail(OAuthRefresh(status=401, body=None))          # a WAF page: not auth-shaped
        assert pool._auth_fail_streaks["sub-1"][1] == 1

    def test_a_token_endpoint_429_rests_the_account_briefly(self):
        self._fail(OAuthRefresh(status=429, body={"error": "rate_limited"}))
        assert pool._is_throttled("sub-1")

    def test_an_engine_that_cannot_refresh_is_transient_not_terminal(self):
        with patch.object(pool.subscription_store, "get_credential_data",
                          return_value={"oauth_token": {"accessToken": "old"}}), \
             patch.object(pool.subscription_store, "update_credential_data") as upd:
            # No login on this engine (default raises NotImplementedError).
            assert pool._refresh_oauth_token(_row(layer="direct-llm", provider="anthropic"), "r1") == (None, False)
            # Not registered at all.
            assert pool._refresh_oauth_token(_row(layer="acme-cli"), "r1") == (None, False)
        upd.assert_not_called()
        assert "sub-1" not in pool._auth_fail_streaks

    def test_an_adapter_exception_is_transient(self):
        with patch.object(pool.subscription_store, "get_credential_data",
                          return_value={"oauth_token": {"accessToken": "old"}}), \
             patch.object(pool.subscription_store, "update_credential_data") as upd, \
             patch(_REFRESH, side_effect=RuntimeError("vendor bug")):
            assert pool._refresh_oauth_token(_row(), "r1") == (None, False)
        upd.assert_not_called()
