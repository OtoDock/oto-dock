"""The release dialog's rule (services.infra.release_notes): who is eligible,
what an admin and a member are told, the two routes and the atomic mark.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from services.infra import release_notes
from storage import schema
from storage.pg import get_conn
from storage.prefs import user_ui_prefs_store

# ── the rule ─────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
UPGRADED = {"created_by": "", "last_migrated_by": "1.8.0", "last_migrated_at": NOW, "upgraded_from": "1.7.1"}
FRESH = {"created_by": "1.8.0", "last_migrated_by": "1.8.0", "last_migrated_at": NOW, "upgraded_from": ""}
OLD_USER = (NOW - timedelta(days=30)).isoformat()
NEW_USER = (NOW + timedelta(minutes=1)).isoformat()
SHAPE = {"show_admin", "eligible", "version", "previous", "direction", "seen", "release_url"}


def _card(**kw):
    args = dict(running="1.8.0", is_admin=True, user_created_at=OLD_USER, seen="", meta=UPGRADED)
    args.update(kw)
    return release_notes.card_for(**args)


def test_an_admin_who_was_there_before_the_upgrade_is_shown_with_the_release_page():
    card = _card()
    assert set(card) == SHAPE
    assert card["eligible"] is True and card["show_admin"] is True
    assert card["previous"] == "1.7.1" and card["direction"] == "up" and card["version"] == "1.8.0"
    assert card["release_url"] == "https://github.com/OtoDock/oto-dock/releases/tag/v1.8.0"
    assert _card(running="1.8.0-rc1", seen="1.7.1")["release_url"] == release_notes.RELEASES_URL


def test_a_fresh_install_a_new_person_and_a_seen_version_are_not_eligible():
    for card in (_card(meta=FRESH), _card(user_created_at=NEW_USER), _card(seen="1.8.0"),
                 _card(meta=None), _card(running="1.8.0-laneu")):
        assert card["eligible"] is False and card["show_admin"] is False and card["release_url"] == ""


def test_a_dev_tree_tells_nobody():
    """T1 and every working tree: never refused, never recorded, never told,
    whatever the row and the seen version say."""
    assert _card(running="1.8.0-dev")["eligible"] is False
    assert _card(running="1.8.0-dev", seen="1.7.1")["eligible"] is False
    assert _card(running="1.8.0-rc1", seen="1.7.1")["eligible"] is True


def test_a_seen_version_that_differs_is_eligible_whatever_the_install_says():
    card = _card(seen="1.7.0", meta=FRESH, user_created_at=NEW_USER)
    assert card["eligible"] is True and card["previous"] == "1.7.0" and card["direction"] == "up"


def test_a_downgrade_is_told_as_one():
    card = _card(seen="1.9.0")
    assert card["eligible"] is True and card["direction"] == "down" and card["previous"] == "1.9.0"


def test_a_member_is_eligible_but_not_shown_by_the_server_and_gets_no_link():
    """The dashboard decides a member's show by the running version's
    highlights for everyone; the server says only that the rule passes."""
    card = _card(is_admin=False)
    assert card["eligible"] is True and card["show_admin"] is False and card["release_url"] == ""
    assert _card(is_admin=False, seen="1.8.0")["eligible"] is False


# ── the routes and the mark ──────────────────────────────────────────────────

def _client(sub: str, email: str, role: str) -> TestClient:
    from app import app
    from auth.providers import create_session_jwt
    return TestClient(app, cookies={"session": create_session_jwt(sub, email, "U", role)})


@pytest.fixture
def upgraded_install(monkeypatch):
    monkeypatch.setattr(config, "PINNED_OTODOCK_VERSION", "1.8.0")
    with get_conn() as conn:
        conn.execute("UPDATE users SET created_at = %s", ("2026-01-01T00:00:00+00:00",))
        schema.migrate_database(conn, "1.7.1")
        schema.migrate_database(conn, "1.8.0")
        conn.commit()


def test_the_card_route_tells_an_admin_once_and_the_mark_is_atomic(upgraded_install):
    admin = _client("user-admin", "admin@test.com", "admin")
    r = admin.get("/v1/users/me/release-card")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == SHAPE
    assert body["show_admin"] is True and body["eligible"] is True
    assert body["version"] == "1.8.0" and body["previous"] == "1.7.1" and body["seen"] == ""
    assert body["release_url"].endswith("/releases/tag/v1.8.0")
    r = admin.post("/v1/users/me/release-card/seen")
    assert r.status_code == 200 and r.json()["seen"] == "1.8.0"
    assert admin.get("/v1/users/me/release-card").json()["eligible"] is False
    # The mark carries the version the dialog showed: one left open across an
    # upgrade and dismissed after it marks the old version, so the new one's
    # dialog still shows; a newer or unparsable version falls back to the
    # running one.
    r = admin.post("/v1/users/me/release-card/seen", json={"version": "1.7.1"})
    assert r.json()["seen"] == "1.7.1"
    assert admin.get("/v1/users/me/release-card").json()["eligible"] is True
    assert admin.post("/v1/users/me/release-card/seen", json={"version": "1.9.0"}).json()["seen"] == "1.8.0"
    assert admin.post("/v1/users/me/release-card/seen", json={"version": "latest"}).json()["seen"] == "1.8.0"
    assert admin.post("/v1/users/me/release-card/seen", json={}).json()["seen"] == "1.8.0"
    # The mark merges into the bag without touching other keys.
    user_ui_prefs_store.upsert_prefs("user-admin", {"other": 1})
    user_ui_prefs_store.mark_release_seen("user-admin", "1.9.0")
    assert user_ui_prefs_store.get_prefs("user-admin") == {"other": 1, "last_seen_version": "1.9.0"}


def test_a_member_created_before_the_upgrade_is_eligible_without_a_link(upgraded_install):
    member = _client("user-viewer", "viewer@test.com", "member")
    body = member.get("/v1/users/me/release-card").json()
    assert body["eligible"] is True and body["show_admin"] is False and body["release_url"] == ""


def test_a_person_created_after_the_upgrade_is_not_told(upgraded_install):
    # The upgrade happened before every seeded person existed (a session
    # token predating a person's created_at is refused, so the row moves).
    with get_conn() as conn:
        conn.execute("UPDATE schema_meta SET last_migrated_at = %s", ("2025-06-01T00:00:00+00:00",))
        conn.commit()
    r = _client("user-viewer2", "viewer2@test.com", "member").get("/v1/users/me/release-card")
    assert r.status_code == 200, r.text
    assert r.json()["eligible"] is False and r.json()["seen"] == ""
