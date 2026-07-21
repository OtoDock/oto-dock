"""Per-session token-expiry tracking + rotation fan-out plumbing.

Sessions authenticate from an on-disk credential file (never env); the pool is
the sole rotator and fans every rotation out to live sessions' files. These
tests pin the audit trail that lets the turn-start guard and the freshness
worker see the REAL runway of the token a session's file holds — including the
fail-soft case (refresh failed → aging stored token handed out), which is how
the 2026-07-06 mid-turn 401s hid from every full-runway assumption — and the
fan-out chokepoint that keeps live sessions alive across a rotation.
"""

import time
from unittest.mock import patch

from services.engines import subscription_pool as pool
from services.engines import token_fanout


def _clean():
    pool._issued_token_expiry.clear()
    pool._session_token_expiry.clear()
    pool._session_subscriptions.clear()
    pool._refresh_backoff.clear()
    token_fanout._targets.clear()


class TestBindSnapshot:
    def setup_method(self):
        _clean()

    def test_bind_snapshots_issued_expiry(self):
        pool._issued_token_expiry["sub-1"] = 12345
        pool.bind_session("sess-1", "sub-1")
        assert pool.session_token_expiry_ms("sess-1") == 12345

    def test_bind_without_issued_entry_clears_stale_snapshot(self):
        pool._session_token_expiry["sess-1"] = 999
        pool.bind_session("sess-1", "sub-apikey")
        assert pool.session_token_expiry_ms("sess-1") is None

    def test_release_pops_snapshot_and_fanout_target(self):
        pool._issued_token_expiry["sub-1"] = 12345
        pool.bind_session("sess-1", "sub-1")
        token_fanout.register_session_target(
            "sess-1", token_fanout.CredentialFileTarget(kind="claude", host_dir="/x"),
        )
        with patch.object(pool.subscription_store, "decrement_active_sessions"):
            pool.release_subscription("sess-1")
        assert pool.session_token_expiry_ms("sess-1") is None
        assert token_fanout.session_target("sess-1") is None

    def test_unknown_session_is_none(self):
        assert pool.session_token_expiry_ms("nope") is None

    def test_bound_oauth_subscription_ids_skips_non_expiring(self):
        pool._issued_token_expiry["sub-oauth"] = 12345
        pool.bind_session("sess-1", "sub-oauth")
        pool.bind_session("sess-2", "sub-apikey")  # no expiry stamp
        assert pool.bound_oauth_subscription_ids() == {"sub-oauth"}


class TestResolveReportsExpiry:
    def setup_method(self):
        _clean()

    def test_fresh_token_reports_stored_expiry(self):
        exp = int(time.time() * 1000) + 8 * 3600 * 1000
        tok, got = pool._resolve_oauth_access_token(
            {"id": "sub-fresh", "provider": "anthropic"},
            {"accessToken": "tok", "refreshToken": "r", "expiresAt": exp},
        )
        assert tok == "tok"
        assert got == exp

    def test_failsoft_reports_the_aging_stored_expiry(self):
        # Runway below the spawn-refresh threshold → wants refresh; refresh
        # FAILS → the still-usable stored token is handed out. The reported
        # expiry must be the stored token's own (short!) expiry — not 0, not
        # a full-runway assumption.
        exp = int(time.time() * 1000) + 90 * 60 * 1000  # 1h30m left
        cred = {"oauth_token": {"accessToken": "tok", "refreshToken": "r", "expiresAt": exp}}
        with patch.object(pool.subscription_store, "get_credential_data", return_value=cred), \
             patch.object(pool, "_refresh_oauth_token", return_value=None):
            tok, got = pool._resolve_oauth_access_token(
                {"id": "sub-failsoft", "provider": "anthropic"}, cred["oauth_token"],
            )
        assert tok == "tok"
        assert got == exp

    def test_successful_refresh_reports_the_new_expiry(self):
        old_exp = int(time.time() * 1000) + 60 * 60 * 1000
        new_exp = int(time.time() * 1000) + 8 * 3600 * 1000
        before = {"oauth_token": {"accessToken": "tok", "refreshToken": "r", "expiresAt": old_exp}}
        after = {"oauth_token": {"accessToken": "new", "refreshToken": "r2", "expiresAt": new_exp}}
        # First read: under-lock re-check; second read: post-refresh expiry.
        with patch.object(pool.subscription_store, "get_credential_data",
                          side_effect=[before, after]), \
             patch.object(pool, "_refresh_oauth_token", return_value="new"):
            tok, got = pool._resolve_oauth_access_token(
                {"id": "sub-refresh", "provider": "anthropic"}, before["oauth_token"],
            )
        assert tok == "new"
        assert got == new_exp

    def test_credential_without_expiry_reports_zero(self):
        tok, got = pool._resolve_oauth_access_token(
            {"id": "sub-noexp"}, {"accessToken": "tok"},
        )
        assert tok == "tok"
        assert got == 0

    def test_min_runway_override_skips_spawn_threshold(self):
        # 3 h of runway: below the 2 h spawn threshold? No — above it, but the
        # point is the 45-min override must NOT refresh a 3 h token.
        exp = int(time.time() * 1000) + 3 * 3600 * 1000
        with patch.object(pool, "_refresh_oauth_token") as refresh:
            tok, got = pool._resolve_oauth_access_token(
                {"id": "sub-turn", "provider": "anthropic"},
                {"accessToken": "tok", "refreshToken": "r", "expiresAt": exp},
                min_runway_ms=pool.TURN_MIN_TOKEN_RUNWAY_MS,
            )
        refresh.assert_not_called()
        assert tok == "tok"
        assert got == exp


class TestRefreshPreservesPlanTier:
    """The Anthropic token-refresh response does not echo subscriptionType /
    rateLimitTier, and the Claude Code TUI gates plan-included models (Fable 5
    on Max) on that field in .credentials.json — so a refresh must preserve the
    stored values, and backfill them from the profile endpoint when empty."""

    def setup_method(self):
        _clean()

    _REFRESH_RESP = {"access_token": "new", "refresh_token": "r2", "expires_in": 28800}

    def _run_refresh(self, stored_token: dict, profile_fields=("", "")):
        import httpx
        updates: list[dict] = []
        resp = type("R", (), {"status_code": 200, "json": lambda self: dict(self._d)})()
        resp._d = self._REFRESH_RESP
        with patch.object(httpx, "post", return_value=resp), \
             patch.object(pool.subscription_store, "get_credential_data",
                          return_value={"oauth_token": stored_token}), \
             patch.object(pool.subscription_store, "update_credential_data",
                          side_effect=lambda _sid, cred: updates.append(cred)), \
             patch.object(pool, "fetch_anthropic_subscription_fields",
                          return_value=profile_fields) as fetch:
            got = pool._refresh_anthropic_oauth_token("sub-tier", "r")
        assert got == "new"
        assert len(updates) == 1
        return updates[0]["oauth_token"], fetch

    def test_refresh_preserves_stored_plan_tier(self):
        tok, fetch = self._run_refresh(
            {"accessToken": "old", "refreshToken": "r", "expiresAt": 1,
             "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"},
        )
        assert tok["subscriptionType"] == "max"
        assert tok["rateLimitTier"] == "default_claude_max_20x"
        fetch.assert_not_called()

    def test_refresh_backfills_empty_tier_from_profile(self):
        tok, fetch = self._run_refresh(
            {"accessToken": "old", "refreshToken": "r", "expiresAt": 1,
             "subscriptionType": "", "rateLimitTier": ""},
            profile_fields=("max", "default_claude_max_20x"),
        )
        assert tok["subscriptionType"] == "max"
        assert tok["rateLimitTier"] == "default_claude_max_20x"
        fetch.assert_called_once_with("new")

    def test_refresh_backfill_failure_stays_empty(self):
        tok, _ = self._run_refresh(
            {"accessToken": "old", "refreshToken": "r", "expiresAt": 1},
            profile_fields=("", ""),
        )
        assert tok["subscriptionType"] == ""
        assert tok["rateLimitTier"] == ""

    def test_derive_subscription_fields_mapping(self):
        assert pool._derive_subscription_fields(
            {"account": {"has_claude_max": True},
             "organization": {"rate_limit_tier": "default_claude_max_20x"}},
        ) == ("max", "default_claude_max_20x")
        assert pool._derive_subscription_fields(
            {"account": {"has_claude_pro": True}, "organization": {}},
        ) == ("pro", "")
        assert pool._derive_subscription_fields(
            {"account": {}, "organization": {"organization_type": "claude_enterprise"}},
        ) == ("enterprise", "")
        assert pool._derive_subscription_fields({}) == ("", "")


class TestIssueStamping:
    def setup_method(self):
        _clean()

    def _handle(self, exp_ms):
        return pool.SubscriptionHandle(
            subscription_id="sub-9", layer="claude-code-cli", provider="anthropic",
            auth_type="oauth", api_key=None, oauth_access_token="tok",
            endpoint_url=None, oauth_expires_at_ms=exp_ms,
            claude_creds_blob=(
                pool._claude_file_blob({"accessToken": "tok", "expiresAt": exp_ms})
                if exp_ms else None
            ),
        )

    def test_resolve_env_stamps_issued_expiry_and_emits_file_blob(self):
        import json
        with patch.object(pool, "acquire_subscription", return_value=self._handle(777)):
            sub_id, env = pool.resolve_subscription_env("claude-code-cli", None)
        assert sub_id == "sub-9"
        # OAuth never rides env — the layer writes the blob to
        # .credentials.json (env is frozen at exec + outranks the file).
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        blob = json.loads(env["_CLAUDE_CREDS_BLOB"])
        assert blob["accessToken"] == "tok"
        assert blob["refreshToken"] == ""  # pool = sole rotator
        assert blob["expiresAt"] == 777
        assert pool._issued_token_expiry["sub-9"] == 777

    def test_resolve_env_clears_stamp_for_non_expiring_credential(self):
        pool._issued_token_expiry["sub-9"] = 111
        with patch.object(pool, "acquire_subscription", return_value=self._handle(0)):
            pool.resolve_subscription_env("claude-code-cli", None)
        assert "sub-9" not in pool._issued_token_expiry


class TestClaudeFileBlob:
    def test_blob_shape_and_neutralized_refresh(self):
        blob = pool._claude_file_blob({
            "accessToken": "at", "refreshToken": "SECRET", "expiresAt": 42,
            "scopes": ["user:inference"], "subscriptionType": "max",
            "rateLimitTier": "tier5",
        })
        assert blob == {
            "accessToken": "at",
            "refreshToken": "",
            "expiresAt": 42,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
            "rateLimitTier": "tier5",
        }


class TestFanOutOnRotation:
    def setup_method(self):
        _clean()

    def test_rotation_fans_out_and_advances_snapshots(self, tmp_path):
        exp = int(time.time() * 1000) + 8 * 3600 * 1000
        pool.bind_session("sess-a", "sub-r")
        pool.bind_session("sess-b", "sub-r")
        pool._session_token_expiry["sess-a"] = 1
        pool._session_token_expiry["sess-b"] = 1
        token_fanout.register_session_target(
            "sess-a",
            token_fanout.CredentialFileTarget(kind="claude", host_dir=str(tmp_path)),
        )
        # sess-b has no target (e.g. spawned before the last proxy restart) —
        # its snapshot must NOT advance.
        cred = {"oauth_token": {
            "accessToken": "rotated", "refreshToken": "r2", "expiresAt": exp,
        }}
        with patch.object(pool.subscription_store, "get_credential_data",
                          return_value=cred), \
             patch.object(pool, "_refresh_anthropic_oauth_token",
                          return_value="rotated"):
            assert pool._refresh_oauth_token("sub-r", "r1") == "rotated"
        import json
        written = json.loads((tmp_path / ".credentials.json").read_text())
        assert written["claudeAiOauth"]["accessToken"] == "rotated"
        assert written["claudeAiOauth"]["refreshToken"] == ""
        assert pool._session_token_expiry["sess-a"] == exp
        assert pool._session_token_expiry["sess-b"] == 1

    def test_fanout_failure_never_fails_the_refresh(self):
        pool.bind_session("sess-a", "sub-r")
        with patch.object(pool.subscription_store, "get_credential_data",
                          side_effect=RuntimeError("db down")), \
             patch.object(pool, "_refresh_anthropic_oauth_token",
                          return_value="rotated"):
            assert pool._refresh_oauth_token("sub-r", "r1") == "rotated"

    def test_rotation_without_bound_sessions_skips_fanout(self):
        with patch.object(pool, "_refresh_anthropic_oauth_token",
                          return_value="rotated"), \
             patch.object(pool.subscription_store, "get_credential_data") as read:
            assert pool._refresh_oauth_token("sub-lonely", "r1") == "rotated"
        read.assert_not_called()


class TestEnsureFreshAndFanOut:
    def setup_method(self):
        _clean()

    def _sub(self):
        return {"id": "sub-e", "provider": "anthropic", "layer": "claude-code-cli",
                "auth_type": "oauth"}

    def test_fresh_token_is_a_noop(self):
        exp = int(time.time() * 1000) + 3 * 3600 * 1000
        cred = {"oauth_token": {"accessToken": "t", "refreshToken": "r", "expiresAt": exp}}
        with patch.object(pool.subscription_store, "get_subscription",
                          return_value=self._sub()), \
             patch.object(pool.subscription_store, "get_credential_data",
                          return_value=cred), \
             patch.object(pool, "_refresh_oauth_token") as refresh:
            assert pool.ensure_fresh_and_fan_out("sub-e") is True
        refresh.assert_not_called()

    def test_low_runway_refreshes(self):
        old_exp = int(time.time() * 1000) + 10 * 60 * 1000   # 10 min
        new_exp = int(time.time() * 1000) + 8 * 3600 * 1000
        before = {"oauth_token": {"accessToken": "t", "refreshToken": "r", "expiresAt": old_exp}}
        after = {"oauth_token": {"accessToken": "new", "refreshToken": "r2", "expiresAt": new_exp}}
        with patch.object(pool.subscription_store, "get_subscription",
                          return_value=self._sub()), \
             patch.object(pool.subscription_store, "get_credential_data",
                          side_effect=[before, before, after]), \
             patch.object(pool, "_refresh_oauth_token", return_value="new") as refresh:
            assert pool.ensure_fresh_and_fan_out("sub-e") is True
        refresh.assert_called_once()

    def test_failed_refresh_reports_failsoft(self):
        old_exp = int(time.time() * 1000) + 10 * 60 * 1000
        cred = {"oauth_token": {"accessToken": "t", "refreshToken": "r", "expiresAt": old_exp}}
        with patch.object(pool.subscription_store, "get_subscription",
                          return_value=self._sub()), \
             patch.object(pool.subscription_store, "get_credential_data",
                          return_value=cred), \
             patch.object(pool, "_refresh_oauth_token", return_value=None):
            assert pool.ensure_fresh_and_fan_out("sub-e") is False

    def test_non_expiring_credential_is_fresh(self):
        with patch.object(pool.subscription_store, "get_subscription",
                          return_value=self._sub()), \
             patch.object(pool.subscription_store, "get_credential_data",
                          return_value={"api_key": "sk"}):
            assert pool.ensure_fresh_and_fan_out("sub-e") is True

    def test_unknown_subscription_is_false(self):
        with patch.object(pool.subscription_store, "get_subscription",
                          return_value=None):
            assert pool.ensure_fresh_and_fan_out("sub-gone") is False


class TestConcurrentBindDuringFanOut:
    """The fan-out iterates ``_session_subscriptions`` on a to_thread worker
    while bind/release mutate it on the event loop — an unguarded iteration
    would raise "dictionary changed size". Hammer both to prove the lock holds."""

    def setup_method(self):
        _clean()

    def test_fanout_snapshot_survives_bind_release_churn(self):
        import threading
        stop = threading.Event()
        errors: list[BaseException] = []

        def churn():
            i = 0
            while not stop.is_set():
                sid = f"sess-{i & 0xFF}"
                try:
                    pool.bind_session(sid, "sub-hot")
                    with patch.object(pool.subscription_store,
                                      "decrement_active_sessions"):
                        pool.release_subscription(sid)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return
                i += 1

        # Seed enough live bindings that the fan-out snapshot iterates a
        # non-trivial dict every call.
        for n in range(200):
            pool.bind_session(f"live-{n}", "sub-hot")

        t = threading.Thread(target=churn)
        t.start()
        try:
            with patch.object(pool.subscription_store, "get_credential_data",
                              return_value={"oauth_token": {
                                  "accessToken": "a", "refreshToken": "r",
                                  "expiresAt": 1}}), \
                 patch.object(pool, "_refresh_anthropic_oauth_token",
                              return_value="a"), \
                 patch("services.engines.token_fanout.fan_out"):
                for _ in range(500):
                    pool._refresh_oauth_token("sub-hot", "r")
        finally:
            stop.set()
            t.join()
        assert not errors, f"bind/release raised under fan-out: {errors[0]!r}"
