"""The load harness's instruments, the production-shaped loop, the real
server in process and the plumbing of the helper processes. Imported lazily
by the tests, so a skipped run imports nothing heavy."""

import asyncio
import contextlib
import gc
import json
import logging
import logging.handlers
import math
import os
import queue
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE / "_client.py"
FEEDER = HERE / "_feeder.py"
RELAY = HERE / "_pg_relay.py"

# The acceptance values (CONCURRENCY.md "The 100-session profile").
STREAM_P99_S = 0.020
STREAM_MAX_S = 0.100
# ``done`` is sent once the turn's rows have landed: the viewer path after
# that, and the whole path from the stream's end through the durable write.
DONE_AFTER_ROWS_S = 0.100
DONE_AFTER_END_S = 0.250
PAGE_LOAD_MAX_S = 0.050
REFUSAL_MEDIAN_S = 0.005
HEAVY_MAX_S = 0.020
# A pasted photo's frame inflate and parse run on the loop by design (about
# 25 ms for the dashboard's largest, accepted); the bound catches a regression.
PHOTO_MAX_S = 0.035
CUT_MAX_S = 0.600
FREEZE_STALL_S = 3.5
FREEZE_RECOVERY_S = 3.5
FREEZE_REFUSAL_MAX_S = 0.050
# 2,000 header deadlines expiring within a second of each other.
DEADLINE_BURST_MAX_S = 0.100
# A helper process that fell this far behind makes its run invalid.
HELPER_LAG_MAX_S = 0.200

TICK_S = 0.005

RECORDS: list[dict] = []
_HELPER_PIDS: set[int] = set()


def pct(values, p: float) -> float:
    """The p-quantile (nearest rank) of ``values``; 0.0 for none."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 2)


def dist(values) -> dict:
    return {"n": len(values), "p50_ms": ms(pct(values, 0.5)), "p99_ms": ms(pct(values, 0.99)),
            "max_ms": ms(max(values, default=0.0))}


# --------------------------------------------------------------------------
# instruments
# --------------------------------------------------------------------------

def _schedstat() -> tuple[int, int]:
    """(CPU ns, run-queue wait ns) of the calling thread."""
    try:
        run, wait, _ = Path("/proc/thread-self/schedstat").read_text().split()
        return int(run), int(wait)
    except (OSError, ValueError):
        return 0, 0


class Ticker:
    """Sleeps ``period`` on the running loop and records how late each wake
    was, with its time, on ``time.monotonic()`` (uvloop's ``loop.time()`` has
    millisecond grain, and the helpers stamp the same clock). Start and stop
    it on the loop thread: they also read that thread's CPU and run-queue
    wait."""

    def __init__(self, period: float = TICK_S):
        self.period = period
        self.samples: list[tuple[float, float]] = []
        self._stopped = False
        self._task = None
        self.t_start = self.t_end = 0.0
        self._sched0 = (0, 0)
        self.loop_cpu_s = self.runqueue_wait_s = 0.0

    async def _run(self):
        while not self._stopped:
            t0 = time.monotonic()
            await asyncio.sleep(self.period)
            now = time.monotonic()
            self.samples.append((now, max(0.0, now - t0 - self.period)))

    def start(self) -> "Ticker":
        self.t_start = time.monotonic()
        self._sched0 = _schedstat()
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    async def stop(self) -> dict:
        self._stopped = True
        await self._task
        self.t_end = time.monotonic()
        run, wait = _schedstat()
        self.loop_cpu_s = (run - self._sched0[0]) / 1e9
        self.runqueue_wait_s = (wait - self._sched0[1]) / 1e9
        return self.stats()

    def late(self, start: float | None = None, end: float | None = None) -> list[float]:
        return [lt for t, lt in self.samples
                if (start is None or t >= start) and (end is None or t <= end)]

    def over(self, bound: float) -> list[float]:
        return [lt for _t, lt in self.samples if lt > bound]

    def stats(self, start: float | None = None, end: float | None = None) -> dict:
        late = self.late(start, end)
        window = self.t_end - self.t_start
        out = {"samples": len(late), "p50_ms": ms(pct(late, 0.50)), "p99_ms": ms(pct(late, 0.99)),
               "max_ms": ms(max(late, default=0.0)),
               "over_20ms": sum(1 for x in late if x > 0.020),
               "over_100ms": sum(1 for x in late if x > 0.100)}
        if start is None and end is None and window > 0:
            out.update(window_s=round(window, 2), coverage=round(self.coverage(), 3),
                       loop_cpu_share=round(self.loop_cpu_s / window, 3),
                       runqueue_wait_ms=ms(self.runqueue_wait_s))
        return out

    def coverage(self) -> float:
        """The share of the window the ticks and their lateness account for:
        a ticker that stopped early, or ran on another loop, reads low."""
        window = self.t_end - self.t_start
        if window <= 0:
            return 0.0
        return (sum(lt for _t, lt in self.samples) + len(self.samples) * self.period) / window

    def assert_covered(self) -> None:
        assert self.coverage() >= 0.9, (
            f"the ticker covered {self.coverage():.0%} of its window: it did not "
            "measure the loop the load ran on")


class GcLog:
    """``gc.collect()`` before the window, then every generation-2 collection
    inside it (when, how long): a threshold miss that overlaps one reads as
    the collector, not the product."""

    def __init__(self):
        self.full: list[tuple[float, float]] = []
        self._t = 0.0

    def _cb(self, phase, info):
        if info.get("generation") != 2:
            return
        if phase == "start":
            self._t = time.monotonic()
        elif self._t:
            self.full.append((self._t, time.monotonic() - self._t))
            self._t = 0.0

    def __enter__(self):
        gc.collect()
        gc.callbacks.append(self._cb)
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(ValueError):
            gc.callbacks.remove(self._cb)

    def summary(self) -> dict:
        return {"gen2": len(self.full), "gen2_max_ms": ms(max((d for _t, d in self.full), default=0.0))}


_BUSY_WORDS = ("pytest", "exec(eval(sys.stdin.readline()))", "vitest", "vite build", "tsc ",
               "gradle", "next build", "docker build", "buildx", "npm run build", "npm ci")


def _own_tree() -> set[int]:
    own, pid = set(), os.getpid()
    while pid > 1:
        own.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return own | _HELPER_PIDS


def busy_processes() -> list[str]:
    """Other suites and builds running on the box (not this run's tree)."""
    own, busy = _own_tree(), []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in own:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(w in argv for w in _BUSY_WORDS):
            busy.append(f"{entry.name} {argv[:100].strip()}")
    return busy[:12]


def _counters() -> dict:
    out = {"t": time.monotonic()}
    try:
        line = Path("/proc/pressure/cpu").read_text().splitlines()[0]
        out["psi_us"] = int(dict(kv.split("=") for kv in line.split()[1:])["total"])
    except (OSError, KeyError, ValueError, IndexError):
        pass
    try:
        cpu = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        out["cpu_total"] = sum(cpu[:8])
        out["cpu_idle"] = cpu[3] + cpu[4]
        out["cpu_steal"] = cpu[7]
    except (OSError, ValueError, IndexError):
        pass
    return out


def box_snapshot() -> dict:
    snap = {"loadavg": [round(x, 2) for x in os.getloadavg()], "cpus": os.cpu_count()}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                snap["mem_available_mb"] = int(line.split()[1]) // 1024
    except OSError:
        pass
    snap["busy"] = busy_processes()
    return snap


class BoxWindow:
    """What the rest of the box did during one measured window: CPU busy and
    steal shares, the share of time some task waited for a CPU (PSI), the
    load average and the other suites or builds running."""

    def __init__(self):
        self._a = _counters()

    def close(self) -> dict:
        b = _counters()
        out = {"loadavg": [round(x, 2) for x in os.getloadavg()]}
        wall = b["t"] - self._a["t"]
        if "psi_us" in b and "psi_us" in self._a and wall > 0:
            out["psi_cpu_some_pct"] = round((b["psi_us"] - self._a["psi_us"]) / (wall * 1e6) * 100, 1)
        if "cpu_total" in b and "cpu_total" in self._a:
            total = b["cpu_total"] - self._a["cpu_total"]
            if total > 0:
                out["cpu_busy_pct"] = round(
                    100 * (total - (b["cpu_idle"] - self._a["cpu_idle"])) / total, 1)
                out["cpu_steal_pct"] = round(100 * (b["cpu_steal"] - self._a["cpu_steal"]) / total, 1)
        out["busy"] = busy_processes()
        return out


def start_watchdog() -> None:
    """The production stall watchdog on the running loop, at its shipped
    settings (reporting from 250 ms)."""
    import config
    from core import loop_watchdog

    loop_watchdog.stop()
    loop_watchdog.reset_stats()
    assert loop_watchdog.start(threshold_s=config.LOOP_WATCHDOG_THRESHOLD_S,
                               report_s=config.LOOP_WATCHDOG_REPORT_S)


def stop_watchdog() -> dict:
    """What it saw: stalls from 250 ms (``slow``), from 2 s (``stalls``), the
    histogram's non-empty buckets, its tick lateness max (its only exact
    figure below 250 ms) and the executors' queue depths."""
    from core import loop_watchdog

    wd = loop_watchdog.stats()
    loop_watchdog.stop()
    return {"slow": wd["slow"], "stalls": wd["stalls"],
            "hist": {k: v for k, v in wd["histogram_ms"].items() if v},
            "tick_late_max_ms": wd["lateness_ms"]["1h"]["max"],
            "executors": wd["executors"]}


class Window:
    """One measured window: the GC log, the box window, the production
    watchdog and the ticker, started and stopped together on the loop."""

    async def __aenter__(self) -> "Window":
        self.gc = GcLog().__enter__()
        self.box = BoxWindow()
        start_watchdog()
        self.ticker = Ticker().start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ticker.stop()
        self.watchdog = stop_watchdog()
        self.box_stats = self.box.close()
        self.gc.__exit__()

    def summary(self) -> dict:
        return {"loop": self.ticker.stats(), "watchdog": self.watchdog, "gc": self.gc.summary(),
                "box": self.box_stats}


def record(check: str, **metrics) -> dict:
    """One measured result: printed, appended to ``OTODOCK_LOADTEST_REPORT``
    when set, and kept for the terminal summary."""
    rec = {"check": check, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **metrics}
    RECORDS.append(rec)
    line = json.dumps(rec, default=str)
    print("LOADTEST " + line, flush=True)
    path = os.environ.get("OTODOCK_LOADTEST_REPORT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return rec


def summary_line(rec: dict) -> str:
    rest = {k: v for k, v in rec.items() if k not in ("check", "at")}
    return f"{rec['check']:<16} {json.dumps(rest, default=str)}"


def run_info() -> dict:
    """What a run measured: the code, the runtime, the knobs, the database's
    durability settings."""
    import config
    import uvicorn
    import uvloop

    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=HERE.parent.parent,
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "", None
    info = {
        "commit": commit, "proxy_tree_dirty": dirty,
        "python": sys.version.split()[0], "uvicorn": uvicorn.__version__, "uvloop": uvloop.__version__,
        "cpu": _cpu_model(), "cpus": os.cpu_count(),
        "knobs": {k: getattr(config, k) for k in (
            "DB_POOL_MAX_SIZE", "DB_FAST_LANE_WORKERS", "DB_BULK_LANE_WORKERS",
            "DB_LOOP_POOL_TIMEOUT_S", "DB_LOOP_BREAKER_S", "DB_LOOP_LIVENESS_PROBE_S",
            "DEFAULT_EXECUTOR_WORKERS", "HTTP_LIMIT_CONCURRENCY", "MAX_UNAUTH_BODY_BYTES",
            "MAX_JSON_BODY_BYTES", "LOOP_WATCHDOG_THRESHOLD_S", "LOOP_WATCHDOG_REPORT_S")},
    }
    from storage import pg
    with pg.get_conn() as conn:
        info["db"] = {name: conn.execute(f"SHOW {name}").fetchone()[name]
                      for name in ("server_version", "fsync", "synchronous_commit", "wal_sync_method")}
    return info


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# the production-shaped loop
# --------------------------------------------------------------------------

@contextlib.contextmanager
def production_logging(log_path: Path):
    """Root logging at INFO through production's queue handler: the record is
    prepared on the loop and written by the queue's thread, and a full queue
    drops it. pytest's capture handlers are detached for the window: they
    would also write every record on the loop."""
    from core import log_queue

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    q: queue.Queue = queue.Queue(log_queue.QUEUE_SIZE)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    listener = logging.handlers.QueueListener(q, file_handler)
    root.handlers = [log_queue._DroppingQueueHandler(q)]
    root.setLevel(logging.INFO)
    listener.start()
    try:
        yield
    finally:
        root.handlers, level = saved
        root.setLevel(level)
        listener.stop()
        file_handler.close()


@contextlib.asynccontextmanager
async def production_loop(log_dir: Path):
    """What the lifespan does that sits on a measured path (TESTING.md "The
    load-test harness" lists what is reproduced and what is left out)."""
    import startup
    from adapters import register_adapter
    from adapters.dashboard import DashboardAdapter
    from api.apps import catalog
    from core import concurrency, loop_watchdog
    from services.checks import evaluator
    from services.mcp import mcp_registry
    from storage import pg

    loop = asyncio.get_running_loop()
    default_executor = startup.set_default_executor(loop)
    register_adapter(DashboardAdapter())
    catalog.install(loop)
    evaluator.install()
    await asyncio.to_thread(mcp_registry.scan_manifests)
    concurrency.init()
    pg.arm_loop_pool()
    loop_watchdog.watch_executor("default", default_executor)
    for lane in (pg.LANE_FAST, pg.LANE_BULK):
        loop_watchdog.watch_executor(f"run_db-{lane}", pg.db_executor(lane))
    try:
        with production_logging(log_dir / "proxy.log"):
            yield
    finally:
        pg.disarm_loop_pool()
        loop_watchdog.stop()


class _Router:
    """In front of ``app.app``: answers the lifespan itself (the real one
    starts schedulers, reapers and sidecars), serves the harness's own
    WebSockets, and for the witnessed paths counts the body bytes the app
    pulled and keeps the status it sent."""

    def __init__(self, app, ws_routes: dict, witness: set[str]):
        self.app = app
        self.ws_routes = ws_routes
        self.witness = witness
        self.requests: list[dict] = []

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            while True:
                msg = await receive()
                await send({"type": msg["type"] + ".complete"})
                if msg["type"] == "lifespan.shutdown":
                    return
        path = scope.get("path", "")
        if kind == "websocket" and path in self.ws_routes:
            from starlette.websockets import WebSocket
            await self.ws_routes[path](WebSocket(scope, receive, send))
            return
        if kind == "http" and path in self.witness:
            rec = {"method": scope["method"], "path": path, "status": None, "body_bytes": 0}
            self.requests.append(rec)

            async def counted_receive():
                msg = await receive()
                if msg["type"] == "http.request":
                    rec["body_bytes"] += len(msg.get("body", b""))
                return msg

            async def witnessed_send(msg):
                if msg["type"] == "http.response.start":
                    rec["status"] = msg["status"]
                await send(msg)

            await self.app(scope, counted_receive, witnessed_send)
            return
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def serve_app(*, ws_routes: dict | None = None, witness: set[str] | None = None):
    """The production server (``app._build_server``) on 127.0.0.1, an
    ephemeral port, on the running loop. Yields ``(host, port, router)``."""
    import app as app_module
    import config
    import startup

    saved = (config.HOST, config.PORT, config.INTERNAL_LISTENER_PORT)
    config.HOST, config.PORT = "127.0.0.1", 0
    router = _Router(app_module.app, ws_routes or {}, witness or set())
    startup.warm_routes(app_module.app)
    server, socks = app_module._build_server(router)
    # The harness owns the process's signals; uvicorn's capture would turn a
    # Ctrl-C into a ten-second graceful drain.
    server.capture_signals = contextlib.nullcontext
    task = None
    try:
        task = asyncio.get_running_loop().create_task(server.serve(sockets=socks))
        async with asyncio.timeout(30):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        host, port = socks[0].getsockname()[:2]
        yield host, port, router
    finally:
        server.should_exit = True
        if task is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(30):
                    await task
        for s in socks:
            with contextlib.suppress(OSError):
                s.close()
        config.HOST, config.PORT, config.INTERNAL_LISTENER_PORT = saved


# --------------------------------------------------------------------------
# the helper processes
# --------------------------------------------------------------------------

async def spawn(script: Path, *args: str, pass_fds=()) -> asyncio.subprocess.Process:
    """Run a helper under this interpreter; its stdout is a JSON line stream,
    its stdin takes commands, and it dies with this process."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        pass_fds=tuple(pass_fds), limit=64 * 1024 * 1024,
    )
    _HELPER_PIDS.add(proc.pid)
    return proc


async def read_line(proc: asyncio.subprocess.Process, timeout: float) -> dict:
    """The helper's next JSON line; a helper that exits first is an error."""
    raw = await asyncio.wait_for(proc.stdout.readline(), timeout)
    if not raw:
        rc = await proc.wait()
        raise AssertionError(f"helper {proc.pid} exited ({rc}) before reporting")
    return json.loads(raw)


async def tell(proc: asyncio.subprocess.Process, word: str) -> None:
    proc.stdin.write(word.encode() + b"\n")
    await proc.stdin.drain()


async def end(proc: asyncio.subprocess.Process) -> None:
    """Stop a helper and reap it, whatever state it is in."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 10)
    _HELPER_PIDS.discard(proc.pid)


def kill_helpers() -> None:
    """The sync backstop: every helper this process started and did not reap."""
    for pid in list(_HELPER_PIDS):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.waitpid(pid, 0)
        _HELPER_PIDS.discard(pid)


def run_loop(main, timeout: float):
    """Run ``main()`` on a fresh uvloop, the whole scenario under
    ``timeout``: pytest-timeout's signal cannot interrupt an idle uvloop."""
    import uvloop

    async def bounded():
        async with asyncio.timeout(timeout):
            return await main()

    return asyncio.run(bounded(), loop_factory=uvloop.new_event_loop)


def calibrate() -> dict:
    """The instruments against stalls they are shown: a quiet window, a
    30 ms block the ticker must read at the grain the thresholds need, and a
    400 ms block the production watchdog must count at its 250 ms line."""

    async def main():
        start_watchdog()
        await asyncio.sleep(0.2)
        quiet = Ticker().start()
        await asyncio.sleep(2.0)
        await quiet.stop()
        quiet_wd = stop_watchdog()
        start_watchdog()
        await asyncio.sleep(0.2)
        blocked = Ticker().start()
        await asyncio.sleep(0.3)
        time.sleep(0.03)
        await asyncio.sleep(0.3)
        time.sleep(0.4)
        await asyncio.sleep(0.3)
        await blocked.stop()
        await asyncio.sleep(0.1)
        blocked_wd = stop_watchdog()
        return quiet, quiet_wd, blocked, blocked_wd

    quiet, quiet_wd, blocked, blocked_wd = run_loop(main, 60)
    small = [x for _t, x in blocked.samples if 0.025 <= x <= 0.06]
    big = [x for _t, x in blocked.samples if x >= 0.35]
    problems = []
    if quiet.coverage() < 0.9:
        problems.append(f"quiet window coverage {quiet.coverage():.0%}")
    if quiet.stats()["p99_ms"] >= 5:
        problems.append(f"quiet p99 {quiet.stats()['p99_ms']} ms (the box is too busy to measure)")
    if quiet_wd["slow"]:
        problems.append("the watchdog saw a stall in the quiet window")
    if len(small) != 1 or len(big) != 1:
        problems.append(f"the ticker read the 30 and 400 ms blocks as {len(small)} and {len(big)} samples")
    if blocked_wd["slow"] != 1:
        problems.append(f"the watchdog counted {blocked_wd['slow']} slow stalls for one 400 ms block")
    return {"quiet": quiet.stats(), "quiet_watchdog": quiet_wd, "blocked": blocked.stats(),
            "blocked_watchdog": blocked_wd, "problems": problems}
