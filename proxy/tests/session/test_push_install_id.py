"""The source-installation tag (`install_id`) must ride along on every native
push so the Android app can route a multi-installation notification to the
server it came from. Covers both push construction sites in notification_manager
(the regular delivery payload and the ephemeral turn-complete signal)."""

import pytest

import services.notifications.notification_manager as nm


@pytest.mark.asyncio
async def test_delivery_push_tags_install_id(monkeypatch):
    captured = {}

    async def fake_send_to_user(user_sub, payload):
        captured["payload"] = payload

    import services.notifications.push_sender as ps
    monkeypatch.setattr(ps, "send_to_user", fake_send_to_user)
    monkeypatch.setattr(nm, "_install_id", lambda: "INST-XYZ")
    # No connected device → the native-push branch runs.
    monkeypatch.setattr(nm, "get_active_connections", lambda u: [])
    monkeypatch.setattr(nm, "get_all_connections", lambda u: [])

    delivery = {
        "id": "d1", "notification_id": "n1", "title": "T", "body": "B",
        "severity": "warning", "scope": "user", "source": "",
        "delivered_at": "2026-06-27T00:00:00Z",
        "agent_slug": "agentx", "chat_id": "chat1",
    }
    await nm._deliver_to_user("user1", delivery)

    assert captured["payload"]["install_id"] == "INST-XYZ"
    assert captured["payload"]["click_url"] == "/chat/agentx/chat1"


@pytest.mark.asyncio
async def test_ephemeral_push_tags_install_id(monkeypatch):
    captured = {}

    async def fake_send_fcm(token, payload, platform="android"):
        captured["payload"] = payload

    import services.notifications.push_sender as ps
    from storage.automation import notification_store as ns
    monkeypatch.setattr(ps, "send_fcm", fake_send_fcm)
    monkeypatch.setattr(ns, "get_push_subscriptions",
                        lambda u: [{"platform": "android", "subscription_data": "tok"}])
    monkeypatch.setattr(nm, "_install_id", lambda: "INST-XYZ")
    # No active connection + no recorded origin → ephemeral falls through to FCM.
    monkeypatch.setattr(nm, "has_active_connection", lambda u: False)

    await nm.fire_ephemeral("user1", "Response ready", "", chat_id=None)

    assert captured["payload"]["install_id"] == "INST-XYZ"
    assert captured["payload"]["ephemeral"] is True


@pytest.mark.asyncio
async def test_an_app_notification_push_opens_the_app(monkeypatch):
    """A delivery whose href is an app's page pushes that page as click_url,
    ahead of the chat deep link: the Android app navigates there in place or
    hands it to the installation it came from (useFcmPush, switchToInstall)."""
    captured = {}

    async def fake_send_to_user(user_sub, payload):
        captured["payload"] = payload

    import services.notifications.push_sender as ps
    monkeypatch.setattr(ps, "send_to_user", fake_send_to_user)
    monkeypatch.setattr(nm, "_install_id", lambda: "INST-XYZ")
    monkeypatch.setattr(nm, "get_active_connections", lambda u: [])
    monkeypatch.setattr(nm, "get_all_connections", lambda u: [])

    app_id = "6f1c2e0a-1b2c-4d5e-8f90-123456789abc"
    delivery = {
        "id": "d2", "notification_id": "", "title": "Board stopped", "body": "B",
        "severity": "warning", "scope": "user", "source": "app",
        "delivered_at": "2026-10-08T00:00:00Z",
        "agent_slug": "agentx", "chat_id": "chat9", "href": f"/apps/{app_id}",
    }
    await nm._deliver_to_user("user1", delivery)

    assert captured["payload"]["click_url"] == f"/apps/{app_id}"
    assert captured["payload"]["install_id"] == "INST-XYZ"
