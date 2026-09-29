"""Same-tick spawn spacing: scheduled fires reserve session-start slots at
least TASK_SPAWN_SPACING_SECONDS apart so a shared fire time (several 06:30
digests) cannot start every CLI + sandbox in the same second."""

import asyncio

import pytest

import config
from services.scheduler import scheduler
from services.scheduler import firing


@pytest.fixture(autouse=True)
def _reset_slot(monkeypatch):
    monkeypatch.setattr(firing, "_next_spawn_slot", 0.0)
    yield


def test_concurrent_reservations_are_spaced(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)

    async def run():
        return await asyncio.gather(*[
            scheduler._reserve_spawn_slot() for _ in range(3)
        ])

    waits = sorted(asyncio.run(run()))
    assert waits[0] == pytest.approx(0.0, abs=0.5)
    assert waits[1] == pytest.approx(10.0, abs=0.5)
    assert waits[2] == pytest.approx(20.0, abs=0.5)


def test_lone_fire_waits_zero(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)

    async def run():
        first = await scheduler._reserve_spawn_slot()
        # Simulate the herd having passed long ago: the reserved horizon is
        # behind now, so a later lone fire must start immediately.
        firing._next_spawn_slot = (
            asyncio.get_running_loop().time() - 60.0
        )
        second = await scheduler._reserve_spawn_slot()
        return first, second

    first, second = asyncio.run(run())
    assert first == pytest.approx(0.0, abs=0.5)
    assert second == pytest.approx(0.0, abs=0.5)


def test_zero_spacing_disables(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 0)

    async def run():
        return await asyncio.gather(*[
            scheduler._reserve_spawn_slot() for _ in range(5)
        ])

    assert all(w == 0.0 for w in asyncio.run(run()))


def test_gate_sleeps_the_reserved_wait(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(firing.asyncio, "sleep", fake_sleep)

    async def run():
        await scheduler._spawn_spacing_gate("dyn-a")
        await scheduler._spawn_spacing_gate("dyn-b")

    asyncio.run(run())
    # First fire starts immediately (no sleep call); second sleeps ~10s.
    assert len(slept) == 1
    assert slept[0] == pytest.approx(10.0, abs=0.5)


# ---------------------------------------------------------------------------
# The gate is fair across creators; with spacing off, jitter.
# ---------------------------------------------------------------------------

@pytest.fixture
def fair(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(firing.asyncio, "sleep", fake_sleep)

    async def creator_of(task_id):
        return task_id.split(":")[0]
    monkeypatch.setattr(firing, "_creator_of", creator_of)
    yield slept


def test_one_creators_burst_does_not_push_another_back(fair):
    released: list[str] = []

    async def fire(tid):
        await firing._spawn_spacing_gate(tid)
        released.append(tid)

    async def run():
        await asyncio.gather(*(fire(t) for t in ("a:1", "a:2", "a:3", "b:1")))

    asyncio.run(run())
    assert released == ["a:1", "b:1", "a:2", "a:3"]
    assert fair == pytest.approx([10.0, 20.0, 30.0], abs=0.5)


def test_a_cancelled_waiter_is_skipped(fair):
    released: list[str] = []

    async def fire(tid):
        await firing._spawn_spacing_gate(tid)
        released.append(tid)

    async def run():
        tasks = [asyncio.create_task(fire(t)) for t in ("a:1", "a:2", "b:1")]
        await asyncio.sleep(0)
        tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    assert released == ["a:1", "b:1"]


def test_a_failed_creator_lookup_still_fires(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)

    def boom(task_id):
        raise RuntimeError("database down")

    monkeypatch.setattr(firing.task_store, "get_dynamic_task", boom)

    async def run():
        await asyncio.wait_for(firing._spawn_spacing_gate("dyn-x"), 5)

    asyncio.run(run())


def test_spacing_off_jitters_each_fire(monkeypatch):
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 0)
    monkeypatch.setattr(firing, "_jitter", lambda lo, hi: 2.5)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(firing.asyncio, "sleep", fake_sleep)
    asyncio.run(firing._spawn_spacing_gate("dyn-j"))
    assert slept == [2.5]


# ---------------------------------------------------------------------------
# The slot is taken only by a fire that will run, one waiting fire per task,
# and the row is read again after the wait.
# ---------------------------------------------------------------------------

from services.scheduler import runner, shared  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage.agents import agent_store  # noqa: E402


@pytest.fixture
def spaced(monkeypatch, temp_db):
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    monkeypatch.setattr(config, "TASK_SPAWN_SPACING_SECONDS", 10)
    monkeypatch.setattr(firing, "_next_spawn_slot", 0.0)
    firing._spacing_pending.clear()
    shared._active_task_ids.clear()
    agent_store.create_agent("proj-a", "Project A", collaborative=True, default_scope="user")
    task_store.upsert_user("pm-1", "pm@x.test", "PM", "member")
    task_store.set_user_agent_role("pm-1", "proj-a", "contributor")
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(firing.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(config, "get_cli_model", lambda agent, layer=None: "test-model")
    yield slept
    firing._spacing_pending.clear()
    shared._active_task_ids.clear()


def _row(tid: str) -> scheduler.TaskDefinition:
    task_store.create_dynamic_task(tid, "proj-a", "f", "p", "cli", "scheduled", None, None,
                                   None, 600, "pm-1", scope="user", interval_seconds=60)
    return scheduler._row_to_task(task_store.get_dynamic_task(tid))


def test_a_refused_fire_moves_no_slot(spaced, monkeypatch):
    """Sixty fires the usage limit refuses take no slot (sixty slots are ten
    minutes of delay for everyone else's next fire)."""
    from services.billing import usage_service
    monkeypatch.setattr(usage_service, "check_user_limit",
                        lambda sub, role: {"allowed": sub != "pm-1"})
    monkeypatch.setattr(usage_service, "check_agent_limit", lambda a: {"allowed": True})
    flood = [_row(f"dyn-f{i:03d}") for i in range(60)]

    async def go():
        for t in flood:
            await runner._execute_task(t, trigger_type="scheduled")
        return await firing._reserve_spawn_slot()

    wait = asyncio.run(go())
    runs = [r for r in task_store.list_runs(limit=1000) if r.get("created_by") == "pm-1"]
    assert len(runs) == 60 and all(r["status"] == "limit_exceeded" for r in runs)
    assert spaced == [] and wait == pytest.approx(0.0, abs=0.5)


def test_a_second_fire_of_a_waiting_task_is_dropped_and_a_run_now_wins(spaced, monkeypatch):
    task = _row("dyn-wait")
    entered, gate = asyncio.Event(), asyncio.Event()
    launched: list[str] = []

    async def hold(task_id):
        entered.set()
        await gate.wait()
    monkeypatch.setattr(firing, "_spawn_spacing_gate", hold)

    async def _run(run_id, *a, **k):
        launched.append(run_id)
    monkeypatch.setattr(runner, "_run_task", _run)

    async def go():
        first = asyncio.ensure_future(runner._execute_task(task, trigger_type="scheduled"))
        await entered.wait()
        assert "dyn-wait" in firing._spacing_pending
        # A second scheduled fire while the first waits is dropped.
        assert await runner._execute_task(task, trigger_type="scheduled") == ""
        # A Run Now during the wait runs at once and reserves the task...
        manual = await runner._execute_task(task, trigger_type="manual")
        assert manual.startswith("run-") and shared._active_task_ids["dyn-wait"] == manual
        # ...so the scheduled fire, once its slot comes, yields to it.
        gate.set()
        assert await first == manual
        return launched

    assert len(asyncio.run(go())) == 1
    assert "dyn-wait" not in firing._spacing_pending


def test_a_row_deleted_or_paused_during_the_wait_never_runs(spaced, monkeypatch):
    task = _row("dyn-gone")
    paused = _row("dyn-paused")
    seen: list[str] = []

    async def gate(task_id):
        seen.append(task_id)
        if task_id == "dyn-gone":
            task_store.delete_dynamic_task(task_id)
        else:
            task_store.set_dynamic_task_enabled(task_id, False)
    monkeypatch.setattr(firing, "_spawn_spacing_gate", gate)

    async def go():
        return (await runner._execute_task(task, trigger_type="scheduled"),
                await runner._execute_task(paused, trigger_type="scheduled"))

    assert asyncio.run(go()) == ("", "")
    assert seen == ["dyn-gone", "dyn-paused"]
    assert task_store.list_runs(limit=10) == [] and shared._active_task_ids == {}


def test_an_edit_during_the_wait_is_what_runs(spaced, monkeypatch):
    task = _row("dyn-edit")
    ran: list = []

    async def gate(task_id):
        task_store.update_dynamic_task(task_id, {"prompt": "the edited prompt"})
    monkeypatch.setattr(firing, "_spawn_spacing_gate", gate)

    async def _run(run_id, session_id, task, prompt, *a, **k):
        ran.append((task.prompt, prompt))
    monkeypatch.setattr(runner, "_run_task", _run)

    async def go():
        rid = await runner._execute_task(task, trigger_type="scheduled")
        await asyncio.sleep(0)
        return rid

    assert asyncio.run(go()).startswith("run-")
    assert ran == [("the edited prompt", "the edited prompt")]
