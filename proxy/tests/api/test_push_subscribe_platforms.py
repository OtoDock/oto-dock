"""``POST /v1/push/subscribe`` stores a browser's subscription (``web``) and
an app's push token under the app's own platform: ``android`` or ``ios``,
and a token registered again under another platform takes it. Any other
word is refused. An ``ios`` token is accepted and routed with its platform;
a visible alert on an iPhone needs the hosted relay's alert payload (a
pending relay change) and is not sent on the self-hosted direct Firebase
path. ``DELETE /v1/push/unsubscribe`` drops the caller's own row for a token
(the app's step when notifications are turned off): another person's row for
the same token stays, and a token it no longer holds answers ``not_found``
with a 200, which the app counts as done."""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage.automation import notification_store

client = TestClient(app)
ME = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User", role="member")


@pytest.fixture(autouse=True)
def _as_me():
    app.dependency_overrides[get_current_user] = lambda: ME
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("platform", ["web", "android", "ios"])
def test_each_platform_is_stored_as_itself(platform):
    r = client.post("/v1/push/subscribe",
                    json={"platform": platform, "subscription_data": f"tok-{platform}"})
    assert r.status_code == 200
    subs = notification_store.get_push_subscriptions("user-viewer")
    assert [(s["platform"], s["subscription_data"]) for s in subs] == [(platform, f"tok-{platform}")]


def test_another_word_is_refused():
    r = client.post("/v1/push/subscribe", json={"platform": "windows", "subscription_data": "t"})
    assert r.status_code == 400
    assert notification_store.get_push_subscriptions("user-viewer") == []


def test_a_token_registered_again_takes_its_new_platform():
    client.post("/v1/push/subscribe", json={"platform": "android", "subscription_data": "tok"})
    client.post("/v1/push/subscribe", json={"platform": "ios", "subscription_data": "tok"})
    subs = notification_store.get_push_subscriptions("user-viewer")
    assert [(s["platform"], s["subscription_data"]) for s in subs] == [("ios", "tok")]


OTHER = UserContext(sub="user-other", email="other@test.com", name="Other User", role="member")


def test_unsubscribe_drops_the_callers_own_row_only():
    client.post("/v1/push/subscribe", json={"platform": "android", "subscription_data": "tok-shared"})
    app.dependency_overrides[get_current_user] = lambda: OTHER
    client.post("/v1/push/subscribe", json={"platform": "android", "subscription_data": "tok-shared"})
    app.dependency_overrides[get_current_user] = lambda: ME
    r = client.delete("/v1/push/unsubscribe", params={"subscription_data": "tok-shared"})
    assert r.status_code == 200 and r.json() == {"status": "unsubscribed"}
    assert notification_store.get_push_subscriptions("user-viewer") == []
    others = notification_store.get_push_subscriptions("user-other")
    assert [s["subscription_data"] for s in others] == ["tok-shared"]


def test_unsubscribing_a_token_no_longer_held_answers_not_found():
    r = client.delete("/v1/push/unsubscribe", params={"subscription_data": "tok-gone"})
    assert r.status_code == 200 and r.json() == {"status": "not_found"}


def test_unsubscribe_needs_a_token_and_a_person():
    assert client.delete("/v1/push/unsubscribe").status_code == 422
    app.dependency_overrides[get_current_user] = lambda: None
    r = client.delete("/v1/push/unsubscribe", params={"subscription_data": "tok"})
    assert r.status_code == 401
