"""The pace of an app's platform calls.

Load-bearing: a page that opens gets a reserve, a page that never stops
calling gets the steady rate; one app's callers share a ceiling; a write
has a tighter bucket; a refusal says how long to wait and costs nothing;
both method routes count with one rule; the calls that run at once are
bounded, and the rest wait.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_proxy, apps as apps_api
from app import app
from auth.providers import UserContext, get_current_user
from services.apps import app_supervisor, app_tokens, call_rate, manifest as _mf
from storage import database as task_store

client = TestClient(app)
AGENT = "rate-agent"
ME = [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"},
      {"id": "n", "label": "Note", "type": "platform", "method": "notifications.create"}]


def _user(sub: str, role: str = "manager") -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=[AGENT], agent_roles={AGENT: role})


ALICE, BOB = _user("alice-sub"), _user("bob-sub", "viewer")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(call_rate, "time", c)
    apps_api._fire_rate.clear()
    call_rate.reset()
    app.dependency_overrides[get_current_user] = lambda: ALICE
    yield c
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    call_rate.reset()


@pytest.fixture
def row(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    for sub in ("alice-sub", "bob-sub"):
        task_store.upsert_user(sub, f"{sub}@test.com", sub, "member")
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    actions_json, err = _mf.validate_actions(ME, AGENT, shared=True)
    assert actions_json is not None, err
    made = task_store.upsert_app(AGENT, "", None, "board", title="Board", rel_path="workspace/apps/board",
                                 kind="folder", actions_json=actions_json)
    task_store.approve_app_actions(made["id"], task_store.manifest_sig(made), "alice-sub")
    return task_store.get_app(made["id"])


def _calls(n: int, actor: str = "user:a", method: str = "viewer.me", app_id: str = "app") -> int:
    """How many of ``n`` calls in a row pass."""
    passed = 0
    for _ in range(n):
        try:
            call_rate.platform_call(app_id, actor, method)
            passed += 1
        except call_rate.TooFast:
            pass
    return passed


def test_a_page_that_opens_has_a_reserve_and_one_that_never_stops_gets_the_steady_rate(clock):
    assert _calls(200) == int(call_rate.RESERVE)              # the opening: the whole reserve at once
    for _second in range(30):                                  # then it keeps calling, flat out
        clock.now += 1.0
        assert _calls(50) == int(call_rate.REFILL_PER_S)       # four a second, whatever it sends
    clock.now += call_rate.RESERVE / call_rate.REFILL_PER_S    # left alone, the reserve is whole again
    assert _calls(200) == int(call_rate.RESERVE)
    assert _calls(200, actor="user:b") == int(call_rate.RESERVE)   # another viewer has their own


def test_a_refusal_says_how_long_to_wait_and_takes_nothing(clock):
    _calls(int(call_rate.RESERVE))
    with pytest.raises(call_rate.TooFast) as refused:
        call_rate.platform_call("app", "user:a", "viewer.me")
    assert refused.value.retry_after == pytest.approx(1 / call_rate.REFILL_PER_S)
    assert refused.value.retry_after_ms == 250 and refused.value.retry_after_s == 1
    for _ in range(40):                                        # hammering while refused takes no token
        with pytest.raises(call_rate.TooFast):
            call_rate.platform_call("app", "user:a", "viewer.me")
    clock.now += refused.value.retry_after
    call_rate.platform_call("app", "user:a", "viewer.me")      # exactly when the first was told
    # A batch of media links costs more than one, and a cost over the reserve still passes once.
    call_rate.reset()
    call_rate.platform_call("app", "user:a", "files.media", cost=8.0)
    assert _calls(200) == int(call_rate.RESERVE) - 8
    call_rate.reset()
    call_rate.platform_call("app", "user:a", "files.media", cost=1000.0)
    assert _calls(5) == 0


def test_calls_refused_together_are_told_to_come_back_one_refill_apart(clock):
    # Told the same wait they would all return at once: one would pass and
    # the rest be refused a second time. Each gets its own moment instead.
    _calls(int(call_rate.RESERVE))
    waits = []
    for _ in range(8):
        with pytest.raises(call_rate.TooFast) as refused:
            call_rate.platform_call("app", "user:a", "viewer.me")
        waits.append(refused.value.retry_after)
    assert waits == pytest.approx([0.25 * (i + 1) for i in range(8)])
    start = clock.now
    for wait in waits:                                         # each comes back when told, and passes
        clock.now = start + wait
        call_rate.platform_call("app", "user:a", "viewer.me")
    with pytest.raises(call_rate.TooFast):
        call_rate.platform_call("app", "user:a", "viewer.me")
    # A moment that passed is forgotten: the next refusal starts from now.
    clock.now += 600.0
    _calls(int(call_rate.RESERVE))
    with pytest.raises(call_rate.TooFast) as refused:
        call_rate.platform_call("app", "user:a", "viewer.me")
    assert refused.value.retry_after == pytest.approx(0.25)
    # A batch that costs five is told when five are there, and the next call after it.
    call_rate.reset()
    _calls(int(call_rate.RESERVE))
    with pytest.raises(call_rate.TooFast) as batch:
        call_rate.platform_call("app", "user:a", "files.media", cost=5.0)
    with pytest.raises(call_rate.TooFast) as after:
        call_rate.platform_call("app", "user:a", "viewer.me")
    assert batch.value.retry_after == pytest.approx(1.25) and after.value.retry_after == pytest.approx(1.5)


def test_an_apps_callers_share_a_ceiling_that_one_viewer_cannot_empty(clock, monkeypatch):
    reserve = int(call_rate.RESERVE)
    monkeypatch.setattr(call_rate, "APP_CEILING", reserve + 40.0)
    assert _calls(200, actor="user:a") == reserve              # one viewer stops at their own reserve
    assert _calls(40, actor="user:b") == 40                    # the app's ceiling holds across viewers
    with pytest.raises(call_rate.TooFast) as refused:
        call_rate.platform_call("app", "user:c", "viewer.me")
    assert refused.value.retry_after == pytest.approx(1 / call_rate.APP_REFILL_PER_S)
    assert _calls(200, actor="user:c", app_id="other") == reserve   # another app is not touched
    # The refused viewer lost nothing of their own reserve to the app's refusal.
    clock.now += 60.0
    assert _calls(200, actor="user:c") == reserve


def test_writes_have_a_tighter_bucket_and_reads_go_on(clock):
    for method in sorted(call_rate.WRITE_METHODS):
        call_rate.reset()
        assert _calls(100, method=method) == int(call_rate.WRITE_RESERVE), method
        assert _calls(200) == int(call_rate.RESERVE - call_rate.WRITE_RESERVE)   # reads still pass
        clock.now += 1.0
        assert _calls(100, method=method) == int(call_rate.WRITE_REFILL_PER_S)
    assert {"files.read", "files.list", "files.media", "viewer.me"}.isdisjoint(call_rate.WRITE_METHODS)


def test_the_table_stays_bounded_under_a_flood_of_keys(clock):
    for i in range(call_rate._BUCKETS_HARD + 3000):
        call_rate.take(f"k{i}", 1.0, 1.0)
        clock.now += 0.001
    assert len(call_rate._buckets) <= call_rate._BUCKETS_HARD + call_rate._SWEEP_STEP + 1
    clock.now += 200.0
    call_rate.take("one-more", 1.0, 1.0)
    assert len(call_rate._buckets) < 10


def test_a_full_table_is_swept_once_a_second_or_once_a_step_of_new_keys(clock, monkeypatch):
    sweeps: list[float] = []
    sweep = call_rate._sweep
    monkeypatch.setattr(call_rate, "_sweep", lambda now: (sweeps.append(now), sweep(now))[1])
    for i in range(call_rate._BUCKETS_SOFT + 1):
        call_rate.take(f"k{i}", 1.0, 1.0)                      # past the soft bound: the first sweep
    assert len(sweeps) == 1
    for _ in range(200):
        call_rate.take("k0", 1.0, 1.0)                         # the same second, no new key: no scan
    assert len(sweeps) == 1
    for i in range(call_rate._SWEEP_STEP + 1):
        call_rate.take(f"new{i}", 1.0, 1.0)                    # a step of new keys in that second
    assert len(sweeps) == 2
    clock.now += 1.0
    call_rate.take("k0", 1.0, 1.0)                             # a second later
    assert len(sweeps) == 3


def test_the_pages_route_answers_a_refusal_with_the_wait_and_the_words_pages_know(row, clock):
    url = f"/v1/apps/{row['id']}/catalog/viewer.me"
    codes = [client.post(url, json={}).status_code for _ in range(int(call_rate.RESERVE))]
    assert codes == [200] * int(call_rate.RESERVE)
    refused = client.post(url, json={})
    assert refused.status_code == 429
    assert refused.json() == {"detail": "Too fast — try again in a moment", "retry_after_ms": 250}
    assert refused.headers["retry-after"] == "1"
    assert client.post(url, json={}).json()["retry_after_ms"] == 500   # the next one, a refill later
    clock.now += 0.25
    assert client.post(url, json={}).status_code == 200
    # Paced before any work: an app that does not exist is refused the same way.
    call_rate.reset()
    ghost = "/v1/apps/00000000-0000-0000-0000-000000000000/catalog/viewer.me"
    codes = [client.post(ghost, json={}).status_code for _ in range(int(call_rate.RESERVE) + 1)]
    assert set(codes[:-1]) == {404} and codes[-1] == 429
    assert client.post(f"/v1/apps/{row['id']}/catalog/nope", json={}).status_code == 404


def test_both_routes_count_with_one_rule_and_a_relayed_viewer_shares_their_own_reserve(row, clock):
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(AGENT),
                                   data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    claim = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    relayed = {"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": claim}
    server = f"/v1/apps/{row['id']}/platform/viewer.me"
    page = f"/v1/apps/{row['id']}/catalog/viewer.me"
    half = int(call_rate.RESERVE) // 2
    assert [client.post(page, json={}).status_code for _ in range(half)] == [200] * half
    assert [client.post(server, json={}, headers=relayed).status_code for _ in range(half)] == [200] * half
    refused = client.post(server, json={}, headers=relayed)     # the same person, the same reserve
    assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
    assert client.post(page, json={}).status_code == 429
    # The server's own calls, with no person behind them, have a reserve of their own.
    own = {"Authorization": f"Bearer {inst.token}"}
    assert client.post(server, json={}, headers=own).status_code == 200
    assert app_proxy._platform_actor({"x-otodock-viewer": "not-a-claim"}, row,
                                     app_proxy.Caller(basis="app", sub=row["id"])) == f"app:{row['id']}"
    # Another viewer is untouched.
    app.dependency_overrides[get_current_user] = lambda: BOB
    assert client.post(page, json={}).status_code == 200


def _peak(calls: list[tuple[str, str]]) -> tuple[int, int, list[int]]:
    """Every call at once: the most that ran together, the most that were
    counted as waiting, and the order they ended in."""
    running = peak = waited = 0
    order: list[int] = []

    async def one(i: int, app_id: str, actor: str) -> None:
        nonlocal running, peak, waited
        async with call_rate.slot(app_id, actor):
            running += 1
            peak = max(peak, running)
            waited = max(waited, call_rate._waiting)
            await asyncio.sleep(0.01)
            order.append(i)
            running -= 1

    async def scene() -> None:
        await asyncio.gather(*(one(i, app_id, actor) for i, (app_id, actor) in enumerate(calls)))

    asyncio.run(scene())
    assert not call_rate._caller_gates and not call_rate._app_gates
    assert call_rate._platform_gate.idle and call_rate._waiting == 0
    return peak, waited, order


def test_one_callers_calls_run_four_at_a_time_and_the_rest_wait_their_turn(monkeypatch):
    monkeypatch.setattr(call_rate, "IN_FLIGHT_PER_APP", 6)     # so the caller's own bound is the one met
    peak, waited, order = _peak([("app", "user:a")] * 12)
    assert peak == 4 and order == sorted(order)
    # The rest are counted as waiting (seen by the first of them to get a place).
    assert waited == 12 - 4 - 1


def test_one_apps_callers_share_four_places_whoever_they_are():
    peak, _waited, _order = _peak([("app", f"user:{i}") for i in range(12)])
    assert peak == 4                                           # one app never takes the platform's eight


def test_the_platform_runs_twelve_calls_at_a_time_across_every_app():
    peak, _waited, _order = _peak([(f"app{i}", f"user:{i}") for i in range(20)])
    assert peak == 12                                          # three apps' share: two apps never fill it


def test_a_gate_left_idle_by_a_wait_that_ran_out_is_dropped(monkeypatch):
    monkeypatch.setattr(call_rate, "PLACE_WAIT_S", 0.0)        # the wait's time is up as it begins

    async def call() -> None:
        async with call_rate.slot("app", "user:a"):
            raise AssertionError("the call ran")

    async def scene() -> None:
        gate = call_rate._caller_gates["app|user:a"] = call_rate.Gate(1)
        gate.busy = 1                                          # the caller's one call runs
        waiting = asyncio.ensure_future(call())
        await asyncio.sleep(0)                                 # it stands in the caller's line
        gate.leave()                                           # the call ends in the same turn: the place is handed over
        with pytest.raises(call_rate.TooFast):
            await waiting                                      # too late for this call, which gives the place on

    asyncio.run(scene())
    assert not call_rate._caller_gates and not call_rate._app_gates   # nobody is left: the gate goes
    assert call_rate._platform_gate.idle and call_rate._waiting == 0


def test_a_call_that_gets_no_place_in_time_is_told_to_come_back(row, monkeypatch):
    monkeypatch.setattr(call_rate, "PLACE_WAIT_S", 0.05)
    call_rate._platform_gate.busy = call_rate._platform_gate.limit   # every place of the platform is held

    async def scene() -> float:
        started = asyncio.get_running_loop().time()
        with pytest.raises(call_rate.TooFast) as refused:
            async with call_rate.slot("app", "user:a"):
                raise AssertionError("the call ran")
        assert asyncio.get_running_loop().time() - started < 1.0   # it waited the bound, no longer
        return refused.value.retry_after

    assert asyncio.run(scene()) >= call_rate.LINE_WAIT_S
    assert not call_rate._caller_gates and not call_rate._app_gates
    assert not call_rate._platform_gate.waiters and call_rate._waiting == 0
    # Both routes answer it as any refusal of the pace.
    page = client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={})
    assert page.status_code == 429 and page.json()["retry_after_ms"] == 1005   # a step after the call before it
    assert page.json()["detail"] == call_rate.TOO_FAST and page.headers["retry-after"] == "2"
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(AGENT),
                                   data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    claim = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    server = client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={},
                         headers={"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": claim})
    assert server.status_code == 429 and server.headers["retry-after"] == "2"
    call_rate._platform_gate.busy = 0
    assert client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={}).status_code == 200


def test_the_waiting_lines_stay_well_under_the_servers_connection_bound(monkeypatch):
    from api.apps import app_media
    assert call_rate.WAITING_PLATFORM == call_rate.line_bound() == app_media.WAITING_PLATFORM
    for limit in (0, 8, 100, 4000, 20000):
        monkeypatch.setattr(config, "HTTP_LIMIT_CONCURRENCY", limit)
        bound = call_rate.line_bound()
        # A waiting request holds its connection: both lines together take half of them at most.
        assert 1 <= bound and 2 * bound <= (limit or 4000) // 2


def test_the_platforms_line_has_an_end_and_a_call_past_it_is_told_its_own_moment(row, clock, monkeypatch):
    monkeypatch.setattr(call_rate, "WAITING_PLATFORM", 2)
    monkeypatch.setattr(call_rate, "_waiting", 2)              # two admitted calls wait for a place
    waits = []
    for _ in range(3):
        with pytest.raises(call_rate.TooFast) as refused:
            call_rate.platform_call("app", "user:a", "viewer.me")
        waits.append(refused.value.retry_after)
    assert waits[0] == pytest.approx(call_rate.LINE_WAIT_S)
    assert waits[1] - waits[0] == pytest.approx(call_rate.LINE_STEP_S)
    assert waits[2] - waits[1] == pytest.approx(call_rate.LINE_STEP_S)
    answer = client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={})
    assert answer.status_code == 429 and answer.json()["retry_after_ms"] >= 1000
    monkeypatch.setattr(call_rate, "_waiting", 1)              # a place in the line again
    assert _calls(int(call_rate.RESERVE)) == int(call_rate.RESERVE)   # the refusals took nothing


def test_a_waiter_that_is_cancelled_gives_its_place_on():
    async def scene() -> None:
        gate = call_rate.Gate(1)
        await gate.enter()
        waiting = [asyncio.create_task(gate.enter()) for _ in range(2)]
        await asyncio.sleep(0)
        waiting[0].cancel()
        await asyncio.sleep(0)
        gate.leave()                                           # the place goes to the second waiter
        await asyncio.wait_for(waiting[1], 1)
        assert gate.busy == 1 and not gate.waiters
        # Cancelled in the very moment the place was handed over: it is not lost.
        late = asyncio.create_task(gate.enter())
        await asyncio.sleep(0)
        gate.leave()
        late.cancel()
        with pytest.raises(asyncio.CancelledError):
            await late
        assert gate.idle

    asyncio.run(scene())
