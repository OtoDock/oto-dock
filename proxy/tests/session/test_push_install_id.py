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

    async def fake_send_fcm(token, payload):
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
