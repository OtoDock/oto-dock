"""Bring-your-own FCM (``push_sender._send_fcm_direct``): the Google access
token is cached while valid and refreshed in a worker thread with one
refresh in flight; pushes share one HTTP client per loop; a 401 from FCM
clears the token so the next push refreshes."""

import asyncio
import json
import threading
import time

import httpx
import pytest

from services.notifications import push_sender

pytestmark = pytest.mark.asyncio


class _Creds:
    """The parts of ``google.oauth2.service_account.Credentials`` the sender
    uses: ``valid``, ``token`` and a blocking ``refresh(request)``."""

    def __init__(self, delay: float = 0.2) -> None:
        self.token = None
        self.delay = delay
        self.refreshes = 0
        self.threads: list[threading.Thread] = []
        self.requests: list = []

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request) -> None:
        self.threads.append(threading.current_thread())
        self.requests.append(request)
        time.sleep(self.delay)
        self.refreshes += 1
        self.token = f"tok-{self.refreshes}"


class _Request:
    """The google-auth transport callable the sender hands to ``refresh``."""

    def __init__(self, session=None):
        self.session = session

    def __call__(self, url, method="GET", body=None, headers=None, timeout=120, **kw):
        return None


def _byo(monkeypatch, *, fcm_status: int = 200, bodies: list | None = None):
    """``bodies``, when given, collects every message posted to FCM."""
    creds = _Creds()
    monkeypatch.setattr(push_sender, "_fcm_credentials", creds, raising=False)
    monkeypatch.setattr(push_sender, "_fcm_available", True)
    monkeypatch.setattr(push_sender, "_fcm_project_id", "p1")
    monkeypatch.setattr(push_sender, "GoogleAuthRequest", _Request, raising=False)
    push_sender.reset_fcm_state()
    posts: list[str] = []
    built: list[int] = []

    def handler(request):
        posts.append(request.headers.get("authorization"))
        if bodies is not None:
            bodies.append(json.loads(request.content))
        return httpx.Response(fcm_status, json={"name": "projects/p1/messages/1"})

    real = httpx.AsyncClient

    class FakeClient(real):
        def __init__(self, *a, **kw):
            built.append(1)
            kw["transport"] = httpx.MockTransport(handler)
            kw.pop("verify", None)
            kw.pop("mounts", None)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return creds, posts, built


class _Ticker:
    def __init__(self) -> None:
        self.late: list[float] = []
        self._stop = False

    async def run(self) -> None:
        while not self._stop:
            t0 = time.perf_counter()
            await asyncio.sleep(0.005)
            self.late.append(time.perf_counter() - t0 - 0.005)


async def test_concurrent_pushes_refresh_once_off_the_loop_and_share_a_client(monkeypatch):
    creds, posts, built = _byo(monkeypatch)
    ticker = _Ticker()
    task = asyncio.ensure_future(ticker.run())
    loop_thread = threading.current_thread()

    results = await asyncio.gather(
        *[push_sender._send_fcm_direct(f"dev-{i}", {"title": "T"}) for i in range(10)])
    ticker._stop = True
    await task

    assert results == [True] * 10
    assert creds.refreshes == 1 and len(posts) == 10
    assert all(h == "Bearer tok-1" for h in posts)
    assert creds.threads and all(t is not loop_thread for t in creds.threads)
    assert creds.requests[0].keywords == {"timeout": push_sender._FCM_TOKEN_TIMEOUT_S}
    assert len(built) == 1
    # The 200 ms refresh ran while the loop kept ticking.
    assert max(ticker.late) < 0.1

    # A later push reuses the token and the client.
    assert await push_sender._send_fcm_direct("dev-x", {"title": "T"}) is True
    assert creds.refreshes == 1 and len(built) == 1


async def test_fcm_401_clears_the_token_so_the_next_push_refreshes(monkeypatch):
    creds, posts, _built = _byo(monkeypatch, fcm_status=401)
    assert await push_sender._send_fcm_direct("dev-1", {"title": "T"}) is False
    assert creds.refreshes == 1 and creds.valid is False
    assert await push_sender._send_fcm_direct("dev-1", {"title": "T"}) is False
    assert creds.refreshes == 2 and posts == ["Bearer tok-1", "Bearer tok-2"]


# --- The message, the relay's shape -----------------------------------------
#
# The relay (``fcm_sender.build_message`` in the commercial repo) sends the
# iOS app an alert; the direct path builds the same message, so a self-hosted
# install with its own Firebase project reaches the app the same way. The
# expected blocks are the relay's own test expectations, copied.

_RELAY_DANGER_APNS = {
    "headers": {"apns-priority": "10", "apns-push-type": "alert"},
    "payload": {"aps": {
        "alert": {"title": "Disk full", "body": "The backup volume is full."},
        "sound": "notif_danger.caf",
        "interruption-level": "time-sensitive",
        "thread-id": "otodock-danger",
    }},
}

# APNs refuses a payload over 4 KB. FCM adds its own keys (``gcm.message_id``,
# ``google.c.*``) beside ``aps`` and the data map; leave them room.
_APNS_MAX_BYTES = 4096
_FCM_OWN_KEYS_BYTES = 256


def _apns_payload_bytes(msg: dict) -> int:
    """The size of the Apple payload FCM builds from ``msg``: ``aps`` plus the
    data map copied into the root, as compact UTF-8 JSON."""
    payload = {**msg["apns"]["payload"], **msg["data"]}
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


async def test_an_ios_danger_push_is_the_relays_time_sensitive_alert():
    msg = push_sender.fcm_message("ios", "ios-tok", {
        "title": "Disk full", "body": "The backup volume is full.",
        "delivery_id": "d-1", "severity": "danger",
        "click_url": "/chat/ops/c-1", "install_id": "inst-1",
    })["message"]
    assert msg["token"] == "ios-tok"
    assert msg["apns"] == _RELAY_DANGER_APNS
    assert "notification" not in msg
    # The data map stays: the tap handler reads these from the payload's root.
    assert msg["data"]["install_id"] == "inst-1"
    assert msg["data"]["click_url"] == "/chat/ops/c-1"
    assert msg["data"]["severity"] == "danger"
    assert msg["data"]["ephemeral"] == "false"


@pytest.mark.parametrize(("severity", "sound", "level"), [
    ("warning", "notif_warning.caf", "active"),
    ("success", "notif_success.caf", "active"),
    ("info", "notif_success.caf", "active"),
    ("unknown", "notif_success.caf", "active"),
])
async def test_an_ios_push_takes_its_sound_and_level_from_the_severity(severity, sound, level):
    aps = push_sender.fcm_message("ios", "tok", {
        "title": "T", "body": "B", "severity": severity,
    })["message"]["apns"]["payload"]["aps"]
    assert aps["sound"] == sound
    assert aps["interruption-level"] == level
    assert aps["thread-id"] == f"otodock-{severity}"


async def test_a_plain_ios_push_is_an_active_alert_with_no_content_available():
    msg = push_sender.fcm_message("ios", "tok", {"title": "Hi", "body": "done"})["message"]
    aps = msg["apns"]["payload"]["aps"]
    assert msg["data"]["severity"] == "info"
    assert aps == {
        "alert": {"title": "Hi", "body": "done"},
        "sound": "notif_success.caf",
        "interruption-level": "active",
        "thread-id": "otodock-info",
    }
    assert "content-available" not in aps


@pytest.mark.parametrize("severity", ["info", "danger"])
async def test_an_ephemeral_ios_push_is_the_turn_complete_alert_whatever_its_severity(severity):
    msg = push_sender.fcm_message("ios", "tok", {
        "title": "Agent", "body": "Finished.", "severity": severity, "ephemeral": True,
    })["message"]
    aps = msg["apns"]["payload"]["aps"]
    assert msg["data"]["ephemeral"] == "true"
    assert aps["sound"] == "notif_turn_done.caf"
    assert aps["interruption-level"] == "active"
    assert aps["thread-id"] == "otodock-turn-complete"


@pytest.mark.parametrize("letter", ["x", "λ", "🔔"])
async def test_a_long_ios_body_is_capped_in_both_copies_and_fits_apns(letter):
    body = letter * 5000
    msg = push_sender.fcm_message("ios", "tok", {
        "title": "Report", "body": body, "severity": "danger",
        "delivery_id": "0f8e6c1a-3b2d-4e5f-9a8b-7c6d5e4f3a2b",
        "click_url": "/chat/operations-agent/0f8e6c1a-3b2d-4e5f-9a8b-7c6d5e4f3a2b",
        "install_id": "5a4b3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d",
    })["message"]
    alert_body = msg["apns"]["payload"]["aps"]["alert"]["body"]
    assert alert_body == msg["data"]["body"]
    assert alert_body.endswith("…")
    assert body.startswith(alert_body[:-1])
    assert len(alert_body.encode()) <= push_sender._IOS_BODY_MAX_BYTES
    assert _apns_payload_bytes(msg) <= _APNS_MAX_BYTES - _FCM_OWN_KEYS_BYTES


async def test_a_long_ios_title_is_capped_too_and_a_fitting_text_is_kept_whole():
    msg = push_sender.fcm_message("ios", "tok", {"title": "Τ" * 5000, "body": "🔔" * 5000, "severity": "danger"})["message"]
    title = msg["apns"]["payload"]["aps"]["alert"]["title"]
    assert title == msg["data"]["title"] and title.endswith("…")
    assert len(title.encode()) <= push_sender._IOS_TITLE_MAX_BYTES
    assert _apns_payload_bytes(msg) <= _APNS_MAX_BYTES - _FCM_OWN_KEYS_BYTES
    whole = "x" * push_sender._IOS_BODY_MAX_BYTES
    assert push_sender.fcm_message("ios", "tok", {"body": whole})["message"]["data"]["body"] == whole


async def test_android_keeps_the_data_only_message_uncapped():
    msg = push_sender.fcm_message("android", "tok", {"title": "T" * 5000, "body": "B" * 5000, "severity": "danger"})["message"]
    assert "apns" not in msg and "notification" not in msg
    assert msg["android"] == {"priority": "high"}
    assert msg["data"]["title"] == "T" * 5000 and msg["data"]["body"] == "B" * 5000


async def test_the_direct_send_posts_the_block_on_ios_and_none_on_android(monkeypatch):
    bodies: list = []
    _byo(monkeypatch, bodies=bodies)
    payload = {"title": "Disk full", "body": "The backup volume is full.", "severity": "danger"}
    assert await push_sender._send_fcm_direct("ios-tok", payload, "ios") is True
    assert await push_sender._send_fcm_direct("droid-tok", payload, "android") is True
    assert await push_sender._send_fcm_direct("default-tok", payload) is True
    assert [b["message"]["token"] for b in bodies] == ["ios-tok", "droid-tok", "default-tok"]
    assert bodies[0]["message"]["apns"] == _RELAY_DANGER_APNS
    assert "apns" not in bodies[1]["message"] and "apns" not in bodies[2]["message"]
    # The platform rides from send_fcm, where send_to_user passes the subscription's.
    assert await push_sender.send_fcm("ios-tok", payload, "ios") is True
    assert bodies[3]["message"]["apns"] == _RELAY_DANGER_APNS
