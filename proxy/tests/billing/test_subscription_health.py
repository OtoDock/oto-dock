"""Grant-health sweep (services.infra.subscription_health): pre-expiry
warnings at 72h/24h, the expired-flip alert, and the blob-persisted dedup
stamps. Since 2026-09-01 stamps are {"expiry", "at"} dicts and a fired
threshold re-arms only on a MATERIAL forward push (≥_REARM_DELTA_MS) after
the per-key floor (_MIN_RENOTIFY_MS) — token rotations recompute the expiry
from wall-clock, so exact-match dedup re-fired on every rotation (19
warnings in ~52h live). Reconnect still re-arms by rebuilding the blob
without stamps; legacy bare-int stamps stay honored."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from services.infra import subscription_health


def _row(sub_id="s1", *, status="active", owner="user-1", auth_type="oauth",
         layer="claude-code-cli"):
    return {
        "id": sub_id, "status": status, "owner_sub": owner,
        "auth_type": auth_type, "layer": layer, "provider": "anthropic",
        "label": "Claude Max",
    }


def _sweep(rows, blobs):
    """Run one forced sweep; returns (fire_mock, blobs) — stamp writes land
    back into ``blobs`` so a second sweep sees them, like the real store."""
    fire = AsyncMock()

    def _get_cred(sub_id):
        return blobs.get(sub_id, {})

    def _update_cred(sub_id, cred):
        blobs[sub_id] = cred

    with patch.object(subscription_health, "subscription_store") as store, \
         patch("services.notifications.notification_manager.fire_notification", fire):
        store.list_subscriptions.return_value = rows
        store.get_credential_data.side_effect = _get_cred
        store.update_credential_data.side_effect = _update_cred
        subscription_health._last_run = 0.0
        asyncio.run(subscription_health.check_subscription_health())
    return fire


def test_72h_warning_fires_once_per_grant_generation():
    grant_ms = int((time.time() + 48 * 3600) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1
    assert "expires soon" in fire.await_args.kwargs["title"]
    assert blobs["s1"]["oauth_token"]["healthAlerts"]["72h"]["expiry"] == grant_ms

    # Stamped — the next sweep is silent.
    fire2 = _sweep([_row()], blobs)
    assert fire2.await_count == 0


def test_rotation_jitter_does_not_refire():
    """A token rotation recomputes refreshTokenExpiresAt from wall-clock —
    the drifted-but-same grant must NOT re-arm (the 2026-09-01 spam bug)."""
    grant_ms = int((time.time() + 48 * 3600) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1

    for drift_ms in (126, 5 * 3600 * 1000, -8 * 3600 * 1000):
        blobs["s1"]["oauth_token"]["refreshTokenExpiresAt"] = grant_ms + drift_ms
        fire_n = _sweep([_row()], blobs)
        assert fire_n.await_count == 0, f"re-fired on drift {drift_ms}"


def test_material_renewal_rearms_after_floor():
    grant_ms = int((time.time() + 48 * 3600) * 1000)
    now_ms = int(time.time() * 1000)
    aged_at = now_ms - subscription_health._MIN_RENOTIFY_MS - 1000
    blobs = {"s1": {"oauth_token": {
        "accessToken": "at",
        # Grant renewed ≥ _REARM_DELTA_MS past the stamped expiry, floor elapsed.
        "refreshTokenExpiresAt": grant_ms,
        "healthAlerts": {"72h": {
            "expiry": grant_ms - subscription_health._REARM_DELTA_MS - 1000,
            "at": aged_at,
        }},
    }}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1


def test_floor_blocks_even_a_material_push():
    grant_ms = int((time.time() + 48 * 3600) * 1000)
    now_ms = int(time.time() * 1000)
    blobs = {"s1": {"oauth_token": {
        "accessToken": "at",
        "refreshTokenExpiresAt": grant_ms,
        "healthAlerts": {"72h": {
            "expiry": grant_ms - subscription_health._REARM_DELTA_MS - 1000,
            "at": now_ms - 60_000,  # fired a minute ago
        }},
    }}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 0


def test_legacy_int_stamp_honored():
    """Pre-2026-09 stamps are bare expiry ints (at=0): the floor never blocks
    them, but the jitter guard must still suppress a drifted same-grant."""
    grant_ms = int((time.time() + 48 * 3600) * 1000)
    blobs = {"s1": {"oauth_token": {
        "accessToken": "at",
        "refreshTokenExpiresAt": grant_ms,
        "healthAlerts": {"72h": grant_ms - 5000},  # legacy, tiny drift since
    }}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 0


def test_24h_is_the_most_urgent_and_only_warning():
    grant_ms = int((time.time() + 12 * 3600) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1
    alerts = blobs["s1"]["oauth_token"]["healthAlerts"]
    assert list(alerts) == ["24h"]
    assert alerts["24h"]["expiry"] == grant_ms
    assert alerts["24h"]["at"]


def test_24h_fires_after_72h_despite_recent_stamp():
    """The normal pair: 72h warn stamped, then the account crosses into the
    24h window — the 24h key has no stamp, so the floor on the 72h key must
    not block the second (more urgent) warning."""
    grant_ms = int((time.time() + 23 * 3600) * 1000)
    now_ms = int(time.time() * 1000)
    blobs = {"s1": {"oauth_token": {
        "accessToken": "at",
        "refreshTokenExpiresAt": grant_ms,
        "healthAlerts": {"72h": {"expiry": grant_ms, "at": now_ms - 60_000}},
    }}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1
    assert "24h" in blobs["s1"]["oauth_token"]["healthAlerts"]


def test_expired_row_alerts_once():
    blobs = {"s1": {"oauth_token": {"accessToken": "at"}}}
    fire = _sweep([_row(status="expired")], blobs)
    assert fire.await_count == 1
    assert "expired" in fire.await_args.kwargs["title"]
    assert fire.await_args.kwargs["severity"] == "warning"
    assert fire.await_args.kwargs["target"] == "user-1"
    assert blobs["s1"]["oauth_token"]["healthAlerts"]["expired"]

    fire2 = _sweep([_row(status="expired")], blobs)
    assert fire2.await_count == 0


def test_unknown_grant_expiry_stays_silent():
    # Rows connected before the field existed — absence never means expiring.
    blobs = {"s1": {"oauth_token": {"accessToken": "at"}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 0


def test_non_oauth_and_ownerless_rows_skipped():
    grant_ms = int((time.time() + 3600) * 1000)
    blobs = {
        "k1": {"api_key": "sk-x"},
        "s2": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}},
    }
    fire = _sweep(
        [_row("k1", auth_type="api_key"), _row("s2", owner="")], blobs,
    )
    assert fire.await_count == 0


def test_sweep_self_throttles():
    grant_ms = int((time.time() + 3600) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = AsyncMock()
    with patch.object(subscription_health, "subscription_store") as store, \
         patch("services.notifications.notification_manager.fire_notification", fire):
        store.list_subscriptions.return_value = [_row()]
        store.get_credential_data.side_effect = lambda s: blobs.get(s, {})
        store.update_credential_data.side_effect = blobs.__setitem__
        subscription_health._last_run = 0.0
        asyncio.run(subscription_health.check_subscription_health())
        calls_after_first = store.list_subscriptions.call_count
        # Immediately again — inside the 15-min window, the pass is skipped.
        asyncio.run(subscription_health.check_subscription_health())
        assert store.list_subscriptions.call_count == calls_after_first
