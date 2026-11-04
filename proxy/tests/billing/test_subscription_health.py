"""Grant-health sweep (services.infra.subscription_health): pre-expiry
warnings at 72h/24h, the expired-flip alert, and the blob-persisted dedup
stamps. Since 2026-09-01 stamps are {"expiry", "at"} dicts and a fired
threshold re-arms only on a MATERIAL forward push (≥_REARM_DELTA_MS) after
the per-key floor (_MIN_RENOTIFY_MS) — token rotations recompute the expiry
from wall-clock, so exact-match dedup re-fired on every rotation (19
warnings in ~52h live). Reconnect still re-arms by rebuilding the blob
without stamps; legacy bare-int stamps stay honored.

Both notices name the account by its label with its email and open AI
Engines; the two AI Engines lists carry each OAuth row's ``login_expires_at``
from the same reading, to the rows the caller may see."""

import asyncio
import re
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.infra import subscription_health
from tests._paths import REPO_ROOT


def _row(sub_id="s1", *, status="active", owner="user-1", auth_type="oauth",
         layer="claude-code-cli", label="Claude Max", email=""):
    return {
        "id": sub_id, "status": status, "owner_sub": owner,
        "auth_type": auth_type, "layer": layer, "provider": "anthropic",
        "label": label, "oauth_email": email,
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


# ---------------------------------------------------------------------------
# The words: the account's name, when it ends, where the notice opens
# ---------------------------------------------------------------------------

_H = 3600 * 1000


@pytest.mark.parametrize(("label", "email", "named"), [
    ("Claude Max", "a@example.com", "Claude Max (a@example.com)"),
    ("a@example.com", "A@Example.com", "a@example.com"),
    ("Claude Max", "3f2a9c1e-0000-4000-8000-000000000000", "Claude Max"),
    ("", "a@example.com", "a@example.com"),
    ("", "3f2a9c1e-0000-4000-8000-000000000000", "Claude"),
    ("", "", "Claude"),
])
def test_account_name(label, email, named):
    assert subscription_health.account_name(
        {"label": label, "oauth_email": email}, "Claude") == named


@pytest.mark.parametrize(("remaining_ms", "words"), [
    (72 * _H, "in about 3 days"),
    (71 * _H, "in about 3 days"),
    (60 * _H, "in about 3 days"),
    (49 * _H, "in about 2 days"),
    (47 * _H + 40 * 60_000, "in about 2 days"),
    (47 * _H + 20 * 60_000, "in about 47 hours"),
    (5 * _H, "in about 5 hours"),
    (1 * _H, "in about 1 hour"),
    (40 * 60_000, "in about 1 hour"),
    (20 * 60_000, "within the hour"),
    (-5 * 60_000, "within the hour"),
])
def test_ends_in_rounds_half_up_with_no_gap(remaining_ms, words):
    assert subscription_health.ends_in(remaining_ms) == words


@pytest.mark.parametrize(("cred", "ms"), [
    ({"oauth_token": {"refreshTokenExpiresAt": 1_800_000_000_000}}, 1_800_000_000_000),
    ({"oauth_token": {"refreshTokenExpiresAt": "1800000000000"}}, 1_800_000_000_000),
    ({"oauth_token": {"accessToken": "at"}}, 0),
    ({"oauth_token": {"refreshTokenExpiresAt": "soon"}}, 0),
    ({"oauth_token": {"refreshTokenExpiresAt": -5}}, 0),
    ({"oauth_token": "not a dict"}, 0),
    ({"api_key": "sk-x"}, 0),
    ([], 0),
])
def test_login_expiry_ms_never_raises(cred, ms):
    assert subscription_health.login_expiry_ms(cred) == ms


def test_soon_notice_names_the_account_and_opens_ai_engines():
    grant_ms = int((time.time() + 66 * 3600) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = _sweep([_row(email="a@example.com")], blobs)
    assert fire.await_count == 1
    kw = fire.await_args.kwargs
    assert kw["body"] == (
        "The login for “Claude Max (a@example.com)” ends in about 3 days. Press "
        "Reconnect on its row in User Settings, AI Engines to renew it."
    )
    assert kw["href"] == "/user-settings/ai-engines"
    assert "—" not in kw["body"] and "(s)" not in kw["body"]


def test_soon_notice_in_hours_names_the_label_alone_without_an_email():
    grant_ms = int((time.time() + 5 * 3600 + 60) * 1000)
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "refreshTokenExpiresAt": grant_ms}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_args.kwargs["body"].startswith(
        "The login for “Claude Max” ends in about 5 hours.")


def test_expired_notice_names_the_account_and_opens_ai_engines():
    blobs = {"s1": {"oauth_token": {"accessToken": "at"}}}
    fire = _sweep([_row(status="expired", email="a@example.com")], blobs)
    kw = fire.await_args.kwargs
    assert kw["body"] == (
        "The login for “Claude Max (a@example.com)” has ended. Press Reconnect "
        "on its row in User Settings, AI Engines to sign in again."
    )
    assert kw["href"] == "/user-settings/ai-engines"
    assert "—" not in kw["body"]


def test_a_token_of_another_shape_is_skipped_not_raised():
    blobs = {"s1": {"oauth_token": {"accessToken": "at", "healthAlerts": "junk",
                                    "refreshTokenExpiresAt": int((time.time() + 3600) * 1000)}}}
    fire = _sweep([_row()], blobs)
    assert fire.await_count == 1
    assert set(blobs["s1"]["oauth_token"]["healthAlerts"]) == {"24h"}
    fire2 = _sweep([_row("s2")], {"s2": {"oauth_token": "not a dict"}})
    assert fire2.await_count == 0


def test_the_window_is_one_value_on_both_sides():
    source = (REPO_ROOT / "dashboard" / "src" / "api" / "executionLayers.ts").read_text()
    m = re.search(r"export const LOGIN_ENDING_WINDOW_HOURS = (\d+)", source)
    assert m, "LOGIN_ENDING_WINDOW_HOURS is gone from api/executionLayers.ts"
    assert int(m.group(1)) * 3600 * 1000 == subscription_health.LOGIN_ENDING_WINDOW_MS
    assert dict(subscription_health._WARN_THRESHOLDS)["72h"] == subscription_health.LOGIN_ENDING_WINDOW_MS


def test_the_notices_link_is_a_dashboard_route():
    """The notice opens AI Engines: the settings base and the section's route
    as the dashboard declares them, so a renamed route fails here."""
    sections = (REPO_ROOT / "dashboard" / "src" / "components" / "settings" / "sections.ts").read_text()
    base = re.search(r"export const USER_SETTINGS_BASE = '([^']+)'", sections)
    assert base, "USER_SETTINGS_BASE is gone from components/settings/sections.ts"
    assert subscription_health.AI_ENGINES_PATH == f"{base.group(1)}/ai-engines"
    app = (REPO_ROOT / "dashboard" / "src" / "App.tsx").read_text()
    block = re.search(rf'<Route path="{base.group(1).lstrip("/")}"[^>]*>(.*?)\n\s*</Route>', app, re.S)
    assert block, "the user settings routes are gone from App.tsx"
    assert '<Route path="ai-engines"' in block.group(1)


# ---------------------------------------------------------------------------
# The AI Engines lists: login_expires_at on the rows the caller may see
# ---------------------------------------------------------------------------

_END_MS = 1_800_000_000_000
_END_ISO = datetime.fromtimestamp(_END_MS / 1000, tz=timezone.utc).isoformat(timespec="seconds")


class TestLoginExpiryOnTheLists:

    def _client(self, sub, role):
        from fastapi.testclient import TestClient

        from app import app
        from auth.providers import UserContext, get_current_user

        async def _who():
            return UserContext(sub=sub, email=f"{sub}@t.com", name=sub, role=role)

        app.dependency_overrides[get_current_user] = _who
        return TestClient(app), app, get_current_user

    @staticmethod
    def _oauth(owner, *, end_ms=_END_MS, contribute=False, email=""):
        from storage.billing import subscription_store
        token = {"accessToken": "", "refreshToken": ""}
        if end_ms:
            token["refreshTokenExpiresAt"] = end_ms
        return subscription_store.add_subscription(
            "claude-code-cli", "anthropic", "oauth", owner_sub=owner,
            contribute_platform=contribute, label="Claude Max",
            credential_data={"oauth_token": token}, oauth_email=email or f"{owner}@example.com")

    @staticmethod
    def _rows(payload, key):
        """Every listed row by id: the admin's pool rows or the user's own."""
        return {
            s["id"]: s
            for layer in payload["layers"]
            for s in (layer["subscriptions"]["platform"] if key == "admin"
                      else layer["user_subscriptions"])
        }

    def test_the_owner_sees_their_own_rows_only(self, temp_db):
        from storage.billing import subscription_store
        mine = self._oauth("local:m1")
        unknown = self._oauth("local:m1", end_ms=0, email="b@example.com")
        key = subscription_store.add_subscription(
            "codex-cli", "openai", "api_key", owner_sub="local:m1",
            credential_data={"api_key": "sk-x"})
        theirs = self._oauth("local:m2")
        client, app, dep = self._client("local:m1", "member")
        try:
            resp = client.get("/v1/users/me/execution-layers")
            assert resp.status_code == 200
            rows = self._rows(resp.json(), "user")
        finally:
            app.dependency_overrides.pop(dep, None)
        assert rows[mine["id"]]["login_expires_at"] == _END_ISO
        assert rows[unknown["id"]]["login_expires_at"] is None
        assert "login_expires_at" not in rows[key["id"]]
        assert theirs["id"] not in rows

    def test_the_admin_lists_carry_every_row(self, temp_db):
        own = self._oauth("local:admin", contribute=True)
        other = self._oauth("local:admin2", contribute=True, end_ms=_END_MS + 3_600_000)
        client, app, dep = self._client("local:admin", "admin")
        try:
            rows = self._rows(client.get("/v1/admin/execution-layers").json(), "admin")
            per_layer = {
                s["id"]: s for s in
                client.get("/v1/admin/execution-layers/claude-code-cli/subscriptions").json()["subscriptions"]
            }
        finally:
            app.dependency_overrides.pop(dep, None)
        other_iso = datetime.fromtimestamp(
            (_END_MS + 3_600_000) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        assert rows[own["id"]]["login_expires_at"] == _END_ISO
        assert rows[other["id"]]["login_expires_at"] == other_iso
        assert per_layer[own["id"]]["login_expires_at"] == _END_ISO
        assert per_layer[other["id"]]["login_expires_at"] == other_iso

    def test_a_member_cannot_read_the_admin_lists(self, temp_db):
        self._oauth("local:admin", contribute=True)
        client, app, dep = self._client("local:m1", "member")
        try:
            assert client.get("/v1/admin/execution-layers").status_code == 403
            assert client.get(
                "/v1/admin/execution-layers/claude-code-cli/subscriptions").status_code == 403
        finally:
            app.dependency_overrides.pop(dep, None)

    def test_an_unreadable_credential_reads_null_and_the_list_answers(self, temp_db, monkeypatch, caplog):
        from cryptography.fernet import InvalidToken

        from api.admin import execution_layers
        from storage.billing import subscription_store
        broken = self._oauth("local:m1")
        good = self._oauth("local:m1", email="b@example.com")
        real = subscription_store.get_credential_data

        def _read(sub_id):
            if sub_id == broken["id"]:
                raise InvalidToken()
            return real(sub_id)

        monkeypatch.setattr(subscription_store, "get_credential_data", _read)
        execution_layers._login_read_warned.discard(broken["id"])
        client, app, dep = self._client("local:m1", "member")
        try:
            with caplog.at_level("DEBUG", logger="api.admin.execution_layers"):
                first = client.get("/v1/users/me/execution-layers")
                second = client.get("/v1/users/me/execution-layers")
        finally:
            app.dependency_overrides.pop(dep, None)
        assert first.status_code == second.status_code == 200
        rows = self._rows(first.json(), "user")
        assert rows[broken["id"]]["login_expires_at"] is None
        assert rows[good["id"]]["login_expires_at"] == _END_ISO
        said = [r for r in caplog.records if "could not be read" in r.getMessage()]
        assert [r.levelname for r in said] == ["WARNING", "DEBUG"]
