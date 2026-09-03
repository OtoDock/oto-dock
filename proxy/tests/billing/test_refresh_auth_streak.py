"""Sustained-401 refresh streak → expired verdict (subscription_pool).

Not every provider confirms a dead login with ``invalid_grant`` — OpenAI
answers a generic 401 ``invalid_request_error`` forever (observed live:
118+ attempts at the 10-min backoff cap, no notification ever). The streak
escalation flips such rows to ``expired`` once BOTH thresholds hold
(≥ _AUTH_STREAK_MIN_ATTEMPTS auth-shaped 401s spanning
≥ _AUTH_STREAK_MIN_SPAN_S), which fires the existing one-time
"login expired" health notification. Everything non-401 (or non-JSON)
neither counts nor resets. Sub ids here are unique to this file; the
autouse fixture keeps the module-level maps clean either way.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from services.engines import subscription_pool as sp


@pytest.fixture(autouse=True)
def _clean_refresh_state():
    sp._refresh_backoff.clear()
    sp._auth_fail_streaks.clear()
    yield
    sp._refresh_backoff.clear()
    sp._auth_fail_streaks.clear()


def _sub(sub_id: str, provider: str = "openai") -> dict:
    layer = "codex-cli" if provider == "openai" else "claude-code-cli"
    return {
        "id": sub_id, "layer": layer, "provider": provider,
        "auth_type": "oauth", "owner_type": "platform",
    }


def _oauth(refresh_token: str = "ort-dead", runway_s: int = -60) -> dict:
    return {
        "accessToken": "oat-stored",
        "refreshToken": refresh_token,
        "expiresAt": int((time.time() + runway_s) * 1000),
    }


def _resp_401_json() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"error": {"type": "invalid_request_error"}}
    return resp


def _resolve(sub_id: str, cred: dict, resp: MagicMock, provider: str = "openai"):
    """One refresh attempt through _resolve_oauth_access_token."""
    post_target = (
        "services.engines.subscription_pool.requests.post"
        if provider == "openai" else "httpx.post"
    )
    with patch("services.engines.subscription_pool.subscription_store") as store, \
            patch(post_target, return_value=resp):
        store.get_credential_data.return_value = cred
        result = sp._resolve_oauth_access_token(_sub(sub_id, provider), cred["oauth_token"])
        return result, store


def test_single_401_counts_but_stays_transient():
    (token, _), store = _resolve("st-first", {"oauth_token": _oauth()}, _resp_401_json())
    assert token is None  # stored token already past expiry
    assert sp._auth_fail_streaks["st-first"][1] == 1
    store.update_subscription.assert_not_called()


def test_streak_past_both_thresholds_expires_row():
    first = time.time() - sp._AUTH_STREAK_MIN_SPAN_S - 60
    sp._auth_fail_streaks["st-dead"] = (first, sp._AUTH_STREAK_MIN_ATTEMPTS - 1)
    _, store = _resolve("st-dead", {"oauth_token": _oauth()}, _resp_401_json())
    store.update_subscription.assert_called_once_with("st-dead", status="expired")
    assert "st-dead" not in sp._auth_fail_streaks
    assert "st-dead" not in sp._refresh_backoff


def test_count_without_span_stays_transient():
    sp._auth_fail_streaks["st-young"] = (time.time() - 600, sp._AUTH_STREAK_MIN_ATTEMPTS + 3)
    _, store = _resolve("st-young", {"oauth_token": _oauth()}, _resp_401_json())
    store.update_subscription.assert_not_called()
    assert sp._auth_fail_streaks["st-young"][1] == sp._AUTH_STREAK_MIN_ATTEMPTS + 4


def test_span_without_count_stays_transient():
    sp._auth_fail_streaks["st-sparse"] = (time.time() - 3 * 3600, 2)
    _, store = _resolve("st-sparse", {"oauth_token": _oauth()}, _resp_401_json())
    store.update_subscription.assert_not_called()


def test_400_does_not_count_toward_streak():
    # RFC 6749 client-side bugs (invalid_request/invalid_scope/...) are 400s —
    # they must stay transient forever, however long they persist.
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {"error": "invalid_request"}
    _, store = _resolve("st-clientbug", {"oauth_token": _oauth()}, resp)
    assert "st-clientbug" not in sp._auth_fail_streaks
    store.update_subscription.assert_not_called()


def test_5xx_and_non_json_401_neither_count_nor_reset():
    seeded = (time.time() - 3 * 3600, 5)
    sp._auth_fail_streaks["st-mixed"] = seeded

    resp_500 = MagicMock()
    resp_500.status_code = 500
    _resolve("st-mixed", {"oauth_token": _oauth()}, resp_500)
    assert sp._auth_fail_streaks["st-mixed"] == seeded

    sp._refresh_backoff.clear()  # allow the next attempt through
    resp_waf = MagicMock()  # WAF challenge page: 401 but not JSON
    resp_waf.status_code = 401
    resp_waf.json.side_effect = ValueError("not json")
    _, store = _resolve("st-mixed", {"oauth_token": _oauth()}, resp_waf)
    assert sp._auth_fail_streaks["st-mixed"] == seeded
    store.update_subscription.assert_not_called()


def test_successful_refresh_clears_streak():
    sp._auth_fail_streaks["st-heals"] = (time.time() - 3600, 4)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "oat-fresh", "refresh_token": "ort-fresh",
        "expires_in": 28800,
    }
    (token, _), _ = _resolve("st-heals", {"oauth_token": _oauth()}, resp)
    assert token == "oat-fresh"
    assert "st-heals" not in sp._auth_fail_streaks


def test_clear_refresh_backoff_clears_streak():
    sp._auth_fail_streaks["st-reconnect"] = (time.time() - 3600, 4)
    sp._refresh_backoff["st-reconnect"] = (time.time(), 3)
    sp.clear_refresh_backoff("st-reconnect")
    assert "st-reconnect" not in sp._auth_fail_streaks
    assert "st-reconnect" not in sp._refresh_backoff


def test_replacement_mid_attempt_discards_verdict_and_streak():
    # The credential was swapped while the (dead) attempt was in flight: the
    # verdict is discarded AND the old grant's exhausted streak must not
    # survive to expire the fresh grant on its first 401.
    first = time.time() - sp._AUTH_STREAK_MIN_SPAN_S - 60
    sp._auth_fail_streaks["st-swap"] = (first, sp._AUTH_STREAK_MIN_ATTEMPTS - 1)
    cred_old = {"oauth_token": _oauth("ort-old")}
    cred_new = {"oauth_token": _oauth("ort-new", runway_s=7200)}
    with patch("services.engines.subscription_pool.subscription_store") as store, \
            patch("services.engines.subscription_pool.requests.post",
                  return_value=_resp_401_json()):
        # Two reads inside _resolve: the under-lock re-read (old blob), then
        # the post-failure ownership check (replacement landed).
        store.get_credential_data.side_effect = [cred_old, cred_new]
        token, _ = sp._resolve_oauth_access_token(
            _sub("st-swap"), cred_old["oauth_token"],
        )
    assert token == "oat-stored"  # the replacement's still-valid token
    store.update_subscription.assert_not_called()
    assert "st-swap" not in sp._auth_fail_streaks
