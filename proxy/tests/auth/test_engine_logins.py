"""The engine connect routes.

Each ChatGPT device login runs in a private Codex home that only its own
``status`` and ``finish`` read; a ``start`` touches only the caller's own
earlier login; every connect route takes a dashboard principal; a start
is rate limited per person. Everything is mocked: the spawned ``codex``
is a fake that prints the URL and code when released and writes its
``auth.json`` where the spawn env's ``CODEX_HOME`` says (or at the
module's global path when the env carries none).
"""

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock

import pytest
from fastapi import HTTPException

from api.auth import claude_oauth as claude_api
from api.auth import openai_oauth as openai_api
from api.auth.openai_oauth import OAuthFinishRequest, OAuthStartRequest
from auth import rate_limiter

A = SimpleNamespace(sub="user-a", role="member")
B = SimpleNamespace(sub="user-b", role="member")
BEARER = SimpleNamespace(sub="user-a", role="admin", is_api_key=True)

_BUCKET = getattr(openai_api, "_START_BUCKET", "oauth_start_openai")


def _blob(who: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": f"idtok-{who}",
            "access_token": f"ACCESS-{who}",
            "refresh_token": f"REFRESH-{who}",
            "account_id": f"acct-{who}",
        },
        "last_refresh": "2026-09-28T00:00:00Z",
    }


def _jwt(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"hdr.{seg}.sig"


class FakeCodex:
    """A stand-in for the spawned ``codex login --device-auth``."""

    def __init__(self, argv, kwargs, global_path: Path):
        self.argv = argv
        self.kwargs = kwargs
        self.env = kwargs.get("env") or {}
        self.pid = None
        self.returncode = None
        self.signals: list[int] = []
        self.parked = asyncio.Event()
        self._wake = asyncio.Event()
        self._released = False
        self._lines = [b"Open https://auth.openai.com/codex/device\n", b"ABCD-EFGH\n"]
        self.stdout = self
        home = self.env.get("CODEX_HOME")
        self.auth_path = Path(home) / "auth.json" if home else global_path

    async def readline(self) -> bytes:
        if not self._released and self.returncode is None:
            self.parked.set()
            await self._wake.wait()
        if not self._released or not self._lines:
            return b""
        return self._lines.pop(0)

    def release(self) -> None:
        self._released = True
        self._wake.set()

    def exit(self, rc: int = 0) -> None:
        self.returncode = rc
        self._wake.set()

    def send_signal(self, sig) -> None:
        self.signals.append(int(sig))
        self.exit(-int(sig))

    def terminate(self) -> None:
        self.send_signal(15)

    def kill(self) -> None:
        self.send_signal(9)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.001)
        return self.returncode

    def complete(self, blob: dict) -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_path.write_text(json.dumps(blob))
        self.exit(0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    base = tmp_path / "logins"
    global_path = tmp_path / "global" / "auth.json"
    procs: list[FakeCodex] = []

    async def spawn(*argv, **kwargs):
        proc = FakeCodex(argv, kwargs, global_path)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(openai_api, "_LOGIN_HOME_BASE", base, raising=False)
    monkeypatch.setattr(openai_api, "_AUTH_JSON_PATH", global_path, raising=False)
    monkeypatch.setattr(openai_api, "_END_GRACE_S", 0.01, raising=False)
    monkeypatch.setattr(openai_api, "_FINISH_EXIT_GRACE_S", 0.01, raising=False)
    monkeypatch.setattr(openai_api, "_require_chatgpt_login_engine", lambda layer: None)
    store = MagicMock()
    store.list_subscriptions.return_value = []
    store.add_subscription.return_value = {"id": "new-sub"}
    store.get_subscription.return_value = {"id": "refreshed-sub"}
    monkeypatch.setattr(openai_api, "subscription_store", store)
    monkeypatch.setattr(openai_api, "subscription_pool", MagicMock())
    from services.engines import subscription_windows
    from storage import database
    monkeypatch.setattr(subscription_windows, "schedule_poll", lambda sid: None)
    monkeypatch.setattr(database, "get_user", lambda sub: {"role": "member"})
    openai_api._active_logins.clear()
    claude_api._oauth_states.clear()
    rate_limiter._attempts.clear()
    yield SimpleNamespace(procs=procs, base=base, global_path=global_path, store=store)
    openai_api._active_logins.clear()
    claude_api._oauth_states.clear()
    rate_limiter._attempts.clear()


def _start(user):
    return asyncio.ensure_future(
        openai_api.oauth_start(OAuthStartRequest(owner_type="user"), user=user),
    )


async def _parked(rig, n: int) -> FakeCodex:
    for _ in range(400):
        if len(rig.procs) >= n and rig.procs[n - 1].parked.is_set():
            return rig.procs[n - 1]
        await asyncio.sleep(0.005)
    raise AssertionError("the login did not reach its read window")


async def _connected(rig, user):
    """Start a login for ``user`` and let its fake print the URL and code
    (a start that fails before its read window raises its own error)."""
    task = _start(user)
    parked = asyncio.ensure_future(_parked(rig, len(rig.procs) + 1))
    await asyncio.wait({task, parked}, return_when=asyncio.FIRST_COMPLETED)
    if task.done() and not parked.done():
        parked.cancel()
        await task
    proc = await parked
    proc.release()
    return await task, proc


def _finish(login_id: str, user):
    return openai_api.oauth_finish(OAuthFinishRequest(login_id=login_id), user=user)


def _status(login_id: str, user):
    return openai_api.oauth_status(login_id, user=user)


# ---------------------------------------------------------------------------
# One home per login, read only by its own login
# ---------------------------------------------------------------------------

def test_two_people_never_share_a_login_result(rig):
    async def scenario():
        ta = _start(A)
        pa = await _parked(rig, 1)
        tb = _start(B)                    # inside A's read window
        pb = await _parked(rig, 2)
        pa.release()
        pb.release()
        ra, rb = await ta, await tb

        pa.complete(_blob("A"))
        assert (await _status(rb["login_id"], B))["status"] == "pending"
        with pytest.raises(HTTPException) as exc:
            await _finish(rb["login_id"], B)
        assert exc.value.status_code == 400
        rig.store.add_subscription.assert_not_called()
        rig.store.update_credential_data.assert_not_called()

        assert (await _status(ra["login_id"], A))["status"] == "completed"
        await _finish(ra["login_id"], A)
        kw = rig.store.add_subscription.call_args.kwargs
        assert kw["owner_sub"] == "user-a"
        assert kw["oauth_email"] == "acct-A"
        assert kw["credential_data"]["oauth_token"]["accessToken"] == "ACCESS-A"
        assert kw["credential_data"]["codex_auth_blob"]["tokens"]["id_token"] == "idtok-A"
        homes = {pa.env.get("CODEX_HOME"), pb.env.get("CODEX_HOME")}
        assert None not in homes and len(homes) == 2

    asyncio.run(scenario())


def test_status_waits_for_a_whole_file(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.auth_path.parent.mkdir(parents=True, exist_ok=True)
        pa.auth_path.write_text('{"tokens": {"acc')
        assert (await _status(ra["login_id"], A))["status"] == "pending"
        pa.complete(_blob("A"))
        assert (await _status(ra["login_id"], A))["status"] == "completed"

    asyncio.run(scenario())


def test_finish_refuses_a_blob_without_identity(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.complete({"tokens": {"access_token": "ACCESS-A", "refresh_token": "R"}})
        with pytest.raises(HTTPException) as exc:
            await _finish(ra["login_id"], A)
        assert exc.value.status_code == 400
        assert "did not identify" in exc.value.detail
        rig.store.add_subscription.assert_not_called()
        rig.store.update_credential_data.assert_not_called()
        assert not Path(pa.env["CODEX_HOME"]).exists()

    asyncio.run(scenario())


def test_identity_falls_back_to_the_id_token_claims(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.complete({"tokens": {
            "access_token": "ACCESS-A",
            "id_token": _jwt({"email": "Person@Example.com"}),
        }})
        await _finish(ra["login_id"], A)
        assert rig.store.add_subscription.call_args.kwargs["oauth_email"] == "Person@Example.com"

    asyncio.run(scenario())


def test_finish_removes_the_login_home(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        home = Path(pa.env["CODEX_HOME"])
        assert home.is_dir()
        pa.complete(_blob("A"))
        await _finish(ra["login_id"], A)
        assert not home.exists()
        assert ra["login_id"] not in openai_api._active_logins

    asyncio.run(scenario())


def _refused_on_loop(*_a, **_kw):
    """A store mock's side effect: refuse a call made on a thread with a
    running event loop (a worker thread has none)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return DEFAULT
    raise AssertionError("a store call ran on the event loop")


def test_finish_stores_the_login_off_the_loop(rig, monkeypatch):
    from storage import database
    for name in ("list_subscriptions", "add_subscription", "get_subscription",
                 "update_credential_data", "update_subscription"):
        getattr(rig.store, name).side_effect = _refused_on_loop
    get_user = MagicMock(side_effect=_refused_on_loop, return_value={"role": "member"})
    monkeypatch.setattr(database, "get_user", get_user)

    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.complete(_blob("A"))
        await _finish(ra["login_id"], A)
        rig.store.add_subscription.assert_called_once()
        get_user.assert_called_once_with("user-a")

        # The same account again: the row is matched and re-read.
        rig.store.list_subscriptions.return_value = [{
            "id": "row-a", "auth_type": "oauth", "provider": "openai",
            "oauth_email": "acct-A", "status": "active",
        }]
        ra, pa = await _connected(rig, A)
        pa.complete(_blob("A"))
        assert await _finish(ra["login_id"], A) == {"subscription": {"id": "refreshed-sub"}}
        rig.store.get_subscription.assert_called_once_with("row-a")
        rig.store.add_subscription.assert_called_once()

    asyncio.run(scenario())


class _LoopFileGuard:
    """While armed, refuses file work (a directory made, a mode set, a
    directory listed or removed, a file read or tested) on a thread with a
    running event loop; a worker thread has none. Disarmed around the test
    body's own file work (the fake CLI writing its file)."""

    def __init__(self, monkeypatch):
        import shutil
        self.armed = False
        for owner, name in ((os, "mkdir"), (os, "chmod"), (os, "scandir"),
                            (shutil, "rmtree"), (Path, "read_text"), (Path, "exists")):
            monkeypatch.setattr(owner, name, self._guarded(getattr(owner, name), name))

    def _guarded(self, real, name):
        def call(*a, **kw):
            if self.armed:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError(f"{name} ran on the event loop")
            return real(*a, **kw)
        return call


def test_start_poll_and_finish_do_their_file_work_off_the_loop(rig, monkeypatch):
    guard = _LoopFileGuard(monkeypatch)

    async def scenario():
        guard.armed = True
        ra, pa = await _connected(rig, A)          # the sweep, the home
        assert (await _status(ra["login_id"], A))["status"] == "pending"
        guard.armed = False
        pa.complete(_blob("A"))
        guard.armed = True
        assert (await _status(ra["login_id"], A))["status"] == "completed"
        await _finish(ra["login_id"], A)           # the read, the removal
        guard.armed = False
        assert not Path(pa.env["CODEX_HOME"]).exists()
        rig.store.add_subscription.assert_called_once()

    asyncio.run(scenario())


def test_an_ended_login_and_the_sweep_remove_homes_off_the_loop(rig, monkeypatch):
    guard = _LoopFileGuard(monkeypatch)

    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.exit(1)
        guard.armed = True
        assert (await _status(ra["login_id"], A))["status"] == "failed"
        guard.armed = False
        assert not Path(pa.env["CODEX_HOME"]).exists()

        rb, pb = await _connected(rig, B)
        openai_api._active_logins[rb["login_id"]]["started_at"] -= 1201
        old = rig.base / "orphan-old"
        old.mkdir()
        stale = time.time() - 1300
        os.utime(old, (stale, stale))
        guard.armed = True
        await _connected(rig, A)                   # the sweep ends B's, removes the orphan
        guard.armed = False
        assert rb["login_id"] not in openai_api._active_logins
        assert not old.exists() and not Path(pb.env["CODEX_HOME"]).exists()

    asyncio.run(scenario())


def _poll_read_hook(monkeypatch, during_read, *, first: bool = False):
    """Run ``during_read`` while the poll's file read is in flight (before
    the read itself when ``first``) when the read runs in a worker thread.
    A read on the loop cannot overlap anything: ``during_read`` then runs
    after the poll's loop slice, or not at all when ``first``."""
    real = openai_api._whole_login_file

    def read(home):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if first:
                during_read()
                return real(home)
            out = real(home)
            during_read()
            return out
        out = real(home)
        if not first:
            asyncio.get_running_loop().call_soon(during_read)
        return out

    monkeypatch.setattr(openai_api, "_whole_login_file", read)


def test_a_cli_that_exits_during_the_poll_read_is_not_failed(rig, monkeypatch):
    """The CLI writes its file, then exits: an exit seen only after a read
    that found no file is no failure, and the home stays for the finish."""
    async def scenario():
        ra, pa = await _connected(rig, A)
        loop = asyncio.get_running_loop()

        def write_and_exit():
            pa.auth_path.write_text(json.dumps(_blob("A")))
            if threading.get_ident() == loop_thread:
                pa.returncode = 0
                return
            done = threading.Event()
            loop.call_soon_threadsafe(lambda: (setattr(pa, "returncode", 0), done.set()))
            done.wait(5)

        loop_thread = threading.get_ident()
        _poll_read_hook(monkeypatch, write_and_exit)
        assert (await _status(ra["login_id"], A))["status"] in ("pending", "completed")
        assert Path(pa.env["CODEX_HOME"]).is_dir()
        assert ra["login_id"] in openai_api._active_logins

    asyncio.run(scenario())


def test_a_poll_that_overlaps_the_finish_never_ends_the_login(rig, monkeypatch):
    """A poll whose read is in flight while the same login is finished
    answers as the finish left it (completed, or 404 once it is gone) and
    never reports it failed."""
    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.complete(_blob("A"))
        finished = asyncio.Event()

        async def finish_now():
            await _finish(ra["login_id"], A)
            finished.set()

        loop = asyncio.get_running_loop()

        def finish_during_read():
            done = threading.Event()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(finish_now()).add_done_callback(lambda _f: done.set()))
            done.wait(5)

        _poll_read_hook(monkeypatch, finish_during_read, first=True)
        try:
            answer = (await _status(ra["login_id"], A))["status"]
        except HTTPException as exc:
            answer = exc.status_code
        if not finished.is_set():
            await finish_now()
        assert answer in ("completed", 404)
        rig.store.add_subscription.assert_called_once()

    asyncio.run(scenario())


def test_a_failed_login_is_ended_when_reported(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        home = Path(pa.env["CODEX_HOME"])
        pa.exit(1)
        assert (await _status(ra["login_id"], A))["status"] == "failed"
        assert ra["login_id"] not in openai_api._active_logins
        assert not home.exists()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# A start touches only the caller's own login
# ---------------------------------------------------------------------------

def test_a_start_leaves_another_persons_login_alive(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        pa.complete(_blob("A"))            # A's file is on disk, unfinished
        rb, pb = await _connected(rig, B)
        assert pa.signals == []
        assert ra["login_id"] in openai_api._active_logins
        assert pa.auth_path.is_file()
        assert (await _status(ra["login_id"], A))["status"] == "completed"

        ra2, pa2 = await _connected(rig, A)  # A's own next start ends A's earlier login
        assert ra["login_id"] not in openai_api._active_logins
        assert not pa.auth_path.exists()
        assert pb.signals == [] and rb["login_id"] in openai_api._active_logins
        assert ra2["login_id"] in openai_api._active_logins

    asyncio.run(scenario())


def test_a_login_is_registered_before_its_output_is_read(rig):
    async def scenario():
        ta = _start(A)
        pa = await _parked(rig, 1)
        registered = [m for m in openai_api._active_logins.values() if m["user_sub"] == "user-a"]
        assert len(registered) == 1 and registered[0]["proc"] is pa
        pa.release()
        await ta

    asyncio.run(scenario())


def test_a_start_replaced_by_a_newer_one_answers_409(rig):
    async def scenario():
        ta = _start(A)
        pa = await _parked(rig, 1)
        ta2 = _start(A)
        pa2 = await _parked(rig, 2)
        with pytest.raises(HTTPException) as exc:
            await ta
        assert exc.value.status_code == 409
        assert 15 in pa.signals
        pa2.release()
        ra2 = await ta2
        assert list(openai_api._active_logins) == [ra2["login_id"]]
        assert not Path(pa.env["CODEX_HOME"]).exists()

    asyncio.run(scenario())


def test_the_sweep_removes_stale_logins_and_orphan_homes(rig):
    async def scenario():
        ra, pa = await _connected(rig, A)
        openai_api._active_logins[ra["login_id"]]["started_at"] -= 1201
        old = rig.base / "orphan-old"
        young = rig.base / "orphan-young"
        old.mkdir()
        young.mkdir()
        stale = time.time() - 1300
        os.utime(old, (stale, stale))
        await _connected(rig, B)
        assert ra["login_id"] not in openai_api._active_logins
        assert 15 in pa.signals
        assert not Path(pa.env["CODEX_HOME"]).exists()
        assert not old.exists()
        assert young.is_dir()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Human only, rate limited
# ---------------------------------------------------------------------------

def test_bearer_principals_are_refused_on_every_connect_route(rig):
    calls = [
        lambda: openai_api.oauth_start(OAuthStartRequest(owner_type="user"), user=BEARER),
        lambda: openai_api.oauth_status("some-login", user=BEARER),
        lambda: openai_api.oauth_finish(OAuthFinishRequest(login_id="some-login"), user=BEARER),
        lambda: claude_api.oauth_start(claude_api.OAuthStartRequest(owner_type="user"), user=BEARER),
        lambda: claude_api.oauth_exchange(
            claude_api.OAuthExchangeRequest(code="code", state="state"), user=BEARER,
        ),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call())
        assert exc.value.status_code == 403
        assert "not API key" in exc.value.detail
    assert rig.procs == []
    assert openai_api._active_logins == {}
    assert claude_api._oauth_states == {}
    assert rate_limiter._attempts == {}
    assert not rig.base.exists()


def test_starts_over_the_limit_are_refused(rig):
    cap = rate_limiter._rule(_BUCKET)["max"]

    async def scenario():
        for _ in range(cap):
            await _connected(rig, A)
        with pytest.raises(HTTPException) as exc:
            await openai_api.oauth_start(OAuthStartRequest(owner_type="user"), user=A)
        assert exc.value.status_code == 429
        assert exc.value.detail.startswith("Too many connect attempts: try again in ")
        assert int(exc.value.headers["Retry-After"]) >= 1
        assert len(rig.procs) == cap
        # Another person is unaffected, and the Claude start draws on its own bucket.
        await _connected(rig, B)
        out = await claude_api.oauth_start(claude_api.OAuthStartRequest(owner_type="user"), user=A)
        assert out["state"] in claude_api._oauth_states

    asyncio.run(scenario())


def test_the_connect_buckets_have_their_own_rows():
    """Each start draws on its own bucket with its own env knobs, not on the
    limiter's fallback to the login rule."""
    import config

    openai_rule = config.RATE_LIMIT_RULES["oauth_start_openai"]
    claude_rule = config.RATE_LIMIT_RULES["oauth_start_claude"]
    assert (openai_rule["max"], openai_rule["window"]) == (10, 900)
    assert (claude_rule["max"], claude_rule["window"]) == (20, 900)
    assert openai_rule["max_block"] == claude_rule["max_block"] == 3600
