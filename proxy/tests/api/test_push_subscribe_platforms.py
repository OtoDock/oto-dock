"""``POST /v1/push/subscribe`` stores a browser's subscription (``web``) and
an app's push token under the app's own platform: ``android`` or ``ios``,
and a token registered again under another platform takes it. Any other
word is refused. An ``ios`` token is accepted and routed with its platform;
a visible alert on an iPhone needs the hosted relay's alert payload (a
pending relay change) and is not sent on the self-hosted direct Firebase
path."""

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
