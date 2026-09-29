"""Bring-your-own FCM (``push_sender._send_fcm_direct``): the Google access
token is cached while valid and refreshed in a worker thread with one
refresh in flight; pushes share one HTTP client per loop; a 401 from FCM
clears the token so the next push refreshes."""

import asyncio
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

    def __call__(self, url, method="GET", body=None, headers=None, timeout=120, **kw):
        return None


def _byo(monkeypatch, *, fcm_status: int = 200):
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
        return httpx.Response(fcm_status, json={"name": "projects/p1/messages/1"})

    real = httpx.AsyncClient

    class FakeClient(real):
        def __init__(self, *a, **kw):
            built.append(1)
            kw["transport"] = httpx.MockTransport(handler)
            kw.pop("verify", None)
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
