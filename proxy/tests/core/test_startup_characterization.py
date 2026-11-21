"""Characterization of the proxy's start and stop (``startup.py``,
``startup_sweeps.py``, ``startup_shutdown.py``): the 60 s
sweep's table and its runner, what each sweep calls, the process limits, the
route warm-up, the order the lifespan runs its steps in, and the shutdown's
ordered tail. A drift is a behaviour change, and the commit that records it says
why.

Every collaborator is faked on its own module, read at call time, so nothing
here starts a real service or closes the database pool. The steps of the
lifespan are patched on ``startup``, which keeps them by name. No test arms the
exit failsafe: the suite turns it off, and the tail's tests keep a stopped timer
and a refusing ``os._exit`` behind that."""
import asyncio
import inspect
import logging
import os
import sys
import threading
from types import SimpleNamespace

import pytest

import config
import startup
from core.events import stream_pump
from tests.fixtures.characterization.golden_master import check_golden

_REAL_SLEEP = asyncio.sleep


class _Log:
    """The logger argument the shutdown's functions take: the lines they log."""

    def __init__(self):
        self.name = "char"
        self.lines: list[list[str]] = []

    def _note(self, level: str, message: str, *args, **_kw) -> None:
        self.lines.append([level, message % args if args else message])

    def info(self, message, *args, **kw):
        self._note("info", message, *args, **kw)

    def warning(self, message, *args, **kw):
        self._note("warning", message, *args, **kw)

    def error(self, message, *args, **kw):
        self._note("error", message, *args, **kw)

    def exception(self, message, *args, **kw):
        self._note("exception", message, *args, **kw)


def _logged(caplog) -> list[list[str]]:
    return [[r.levelname, r.getMessage()] for r in caplog.records if r.name == "otodock"]


def _spy(monkeypatch, module, name: str, note, *, returns=None, raises: Exception | None = None,
         label: str = ""):
    """Fake ``module.name``, sync or async as the real one is, noting each
    call as ``<name>`` or ``label`` (with its keyword arguments and any
    positional ones)."""
    def _record(args, kwargs):
        shown = label or name
        call = {"call": shown} | ({"args": list(args)} if args else {}) | ({"kwargs": kwargs} if kwargs else {})
        note(call if len(call) > 1 else shown)
        if raises is not None:
            raise raises
        return returns

    if inspect.iscoroutinefunction(getattr(module, name)):
        async def fake(*args, **kwargs):
            return _record(args, kwargs)
    else:
        def fake(*args, **kwargs):
            return _record(args, kwargs)
    monkeypatch.setattr(module, name, fake)


def _spy_sleep(monkeypatch, *, ticks: int | None = None) -> list[float]:
    """``asyncio.sleep`` that returns at once, noting each delay; after
    ``ticks`` sleeps a loop's next one ends it."""
    delays: list[float] = []

    async def _sleep(delay, *_a, **_k):
        delays.append(delay)
        if ticks is not None and len(delays) > ticks:
            raise asyncio.CancelledError
        await _REAL_SLEEP(0)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return delays


def _spy_wait_for(monkeypatch) -> list[float]:
    """``asyncio.wait_for`` as it is, noting each timeout."""
    real, timeouts = asyncio.wait_for, []

    async def _wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await real(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    return timeouts


@pytest.fixture(autouse=True)
def _no_leftovers():
    pumps = dict(stream_pump._active_pumps)
    tasks = list(startup._bg_tasks)
    stream_pump._active_pumps.clear()
    startup._bg_tasks.clear()
    yield
    stream_pump._active_pumps.clear()
    stream_pump._active_pumps.update(pumps)
    startup._bg_tasks[:] = tasks


# --- the sweep -------------------------------------------------------------

def test_the_sweep_table():
    from core.remote import install_registry, transfer_registry
    from core.session import warmup_registry
    items = startup._sweep_items(warmup_registry, install_registry, transfer_registry)
    check_golden("startup/sweep-table", {
        "items": [{"name": name, "function": fn.__name__, "coroutine": inspect.iscoroutinefunction(fn)}
                  for name, fn in items],
        "registries_are_bound": [items[0][1] is warmup_registry.sweep_stale,
                                 items[1][1] is install_registry.sweep_stale,
                                 items[2][1] is transfer_registry.sweep_stale],
        "pairs": all(isinstance(item, tuple) and len(item) == 2 for item in items),
    })


@pytest.mark.asyncio
async def test_a_tick_runs_four_at_a_time_and_a_failure_is_its_own(caplog):
    running = peak = 0
    order: list[str] = []

    def item(name: str, fail: bool = False):
        async def _run():
            nonlocal running, peak
            order.append(f"start {name}")
            running += 1
            peak = max(peak, running)
            for _ in range(3):
                await _REAL_SLEEP(0)
            running -= 1
            order.append(f"end {name}")
            if fail:
                raise RuntimeError("the sweep failed")
        return (f"char-tick {name}", _run)

    with caplog.at_level(logging.WARNING, logger="otodock"):
        ran = await startup.run_sweep_items(
            [item("a"), item("b", fail=True), item("c"), item("d"), item("e"), item("f", fail=True), item("g")])
    check_golden("startup/sweep-tick", {
        "ran": ran, "peak": peak, "order": order, "logged": _logged(caplog),
        "no_items": await startup.run_sweep_items([]),
    })


@pytest.mark.asyncio
async def test_an_item_still_running_is_skipped(caplog):
    gate = asyncio.Event()
    starts = {"hung": 0, "fast": 0}

    async def hung():
        starts["hung"] += 1
        await gate.wait()

    async def fast():
        starts["fast"] += 1
    items = [("char-skip hung", hung), ("char-skip fast", fast)]

    with caplog.at_level(logging.WARNING, logger="otodock"):
        first = asyncio.create_task(startup.run_sweep_items(items))
        for _ in range(5):
            await _REAL_SLEEP(0)
        second = await startup.run_sweep_items(items)
        after_second = dict(starts)
        gate.set()
        first_ran = await first
        third = await startup.run_sweep_items(items)
    check_golden("startup/sweep-skip", {
        "first": first_ran, "second": second, "third": third,
        "after_second": after_second, "after_third": starts, "logged": _logged(caplog),
    })


@pytest.mark.asyncio
async def test_the_loop_starts_a_tick_a_minute(monkeypatch):
    delays = _spy_sleep(monkeypatch, ticks=3)
    runs: list[str] = []

    async def item():
        runs.append("ran")
    ticks: set = set()
    loop = asyncio.create_task(startup._registry_sweep_loop([("char-loop item", item)], ticks))
    with pytest.raises(asyncio.CancelledError):
        await loop
    while ticks:
        await _REAL_SLEEP(0)
    check_golden("startup/sweep-loop", {"sleeps": delays, "runs": runs})


@pytest.mark.asyncio
async def test_what_each_sweep_calls(monkeypatch):
    from core.credentials import catalog_install_registry
    from core.session import session_manager
    from services.community import community_agent_updater
    from services.containers import idle
    from services.infra import (
        quota_monitor,
        retention,
        subscription_health,
        subscription_window_alerts,
    )
    from services.mcp import mcp_autoupdate
    from services.media import media_pipeline, preview_snapshots
    from storage import database
    from storage.files import file_tombstones_store, recover_bin_store

    calls: list = []
    _spy(monkeypatch, database, "sweep_expired_media_tokens", calls.append, returns=[])
    for module, name in (
        (media_pipeline, "sweep_host_media_cache"),
        (preview_snapshots, "sweep_orphans"), (retention, "maybe_run_daily"),
        (quota_monitor, "check_quotas"), (subscription_health, "check_subscription_health"),
        (subscription_window_alerts, "check_window_alerts"), (mcp_autoupdate, "maybe_run_weekly"),
        (community_agent_updater, "maybe_notify_updates"), (catalog_install_registry, "sweep_stale"),
        (idle, "sweep_once"),
    ):
        _spy(monkeypatch, module, name, calls.append)
    monkeypatch.setattr(recover_bin_store, "delete_expired", lambda: calls.append("recover_bin_store.delete_expired"))
    monkeypatch.setattr(file_tombstones_store, "delete_expired", lambda: calls.append("tombstones.delete_expired"))

    async def fingerprints():
        calls.append("remote_layer.run_idle_fingerprint_sweep")
    remote = {"layer": SimpleNamespace(run_idle_fingerprint_sweep=fingerprints)}
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: remote["layer"])

    def registry(name: str):
        async def sweep_stale():
            calls.append(f"{name}.sweep_stale")
        return SimpleNamespace(sweep_stale=sweep_stale)

    bodies = {}
    for name, fn in startup._sweep_items(registry("warmup"), registry("install"), registry("transfer")):
        await fn()
        bodies[name] = list(calls)
        calls.clear()
    remote["layer"] = None
    await dict(startup._sweep_items(registry("w"), registry("i"), registry("t")))["idle fingerprint sweep"]()
    bodies["idle fingerprint sweep, no remote layer"] = list(calls)
    check_golden("startup/sweep-bodies", bodies)


# --- the process limits, the warm-up, the pump callbacks ---------------------

def _nofile(monkeypatch, soft, hard, *, refuse: Exception | None = None, target: int | None = None) -> dict:
    set_calls: list[list[int]] = []

    def setrlimit(_which, limits):
        if refuse is not None:
            raise refuse
        set_calls.append(list(limits))
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        RLIMIT_NOFILE=7, RLIM_INFINITY=-1, getrlimit=lambda _which: (soft, hard), setrlimit=setrlimit))
    got = startup.raise_nofile_limit() if target is None else startup.raise_nofile_limit(target)
    return {"effective": got, "set": set_calls}


def test_the_open_file_limit(monkeypatch, caplog):
    infinite = -1
    cases = {
        "raised to the target": {"soft": 1024, "hard": 524288},
        "raised to a low hard limit, warned": {"soft": 1024, "hard": 4096},
        "raised under an unlimited hard": {"soft": 1024, "hard": infinite},
        "never lowered": {"soft": 100_000, "hard": 200_000},
        "already at the target": {"soft": 65536, "hard": 65536},
        "already unlimited": {"soft": infinite, "hard": infinite},
        "below the warning at the hard limit": {"soft": 8191, "hard": 8191},
        "the warning's own line": {"soft": 8192, "hard": 8192},
        "the kernel refuses (ValueError)": {"soft": 1024, "hard": 4096, "refuse": ValueError("no")},
        "the kernel refuses (OSError)": {"soft": 9000, "hard": 70_000, "refuse": OSError("no")},
        "another target": {"soft": 1024, "hard": 524288, "target": 2048},
    }
    got = {}
    for name, case in cases.items():
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="otodock"):
            got[name] = _nofile(monkeypatch, **case) | {"logged": _logged(caplog)}
    monkeypatch.setitem(sys.modules, "resource", None)
    got["no rlimits on this platform"] = {"effective": startup.raise_nofile_limit()}
    check_golden("startup/nofile", got)


def test_the_default_executor(monkeypatch):
    got = {}
    for name, workers in (("as configured", 7), ("never none", 0)):
        monkeypatch.setattr(config, "DEFAULT_EXECUTOR_WORKERS", workers)
        seen: list = []
        executor = startup.set_default_executor(SimpleNamespace(set_default_executor=seen.append))
        got[name] = {"max_workers": executor._max_workers, "prefix": executor._thread_name_prefix,
                     "set_on_the_loop": seen == [executor]}
        executor.shutdown(wait=False)
    check_golden("startup/default-executor", got)


def test_the_route_warm_up():
    from fastapi import FastAPI
    seen: list[list] = []

    class _Route:
        def __init__(self, name: str, *, fails: bool = False):
            self.name, self.fails = name, fails

        def matches(self, scope: dict):
            shown = {k: v for k, v in scope.items() if k != "app"}
            seen.append([self.name, shown, scope["app"] is app])
            scope["path"] = "written over by a route"    # each match has its own scope
            if self.fails:
                raise ValueError("a matcher may raise anything")

    app = SimpleNamespace(router=SimpleNamespace(routes=[_Route("a", fails=True), _Route("b")]))
    assert startup.warm_routes(app) is None
    real = FastAPI()

    @real.get("/char")
    async def _handler():
        raise AssertionError("no handler runs")
    check_golden("startup/warm-routes", {"seen": seen, "real_app": startup.warm_routes(real)})


@pytest.mark.asyncio
async def test_the_pump_callbacks(monkeypatch):
    pushed: list[dict] = []

    def pump(*, done: bool = False, consumer: bool = False):
        return SimpleNamespace(is_done=done, system_queue_consumer=consumer, system_queue=[],
                               event_queue=asyncio.Queue(), push_ws_event=lambda ev: bool(pushed.append(ev)) or True)
    live, done_pump, consumer = pump(), pump(done=True), pump(consumer=True)
    monkeypatch.setitem(stream_pump._active_pumps, "live", live)
    monkeypatch.setitem(stream_pump._active_pumps, "done", done_pump)
    monkeypatch.setitem(stream_pump._active_pumps, "consumer", consumer)
    check_golden("startup/pump-callbacks", {
        "push": {chat: startup._push_to_pump(chat, {"to": chat}) for chat in ("live", "done", "none")},
        "pushed": pushed,
        "queue": {
            "a person's message, no consumer": startup._queue_on_pump("live", "t"),
            "a system prompt, no consumer": startup._queue_on_pump("live", "t", system=True),
            "a person's message, a consumer": startup._queue_on_pump("consumer", "t"),
            "a system prompt, a consumer": startup._queue_on_pump("consumer", "t", system=True),
            "a pump that ended": startup._queue_on_pump("done", "t", system=True),
            "no pump": startup._queue_on_pump("none", "t", system=True),
        },
        "queued": {"live": live.system_queue, "consumer": consumer.system_queue, "done": done_pump.system_queue},
        "inject": {chat: startup._inject_to_pump(chat, f"event-{chat}") for chat in ("live", "done", "none")},
        "injected": {"live": live.event_queue.qsize(), "done": done_pump.event_queue.qsize()},
    })


def test_the_build_outputs_are_emptied(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    for path in ("user-mcp-configs/u/mcp.json", "sse-mcp-configs/s.json", "gateway/kept.json", "index.json"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("{}")
    startup._purge_mcp_build_outputs()
    startup._purge_mcp_build_outputs()                  # nothing left: no complaint
    check_golden("startup/purge-build-outputs", sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")))


@pytest.mark.asyncio
async def test_the_delayed_boot_tasks(monkeypatch, caplog):
    from services.phone import phone_config
    from services.scheduler import scheduler
    from services.webhooks import relay_events
    calls: list = []
    delays = _spy_sleep(monkeypatch)
    got: dict = {}
    wakes = {"woken": 2}

    async def redeliver():
        calls.append("redeliver_pending_wakes")
        if wakes["woken"] is None:
            raise RuntimeError("no store")
        return wakes["woken"]
    monkeypatch.setattr(scheduler, "redeliver_pending_wakes", redeliver)
    _spy(monkeypatch, relay_events, "sync_all", calls.append)
    # What the last process left, in the order the boot takes it up.
    from services.scheduler import continue_pass, run_recovery
    _spy(monkeypatch, continue_pass, "arm_queues", calls.append, returns=1)
    _spy(monkeypatch, continue_pass, "run_pending", calls.append, returns=0)
    _spy(monkeypatch, continue_pass, "settle_waiters", calls.append)
    _spy(monkeypatch, run_recovery, "start_pending_runs", calls.append,
         raises=RuntimeError("no store"))
    _spy(monkeypatch, scheduler, "fire_held", calls.append, returns=0)
    _spy(monkeypatch, scheduler, "fire_downtime", calls.append, returns=3)

    with caplog.at_level(logging.INFO, logger="otodock"):
        await startup._boot_continue()
        wakes["woken"] = 0
        await startup._boot_continue()
        wakes["woken"] = None
        await startup._boot_continue()
        got["what_the_last_process_left"] = {"sleeps": list(delays), "calls": list(calls),
                                             "logged": _logged(caplog)}
        delays.clear()
        calls.clear()
        caplog.clear()

        await startup._boot_relay_events()
        _spy(monkeypatch, relay_events, "sync_all", calls.append, raises=RuntimeError("no relay"))
        await startup._boot_relay_events()
        got["relay_events"] = {"sleeps": list(delays), "calls": list(calls), "logged": _logged(caplog)}
        delays.clear()
        calls.clear()
        caplog.clear()

        bringup = SimpleNamespace(startup_docker_mcps=lambda: calls.append("startup_docker_mcps"))
        await startup._docker_mcp_bringup(bringup)

        def _no_docker():
            raise RuntimeError("no docker")
        await startup._docker_mcp_bringup(SimpleNamespace(startup_docker_mcps=_no_docker))
        got["docker_bringup"] = {"calls": list(calls), "logged": _logged(caplog)}
        calls.clear()
        caplog.clear()

        answers = iter([("key", "https://relay"), ("key", ""), RuntimeError("no config")])

        async def credentials():
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        monkeypatch.setattr(phone_config, "classifier_credentials", credentials)
        _spy(monkeypatch, phone_config, "notify_phone_config_changed", calls.append)
        delays = _spy_sleep(monkeypatch, ticks=3)
        with pytest.raises(asyncio.CancelledError):
            await startup._phone_classifier_token_refresh_loop()
        got["phone_token_refresh"] = {"sleeps": list(delays), "calls": list(calls), "logged": _logged(caplog)}
    check_golden("startup/boot-tasks", got)


# --- the lifespan ----------------------------------------------------------

BOOT_STEPS = ("_boot_process", "_boot_database", "_boot_registrations", "_boot_mcps",
              "_boot_subscriptions", "_boot_services", "_boot_arm")


class _Lifespan:
    """Every step of a lifespan run faked and noted, in order."""

    def __init__(self, monkeypatch, *, raises: dict[str, Exception] | None = None):
        from core.remote import satellite_http_tunnel
        from services.apps import app_supervisor
        self.order: list = []
        self.app, self.executor = object(), object()
        raises = raises or {}
        note = self.order.append
        for name in BOOT_STEPS:
            monkeypatch.setattr(startup, name, self._step(name, raises.get(name)))
        monkeypatch.setattr(startup, "_stop_workers", self._step("_stop_workers", raises.get("_stop_workers")))
        monkeypatch.setattr(startup, "_shutdown_sessions",
                            self._step("_shutdown_sessions", raises.get("_shutdown_sessions")))
        monkeypatch.setattr(startup, "_shutdown_cleanup",
                            self._step("_shutdown_cleanup", raises.get("_shutdown_cleanup")))
        _spy(monkeypatch, stream_pump, "note_platform_ending", note)
        _spy(monkeypatch, app_supervisor, "stop_all", note, raises=raises.get("stop_all"))

        async def dispatcher_shutdown():
            note("dispatcher.shutdown")
            if "dispatcher.shutdown" in raises:
                raise raises["dispatcher.shutdown"]
        monkeypatch.setattr(satellite_http_tunnel, "get_dispatcher",
                            lambda: SimpleNamespace(shutdown=dispatcher_shutdown))
        self.wait_for_timeouts = _spy_wait_for(monkeypatch)

    def _step(self, name: str, raises: Exception | None):
        def _shown(args) -> list:
            return ["app" if a is self.app else "executor" if a is self.executor
                    else f"logger {a.name}" if isinstance(a, logging.Logger) else repr(a) for a in args]

        def enter(args):
            self.order.append({"step": name, "args": _shown(args)} if args else name)
            if raises is not None:
                raise raises

        if name in ("_boot_process", "_boot_subscriptions", "_boot_arm"):
            def fake(*args):
                enter(args)
                return self.executor if name == "_boot_process" else None
        else:
            async def fake(*args):
                enter(args)
        return fake

    async def run(self, *, while_serving=None) -> None:
        async with startup.lifespan(self.app):
            self.order.append("serving")
            if while_serving is not None:
                raise while_serving


def test_the_open_documents_hear_the_offboarding_event():
    """A person removed from an agent, lowered or deleted is judged again at
    their editor's next request: the boot subscribes the documents to the
    offboarding event beside the other subscribers."""
    source = inspect.getsource(startup._boot_registrations)
    assert "_wopi.register_offboarding()" in source
    assert source.find("_wopi.register_offboarding()") < source.find("_app_lifecycle.register()")


def test_the_session_index_loads_before_the_boot_reads_it():
    """Recovery reads a session's machine from its own record: an index loaded
    after it reads every session as unrecorded, and falls back to the pin."""
    source = inspect.getsource(startup._boot_registrations)
    load = source.find("load_session_security()")
    assert load != -1
    for reader in ("cut_turns.record_at_boot(", "run_recovery.defer_orphaned_runs("):
        assert load < source.find(reader), reader


@pytest.mark.asyncio
async def test_the_lifespan_runs_its_steps_in_order(monkeypatch, caplog):
    rig = _Lifespan(monkeypatch)
    with caplog.at_level(logging.INFO, logger="otodock"):
        await rig.run()
    check_golden("startup/lifespan-order", {
        "order": rig.order, "wait_for_timeouts": rig.wait_for_timeouts, "logged": _logged(caplog),
    })


@pytest.mark.asyncio
async def test_a_boot_step_that_raises_ends_the_boot(monkeypatch):
    got = {}
    for failing in BOOT_STEPS:
        rig = _Lifespan(monkeypatch, raises={failing: RuntimeError(f"{failing} failed")})
        with pytest.raises(RuntimeError) as raised:
            await rig.run()
        got[failing] = {"order": rig.order, "raised": str(raised.value)}
    check_golden("startup/lifespan-boot-failures", got)


@pytest.mark.asyncio
async def test_a_raise_while_serving_skips_the_shutdown(monkeypatch):
    rig = _Lifespan(monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        await rig.run(while_serving=RuntimeError("while serving"))
    check_golden("startup/lifespan-serving-raises", {"order": rig.order, "raised": str(raised.value)})


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["timeout", "tail"])
async def test_a_shutdown_step_that_fails(monkeypatch, caplog, failing):
    raises = {
        "timeout": {"_shutdown_sessions": asyncio.TimeoutError(), "stop_all": RuntimeError("no apps"),
                    "dispatcher.shutdown": RuntimeError("no tunnel")},
        "tail": {"_shutdown_cleanup": RuntimeError("the tail failed")},
    }[failing]
    rig = _Lifespan(monkeypatch, raises=raises)
    raised = None
    with caplog.at_level(logging.INFO, logger="otodock"):
        try:
            await rig.run()
        except RuntimeError as exc:
            raised = str(exc)
    check_golden(f"startup/lifespan-shutdown-{failing}", {
        "order": rig.order, "raised": raised, "logged": _logged(caplog),
    })


# --- the shutdown ----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_workers_stop_in_order(monkeypatch, caplog):
    from services.billing import license_check_worker
    from services.engines import token_fanout
    from services.infra import release_check
    from services.oauth import oauth_refresh_worker
    from services.phone import phone_health_worker
    from services.webhooks import subscription_renewer
    workers = {"oauth_refresh_worker": oauth_refresh_worker, "subscription_renewer": subscription_renewer,
               "license_check_worker": license_check_worker, "release_check": release_check,
               "phone_health_worker": phone_health_worker, "token_fanout": token_fanout}

    def stopper(label: str, order: list[str], fail: bool):
        async def stop():
            order.append(label)
            if fail:
                raise RuntimeError(f"{label} failed")
        return stop

    got = {}
    for run, fail in (("clean", False), ("each one fails", True)):
        order: list[str] = []
        for label, module in workers.items():
            monkeypatch.setattr(module, "stop_worker", stopper(label, order, fail))
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="otodock"):
            await startup._stop_workers()
        got[run] = {"order": order, "logged": _logged(caplog)}
    check_golden("startup/stop-workers", got)


class _Layer:
    def __init__(self, sids: list[str], order: list, *, fail: tuple[str, ...] = ()):
        self.sids, self.order, self.fail = sids, order, fail

    def local_session_ids(self) -> list[str]:
        return list(self.sids)

    async def close_session(self, sid: str) -> None:
        self.order.append(f"close {sid}")
        if sid in self.fail:
            raise RuntimeError("the close failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_the_sessions_close(monkeypatch, remote):
    from core.session import session_manager
    from services.meetings import meeting_orchestrator
    from services.scheduler import scheduler
    order: list = []
    _spy(monkeypatch, scheduler, "shutdown", order.append)
    _spy(monkeypatch, meeting_orchestrator, "shutdown_meetings", order.append)
    from core.session import interactive_session
    _spy(monkeypatch, interactive_session, "close_all", order.append)
    monkeypatch.setattr(session_manager, "holding_engines", lambda: {
        "engine-a": _Layer(["a-session-1", "a-session-2"], order, fail=("a-session-1",)),
        "engine-b": _Layer(["b-session-1"], order),
    })
    remote_layer = _Layer(["r-session-1"], order)
    remote_layer._sessions = {}
    monkeypatch.setattr(session_manager, "_remote_layer", remote_layer if remote else None)
    log = _Log()
    await startup._shutdown_sessions(log)
    check_golden(f"startup/shutdown-sessions-remote-{str(remote).lower()}", {"order": order, "logged": log.lines})


@pytest.mark.asyncio
async def test_remote_sessions_at_shutdown():
    order: list = []
    remote = _Layer(["claude-idle", "claude-turn", "codex-idle", "direct-1", "other-1", "unrecorded", "fails-1"],
                    order, fail=("fails-1",))
    remote._sessions = {
        "claude-idle": SimpleNamespace(execution_path="claude-code-cli", turn_active=False),
        "claude-turn": SimpleNamespace(execution_path="claude-code-cli", turn_active=True),
        "codex-idle": SimpleNamespace(execution_path="codex-cli", turn_active=False),
        "direct-1": SimpleNamespace(execution_path="direct-llm"),
        "other-1": SimpleNamespace(execution_path="no-such-engine"),
        "fails-1": SimpleNamespace(execution_path=""),
    }
    log = _Log()
    await startup._shutdown_remote_sessions(remote, log)
    check_golden("startup/shutdown-remote", {"order": order, "logged": log.lines})


class _Pump:
    def __init__(self, session_id: str, order: list, *, source_type: str = "chat", task=None,
                 fail: bool = False):
        self.session_id, self.source_type, self._task = session_id, source_type, task
        self.order, self.fail = order, fail
        self.records_engine_turn = False

    def abort(self) -> None:
        self.order.append(f"abort {self.session_id}")
        if self.fail:
            raise RuntimeError("the abort failed")


@pytest.mark.asyncio
async def test_the_open_pumps_flush(monkeypatch):
    from core.session import session_kind, session_state
    from services.scheduler import run_recovery
    order: list = []
    log = _Log()
    await startup._flush_active_pumps(log)
    nothing_open = {"order": list(order), "logged": list(log.lines)}

    async def _turn_tail():
        order.append("the pump's tail ran")
    pumps = {
        "chat-replayed": _Pump("s-replayed", order),
        "chat-run": _Pump("s-run", order, source_type=session_kind.TASK.source_type),
        "chat-open": _Pump("s-open", order, task=asyncio.create_task(_turn_tail())),
        "chat-plain": _Pump("s-plain", order),
        "chat-raises-abort": _Pump("s-raises", order, fail=True),
    }
    for chat, pump in pumps.items():
        monkeypatch.setitem(stream_pump._active_pumps, chat, pump)
    monkeypatch.setattr(run_recovery, "is_recovery_eligible", lambda chat: chat in ("chat-replayed", "chat-run"))
    _spy(monkeypatch, stream_pump, "suppress_recovery_flush", order.append)
    _spy(monkeypatch, session_state, "mark_recover_pending", order.append)
    await startup._flush_active_pumps(log)
    check_golden("startup/flush-pumps", {"nothing_open": nothing_open, "order": order, "logged": log.lines})


class _Tail:
    """Every collaborator of the shutdown's tail faked on its own module."""

    def __init__(self, monkeypatch, *, drained: bool = True, fail: bool = False):
        from core import background, loop_watchdog
        from core.events import chat_writer
        from core.layers.direct import mcp as direct_mcp
        from core.remote import satellite_connection
        from core.session import session_state
        from services.engines import subscription_pool
        from services.mcp import docker_manager
        from services.scheduler import run_recovery, scheduler
        from storage import database, pg

        order: list = []
        self.order = order
        note = order.append
        boom = RuntimeError("failed") if fail else None

        class _NoTimer:
            """Nothing below may arm a real timer, whatever the flag says."""

            def __init__(self, *_a, **_k):
                self.daemon = True

            def start(self):
                return None
        monkeypatch.setattr(threading, "Timer", _NoTimer)

        def _refuse(code):
            raise AssertionError(f"os._exit({code})")
        monkeypatch.setattr(os, "_exit", _refuse)

        # The net keeps what the next boot takes up again: a turn its machine
        # replays, a run that reopens. An app handler's run and a judge's are
        # never reopened, and neither is a run that had ended.
        from services.infra import platform_marks
        from services.scheduler import task_kinds
        monkeypatch.setattr(run_recovery, "is_recovery_eligible", lambda chat: chat == "chat-replayed")
        _spy(monkeypatch, database, "list_orphaned_runs", note, raises=boom, returns=[
            {"id": "run-replayed", "chat_id": "chat-replayed", "task_type": task_kinds.RUN_CHECK},
            {"id": "run-reopens", "chat_id": "chat-lost"},
            {"id": "run-judge", "task_type": task_kinds.RUN_CHECK},
            {"id": "run-had-ended", "completed_at": "2026-01-01T00:00:00+00:00"}])
        _spy(monkeypatch, platform_marks, "note_clean_stop", note, raises=boom)
        _spy(monkeypatch, database, "mark_orphaned_runs_failed", note, returns=2)
        _spy(monkeypatch, database, "mark_orphaned_meetings_failed", note, returns=1)
        _spy(monkeypatch, docker_manager, "end_running_compose", note, raises=boom, returns=1)
        _spy(monkeypatch, background, "cancel_all", note, raises=boom, returns=3)
        _spy(monkeypatch, scheduler, "stop", note, label="scheduler.stop")

        async def drain_persists(timeout):
            note({"call": "drain_persists", "timeout": timeout})
            if boom is not None:
                raise boom
            return drained
        monkeypatch.setattr(satellite_connection, "get_connection_manager",
                            lambda: SimpleNamespace(drain_persists=drain_persists))
        _spy(monkeypatch, chat_writer, "drain_all", note, raises=boom, returns=drained)
        _spy(monkeypatch, session_state, "flush_session_index", note, raises=boom)
        _spy(monkeypatch, loop_watchdog, "stop", note, label="loop_watchdog.stop")
        _spy(monkeypatch, subscription_pool, "close_binding_writes", note, raises=boom)
        _spy(monkeypatch, pg, "shutdown_db_executor", note)
        _spy(monkeypatch, pg, "close_pool", note)
        _spy(monkeypatch, direct_mcp, "stop_mcp_thread", note)
        self.wait_for_timeouts = _spy_wait_for(monkeypatch)

    async def run(self) -> dict:
        async def _loop_of_the_lifespan():
            try:
                await _REAL_SLEEP(3600)
            except asyncio.CancelledError:
                self.order.append("a background loop was cancelled")
                raise
        pump = _Pump("s-open", self.order)
        stream_pump._active_pumps["chat-open"] = pump
        startup._bg_tasks.append(asyncio.create_task(_loop_of_the_lifespan()))
        await _REAL_SLEEP(0)
        log = _Log()
        await startup._shutdown_cleanup(log)
        return {"order": self.order, "wait_for_timeouts": self.wait_for_timeouts, "logged": log.lines,
                "bg_tasks_left": len(startup._bg_tasks)}


@pytest.mark.asyncio
@pytest.mark.parametrize("run,kwargs", [
    ("clean", {}),
    ("not-drained", {"drained": False}),
    ("failing", {"fail": True}),
])
async def test_the_ordered_tail(monkeypatch, run, kwargs):
    rig = _Tail(monkeypatch, **kwargs)
    check_golden(f"startup/shutdown-tail-{run}", await rig.run())
